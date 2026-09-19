"""
Jarvis Task Executor

Executes planned tasks by interacting with hardware and system interfaces.
"""

import os
import re
import subprocess
import logging
from typing import Any, Optional

from jarvis.agent.planner import Task, TaskType, TaskStatus
from jarvis.agent.memory import AgentMemory


# Guard rails for LLM-planned shell commands. This is a deny-list, not a
# sandbox: the agent runs with full access by design, these just stop the
# obviously catastrophic or self-defeating commands from ever running.
# Patterns are matched case-insensitively against the whole command line
# and against each simple command after splitting on ; && || | and newlines.
_FLAGS = r"(?:--?[\w=.,-]+\s+)*"          # any run of short/long options
_CMD = r"(?:^|[;&|(]\s*)(?:sudo\s+(?:-\S+\s+)*)?(?:\S*/)?"  # command position
_SYS_DIRS = (r"/(?:etc|usr|var|boot|bin|sbin|lib\S*|root|home|opt|proc|sys|dev|srv"
             r"|etc/jarvis|etc/systemd\S*|usr/lib/jarvis)?")
_ROOT_TARGET = r"(?:['\"]?)(?:" + _SYS_DIRS + r"|~|\$HOME|/\*)(?:/\*?)?(?:['\"]?)(?=[\s;&|>)]|$)"
_BLOCK_DEV = r"/dev/(?:sd|nvme|xvd|vd|hd|mapper/|md|disk/|loop|mem\b|kmem\b|port\b)"
_PROTECTED_UNITS = (r"(?:jarvis|jarvis-bootstrap|amazon-ssm-agent|snap\.amazon-ssm-agent\S*"
                    r"|sshd?|systemd-networkd|systemd-resolved|NetworkManager|cloud-init\S*)")

DEFAULT_SHELL_DENY_PATTERNS = [
    # --- filesystem wipes ---
    _CMD + r"rm\s+" + _FLAGS + _ROOT_TARGET,
    r"--no-preserve-root",
    _CMD + r"find\s+" + _ROOT_TARGET.replace(r"(?=[\s;&|>)]|$)", r"(?=\s)") + r".*(?:-delete|-exec\s+\S*rm\b)",
    _CMD + r"(?:chmod|chown|chgrp)\s+" + _FLAGS + r".*?-R.*?\s" + _ROOT_TARGET,
    _CMD + r"(?:chmod|chown|chgrp)\s+-R\s",
    # --- block devices ---
    _CMD + r"(?:mkfs\S*|mke2fs|mkswap|wipefs|blkdiscard|sgdisk|sfdisk|parted|fdisk|shred|badblocks)\b",
    _CMD + r"dd\b.*\bof=" + _BLOCK_DEV,
    r">{1,2}\s*['\"]?" + _BLOCK_DEV,
    _CMD + r"(?:cat|cp|tee|mv|install)\b.*\s['\"]?" + _BLOCK_DEV,
    # --- boot / critical files ---
    r">{1,2}\s*['\"]?/(?:boot/|etc/fstab|etc/ld\.so\.preload|etc/sysctl|proc/sysrq-trigger|proc/sys/)",
    _CMD + r"(?:sed\s+-i|tee|truncate|cp|mv)\b.*\s['\"]?/(?:boot/|etc/fstab|etc/ld\.so\.preload|etc/sysctl|proc/sys/)",
    r"/proc/sysrq-trigger",
    # --- kernel parameter tuning by another route ---
    # Writing /proc/sys is denied above. `sysctl -w k=v`, `sysctl k=v` and
    # `sysctl -p` are the same change with different syntax: a planner
    # refused once must not reach the same end by rephrasing. Reads
    # (sysctl -a, sysctl <key>) stay allowed.
    _CMD + r"sysctl\b[^|;&]*=",
    _CMD + r"sysctl\s+(?:-\S+\s+)*(?:-p|--load|--system)\b",
    # --- power / init / agent ---
    _CMD + r"(?:shutdown|reboot|halt|poweroff|telinit)\b",
    _CMD + r"init\s+[06]\b",
    _CMD + r"systemctl\s+(?:-\S+\s+)*(?:reboot|poweroff|halt|kexec|rescue|emergency|isolate)\b",
    _CMD + r"systemctl\s+(?:-\S+\s+)*(?:stop|disable|mask|kill|restart)\s+(?:-\S+\s+)*" + _PROTECTED_UNITS + r"\b",
    _CMD + r"(?:pkill|killall)\b.*\b(?:jarvis|python\S*|amazon-ssm-agent|ssm-agent|sshd)\b",
    _CMD + r"kill\s+(?:-\S+\s+)*(?:1|-1|\$\$|\$PPID|\$\(\s*pidof\s+\S*python)\b",
    r":\s*\(\s*\)\s*\{",
    # --- remote code / obfuscation ---
    r"\b(?:curl|wget|fetch)\b.*\|\s*(?:\S*/)?(?:sudo\s+)?(?:busybox\s+)?(?:(?:ba|z|da|k|a|c|tc|fi)?sh|python[0-9.]*|perl|ruby|node|php)\b",
    r"(?:\$\(|<\(|`)\s*(?:curl|wget|fetch)\b",
    r"\bbase64\s+(?:-d|--decode)\b.*\|\s*(?:\S*/)?(?:(?:ba|z|da|k|a)?sh|python[0-9.]*|perl)\b",
    r"\b(?:ba|z|da|k)?sh\s+-c\s+['\"]?\$\(",
    r"\beval\s+['\"]?\$\(",
    # --- network / firewall lockout ---
    r"\b(?:iptables|ip6tables|nft)\b.*(?:\s-F\b|\bflush\b|-P\s+(?:INPUT|OUTPUT)\s+DROP|\bdrop\b.*\b(?:input|output)\b)",
    _CMD + r"ip\s+link\s+set\s+\S+\s+down\b",
    _CMD + r"(?:ifdown|ifconfig\s+\S+\s+down)\b",
    _CMD + r"ip\s+route\s+(?:del|flush)\b",
    # --- credentials / accounts ---
    _CMD + r"(?:passwd|chpasswd|useradd|userdel|usermod|adduser|deluser|visudo)\b",
    r"/etc/(?:shadow|gshadow|sudoers)",
    r"/etc/jarvis/anthropic\.key|/proc/\S*/environ",
    # the operator's own credentials: the runner token and the key that
    # signs UI sessions are as good as a login, and the tunnel token is the
    # tunnel itself -- whoever holds it can re-point the hostname at their
    # own machine and collect the logins meant for this one
    r"/etc/jarvis/(?:token|session\.key|cloudflared\.env|notify\.dest)\b",
    _CMD + r"cloudflared\b",
    r"\baws\s+ssm\s+get-parameter",
    r"~?/\.(?:ssh|aws|config/anthropic)\b",
    _CMD + r"crontab\s+(?:-\S+\s+)*-r\b",
    _CMD + r"cloud-init\s+clean\b",
    # --- the Glass Ledger: evidence about the agent, never context for it ---
    r"/(?:var/lib|etc)/jarvis/ledger",
    # --- the agent's own durable memory: written through the agent, which
    #     keeps its provenance and dedupe, never edited underneath itself ---
    r"/var/lib/jarvis/memory\.db",
    r"\bjarvis\.ledger\b",
    # --- the agent's own control plane: its runner token, status API and UI ---
    r"/run/jarvis\b",
    r"(?<![\w.])(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\]|::1)(?::|\s+)(?:8471|8443)\b",
    r"\bAuthorization:\s*Bearer\b",
]

# Installing software or enabling repositories as root is denied unless the
# operator opts in: a planner that cannot find a tool tends to reach for the
# package manager instead of the task types it was given.
# Outbound network tools. Denied unless the operator opts in, for the same
# reason package installs are: the planner has no business reaching the
# internet, and the only thing that was stopping it was that `curl` happens
# not to be on the authority layer's read-only list. That is an accident, and
# an accident is not a control: `curl` reads a URL, so a later tidy-up of that
# list could reasonably add it and silently open egress. This states the
# intent where it cannot be mistaken for an oversight.
#
# This is a deny-list, so it is not complete. A planner with an interpreter
# can still open a socket. It is the third layer, behind the mandate rung and
# the security group; none of the three is load-bearing on its own.
EGRESS_DENY_PATTERNS = [
    _CMD + r"(?:curl|wget|fetch|aria2c|httpie|http|https)\b",
    _CMD + r"(?:nc|ncat|netcat|socat|telnet|ftp|tftp|lftp)\b",
    _CMD + r"(?:ssh|scp|sftp|rsync)\b",
    _CMD + r"(?:python[0-9.]*|perl|ruby|php|node)\s+-\S*[ce]\b[^|;&]*(?:urllib|requests|httpx|socket|http\.client|net/http|LWP|Net::)",
    _CMD + r"(?:bash|sh|zsh)\s+-c\b[^|;&]*(?:/dev/tcp/|/dev/udp/)",
    r"/dev/(?:tcp|udp)/",
]

PACKAGE_INSTALL_DENY_PATTERNS = [
    _CMD + r"(?:dnf|yum|apt|apt-get|microdnf|zypper|apk|pacman)\s+(?:-\S+\s+)*(?:install|reinstall|remove|erase|purge|upgrade|update|dist-upgrade|config-manager|copr|add-repository|groupinstall)\b",
    _CMD + r"(?:pip3?|pipx|python[0-9.]*\s+-m\s+pip|npm|yarn|gem|cargo|go)\s+(?:-\S+\s+)*install\b",
    _CMD + r"(?:amazon-linux-extras|snap|flatpak|brew)\s+(?:-\S+\s+)*(?:install|enable)\b",
    _CMD + r"(?:rpm|dpkg)\s+(?:-\S+\s+)*(?:-i|--install|-U|-e|--erase)\b",
    r"/etc/yum\.repos\.d/|/etc/apt/sources\.list",
    r"\bepel-release\b",
]

DEFAULT_SHELL_POLICY = {
    "enabled": False,
    "timeout": 60,
    "max_output": 4000,
    "cwd": "/",
    "deny_patterns": None,          # extra patterns, added to the defaults
    "allow_package_install": False,   # true = let the planner install software
    "allow_egress": False,            # true = let the planner reach the network
}

# Environment variables never handed to a planner-chosen shell command.
_SECRET_ENV = re.compile(r"(ANTHROPIC_|AWS_(SECRET|SESSION|ACCESS)|.*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)\w*$)",
                         re.IGNORECASE)


def scrub_env(env: dict) -> dict:
    """Copy of ``env`` without anything that looks like a credential."""
    return {k: v for k, v in env.items() if not _SECRET_ENV.match(k)}


def _compile_patterns(patterns, log=None) -> list:
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(str(pattern), re.IGNORECASE))
        except re.error as e:
            msg = f"invalid shell deny pattern {pattern!r}: {e}"
            (log or logging.getLogger("jarvis.executor")).error(msg)
    return compiled


def normalise_shell_policy(policy: Optional[dict], log=None) -> dict:
    """Fill in defaults; a missing or falsy policy means shell is disabled.

    Operator patterns are *added* to the built-in list; the built-in
    ceiling never shrinks by configuration (a ``replace_deny_patterns``
    key is ignored with a warning). Invalid patterns are logged and
    dropped, never silently ignored.
    """
    merged = dict(DEFAULT_SHELL_POLICY)
    if isinstance(policy, dict):
        merged.update({k: v for k, v in policy.items() if v is not None})
    if merged.pop("replace_deny_patterns", None):
        (log or logging.getLogger("jarvis.executor")).warning(
            "llm.shell.replace_deny_patterns is ignored: the built-in deny list never shrinks")
    extra = merged.get("deny_patterns")
    extra = [str(p) for p in extra] if isinstance(extra, list) else []
    patterns = list(DEFAULT_SHELL_DENY_PATTERNS) + extra
    if not merged.get("allow_package_install"):
        patterns += PACKAGE_INSTALL_DENY_PATTERNS
    if not merged.get("allow_egress"):
        patterns += EGRESS_DENY_PATTERNS
    merged["deny_patterns"] = patterns
    merged["_compiled"] = _compile_patterns(patterns, log)
    merged["enabled"] = bool(merged.get("enabled"))
    merged["timeout"] = max(1, int(merged.get("timeout") or 60))
    merged["max_output"] = max(200, int(merged.get("max_output") or 4000))
    return merged


_SPLIT = re.compile(r"\s*(?:;|&&|\|\||\||\n)\s*")


def check_command_allowed(command: str, policy: dict) -> Optional[str]:
    """Return a reason string when ``command`` must not run, else ``None``."""
    if not policy.get("enabled"):
        return "shell execution disabled by policy (llm.shell.enabled)"
    if not command or not command.strip():
        return "empty command"
    if "\x00" in command:
        return "command contains NUL byte"
    compiled = policy.get("_compiled")
    if compiled is None:
        compiled = _compile_patterns(policy.get("deny_patterns") or [])
    candidates = [command] + [part for part in _SPLIT.split(command) if part]
    for rx in compiled:
        for text in candidates:
            if rx.search(text):
                return f"command matches deny pattern: {rx.pattern}"
    return None


class TaskExecutor:
    """
    Executes tasks generated by the planner.

    Maps task types to execution handlers and manages hardware interaction
    during task execution.
    """

    def __init__(self, hardware: dict, memory: AgentMemory, logger: logging.Logger,
                 shell_policy: Optional[dict] = None, notifier=None):
        self.hardware = hardware
        self.memory = memory
        self.log = logger.getChild("executor")
        self.shell_policy = normalise_shell_policy(shell_policy, self.log)
        # Set by the agent; None leaves the executor unbounded, as before.
        self.rung = None
        # The one way out to the operator. None means nothing was configured,
        # and the handler says so rather than pretending the message landed.
        self.notifier = notifier

        # Map task types to handlers
        self._handlers = {
            TaskType.SYSTEM_CHECK: self._handle_system_check,
            TaskType.HARDWARE_PROBE: self._handle_hardware_probe,
            TaskType.SECURITY_SCAN: self._handle_security_scan,
            TaskType.USER_COMMAND: self._handle_user_command,
            TaskType.MAINTENANCE: self._handle_maintenance,
            TaskType.OBSERVATION: self._handle_observation,
            TaskType.GOAL_STEP: self._handle_goal_step,
            TaskType.CLOUD_PROBE: self._handle_cloud_probe,
            TaskType.SHELL_COMMAND: self._handle_shell_command,
            TaskType.INSPECT_PATH: self._handle_inspect_path,
            TaskType.NOTIFY_OPERATOR: self._handle_notify_operator,
        }

    def execute(self, task: Task) -> dict:
        """Execute a task and return results."""
        handler = self._handlers.get(task.task_type)
        if handler is None:
            return {
                "success": False,
                "error": f"No handler for task type: {task.task_type}",
            }

        try:
            result = handler(task)
            task.status = TaskStatus.COMPLETED
            return result
        except Exception as e:
            self.log.error("Task execution error: %s", e)
            task.status = TaskStatus.FAILED
            return {"success": False, "error": str(e)}

    def _handle_system_check(self, task: Task) -> dict:
        """Check system resources and health."""
        info = {}

        # CPU info
        try:
            with open("/proc/cpuinfo", "r") as f:
                cpuinfo = f.read()
            cpu_count = cpuinfo.count("processor\t:")
            model = ""
            for line in cpuinfo.split("\n"):
                if line.startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
            info["cpu"] = {"cores": cpu_count, "model": model}
        except FileNotFoundError:
            info["cpu"] = {"error": "/proc/cpuinfo not available"}

        # Memory info
        if self.hardware.get("memory"):
            info["memory"] = self.hardware["memory"].get_stats()
        else:
            try:
                with open("/proc/meminfo", "r") as f:
                    meminfo = f.read()
                info["memory"] = {"raw": meminfo[:500]}
            except FileNotFoundError:
                info["memory"] = {"error": "not available"}

        # Uptime
        try:
            with open("/proc/uptime", "r") as f:
                uptime = float(f.read().split()[0])
            info["uptime_seconds"] = uptime
        except (FileNotFoundError, ValueError):
            pass

        # Load average
        try:
            with open("/proc/loadavg", "r") as f:
                info["loadavg"] = f.read().strip()
        except FileNotFoundError:
            pass

        self.memory.store(category="system_check", data=info)
        return {"success": True, "output": info}

    def _handle_hardware_probe(self, task: Task) -> dict:
        """Probe and enumerate hardware devices."""
        devices = {}

        # Display
        if self.hardware.get("display"):
            try:
                devices["display"] = self.hardware["display"].get_state()
            except Exception as e:
                devices["display"] = {"error": str(e)}

        # Input devices
        if self.hardware.get("input"):
            try:
                devices["input"] = self.hardware["input"].list_devices()
            except Exception as e:
                devices["input"] = {"error": str(e)}

        # Storage
        if self.hardware.get("storage"):
            try:
                devices["storage"] = self.hardware["storage"].get_devices()
            except Exception as e:
                devices["storage"] = {"error": str(e)}

        # PCI devices (if lspci available)
        try:
            result = subprocess.run(
                ["lspci", "-mm"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                devices["pci"] = result.stdout[:2000]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # USB devices
        try:
            result = subprocess.run(
                ["lsusb"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                devices["usb"] = result.stdout[:1000]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        self.memory.store(category="hardware_probe", data=devices)
        return {"success": True, "output": devices}

    def _handle_security_scan(self, task: Task) -> dict:
        """Run a security scan."""
        from jarvis.security.scanner import SecurityScanner
        scanner = SecurityScanner()
        report = scanner.full_scan()
        self.memory.store(category="security_scan", data=report)
        return {"success": True, "output": report}

    def _handle_inspect_path(self, task: Task) -> dict:
        """Look at a real path: a bounded listing or a stat, never a guess.

        This exists so the planner can check the filesystem instead of
        recalling it. It only reads, and it refuses the same paths the shell
        deny-list refuses, so it is not a way around that fence.
        """
        from jarvis.agent import environment

        target = str(task.metadata.get("path")
                     or task.metadata.get("command") or "").strip()
        if not target:
            return {"success": False, "error": "inspect_path needs a path"}
        if not target.startswith("/"):
            return {"success": False,
                    "error": f"inspect_path needs an absolute path, got {target!r}"}

        reason = environment.fenced_reason(target)
        if reason:
            self.log.warning("inspect_path refused %s: %s", target, reason)
            return {"success": False, "error": f"refused: {reason}",
                    "output": {"path": target, "refused": reason}}

        try:
            depth = int(task.metadata.get("depth", 1))
        except (TypeError, ValueError):
            depth = 1
        info = environment.stat_path(target)
        if info.get("kind") == "dir":
            result = environment.tree(target, depth=depth)
        else:
            result = info
        self.memory.store(category="inspect_path", data=result)
        # A path that is not there is a useful answer, not a failure: it is
        # the answer that stops the planner inventing one.
        return {"success": True, "output": result}

    def _handle_notify_operator(self, task: Task) -> dict:
        """Say something to the operator when he is not looking at the UI.

        The planner chooses whether a thing is worth saying and how urgent it
        is. It does not choose who to tell, and it cannot talk its way past
        the limits: the destination and the rate rules live in the notifier,
        below this, set from config the agent cannot edit.

        A held message is a success, not a failure. The agent did the right
        thing by raising it; the notifier decided it was not worth a buzz, and
        the reason comes back so the planner learns the shape of the budget
        instead of retrying into it.
        """
        meta = task.metadata or {}
        subject = str(meta.get("subject") or task.description or "").strip()
        body = str(meta.get("body") or meta.get("detail") or "").strip()
        severity = str(meta.get("severity") or "notice").strip().lower()
        if not subject:
            return {"success": False, "error": "notify_operator needs a subject"}
        if self.notifier is None:
            return {"success": False,
                    "error": "no operator channel configured; nothing was sent"}
        verdict = self.notifier.send(subject, body, severity=severity,
                                     key=str(meta.get("key") or "") or None)
        self.memory.store(category="notification", data=verdict)
        return {"success": True, "output": verdict}

    def _handle_user_command(self, task: Task) -> dict:
        """Handle a user-initiated command via input devices."""
        event = task.metadata.get("event", {})
        self.log.info("Processing user input event: %s", event)

        # Store the event for the UI to process
        self.memory.store(
            category="user_input",
            data={"event": event, "processed": True},
        )
        return {"success": True, "output": {"event_processed": True}}

    def _handle_maintenance(self, task: Task) -> dict:
        """Handle system maintenance tasks."""
        description = task.description.lower()

        if "memory" in description:
            # Log memory state
            if self.hardware.get("memory"):
                stats = self.hardware["memory"].get_stats()
                self.log.warning("Memory maintenance: %s", stats)
                return {"success": True, "output": stats}

        if "storage" in description:
            # Check storage health
            if self.hardware.get("storage"):
                devices = self.hardware["storage"].get_devices()
                return {"success": True, "output": devices}

        return {"success": True, "output": {"action": "logged"}}

    def _handle_observation(self, task: Task) -> dict:
        """Passive observation of system state."""
        state = {}
        for name, hw in self.hardware.items():
            if hw is not None:
                try:
                    state[name] = hw.get_state() if hasattr(hw, "get_state") else "active"
                except Exception:
                    state[name] = "error"
        return {"success": True, "output": state}

    def _handle_cloud_probe(self, task: Task) -> dict:
        """Ask the cloud metadata service who and where we are."""
        from jarvis.cloud.imds import IMDSClient
        imds = IMDSClient()
        info = imds.summary()
        if not info:
            info = {"provider": "none", "available": False}
        else:
            info["available"] = True
        self.memory.store(category="cloud_probe", data=info)
        return {"success": True, "output": info}

    def _authority_refusal(self, task: Task):
        """Second check of the mandate, at the point of execution.

        The decision path refuses an out-of-scope task and turns it into a
        proposal. This is the backstop for a task that arrives another way:
        the ceiling should not depend on one code path being taken.
        """
        if self.rung is None:
            return None
        from jarvis.agent import authority
        verdict = authority.review(task, self.rung)
        return None if verdict.allowed else verdict.reason

    def _handle_shell_command(self, task: Task) -> dict:
        """Run a planner-chosen shell command under the shell policy."""
        refusal = self._authority_refusal(task)
        if refusal:
            self.log.warning("Shell command outside mandate: %s", refusal)
            return {"success": False, "error": f"outside mandate: {refusal}"}
        command = str(task.metadata.get("command") or "")
        denied = check_command_allowed(command, self.shell_policy)
        record = {"command": command, "goal": task.metadata.get("goal")}
        if denied:
            self.log.warning("Shell command refused (%s): %s", denied, command)
            record["denied"] = denied
            self.memory.store(category="shell_command", data=record)
            return {"success": False, "error": denied, "output": record}

        timeout = self.shell_policy["timeout"]
        limit = self.shell_policy["max_output"]
        self.log.info("Shell: %s", command)
        env = scrub_env(os.environ)
        env["JARVIS_TASK"] = "1"
        try:
            # New session so a timeout can kill the whole process group,
            # not just /bin/sh (backgrounded or exec'd children included).
            proc = subprocess.Popen(
                ["/bin/sh", "-c", command],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                text=True, cwd=self.shell_policy.get("cwd") or None, env=env,
                start_new_session=True,
            )
        except (FileNotFoundError, OSError) as e:
            record["error"] = str(e)
            self.memory.store(category="shell_command", data=record)
            return {"success": False, "error": str(e), "output": record}
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_group(proc)
            record["timeout"] = timeout
            self.memory.store(category="shell_command", data=record)
            return {"success": False, "error": f"timed out after {timeout}s", "output": record}
        finally:
            self._kill_group(proc, only_if_alive=True)

        def clip(text: str) -> str:
            if len(text) <= limit:
                return text
            return text[:limit] + f"\n...[{len(text) - limit} more chars truncated]"

        record.update({
            "returncode": proc.returncode,
            "stdout": clip(stdout or ""),
            "stderr": clip(stderr or ""),
        })
        self.memory.store(category="shell_command", data=record)
        if proc.returncode != 0:
            return {"success": False,
                    "error": f"exit status {proc.returncode}",
                    "output": record}
        return {"success": True, "output": record}

    @staticmethod
    def _kill_group(proc, only_if_alive=False):
        """Kill the command's whole process group (stragglers included)."""
        import signal
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            return
        if only_if_alive:
            # Reap leftovers of a finished command: children that outlived sh.
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            return
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass

    def _handle_goal_step(self, task: Task) -> dict:
        """Execute a step toward a goal."""
        goal = task.metadata.get("goal", "unknown")
        self.log.info("Working toward goal: %s", goal)

        # Store progress
        self.memory.store(
            category="goal_progress",
            data={"goal": goal, "step": task.description},
        )
        return {"success": True, "output": {"goal": goal, "progressed": True}}
