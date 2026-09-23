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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from jarvis.browser import guard
from jarvis.browser.page import EXTRACT_JS, Page

DEFAULT_TIMEOUT_MS = 20000
DEFAULT_VIEWPORT = {"width": 1280, "height": 900}
# Screenshots are for Paul's eyes in the UI, not for the model: nothing here
# sends an image to a model, and doing so would be a separate decision about
# vision that nobody has taken.
SHOT_MAX_BYTES = 4 * 1024 * 1024

# Chromium flags. --no-sandbox is the one that needs saying: the usual reason
# to keep it is that the browser is running as a user with something to lose,
# and here it is running as a uid with no files, no credentials and no network
# beyond what the guard permits. The systemd unit is the sandbox.
CHROMIUM_ARGS = (
    "--no-sandbox",
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
                 timeout_ms: int = DEFAULT_TIMEOUT_MS, headless: bool = True):
        self.log = logger or logging.getLogger("jarvis.browser")
        self.timeout_ms = int(timeout_ms)
        self.headless = headless
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
        try:
            self._browser = self._pw.chromium.launch(
                headless=self.headless, args=list(CHROMIUM_ARGS))
        except Exception as exc:      # noqa: BLE001 - the message is the value
            self._pw.stop()
            self._pw = None
            raise BrowserUnavailable(f"chromium would not start: {exc}") from exc
        # A fresh context is a fresh profile: no storage state is passed in and
        # none is written out.
        self._context = self._browser.new_context(
            viewport=dict(DEFAULT_VIEWPORT),
            accept_downloads=False,
            java_script_enabled=True)
        self._context.set_default_timeout(self.timeout_ms)
        self._context.route("**/*", self._screen)
        self._page = self._context.new_page()
        self.started_at = time.time()
        self.log.info("browser up: chromium, clean profile, %dms timeout",
                      self.timeout_ms)

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

    def _act(self, kind: str, ref: str = "", text: str = "") -> Page:
        """Click, type or submit. The caller has already cleared the rung.

        The refusals that live here are the ones no rung lifts: a secret field
        is never typed into and a form holding one is never submitted, whoever
        is asking.
        """
        with self._lock:
            # The refusals come before the browser is touched, deliberately.
            # A fence that needs Chromium running to say no is a fence that
            # has already started doing the thing, and it is also a fence no
            # test can reach without a browser.
            if self._last is None:
                raise ValueError("no page has been read yet")
            kind = (kind or "").strip().lower()
            if kind not in ("click", "type", "submit"):
                raise ValueError(f"not an action: {kind!r}")

            if kind in ("type", "submit"):
                target = self._last.field_by_ref(ref) if ref else None
                if kind == "type":
                    if target is None:
                        raise ValueError(f"no field {ref!r} on the page that was read")
                    if target.is_secret:
                        raise PermissionError(
                            "that field is a secret; this browser never types into one")
                if self._last.has_password:
                    raise PermissionError(
                        "the page holds a password field; this browser never "
                        "submits a form on a page that does")

            self._start()
            selectors = {"click": "a, button, input[type=submit], [role=button]",
                         "type": "input, textarea, select",
                         "submit": "form"}
            index = self._index_of(ref)
            handle = self._page.locator(selectors[kind]).nth(index)
            if kind == "click":
                handle.click(timeout=self.timeout_ms)
            elif kind == "type":
                handle.fill(text or "", timeout=self.timeout_ms)
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

    def act(self, kind: str, ref: str = "", text: str = "") -> Page:
        """Click, type or submit. The caller has already cleared the rung."""
        return self._on_pump(self._act, kind, ref, text)

    def reset(self) -> dict:
        """Throw the profile away and start again with nothing."""
        return self._on_pump(self._reset)

    def screenshot(self) -> bytes:
        return self._on_pump(self._screenshot)

    def health(self) -> dict:
        with self._lock:
            up = self._page is not None
            out = {"up": up, "engine": "chromium", "blocked": self.blocked,
                   "profile": "clean per session",
                   "since": self.started_at,
                   "url": self._last.url if self._last else ""}
            if not up:
                try:
                    import playwright                      # noqa: F401
                    out["reason"] = "not started yet"
                except ImportError:
                    out["reason"] = "playwright is not installed"
            return out
