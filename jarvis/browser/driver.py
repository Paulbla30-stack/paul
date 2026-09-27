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
from concurrent.futures import TimeoutError as _FutureTimeout
from typing import Optional

from jarvis.browser import guard, publish, trust
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
# How long one piece of browser work may hold the pump before the browser is
# declared wedged. Each Playwright step is bounded by the navigation timeout,
# but evaluate() is not, and a page spinning its main thread can hold it for
# ever -- and with it every later request, /health and shutdown. An action is
# at most a press, a wait for the load and a read, so three navigation
# timeouts (and never less than a minute) is slow rather than stuck.
MIN_PUMP_TIMEOUT_S = 60.0
PUMP_TIMEOUTS_PER_CALL = 3
# The most /health will wait for its liveness round trip, and only when the
# pump is idle. Past it, health answers with what it already knows.
HEALTH_PROBE_S = 1.0

# What Playwright says when the thing it was talking to has gone: Chromium
# OOM-killed under MemoryMax, or crashed.
_GONE = ("has been closed", "target closed", "browser closed",
         "connection closed", "browser has disconnected")

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


class NeedsApproval(PermissionError):
    """Not a refusal: a thing the agent may do once Paul has said yes.

    Two gates raise it and the caller has to be able to tell them apart from
    each other and from a plain refusal, because they become different cards:

      gate="origin"   the browser stays signed in and this site is not yet
                      approved for acting (trust.py)
      gate="publish"  this action would SAY something -- post, send, submit,
                      buy -- and Paul's standing rule is that those wait for
                      him whatever else has been granted (publish.py)
    """

    def __init__(self, gate: str, detail: str):
        self.gate, self.detail = gate, detail
        super().__init__(f"needs approval ({gate}): {detail}")


class Driver:
    """One browser, one page, driven by the service above it.

    Every public method but health() is serialised on a lock. There is one
    page and the agent is one caller, so contention is not the worry; two
    navigations interleaving and the extractor reading half of each is.
    """

    def __init__(self, logger: Optional[logging.Logger] = None,
                 timeout_ms: int = DEFAULT_TIMEOUT_MS, headless: bool = True,
                 persistent: bool = False, profile_dir: str = DEFAULT_PROFILE_DIR,
                 download_dir: str = DEFAULT_DOWNLOAD_DIR,
                 allow_secrets: bool = False,
                 pump_timeout_s: Optional[float] = None):
        self.log = logger or logging.getLogger("jarvis.browser")
        self.timeout_ms = int(timeout_ms)
        self.pump_timeout_s = float(pump_timeout_s) if pump_timeout_s else max(
            MIN_PUMP_TIMEOUT_S, PUMP_TIMEOUTS_PER_CALL * self.timeout_ms / 1000.0)
        # Called once when the pump is declared wedged. The service sets it to
        # exit the process, so systemd (Restart=always) brings up a fresh one
        # and takes Chromium down with the old cgroup. Nothing in-process can
        # unstick a thread blocked inside Playwright.
        self.on_wedged = None
        self._wedged = False
        # Set by Playwright's close/disconnected events, which fire on the
        # pump thread while it is doing something. Read by health() and by
        # _start(), which starts a new browser rather than reuse a dead one.
        self._dead = False
        self.last_death = ""
        self._busy_since: Optional[float] = None
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
        # Text fields the agent has typed into since it last opened an
        # address. The page reports composed text it can see; this covers the
        # case where the agent typed and the page's own report is stale. Back,
        # forward and reload do not clear it: the text can come back with the
        # page, so only a fresh open does.
        self._typed_composing = set()
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

    def _alive(self) -> bool:
        """Whether the page we hold still has a browser behind it.

        is_closed() and is_connected() only change when Playwright next hears
        from the browser, and between calls it hears nothing: after Chromium
        was killed on a test box both still said alive. So one round trip to
        the browser process (not the page, which may be busy) comes first. On
        a dead browser it fails at once, and the disconnected event fires.
        """
        if self._dead:
            return False
        try:
            self._context.cookies()
            if self._page.is_closed():
                return False
            if self._browser is not None and not self._browser.is_connected():
                return False
        except Exception:             # noqa: BLE001 - asking a dead thing can throw
            return False
        return not self._dead

    def _on_gone(self, *_):
        self._dead = True

    def _probe(self):
        """On the pump, for health(): None when nothing is started."""
        if self._page is None:
            return None
        return self._alive()

    def _start(self):
        """Bring Chromium up on first use, and again if it has died.

        A Chromium OOM-killed under MemoryMax leaves self._page set to a page
        with nothing behind it. Returning early on "a page exists" meant it
        was never started again and every request failed until someone
        restarted the service by hand.
        """
        if self._page is not None:
            if self._alive():
                return
            self.log.warning("the browser had died; starting it again")
            self._close()
            self._last = None
            self._typed_composing.clear()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "playwright is not installed for this interpreter") from exc
        # The directories first, before there is a Playwright to leak: made
        # after start(), a failure here left a node driver running per call,
        # and a PermissionError came back as a 403 "refused by the browser"
        # when the browser was simply unable to start.
        try:
            os.makedirs(self.download_dir, mode=0o750, exist_ok=True)
            if self.persistent:
                os.makedirs(self.profile_dir, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise BrowserUnavailable(
                f"the browser's directories could not be made: {exc}") from exc
        try:
            self._pw = sync_playwright().start()
        except Exception as exc:      # noqa: BLE001 - the message is the value
            self._pw = None
            raise BrowserUnavailable(f"playwright would not start: {exc}") from exc
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
            self._context.set_default_timeout(self.timeout_ms)
            self._context.route("**/*", self._screen)
            self._context.on("download", self._keep_download)
            self._context.on("close", self._on_gone)
            if self._browser is not None:
                self._browser.on("disconnected", self._on_gone)
            pages = self._context.pages
            page = pages[0] if pages else self._context.new_page()
            page.on("close", self._on_gone)
        except Exception as exc:      # noqa: BLE001 - the message is the value
            # Everything that was started is stopped, Playwright included.
            self._close()
            raise BrowserUnavailable(f"chromium would not start: {exc}") from exc
        self._page = page
        self._dead = False
        self.last_death = ""
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
            self._dead = False
            self.started_at = None

    # ---- reading --------------------------------------------------------

    def _extract(self, status=None) -> Page:
        raw = None
        for attempt in range(4):
            try:
                raw = self._page.evaluate(EXTRACT_JS)
                break
            except Exception as exc:          # noqa: BLE001
                # An action that navigates tears down the document this
                # evaluate is running in. wait_for_load_state can return
                # before the new navigation has even committed -- the OLD
                # document was loaded -- so the read lands in the gap. Seen
                # on the second live proof: the submit itself succeeded and
                # the read after it died. Wait for the new document and read
                # again; anything that is not the teardown is re-raised.
                msg = str(exc)
                if "context was destroyed" not in msg and "navigat" not in msg.lower():
                    raise
                if attempt == 3:
                    raise
                try:
                    self._page.wait_for_load_state("domcontentloaded",
                                                   timeout=self.timeout_ms)
                except Exception:             # noqa: BLE001
                    time.sleep(0.5)
        text = str(raw.get("text") or "")
        raw["truncated"] = len(text) > 0 and len(text) > 40000
        # The page reports text it can see in any box that arms the gate; what
        # the agent typed and the page no longer shows is known only here. The
        # model should be told the next press will wait for Paul either way.
        raw["composing"] = bool(raw.get("composing")) or bool(self._typed_composing)
        raw["status"] = status
        raw["fetched_at"] = time.time()
        page = Page.from_dict(raw)
        self._last = page
        return page

    def _check_landed(self, landed: str):
        """The guard on an address the browser is already at.

        Refusing the address is not enough on its own: the refused page stays
        loaded, and the next read would hand its content over anyway. So a
        refusal here also clears the page and forgets the last read before
        the refusal goes back to the caller.
        """
        try:
            guard.check(landed)
        except guard.Refused:
            self._last = None
            self._typed_composing.clear()
            try:
                self._page.goto("about:blank", timeout=self.timeout_ms)
            except Exception as exc:  # noqa: BLE001 - the refusal still stands
                self.log.warning("could not clear a refused page: %s", exc)
            raise

    def _read(self) -> Page:
        with self._lock:
            self._start()
            if not self._page.url or self._page.url == "about:blank":
                return Page(url="", title="", text="", error="nothing open yet")
            self._check_landed(self._page.url)
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
            self._typed_composing.clear()
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
                    # Where it got to before failing may be somewhere it may
                    # not be, or Chrome's own error page. Either way the check
                    # clears it, and the read below is not made.
                    landed = self._page.url
                    if landed and landed not in (url, "about:blank"):
                        self._check_landed(landed)
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
                self._check_landed(landed)
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
            # No disarming here. Chromium restores a typed box on back and
            # forward, and clearing on the move let type, back, forward, press
            # send the text with nobody asked.
            if kind == "scroll":
                step = int(amount or 600)
                self._page.mouse.wheel(0, step)
            elif kind == "reload":
                here = self._page.url
                if here and here != "about:blank":
                    self._check_landed(here)
                    self._page.goto(here, wait_until="domcontentloaded",
                                    timeout=self.timeout_ms)
            else:
                getattr(self._page, "go_back" if kind == "back" else "go_forward")(
                    wait_until="domcontentloaded", timeout=self.timeout_ms)
            landed = self._page.url
            if landed and landed != "about:blank":
                self._check_landed(landed)
            return self._extract()

    def _act(self, kind: str, ref: str = "", text: str = "",
             approved=(), operator: bool = False) -> Page:
        """Click, type, submit, press a key, choose from a dropdown.

        Four fences, in order, and they answer different questions.

        1. **Is this the kind of thing this browser does at all?** A secret
           field is not typed into and a form on a page holding one is not
           submitted -- unless the operator has separately switched that on.
           This one applies to Paul too.
        2. **May it act on THIS site?** trust.py. Clean profile: always yes.
           Signed-in profile: each origin approved once by Paul.
        3. **Would this SAY something?** publish.py. Paul's standing rule:
           posting, sending, submitting, buying waits for him, whatever else
           has been granted. Fails closed -- an action is publishing unless it
           is recognisably not.
        4. **Did the page move somewhere it should not have?** The guard, on
           the address it landed on.

        ``operator`` is Paul driving the tab himself. Gates 2 and 3 exist to
        keep the agent from acting AS him or FOR him without asking; when he
        is the one pressing the button there is nobody to ask, so they do not
        apply. Gate 1 does: it is a property of the browser, not a judgement
        about who is asking.

        All of it before Chromium is touched, except the last, which cannot be.
        """
        with self._lock:
            if self._last is None:
                raise ValueError("no page has been read yet")
            kind = (kind or "").strip().lower()
            if kind not in self.ACTIONS:
                raise ValueError(f"not an action: {kind!r}")
            ref = (ref or "").strip()

            field = self._last.field_by_ref(ref) if ref.startswith("F") else None
            control = self._last.control(ref) if ref.startswith("C") else None
            link = self._last.link(ref) if ref.startswith("L") else None
            if kind in ("type", "select"):
                if field is None:
                    raise ValueError(f"no field {ref!r} on the page that was read")
                if field.is_secret and not self.allow_secrets:
                    raise PermissionError(
                        "that field is a secret and filling those is not "
                        "switched on (JARVIS_BROWSER_ALLOW_SECRETS)")
            if kind == "click" and control is None and link is None:
                raise ValueError(f"no control or link {ref!r} on the page that was read")
            if kind == "press" and field is None and control is None:
                raise ValueError(f"no field or control {ref!r} to press a key on")

            if not operator:
                verdict, why = trust.decide(
                    self._last.url, self.persistent, approved,
                    has_secret=self._last.has_password,
                    secrets_unlocked=self.allow_secrets)
                if verdict == trust.NEEDS_APPROVAL:
                    raise NeedsApproval("origin", why)
                if verdict != trust.ALLOW:
                    raise PermissionError(why)

                composed = bool(self._last.composing) or bool(self._typed_composing)
                element_text = (control.text if control else link.text if link else "")
                verdict, why = publish.classify(
                    kind, element_text=element_text,
                    in_form=bool(control and control.in_form),
                    page_has_composed_text=composed,
                    field_kind=(field.kind if field else ""))
                if verdict == publish.NEEDS_APPROVAL:
                    raise NeedsApproval("publish", why)
            elif self._last.has_password and not self.allow_secrets and kind == "submit":
                raise PermissionError(
                    "the page holds a password field; this browser never "
                    "submits a form on a page that does (JARVIS_BROWSER_ALLOW_SECRETS)")

            self._start()
            # Located by the ref the extractor stamped on the element, not by
            # position. The extractor reports only SHOWN elements and the DOM
            # holds the hidden ones too, so nth() drifted: on the first live
            # proof F12 in the page's report resolved to a checkbox in the
            # DOM and the fill failed. The stamp cannot drift.
            by_ref = lambda r: self._page.locator(f'[data-jarvis-ref="{r}"]').first
            if kind == "click":
                by_ref(ref).click(timeout=self.timeout_ms)
            elif kind == "type":
                # Armed before the fill, so a fill that half-happens and then
                # throws still leaves the page armed.
                if publish.arms_page(field.kind, search=field.search):
                    self._typed_composing.add(ref)
                by_ref(ref).fill(text or "", timeout=self.timeout_ms)
            elif kind == "select":
                by_ref(ref).select_option(text or "", timeout=self.timeout_ms)
            elif kind == "press":
                by_ref(ref).press(text or "Enter", timeout=self.timeout_ms)
            else:
                form = (self._page.locator(f'[data-jarvis-form="{ref}"]').first if ref
                        else self._page.locator("form").first)
                try:
                    form.evaluate("f => f.requestSubmit ? f.requestSubmit() : f.submit()")
                except Exception as exc:      # noqa: BLE001
                    # Submitting navigates, and the navigation tears down the
                    # execution context the evaluate was running in before it
                    # returns. That is the SUCCESS case, seen on the first
                    # live proof; anything else is re-raised.
                    if "context was destroyed" not in str(exc) and "navigation" not in str(exc).lower():
                        raise
            try:
                self._page.wait_for_load_state("domcontentloaded",
                                               timeout=self.timeout_ms)
            except Exception:             # noqa: BLE001 - not every action navigates
                pass
            landed = self._page.url
            if landed:
                self._check_landed(landed)
            return self._extract()

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

        Bounded. A page that spins its main thread can hold evaluate() for
        ever, and with one worker that held every later request with it. Past
        pump_timeout_s the browser is declared wedged: this call and every
        later one answer "unavailable" at once, and on_wedged -- in the
        service, exiting so systemd starts a fresh process -- is called.
        """
        if self._wedged:
            raise BrowserUnavailable(
                "the browser stopped answering and is being restarted")
        future = self._pump.submit(self._run, fn, *args)
        try:
            return future.result(timeout=self.pump_timeout_s)
        except _FutureTimeout:
            future.cancel()
            first = not self._wedged
            self._wedged = True
            if first:
                self.log.error("browser wedged: one call held it for more than %.0fs",
                               self.pump_timeout_s)
                hook = self.on_wedged
                if hook is not None:
                    try:
                        hook()
                    except Exception as exc:  # noqa: BLE001 - still unavailable
                        self.log.error("on_wedged failed: %s", exc)
            raise BrowserUnavailable(
                f"the browser did not answer within {self.pump_timeout_s:.0f}s "
                "and is being restarted") from None

    def _run(self, fn, *args):
        """On the pump thread: one call, and what to do if Chromium has gone."""
        self._busy_since = time.time()
        try:
            return fn(*args)
        except (guard.Refused, PermissionError, ValueError, BrowserUnavailable):
            raise
        except Exception as exc:          # noqa: BLE001 - re-raised either way
            gone = self._dead or any(g in str(exc).lower() for g in _GONE)
            if self._page is not None and gone:
                # Torn down here so the next call starts a new one, and said
                # as unavailable (503) rather than as a crash (500).
                self.last_death = f"{type(exc).__name__}: {exc}"[:300]
                self.log.warning("the browser has gone: %s", exc)
                self._close()
                self._last = None
                self._typed_composing.clear()
                raise BrowserUnavailable(
                    f"the browser had stopped ({exc}); it starts again on the "
                    "next request") from exc
            raise
        finally:
            self._busy_since = None

    def open(self, url: str) -> Page:
        """Navigate to a URL. Checked before the browser is even started."""
        url = guard.check(url)          # refused here, on the caller's thread
        return self._on_pump(self._open, url)

    def follow(self, ref: str) -> Page:
        """Navigate to a link on the current page, by its href. No click."""
        return self._on_pump(self._follow, ref)

    def read(self) -> Page:
        return self._on_pump(self._read)

    def act(self, kind: str, ref: str = "", text: str = "", approved=(),
            operator: bool = False) -> Page:
        """Click, type, submit, press or select. The rung is already cleared."""
        return self._on_pump(self._act, kind, ref, text, approved, operator)

    def move(self, kind: str, amount: int = 0) -> Page:
        """Back, forward, reload or scroll. Reading, not acting."""
        return self._on_pump(self._move, kind, amount)

    def reset(self) -> dict:
        """Throw the profile away and start again with nothing."""
        return self._on_pump(self._reset)

    def screenshot(self) -> bytes:
        return self._on_pump(self._screenshot)

    def health(self) -> dict:
        """What state the browser is in, answered without waiting for it.

        Takes neither the lock nor the pump: a page that has wedged the
        browser holds both, and a health check that queues behind it cannot
        report that it is wedged. So this reads plain attributes, which may
        be a moment stale and are never blocked. Chromium dying while idle is
        seen by Playwright only when it next hears from it, so when the pump
        is idle one round trip to the browser process is made, bounded at
        HEALTH_PROBE_S. When the pump is busy that is skipped and busy_s says
        for how long.
        """
        page, busy = self._page, self._busy_since
        if page is not None and busy is None and not self._dead and not self._wedged:
            probe = self._pump.submit(self._probe)
            try:
                if probe.result(timeout=HEALTH_PROBE_S) is False:
                    self._dead = True
            except _FutureTimeout:
                probe.cancel()
            except Exception:          # noqa: BLE001 - a probe is not a verdict
                pass
        page, last, busy = self._page, self._last, self._busy_since
        up = page is not None and not self._dead and not self._wedged
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
               "busy_s": round(time.time() - busy, 1) if busy else 0,
               "url": last.url if last else ""}
        if not up:
            if self._wedged:
                out["reason"] = "the browser stopped answering and is being restarted"
            elif page is not None:
                out["reason"] = "the browser has died; it starts again on the next request"
            elif self.last_death:
                out["reason"] = ("the browser died and starts again on the next "
                                 f"request ({self.last_death})")
            else:
                try:
                    import playwright                      # noqa: F401
                    out["reason"] = "not started yet"
                except ImportError:
                    out["reason"] = "playwright is not installed"
        return out
