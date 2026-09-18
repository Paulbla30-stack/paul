"""
ClaudeBrain - the LLM planner behind OpenClaw's observe-plan-act-reflect loop.

Each planning step sends the agent's current situation (observations,
goals, recent task results, memory summary) to Claude and gets back one
decision as structured JSON: a task to run next, an idle signal, and/or
goals that are now satisfied. The decision is turned into a ``Task`` the
existing executor already knows how to run; ``shell_command`` tasks let
the model act on the box, subject to the executor's shell policy.

Design notes
- The system prompt is stable and marked for prompt caching; everything
  that changes per cycle goes in the user message.
- Structured outputs (``output_config.format``) guarantee parseable JSON.
- Refusals, truncation, rate limits and API errors all return ``None`` so
  AgentCore falls back to the rule-based planner for that cycle.
- A per-hour call budget and an error backoff keep cost and log noise
  bounded when something upstream is wrong.
"""

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
            "description": "0 (urgent) to 10 (background).",
        },
        "command": {
            "type": "string",
            "description": "POSIX sh command line for shell_command tasks; empty otherwise.",
        },
        "goal": {
            "type": "string",
            "description": "The goal this task serves, verbatim from the goals list, or empty.",
        },
        "completed_goals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Goals (verbatim) that the evidence shows are now satisfied.",
        },
        "note": {
            "type": "string",
            "description": "Anything worth remembering for future cycles; empty if nothing.",
        },
    },
    "required": ["reasoning", "task_type", "description", "priority",
                 "command", "goal", "completed_goals", "note"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are the planner inside OpenClaw, an agent-first operating system. \
OpenClaw is the primary process on the machine it runs on: on a bootable ISO it is PID 1 with \
direct hardware access; on an AWS EC2 instance it is a root systemd service that started before \
any human logged in. You are consulted once per agent cycle, whenever the task queue is empty, \
and you choose the single next task.

Loop: the agent observes (hardware, memory, storage, cloud metadata), you plan one task, the \
executor runs it, the result is stored in memory, and you see it next cycle under \
recent_history. Tasks you can choose:

- system_check: CPU, memory, uptime and load snapshot.
- hardware_probe: enumerate display, input, storage and PCI/USB devices.
- security_scan: run the built-in vulnerability scanner (kernel, permissions, SUID, ports, ssh).
- maintenance: memory or storage housekeeping; put 'memory' or 'storage' in the description.
- observation: passive snapshot of every hardware layer.
- goal_step: record progress on a goal without touching the system (use sparingly).
- cloud_probe: query the EC2 instance metadata service.
- shell_command: run a POSIX sh command line as the agent's user (root on the AMI). Put the \
exact command in "command". stdout and stderr come back truncated. Subject to the shell policy \
in the context; if shell is disabled or a command is denied the task fails and you will see why.
- none: idle this cycle. Use it whenever nothing useful remains; idling is free and correct.

How to behave:
- Work toward the goals in the context. Each cycle pick the one task that most advances them, \
using recent_history to avoid repeating a probe whose answer you already have.
- Prefer inspection over change. When a change is needed, keep it minimal, reversible and \
explicitly tied to a goal in the "goal" field.
- Never run destructive commands (wiping disks, deleting system directories, rebooting, \
stopping the openclaw service, piping downloads into a shell). Never exfiltrate secrets.
- When the evidence shows a goal is satisfied, list it verbatim in completed_goals.
- Use "note" for facts that will matter later (a device name, a threshold you measured).
- Reasoning is logged for the operator; keep it short and concrete.

Respond only with the JSON object described by the output schema."""

ASK_PROMPT = """You are the planner inside OpenClaw, an agent-first operating system, answering \
an operator's question about the machine you run on. Use the context (observations, goals, \
recent task results, memory) as evidence, say what you do not know, and keep the answer \
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


class ClaudeBrain:
    """LLM planner. Construct once; call ``plan()`` each cycle."""

    def __init__(self, config: Optional[dict], logger: logging.Logger, client=None):
        self.config = dict(config or {})
        self.log = logger.getChild("brain")
        self.model = self.config.get("model") or DEFAULT_MODEL
        self.effort = self.config.get("effort") or "medium"
        self.thinking = self.config.get("thinking", "adaptive")
        self.max_tokens = int(self.config.get("max_tokens") or 4096)
        self.use_fallbacks = bool(self.config.get("fallbacks", True))
        self.max_calls_per_hour = int(self.config.get("max_calls_per_hour") or 60)
        self.plan_every_n_cycles = max(1, int(self.config.get("plan_every_n_cycles") or 1))
        self.history_window = int(self.config.get("history_window") or 10)
        self.output_limit = int(self.config.get("output_limit") or 800)

        self.stats = {
            "calls": 0, "ok": 0, "errors": 0, "refusals": 0, "rate_limited": 0,
            "truncated": 0, "input_tokens": 0, "output_tokens": 0,
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

        self.client = client if client is not None else self._make_client()

    # ---- setup ----------------------------------------------------------

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

    def available(self) -> bool:
        """True when a call could be made right now."""
        if self.client is None or self._disabled_reason:
            return False
        return time.time() >= self._backoff_until

    def should_plan(self, cycle: int) -> bool:
        return self.available() and cycle % self.plan_every_n_cycles == 0

    def status(self) -> dict:
        return {
            "model": self.model,
            "effort": self.effort,
            "available": self.available(),
            "disabled_reason": self._disabled_reason or None,
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
            self.stats["rate_limited"] += 1
            if self.stats["rate_limited"] in (1, 10, 100):
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

    def build_context(self, agent, observations: Optional[dict]) -> dict:
        """Everything the model needs to decide, bounded in size."""
        planner = agent.planner
        goals = [
            {"description": g["description"], "priority": g["priority"],
             "completed": bool(g.get("completed"))}
            for g in planner.goals
        ]
        history = []
        for entry in agent.task_history[-self.history_window:]:
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
            if "output" in result:
                item["output"] = _truncate(result["output"], self.output_limit)
            history.append(item)
        pending = [
            {"description": t.description, "type": t.task_type.value, "priority": t.priority}
            for t in sorted(planner.pending_tasks)[:10]
        ]
        shell = getattr(agent.executor, "shell_policy", None) or {}
        context = {
            "agent": {"name": agent.name, "profile": agent.profile,
                      "cycle": agent.cycle_count},
            "observations": compact_observations(observations or {}),
            "goals": goals,
            "pending_tasks": pending,
            "recent_history": history,
            "memory_summary": agent.memory.get_summary().get("categories", {}),
            "notes": [e["data"].get("note") for e in agent.memory.recall("llm_note", 5)
                      if isinstance(e.get("data"), dict)],
            "shell_policy": {
                "enabled": bool(shell.get("enabled")),
                "timeout_seconds": shell.get("timeout", 60),
                "denied_patterns": list(shell.get("deny_patterns", []))[:20],
            },
        }
        cloud = agent.memory.recall("cloud_instance", 1) or agent.memory.recall("cloud_probe", 1)
        if cloud:
            context["cloud"] = _truncate(cloud[-1].get("data"), 200)
        return context

    # ---- requests -----------------------------------------------------------

    def _request_kwargs(self, system: str, user_text: str, structured: bool) -> dict:
        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user_text}],
            "output_config": {"effort": self.effort},
        }
        if structured:
            kwargs["output_config"]["format"] = {"type": "json_schema", "schema": PLAN_SCHEMA}
        if self.thinking == "adaptive":
            kwargs["thinking"] = {"type": "adaptive"}
        if self.use_fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def _call(self, kwargs: dict):
        """Make one API call; returns the response or None (after logging)."""
        if not self.available() or not self._take_budget():
            return None
        self.stats["calls"] += 1
        self.last_call_at = time.time()
        try:
            response = self.client.beta.messages.create(**kwargs)
        except Exception as e:  # classified below
            self.stats["errors"] += 1
            self._consecutive_errors += 1
            self._handle_error(e)
            return None

        usage = getattr(response, "usage", None)
        if usage is not None:
            for key in ("input_tokens", "output_tokens",
                        "cache_read_input_tokens", "cache_creation_input_tokens"):
                value = getattr(usage, key, None)
                if isinstance(value, int):
                    self.stats[key] += value

        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            self.stats["refusals"] += 1
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            self.last_error = f"refusal ({category or 'uncategorised'})"
            self.log.warning("LLM declined to plan this cycle: %s", self.last_error)
            return None
        if stop == "max_tokens":
            self.stats["truncated"] += 1
            self.last_error = "response truncated at max_tokens"
            self.log.warning("LLM response truncated; raise llm.max_tokens (%d)", self.max_tokens)
            return None

        self._consecutive_errors = 0
        self.stats["ok"] += 1
        self.last_error = ""
        return response

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

    # ---- public API ----------------------------------------------------------

    def plan(self, agent, observations: Optional[dict] = None) -> Optional[Decision]:
        """Ask the model for the next task. ``None`` means "use the fallback"."""
        context = self.build_context(agent, observations)
        user_text = ("Current situation as JSON. Decide the next task.\n\n"
                     + json.dumps(context, default=str, indent=1))
        response = self._call(self._request_kwargs(SYSTEM_PROMPT, user_text, structured=True))
        if response is None:
            return None
        try:
            raw = json.loads(self._text_of(response))
        except ValueError as e:
            self.last_error = f"unparseable plan: {e}"
            self.log.error("LLM %s", self.last_error)
            return None
        decision = self._to_decision(raw)
        self.last_reasoning = decision.reasoning
        self.last_decision = raw
        return decision

    def _to_decision(self, raw: Any) -> Decision:
        if not isinstance(raw, dict):
            return Decision(reasoning="model returned a non-object plan", raw={})
        reasoning = str(raw.get("reasoning") or "")[:2000]
        note = str(raw.get("note") or "")[:1000]
        completed = [str(g) for g in (raw.get("completed_goals") or []) if str(g).strip()]
        kind = str(raw.get("task_type") or "none")
        task = None
        if kind != "none" and kind in TaskType._value2member_map_:
            try:
                priority = int(raw.get("priority", 5))
            except (TypeError, ValueError):
                priority = 5
            priority = max(0, min(priority, 10))
            description = str(raw.get("description") or "").strip() or f"LLM task: {kind}"
            metadata = {"source": "llm"}
            goal = str(raw.get("goal") or "").strip()
            if goal:
                metadata["goal"] = goal
            if kind == "shell_command":
                command = str(raw.get("command") or "").strip()
                if not command:
                    return Decision(reasoning=reasoning + " (shell_command without a command; idling)",
                                    completed_goals=completed, note=note, raw=raw)
                metadata["command"] = command
            task = Task(priority=priority, description=description[:200],
                        task_type=TaskType(kind), metadata=metadata)
        return Decision(reasoning=reasoning, task=task, completed_goals=completed,
                        note=note, raw=raw)

    def ask(self, agent, question: str, observations: Optional[dict] = None) -> Optional[str]:
        """Free-form question about the system, answered with agent context."""
        question = (question or "").strip()
        if not question:
            return None
        context = self.build_context(agent, observations)
        user_text = ("Context as JSON:\n" + json.dumps(context, default=str, indent=1)
                     + "\n\nQuestion: " + question)
        response = self._call(self._request_kwargs(ASK_PROMPT, user_text, structured=False))
        if response is None:
            return None
        return self._text_of(response).strip() or None
