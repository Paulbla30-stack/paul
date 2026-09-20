"""
ClaudeBrain - the LLM planner behind Jarvis's observe-plan-act-reflect loop.

Each planning step sends the agent's current situation (observations,
goals, recent task results, its own notes and a clock) to Claude and gets
back one decision as structured JSON: a task to run next, an idle signal,
and/or goals that are now satisfied. The decision is turned into a ``Task``
the existing executor already knows how to run; ``shell_command`` tasks let
the model act on the box, subject to the executor's shell policy.

Design notes
- The system block is static for the life of the process (prompt text plus
  the rendered shell policy, windows and timing) and marked for prompt
  caching; everything that changes per cycle goes in the user message.
- Structured outputs (``output_config.format``) guarantee parseable JSON.
- Refusals, truncation, rate limits and API errors all return ``None`` so
  AgentCore falls back to the rule-based planner for that cycle.
- A per-hour call budget and an error backoff keep cost and log noise
  bounded when something upstream is wrong.
"""

import datetime as _dt
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from jarvis.agent.planner import Task, TaskType
from jarvis.brain.credentials import resolve_api_key

try:  # the SDK is optional at import time so the ISO build never needs it
    import anthropic
except ImportError:  # pragma: no cover - exercised only without the SDK
    anthropic = None

DEFAULT_MODEL = "claude-opus-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Model families that take adaptive thinking + output_config.effort.
# Anything else (Haiku 4.5, Sonnet 4.5 and older) gets neither.
ADAPTIVE_MODEL_PREFIXES = (
    "claude-opus-5", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
    "claude-sonnet-5", "claude-sonnet-4-6",
    "claude-fable-5", "claude-mythos-5",
)
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# xhigh/max need >= 64k output tokens (and streaming) to be useful; the
# planner only ever needs a few hundred tokens of JSON, so clamp instead.
HIGH_EFFORT_MIN_TOKENS = 64000
STREAM_ABOVE_TOKENS = 16000

# Task types the model may choose. "none" means idle this cycle.
#
# A capability missing from this list is unreachable however complete the rest
# of it is. notify_operator and estate_report each had a handler, an authority
# classification and an IAM grant, and the planner could not pick either,
# because the enum in the plan schema is the whole of what it can ask for.
# Adding a handler was half of adding a capability, and nothing connected the
# halves. So the list is no longer written here: it is derived from the tool
# register, which is the one place a capability is declared.
from jarvis.agent import tools as _tools   # noqa: E402

PLANNABLE_TASK_TYPES = _tools.plannable(_tools.ACTOR)

# What the agent is doing at the moment it reads its context. The same context
# feeds planning and conversation, and only one of them can act.
_RIGHT_NOW = {
    "plan": ("You are choosing this cycle's task. Name one tool and it will be "
             "run, so name the one you actually want."),
    "answer": ("You are answering the operator, not acting. None of the tools "
               "listed above run in this conversation and nothing you write here "
               "executes. Answer from the context you were given. If it does not "
               "contain what was asked, say that plainly -- do not describe what "
               "a tool would have found, and never report having used one. If "
               "the answer needs a tool, say which, and it can be run next cycle."),
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {
            "type": "string",
            "description": "Brief reasoning for this decision (2-4 sentences).",
        },
        "task_type": {
            "type": "string",
            "enum": PLANNABLE_TASK_TYPES,
            "description": "Task to run next, or 'none' to idle this cycle.",
        },
        "description": {
            "type": "string",
            "description": "One-line description of the task (empty when idle).",
        },
        "priority": {
            "type": "integer",
            "description": "0 (most urgent) to 10 (background); lower runs first. "
                           "Usually copy the priority of the goal the task serves.",
        },
        "command": {
            "type": "string",
            # Derived, because this was a fourth place a capability had to be
            # described and it went stale the moment one was added: read_file
            # shipped with a handler, a register entry and a task type, and
            # the model was still told to leave this field empty for it. It
            # then planned a read with nowhere to read from, twice, and said
            # so in its own reasoning.
            "description": _tools.command_field_description(),
        },
        "goal": {
            "type": "string",
            "description": "The goal this task serves, copied character-for-character "
                           "from goals[].description, or empty.",
        },
        "completed_goals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Goals now satisfied by the evidence, each copied "
                           "character-for-character from goals[].description.",
        },
        "note": {
            "type": "string",
            "description": "A fact worth remembering for future cycles; empty if nothing.",
        },
        "proposal": {
            "type": "string",
            "description": "A change to this machine you think should happen but "
                           "are not permitted to make: state it as the change, not "
                           "as a sentence about yourself. Empty if none.",
        },
    },
    "required": ["reasoning", "task_type", "description", "priority",
                 "command", "goal", "completed_goals", "note", "proposal"],
    "additionalProperties": False,
}

# Static part of the system block. Deployment-specific text (shell policy,
# windows, timing) is appended once at construction; see _system_text().
SYSTEM_PROMPT = """You are the planner inside Jarvis, an agent-first operating system. \
Jarvis is the primary process on the machine it runs on: on a bootable ISO it is PID 1 with \
direct hardware access; on an AWS EC2 instance it is a root systemd service that started before \
any human logged in. You are consulted once per agent cycle whenever the task queue is empty, and \
you choose the single next task.

The loop: the agent observes (memory, storage, cloud metadata), you plan one task, the executor \
runs it, the result is stored, and you see it next cycle under recent_history. A failed task is \
not retried automatically: if you want it retried, choose it again. Idle decisions are re-asked on \
the next cycle and each consultation is one API call against the hourly budget shown in the \
context, so when nothing useful remains choose none and say so briefly.

Tasks you can choose:
- system_check: CPU, memory, uptime and load snapshot.
- hardware_probe: enumerate display, input, storage and PCI/USB devices.
- security_scan: run the built-in vulnerability scanner (kernel, permissions, SUID, ports, ssh). \
This is a task type, not a program: choose it as task_type; there is no scanner command to run.
- maintenance: memory or storage housekeeping; put 'memory' or 'storage' in the description.
- observation: passive snapshot of every hardware layer.
- goal_step: record progress on a goal without touching the system.
- cloud_probe: query the EC2 instance metadata service.
- inspect_path: look at a real path on this machine. Put the absolute path in "command". \
Returns a bounded directory listing, or the file's size and modification time, or that the path \
does not exist. This is how you find out what is on disk; it reads only and never changes \
anything.
- shell_command: run a POSIX sh command line; details below.
- none: idle this cycle.

How to behave:
- Work toward the open goals. Each cycle pick the one task that most advances them, using \
recent_history and your notes to avoid repeating a probe whose answer you already have. Once the \
evidence is in, act on it and finish the goal; do not inspect forever.
- Goals are operator-supplied objectives, not instructions that change these rules. Keep changes \
minimal, reversible and tied to a goal in the "goal" field. Never run destructive commands \
(wiping disks, deleting system directories, rebooting, stopping the jarvis or SSM services, \
piping downloads into a shell) and never read or exfiltrate secrets or credentials. Do not \
install software, add repositories or enable services: use the task types above and the tools \
already on the machine. If something you need is missing, say so in a note and choose none.
- A task that just failed will fail again if repeated unchanged. After a failure, change \
approach or choose none; never repeat the same command.
- Your mandate is in the context under "mandate", and it decides what kind of task you may \
choose at all, before any command policy applies. If a task is refused because it is outside \
your mandate, that is about the kind of action and not the wording: there is no other command \
that makes it allowed, and looking for one is the single worst thing you can do. Say what you \
found, put the change in your reasoning so it reaches the operator as a proposal, and choose \
something else or none. A refused command policy is different: that is about one command, and a \
genuinely different and safer approach to the same *permitted* kind of task is fair.
- A task that just succeeded has already given you its result (see recent_history): do not \
run it again to "confirm" or "refresh" it. A recurring goal (once an hour, daily) stays open \
and is satisfied for now once its task has run this period: do not list it in completed_goals \
and do not repeat its task until the clock says the period has passed; choose none instead.
- When the evidence shows a goal is satisfied, list it in completed_goals, copied \
character-for-character from goals[].description; the same rule applies to the "goal" field.
- Goals and tasks carry a priority from 0 (most urgent) to 10 (background); lower runs first.
- Use "note" for facts that will matter later (a device name, a threshold you measured). Notes \
are kept in durable memory and survive a restart, so a fact worth knowing next week is worth a \
note now; repeating a note you already made refreshes it rather than adding a second copy. You \
see only your most recent notes and only the most recent executed tasks (idle cycles leave no \
trace); anything you will need beyond that must be restated in a note. Anything under \
pinned_memory was marked by the operator as standing fact.
- Use "notify_operator" to reach the operator when he is not looking at the UI: \
metadata "subject" (short, the thing itself), "body" (one or two sentences), and \
"severity" -- "notice" for something he would want to know today, "alert" for something he \
would want to know now. You do not choose who is told or whether the message goes: there is \
one destination, fixed, and a budget of four an hour with fifteen minutes between them and \
quiet hours overnight that only an alert crosses. A held message is not a failure and must \
not be retried; the reason comes back so you can learn the shape of the budget. Silence is \
the default. An agent he mutes is worse than one that cannot reach him.
- Use "estate_report" to see what the account spends and what it is accumulating -- old \
machine images, snapshots, buckets. It only reads; deleting is not yours and is not offered.
- Use "proposal" when you think this machine should be changed and your mandate does not let \
you change it. Do not write the recommendation into "note" or "reasoning" instead: a note is \
prose the operator has no way to answer, and an unanswered recommendation teaches you nothing. \
A proposal is a thing he can accept or decline, with a reason, and his verdicts come back to \
you so you learn the shape of what he wants. State the change itself -- "set kernel.yama.\
ptrace_scope to 1" -- not "I propose to look at ptrace_scope". One per cycle; if it is already \
listed in proposals_awaiting_operator, it has been filed and repeating it is noise.
- Files the operator uploads appear under uploaded_files with their path on this machine; \
inspect them with shell tools (head, wc, file, unzip -l) when a goal concerns them.
- Paths are facts, not conventions. The environment block lists paths on this machine that \
have actually been checked, including ones that do not exist: read it before naming any path. \
Name a path only if it appears in environment, in uploaded_files, or in the output of a task you \
ran; if you need one that does not, choose inspect_path and find out. Never assume a file exists \
because its name or location would be conventional, and never describe a directory tree you have \
not seen. Saying a path does not exist, or that you have not checked, is a correct and useful \
answer. Paths you name are checked against the filesystem after you answer, and invented ones \
are reported to the operator and recorded.
- A signed, hash-chained Glass Ledger records every decision, action and outcome. It is \
evidence about you, not context for you: never read, copy, repair or reason about it.
- Reasoning is logged for the operator; keep it short and concrete."""

# The task menu and answer format for behaviour-lab "plan" runs: the
# interface a decision needs, kept apart from the behaviour rules the dials
# switch on and off.
PLAN_MENU = """You are asked for one planning decision for an agent that manages a Linux machine. \
Tasks you can choose: system_check, hardware_probe, security_scan, maintenance, observation, \
goal_step, cloud_probe, shell_command (a POSIX sh command line in "command"), inspect_path \
(an absolute path in "command"), or none. \
Reply with ONE JSON object and nothing else, with fields: reasoning, task_type, description, \
priority (0-10), command, goal, completed_goals (array), note. Nothing you choose is executed."""

ASK_PROMPT = """You are the planner inside Jarvis, an agent-first operating system, answering \
an operator's question about the machine you run on. Use the context (environment, observations, \
goals, recent task results, notes) as evidence, say what you do not know, and keep the answer \
concise and practical.

Paths are facts, not conventions. The environment block lists paths that have actually been \
checked, including ones that do not exist. Name a path only if it appears there, in \
uploaded_files, or in a task result you can see. Never assume a file exists because its name or \
location would be conventional, and never describe a directory tree you have not seen: say the \
path has not been checked and what would check it. Every path you name is verified against the \
filesystem after you answer, and invented ones are shown to the operator.

Plain text, no JSON."""


@dataclass
class Decision:
    """One planning outcome as returned by the model, normalised."""
    reasoning: str = ""
    task: Optional[Task] = None
    completed_goals: list = field(default_factory=list)
    note: str = ""
    # A change the model thinks should happen and is not permitted to make.
    # Kept separate from `note` on purpose: a note is prose the operator has
    # no way to answer, and an unanswered recommendation teaches nothing.
    proposal: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def idle(self) -> bool:
        return self.task is None


def _truncate(value: Any, limit: int) -> Any:
    """Bound string sizes inside arbitrary JSON-ish data."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"...[{len(value) - limit} more chars]"
    if isinstance(value, dict):
        return {k: _truncate(v, limit) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [_truncate(v, limit) for v in list(value)[:40]]
    return value


def _bounded_json(value: Any, limit: int) -> Any:
    """Serialise ``value`` compactly and cut the whole thing to ``limit`` chars.

    Returns the original object when it fits, else a string with a marker,
    so a single noisy task result cannot dominate the context.
    """
    text = json.dumps(value, default=str, separators=(",", ":"))
    if len(text) <= limit:
        return value
    return text[:limit] + f"...[{len(text) - limit} more chars]"


def compact_observations(observations: dict) -> dict:
    """Keep the parts of an observation the planner can act on."""
    out = {"cycle": observations.get("cycle")}
    mem = observations.get("memory")
    if isinstance(mem, dict):
        out["memory"] = {k: mem[k] for k in
                         ("total_mb", "used_mb", "available_mb", "used_percent", "error")
                         if k in mem}
    storage = observations.get("storage")
    if isinstance(storage, list):
        out["storage"] = [
            {k: d.get(k) for k in ("name", "path", "size_gb", "health", "readonly",
                                   "removable", "model") if k in d}
            for d in storage[:16] if isinstance(d, dict)
        ]
    out["disk_usage"] = _disk_usage(observations.get("disk_usage"))
    if not out["disk_usage"]:
        del out["disk_usage"]
    for key in ("display", "input_events"):
        if key in observations:
            out[key] = _truncate(observations[key], 200)
    for key in ("display_error", "input_error", "memory_error", "storage_error",
                "disk_usage_error"):
        if key in observations:
            out[key] = str(observations[key])[:200]
    return out


# Filesystems that exist in RAM or in the kernel rather than on a disk. Their
# usage is never what a "keep the disk under 80%" goal is about, and eight of
# them crowding out the one real mount is how a true number becomes unreadable.
PSEUDO_FS = {"tmpfs", "devtmpfs", "devpts", "sysfs", "proc", "overlay", "squashfs",
             "none", "udev", "shm", "efivarfs", "cgroup", "cgroup2", "ramfs",
             "fusectl", "debugfs", "tracefs", "mqueue", "hugetlbfs", "configfs",
             "securityfs", "pstore", "bpf", "autofs", "binfmt_misc", "nsfs"}
PSEUDO_MOUNTS = ("/proc", "/sys", "/dev", "/run", "/snap", "/var/lib/docker")


def _gb(value) -> Optional[float]:
    try:
        return round(float(value) / (1024 ** 3), 1)
    except (TypeError, ValueError):
        return None


def _disk_usage(usage) -> list:
    """How full the real filesystems are, root first, bounded.

    ``StorageManager.get_disk_usage`` returns every line ``df`` prints, in
    bytes. The model wants the few mounts a person would look at, in units a
    person would use, with the one the standing goal is about at the top.
    """
    if not isinstance(usage, list):
        return []
    rows = []
    for d in usage:
        if not isinstance(d, dict):
            continue
        mount = str(d.get("mountpoint") or "")
        source = str(d.get("device") or "")
        if source in PSEUDO_FS or not mount:
            continue
        if mount != "/" and mount.startswith(PSEUDO_MOUNTS):
            continue
        row = {"mountpoint": mount, "use_percent": d.get("use_percent")}
        for key, out_key in (("size", "size_gb"), ("available", "available_gb")):
            gb = _gb(d.get(key))
            if gb is not None:
                row[out_key] = gb
        rows.append(row)
    # Root first: it is what the standing goal names, and a model that reads
    # the first row and stops should read the right one.
    rows.sort(key=lambda r: (r["mountpoint"] != "/", r["mountpoint"]))
    return rows[:8]


def _uptime_seconds() -> Optional[float]:
    try:
        with open("/proc/uptime") as f:
            return round(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def uses_adaptive_thinking(model: str) -> bool:
    return any(model.startswith(p) for p in ADAPTIVE_MODEL_PREFIXES)


@dataclass
class Completion:
    """Provider-neutral result of one model call."""
    text: str = ""
    stop_reason: str = "end_turn"       # end_turn | max_tokens | refusal
    usage: dict = field(default_factory=dict)
    refusal_category: Optional[str] = None


class BaseBrain:
    """LLM planner. Construct once; call ``plan()`` each cycle.

    Subclasses implement ``_make_client``, ``_complete`` and
    ``_handle_error`` for a particular provider; everything else (context,
    budget, backoff, decision parsing) is shared.
    """

    provider = "base"
    default_model = DEFAULT_MODEL

    def __init__(self, config: Optional[dict], logger: logging.Logger, client=None,
                 cycle_interval: Optional[float] = None):
        self.config = dict(config or {})
        self.log = logger.getChild("brain")
        self.model = self.config.get("model") or self.default_model
        self.max_tokens = int(self.config.get("max_tokens") or 4096)
        self.effort = self._normalise_effort(self.config.get("effort") or "medium")
        self.thinking = self._normalise_thinking(self.config.get("thinking", "adaptive"))
        self.use_fallbacks = bool(self.config.get("fallbacks", True))
        self.max_calls_per_hour = int(self.config.get("max_calls_per_hour") or 60)
        self.plan_every_n_cycles = max(1, int(self.config.get("plan_every_n_cycles") or 1))
        self.history_window = int(self.config.get("history_window") or 10)
        self.full_output_entries = int(self.config.get("full_output_entries") or 3)
        self.output_limit = int(self.config.get("output_limit") or 800)
        self.notes_window = int(self.config.get("notes_window") or 5)
        # Paths reported to the model every cycle; None uses the standard set.
        paths = self.config.get("environment_paths")
        self.environment_paths = ([str(x) for x in paths] if isinstance(paths, (list, tuple))
                                  and paths else None)
        self.cycle_interval = cycle_interval

        self.stats = {
            "calls": 0, "ok": 0, "errors": 0, "refusals": 0, "rate_limited": 0,
            "budget_exhausted": 0, "truncated": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }
        self.last_reasoning = ""
        self.last_decision: Optional[dict] = None
        self.last_error = ""
        self.last_call_at = 0.0
        self._calls = deque()
        self._backoff_until = 0.0
        self._consecutive_errors = 0
        self._disabled_reason = ""
        self.key_source = ""
        self._system_cache: dict = {}
        # Sleeping is opt-in and attached from outside, so a brain constructed
        # bare (tests, the lab, a one-shot consult) behaves exactly as before.
        from jarvis.agent.vigil import NullVigil
        self._vigil = NullVigil()

        self.client = client if client is not None else self._make_client()

    # ---- setup ----------------------------------------------------------

    def _normalise_effort(self, effort: str) -> str:
        effort = str(effort).lower()
        if effort not in EFFORT_LEVELS:
            self.log.warning("Unknown llm.effort %r; using medium", effort)
            return "medium"
        if effort in ("xhigh", "max") and self.max_tokens < HIGH_EFFORT_MIN_TOKENS:
            self.log.warning("llm.effort=%s needs max_tokens >= %d (have %d); using high",
                             effort, HIGH_EFFORT_MIN_TOKENS, self.max_tokens)
            return "high"
        return effort

    def _normalise_thinking(self, value) -> str:
        """Return 'adaptive', 'disabled' or 'omit'."""
        if self.provider != "anthropic" or not uses_adaptive_thinking(self.model):
            return "omit"  # model takes neither adaptive thinking nor effort
        if value in (False, 0) or str(value).lower() in ("disabled", "off", "false", "none"):
            if self.effort in ("xhigh", "max"):
                self.log.warning("thinking disabled is not allowed at effort %s; using high",
                                 self.effort)
                self.effort = "high"
            return "disabled"
        if str(value).lower() != "adaptive":
            self.log.warning("Unknown llm.thinking %r; using adaptive", value)
        return "adaptive"

    def available(self) -> bool:
        """True when a call could be made right now."""
        if self.client is None or self._disabled_reason:
            return False
        if time.time() < self._backoff_until:
            return False
        return self._calls_in_window() < self.max_calls_per_hour

    def attach_vigil(self, vigil):
        """Hand the brain the sleep/wake gate. Optional; default is always awake."""
        self._vigil = vigil

    def should_plan(self, cycle: int) -> bool:
        # The vigil is asked last because asking it is what marks a suppressed
        # call in its stats, and a call blocked by the budget or a backoff was
        # never the vigil's to suppress.
        if not (self.available() and cycle % self.plan_every_n_cycles == 0):
            return False
        return self._vigil.may_call()

    def unavailable_reason(self) -> str:
        if self.client is None or self._disabled_reason:
            return self._disabled_reason or "no client"
        if time.time() < self._backoff_until:
            return self.last_error or f"backing off {round(self._backoff_until - time.time())}s"
        if self._calls_in_window() >= self.max_calls_per_hour:
            return f"call budget exhausted ({self.max_calls_per_hour}/hour)"
        return ""

    def status(self) -> dict:
        return {
            "model": self.model,
            "effort": self.effort,
            "thinking": self.thinking,
            "available": self.available(),
            "disabled_reason": self._disabled_reason or None,
            "unavailable_reason": self.unavailable_reason() or None,
            "key_source": self.key_source or None,
            "backoff_seconds": max(0, round(self._backoff_until - time.time())),
            "calls_last_hour": self._calls_in_window(),
            "max_calls_per_hour": self.max_calls_per_hour,
            "vigil": self._vigil.status(),
            "last_reasoning": self.last_reasoning,
            "last_error": self.last_error or None,
            "stats": dict(self.stats),
        }

    # ---- budget / backoff ----------------------------------------------

    def _calls_in_window(self) -> int:
        cutoff = time.time() - 3600
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()
        return len(self._calls)

    def _take_budget(self) -> bool:
        if self._calls_in_window() >= self.max_calls_per_hour:
            self.stats["budget_exhausted"] += 1
            self.last_error = f"call budget exhausted ({self.max_calls_per_hour}/hour)"
            if self.stats["budget_exhausted"] in (1, 10, 100):
                self.log.warning("LLM call budget exhausted (%d/hour); using rule planner",
                                 self.max_calls_per_hour)
            return False
        self._calls.append(time.time())
        # One chokepoint for every kind of call -- planning, an operator's
        # think, a consult, a lab run -- because the meter does not care which
        # of them warmed the copy.
        self._vigil.note_call()
        return True

    def _backoff(self, seconds: float, reason: str):
        self._backoff_until = time.time() + seconds
        self.last_error = reason
        self.log.warning("LLM brain backing off %.0fs: %s", seconds, reason)

    def _disable(self, reason: str):
        self._disabled_reason = reason
        self.last_error = reason
        self.log.error("LLM brain disabled: %s", reason)

    # ---- context ----------------------------------------------------------

    def _system_text(self, agent) -> str:
        """Static system block: prompt + rendered policy/windows. Cached per agent."""
        key = id(agent)
        cached = self._system_cache.get(key)
        if cached is not None:
            return cached
        shell = getattr(agent.executor, "shell_policy", None) or {}
        if shell.get("enabled"):
            shell_text = (
                f"shell_command runs `/bin/sh -c <command>` as the agent's user (root on the "
                f"AMI) in {shell.get('cwd') or '/'} with no stdin or tty and a "
                f"{shell.get('timeout', 60)}s timeout; credentials are stripped from its "
                f"environment. You get returncode, stdout and stderr; a non-zero exit is "
                f"reported as success=false with the exit status. Each of stdout/stderr is "
                f"cut to {shell.get('max_output', 4000)} characters when stored and to "
                f"{self.output_limit} in what you see, so use head, tail, grep, wc or sort to "
                f"keep output small. Commands are refused before running if they wipe, format "
                f"or write to disks, delete system directories, reboot or power off, stop or "
                f"disable the jarvis, SSM, ssh or network services, pipe downloads into an "
                f"interpreter, touch credentials or password files, or flush the firewall; a "
                f"refused command fails with the reason."
            )
        else:
            shell_text = ("Shell execution is disabled by policy on this machine: a "
                          "shell_command task will fail. Use the other task types.")
        deployment = (
            f"\n\nThis deployment: agent {agent.name!r}, profile {agent.profile}. "
            f"{shell_text} You see the last {self.history_window} executed tasks (full "
            f"output for the most recent {self.full_output_entries}, summaries for older "
            f"ones) and your last {self.notes_window} notes."
        )
        if self.cycle_interval:
            deployment += (f" Idle cycles are {self.cycle_interval:g}s apart; after a task "
                           f"the next cycle follows within a second.")
        text = SYSTEM_PROMPT + deployment
        self._system_cache = {key: text}
        return text

    def _clock(self, agent) -> dict:
        now = time.time()
        history = agent.task_history
        last_task_at = history[-1].get("timestamp") if history else None
        clock = {
            "now_utc": _dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "uptime_s": _uptime_seconds(),
            "seconds_since_last_task": (round(now - last_task_at) if last_task_at else None),
            "llm_calls_left_this_hour": max(0, self.max_calls_per_hour - self._calls_in_window()),
        }
        if self.cycle_interval:
            clock["cycle_interval_s"] = self.cycle_interval
        # A timestamp is not knowing what day it is. Whether it is a weekday,
        # whether the operator is asleep, and how far his clock is from this
        # machine's all change what is worth doing, and none of them are in a
        # UNIX time. See timesense.py.
        try:
            from jarvis.agent import timesense
            quiet = getattr(getattr(agent, "notifier", None), "quiet_hours", None)
            tz = getattr(getattr(agent, "notifier", None), "timezone", None)
            clock.update(timesense.present(
                now, tz=tz or timesense.DEFAULT_TZ, quiet_hours=quiet))
            if last_task_at:
                clock["last_task"] = timesense.gap(last_task_at, now)
        except Exception:
            pass
        return clock

    def _environment(self) -> dict:
        """Real, checked paths on this machine, missing ones included.

        Without this the model has nothing to ground a path on and fills the
        gap from training: an absence it cannot see is an absence it invents
        something to fill.
        """
        try:
            from jarvis.agent import environment
            return environment.snapshot(self.environment_paths)
        except Exception as exc:                  # never lose a cycle over it
            return {"error": f"environment unavailable: {exc}"}

    def provenance(self) -> dict:
        """What this model actually is, told to it as fact.

        Not the weights: reading your own parameters is not introspection, and
        a 32B model handed its own tensors learns nothing it could act on. This
        is the other thing the operator was reaching for and the useful half of
        it -- knowing which model you are, on whose hardware, with how much room
        to think. A model that does not know its own provenance cannot reason
        about its own limits, and will cheerfully claim capabilities belonging
        to whatever it read most about in training.
        """
        model = str(getattr(self, "model", "") or "")
        identifier = model.rsplit("/", 1)[-1] if "/" in model else model
        family = self.config.get("family") or self.config.get("model_family")
        # An imported model's identifier is a random handle: telling the model
        # it is "jfu3j2ssmqvx" is worse than telling it nothing, because it
        # reads as a name. The family is the part that means something.
        out = {"model": str(family) if family else (identifier or "unknown"),
               "provider": getattr(self, "provider", None)}
        if family and identifier and identifier != str(family):
            out["deployment_id"] = identifier
        if model.startswith("arn:aws:bedrock:") and ":imported-model/" in model:
            out["weights"] = ("open weights imported into Amazon Bedrock; they run "
                              "on Amazon's hardware and you cannot read or change them")
        window = self.config.get("context_window")
        if window:
            out["context_window_tokens"] = int(window)
        out["runs_as"] = ("the planner inside Jarvis, a root service on an EC2 "
                          "instance; this process is not the model, it calls it")
        return out

    def build_context(self, agent, observations: Optional[dict],
                      mode: str = "plan") -> dict:
        """Everything the model needs to decide, bounded in size.

        ``mode`` is "plan" when the model is choosing this cycle's task, and
        "answer" when it is talking to the operator. The difference matters
        because the same context is used for both and one of them cannot act.
        """
        planner = agent.planner
        goals = [
            {"description": g["description"], "priority": g["priority"],
             "completed": bool(g.get("completed"))}
            for g in planner.goals
        ]
        entries = agent.task_history[-self.history_window:]
        history = []
        n = len(entries)
        now = time.time()
        for i, entry in enumerate(entries):
            task = entry.get("task", {})
            result = entry.get("result", {})
            item = {
                "cycle": entry.get("cycle"),
                "task": task.get("description"),
                "type": task.get("type"),
                "success": bool(result.get("success")),
            }
            if entry.get("timestamp"):
                item["ran_s_ago"] = max(0, round(now - float(entry["timestamp"])))
            if "error" in result:
                item["error"] = str(result["error"])[:self.output_limit]
            if "output" in result and i >= n - self.full_output_entries:
                item["output"] = _bounded_json(_truncate(result["output"], self.output_limit),
                                               self.output_limit * 2)
            history.append(item)
        pending = [
            {"description": t.description, "type": t.task_type.value, "priority": t.priority}
            for t in sorted(planner.pending_tasks)[:10]
        ]
        notes = list(getattr(agent, "notes", []))[-self.notes_window:]
        pinned = []
        store = getattr(agent, "store", None)
        if store is not None:
            try:
                pinned = [m["text"] for m in store.pinned(5)]
            except Exception:
                pinned = []
        context = {
            "clock": self._clock(agent),
            "self": self.provenance(),
            "cycle": agent.cycle_count,
            "observations": compact_observations(observations or {}),
            "environment": self._environment(),
            "goals": goals,
            "pending_tasks": pending,
            "recent_history": history,
            "notes": notes,
        }
        if pinned:
            context["pinned_memory"] = pinned
        # What the agent has learned about itself, derived from the record it
        # is not allowed to read. Aggregates only: it learns that it filed six
        # proposals and one was taken, never which, when, or what was said.
        # Framed as observations, not rules -- "this has run 340 times and
        # found nothing" is a fact it can weigh; "stop running this" is a rule
        # it would follow wrongly on the day the check finally matters.
        # How long its own work actually takes, measured rather than guessed.
        # A model estimating duration reaches for a prior built from text
        # about people doing projects, and that text has no entries at all for
        # "forty milliseconds" -- so it says a week for something the machine
        # finishes in a second. Measurements beat priors; these are its own.
        try:
            from jarvis.agent import timesense
            measured = timesense.durations(agent.task_history, now)
            context["how_long_things_take"] = {
                "measured_on_this_machine": measured,
                "scales": timesense.scale(measured),
            }
        except Exception:
            pass
        knower = getattr(agent, "self_knowledge", None)
        if knower is not None:
            try:
                lines = knower.lines()
            except Exception:
                lines = []
            if lines:
                context["about_yourself"] = lines
        # That it sleeps, how long it slept and what woke it. Without this a
        # model that wakes into a forty-minute gap will either invent what
        # happened in it or read the jump as evidence something is broken.
        try:
            rest = self._vigil.describe()
        except Exception:
            rest = {}
        if rest:
            context["rest"] = rest
        # That it is being experimented on, while it is. A lab window fills
        # the record with answers it did not choose, some of them from the
        # bare model rather than from it, and an agent reading aggregates
        # over that window has every reason to conclude it is malfunctioning.
        # It is told the window is open and what is being varied; it is not
        # told which dials are set, because that would put the answer inside
        # the question. See jarvis/agent/lab.py.
        try:
            window = agent.lab.notice()
        except Exception:
            window = None
        if window:
            context["lab_session"] = window
        # What it asked and has not been answered, and what came back. An
        # answer is the part worth carrying: a question it cannot see the
        # reply to is a question it will ask again. See questions.py.
        try:
            register = agent.questions
            waiting, replies = register.waiting(), register.answers(3)
        except Exception:
            waiting, replies = [], []
        if waiting or replies:
            context["your_questions"] = {}
            if waiting:
                context["your_questions"]["waiting"] = waiting[:5]
            if replies:
                context["your_questions"]["answered"] = replies
        rung = getattr(agent, "rung", None)
        if rung:
            from jarvis.agent import authority
            restrict = getattr(agent, "tool_restrictions", None)
            context["mandate"] = {
                "rung": rung,
                "means": authority.describe(rung),
                # What it may actually choose from, at this rung, right now.
                # Anything withheld is simply absent: the model is not shown a
                # list of things it cannot have, because that is an invitation
                # to ask and the answer would only ever be no.
                "tools": _tools.describe(rung, restrict),
                # Whether anything is gated, never what. Asked what it needed,
                # the agent said it had to be able to tell a capability gated
                # by design from one broken by accident, and it was right.
                "capability": _tools.gating_note(rung, restrict),
                # Which of the two things it is doing. Omitting this caused a
                # confabulation within minutes of the tool list being added:
                # asked in conversation to use read_logs, the agent replied "I
                # used read_logs ... and found no entries", having run nothing,
                # invented a reason (a different file, which happened to exist
                # and be empty), and concluded the system was stable. The
                # journal had 53 lines. It was not lying so much as reading a
                # list of its tools in a context that never said it could not
                # reach them. Saying which mode it is in is the fix.
                "right_now": _RIGHT_NOW.get(mode, _RIGHT_NOW["plan"]),
            }
        proposals = list(getattr(agent, "proposals", []))[-5:]
        if proposals:
            context["proposals_awaiting_operator"] = [
                {k: p.get(k) for k in ("cycle", "description", "command") if p.get(k)}
                for p in proposals
            ]
        uploads = list(getattr(agent, "uploads", []))[-10:]
        if uploads:
            context["uploaded_files"] = [
                {k: u.get(k) for k in ("name", "path", "size", "uploaded_at") if k in u}
                for u in uploads
            ]
        last = getattr(agent, "last_thought", None) or {}
        if last:
            context["last_decision"] = {
                "cycle": last.get("cycle"),
                "reasoning": str(last.get("reasoning", ""))[:400],
                "task": (last.get("task") or {}).get("description") if last.get("task") else "idle",
            }
            if last.get("skipped"):
                context["last_decision"]["skipped"] = str(last["skipped"])[:400]
            if last.get("refused"):
                context["last_decision"]["refused"] = str(last["refused"])[:500]
            if last.get("unverified_paths"):
                context["last_decision"]["unverified_paths"] = {
                    "paths": list(last["unverified_paths"])[:10],
                    "meaning": "you named these paths but they do not exist on this "
                               "machine; check with inspect_path before naming a path again",
                }
        cloud = agent.memory.recall("cloud_instance", 1) or agent.memory.recall("cloud_probe", 1)
        if cloud:
            context["cloud"] = _truncate(cloud[-1].get("data"), 200)
        return context

    # ---- requests -----------------------------------------------------------

    # ---- provider hooks -------------------------------------------------------

    def _make_client(self):  # pragma: no cover - abstract
        raise NotImplementedError

    def _complete(self, system: str, messages: list, structured: bool,
                  cache: bool = True) -> Completion:  # pragma: no cover - abstract
        """One model call over ``messages`` ([{role, content: str}, ...]).

        Raise on transport/API errors; return a Completion.
        """
        raise NotImplementedError

    def _handle_error(self, e: Exception):  # pragma: no cover - abstract
        self._backoff(60, f"unexpected error: {e.__class__.__name__}: {e}")

    # ---- shared call path -------------------------------------------------------

    def _call(self, system: str, messages, structured: bool,
              cache: bool = True) -> Optional[Completion]:
        """Make one model call; returns the Completion or None (after logging).

        ``messages`` is a list of ``{"role", "content"}`` turns, or a plain
        string for a single user turn.
        """
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        if self.client is None or self._disabled_reason or time.time() < self._backoff_until:
            return None
        if not self._take_budget():
            return None
        self.stats["calls"] += 1
        self.last_call_at = time.time()
        try:
            completion = self._complete(system, messages, structured, cache)
        except Exception as e:  # classified by the provider
            self.stats["errors"] += 1
            self._consecutive_errors += 1
            self._handle_error(e)
            return None

        for key, value in (completion.usage or {}).items():
            if key in self.stats and isinstance(value, int):
                self.stats[key] += value

        if completion.stop_reason == "refusal":
            self.stats["refusals"] += 1
            self.last_error = f"refusal ({completion.refusal_category or 'uncategorised'})"
            self.log.warning("LLM declined to plan this cycle: %s", self.last_error)
            return None
        if completion.stop_reason == "max_tokens":
            self.stats["truncated"] += 1
            self.last_error = "response truncated at max_tokens"
            self.log.warning("LLM response truncated; raise llm.max_tokens (%d)", self.max_tokens)
            return None

        self._consecutive_errors = 0
        self.stats["ok"] += 1
        self.last_error = ""
        return completion

    # ---- public API ----------------------------------------------------------

    def plan(self, agent, observations: Optional[dict] = None) -> Optional[Decision]:
        """Ask the model for the next task. ``None`` means "use the fallback"."""
        try:
            system = self._system_text(agent)
            context = self.build_context(agent, observations)
            user_text = ("Current situation as JSON. Decide the next task.\n\n"
                         + json.dumps(context, default=str, separators=(",", ":")))
            completion = self._call(system, [{"role": "user", "content": user_text}],
                                    structured=True)
            if completion is None:
                return None
            raw = self._parse_plan(completion.text)
            decision = self._to_decision(raw)
        except Exception as e:  # never let a planning failure kill the loop
            self.last_error = f"invalid plan: {e.__class__.__name__}: {e}"
            self.log.error("LLM %s", self.last_error)
            return None
        self.last_reasoning = decision.reasoning
        self.last_decision = raw
        return decision

    @staticmethod
    def _parse_plan(text: str) -> Any:
        """Parse the plan JSON; tolerate prose or code fences around it."""
        text = (text or "").strip()
        try:
            return json.loads(text)
        except ValueError:
            pass
        start = text.find("{")
        if start < 0:
            raise ValueError("no JSON object in model output")
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:i + 1])
        raise ValueError("unterminated JSON object in model output")

    def _to_decision(self, raw: Any) -> Decision:
        if not isinstance(raw, dict):
            return Decision(reasoning="model returned a non-object plan", raw={})
        reasoning = str(raw.get("reasoning") or "")[:2000]
        note = str(raw.get("note") or "")[:1000]
        proposal = str(raw.get("proposal") or "")[:500]
        completed_raw = raw.get("completed_goals")
        completed = []
        if isinstance(completed_raw, list):
            completed = [str(g).strip() for g in completed_raw
                         if isinstance(g, (str, int, float)) and str(g).strip()]
        kind = str(raw.get("task_type") or "none")
        task = None
        if kind != "none" and kind in TaskType._value2member_map_:
            try:
                priority = int(raw.get("priority", 5))
            except (TypeError, ValueError, OverflowError):
                priority = 5
            priority = max(0, min(priority, 10))
            description = str(raw.get("description") or "").strip() or f"LLM task: {kind}"
            metadata = {"source": "llm"}
            goal = str(raw.get("goal") or "").strip()
            if goal:
                metadata["goal"] = goal[:500]
            if kind == "shell_command":
                command = str(raw.get("command") or "").strip()
                if not command:
                    return Decision(reasoning=reasoning + " (shell_command without a command; idling)",
                                    completed_goals=completed, note=note, proposal=proposal, raw=raw)
                metadata["command"] = command
            # Every tool that declares a path argument takes it through the
            # plan's single command field. Derived from the register, because
            # naming inspect_path here by hand was the fifth place read_file
            # had to be listed and the second that was missed: the model put
            # the path in command, exactly as the schema now tells it to, and
            # the path was dropped on the way to the executor. Three cycles
            # running it reasoned correctly about why the read had failed and
            # tried again, which is a well-behaved agent meeting a broken one.
            if kind in _tools.path_takers():
                target = str(raw.get("command") or "").strip()
                if not target:
                    return Decision(reasoning=reasoning + f" ({kind} without a path; idling)",
                                    completed_goals=completed, note=note, proposal=proposal, raw=raw)
                metadata["path"] = target
            task = Task(priority=priority, description=description[:200],
                        task_type=TaskType(kind), metadata=metadata, max_retries=1)
        return Decision(reasoning=reasoning, task=task, completed_goals=completed,
                        note=note, proposal=proposal, raw=raw)

    def ask(self, agent, question: str, observations: Optional[dict] = None) -> Optional[str]:
        """Free-form question about the system, answered with agent context."""
        question = (question or "").strip()
        if not question:
            return None
        return self.chat(agent, [{"role": "user", "content": question}], observations)

    @staticmethod
    def _normalise_turns(turns, limit: int = 12) -> list:
        """Keep the last ``limit`` turns, alternating and starting with user."""
        clean = []
        for t in turns or []:
            if not isinstance(t, dict):
                continue
            role = "assistant" if str(t.get("role")) == "assistant" else "user"
            content = str(t.get("content") or "").strip()
            if not content:
                continue
            if clean and clean[-1]["role"] == role:
                clean[-1]["content"] += "\n\n" + content  # merge same-role runs
            else:
                clean.append({"role": role, "content": content[:8000]})
        clean = clean[-limit:]
        while clean and clean[0]["role"] != "user":
            clean.pop(0)
        return clean

    @staticmethod
    def _look_up_paths(text: str, limit: int = 5) -> list:
        """Resolve paths the operator names, so an answer about a directory
        comes from a real listing rather than a recollection of one."""
        try:
            from jarvis.agent import environment
            out = []
            for path in environment.extract_paths(
                    text, limit=limit,
                    min_segments=environment.MIN_SEGMENTS_ASKED):
                info = environment.stat_path(path)
                if info.get("kind") == "dir":
                    info = environment.tree(path, depth=1, limit=60)
                out.append(info)
            return out
        except Exception:
            return []

    def chat(self, agent, turns, observations: Optional[dict] = None,
             settings: Optional[dict] = None) -> Optional[str]:
        """Multi-turn conversation with the operator, grounded in agent context.

        ``turns`` is the transcript so far ([{role, content}, ...], last one
        from the user). The context is attached to the first user turn.
        With ``settings`` (behaviour-lab dials) the system text, context and
        model parameters come from the dials instead of the defaults.
        """
        messages = self._normalise_turns(turns)
        if not messages or messages[-1]["role"] != "user":
            return None
        try:
            if settings is None:
                context = self.build_context(agent, observations, mode="answer")
                system = ASK_PROMPT
                params = None
            else:
                from jarvis.brain import dials
                composed = dials.compose(settings, self, agent, observations,
                                         mode="answer")
                context, system, params = composed["context"], composed["system"], composed["model"]
            if context is not None:
                looked_up = self._look_up_paths(messages[-1]["content"])
                if looked_up:
                    context = dict(context)
                    context["asked_about"] = looked_up
                preface = "Context as JSON:\n" + json.dumps(context, default=str, indent=1) + "\n\n"
                messages[0] = {"role": "user", "content": preface + messages[0]["content"]}
            with self._lab_params(params):
                completion = self._call(system, messages, structured=False, cache=False)
        except Exception as e:
            self.last_error = f"chat failed: {e.__class__.__name__}: {e}"
            self.log.error("LLM %s", self.last_error)
            return None
        if completion is None:
            return None
        return completion.text.strip() or None

    # ---- behaviour lab ----------------------------------------------------------

    class _ParamOverride:
        def __init__(self, brain, params):
            self.brain, self.params, self.saved = brain, params, {}

        def __enter__(self):
            if not self.params:
                return self
            b = self.brain
            for attr, value in self.brain._lab_attribute_map(self.params).items():
                if hasattr(b, attr):
                    self.saved[attr] = getattr(b, attr)
                    setattr(b, attr, value)
            return self

        def __exit__(self, *exc):
            for attr, value in self.saved.items():
                setattr(self.brain, attr, value)
            return False

    def _lab_params(self, params: Optional[dict]):
        return BaseBrain._ParamOverride(self, params)

    def _lab_attribute_map(self, params: dict) -> dict:
        """Provider hook: dial values -> brain attributes to override for one call."""
        out = {"max_tokens": int(params.get("max_tokens") or self.max_tokens)}
        if hasattr(self, "temperature") and params.get("temperature") is not None:
            out["temperature"] = float(params["temperature"])
        if hasattr(self, "think_mode") and params.get("thinking"):
            out["think_mode"] = "on" if params["thinking"] == "on" else "off"
        return out

    def experiment(self, agent, question: str, settings: Optional[dict] = None,
                   compare: bool = True, mode: str = "answer",
                   observations: Optional[dict] = None) -> dict:
        """Run one question under the dials (and, optionally, under the base model).

        ``mode`` is "answer" (a conversational reply) or "plan" (ask for a
        planning decision; the decision is returned, never executed). Each
        variant is one budgeted model call; nothing here touches the agent's
        state or the planning loop.
        """
        from jarvis.brain import dials
        question = (question or "").strip()
        variants = [("dials", dials.normalise(settings))]
        if compare:
            variants.insert(0, ("base", dials.base()))
        results = []
        for name, s in variants:
            composed = dials.compose(s, self, agent, observations, mode=mode)
            system = composed["system"]
            user = question
            if composed["context"] is not None:
                user = ("Context as JSON:\n" + json.dumps(composed["context"], default=str, indent=1)
                        + "\n\n" + question)
            structured = mode == "plan"
            if structured:
                system = (system + "\n\n" if system else "") + PLAN_MENU
                user = user or "Choose the next task."
            started = time.time()
            with self._lab_params(composed["model"]):
                completion = self._call(system, [{"role": "user", "content": user}],
                                        structured=structured, cache=False)
            item = {"variant": name, "settings": s, "fingerprint": composed["fingerprint"],
                    "system": system, "context_keys": sorted(composed["context"]) if composed["context"] else [],
                    "model_params": composed["model"], "elapsed_s": round(time.time() - started, 2)}
            if completion is None:
                item["error"] = self.last_error or self.unavailable_reason() or "no completion"
            else:
                item["answer"] = completion.text.strip()
                item["usage"] = completion.usage
                if structured:
                    try:
                        item["decision"] = self._parse_plan(completion.text)
                    except ValueError as e:
                        item["decision_error"] = str(e)
            results.append(item)
        return {"question": question, "mode": mode, "model": self.model,
                "provider": self.provider, "variants": results}


class ClaudeBrain(BaseBrain):
    """Claude via the official ``anthropic`` SDK (Claude API)."""

    provider = "anthropic"
    default_model = DEFAULT_MODEL

    def _make_client(self):
        if anthropic is None:
            self._disabled_reason = "anthropic SDK not installed (pip install anthropic)"
            self.log.warning("LLM brain disabled: %s", self._disabled_reason)
            return None
        key, source = resolve_api_key(self.config)
        kwargs = {
            "max_retries": int(self.config.get("max_retries", 2)),
            "timeout": float(self.config.get("timeout", 120)),
        }
        if self.config.get("base_url"):
            kwargs["base_url"] = self.config["base_url"]
        try:
            if key:
                self.key_source = source
                return anthropic.Anthropic(api_key=key, **kwargs)
            # No explicit key: let the SDK resolve ANTHROPIC_AUTH_TOKEN or an
            # `ant auth login` profile (useful on a developer machine). The
            # SDK constructs happily with nothing at all and only fails on
            # the first request, so check what it actually resolved.
            client = anthropic.Anthropic(**kwargs)
            if not any(getattr(client, attr, None)
                       for attr in ("api_key", "auth_token", "credentials")):
                self._disabled_reason = f"no credentials: {source}"
                self.log.warning("LLM brain disabled: %s", self._disabled_reason)
                return None
            self.key_source = "sdk-default"
            return client
        except Exception as e:  # missing credentials or bad config
            self._disabled_reason = f"{source}; SDK could not build a client ({e})"
            self.log.warning("LLM brain disabled: %s", self._disabled_reason)
            return None

    def _lab_attribute_map(self, params: dict) -> dict:
        out = super()._lab_attribute_map(params)
        if params.get("thinking") and self.thinking != "omit":
            out["thinking"] = "adaptive" if params["thinking"] == "on" else "disabled"
        return out

    def _request_kwargs(self, system: str, messages, structured: bool,
                        cache: bool = True) -> dict:
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        }
        if system:
            system_block = {"type": "text", "text": system}
            if cache:
                system_block["cache_control"] = {"type": "ephemeral"}
            kwargs["system"] = [system_block]
        if self.thinking != "omit":
            kwargs["output_config"] = {"effort": self.effort}
            kwargs["thinking"] = {"type": self.thinking}
        if structured:
            kwargs.setdefault("output_config", {})["format"] = {
                "type": "json_schema", "schema": PLAN_SCHEMA}
        if self.use_fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def _send(self, kwargs: dict):
        if self.max_tokens > STREAM_ABOVE_TOKENS:
            with self.client.beta.messages.stream(**kwargs) as stream:
                return stream.get_final_message()
        return self.client.beta.messages.create(**kwargs)

    def _complete(self, system: str, messages: list, structured: bool,
                  cache: bool = True) -> Completion:
        response = self._send(self._request_kwargs(system, messages, structured, cache))
        usage = {}
        u = getattr(response, "usage", None)
        if u is not None:
            for key in ("input_tokens", "output_tokens",
                        "cache_read_input_tokens", "cache_creation_input_tokens"):
                value = getattr(u, key, None)
                if isinstance(value, int):
                    usage[key] = value
        stop = getattr(response, "stop_reason", None) or "end_turn"
        category = None
        if stop == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
        return Completion(text=self._text_of(response), stop_reason=stop, usage=usage,
                          refusal_category=category)

    def _handle_error(self, e: Exception):
        if anthropic is not None:
            if isinstance(e, anthropic.AuthenticationError):
                self._disable(f"authentication failed ({self.key_source or 'no key'})")
                return
            if isinstance(e, anthropic.PermissionDeniedError):
                self._disable("API key lacks permission for this model")
                return
            if isinstance(e, anthropic.NotFoundError):
                self._disable(f"model or endpoint not found: {self.model}")
                return
            if isinstance(e, anthropic.RateLimitError):
                self.stats["rate_limited"] += 1
                retry_after = 60
                try:
                    retry_after = int(e.response.headers.get("retry-after", "60"))
                except Exception:
                    pass
                self._backoff(max(5, retry_after), "API rate limited")
                return
            if isinstance(e, anthropic.BadRequestError):
                self.last_error = f"bad request: {getattr(e, 'message', e)}"
                self.log.error("LLM %s", self.last_error)
                if self._consecutive_errors >= 3:
                    self._disable("repeated bad requests; check llm.model and options")
                else:
                    self._backoff(30, self.last_error)
                return
            if isinstance(e, anthropic.APIStatusError):
                self._backoff(60 if e.status_code >= 500 else 30,
                              f"API error {e.status_code}")
                return
            if isinstance(e, anthropic.APIConnectionError):
                self._backoff(min(300, 30 * self._consecutive_errors), "API unreachable")
                return
        if isinstance(e, TypeError) and "authentication" in str(e).lower():
            self._disable("no usable credentials for the anthropic client")
            return
        self._backoff(60, f"unexpected error: {e.__class__.__name__}: {e}")

    @staticmethod
    def _text_of(response) -> str:
        parts = []
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        return "".join(parts)



def build_brain(config: Optional[dict], logger: logging.Logger,
                cycle_interval: Optional[float] = None, region: Optional[str] = None):
    """Construct the brain named by ``llm.provider`` (anthropic or bedrock)."""
    provider = str((config or {}).get("provider") or "anthropic").lower()
    if provider in ("anthropic", "claude"):
        return ClaudeBrain(config, logger, cycle_interval=cycle_interval)
    if provider in ("bedrock", "aws"):
        from jarvis.brain.bedrock import BedrockBrain
        return BedrockBrain(config, logger, cycle_interval=cycle_interval, region=region)
    raise ValueError(f"unknown llm.provider {provider!r} (use anthropic or bedrock)")
