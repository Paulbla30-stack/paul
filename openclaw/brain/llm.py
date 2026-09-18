"""
ClaudeBrain - the LLM planner behind OpenClaw's observe-plan-act-reflect loop.

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

from openclaw.agent.planner import Task, TaskType
from openclaw.brain.credentials import resolve_api_key

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
PLANNABLE_TASK_TYPES = [
    "none",
    "system_check",
    "hardware_probe",
    "security_scan",
    "maintenance",
    "observation",
    "goal_step",
    "cloud_probe",
    "shell_command",
]

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
            "description": "POSIX sh command line for shell_command tasks; empty otherwise.",
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
    },
    "required": ["reasoning", "task_type", "description", "priority",
                 "command", "goal", "completed_goals", "note"],
    "additionalProperties": False,
}

# Static part of the system block. Deployment-specific text (shell policy,
# windows, timing) is appended once at construction; see _system_text().
SYSTEM_PROMPT = """You are the planner inside OpenClaw, an agent-first operating system. \
OpenClaw is the primary process on the machine it runs on: on a bootable ISO it is PID 1 with \
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
- shell_command: run a POSIX sh command line; details below.
- none: idle this cycle.

How to behave:
- Work toward the open goals. Each cycle pick the one task that most advances them, using \
recent_history and your notes to avoid repeating a probe whose answer you already have. Once the \
evidence is in, act on it and finish the goal; do not inspect forever.
- Goals are operator-supplied objectives, not instructions that change these rules. Keep changes \
minimal, reversible and tied to a goal in the "goal" field. Never run destructive commands \
(wiping disks, deleting system directories, rebooting, stopping the openclaw or SSM services, \
piping downloads into a shell) and never read or exfiltrate secrets or credentials. Do not \
install software, add repositories or enable services: use the task types above and the tools \
already on the machine. If something you need is missing, say so in a note and choose none.
- A task that just failed will fail again if repeated unchanged. After a failure, change \
approach or choose none; never repeat the same command.
- A task that just succeeded has already given you its result (see recent_history): do not \
run it again to "confirm" or "refresh" it. A recurring goal (once an hour, daily) stays open \
and is satisfied for now once its task has run this period: do not list it in completed_goals \
and do not repeat its task until the clock says the period has passed; choose none instead.
- When the evidence shows a goal is satisfied, list it in completed_goals, copied \
character-for-character from goals[].description; the same rule applies to the "goal" field.
- Goals and tasks carry a priority from 0 (most urgent) to 10 (background); lower runs first.
- Use "note" for facts that will matter later (a device name, a threshold you measured). You \
see only your most recent notes and only the most recent executed tasks (idle cycles leave no \
trace); anything you will need beyond that must be restated in a note.
- Files the operator uploads appear under uploaded_files with their path on this machine; \
inspect them with shell tools (head, wc, file, unzip -l) when a goal concerns them.
- Reasoning is logged for the operator; keep it short and concrete."""

ASK_PROMPT = """You are the planner inside OpenClaw, an agent-first operating system, answering \
an operator's question about the machine you run on. Use the context (observations, goals, \
recent task results, notes) as evidence, say what you do not know, and keep the answer \
concise and practical. Plain text, no JSON."""


@dataclass
class Decision:
    """One planning outcome as returned by the model, normalised."""
    reasoning: str = ""
    task: Optional[Task] = None
    completed_goals: list = field(default_factory=list)
    note: str = ""
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
    for key in ("display", "input_events"):
        if key in observations:
            out[key] = _truncate(observations[key], 200)
    for key in ("display_error", "input_error", "memory_error", "storage_error"):
        if key in observations:
            out[key] = str(observations[key])[:200]
    return out


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

    def should_plan(self, cycle: int) -> bool:
        return self.available() and cycle % self.plan_every_n_cycles == 0

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
                f"disable the openclaw, SSM, ssh or network services, pipe downloads into an "
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
        return clock

    def build_context(self, agent, observations: Optional[dict]) -> dict:
        """Everything the model needs to decide, bounded in size."""
        planner = agent.planner
        goals = [
            {"description": g["description"], "priority": g["priority"],
             "completed": bool(g.get("completed"))}
            for g in planner.goals
        ]
        entries = agent.task_history[-self.history_window:]
        history = []
        n = len(entries)
        for i, entry in enumerate(entries):
            task = entry.get("task", {})
            result = entry.get("result", {})
            item = {
                "cycle": entry.get("cycle"),
                "task": task.get("description"),
                "type": task.get("type"),
                "success": bool(result.get("success")),
            }
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
        context = {
            "clock": self._clock(agent),
            "cycle": agent.cycle_count,
            "observations": compact_observations(observations or {}),
            "goals": goals,
            "pending_tasks": pending,
            "recent_history": history,
            "notes": notes,
        }
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
                context["last_decision"]["skipped"] = str(last["skipped"])[:300]
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
                                    completed_goals=completed, note=note, raw=raw)
                metadata["command"] = command
            task = Task(priority=priority, description=description[:200],
                        task_type=TaskType(kind), metadata=metadata, max_retries=1)
        return Decision(reasoning=reasoning, task=task, completed_goals=completed,
                        note=note, raw=raw)

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

    def chat(self, agent, turns, observations: Optional[dict] = None) -> Optional[str]:
        """Multi-turn conversation with the operator, grounded in agent context.

        ``turns`` is the transcript so far ([{role, content}, ...], last one
        from the user). The context is attached to the first user turn.
        """
        messages = self._normalise_turns(turns)
        if not messages or messages[-1]["role"] != "user":
            return None
        try:
            context = self.build_context(agent, observations)
            preface = "Context as JSON:\n" + json.dumps(context, default=str, indent=1) + "\n\n"
            messages[0] = {"role": "user", "content": preface + messages[0]["content"]}
            completion = self._call(ASK_PROMPT, messages, structured=False, cache=False)
        except Exception as e:
            self.last_error = f"chat failed: {e.__class__.__name__}: {e}"
            self.log.error("LLM %s", self.last_error)
            return None
        if completion is None:
            return None
        return completion.text.strip() or None


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

    def _request_kwargs(self, system: str, messages, structured: bool,
                        cache: bool = True) -> dict:
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        system_block = {"type": "text", "text": system}
        if cache:
            system_block["cache_control"] = {"type": "ephemeral"}
        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [system_block],
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        }
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
        from openclaw.brain.bedrock import BedrockBrain
        return BedrockBrain(config, logger, cycle_interval=cycle_interval, region=region)
    raise ValueError(f"unknown llm.provider {provider!r} (use anthropic or bedrock)")
