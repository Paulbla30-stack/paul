"""
Jarvis Headless Runner

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
    POST /chat     -> JSON {"messages": [{role, content}, ...]}; multi-turn answer
    POST /upload   -> raw file body with X-Filename; saved under the upload dir
    GET  /uploads  -> files the agent has been given
    GET  /ui       -> the web UI (chat, agent panel, uploads)   (no token; the
                      page asks for the token and sends it with every call)

A second listener (``ui`` settings) can serve the same handler on a
public address with TLS, so the UI works from a phone without a tunnel.

    GET  /lab      -> behaviour-lab dials, current settings, locked layers
    POST /lab/settings {settings, apply_to_chat}, /lab/preview {settings},
         /lab/run {question, settings?, compare, mode: answer|plan}
    GET  /ledger, /ledger/tail?limit=N, /ledger/verify, /ledger/pubkey
                  -> Glass Ledger status, last entries, on-box verdict, key

Everything except /health and /status requires the runner token, sent as
``Authorization: Bearer <token>`` or ``X-Jarvis-Token``. The token is
taken from ``cloud.status_token`` or generated at start and written 0600
to ``cloud.status_token_file`` (default /run/jarvis/token). Loopback is
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
DEFAULT_UPLOAD_DIR = "/var/lib/jarvis/uploads"
DEFAULT_TLS_DIR = "/etc/jarvis/tls"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

DEFAULT_STATUS_PORT = 8471
DEFAULT_STATUS_FILE = "/run/jarvis/status.json"
DEFAULT_TOKEN_FILE = "/run/jarvis/token"
# Survives a restart, unlike the runtime directory copy above.
DEFAULT_TOKEN_PERSIST_FILE = "/etc/jarvis/token"
# What the loopback status API (127.0.0.1) serves without a token: the box's
# own `jarvis --status` reads it, and nothing off the box can reach it.
OPEN_PATHS = ("/", "/health", "/status")
# What the UI listener serves without a token. It is reachable from wherever
# ui_cidr allows, so it gives away nothing but liveness; /status on this
# listener would hand a stranger the model ARN, the goals and the agent's
# last reasoning.
UI_OPEN_PATHS = ("/health",)
# Addresses that can only be the box itself, and so the tunnel.
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


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
                 max_idle_wait: Optional[float] = None):
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
        from jarvis.cloud import session as _session
        self.session_days = max(1, min(int(session_days or 30), _session.MAX_DAYS))
        self.session_key_file = session_key_file or _session.DEFAULT_KEY_FILE
        self.session_key = _session.load_or_create_key(self.session_key_file, self.log)
        ui = dict(ui or {})
        self.ui_enabled = bool(ui.get("enabled"))
        self.ui_host = ui.get("host") or "0.0.0.0"
        self.ui_port = int(ui.get("port") or 8443)
        self.ui_tls = bool(ui.get("tls", True))
        self.tls_dir = ui.get("tls_dir") or DEFAULT_TLS_DIR
        self.upload_dir = ui.get("upload_dir") or DEFAULT_UPLOAD_DIR
        self.max_upload_bytes = int(float(ui.get("max_upload_mb") or 50) * 1024 * 1024)
        self._ui_server: Optional[ThreadingHTTPServer] = None
        self._ui_thread: Optional[threading.Thread] = None
        # Brute-force guard for the token: per client address, failures in
        # the last window; from the Nth failure on, 429 until the window passes.
        self._auth_failures: dict = {}
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
        # Behaviour lab: the current dial settings and whether live chat uses them.
        from jarvis.brain import dials as _dials
        self.lab_settings = _dials.defaults()
        self.lab_apply_to_chat = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        # Serialises agent cycles between the loop and HTTP-triggered work.
        self._lock = threading.RLock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self.last_cycle: dict = {}

    # ---- status --------------------------------------------------------

    def lab_state(self) -> dict:
        from jarvis.brain import dials as _dials
        state = _dials.registry()
        state["settings"] = dict(self.lab_settings)
        state["fingerprint"] = _dials.fingerprint(self.lab_settings)
        state["apply_to_chat"] = self.lab_apply_to_chat
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

    def _write_status_file(self):
        if not self.status_file:
            return
        try:
            with self._lock:
                snapshot = self.snapshot()
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

            def _authorised(self) -> bool:
                if runner.auth_locked(self._client()):
                    return False
                if runner.session_valid(self.headers.get("Cookie")):
                    return True
                header = self.headers.get("Authorization") or ""
                supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
                supplied = supplied or (self.headers.get("X-Jarvis-Token") or "").strip()
                ok = bool(supplied) and secrets.compare_digest(supplied, runner.token)
                if not ok and supplied:
                    runner.auth_failed(self._client())
                return ok

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

            def _send_html(self, html: str):
                body = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                raw_path, _, query = self.path.partition("?")
                path = raw_path.rstrip("/") or "/"
                if path == "/ui":
                    return self._send_html(runner.ui_html())
                if not self._require_token(path):
                    return
                if path == "/uploads":
                    return self._send(200, runner.list_uploads())
                limit = 20
                search = ""
                for part in query.split("&"):
                    if part.startswith("limit="):
                        try:
                            limit = max(1, min(int(part[6:]), 200))
                        except ValueError:
                            pass
                    elif part.startswith("q="):
                        search = urllib.parse.unquote_plus(part[2:])[:200].strip()
                with runner._lock:
                    if path in ("/", "/health"):
                        if loopback_only:
                            self._send(200, {"ok": True, "agent": runner.agent.name,
                                             "cycles": runner.agent.cycle_count})
                        else:
                            self._send(200, {"ok": True})
                    elif path == "/status":
                        self._send(200, runner.snapshot())
                    elif path == "/memory":
                        self._send(200, runner.agent.memory.get_summary())
                    elif path == "/proposals":
                        self._send(200, {
                            "rung": runner.agent.rung,
                            "proposals": list(runner.agent.proposals)[-limit:]})
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
                    elif path == "/lab":
                        self._send(200, runner.lab_state())
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
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length).decode("utf-8", errors="replace") if length else ""

            def do_POST(self):
                raw_path, _, query = self.path.partition("?")
                path = raw_path.rstrip("/") or "/"
                # Login and logout come before the auth check: one is how you
                # get authorised, and the other only clears your own cookie.
                if path == "/login":
                    if runner.auth_locked(self._client()):
                        return self._send(429, {"error": "too many failed logins; try again later"})
                    if int(self.headers.get("Content-Length") or 0) > 4096:
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
                    return self._send(200, {"ok": True},
                                      headers=[("Set-Cookie", runner.clear_session_cookie())])
                if not self._authorised():
                    return self._deny()
                if path == "/upload":
                    return self._handle_upload()
                if int(self.headers.get("Content-Length") or 0) > 1_000_000:
                    return self._send(413, {"error": "body too large"})
                body = self._body().strip()
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
                    settings = runner.lab_settings if runner.lab_apply_to_chat else None
                    with runner._lock:
                        answer = runner.agent.chat(turns, settings=settings)
                    runner.wake()
                    self._send(200, {"answer": answer, "dials": settings is not None})
                elif path in ("/lab/settings", "/lab/preview", "/lab/run"):
                    try:
                        payload = json.loads(body or "{}")
                    except ValueError:
                        return self._send(400, {"error": "body must be JSON"})
                    if not isinstance(payload, dict):
                        return self._send(400, {"error": "JSON object required"})
                    from jarvis.brain import dials as _dials
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
                length = int(self.headers.get("Content-Length") or 0)
                if length > limit:
                    return None
                return self.rfile.read(length) if length else b""

            def _handle_upload(self):
                name = runner.safe_filename(self.headers.get("X-Filename") or "")
                if not name:
                    return self._send(400, {"error": "X-Filename header required"})
                length = int(self.headers.get("Content-Length") or 0)
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
            target=self._server.serve_forever, name="jarvis-status",
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
        from jarvis.cloud import session as _session
        return _session.verify(self.session_key,
                               _session.from_cookie_header(cookie_header))

    def new_session_cookie(self) -> Optional[str]:
        """A Set-Cookie value for a fresh session, or None when disabled."""
        if not self.session_key:
            return None
        from jarvis.cloud import session as _session
        return _session.cookie_header(
            _session.issue(self.session_key, self.session_days),
            self.session_days, secure=self.ui_tls)

    def clear_session_cookie(self) -> str:
        from jarvis.cloud import session as _session
        return _session.clear_header(secure=self.ui_tls)

    def auth_locked(self, client: str) -> bool:
        now = time.time()
        stamps = [t for t in self._auth_failures.get(client, []) if now - t < self.auth_failure_window]
        self._auth_failures[client] = stamps
        return len(stamps) >= self.auth_failure_limit

    def auth_failed(self, client: str):
        self._auth_failures.setdefault(client, []).append(time.time())
        if len(self._auth_failures[client]) == self.auth_failure_limit:
            self.log.warning("Too many bad tokens from %s; locked out for %.0fs",
                             client, self.auth_failure_window)

    # ---- UI listener ----------------------------------------------------------

    def ui_html(self) -> str:
        try:
            with open(UI_HTML_PATH, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return "<!doctype html><title>Jarvis</title><p>UI page not installed.</p>"

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
                 "-days", "3650", "-subj", f"/CN={self.agent.name or 'jarvis'}",
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
            server = ThreadingHTTPServer((self.ui_host, self.ui_port),
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
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            scheme = "https"
        server.daemon_threads = True
        self._ui_server = server
        self._ui_thread = threading.Thread(target=server.serve_forever, name="jarvis-ui",
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
                if self.max_cycles and cycles >= self.max_cycles:
                    break
                # Idle cycles wait the full interval; successful work goes
                # straight on; a failure waits the full interval (times the
                # streak, capped) so a confused planner cannot burn its call
                # budget retrying a bad idea every second.
                if action == "idle":
                    self.idle_streak += 1
                    wait = min(self.interval * self.idle_streak, self.max_idle_wait)
                elif result.get("result", {}).get("success"):
                    self.failure_streak = 0
                    self.idle_streak = 0
                    wait = min(self.interval, 1.0)
                else:
                    self.failure_streak += 1
                    self.idle_streak = 0
                    wait = min(self.interval * self.failure_streak, self.max_failure_wait)
                    if self.failure_streak >= 3:
                        self.log.warning("%d consecutive failed tasks; waiting %.0fs before "
                                         "the next cycle", self.failure_streak, wait)
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
