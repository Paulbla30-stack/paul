"""The browser itself: one page, one clean profile, no memory of yesterday.

Runs in its own process under its own uid, which is the point rather than an
implementation detail. On this box -- a t3.small with 1.9 GB of RAM and about
350 MB already in use -- one Chromium tab on a heavy page can take more than
half the machine. In-process, the kernel's OOM killer would be choosing
between the browser and the agent, and it does not always pick the browser.
Out of process under `MemoryMax=`, the browser dies and the agent notices.

**The profile is thrown away every session.** No cookies, no local storage, no
saved passwords, nothing carried between one `reset` and the next. Jarvis and
I agreed on this independently and its reasoning is the better statement of
it: Paul does not want the agent *being* him online, he wants it helping him
look things up. A browser with his sessions in it is a browser that can act as
him, and every injected instruction in every page becomes an instruction with
his authority behind it. The cost is real and worth naming: nothing behind a
login can be read. If that changes it should change deliberately, per site,
and not by a cookie jar quietly filling up.

**Navigation does not click.** `follow` reads a link's href out of the
extracted page and navigates to that URL. It does not dispatch a click, so the
page's own handlers never run. Jarvis argued this against an earlier design
where I tried to sort clicks into safe and unsafe by inspecting the DOM, and
it was right that the sort cannot be made to work: an anchor with an `onclick`
looks exactly like one without until it fires. Following by href is the same
request the address bar would make, and it is structurally incapable of
running the page's script.
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from jarvis.browser import guard, trust
from jarvis.browser.page import EXTRACT_JS, Page

DEFAULT_TIMEOUT_MS = 20000
# The full Chrome build, not Playwright's headless shell. See the note on
# CHROMIUM_ARGS: only this one actually sandboxes its renderers.
CHANNEL = "chromium"
# Passed to launch(). Playwright's default is False, which means it puts
# --no-sandbox on the command line for you.
SANDBOX = True
DEFAULT_PROFILE_DIR = "/var/lib/jarvis-browser/profile"
# Downloads land in one directory, fixed here rather than chosen per request,
# for the same reason composed documents do: a caller that could choose the
# directory could choose any directory. The agent's uid can read it; the
# browser's can write it.
DEFAULT_DOWNLOAD_DIR = "/var/lib/jarvis-browser/downloads"
MAX_DOWNLOADS_REMEMBERED = 40
DEFAULT_VIEWPORT = {"width": 1280, "height": 900}
# Screenshots are for Paul's eyes in the UI, not for the model: nothing here
# sends an image to a model, and doing so would be a separate decision about
# vision that nobody has taken.
SHOT_MAX_BYTES = 4 * 1024 * 1024

# Chromium flags.
#
# --no-sandbox is NOT here, and its absence is the most important line in this
# file. It was here until Paul said "can we sandbox it, that would help with
# risk", and he was right to ask: running as an unprivileged uid is a fence
# around the browser, and it does nothing at all inside it. Every tab shares
# one process boundary, so a renderer exploit owns the whole browser including
# any other tab's cookies.
#
# With the flag gone, Chromium uses its own layered sandbox: each renderer --
# the part that actually parses hostile HTML, CSS, images and JavaScript --
# runs in its own user namespace under a seccomp-bpf filter, unable to open
# files, reach the network or see other processes. That is a kernel-enforced
# boundary around the untrusted work, which is the thing a uid cannot give you.
#
# Three things had to be true, and finding the third is why this note is long.
#
# 1. The flag must not be in CHROMIUM_ARGS. Necessary, and nowhere near
#    sufficient -- which cost an hour and a false claim in a commit message.
# 2. The unit must not carry SystemCallFilter=@system-service. Bisecting the
#    unit one property at a time, on the box, showed Chromium could not start
#    a renderer at all under it, and naming the missing syscalls back
#    individually did not recover it.
# 3. **Playwright adds --no-sandbox itself.** `chromium_sandbox` defaults to
#    False, so the flag went back on the command line no matter what this file
#    left out of its own args list. Found by reading /proc/<pid>/cmdline of a
#    live renderer, which is the only place the truth was written down:
#
#      chrome --type=renderer ... --no-sandbox --disable-dev-shm-usage ...
#
#    Every earlier check had asked the config whether the sandbox was on. The
#    config was not the thing adding the flag.
#
# So SANDBOX=True is passed explicitly at launch. The measure of success is
# not a flag or a setting; it is that a live renderer sits in its own user
# namespace rather than init's, and that is what the standing refusal and the
# deploy check both look at.
#
# user.max_user_namespaces is 7368 on this box. NoNewPrivileges=yes does not
# conflict: it blocks the old setuid helper, not the namespace sandbox.
CHROMIUM_ARGS = (
    "--disable-dev-shm-usage",        # /dev/shm is tiny here; use /tmp instead
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-extensions",
    "--mute-audio",
)


class BrowserUnavailable(RuntimeError):
    """Chromium or Playwright is not installed. Says which, and never guesses."""


class Driver:
    """One browser, one page, driven by the service above it.

    Every public method is serialised on a lock. There is one page and the
    agent is one caller, so contention is not the worry; two navigations
    interleaving and the extractor reading half of each is.
    """

    def __init__(self, logger: Optional[logging.Logger] = None,
                 timeout_ms: int = DEFAULT_TIMEOUT_MS, headless: bool = True,
                 persistent: bool = False, profile_dir: str = DEFAULT_PROFILE_DIR,
                 download_dir: str = DEFAULT_DOWNLOAD_DIR,
                 allow_secrets: bool = False):
        self.log = logger or logging.getLogger("jarvis.browser")
        self.timeout_ms = int(timeout_ms)
        self.headless = headless
        # Whether cookies survive. False is still the default and still the
        # safer shape; true is Paul's choice, taken with the trade in front of
        # him, and it is what makes trust.py's per-origin rule start applying.
        self.persistent = bool(persistent)
        self.profile_dir = profile_dir
        self.download_dir = download_dir
        # Typing into a password or payment field. Separate from the grant to
        # act, because pressing "next page" and filling in a password are not
        # the same kind of act and should not be opened by the same switch.
        self.allow_secrets = bool(allow_secrets)
        self.downloads = []
        self._lock = threading.RLock()
        # Everything that touches Playwright runs on this one thread, and the
        # single worker is the whole point rather than a performance choice.
        #
        # Playwright's sync API binds its greenlet loop to the thread that
        # started it and raises "cannot switch to a different thread" from any
        # other. The service is a ThreadingHTTPServer, so each request arrives
        # on a different thread: the first navigation worked, and the follow
        # after it failed, which is exactly the shape of bug that looks like a
        # flake and is not. Caught on the box, in the first real page load
        # after deploying, by following a link.
        #
        # The lock alone could not fix it -- it serialises access but does not
        # move the work -- so the calls are handed to a thread that owns the
        # browser for its whole life.
        self._pump = ThreadPoolExecutor(max_workers=1,
                                        thread_name_prefix="jarvis-browser")
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._last: Optional[Page] = None
        self.started_at: Optional[float] = None
        self.blocked = 0              # requests the guard refused, this session

    # ---- lifecycle ------------------------------------------------------

    def _start(self):
        """Bring Chromium up, once, on first use."""
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "playwright is not installed for this interpreter") from exc
        self._pw = sync_playwright().start()
        os.makedirs(self.download_dir, mode=0o750, exist_ok=True)
        shared = dict(viewport=dict(DEFAULT_VIEWPORT),
                      accept_downloads=True,
                      java_script_enabled=True)
        try:
            if self.persistent:
                # One object is both browser and context: a profile on disk
                # that keeps cookies, storage and logins between runs. This is
                # the half of Paul's decision that makes trust.py's per-origin
                # rule start applying, because from here on there is an
                # identity in the browser to borrow.
                os.makedirs(self.profile_dir, mode=0o700, exist_ok=True)
                self._context = self._pw.chromium.launch_persistent_context(
                    self.profile_dir, headless=self.headless, channel=CHANNEL,
                    chromium_sandbox=SANDBOX, args=list(CHROMIUM_ARGS),
                    downloads_path=self.download_dir, **shared)
                self._browser = None
            else:
                # A fresh context is a fresh profile: no storage state is
                # passed in and none is written out.
                self._browser = self._pw.chromium.launch(
                    headless=self.headless, channel=CHANNEL,
                    chromium_sandbox=SANDBOX, args=list(CHROMIUM_ARGS),
                    downloads_path=self.download_dir)
                self._context = self._browser.new_context(**shared)
        except Exception as exc:      # noqa: BLE001 - the message is the value
            self._pw.stop()
            self._pw = None
            raise BrowserUnavailable(f"chromium would not start: {exc}") from exc
        self._context.set_default_timeout(self.timeout_ms)
        self._context.route("**/*", self._screen)
        self._context.on("download", self._keep_download)
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        self.started_at = time.time()
        self.log.info("browser up: chromium, %s profile, sandbox on, %dms timeout",
                      "persistent" if self.persistent else "clean", self.timeout_ms)

    def _keep_download(self, download):
        """Put a download in the one directory and remember that it happened.

        Saved under a scrubbed basename inside download_dir and nowhere else:
        a page choosing the filename must not be able to choose the path, and
        suggested_filename comes from the page.
        """
        try:
            name = os.path.basename(str(download.suggested_filename or "download"))
            name = "".join(c for c in name if c.isalnum() or c in "._- ").strip() or "download"
            target = os.path.join(self.download_dir, name)
            if os.path.realpath(os.path.dirname(target)) != os.path.realpath(self.download_dir):
                raise OSError("download would land outside the download directory")
            download.save_as(target)
            self.downloads.append({"name": name, "path": target,
                                   "url": download.url, "at": time.time()})
            del self.downloads[:-MAX_DOWNLOADS_REMEMBERED]
            self.log.info("downloaded %s from %s", name, download.url)
        except Exception as exc:      # noqa: BLE001 - a bad download is not a crash
            self.log.warning("download not kept: %s", exc)

    def _screen(self, route, request):
        """Every request the page makes, checked before it leaves.

        Including the ones the agent never asked for. A page that loads an
        image from a private address is doing the same thing as a page that
        navigates there, and the agent is not in the loop for either.
        """
        try:
            guard.check(request.url)
        except guard.Refused as why:
            self.blocked += 1
            self.log.warning("blocked a request: %s", why)
            try:
                route.abort("blockedbyclient")
            except Exception:         # noqa: BLE001 - the abort itself can race
                pass
            return
        try:
            route.continue_()
        except Exception:             # noqa: BLE001
            pass

    def _reset(self) -> dict:
        with self._lock:
            self._close()
            self._last = None
            return {"ok": True, "reset": True}

    def close(self):
        """Shut the browser down on its own thread, then stop the thread."""
        try:
            self._on_pump(self._close)
        except Exception:            # noqa: BLE001 - shutting down is best effort
            pass

    def _close(self):
        with self._lock:
            for name in ("_context", "_browser"):
                thing = getattr(self, name)
                if thing is not None:
                    try:
                        thing.close()
                    except Exception:     # noqa: BLE001
                        pass
                setattr(self, name, None)
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:         # noqa: BLE001
                    pass
                self._pw = None
            self._page = None
            self.started_at = None

    # ---- reading --------------------------------------------------------

    def _extract(self, status=None) -> Page:
        raw = self._page.evaluate(EXTRACT_JS)
        text = str(raw.get("text") or "")
        raw["truncated"] = len(text) > 0 and len(text) > 40000
        raw["status"] = status
        raw["fetched_at"] = time.time()
        page = Page.from_dict(raw)
        self._last = page
        return page

    def _read(self) -> Page:
        with self._lock:
            self._start()
            if not self._page.url or self._page.url == "about:blank":
                return Page(url="", title="", text="", error="nothing open yet")
            return self._extract()

    # ---- moving ---------------------------------------------------------

    def _open(self, url: str) -> Page:
        # Checked here as well as in the public open(). Not belt and braces
        # for its own sake: _follow() calls this directly with an href taken
        # off the page, and when the pump refactor moved the check up to the
        # public method it took the check off the one path where the URL comes
        # from the page rather than from the agent. A test caught it before it
        # shipped. The check that matters is the one on the untrusted input.
        url = guard.check(url)
        with self._lock:
            self._start()
            status = None
            try:
                response = self._page.goto(url, wait_until="domcontentloaded",
                                           timeout=self.timeout_ms)
                status = response.status if response is not None else None
            except Exception as exc:      # noqa: BLE001
                # A timeout is not nothing: the page may have rendered most of
                # itself. Report what is there AND that it did not finish,
                # rather than throwing away a usable read.
                self.log.warning("navigation to %s: %s", url, exc)
                try:
                    page = self._extract(status=None)
                    return Page(**{**page.__dict__,
                                   "error": f"navigation did not complete: {exc}"})
                except Exception:         # noqa: BLE001
                    return Page(url=url, title="", text="",
                                error=f"navigation failed: {exc}")
            # The address the browser ended on, not the one it was given: a
            # redirect is exactly the case where those differ and exactly the
            # case where it matters.
            landed = self._page.url
            if landed and landed != url:
                guard.check(landed)
            return self._extract(status=status)

    def _follow(self, ref: str) -> Page:
        with self._lock:
            if self._last is None:
                raise ValueError("no page has been read yet")
            link = self._last.link(ref)
            if link is None:
                raise ValueError(f"no link {ref!r} on the page that was read")
            return self._open(link.href)

    # ---- acting ---------------------------------------------------------

    ACTIONS = ("click", "type", "submit", "press", "select")
    MOVES = ("back", "forward", "reload", "scroll")

    def _move(self, kind: str, amount: int = 0) -> Page:
        """Go back, go forward, reload, scroll. Reading, not acting.

        `reload` deliberately navigates to the current address rather than
        calling reload(). A reload after a form submission re-sends the POST,
        which would turn "look at that again" into doing it twice -- ordering
        the thing twice, sending the message twice. Going to the URL is what a
        person means by reload and is always a GET.
        """
        with self._lock:
            kind = (kind or "").strip().lower()
            if kind not in self.MOVES:
                raise ValueError(f"not a move: {kind!r}")
            self._start()
            if kind == "scroll":
                step = int(amount or 600)
                self._page.mouse.wheel(0, step)
            elif kind == "reload":
                here = self._page.url
                if here and here != "about:blank":
                    guard.check(here)
                    self._page.goto(here, wait_until="domcontentloaded",
                                    timeout=self.timeout_ms)
            else:
                getattr(self._page, "go_back" if kind == "back" else "go_forward")(
                    wait_until="domcontentloaded", timeout=self.timeout_ms)
            landed = self._page.url
            if landed and landed != "about:blank":
                guard.check(landed)
            return self._extract()

    def _act(self, kind: str, ref: str = "", text: str = "",
             approved=()) -> Page:
        """Click, type, submit, press a key, choose from a dropdown.

        Three fences, in order, and they answer different questions.

        1. **Is this the kind of thing this browser does at all?** A secret
           field is not typed into and a form on a page holding one is not
           submitted -- unless the operator has separately switched that on,
           which is a different decision from letting the agent act.
        2. **May it act on THIS site?** trust.py. With a clean profile the
           answer is always yes, because nothing is signed in and there is no
           identity to borrow. With a profile that stays signed in, each origin
           is approved once by Paul.
        3. **Did the page move somewhere it should not have?** The guard, on
           the address it landed on.

        All of it before Chromium is touched, except the last, which cannot be.
        """
        with self._lock:
            if self._last is None:
                raise ValueError("no page has been read yet")
            kind = (kind or "").strip().lower()
            if kind not in self.ACTIONS:
                raise ValueError(f"not an action: {kind!r}")

            target = self._last.field_by_ref(ref) if ref else None
            if kind in ("type", "select"):
                if target is None:
                    raise ValueError(f"no field {ref!r} on the page that was read")
                if target.is_secret and not self.allow_secrets:
                    raise PermissionError(
                        "that field is a secret and filling those is not "
                        "switched on (browser.allow_secrets)")

            verdict, why = trust.decide(
                self._last.url, self.persistent, approved,
                has_secret=self._last.has_password,
                secrets_unlocked=self.allow_secrets)
            if verdict != trust.ALLOW:
                raise PermissionError(why)

            self._start()
            selectors = {"click": "a, button, input[type=submit], [role=button]",
                         "type": "input, textarea, select",
                         "select": "select",
                         "press": "input, textarea, select, button, a",
                         "submit": "form"}
            index = self._index_of(ref) if ref else 0
            handle = self._page.locator(selectors[kind]).nth(index)
            if kind == "click":
                handle.click(timeout=self.timeout_ms)
            elif kind == "type":
                handle.fill(text or "", timeout=self.timeout_ms)
            elif kind == "select":
                handle.select_option(text or "", timeout=self.timeout_ms)
            elif kind == "press":
                handle.press(text or "Enter", timeout=self.timeout_ms)
            else:
                handle.evaluate("f => f.requestSubmit ? f.requestSubmit() : f.submit()")
            try:
                self._page.wait_for_load_state("domcontentloaded",
                                               timeout=self.timeout_ms)
            except Exception:             # noqa: BLE001 - not every action navigates
                pass
            landed = self._page.url
            if landed:
                guard.check(landed)
            return self._extract()

    @staticmethod
    def _index_of(ref: str) -> int:
        """L3 -> 2, F1 -> 0. The refs are one-based because people read them."""
        digits = "".join(c for c in str(ref or "") if c.isdigit())
        if not digits:
            raise ValueError(f"not a ref: {ref!r}")
        return max(0, int(digits) - 1)

    # ---- for Paul's eyes only -------------------------------------------

    def _screenshot(self) -> bytes:
        with self._lock:
            self._start()
            shot = self._page.screenshot(type="png", full_page=False)
            return shot[:SHOT_MAX_BYTES]


    # ---- everything public goes through the one thread --------------------

    def _on_pump(self, fn, *args):
        """Run one piece of browser work on the thread that owns the browser.

        The exception comes back to the caller as if it had been raised here,
        which is what keeps the refusals meaningful: a PermissionError raised
        inside _act must still be a PermissionError at the service boundary,
        not a wrapped future error that turns into a 500.
        """
        return self._pump.submit(fn, *args).result()

    def open(self, url: str) -> Page:
        """Navigate to a URL. Checked before the browser is even started."""
        url = guard.check(url)          # refused here, on the caller's thread
        return self._on_pump(self._open, url)

    def follow(self, ref: str) -> Page:
        """Navigate to a link on the current page, by its href. No click."""
        return self._on_pump(self._follow, ref)

    def read(self) -> Page:
        return self._on_pump(self._read)

    def act(self, kind: str, ref: str = "", text: str = "", approved=()) -> Page:
        """Click, type, submit, press or select. The rung is already cleared."""
        return self._on_pump(self._act, kind, ref, text, approved)

    def move(self, kind: str, amount: int = 0) -> Page:
        """Back, forward, reload or scroll. Reading, not acting."""
        return self._on_pump(self._move, kind, amount)

    def reset(self) -> dict:
        """Throw the profile away and start again with nothing."""
        return self._on_pump(self._reset)

    def screenshot(self) -> bytes:
        return self._on_pump(self._screenshot)

    def health(self) -> dict:
        with self._lock:
            up = self._page is not None
            out = {"up": up, "engine": "chromium", "blocked": self.blocked,
                   "profile": "persistent" if self.persistent else "clean per session",
                   "persistent": self.persistent,
                   # What is configured. Whether the kernel agrees is a
                   # different question and is answered by reading
                   # /proc/<pid>/ns/user for a live renderer -- see the
                   # standing refusals. Named "sandbox_requested" so nobody
                   # reads this field as proof of anything.
                   "sandbox_requested": SANDBOX and "--no-sandbox" not in CHROMIUM_ARGS,
                   "channel": CHANNEL,
                   "allow_secrets": self.allow_secrets,
                   "downloads": list(self.downloads[-10:]),
                   "download_dir": self.download_dir,
                   "since": self.started_at,
                   "url": self._last.url if self._last else ""}
            if not up:
                try:
                    import playwright                      # noqa: F401
                    out["reason"] = "not started yet"
                except ImportError:
                    out["reason"] = "playwright is not installed"
            return out
