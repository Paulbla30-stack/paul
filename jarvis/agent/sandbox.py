"""Somewhere to run research code that cannot reach anything that matters.

Paul, 27 September 2026: research projects should be able to "try this code
or that", once he has verified the plan. Jarvis was asked how, before any of
it was built, and set the terms: Python only, standard library only, no
network, nothing of his memory or ledger in reach, wiped after every run, one
run at a time. He asked for 1 GB; the box has 1.9 GB and no swap with
Chromium running beside him, so the cap is 384 MB and he agreed to that.

The fence is systemd, the same kind of fence the browser has, and not this
file's good intentions. Each run is a transient unit started with
``systemd-run``:

  * ``DynamicUser`` -- a fresh uid that owns nothing, allocated per run;
  * ``PrivateNetwork`` plus ``IPAddressDeny=any`` -- no interface but a
    private loopback, and the cgroup drops anything that tries anyway;
  * ``ProtectSystem=strict``, ``ProtectHome``, ``PrivateTmp`` and the agent's
    own directories made inaccessible -- nothing to read, nowhere to keep;
  * ``MemoryMax``, ``CPUQuota``, ``TasksMax``, ``RuntimeMaxSec`` and a file
    size limit -- a runaway script is stopped by the kernel, not by hope;
  * no capabilities, no new privileges, a system-call filter.

The script goes in on stdin and never touches the disk. What comes back is
its output and exit status, capped. If ``systemd-run`` is missing, the code
tool is unavailable and says so: there is no unsandboxed fallback, ever.

Code written here is written by the model from quoted evidence, so a page
could steer what the code does. That is why the sandbox has nothing worth
steering it at: the worst a hostile page can do is make a wrong number.
"""

import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Callable, Optional

MAX_SOURCE = 20_000            # characters of script
MAX_OUTPUT = 12_000            # characters of stdout (and of stderr) kept
DEFAULTS = {"memory_mb": 384, "cpu_percent": 100, "timeout_s": 60, "tasks": 32,
            "file_mb": 100, "python": "/usr/bin/python3.11"}
LIMITS = {"memory_mb": (64, 512), "cpu_percent": (10, 100), "timeout_s": (5, 120),
          "tasks": (4, 64), "file_mb": (1, 100)}

# Directories that hold what the agent is. Made inaccessible inside the unit
# in addition to ProtectSystem, so a mistake in one does not open the other.
PRIVATE = ("/etc/jarvis", "/var/lib/jarvis", "/run/jarvis", "/run/jarvis-browser",
           "/var/lib/jarvis-browser", "/root", "/home")


class SandboxUnavailable(RuntimeError):
    """The sandbox cannot run here. Never a reason to run code without it."""


class Busy(RuntimeError):
    """One run at a time."""


class CodeSandbox:

    def __init__(self, config: Optional[dict] = None, runner: Callable = subprocess.run,
                 which: Callable = shutil.which, exists: Callable = os.path.exists,
                 clock: Callable[[], float] = time.time, logger: Optional[logging.Logger] = None):
        cfg = dict(config or {})
        self.limits = {}
        for key, default in DEFAULTS.items():
            value = cfg.get(key, default)
            if key in LIMITS:
                lo, hi = LIMITS[key]
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    value = default
                value = min(hi, max(lo, value))
            self.limits[key] = value
        self._run = runner
        self._which = which
        self._exists = exists
        self.clock = clock
        self.log = logger or logging.getLogger("jarvis.sandbox")
        self._one = threading.Lock()
        self.runs = 0

    def available(self) -> Optional[str]:
        """None when it can run; otherwise why not, in words for Paul."""
        if not self._which("systemd-run"):
            return "systemd-run is not on this machine, and code never runs outside the sandbox"
        if not self._exists(self.limits["python"]):
            return f"{self.limits['python']} is not installed"
        return None

    def command(self, name: str) -> list:
        """The systemd-run line. Everything that fences the run is here."""
        lim = self.limits
        props = [
            "DynamicUser=yes", "PrivateNetwork=yes", "IPAddressDeny=any",
            "RestrictAddressFamilies=AF_UNIX", "ProtectSystem=strict", "ProtectHome=yes",
            "PrivateTmp=yes", "PrivateDevices=yes", "PrivateUsers=yes", "NoNewPrivileges=yes",
            "CapabilityBoundingSet=", "AmbientCapabilities=", "ProtectKernelTunables=yes",
            "ProtectKernelModules=yes", "ProtectKernelLogs=yes", "ProtectControlGroups=yes",
            "ProtectClock=yes", "ProtectHostname=yes", "ProtectProc=invisible", "ProcSubset=pid",
            "RestrictNamespaces=yes", "RestrictRealtime=yes", "RestrictSUIDSGID=yes",
            "LockPersonality=yes", "SystemCallArchitectures=native",
            "SystemCallFilter=@system-service", "SystemCallFilter=~@privileged @resources",
            "UMask=0077", "WorkingDirectory=/tmp",
            f"MemoryMax={lim['memory_mb']}M", "MemorySwapMax=0",
            f"CPUQuota={lim['cpu_percent']}%", f"TasksMax={lim['tasks']}",
            f"RuntimeMaxSec={lim['timeout_s']}", f"LimitFSIZE={lim['file_mb'] * 1024 * 1024}",
            "LimitCORE=0",
        ] + [f"InaccessiblePaths=-{p}" for p in PRIVATE]
        # Not --quiet: with --wait, systemd-run reports on stderr how the unit
        # ended ("Finished with result: oom-kill"), which is how a kill for
        # memory or time is told apart from the script failing by itself.
        argv = ["systemd-run", "--wait", "--pipe", "--collect",
                f"--unit={name}", "--service-type=exec"]
        for p in props:
            argv += ["-p", p]
        # -I: isolated (no user site, no PYTHON* environment); -S: no site at
        # all, so nothing installed beyond the standard library is importable.
        return argv + ["--", lim["python"], "-I", "-S", "-"]

    def run(self, source: str) -> dict:
        """Run one script. Returns exit code, output, and what was run."""
        source = str(source or "")
        if not source.strip():
            raise ValueError("there is no code to run")
        if len(source) > MAX_SOURCE:
            raise ValueError(f"the script is longer than {MAX_SOURCE} characters")
        why = self.available()
        if why:
            raise SandboxUnavailable(why)
        if not self._one.acquire(blocking=False):
            raise Busy("another research run is still going")
        digest = hashlib.sha256(source.encode()).hexdigest()
        name = f"jarvis-research-{digest[:12]}-{int(self.clock())}"
        started = self.clock()
        try:
            try:
                done = self._run(self.command(name), input=source.encode(), capture_output=True,
                                 timeout=self.limits["timeout_s"] + 15)
                code, out, err, killed = done.returncode, done.stdout, done.stderr, False
            except subprocess.TimeoutExpired as exc:
                code, out, err, killed = None, exc.stdout or b"", exc.stderr or b"", True
            self.runs += 1
        finally:
            self._one.release()
        out = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out or "")
        err = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err or "")
        err, ended = _unit_report(err)
        stopped = {"oom-kill": "stopped: it went over the memory limit",
                   "timeout": "stopped: it ran past the time limit"}.get(ended)
        if killed:
            stopped = "stopped: it ran past the time limit"
        result = {"exit": code, "stdout": out[:MAX_OUTPUT], "stderr": err[-MAX_OUTPUT:],
                  "truncated": len(out) > MAX_OUTPUT or len(err) > MAX_OUTPUT,
                  "ended": ended or ("timeout" if killed else None), "stopped": stopped,
                  "timed_out": killed or ended == "timeout",
                  "sha256": digest, "unit": name, "seconds": round(self.clock() - started, 2),
                  "limits": {k: self.limits[k] for k in LIMITS}}
        self.log.info("research code run %s exit=%s in %.1fs", digest[:12], code, result["seconds"])
        return result


_REPORT = ("Running as unit:", "Finished with result:", "Main processes terminated with:",
           "Service runtime:", "CPU time consumed:", "Memory peak:", "Memory swap peak:",
           "IP traffic received:", "IP traffic sent:", "IO bytes read:", "IO bytes written:")


def _unit_report(err: str):
    """Split systemd-run's own report off the script's stderr.

    Returns (the script's stderr, how the unit finished or None).
    """
    kept, ended = [], None
    for line in err.splitlines():
        stripped = line.strip()
        if stripped.startswith("Finished with result:"):
            ended = stripped.split(":", 1)[1].strip() or None
        if any(stripped.startswith(tag) for tag in _REPORT):
            continue
        kept.append(line)
    return "\n".join(kept), ended


def build_sandbox(config: Optional[dict] = None, logger=None) -> Optional[CodeSandbox]:
    """The sandbox, or None unless research.sandbox.enabled is set."""
    cfg = ((config or {}).get("research") or {}).get("sandbox")
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return None
    return CodeSandbox(cfg, logger=logger)
