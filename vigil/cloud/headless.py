"""
Vigil Headless Runner

Replaces the interactive ConsoleUI when the agent is the primary process
on a server. It runs observe-plan-act-reflect cycles on a timer, writes a
status snapshot to disk, and serves the same snapshot over a loopback
HTTP endpoint so operators (or other agents) can ask "what are you doing?"
without a terminal.

Endpoints (default 127.0.0.1:8471):
    GET  /health   -> {"ok": true, ...}                      (no token)
    GET  /status   -> agent.get_status() plus runner info    (no token)
    GET  /memory   -> agent memory summary
    GET  /history  -> last 20 task results (may contain shell output)
    GET  /brain    -> LLM brain status (or {"brain": null})
    GET  /goals    -> goals with completion state
    POST /goal     -> body is the goal text (optional "?priority=N"); adds a goal
    POST /think    -> one LLM planning step, executed; returns the outcome
    POST /ask      -> body is a question; returns {"answer": ...}
    POST /chat     -> JSON {"messages": [{role, content}, ...], "from": "..."};
                      multi-turn answer. "from" is optional and names the
                      correspondent in the agent's memory of the exchange.
    GET  /marketing        -> the scout's drafts: pending (soonest to lapse
                              first), recently decided, and counts. Read-only;
                              approving is done on the emailed link, which is
                              the only thing holding the signing secret.
    GET  /documents        -> documents the agent has written, newest first
    GET  /documents/<name> -> download one, as an attachment
    POST /compose  -> {content, title, format, name} -> write one. content is
                      markdown; format is pdf, docx, md or txt.
    POST /told     -> {text, by} -> the operator states a fact the agent cannot
                      yet see. Kept apart from what the agent itself said, and
                      promoted to standing only when a reading agrees with it.
    POST /upload   -> raw file body with X-Filename; saved under the upload dir
    GET  /uploads  -> files the agent has been given
    GET  /ui       -> the web UI (chat, agent panel, uploads)   (no token; the
                      page asks for the token and sends it with every call)

A second listener (``ui`` settings) can serve the same handler on a
public address with TLS, so the UI works from a phone without a tunnel.

    GET  /lab      -> behaviour-lab dials, settings, locked layers, session
    GET  /operator -> everything the agent holds about its operator
    POST /operator/add {text, source: stated|observed, by}, /operator/forget
         {text} -> correct it; contact details are refused by construction
    GET  /questions -> what the agent has asked and what it was told
    POST /questions/ask {text, blocked_on, audience}, /questions/answer
         {id, text, by} -> the agent's side of the conversation
    POST /notify/test {note?} -> send one real message to prove the operator
         channel actually delivers, rather than only being configured
    POST /verdict {claim, ruling: held|failed|unchecked, source: operator|review,
                   by?, reason?, supersedes?} -> record a ruling from outside
    POST /lab/session {open: true|false, purpose?} -> opens or closes the lab
         window. Opening tells the agent first and only opens if that worked;
         the other lab endpoints refuse while the window is shut.
    POST /lab/settings {settings, apply_to_chat}, /lab/preview {settings},
         /lab/run {question, settings?, compare, mode: answer|plan}
    GET  /ledger, /ledger/tail?limit=N, /ledger/verify, /ledger/pubkey
                  -> Glass Ledger status, last entries, on-box verdict, key

Everything except /health and /status requires the runner token, sent as
``Authorization: Bearer <token>`` or ``X-Vigil-Token``. The token is
taken from ``cloud.status_token`` or generated at start and written 0600
to ``cloud.status_token_file`` (default /run/vigil/token). Loopback is
not a trust boundary on a multi-user host, and a goal is a root-shell
instruction once the brain has a shell, so the token is mandatory.
"""

import json
import os
import re
import secrets
import ssl
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

UI_HTML_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "ui", "web", "index.html")

# The UI page's one inline script carries this placeholder; each response
# swaps in a fresh nonce and names it in the policy, so the only script that
# can run in the page is the script that shipped with it.
UI_NONCE_PLACEHOLDER = "__VIGIL_CSP_NONCE__"


def ui_csp(nonce: str) -> str:
    """The UI page's Content-Security-Policy.

    Added on 27 September after the security review. The page renders every
    outside string as text, so there is no known way to inject script into it
    -- but the runner token is as good as root, the page is where it is used,
    and every planned feature adds another place outside text is shown. This
    is what makes one slip in one of them not the whole machine. Inline style
    stays allowed: styles cannot run code, and the page is full of them.
    """
    return ("default-src 'none'; "
            f"script-src 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' blob: data:; "
            "connect-src 'self'; "
            "form-action 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; object-src 'none'")


DEFAULT_UPLOAD_DIR = "/var/lib/vigil/uploads"
# Exactly the formats compose.py can write, and nothing that a browser would
# execute. text/html is absent on purpose.
DOCUMENT_TYPES = {
    "pdf": "application/pdf",
    "docx": ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document"),
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
}
DEFAULT_TLS_DIR = "/etc/vigil/tls"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

DEFAULT_STATUS_PORT = 8471
DEFAULT_STATUS_FILE = "/run/vigil/status.json"
DEFAULT_TOKEN_FILE = "/run/vigil/token"
# Survives a restart, unlike the runtime directory copy above.
DEFAULT_TOKEN_PERSIST_FILE = "/etc/vigil/token"
# What the loopback status API (127.0.0.1) serves without a token: the box's
# own `vigil --status` reads it, and nothing off the box can reach it.
OPEN_PATHS = ("/", "/health", "/status")
# What the UI listener serves without a token. It is reachable from wherever
# ui_cidr allows, so it gives away nothing but liveness; /status on this
# listener would hand a stranger the model ARN, the goals and the agent's
# last reasoning.
UI_OPEN_PATHS = ("/health",)
# Addresses that can only be the box itself, and so the tunnel.
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
# Seconds a TLS client gets to finish its handshake on the UI port.
TLS_HANDSHAKE_TIMEOUT = 10.0
# How long /status waits for the agent before serving the last published
# snapshot instead. Under the 2s that `vigil --status` itself waits.
STATUS_LOCK_TIMEOUT = 1.0
# Client addresses the login throttle remembers at most. Each one is a list
# of timestamps; without a cap, a stream of fresh addresses grows it forever.
AUTH_FAILURE_MAX_CLIENTS = 4096


class _BadRequest(Exception):
    """A request the handler cannot even read, answered 400."""


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that finishes a TLS handshake in the request thread.

    Wrapping the listening socket with the default do_handshake_on_connect
    makes accept() do the handshake on the one thread that serves the port,
    with no timeout: a single idle TCP connection then stalls every client.
    Here the socket is wrapped without the handshake, and each connection's
    own thread does it under a timeout.
    """

    tls_handshake_timeout: Optional[float] = None

    def finish_request(self, request, client_address):
        if self.tls_handshake_timeout is not None and isinstance(request, ssl.SSLSocket):
            previous = request.gettimeout()
            try:
                request.settimeout(self.tls_handshake_timeout)
                request.do_handshake()
                request.settimeout(previous)
            except (ssl.SSLError, OSError):
                # Plain HTTP, a scanner, or a client that never spoke. The
                # thread's shutdown_request closes the socket.
                return
        super().finish_request(request, client_address)


def write_token_file(token: str, path: str) -> Optional[str]:
    """Write ``token`` to ``path`` with mode 0600; returns path or None."""
    if not path:
        return None
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        os.replace(tmp, path)
        return path
    except OSError:
        return None


def read_token_file(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        with open(path) as f:
            token = f.read().strip()
    except OSError:
        return None
    return token or None


class HeadlessRunner:
    """Drive an AgentCore without a console."""

    def __init__(self, agent, logger, interval: float = 10.0,
                 max_cycles: int = 0, status_port: Optional[int] = None,
                 status_host: str = "127.0.0.1",
                 status_file: Optional[str] = None,
                 token: Optional[str] = None,
                 token_file: Optional[str] = DEFAULT_TOKEN_FILE,
                 token_persist_file: Optional[str] = DEFAULT_TOKEN_PERSIST_FILE,
                 session_key_file: Optional[str] = None,
                 session_days: int = 30,
                 ui: Optional[dict] = None,
                 max_idle_wait: Optional[float] = None,
                 consolidate_every_s: Optional[float] = None,
                 first_consolidation_s: Optional[float] = None):
        self.agent = agent
        self.log = logger.getChild("headless")
        self.interval = max(0.0, float(interval))
        self.max_cycles = int(max_cycles or 0)
        self.status_port = status_port
        self.status_host = status_host
        self.status_file = status_file
        # The token used to be minted fresh at every start and written only
        # under /run, which systemd wipes on restart: every deploy silently
        # replaced the operator's credential. It now persists.
        self.token_persist_file = token_persist_file
        self.token = token or read_token_file(token_persist_file) or secrets.token_urlsafe(32)
        self.token_file = token_file
        # Signed browser sessions, so a login survives a restart even when the
        # token is rotated. No key means no sessions, never an ephemeral one.
        from vigil.cloud import session as _session
        self.session_days = max(1, min(int(session_days or 30), _session.MAX_DAYS))
        self.session_samesite = (ui or {}).get("session_samesite") or _session.DEFAULT_SAMESITE
        self.session_key_file = session_key_file or _session.DEFAULT_KEY_FILE
        self.session_key = _session.load_or_create_key(self.session_key_file, self.log)
        # Sessions issued before this moment are refused. Kept beside the key
        # so a restart does not quietly bring a logged-out session back.
        self.session_revoked_file = self.session_key_file + ".not_before"
        self.session_not_before = _session.read_not_before(self.session_revoked_file)
        ui = dict(ui or {})
        self.ui_enabled = bool(ui.get("enabled"))
        self.ui_host = ui.get("host") or "0.0.0.0"
        # 0 means "any free port", as it does for status_port; `or 8443` read
        # it as unset, so every test listener quietly shared the real port.
        port = ui.get("port")
        self.ui_port = 8443 if port is None or port == "" else int(port)
        self.ui_tls = bool(ui.get("tls", True))
        self.tls_dir = ui.get("tls_dir") or DEFAULT_TLS_DIR
        self.upload_dir = ui.get("upload_dir") or DEFAULT_UPLOAD_DIR
        self.max_upload_bytes = int(float(ui.get("max_upload_mb") or 50) * 1024 * 1024)
        self._ui_server: Optional[ThreadingHTTPServer] = None
        self._ui_thread: Optional[threading.Thread] = None
        # Brute-force guard for the token: per client address, failures in
        # the last window; from the Nth failure on, 429 until the window passes.
        self._auth_failures: dict = {}
        # Request threads read and write it at once.
        self._auth_lock = threading.Lock()
        self.auth_failure_max_clients = AUTH_FAILURE_MAX_CLIENTS
        self.auth_failure_limit = int(ui.get("auth_failure_limit") or 10)
        self.auth_failure_window = float(ui.get("auth_failure_window") or 300)
        self.started_at = time.time()
        self.failure_streak = 0
        self.max_failure_wait = max(self.interval, 300.0)
        # Idle cycles stretch the wait (interval x streak, capped) so a
        # planner with nothing to do is not consulted every interval; any
        # goal, upload or chat wakes the loop and resets the streak.
        self.idle_streak = 0
        self.max_idle_wait = max(self.interval, float(300.0 if max_idle_wait is None else max_idle_wait))
        # The same task succeeding again and again is not progress, it is a
        # loop, and it must be paced like idling rather than like work. The
        # rule planner answers a standing goal with a goal_step every time it
        # is asked, and a goal_step's whole effect is to write
        # {"progressed": true}. Before the watch the brain interrupted that
        # every few minutes with a real idle, which reset the pacing; once the
        # brain sleeps, nothing does, and the loop spins at one cycle a second
        # writing 7,000 ledger entries an hour about its own heartbeat.
        self.repeat_streak = 0
        self._last_action = None
        # Memory consolidation runs on its own slow clock, not every cycle:
        # an hour of observations is the unit worth reorganising, not one.
        self.consolidate_every_s = float(consolidate_every_s or 3600.0)
        # Start the clock in the past so the first pass lands a few minutes
        # after boot rather than a full interval later. Seeding it with "now"
        # meant a box restarted more often than the interval never
        # consolidated at all -- which is exactly the box being worked on, and
        # why this had not run once in a day of deploys.
        self.first_consolidation_s = float(first_consolidation_s or 300.0)
        self._last_consolidation = (time.time() - self.consolidate_every_s
                                    + min(self.first_consolidation_s,
                                          self.consolidate_every_s))
        self.last_consolidation: Optional[dict] = None
        # Behaviour lab: the current dial settings and whether live chat uses them.
        from vigil.brain import dials as _dials
        self.lab_settings = _dials.defaults()
        self.lab_apply_to_chat = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        # Serialises agent cycles between the loop and HTTP-triggered work.
        self._lock = threading.RLock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self.last_cycle: dict = {}
        # The last snapshot taken, and when: what /status serves, marked
        # stale, while a model call holds the lock.
        self._published: Optional[dict] = None
        self._published_at: Optional[float] = None

    # ---- status --------------------------------------------------------

    def lab_state(self) -> dict:
        from vigil.brain import dials as _dials
        state = _dials.registry()
        state["settings"] = dict(self.lab_settings)
        state["fingerprint"] = _dials.fingerprint(self.lab_settings)
        state["apply_to_chat"] = self.lab_apply_to_chat and self.agent.lab.is_open()
        state["session"] = self.agent.lab.state()
        state["brain"] = self.agent.brain.status() if self.agent.brain else None
        return state

    def snapshot(self) -> dict:
        status = self.agent.get_status()
        status["runner"] = {
            "mode": "headless",
            "interval": self.interval,
            "max_cycles": self.max_cycles,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "last_action": self.last_cycle.get("action"),
            "last_cycle_at": self.last_cycle.get("timestamp"),
            "stopping": self._stop.is_set(),
            "idle_streak": self.idle_streak,
        }
        return status

    def _publish(self, snapshot: dict) -> dict:
        self._published = snapshot
        self._published_at = time.time()
        return snapshot

    def status_view(self, timeout: float = STATUS_LOCK_TIMEOUT) -> dict:
        """A fresh snapshot if the agent is free soon, else the last one.

        The lock is held for a whole cycle and for every /think and /chat,
        which is the length of a model call. /status waiting that long made
        the box look dead to anything that asks it how it is.
        """
        if self._lock.acquire(timeout=timeout):
            try:
                return self._publish(self.snapshot())
            finally:
                self._lock.release()
        published = self._published
        if published is not None:
            return dict(published, stale=True, snapshot_at=self._published_at)
        # Nothing published yet. Plain attributes only: anything that walks
        # the agent's state belongs under the lock.
        return {"name": self.agent.name, "stale": True, "snapshot_at": None,
                "busy": True, "cycle_count": self.agent.cycle_count,
                "runner": {"mode": "headless",
                           "uptime_seconds": round(time.time() - self.started_at, 1),
                           "stopping": self._stop.is_set()}}

    def _write_status_file(self):
        if not self.status_file:
            return
        try:
            with self._lock:
                snapshot = self._publish(self.snapshot())
            directory = os.path.dirname(self.status_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = self.status_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snapshot, f, default=str)
            os.replace(tmp, self.status_file)
        except OSError as e:
            self.log.debug("Could not write status file: %s", e)

    # ---- http ----------------------------------------------------------

    def _make_handler(self, open_paths=OPEN_PATHS):
        runner = self
        loopback_only = open_paths is OPEN_PATHS

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # quiet
                runner.log.debug("http %s", fmt % args)

            def send_response(self, code, message=None):
                # Remembered so the catch-all below knows whether a status
                # line has already gone out and a 500 would corrupt it.
                self._responded = True
                super().send_response(code, message)

            def _content_length(self) -> int:
                """The declared body length; _BadRequest if it is not one.

                int() on the raw header raised ValueError out of the handler,
                and a negative value would have been read as "until EOF".
                """
                raw = (self.headers.get("Content-Length") or "").strip()
                if not raw:
                    return 0
                # ASCII only: isdigit() alone passes "²", which int() rejects.
                if not (raw.isascii() and raw.isdigit()):
                    raise _BadRequest("Content-Length is not a number")
                return int(raw)

            def _guarded(self, route):
                """Run one request; answer rather than drop it if it fails.

                An exception used to reach socketserver, which printed a
                traceback to stderr and closed the socket with nothing sent.
                The client now gets a status and a JSON error. The error text
                is generic: exception messages can carry paths or values
                that a caller holding the page has no business seeing.
                """
                self._responded = False
                try:
                    route()
                except _BadRequest as why:
                    # The body length is unknown, so the connection cannot
                    # be reused for another request.
                    self.close_connection = True
                    if not self._responded:
                        self._send(400, {"error": str(why)})
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                except Exception as exc:              # noqa: BLE001
                    self.close_connection = True
                    runner.log.warning("%s %s failed: %s", self.command,
                                       self.path.partition("?")[0],
                                       type(exc).__name__, exc_info=True)
                    if not self._responded:
                        try:
                            self._send(500, {"error": "internal error"})
                        except OSError:
                            pass

            def _send(self, code: int, payload, headers: Optional[list] = None):
                body = json.dumps(payload, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, value in (headers or []):
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _client(self) -> str:
                """Who to count a failed login against.

                Behind the tunnel every request arrives from loopback, so the
                socket peer alone would put the whole internet in one bucket:
                a stranger guessing tokens would lock the operator out, and
                the operator's own retries would spend the stranger's budget.
                Cloudflare puts the real client in CF-Connecting-IP. It is
                trusted only when the peer is loopback, because a request
                straight at the port could otherwise set the header itself and
                have every guess counted against a different address.
                """
                peer = self.client_address[0] if self.client_address else "?"
                if peer in LOOPBACK:
                    forwarded = (self.headers.get("CF-Connecting-IP") or "").strip()
                    if forwarded:
                        return forwarded[:64]
                return peer

            def _auth_kind(self) -> Optional[str]:
                """"token", "cookie", or None. The token wins when both come.

                The difference matters for one thing: a browser attaches the
                cookie to requests another site makes it send, and never
                attaches a header it was not told to. So only a cookie-only
                request can be forged cross-site, and only it is held to the
                same-origin check in _post.
                """
                if runner.auth_locked(self._client()):
                    return None
                header = self.headers.get("Authorization") or ""
                supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
                supplied = supplied or (self.headers.get("X-Vigil-Token")
                                        or self.headers.get("X-Jarvis-Token") or "").strip()
                if supplied and secrets.compare_digest(supplied, runner.token):
                    return "token"
                # A valid session with a stale token beside it (a browser that
                # stored the token before a rotation) is still the session, as
                # it always was; only a bad token on its own counts as a guess.
                if runner.session_valid(self.headers.get("Cookie")):
                    return "cookie"
                if supplied:
                    runner.auth_failed(self._client())
                return None

            def _authorised(self) -> bool:
                return self._auth_kind() is not None

            def _same_origin(self) -> bool:
                """Did the page that sent this come from this server?

                Sec-Fetch-Site is set by the browser and cannot be set by a
                page, so when it is present it decides. Older browsers send
                Origin instead, compared with the Host this request came in
                on (the tunnel hostname behind Cloudflare, ip:port direct).
                A cookie-carrying request with neither is refused: every
                browser this UI supports sends one of them on a POST, and a
                request that sends neither is not the UI.
                """
                site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
                if site:
                    return site == "same-origin"
                origin = (self.headers.get("Origin") or "").strip()
                host = (self.headers.get("Host") or "").strip().lower()
                if not origin or not host or origin == "null":
                    return False
                from urllib.parse import urlsplit
                try:
                    return urlsplit(origin).netloc.lower() == host
                except ValueError:
                    return False

            def _deny(self):
                if runner.auth_locked(self._client()):
                    self._send(429, {"error": "too many failed logins; try again later"})
                else:
                    self._send(401, {"error": "token required",
                                     "hint": f"Authorization: Bearer $(cat {runner.token_file})"})

            def _require_token(self, path) -> bool:
                if path in open_paths or self._authorised():
                    return True
                self._deny()
                return False

            def _redirect(self, location: str):
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

            def _send_file(self, path: str, filename: str, content_type: str):
                """Hand a document back. Attachment, never inline.

                Content-Disposition: attachment and X-Content-Type-Options
                together stop the browser deciding for itself what a file is
                and rendering it in the page's own origin -- which for a
                document the agent generated would put its text inside the
                session that can drive the agent.
                """
                try:
                    with open(path, "rb") as fh:
                        body = fh.read()
                except OSError as exc:
                    return self._send(404, {"error": f"cannot read it: {exc}"})
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{filename}"')
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                # If a browser ever renders it anyway, it renders with no
                # script, no origin and no frame.
                self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(body)

            def _send_raw(self, code: int, body: bytes, content_type: str):
                """Bytes that are not JSON: a screenshot, so far.

                no-store and DENY for the same reason the UI page has them --
                this is a picture of whatever page the browser is on, and it
                should not sit in a cache or be framed by anything else.
                """
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self, html: str, nonce: Optional[str] = None):
                body = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                if nonce:
                    self.send_header("Content-Security-Policy", ui_csp(nonce))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._guarded(self._get)

            def do_POST(self):
                self._guarded(self._post)

            def _get(self):
                raw_path, _, query = self.path.partition("?")
                path = raw_path.rstrip("/") or "/"
                if path == "/ui":
                    nonce = secrets.token_urlsafe(18)
                    return self._send_html(
                        runner.ui_html().replace(UI_NONCE_PLACEHOLDER, nonce), nonce)
                # The bare domain is the address you actually type or tap, and
                # on the public server it answered "token required" — which
                # reads as a broken login rather than a wrong path. The login
                # page lives at /ui, so send people there. The loopback API
                # keeps its root as a health probe; nothing scripted is
                # pointed at the root of the UI port.
                if path == "/" and not loopback_only:
                    return self._redirect("/ui")
                if not self._require_token(path):
                    return
                if path == "/uploads":
                    return self._send(200, runner.list_uploads())
                if path == "/marketing":
                    view = getattr(runner.agent, "marketing", None)
                    if view is None:
                        return self._send(200, {
                            "enabled": False,
                            "why": ("marketing.enabled is not set in config; "
                                    "the agent is not reading the scout")})
                    return self._send(200, dict(view.state(), enabled=True))
                if path == "/browse":
                    # What the browser is doing, for the tab. The page body
                    # comes back as plain text rather than the envelope: the
                    # envelope is for the model, which needs telling that page
                    # text is not an instruction. Paul knows.
                    view = getattr(runner.agent, "browser", None)
                    if view is None:
                        return self._send(200, {
                            "enabled": False,
                            "why": ("browser.enabled is not set in config; "
                                    "the browser is switched off")})
                    health = view.health()
                    return self._send(200, {"enabled": True, "health": health,
                                            "page": view.last or {}})
                if path == "/browse/debrief":
                    view = getattr(runner.agent, "browser", None)
                    journal = getattr(view, "journal", None)
                    if journal is None:
                        return self._send(200, {"enabled": False, "text": "The browser journal is off."})
                    try:
                        hours = max(1, min(168, int((query.split("hours=") + ["24"])[1].split("&")[0] or 24)))
                    except (TypeError, ValueError):
                        hours = 24
                    got = journal.debrief(hours * 3600)
                    return self._send(200, {"enabled": True, **got, "text": journal.render(got)})
                if path == "/browse/shot":
                    view = getattr(runner.agent, "browser", None)
                    if view is None:
                        return self._send(404, {"error": "the browser is off"})
                    try:
                        # Bind the names, not the package: `import urllib.request`
                        # here would make `urllib` a local of do_GET and shadow
                        # the module-level one for every other branch, including
                        # /documents/<name> further down. It did exactly that,
                        # and three document tests caught it.
                        from urllib.request import Request as _Req, urlopen as _open
                        req = _Req(view.base + "/shot")
                        token = view._token()
                        if token:
                            req.add_header("Authorization", f"Bearer {token}")
                        with _open(req, timeout=view.timeout) as r:
                            shot = r.read()
                    except Exception as exc:      # noqa: BLE001
                        return self._send(503, {"error": f"no screenshot: {exc}"})
                    return self._send_raw(200, shot, "image/png")
                if path == "/documents":
                    from vigil.agent import compose as _compose
                    return self._send(200, {
                        "dir": runner.agent.document_dir,
                        "formats": list(_compose.FORMATS),
                        "documents": _compose.listing(runner.agent.document_dir)})
                if path.startswith("/documents/"):
                    from vigil.agent import compose as _compose
                    # The name is taken apart and rebuilt rather than trusted:
                    # basename first, then the scrub, then a realpath check
                    # against the directory. Any one of those alone is a claim.
                    asked = urllib.parse.unquote(path[len("/documents/"):])[:200]
                    base = os.path.basename(asked)
                    stem, _, ext = base.rpartition(".")
                    if ext.lower() not in _compose.FORMATS or not stem:
                        return self._send(404, {"error": "no such document"})
                    wanted = f"{_compose.safe_name(stem)}.{ext.lower()}"
                    root = os.path.realpath(runner.agent.document_dir)
                    full = os.path.realpath(os.path.join(root, wanted))
                    if os.path.dirname(full) != root or not os.path.isfile(full):
                        return self._send(404, {"error": "no such document"})
                    return self._send_file(full, wanted, DOCUMENT_TYPES[ext.lower()])
                limit = 20
                search = ""
                day = None
                for part in query.split("&"):
                    if part.startswith("limit="):
                        try:
                            limit = max(1, min(int(part[6:]), 200))
                        except ValueError:
                            pass
                    elif part.startswith("q="):
                        search = urllib.parse.unquote_plus(part[2:])[:200].strip()
                    elif part.startswith("day="):
                        day = urllib.parse.unquote_plus(part[4:])[:10].strip() or None
                # Liveness and status stay outside the agent lock, which is
                # held for the length of every model call: a probe that waits
                # on it reports a busy agent as a dead one.
                if path in ("/", "/health"):
                    if loopback_only:
                        return self._send(200, {"ok": True, "agent": runner.agent.name,
                                                "cycles": runner.agent.cycle_count})
                    return self._send(200, {"ok": True})
                if path == "/status":
                    return self._send(200, runner.status_view())
                with runner._lock:
                    if path == "/memory":
                        self._send(200, runner.agent.memory.get_summary())
                    elif path == "/proposals":
                        # Each carries its id, and a decision is sent back by
                        # it: a position in this slice is not a position in
                        # the agent's list, and the list is bounded, so even
                        # a position in the full list moves as it fills.
                        shown = list(runner.agent.proposals)[-limit:]
                        self._send(200, {
                            "rung": runner.agent.rung,
                            "proposals": [dict(p, id=runner.agent.proposal_id(p))
                                          for p in shown]})
                    elif path == "/memory/store":
                        store = runner.agent.store
                        entries = (store.search(search, limit) if search
                                   else store.recent(limit))
                        self._send(200, {"stats": store.stats(), "query": search or None,
                                         "entries": entries})
                    elif path == "/history":
                        self._send(200, runner.agent.task_history[-limit:])
                    elif path == "/brain":
                        brain = runner.agent.brain
                        self._send(200, {"brain": brain.status() if brain else None,
                                         "last_thought": runner.agent.last_thought or None})
                    elif path == "/goals":
                        self._send(200, runner.agent.planner.goals)
                    elif path == "/questions":
                        self._send(200, runner.agent.questions.state())
                    elif path == "/operator":
                        # Everything the agent holds about him, for him to
                        # read. A profile its subject cannot see is a rumour
                        # with his name on it.
                        self._send(200, runner.agent.operator.state())
                    elif path == "/hunches":
                        self._send(200, runner.agent.hunches.state())
                    elif path == "/probes":
                        self._send(200, runner.agent.probes.state())
                    elif path == "/vitals":
                        # The Floor Test's nine, for him. Deliberately not in
                        # the model's context: an agent that could see it had
                        # never disagreed with him would manufacture a
                        # disagreement. Signals, never targets.
                        self._send(200, runner.agent.vitals.read())
                    elif path == "/schedule":
                        # The shape of his week, what collides, and where the
                        # gaps in what is written down are.
                        self._send(200, runner.agent.schedule.state())
                    elif path == "/schedule/free":
                        # Never "free time". See schedule.py: the answer
                        # carries what it is actually a statement about.
                        self._send(200, runner.agent.schedule.free(day))
                    elif path == "/diary":
                        # What is coming, what he has not ruled on, and when
                        # the register last looked.
                        self._send(200, runner.agent.diary.state())
                    elif path == "/lab":
                        self._send(200, runner.lab_state())
                    elif path in ("/research", "/research/project"):
                        research = getattr(runner.agent, "research", None)
                        if research is None:
                            self._send(200, {"enabled": False, "configured": False, "projects": []})
                        elif path == "/research":
                            self._send(200, research.state())
                        else:
                            pid = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("id")
                            try:
                                self._send(200, research.project(pid))
                            except Exception as exc:
                                self._send(404, {"error": str(exc)})
                    elif path == "/ledger":
                        self._send(200, runner.agent.ledger.status())
                    elif path == "/ledger/tail":
                        self._send(200, runner.agent.ledger.tail(limit)
                                   if runner.agent.ledger.enabled else [])
                    elif path == "/ledger/verify":
                        self._send(200, runner.agent.ledger.verify()
                                   if runner.agent.ledger.enabled else {"ok": False, "summary": "ledger disabled"})
                    elif path == "/ledger/pubkey":
                        self._send(200, {"pubkey": getattr(runner.agent.ledger, "pubkey", None),
                                         "format": "glass-ledger/v2"})
                    else:
                        self._send(404, {"error": "not found"})

            def _body(self) -> str:
                length = self._content_length()
                return self.rfile.read(length).decode("utf-8", errors="replace") if length else ""

            def _post(self):
                raw_path, _, query = self.path.partition("?")
                path = raw_path.rstrip("/") or "/"
                # Login and logout come before the auth check: one is how you
                # get authorised, and the other only clears your own cookie.
                if path == "/login":
                    if runner.auth_locked(self._client()):
                        return self._send(429, {"error": "too many failed logins; try again later"})
                    if self._content_length() > 4096:
                        return self._send(413, {"error": "body too large"})
                    supplied = self._body().strip()
                    if not supplied or not secrets.compare_digest(supplied, runner.token):
                        runner.auth_failed(self._client())
                        return self._send(401, {"error": "bad token"})
                    cookie = runner.new_session_cookie()
                    if not cookie:
                        return self._send(200, {"ok": True, "session": False,
                                                "note": "sessions unavailable; the token is still required"})
                    return self._send(200, {"ok": True, "session": True, "days": runner.session_days},
                                      headers=[("Set-Cookie", cookie)])
                if path == "/logout":
                    # Ending every session is only done for a real, same-origin
                    # request from a logged-in page: otherwise any site could
                    # log Paul out by posting here. Clearing this browser's
                    # own cookie is harmless and always done.
                    if (runner.session_valid(self.headers.get("Cookie"))
                            and self._same_origin()):
                        runner.revoke_sessions()
                    return self._send(200, {"ok": True},
                                      headers=[("Set-Cookie", runner.clear_session_cookie())])
                kind = self._auth_kind()
                if kind is None:
                    return self._deny()
                if kind == "cookie" and not self._same_origin():
                    # SameSite=Lax keeps the cookie off most cross-site POSTs,
                    # but not off a sibling subdomain, and it is one setting
                    # away from off. This does not depend on either.
                    return self._send(403, {"error": "cross-site request refused"})
                if path == "/upload":
                    return self._handle_upload()
                if self._content_length() > 1_000_000:
                    return self._send(413, {"error": "body too large"})
                body = self._body().strip()
                if path.startswith("/browse/"):
                    # Paul driving the browser himself. Deliberately the same
                    # client the agent uses, so there is one set of fences and
                    # not two: the metadata service, the private ranges and
                    # the password-field refusals hold for him as well. That
                    # is not paternalism -- they are properties of a browser
                    # running unsandboxed on this box, and they do not stop
                    # being true because a person asked.
                    view = getattr(runner.agent, "browser", None)
                    if view is None:
                        return self._send(404, {"error": "the browser is off"})
                    try:
                        payload = json.loads(body or "{}")
                    except ValueError:
                        return self._send(400, {"error": "body is not JSON"})
                    if not isinstance(payload, dict):
                        return self._send(400, {"error": "JSON object required"})
                    what = path[len("/browse/"):]
                    if what == "open":
                        url = str(payload.get("url") or "").strip()
                        if not url:
                            return self._send(400, {"error": "a url is required"})
                        if "://" not in url:
                            url = "https://" + url.lstrip("/")
                        got = view.open(url)
                    elif what == "follow":
                        got = view.follow(str(payload.get("ref") or ""))
                    elif what == "act":
                        # operator=True: this is Paul pressing the button
                        # himself, so the two gates that exist to keep the
                        # agent from acting AS him or FOR him without asking
                        # do not apply. The browser's own refusals still do.
                        got = view.act(str(payload.get("kind") or ""),
                                       str(payload.get("ref") or ""),
                                       str(payload.get("text") or ""),
                                       operator=True)
                    elif what == "reset":
                        got = view.reset()
                    else:
                        return self._send(404, {"error": f"no route /browse/{what}"})
                    journal = getattr(view, "journal", None)
                    if journal is not None and what != "reset":
                        ok = not (isinstance(got, dict) and got.get("error"))
                        journal.record(f"{what} {payload.get('url') or payload.get('ref') or payload.get('kind') or ''}".strip(),
                                       (got.get("url") if isinstance(got, dict) else "") or "",
                                       "ok" if ok else "error", by="operator",
                                       reason="" if ok else str(got.get("error"))[:200])
                    if isinstance(got, dict) and got.get("error"):
                        return self._send(502, got)
                    return self._send(200, got)
                if path == "/proposals/decide":
                    # Until this existed, proposals went one way: the agent
                    # filed them, they sat in a list, and nothing came back, so
                    # it could file the same one a sixth time and never learn
                    # the first five were unwelcome. Accepting does not run it;
                    # it says the agent was right to want it, which is the part
                    # worth learning from.
                    # By id, which the listing carries. "index" is still read
                    # for an older page, and means a position in the agent's
                    # whole list, which is what decide_proposal always took.
                    try:
                        payload = json.loads(body or "{}")
                    except ValueError:
                        return self._send(400, {"error": "body must be JSON"})
                    if not isinstance(payload, dict):
                        return self._send(400, {"error": "JSON object required"})
                    ident = payload.get("id")
                    index = None
                    if ident is not None:
                        if not isinstance(ident, str) or not ident.strip():
                            return self._send(400, {"error": "id must be a non-empty string"})
                        ident = ident.strip()
                    else:
                        try:
                            index = int(payload.get("index"))
                        except (ValueError, TypeError):
                            return self._send(400, {"error": "id (or index) and accepted required"})
                    accepted = bool(payload.get("accepted"))
                    with runner._lock:
                        decided = runner.agent.decide_proposal(
                            index, accepted, str(payload.get("reason") or ""),
                            proposal_id=ident)
                    runner.wake()
                    if decided is None:
                        return self._send(404, {"error": "no such undecided proposal"})
                    return self._send(200, decided)
                if path == "/goal/withdraw":
                    # An operator who can only add goals cannot take an
                    # instruction back; a goal the agent cannot satisfy would
                    # otherwise sit in its context for the life of the process.
                    if not body:
                        return self._send(400, {"error": "goal text required in body"})
                    with runner._lock:
                        done = runner.agent.withdraw_goal(body)
                    runner.wake()
                    return self._send(200 if done else 404,
                                      {"withdrawn": body if done else None,
                                       "error": None if done else "no open goal with that text"})
                if path == "/goal":
                    if not body:
                        return self._send(400, {"error": "goal text required in body"})
                    if len(body) > 500:
                        return self._send(400, {"error": "goal text over 500 characters"})
                    priority = 5
                    for part in query.split("&"):
                        if part.startswith("priority="):
                            try:
                                priority = max(0, min(int(part[9:]), 10))
                            except ValueError:
                                pass
                    with runner._lock:
                        runner.agent.add_goal(body, priority)
                        open_goals = len(runner.agent.planner.open_goals())
                    runner.wake()
                    runner._write_status_file()
                    self._send(200, {"added": body, "priority": priority,
                                     "open_goals": open_goals})
                elif path == "/think":
                    with runner._lock:
                        out = runner.agent.think()
                    runner._write_status_file()
                    self._send(200 if "error" not in out else 409, out)
                elif path == "/ask":
                    if not body:
                        return self._send(400, {"error": "question required in body"})
                    with runner._lock:
                        runner.agent.note_operator("question")
                        answer = runner.agent.ask(body)
                    self._send(200, {"question": body, "answer": answer})
                elif path == "/chat":
                    try:
                        payload = json.loads(body or "{}")
                    except ValueError:
                        return self._send(400, {"error": "body must be JSON"})
                    turns = payload.get("messages") if isinstance(payload, dict) else None
                    if not isinstance(turns, list) or not turns:
                        return self._send(400, {"error": "messages list required"})
                    settings = (runner.lab_settings
                                if runner.lab_apply_to_chat and runner.agent.lab.is_open()
                                else None)
                    # Who is asking, for the agent's own memory of the exchange.
                    # A claim, not proof: anyone holding the token can write
                    # anything here, so it is clipped, stripped of newlines and
                    # never treated as authority -- it names a correspondent, it
                    # does not grant one anything.
                    asked_by = payload.get("from") if isinstance(payload, dict) else None
                    asked_by = " ".join(str(asked_by or "").split())[:80]
                    with runner._lock:
                        runner.agent.note_operator("chat")
                        answer = runner.agent.chat(turns, settings=settings,
                                                   asked_by=asked_by)
                        options = getattr(runner.agent, "last_chat_choices", None)
                        runner.agent.last_chat_choices = None
                    runner.wake()
                    self._send(200, {"answer": answer, "dials": settings is not None,
                                     "choices": options})
                elif path == "/compose":
                    try:
                        payload = json.loads(body or "{}")
                    except ValueError:
                        return self._send(400, {"error": "body must be JSON"})
                    if not isinstance(payload, dict):
                        return self._send(400, {"error": "JSON object required"})
                    from vigil.agent import compose as _compose
                    with runner._lock:
                        runner.agent.note_operator("compose")
                        try:
                            written = runner.agent.compose_document(
                                str(payload.get("content") or ""),
                                title=str(payload.get("title") or ""),
                                fmt=str(payload.get("format")
                                        or _compose.DEFAULT_FORMAT),
                                name=str(payload.get("name") or ""))
                        except _compose.ComposeError as why:
                            return self._send(400, {"error": str(why)})
                        except OSError as exc:
                            return self._send(500, {"error": str(exc)})
                    self._send(200, dict(written,
                                         url=f"/documents/{written['name']}"))
                elif path == "/told":
                    # Something the operator states as so. Not inferred from a
                    # chat turn: which sentences were assertions is a judgement,
                    # and letting the model make it is the model choosing what
                    # it may later treat as fact.
                    try:
                        payload = json.loads(body or "{}")
                    except ValueError:
                        return self._send(400, {"error": "body must be JSON"})
                    if not isinstance(payload, dict):
                        return self._send(400, {"error": "JSON object required"})
                    with runner._lock:
                        runner.agent.note_operator("told")
                        entry = runner.agent.told(str(payload.get("text") or ""),
                                                  str(payload.get("by") or "operator"))
                    if entry is None:
                        return self._send(400, {"error": "nothing to remember"})
                    runner.wake()
                    self._send(200, {"stored": True, "text": entry.get("text")})
                elif path in ("/operator/add", "/operator/forget"):
                    try:
                        payload = json.loads(body or "{}")
                    except ValueError:
                        return self._send(400, {"error": "body must be JSON"})
                    if not isinstance(payload, dict):
                        return self._send(400, {"error": "JSON object required"})
                    from vigil.agent import operator as _operator
                    with runner._lock:
                        if path == "/operator/forget":
                            gone = runner.agent.operator.forget(
                                str(payload.get("text") or ""))
                            return self._send(200 if gone else 404,
                                              {"removed": gone,
                                               "profile": runner.agent.operator.state()})
                        try:
                            runner.agent.operator.add(
                                str(payload.get("text") or ""),
                                str(payload.get("source") or _operator.STATED),
                                str(payload.get("by") or ""))
                        except _operator.Refused as why:
                            return self._send(409, {"refused": str(why)})
                    runner._write_status_file()
                    self._send(200, {"profile": runner.agent.operator.state()})
                elif path in ("/hunches/resolve", "/hunches/withdraw",
                              "/probes/run"):
                    try:
                        payload = json.loads(body or "{}")
                    except ValueError:
                        return self._send(400, {"error": "body must be JSON"})
                    if not isinstance(payload, dict):
                        return self._send(400, {"error": "JSON object required"})
                    with runner._lock:
                        runner.agent.note_operator("hunches")
                        if path == "/probes/run":
                            # Six model calls, asked for rather than scheduled,
                            # and the agent is told the window is open.
                            return self._send(200, runner.agent.probes.run(
                                as_baseline=bool(payload.get("baseline"))))
                        ident = str(payload.get("id") or payload.get("about") or "")
                        if path == "/hunches/withdraw":
                            done = runner.agent.hunches.withdraw(ident)
                        else:
                            done = runner.agent.hunches.resolve(
                                ident, bool(payload.get("borne_out")),
                                str(payload.get("outcome") or "")) is not None
                    runner._write_status_file()
                    self._send(200 if done else 404,
                               {"changed": done,
                                "hunches": runner.agent.hunches.state()})
                elif path == "/vitals/declare":
                    try:
                        payload = json.loads(body or "{}")
                    except ValueError:
                        return self._send(400, {"error": "body must be JSON"})
                    if not isinstance(payload, dict):
                        return self._send(400, {"error": "JSON object required"})
                    from vigil.agent import vitals as _v
                    with runner._lock:
                        runner.agent.note_operator("vitals")
                        got = runner.agent.vitals.declare(
                            str(payload.get("kind") or ""),
                            str(payload.get("what") or ""),
                            str(payload.get("detail") or ""))
                    if got is None:
                        return self._send(409, {
                            "error": ("kind must be one of "
                                      + ", ".join(_v.DECLARED)
                                      + ", and what cannot be empty"),
                            "refused": True})
                    runner._write_status_file()
                    self._send(200, {"declared": got,
                                     "vitals": runner.agent.vitals.summary()})
                elif path in ("/schedule/add", "/schedule/confirm",
                              "/schedule/cancel", "/schedule/move"):
                    try:
                        payload = json.loads(body or "{}")
                    except ValueError:
                        return self._send(400, {"error": "body must be JSON"})
                    if not isinstance(payload, dict):
                        return self._send(400, {"error": "JSON object required"})
                    from vigil.agent import diary as _d
                    with runner._lock:
                        runner.agent.note_operator("schedule")
                        try:
                            if path == "/schedule/add":
                                item = runner.agent.schedule.add(
                                    str(payload.get("what") or ""),
                                    str(payload.get("when") or ""),
                                    length=payload.get("length"),
                                    repeat=payload.get("repeat") or _d.ONCE,
                                    where=str(payload.get("where") or ""),
                                    remind_before=payload.get("remind_before"))
                                return self._send(
                                    200, {"appointment": item.state_dict(),
                                          "schedule": runner.agent.schedule.state()})
                            ident = str(payload.get("id") or payload.get("what") or "")
                            if path == "/schedule/move":
                                item = runner.agent.schedule.move(
                                    ident, str(payload.get("when") or ""),
                                    length=payload.get("length"))
                                done = item is not None
                            elif path == "/schedule/confirm":
                                done = runner.agent.schedule.confirm(ident) is not None
                            else:
                                done = runner.agent.schedule.cancel(ident)
                        except _d.Refused as why:
                            return self._send(409, {"error": str(why),
                                                    "refused": True})
                    runner._write_status_file()
                    self._send(200 if done else 404,
                               {"changed": done,
                                "schedule": runner.agent.schedule.state()})
                elif path in ("/diary/add", "/diary/confirm", "/diary/drop",
                              "/diary/done"):
                    try:
                        payload = json.loads(body or "{}")
                    except ValueError:
                        return self._send(400, {"error": "body must be JSON"})
                    if not isinstance(payload, dict):
                        return self._send(400, {"error": "JSON object required"})
                    from vigil.agent import diary as _diary
                    with runner._lock:
                        runner.agent.note_operator("diary")
                        if path == "/diary/add":
                            try:
                                item = runner.agent.diary.add(
                                    str(payload.get("what") or ""),
                                    str(payload.get("when") or ""),
                                    repeat=payload.get("repeat") or _diary.ONCE,
                                    remind_before=payload.get("remind_before"),
                                    severity=str(payload.get("severity") or "notice"))
                            except _diary.Refused as why:
                                # The reason is the useful part -- every
                                # refusal in diary.py says what would work
                                # instead -- so it goes where a generic
                                # client will actually show it.
                                return self._send(409, {"error": str(why),
                                                        "refused": True})
                            return self._send(200, {"commitment": item.state_dict(),
                                                    "diary": runner.agent.diary.state()})
                        ident = str(payload.get("id") or payload.get("what") or "")
                        if path == "/diary/confirm":
                            done = runner.agent.diary.confirm(ident) is not None
                        elif path == "/diary/drop":
                            done = runner.agent.diary.drop(ident)
                        else:
                            done = runner.agent.diary.done(ident)
                    runner._write_status_file()
                    self._send(200 if done else 404,
                               {"changed": done, "diary": runner.agent.diary.state()})
                elif path.startswith("/research/"):
                    research = getattr(runner.agent, "research", None)
                    if research is None:
                        return self._send(404, {"error": "research is not configured"})
                    try:
                        payload = json.loads(body or "{}")
                    except ValueError:
                        return self._send(400, {"error": "body must be JSON"})
                    if not isinstance(payload, dict):
                        return self._send(400, {"error": "JSON object required"})
                    from vigil.agent.research import Refused as _ResearchRefused
                    try:
                        with runner._lock:
                            if path == "/research/create":
                                out = research.create(payload.get("title"), payload.get("direction"),
                                                      payload.get("budget"))
                            elif path == "/research/decide":
                                out = research.decide(payload.get("id"), bool(payload.get("accept")),
                                                      str(payload.get("reason") or ""),
                                                      payload.get("edits") if isinstance(payload.get("edits"), dict) else None)
                            elif path == "/research/answer":
                                out = research.answer(payload.get("id"), payload.get("text"))
                            elif path == "/research/control":
                                out = research.control(payload.get("id"), str(payload.get("action") or ""),
                                                       str(payload.get("reason") or ""),
                                                       payload.get("budget") if isinstance(payload.get("budget"), dict) else None)
                            elif path == "/research/verdict":
                                out = research.verdict(payload.get("thread"), str(payload.get("ruling") or ""),
                                                       str(payload.get("reason") or ""))
                            elif path == "/research/follow":
                                out = research.follow(payload.get("id"), payload.get("question"),
                                                      str(payload.get("reason") or ""))
                            elif path == "/research/lesson":
                                out = research.review_finding(payload.get("finding"), bool(payload.get("accept")),
                                                              str(payload.get("reason") or ""))
                            elif path == "/research/plan":
                                counts = payload.get("counts")
                                allowance = payload.get("allowance")
                                out = research.check_plan(payload.get("id"), str(payload.get("action") or ""),
                                                          counts if isinstance(counts, dict) else None,
                                                          allowance if isinstance(allowance, dict) else None,
                                                          str(payload.get("note") or ""))
                            else:
                                return self._send(404, {"error": "not found"})
                    except _ResearchRefused as why:
                        return self._send(409, {"error": str(why)})
                    self._send(200, out)
                elif path in ("/questions/ask", "/questions/answer"):
                    try:
                        payload = json.loads(body or "{}")
                    except ValueError:
                        return self._send(400, {"error": "body must be JSON"})
                    if not isinstance(payload, dict):
                        return self._send(400, {"error": "JSON object required"})
                    with runner._lock:
                        if path == "/questions/ask":
                            # Raising one on the agent's behalf, for testing
                            # the gates and for an operator who wants to see
                            # what a refusal reads like.
                            out = runner.agent.ask_operator(
                                str(payload.get("text") or ""),
                                str(payload.get("blocked_on") or ""),
                                str(payload.get("audience") or "reviewer"))
                            return self._send(200 if out.get("asked") else 409, out)
                        answered = runner.agent.questions.answer(
                            str(payload.get("id") or ""),
                            str(payload.get("text") or ""),
                            str(payload.get("by") or ""))
                    if answered is None:
                        return self._send(404, {"error": "no such open question"})
                    runner._write_status_file()
                    self._send(200, {"answered": answered.to_dict()})
                elif path == "/notify/test":
                    # Ring the bell. A channel reports itself ready on the
                    # strength of its settings and has no idea whether
                    # anything has ever arrived; this is the only way to find
                    # out that is not a 3am alert.
                    try:
                        payload = json.loads(body or "{}") if body else {}
                    except ValueError:
                        payload = {}
                    note = str((payload or {}).get("note") or "") if isinstance(payload, dict) else ""
                    with runner._lock:
                        result = runner.agent.notifier.prove(note)
                        try:
                            runner.agent.ledger.record("notification", {
                                "cycle": runner.agent.cycle_count, "actor": "operator",
                                "test": True, "sent": bool(result.get("sent")),
                                "channel": result.get("channel"),
                                "reason": result.get("reason")})
                        except Exception:
                            pass
                    runner._write_status_file()
                    self._send(200 if result.get("sent") else 503,
                               {"result": result, "notify": runner.agent.notifier.status()})
                elif path == "/verdict":
                    # A ruling from outside the agent. The machine rules by
                    # checking and needs no endpoint; this is for a person or
                    # a second reader, and it is recorded with who they were.
                    try:
                        payload = json.loads(body or "{}")
                    except ValueError:
                        return self._send(400, {"error": "body must be JSON"})
                    if not isinstance(payload, dict):
                        return self._send(400, {"error": "JSON object required"})
                    from vigil.agent import verdicts as _verdicts
                    with runner._lock:
                        entry = runner.agent.record_verdict(
                            claim=payload.get("claim") or "",
                            ruling=str(payload.get("ruling") or ""),
                            source=str(payload.get("source") or _verdicts.OPERATOR),
                            by=str(payload.get("by") or ""),
                            reason=str(payload.get("reason") or ""),
                            supersedes=payload.get("supersedes") or None)
                    if entry is None:
                        return self._send(400, {
                            "error": "claim, a ruling and a source are required",
                            "rulings": list(_verdicts.RULINGS),
                            "sources": [s for s in _verdicts.SOURCES
                                        if s != _verdicts.MACHINE],
                            "note": "the machine rules by checking, not by being told"})
                    runner._write_status_file()
                    self._send(200, {"recorded": entry})
                elif path == "/lab/session":
                    # One switch. It opens the lab and it tells the agent, and
                    # it cannot do one without the other: the announcement
                    # runs first and the window only opens if the agent was
                    # actually told. See vigil/agent/lab.py.
                    try:
                        payload = json.loads(body or "{}")
                    except ValueError:
                        return self._send(400, {"error": "body must be JSON"})
                    if not isinstance(payload, dict):
                        return self._send(400, {"error": "JSON object required"})
                    from vigil.agent import lab as _lab
                    want = payload.get("open")
                    if not isinstance(want, bool):
                        return self._send(400, {"error": "open must be true or false"})
                    with runner._lock:
                        if want:
                            try:
                                state = runner.agent.lab.open(
                                    purpose=payload.get("purpose") or "",
                                    operator=payload.get("operator") or "operator")
                            except _lab.NotAnnounced as e:
                                return self._send(503, {"error": str(e), "open": False})
                        else:
                            state = runner.agent.lab.close(
                                operator=payload.get("operator") or "operator")
                            # A window that ends returns live chat to the
                            # agent's own settings. Leaving the dials on a
                            # closed lab is how behaviour drifts with nobody
                            # having decided it should.
                            runner.lab_apply_to_chat = False
                    runner._write_status_file()
                    self._send(200, {"session": state, "lab": runner.lab_state()})
                elif path in ("/lab/settings", "/lab/preview", "/lab/run"):
                    try:
                        payload = json.loads(body or "{}")
                    except ValueError:
                        return self._send(400, {"error": "body must be JSON"})
                    if not isinstance(payload, dict):
                        return self._send(400, {"error": "JSON object required"})
                    if not runner.agent.lab.is_open():
                        return self._send(409, {
                            "error": "the lab is closed",
                            "remedy": "POST /lab/session {\"open\": true} first; it tells "
                                      "the agent the window has started",
                            "session": runner.agent.lab.state()})
                    from vigil.brain import dials as _dials
                    if path == "/lab/settings":
                        settings = _dials.normalise(payload.get("settings") or {})
                        apply = payload.get("apply_to_chat")
                        with runner._lock:
                            runner.lab_settings = settings
                            if isinstance(apply, bool):
                                runner.lab_apply_to_chat = apply
                            runner.agent.ledger.record("action", {
                                "cycle": runner.agent.cycle_count, "actor": "operator",
                                "action": "dials_set", "dials": _dials.fingerprint(settings),
                                "settings": settings, "apply_to_chat": runner.lab_apply_to_chat})
                        self._send(200, runner.lab_state())
                    elif path == "/lab/preview":
                        settings = _dials.normalise(payload.get("settings") or runner.lab_settings)
                        with runner._lock:
                            composed = _dials.compose(settings, runner.agent.brain, runner.agent,
                                                      runner.agent.observe() if runner.agent.brain else None)
                        self._send(200, composed)
                    else:
                        question = str(payload.get("question") or "").strip()
                        mode = "plan" if payload.get("mode") == "plan" else "answer"
                        if not question and mode == "answer":
                            return self._send(400, {"error": "question required"})
                        settings = _dials.normalise(payload.get("settings") or runner.lab_settings)
                        compare = payload.get("compare", True) is not False
                        with runner._lock:
                            result = runner.agent.experiment(question, settings=settings,
                                                             compare=compare, mode=mode)
                        self._send(200 if "error" not in result else 503, result)
                elif path == "/upload":
                    self._handle_upload()
                else:
                    self._send(404, {"error": "not found"})

            def _body_raw(self, limit: int) -> Optional[bytes]:
                length = self._content_length()
                if length > limit:
                    return None
                return self.rfile.read(length) if length else b""

            def _handle_upload(self):
                name = runner.safe_filename(self.headers.get("X-Filename") or "")
                if not name:
                    return self._send(400, {"error": "X-Filename header required"})
                length = self._content_length()
                if length <= 0:
                    return self._send(400, {"error": "empty upload"})
                if length > runner.max_upload_bytes:
                    return self._send(413, {"error": f"file over {runner.max_upload_bytes} bytes"})
                try:
                    path = runner.save_upload(name, self.rfile, length)
                except OSError as e:
                    return self._send(500, {"error": f"could not save upload: {e}"})
                with runner._lock:
                    entry = runner.agent.record_upload(name, path, length)
                runner.wake()
                runner._write_status_file()
                self._send(200, entry)

        return Handler

    def start_status_server(self) -> Optional[int]:
        if self.status_port is None:
            return None
        try:
            self._server = ThreadingHTTPServer(
                (self.status_host, int(self.status_port)), self._make_handler())
        except OSError as e:
            self.log.warning("Status endpoint unavailable on %s:%s: %s",
                             self.status_host, self.status_port, e)
            self._server = None
            return None
        self._server.daemon_threads = True
        self._server_thread = threading.Thread(
            target=self._server.serve_forever, name="vigil-status",
            daemon=True)
        self._server_thread.start()
        port = self._server.server_address[1]
        if self.token_persist_file:
            # Written outside the runtime directory so a restart does not
            # invalidate the credential already in the operator's browser.
            write_token_file(self.token, self.token_persist_file)
        written = write_token_file(self.token, self.token_file) if self.token_file else None
        self.log.info("Status endpoint listening on http://%s:%d/status (token: %s)",
                      self.status_host, port, written or "not written to disk")
        return port

    def stop_status_server(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._ui_server:
            self._ui_server.shutdown()
            self._ui_server.server_close()
            self._ui_server = None

    # ---- auth throttle ----------------------------------------------------------

    def session_valid(self, cookie_header: Optional[str]) -> bool:
        """True when the request carries a session this runner signed."""
        if not self.session_key:
            return False
        from vigil.cloud import session as _session
        return _session.verify(self.session_key,
                               _session.from_cookie_header(cookie_header),
                               not_before=self.session_not_before)

    def revoke_sessions(self) -> None:
        """End every session issued so far, on every device.

        One operator, so "log out" meaning "log out everywhere" costs him a
        login on his other device and closes the case where a copied cookie
        outlives the logout that was meant to end it.
        """
        import time as _time
        from vigil.cloud import session as _session
        # Whole seconds are what a session carries, so the line sits one
        # past this second: everything issued up to now is on the far side.
        moment = int(_time.time()) + 1
        self.session_not_before = moment
        _session.write_not_before(self.session_revoked_file, moment, self.log)

    def new_session_cookie(self) -> Optional[str]:
        """A Set-Cookie value for a fresh session, or None when disabled."""
        if not self.session_key:
            return None
        from vigil.cloud import session as _session
        # Never stamped before the revocation line: a login in the same
        # second as a logout must not be born already revoked.
        import time as _time
        issued = max(_time.time(), float(self.session_not_before or 0))
        return _session.cookie_header(
            _session.issue(self.session_key, self.session_days, now=issued),
            self.session_days, secure=self.ui_tls,
            samesite=self.session_samesite)

    def clear_session_cookie(self) -> str:
        from vigil.cloud import session as _session
        return _session.clear_header(secure=self.ui_tls,
                                     samesite=self.session_samesite)

    def _live_failures(self, client: str, now: float) -> list:
        """Failures for ``client`` still inside the window; caller holds the lock.

        A client with none left is removed rather than kept as an empty list:
        every request used to leave its address behind for good.
        """
        stamps = [t for t in self._auth_failures.get(client, ())
                  if now - t < self.auth_failure_window]
        if stamps:
            self._auth_failures[client] = stamps
        else:
            self._auth_failures.pop(client, None)
        return stamps

    def auth_locked(self, client: str) -> bool:
        with self._auth_lock:
            return len(self._live_failures(client, time.time())) >= self.auth_failure_limit

    def auth_failed(self, client: str):
        now = time.time()
        with self._auth_lock:
            stamps = self._live_failures(client, now)
            if client not in self._auth_failures and \
                    len(self._auth_failures) >= self.auth_failure_max_clients:
                self._make_room(now)
            stamps.append(now)
            self._auth_failures[client] = stamps
            count = len(stamps)
        if count == self.auth_failure_limit:
            self.log.warning("Too many bad tokens from %s; locked out for %.0fs",
                             client, self.auth_failure_window)

    def _make_room(self, now: float):
        """Bound the table: drop expired clients, then the longest-quiet ones.

        Evicting the least recent failures first keeps an address that is
        guessing right now, which is the one the lockout is for.
        """
        for client in list(self._auth_failures):
            self._live_failures(client, now)
        excess = len(self._auth_failures) - self.auth_failure_max_clients + 1
        if excess > 0:
            quiet = sorted(self._auth_failures, key=lambda c: self._auth_failures[c][-1])
            for client in quiet[:excess]:
                del self._auth_failures[client]

    # ---- UI listener ----------------------------------------------------------

    def ui_html(self) -> str:
        try:
            with open(UI_HTML_PATH, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return "<!doctype html><title>Vigil</title><p>UI page not installed.</p>"

    def ensure_tls_files(self) -> Optional[tuple]:
        """Self-signed cert/key for the UI listener, generated once with openssl."""
        cert = os.path.join(self.tls_dir, "ui.crt")
        key = os.path.join(self.tls_dir, "ui.key")
        if os.path.exists(cert) and os.path.exists(key):
            return cert, key
        try:
            os.makedirs(self.tls_dir, mode=0o700, exist_ok=True)
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
                 "-days", "3650", "-subj", f"/CN={self.agent.name or 'vigil'}",
                 "-keyout", key, "-out", cert],
                check=True, capture_output=True, timeout=60)
            os.chmod(key, 0o600)
            return cert, key
        except (OSError, subprocess.SubprocessError) as e:
            self.log.warning("Could not create TLS files in %s: %s", self.tls_dir, e)
            return None

    def start_ui_server(self) -> Optional[int]:
        """Serve the same API plus /ui on the UI address, with TLS by default."""
        if not self.ui_enabled:
            return None
        try:
            server = _Server((self.ui_host, self.ui_port),
                             self._make_handler(UI_OPEN_PATHS))
        except OSError as e:
            self.log.warning("UI listener unavailable on %s:%s: %s", self.ui_host, self.ui_port, e)
            return None
        scheme = "http"
        if self.ui_tls:
            files = self.ensure_tls_files()
            if files is None:
                self.log.warning("UI listener not started: TLS requested but unavailable")
                server.server_close()
                return None
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(files[0], files[1])
            # No handshake in accept(): that runs on the one serving thread,
            # so a client that connects and says nothing would hold the port
            # for everyone. _Server does it in the connection's own thread.
            server.socket = ctx.wrap_socket(server.socket, server_side=True,
                                            do_handshake_on_connect=False)
            server.tls_handshake_timeout = TLS_HANDSHAKE_TIMEOUT
            scheme = "https"
        server.daemon_threads = True
        self._ui_server = server
        self._ui_thread = threading.Thread(target=server.serve_forever, name="vigil-ui",
                                           daemon=True)
        self._ui_thread.start()
        port = server.server_address[1]
        self.log.info("Web UI listening on %s://%s:%d/ui (login with the runner token)",
                      scheme, self.ui_host, port)
        return port

    # ---- uploads ---------------------------------------------------------------

    @staticmethod
    def safe_filename(name: str) -> str:
        name = os.path.basename((name or "").strip().replace("\\", "/"))
        name = _SAFE_NAME.sub("_", name).strip("._")
        return name[:120]

    def save_upload(self, name: str, stream, length: int) -> str:
        os.makedirs(self.upload_dir, mode=0o750, exist_ok=True)
        base, ext = os.path.splitext(name)
        path = os.path.join(self.upload_dir, name)
        n = 1
        while os.path.exists(path):
            path = os.path.join(self.upload_dir, f"{base}-{n}{ext}")
            n += 1
        tmp = path + ".part"
        remaining = length
        with open(tmp, "wb") as f:
            while remaining > 0:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining > 0:
            os.unlink(tmp)
            raise OSError("upload truncated")
        os.replace(tmp, path)
        return path

    def list_uploads(self) -> list:
        entries = list(self.agent.uploads)
        for e in entries:
            e["exists"] = os.path.exists(e.get("path", ""))
        return entries[-50:]

    # ---- loop ----------------------------------------------------------

    def stop(self):
        self._stop.set()
        self._wake.set()
        self.agent.running = False

    def _consolidation_tick(self):
        """Tidy the agent's own memory on a slow clock, like sleep.

        Deliberately on the idle path rather than driven by the planner. An
        agent that has to decide to consolidate will not, because there is
        always something more interesting; and a consolidation pass the model
        chose is one it can also choose to skip forever. This costs nothing,
        touches only the agent's own notes, and cannot destroy anything.
        """
        consolidator = getattr(self.agent, "consolidator", None)
        if consolidator is None or not consolidator.enabled:
            return
        now = time.time()
        if now - self._last_consolidation < self.consolidate_every_s:
            return
        self._last_consolidation = now
        try:
            report = consolidator.run()
        except Exception as e:                       # never stops the loop
            self.log.warning("consolidation failed: %s", e)
            return
        if report.get("merged") or report.get("superseded") or report.get("promoted"):
            self.log.info("Consolidated memory: %d merged, %d superseded, "
                          "%d promoted, %d decayed (of %d scanned)",
                          len(report["merged"]), len(report["superseded"]),
                          len(report["promoted"]), report["decayed"],
                          report["scanned"])
        self.last_consolidation = report

    def _research_tick(self):
        """One phase of one research project, on its own slow clock.

        Only when research is switched on (research.enabled). One project per
        tick, the one that has waited longest, so a busy project cannot
        starve the others and the planner's own budget is not swamped.
        """
        research = getattr(self.agent, "research", None)
        if research is None or not research.enabled:
            return
        now = time.time()
        if now - getattr(self, "_last_research", 0.0) < research.every_s:
            return
        self._last_research = now
        pid = research.due()
        if pid is None:
            return
        try:
            with self._lock:
                out = research.run_cycle(pid)
        except Exception as e:                       # never stops the loop
            self.log.warning("research cycle failed: %s", e)
            return
        if out.get("paused"):
            self.log.info("research project %s paused: %s", pid, out["paused"])

    def _backup_tick(self):
        """Copy the memory off the box, on its own slow clock.

        Set by main.py; absent or disabled means no bucket is configured and
        the memory lives on exactly one volume, which is the state this
        exists to end.
        """
        backup = getattr(self.agent, "memory_backup", None)
        if backup is None or not backup.enabled or not backup.due():
            return
        try:
            backup.run()
        except Exception as e:                       # never stops the loop
            self.log.warning("memory backup failed: %s", e)

    def _diary_tick(self):
        """Ask the diary which moments have come round.

        On the idle path beside consolidation and the backup, and for the
        same reason: a reminder the planner has to decide to send is one it
        will skip on the day there is something more interesting to do. It
        costs nothing when nothing is due, and it is the only part of this
        loop whose failure the operator would feel directly.
        """
        diary = getattr(self.agent, "diary", None)
        if diary is None:
            return
        # Roll any finished repeat forward first, so the reminder the diary
        # is about to look at belongs to the occurrence in front rather than
        # to one that ended an hour ago.
        # A hunch past its window has passed. Resolved out loud rather than
        # forgotten -- the ones that quietly never happen are the entries the
        # calibration is actually built from.
        felt = getattr(self.agent, "hunches", None)
        if felt is not None:
            try:
                for gone in felt.tick().get("passed", []):
                    self.log.info("Hunch passed (%.0f%%): %s",
                                  gone["confidence"] * 100, gone["about"])
            except Exception as e:
                self.log.warning("hunch tick failed: %s", e)
        calendar = getattr(self.agent, "schedule", None)
        if calendar is not None:
            try:
                for rolled in calendar.tick().get("rolled", []):
                    self.log.info("Calendar: %s now on %s", rolled["what"],
                                  rolled["now_on"])
            except Exception as e:
                self.log.warning("calendar tick failed: %s", e)
        try:
            report = diary.tick()
        except Exception as e:                       # never stops the loop
            self.log.warning("diary tick failed: %s", e)
            return
        for entry in report.get("sent", []):
            self.log.info("Diary: told him about '%s'%s", entry["what"],
                          " (late)" if entry.get("late") else "")
        for entry in report.get("unsaid", []):
            self.log.warning("Diary: never managed to say '%s': %s",
                             entry["what"], entry.get("reason"))
        if report.get("sent") or report.get("unsaid"):
            self._write_status_file()

    def _diary_wait(self) -> Optional[float]:
        """Seconds until the diary's next moment, or None if it has none."""
        diary = getattr(self.agent, "diary", None)
        if diary is None:
            return None
        try:
            soon = diary.seconds_until_next()
        except Exception:
            return None
        return None if soon is None else max(1.0, soon)

    def wake(self):
        """Cut short an idle wait: something new arrived for the planner."""
        self.idle_streak = 0
        self._wake.set()

    def run(self) -> int:
        """Run until stopped or max_cycles reached. Returns cycles run."""
        self.agent.running = True
        self.start_status_server()
        self.start_ui_server()
        self._write_status_file()
        cycles = 0
        self.log.info("Headless loop started (interval=%.1fs, max_cycles=%s)",
                      self.interval, self.max_cycles or "unlimited")
        try:
            while not self._stop.is_set():
                with self._lock:
                    result = self.agent.run_cycle()
                result["timestamp"] = time.time()
                self.last_cycle = result
                cycles += 1
                action = result.get("action", "idle")
                if action != "idle":
                    ok = result.get("result", {}).get("success")
                    self.log.info("cycle %d: %s -> %s", result["cycle"], action,
                                  "ok" if ok else "failed")
                self._write_status_file()
                try:
                    self.agent.ledger.tick()
                except Exception as e:  # the witness copy never stops the loop
                    self.log.warning("ledger anchor tick failed: %s", e)
                self._consolidation_tick()
                self._research_tick()
                self._backup_tick()
                self._diary_tick()
                if self.max_cycles and cycles >= self.max_cycles:
                    break
                # Idle cycles wait the full interval; successful work goes
                # straight on; a failure waits the full interval (times the
                # streak, capped) so a confused planner cannot burn its call
                # budget retrying a bad idea every second.
                if action == "idle":
                    self.idle_streak += 1
                    self.repeat_streak = 0
                    wait = min(self.interval * self.idle_streak, self.max_idle_wait)
                elif result.get("result", {}).get("success"):
                    self.failure_streak = 0
                    self.idle_streak = 0
                    if action == self._last_action:
                        # Doing the identical thing again is a loop, not
                        # progress; back off as if idle.
                        self.repeat_streak += 1
                        wait = min(self.interval * self.repeat_streak, self.max_idle_wait)
                    else:
                        self.repeat_streak = 0
                        wait = min(self.interval, 1.0)
                else:
                    self.failure_streak += 1
                    self.idle_streak = 0
                    self.repeat_streak = 0
                    wait = min(self.interval * self.failure_streak, self.max_failure_wait)
                    if self.failure_streak >= 3:
                        self.log.warning("%d consecutive failed tasks; waiting %.0fs before "
                                         "the next cycle", self.failure_streak, wait)
                # A reminder's punctuality is not the planner's cost problem.
                # The backoff above exists so a confused model cannot retry a
                # bad idea every second; applied to the diary it means a 9am
                # commitment goes out whenever the loop next happens to look,
                # which on an idle streak is up to five minutes later.
                soon = self._diary_wait()
                if soon is not None:
                    wait = min(wait, soon)
                self._last_action = action
                self._wake.clear()
                self._wake.wait(wait)
                if self._stop.is_set():
                    break
        finally:
            self.agent.running = False
            try:
                self.agent.ledger.tick(force=True)
            except Exception as e:
                self.log.warning("final ledger anchor upload failed: %s", e)
            self._write_status_file()
            self.stop_status_server()
            self.log.info("Headless loop stopped after %d cycles", cycles)
        return cycles
