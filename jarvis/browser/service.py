"""The browser service: loopback only, token on every call, one page.

Why this is a service and not a module the agent imports:

  * **Memory.** It gets its own systemd unit and its own ``MemoryMax``, so a
    heavy page kills the browser rather than the agent. On a t3.small that is
    not a theoretical ordering.
  * **Uid.** It runs as ``jarvis-browser``, which can read no part of
    ``/var/lib/jarvis`` or ``/etc/jarvis``. A page that owns this process has
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

from jarvis.browser import guard
from jarvis.browser.driver import BrowserUnavailable, Driver

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8477
DEFAULT_TOKEN_FILE = "/run/jarvis-browser/token"
MAX_BODY = 64 * 1024


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
        self.log = logger or logging.getLogger("jarvis.browser.service")
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
                return 200, self.driver.act(str(body.get("kind") or ""),
                                            str(body.get("ref") or ""),
                                            str(body.get("text") or "")).as_dict()
            if method == "POST" and path == "/reset":
                return 200, self.driver.reset()
            return 404, {"error": f"no route {method} {path}"}
        except guard.Refused as why:
            # 403 rather than 400: this is a refusal, and the agent's own
            # refusal vocabulary should be able to tell the two apart.
            return 403, {"error": str(why), "reason": why.reason, "url": why.url}
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
    ap.add_argument("--host", default=os.environ.get("JARVIS_BROWSER_HOST", DEFAULT_HOST))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("JARVIS_BROWSER_PORT", DEFAULT_PORT)))
    ap.add_argument("--token-file", default=os.environ.get(
        "JARVIS_BROWSER_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    ap.add_argument("--timeout-ms", type=int, default=20000)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    log = logging.getLogger("jarvis.browser")

    if args.host not in ("127.0.0.1", "::1", "localhost"):
        # Not a preference. This process drives a browser with no sandbox and
        # answers to a bearer token; on any other interface that is an
        # internet-facing remote-control endpoint.
        log.error("refusing to listen on %s: loopback only", args.host)
        return 2

    service = BrowserService(driver=Driver(logger=log, timeout_ms=args.timeout_ms),
                             token=_load_token(args.token_file),
                             logger=log, host=args.host, port=args.port)
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
