"""The browser service: loopback only, token on every call, one page.

Why this is a service and not a module the agent imports:

  * **Memory.** It gets its own systemd unit and its own ``MemoryMax``, so a
    heavy page kills the browser rather than the agent. On a t3.small that is
    not a theoretical ordering.
  * **Uid.** It runs as ``vigil-browser``, which can read no part of
    ``/var/lib/vigil`` or ``/etc/vigil``. A page that owns this process has
    owned a process that cannot see the ledger key, the memory store or the
    agent's token.
  * **Crash.** Chromium falls over. When it does, the agent gets a connection
    error and says so, instead of going down with it.

The token is read from a file the unit creates, and the same file is readable
by the agent's uid and nobody else. It is not a secret about the outside
world -- the socket is loopback -- but it stops any other local process from
driving Paul's browser.
"""

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from vigil.browser import guard
from vigil.browser.driver import BrowserUnavailable, Driver, NeedsApproval

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8477
DEFAULT_TOKEN_FILE = "/run/vigil-browser/token"
MAX_BODY = 64 * 1024
# The exit status when the browser has wedged. Any non-zero status would do
# under Restart=always; this one (EX_TEMPFAIL) says "try again" in the journal.
WEDGED_EXIT = 75

# The two switches that change what this browser is, read from this process's
# own environment -- /etc/default/vigil-browser, through the unit's
# EnvironmentFile -- and reported on /health. They used to sit in the agent's
# config, which never reached this process: the agent believed one thing and
# the browser did another. Now there is one place to flip each, and the agent
# reads the answer back from /health instead of from its own config.
ENV_PERSISTENT = "VIGIL_BROWSER_PERSISTENT"
ENV_ALLOW_SECRETS = "VIGIL_BROWSER_ALLOW_SECRETS"
_YES = frozenset({"1", "true", "yes", "on"})


def env_switch(name: str, environ=None) -> bool:
    """On only when it plainly says on. Unset, empty or garbled is off."""
    value = (environ if environ is not None else os.environ).get(name, "")
    return str(value).strip().lower() in _YES


def _load_token(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


class BrowserService:
    """Wraps a Driver in the smallest HTTP surface that can drive it."""

    def __init__(self, driver=None, token: str = "",
                 logger=None, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.log = logger or logging.getLogger("vigil.browser.service")
        self.driver = driver if driver is not None else Driver(logger=self.log)
        self.token = token
        self.host, self.port = host, int(port)
        self._server = None

    # ---- the routes, as plain functions so the tests need no socket ------

    def handle(self, method: str, path: str, body: dict) -> tuple:
        """(status, payload). Every error is a payload, never an exception."""
        try:
            if method == "GET" and path == "/health":
                return 200, self.driver.health()
            if method == "GET" and path == "/read":
                return 200, self.driver.read().as_dict()
            if method == "POST" and path == "/open":
                return 200, self.driver.open(str(body.get("url") or "")).as_dict()
            if method == "POST" and path == "/follow":
                return 200, self.driver.follow(str(body.get("ref") or "")).as_dict()
            if method == "POST" and path == "/act":
                # approved comes from the agent on every call rather than being
                # held here. The browser knows which origin it is on; whose
                # sites those are, and which Paul has said yes to, is the
                # agent's business and belongs where it can be ledgered.
                return 200, self.driver.act(str(body.get("kind") or ""),
                                            str(body.get("ref") or ""),
                                            str(body.get("text") or ""),
                                            body.get("approved") or (),
                                            bool(body.get("operator"))).as_dict()
            if method == "POST" and path == "/move":
                return 200, self.driver.move(str(body.get("kind") or ""),
                                             int(body.get("amount") or 0)).as_dict()
            if method == "POST" and path == "/reset":
                return 200, self.driver.reset()
            return 404, {"error": f"no route {method} {path}"}
        except guard.Refused as why:
            # 403 rather than 400: this is a refusal, and the agent's own
            # refusal vocabulary should be able to tell the two apart.
            return 403, {"error": str(why), "reason": why.reason, "url": why.url}
        except NeedsApproval as why:
            # Still 403 -- it did not happen -- but with a reason the agent
            # turns into a card for Paul rather than a failure to learn from.
            return 403, {"error": str(why), "reason": "needs-approval",
                         "gate": why.gate, "detail": why.detail}
        except PermissionError as why:
            return 403, {"error": str(why), "reason": "refused by the browser"}
        except BrowserUnavailable as why:
            return 503, {"error": str(why), "reason": "browser unavailable"}
        except ValueError as why:
            return 400, {"error": str(why)}
        except Exception as why:            # noqa: BLE001
            self.log.warning("browser service: %s: %s", type(why).__name__, why)
            return 500, {"error": f"{type(why).__name__}: {why}"}

    # ---- the socket -----------------------------------------------------

    def serve_forever(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):      # quieter than the default
                service.log.debug("%s - %s", self.address_string(), fmt % args)

            def _authorised(self) -> bool:
                if not service.token:
                    return True                     # no token configured: dev only
                sent = (self.headers.get("Authorization") or "")
                return sent.strip() == f"Bearer {service.token}"

            def _send(self, status: int, payload, raw: bytes = b"",
                      kind: str = "application/json"):
                data = raw if raw else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):                       # noqa: N802
                if not self._authorised():
                    return self._send(401, {"error": "bad token"})
                if self.path == "/shot":
                    try:
                        return self._send(200, None, raw=service.driver.screenshot(),
                                          kind="image/png")
                    except Exception as exc:        # noqa: BLE001
                        return self._send(503, {"error": str(exc)})
                status, payload = service.handle("GET", self.path, {})
                self._send(status, payload)

            def do_POST(self):                      # noqa: N802
                if not self._authorised():
                    return self._send(401, {"error": "bad token"})
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._send(413, {"error": "body too large"})
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "body is not JSON"})
                if not isinstance(body, dict):
                    return self._send(400, {"error": "body is not an object"})
                status, payload = service.handle("POST", self.path, body)
                self._send(status, payload)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.log.info("browser service on %s:%s", self.host, self.port)
        self._server.serve_forever()

    def shutdown(self):
        if self._server is not None:
            self._server.shutdown()
        self.driver.close()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=os.environ.get("VIGIL_BROWSER_HOST", DEFAULT_HOST))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("VIGIL_BROWSER_PORT", DEFAULT_PORT)))
    ap.add_argument("--token-file", default=os.environ.get(
        "VIGIL_BROWSER_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    ap.add_argument("--timeout-ms", type=int, default=20000)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    log = logging.getLogger("vigil.browser")

    if args.host not in ("127.0.0.1", "::1", "localhost"):
        # Not a preference. This process drives a browser with no sandbox and
        # answers to a bearer token; on any other interface that is an
        # internet-facing remote-control endpoint.
        log.error("refusing to listen on %s: loopback only", args.host)
        return 2

    driver = Driver(logger=log, timeout_ms=args.timeout_ms,
                    persistent=env_switch(ENV_PERSISTENT),
                    allow_secrets=env_switch(ENV_ALLOW_SECRETS))

    def _exit_for_restart():
        # A thread stuck inside Playwright cannot be unstuck from here. Exit
        # and let systemd (Restart=always) start a fresh process; stopping the
        # unit's cgroup takes Chromium with it. The short delay lets the 503
        # that reports it reach whoever asked.
        log.error("the browser is wedged; exiting so systemd starts a fresh one")
        timer = threading.Timer(1.0, os._exit, args=(WEDGED_EXIT,))
        timer.daemon = True
        timer.start()

    driver.on_wedged = _exit_for_restart
    service = BrowserService(driver=driver,
                             token=_load_token(args.token_file),
                             logger=log, host=args.host, port=args.port)
    log.info("switches: persistent=%s allow_secrets=%s (from %s and %s)",
             driver.persistent, driver.allow_secrets, ENV_PERSISTENT, ENV_ALLOW_SECRETS)
    if not service.token:
        log.warning("no token at %s: any local process can drive this browser",
                    args.token_file)
    try:
        service.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
