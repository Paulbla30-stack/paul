"""
OpenClaw Headless Runner

Replaces the interactive ConsoleUI when the agent is the primary process
on a server. It runs observe-plan-act-reflect cycles on a timer, writes a
status snapshot to disk, and serves the same snapshot over a loopback
HTTP endpoint so operators (or other agents) can ask "what are you doing?"
without a terminal.

Endpoints (default 127.0.0.1:8471):
    GET  /health   -> {"ok": true, ...}
    GET  /status   -> agent.get_status() plus runner info
    GET  /memory   -> agent memory summary
    GET  /history  -> last 20 task results
    GET  /brain    -> LLM brain status (or {"brain": null})
    GET  /goals    -> goals with completion state
    POST /goal     -> body is the goal text (optional "?priority=N"); adds a goal
    POST /think    -> one LLM planning step, executed; returns the outcome
    POST /ask      -> body is a question; returns {"answer": ...}
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

DEFAULT_STATUS_PORT = 8471
DEFAULT_STATUS_FILE = "/run/openclaw/status.json"


class HeadlessRunner:
    """Drive an AgentCore without a console."""

    def __init__(self, agent, logger, interval: float = 10.0,
                 max_cycles: int = 0, status_port: Optional[int] = None,
                 status_host: str = "127.0.0.1",
                 status_file: Optional[str] = None):
        self.agent = agent
        self.log = logger.getChild("headless")
        self.interval = max(0.0, float(interval))
        self.max_cycles = int(max_cycles or 0)
        self.status_port = status_port
        self.status_host = status_host
        self.status_file = status_file
        self.started_at = time.time()
        self._stop = threading.Event()
        # Serialises agent cycles between the loop and HTTP-triggered work.
        self._lock = threading.RLock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self.last_cycle: dict = {}

    # ---- status --------------------------------------------------------

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
        }
        return status

    def _write_status_file(self):
        if not self.status_file:
            return
        try:
            directory = os.path.dirname(self.status_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = self.status_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.snapshot(), f, default=str)
            os.replace(tmp, self.status_file)
        except OSError as e:
            self.log.debug("Could not write status file: %s", e)

    # ---- http ----------------------------------------------------------

    def _make_handler(self):
        runner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # quiet
                runner.log.debug("http %s", fmt % args)

            def _send(self, code: int, payload):
                body = json.dumps(payload, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?", 1)[0].rstrip("/") or "/"
                if path in ("/", "/health"):
                    self._send(200, {"ok": True, "agent": runner.agent.name,
                                     "cycles": runner.agent.cycle_count})
                elif path == "/status":
                    self._send(200, runner.snapshot())
                elif path == "/memory":
                    self._send(200, runner.agent.memory.get_summary())
                elif path == "/history":
                    self._send(200, runner.agent.task_history[-20:])
                elif path == "/brain":
                    brain = runner.agent.brain
                    self._send(200, {"brain": brain.status() if brain else None,
                                     "last_thought": runner.agent.last_thought or None})
                elif path == "/goals":
                    self._send(200, runner.agent.planner.goals)
                else:
                    self._send(404, {"error": "not found"})

            def _body(self) -> str:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length).decode("utf-8", errors="replace") if length else ""

            def do_POST(self):
                raw_path, _, query = self.path.partition("?")
                path = raw_path.rstrip("/") or "/"
                body = self._body().strip()
                if path == "/goal":
                    if not body:
                        return self._send(400, {"error": "goal text required in body"})
                    priority = 5
                    for part in query.split("&"):
                        if part.startswith("priority="):
                            try:
                                priority = max(0, min(int(part[9:]), 10))
                            except ValueError:
                                pass
                    runner.agent.add_goal(body, priority)
                    runner._write_status_file()
                    self._send(200, {"added": body, "priority": priority,
                                     "open_goals": len(runner.agent.planner.open_goals())})
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
                else:
                    self._send(404, {"error": "not found"})

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
            target=self._server.serve_forever, name="openclaw-status",
            daemon=True)
        self._server_thread.start()
        port = self._server.server_address[1]
        self.log.info("Status endpoint listening on http://%s:%d/status",
                      self.status_host, port)
        return port

    def stop_status_server(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # ---- loop ----------------------------------------------------------

    def stop(self):
        self._stop.set()
        self.agent.running = False

    def run(self) -> int:
        """Run until stopped or max_cycles reached. Returns cycles run."""
        self.agent.running = True
        self.start_status_server()
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
                if self.max_cycles and cycles >= self.max_cycles:
                    break
                # Idle cycles wait the full interval; busy ones go straight on
                wait = self.interval if action == "idle" else min(self.interval, 1.0)
                if self._stop.wait(wait):
                    break
        finally:
            self.agent.running = False
            self._write_status_file()
            self.stop_status_server()
            self.log.info("Headless loop stopped after %d cycles", cycles)
        return cycles
