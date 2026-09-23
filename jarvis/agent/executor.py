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
    r">{1,2}\s*['\"]?/(?:boot/|etc/fstab|etc/ld\.so\.preload|etc/sysctl|usr/lib/sysctl\.d|run/sysctl\.d|proc/sysrq-trigger|proc/sys/)",
    _CMD + r"(?:sed\s+-i|tee|truncate|cp|mv|install)\b.*\s['\"]?/(?:boot/|etc/fstab|etc/ld\.so\.preload|etc/sysctl|usr/lib/sysctl\.d|run/sysctl\.d|proc/sys/)",
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
    # and the UI's own TLS private key, which was not here. Whoever holds it
    # can impersonate the interface the operator logs in to. Found by the test
    # that asserts this list and environment.SECRET_PATHS cover the same
    # ground: the path fence named it and the shell did not.
    r"/etc/jarvis/tls\b",
    _CMD + r"cloudflared\b",
    r"\baws\s+ssm\s+get-parameter",
    r"~?/\.(?:ssh|aws|config/anthropic)\b",
    _CMD + r"crontab\s+(?:-\S+\s+)*-r\b",
    _CMD + r"cloud-init\s+clean\b",
    # --- the Glass Ledger: evidence about the agent, never context for it ---
    r"/(?:var/lib|etc)/jarvis/ledger",
    # ...and the program that says whether the chain is intact. The chain,
    # its signing key and the audit of it were named; the verifier itself was
    # not, so `cat /usr/lib/jarvis/jarvis/ledger/verify.py` and `rm` of the
    # same path were both allowed. Off-box verification limits the damage only
    # where the off-box machine runs its own copy rather than fetching this
    # one. Found by the standing refusals suite, 23 September 2026.
    r"\bjarvis/ledger\b",
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


# What the planner is told when a command is refused. The whole vocabulary,
# and none of it varies with the command.
#
# It used to be told "command matches deny pattern: <regex>". A model handed
# the rule it tripped is a model that has been given the rule's edges, and the
# pattern for the protected paths names several targets at once, so asking to
# read one disclosed the existence of the others. That is the failure
# authority.py was written about, one layer down: refused with a mechanism, it
# looks for a command the mechanism does not name; refused with a kind, there
# is nothing to rephrase. Found by the standing refusals suite, 23 September
# 2026 -- the suite's own check looked for forbidden words rather than
# asserting the shape, so it passed while the string leaked.
#
# The detail is not lost, it is redirected: refusal_detail() gives the
# operator and the ledger the pattern that fired. Only the model gets the kind.
REFUSAL_OUT_OF_SCOPE = "out_of_scope"
REFUSAL_SHELL_DISABLED = "shell_disabled"
REFUSAL_MALFORMED = "malformed_command"
REFUSAL_KINDS = frozenset({REFUSAL_OUT_OF_SCOPE, REFUSAL_SHELL_DISABLED,
                           REFUSAL_MALFORMED})


def _refusal(command: str, policy: dict):
    """(kind, detail) when ``command`` must not run, else None.

    One pass, two audiences: the kind is for the model, the detail for the
    operator, the log and the chain.
    """
    if not policy.get("enabled"):
        return (REFUSAL_SHELL_DISABLED,
                "shell execution disabled by policy (llm.shell.enabled)")
    if not command or not command.strip():
        return REFUSAL_MALFORMED, "empty command"
    if "\x00" in command:
        return REFUSAL_MALFORMED, "command contains NUL byte"
    compiled = policy.get("_compiled")
    if compiled is None:
        compiled = _compile_patterns(policy.get("deny_patterns") or [])
    candidates = [command] + [part for part in _SPLIT.split(command) if part]
    for rx in compiled:
        for text in candidates:
            if rx.search(text):
                return (REFUSAL_OUT_OF_SCOPE,
                        f"command matches deny pattern: {rx.pattern}")
    return None


def check_command_allowed(command: str, policy: dict) -> Optional[str]:
    """The refusal kind when ``command`` must not run, else ``None``.

    This is the string the model may see. It is one of REFUSAL_KINDS and
    says nothing about how the decision was reached.
    """
    found = _refusal(command, policy)
    return None if found is None else found[0]


def refusal_detail(command: str, policy: dict) -> Optional[str]:
    """Why ``command`` was refused, for the operator, the log and the ledger.

    Never put the result of this in anything the model reads. brain/llm.py
    carries result["error"] and result["output"] into the context; it carries
    nothing else.
    """
    found = _refusal(command, policy)
    return None if found is None else found[1]


class TaskExecutor:
    """
    Executes tasks generated by the planner.

    Maps task types to execution handlers and manages hardware interaction
    during task execution.
    """

    def __init__(self, hardware: dict, memory: AgentMemory, logger: logging.Logger,
                 shell_policy: Optional[dict] = None, notifier=None, estate=None):
        self.hardware = hardware
        self.memory = memory
        self.log = logger.getChild("executor")
        self.shell_policy = normalise_shell_policy(shell_policy, self.log)
        # Set by the agent; None leaves the executor unbounded, as before.
        self.rung = None
        # The one way out to the operator. None means nothing was configured,
        # and the handler says so rather than pretending the message landed.
        self.notifier = notifier
        # Read-only view of spend and what the account is accumulating.
        self.estate = estate
        # Where documents the agent writes are put. One directory, set from
        # config at boot, never from the task: a caller that could choose the
        # directory could choose any directory.
        self.document_dir: str = ""
        # Whether a scan or a photograph may have its characters recognised.
        # Off unless the operator says otherwise: it sends a page image out to
        # a service and bills per page. Set by the agent from config.
        self.document_ocr: dict = {}

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
            TaskType.READ_FILE: self._handle_read_file,
            TaskType.COMPOSE_DOCUMENT: self._handle_compose_document,
            TaskType.READ_LOGS: self._handle_read_logs,
            TaskType.NOTIFY_OPERATOR: self._handle_notify_operator,
            TaskType.ESTATE_REPORT: self._handle_estate_report,
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

    # Units the agent may read the journal of. Its own service, its own health
    # timer, and the tunnel it depends on to be reachable at all.
    #
    # An allowlist rather than a pattern, because "read any unit's log" is a
    # different and much larger capability than "find out what you did". The
    # agent runs as root: sshd, audit and every other service on the box are a
    # systemctl argument away, and none of them are its business.
    READABLE_UNITS = ("jarvis", "jarvis-health", "cloudflared")
    MAX_LOG_LINES = 200
    MAX_LOG_MINUTES = 180

    def _handle_read_logs(self, task: Task) -> dict:
        """Read the agent's own recent journal, bounded.

        It could already reach this through shell_command and journalctl, which
        is precisely the problem: that route is one deny-list entry away from
        reading any unit on the machine, and it arrives as an unstructured wall
        of text. This is the narrow version -- three units, a capped window, a
        capped line count, no shell.
        """
        meta = task.metadata or {}
        unit = str(meta.get("unit") or "jarvis").strip()
        if unit not in self.READABLE_UNITS:
            return {"success": False,
                    "error": (f"{unit!r} is not a unit this agent may read. "
                              f"Readable: {', '.join(self.READABLE_UNITS)}.")}
        try:
            minutes = int(meta.get("minutes") or 15)
        except (TypeError, ValueError):
            minutes = 15
        minutes = max(1, min(self.MAX_LOG_MINUTES, minutes))
        grep = str(meta.get("grep") or "").strip()

        argv = ["journalctl", "-u", unit, "--since", f"-{minutes}min",
                "--no-pager", "-o", "short-iso", "-n", str(self.MAX_LOG_LINES)]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=20,
                                  env=scrub_env(os.environ))
        except FileNotFoundError:
            return {"success": False, "error": "journalctl is not on this machine"}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "journalctl timed out after 20s"}
        if proc.returncode != 0:
            return {"success": False,
                    "error": (proc.stderr or "journalctl failed").strip()[:300]}

        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        matched = None
        if grep:
            matched = len(lines)
            lines = [ln for ln in lines if grep in ln]
        lines = lines[-self.MAX_LOG_LINES:]
        text = "\n".join(lines)
        limit = int((self.shell_policy or {}).get("max_output") or 4000)
        truncated = len(text) > limit
        if truncated:
            text = text[-limit:]

        out = {"unit": unit, "minutes": minutes, "lines": len(lines), "log": text}
        if grep:
            out["grep"] = grep
            out["scanned"] = matched
        if truncated:
            out["truncated"] = True
        # An empty window is a fact, not a fault, and saying so plainly stops
        # the model reading silence as evidence that nothing happened.
        if not lines:
            out["note"] = (f"No lines from {unit} in the last {minutes} minutes"
                           + (f" matching {grep!r}" if grep else "")
                           + ". That is an empty window, not a clean bill of health.")
        self.memory.store(category="read_logs", data={"unit": unit, "minutes": minutes,
                                                      "lines": len(lines)})
        return {"success": True, "output": out}

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

    def _handle_read_file(self, task: Task) -> dict:
        """Read a text file the agent is allowed to see, bounded.

        inspect_path answers what is at a path; this answers what is in it.
        The operator can hand the agent a document and, until now, the agent
        could see its name, size and timestamp and not one word of it -- which
        is the same shape of half-built capability the tool register exists to
        stop, and the one most likely to be filled in with invention.

        Behind the same fence, which was widened first: it protected the
        ledger and the API key but not the runner token, the tunnel token, the
        UI signing key, the notify destination or the memory database. Reading
        metadata through that gap disclosed nothing; reading contents would
        have disclosed all of it.
        """
        from jarvis.agent import environment

        target = str(task.metadata.get("path")
                     or task.metadata.get("command") or "").strip()
        if not target:
            return {"success": False, "error": "read_file needs a path"}
        if not target.startswith("/"):
            return {"success": False,
                    "error": f"read_file needs an absolute path, got {target!r}"}
        try:
            start = int(task.metadata.get("from_line", 1))
        except (TypeError, ValueError):
            start = 1
        # OCR is off unless the operator turned it on: it sends a page image
        # out to a service and is billed per page, which is a decision about
        # his money and his documents rather than a default.
        cfg = getattr(self, "document_ocr", None) or {}
        result = environment.read_file(
            target, start_line=start,
            ocr=bool(cfg.get("enabled")), region=cfg.get("region"))
        if result.get("fenced"):
            self.log.warning("read_file refused %s: %s", target, result.get("reason"))
            return {"success": False, "error": f"refused: {result['reason']}",
                    "output": {"path": target, "refused": result["reason"]}}
        # What is recorded is that it was read and how much of it, never the
        # contents: memory is for what the agent concluded, and a file it can
        # re-read is not worth copying into a store it cannot manage.
        self.memory.store(category="read_file",
                          data={k: v for k, v in result.items() if k != "text"})
        # A file that cannot be read is an answer too, and a better one than a
        # description of what it probably contains.
        return {"success": True, "output": result}

    def _handle_compose_document(self, task: Task) -> dict:
        """Write a document the operator can open, and report what was written.

        The other direction from read_file. The agent could read a statement
        and could not produce a letter, so everything it composed lived in a
        chat window -- which cannot be printed, attached, filed or signed.

        Nothing about where it goes comes from the task. The directory is set
        from config at boot, the extension is decided by the format, the name
        is stripped to a basename and scrubbed, and the resolved path is
        checked against the directory again, because a scrub is a claim and a
        realpath is a fact. The task chooses the words; it does not choose the
        destination.
        """
        from jarvis.agent import compose as _compose

        content = str(task.metadata.get("content")
                      or task.metadata.get("command") or "")
        title = str(task.metadata.get("title") or task.description or "")
        fmt = str(task.metadata.get("format") or _compose.DEFAULT_FORMAT)
        name = str(task.metadata.get("name") or "")
        if not content.strip():
            # The description makes a serviceable title and is no use as a
            # body, so a task with no content is a mistake rather than a
            # request for a title page.
            return {"success": False,
                    "error": "compose_document needs content: the document body, "
                             "as markdown, in 'content'"}
        try:
            written = _compose.compose(
                content, title=title, fmt=fmt, name=name,
                directory=self.document_dir or _compose.DEFAULT_DIR)
        except _compose.ComposeError as why:
            # A refusal with its reason: the model can pick another format or
            # shorten the document, which it cannot do from "failed".
            self.log.warning("compose_document refused: %s", why)
            return {"success": False, "error": str(why)}
        except OSError as exc:
            self.log.warning("compose_document could not write: %s", exc)
            return {"success": False, "error": f"could not write the document: {exc}"}
        self.log.info("Wrote %s (%d bytes, sha256 %s)", written["path"],
                      written["bytes"], written["sha256"][:12])
        self.memory.store(category="compose_document", data=written)
        return {"success": True, "output": written}

    def _handle_estate_report(self, task: Task) -> dict:
        """What the account is spending and what it is holding on to.

        Reads only. It cannot delete and does not ask to: whether an old image
        is still wanted is judgement about the future, which belongs to the
        operator. A missing IAM grant comes back as "I cannot see that" rather
        than an error, because a partial view is still worth having.
        """
        if self.estate is None:
            return {"success": False, "error": "estate reporting not configured"}
        try:
            days = int((task.metadata or {}).get("days", 7))
        except (TypeError, ValueError):
            days = 7
        report = self.estate.report(days)
        report["summary"] = self.estate.lines(days)
        self.memory.store(category="estate", data=report)
        return {"success": True, "output": report}

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

        It used to return None -- permit -- when no rung had been set, on the
        reasoning that an executor nobody had configured should behave as it
        had before rungs existed. That made the backstop fail open in exactly
        the case it exists for: a task arriving down a path that never set the
        rung is the path the decision check was not on. A redundant check that
        permits when it is uninformed is not redundancy, it is an alternative
        way in. So an absent or unrecognised rung is the default rung, and
        normalise_rung decides that rather than this function guessing.
        """
        from jarvis.agent import authority
        verdict = authority.review(task, authority.normalise_rung(self.rung))
        return None if verdict.allowed else verdict.reason

    def _handle_shell_command(self, task: Task) -> dict:
        """Run a planner-chosen shell command under the shell policy."""
        refusal = self._authority_refusal(task)
        if refusal:
            self.log.warning("Shell command outside mandate: %s", refusal)
            return {"success": False, "error": f"outside mandate: {refusal}"}
        command = str(task.metadata.get("command") or "")
        found = _refusal(command, self.shell_policy)
        record = {"command": command, "goal": task.metadata.get("goal")}
        if found:
            kind, detail = found
            self.log.warning("Shell command refused (%s): %s", detail, command)
            # The kind goes in the record, because the record is inside
            # result["output"] and the model reads that. The detail travels
            # beside it, in a key nothing puts in the context, and act() lifts
            # it onto the chain.
            record["denied"] = kind
            self.memory.store(category="shell_command",
                              data=dict(record, denied_detail=detail))
            return {"success": False, "error": kind, "output": record,
                    "denied_detail": detail}

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
