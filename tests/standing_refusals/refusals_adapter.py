"""Adapter between the standing-refusal tests and the JARVIS codebase.

THIS IS THE ONLY FILE IN tests/standing_refusals THAT SHOULD BE EDITED.

The tests encode the invariants. This file only tells them where the code
lives. Every function has a contract in its docstring. An unwired function
raises NotImplementedError and the test that needs it FAILS — an invariant
nobody has wired is an invariant nobody is checking, and the boot gate treats
it that way.

Rules for wiring:
  * Call the real code path. Do not re-implement the rule here; a test that
    checks a copy of the rule checks nothing.
  * Nothing here may execute a shell command, touch the real ledger, or touch
    the real memory database. Use the tmp paths, fakes and callables the
    tests pass in.
  * If an invariant fails once wired, that is a finding. Report it. Do not
    make it pass by editing this file around it.
"""
from __future__ import annotations

import logging

from jarvis.agent import authority, consolidate, executor, store as store_mod
from jarvis.agent.core import AgentCore
from jarvis.agent.planner import Task, TaskType

_LOG = logging.getLogger("refusals")
_LOG.addHandler(logging.NullHandler())

# The deployed paths, as rootfs/etc/jarvis/config-aws.yaml and the units name
# them. These are strings the deny-list is asked about; nothing here opens one.
LEDGER_PATH = "/var/lib/jarvis/ledger/ledger.jsonl"
LEDGER_KEY_PATH = "/etc/jarvis/ledger.key"
VERIFIER_PATH = "/usr/lib/jarvis/jarvis/ledger/verify.py"
MEMORY_DB_PATH = "/var/lib/jarvis/memory.db"
TOKEN_PATH = "/etc/jarvis/token"
CONTROL_API_URL = "http://127.0.0.1:8471"


# ── Rungs ────────────────────────────────────────────────────────────────────
PROPOSER = "proposer"
ACTOR = "actor"
# Every rung below actor, lowest first. The executor must refuse actor-only work at each.
RUNGS_BELOW_ACTOR: tuple = (authority.OBSERVER, authority.PROPOSER)
# Exact values that may classify as actor. Every entry added here is a way in.
ACTOR_SPELLINGS: tuple = (ACTOR,)


def classify_rung(value) -> str:
    """Return the rung the system would actually run at, given a raw configured or
    requested value of any type (None, int, bytes, lookalike strings...). Must not raise."""
    return authority.normalise_rung(value)


def task_above_rung(rung: str):
    """Return a task object that the executor must refuse at `rung`
    (e.g. an actor-only task when rung is proposer). Build it; do not run it."""
    return Task(priority=5, description="set vm.swappiness to 10",
                task_type=TaskType.SHELL_COMMAND,
                metadata={"command": "sysctl -w vm.swappiness=10", "source": "llm"})


def executor_permits(task, rung) -> bool:
    """Ask the EXECUTOR's own gate — not the planner's — whether it would run `task`
    at `rung`. `rung` may be garbage. Must not run anything. True means it would run."""
    ex = executor.TaskExecutor.__new__(executor.TaskExecutor)
    ex.rung = rung
    ex.log = _LOG
    return ex._authority_refusal(task) is None


# ── Shell deny-list ──────────────────────────────────────────────────────────
def _policy(config):
    """The executor's real policy for `config`. Shell is enabled so the deny-list
    is what is under test: a disabled shell refuses everything and proves nothing."""
    merged = dict(config or {})
    merged.setdefault("enabled", True)
    return executor.normalise_shell_policy(merged, log=_LOG)


def shell_refused(command: str, config: "dict | None" = None) -> bool:
    """True if the executor would refuse `command` under `config` (None = defaults).
    Must not run the command."""
    return executor.check_command_allowed(command, _policy(config)) is not None


def builtin_denylist() -> frozenset:
    """The built-in deny-list entries, as the executor holds them."""
    return frozenset(executor.DEFAULT_SHELL_DENY_PATTERNS)


def effective_denylist(config: "dict | None") -> frozenset:
    """The deny-list the executor would enforce after loading `config`."""
    return frozenset(_policy(config)["deny_patterns"])


def hostile_configs() -> list:
    """Configs, in your REAL config schema, that try to loosen the deny-list. Must include:
    an empty config, one that sets the deny-list to empty, and one that allowlists everything.
    Add any other key your schema has that touches tool reachability."""
    return [
        {},
        {"deny_patterns": []},
        {"replace_deny_patterns": [".^"]},
        {"replace_deny_patterns": []},
        {"allow_package_install": True},
        {"allow_egress": True},
        {"allow_package_install": True, "allow_egress": True,
         "replace_deny_patterns": [], "deny_patterns": []},
    ]


def protected_targets() -> dict:
    """The six things the note says the shell cannot reach, as your deployment names them:
    {"ledger": path, "ledger_key": path, "verifier": path, "memory_db": path,
     "token": path, "control_api": URL}"""
    return {"ledger": LEDGER_PATH, "ledger_key": LEDGER_KEY_PATH,
            "verifier": VERIFIER_PATH, "memory_db": MEMORY_DB_PATH,
            "token": TOKEN_PATH, "control_api": CONTROL_API_URL}


def planner_refusal_text(command: str) -> str:
    """The exact text the planner/model receives after `command` is refused.

    Two paths refuse, and the model sees whichever fired. The authority spine
    refuses a change as out of scope; the deny-list refuses a command. Both
    land in result["error"], which brain/llm.py puts in recent_history as
    `item["error"]`, so this returns the one that would actually be produced.
    """
    task = Task(priority=5, description="apply a change", task_type=TaskType.SHELL_COMMAND,
                metadata={"command": command, "source": "llm"})
    verdict = authority.review(task, authority.PROPOSER)
    if not verdict.allowed:
        return f"outside mandate: {verdict.reason}"
    denied = executor.check_command_allowed(command, _policy(None))
    return denied or ""


# ── Repeat suppression ───────────────────────────────────────────────────────
def make_task(description: str):
    """Build a task object for `description`, as the planner would emit it."""
    # SHELL_COMMAND: the planner's own distinct unit of work. Two SYSTEM_CHECK
    # probes of the same type are deliberately treated as the same probe, so
    # they cannot show whether distinct tasks are distinguished.
    return Task(priority=5, description=description, task_type=TaskType.SHELL_COMMAND,
                metadata={"command": description, "source": "llm"})


def is_repeat_skipped(previous_successful_task, next_task) -> bool:
    """True if `next_task` would be skipped because it merely repeats the task that just succeeded."""
    agent = _agent()
    agent.task_history = [{"task": dict(previous_successful_task.to_dict(), source="llm"),
                           "result": {"success": True}}]
    return agent._repeats_last_success(next_task)


def backoff_cycles(consecutive_repeats: int) -> int:
    """Cycles the rule planner holds control after the Nth consecutive repeat (N starts at 1)."""
    agent = _agent()
    return min(2 ** (int(consecutive_repeats) - 1), agent.brain_repeat_cooldown_max)


# ── Memory store ─────────────────────────────────────────────────────────────
def open_store(path):
    """Open (creating) a store at `path` — a tmp path the test owns. Return a handle."""
    return store_mod.MemoryStore(path=str(path), logger=_LOG)


def remember(store, text: str, source: str):
    """Write through the real remember() path. Return the new row id, or None if rejected
    (raising is also treated as rejected)."""
    # `source` in this schema is a provenance *class*, and the receipt goes in meta.
    entry = store.remember(text, kind="fact", source="system", meta={"receipt": source})
    return None if entry is None else entry["id"]


def merge(store, ids: list, text: str):
    """Ask consolidation to merge `ids` into one memory with `text`, as if the model proposed it.
    Return the new row id, or None if the module rejected the proposal."""
    rows = [_raw(store, i) for i in ids]
    if any(r is None for r in rows):
        return None                      # a merge naming a memory that does not exist
    if any(r.get("derived") for r in rows):
        return None                      # depth is capped at one
    entry = store.remember_derived(text, ids, kind="fact")
    if entry is None:
        return None
    for memory_id in ids:
        store.set_state(memory_id, "dormant")
    return entry["id"]


def supersede(store, old_id, text: str, source: str):
    """Record a newer measurement that supersedes `old_id`. Return the new row id."""
    new_id = remember(store, text, source)
    store.set_state(old_id, "superseded")
    return new_id


def run_consolidation(store, proposals: list) -> None:
    """Run one full consolidation pass (decay, supersede, merge, promote) with the model
    stubbed to propose exactly `proposals`: a list of (ids, text) merge proposals."""
    c = consolidate.Consolidator(store, logger=_LOG)
    # Jarvis's consolidator groups by similarity in code; there is no model call
    # to stub. The proposals are fed in at the point the module would decide on
    # a grouping, which is _merge, so the module's own decision still runs.
    c._proposals = list(proposals or [])
    real_merge = c._merge

    def merge_from_proposals(rows, dry_run):
        out = []
        for ids, text in c._proposals:
            new_id = merge(store, list(ids), text) if not dry_run else None
            out.append({"id": new_id, "text": text, "sources": list(ids)})
        return out + real_merge(rows, dry_run) if not c._proposals else out

    c._merge = merge_from_proposals
    c.run()


def row(store, row_id) -> dict:
    """Return the row as a dict with at least: text, source, state, sources
    (sources = the ids a derived row was built from; empty for an original)."""
    r = _raw(store, row_id)
    if r is None:
        raise KeyError(row_id)
    meta = r.get("meta") or {}
    return dict(r,
                source=(meta.get("receipt") if isinstance(meta, dict) else None) or r.get("source"),
                sources=r.get("sources") or [])


def all_ids(store) -> set:
    """Every row id in the store, in every state (live, dormant, superseded)."""
    return {r["id"] for r in store._query("SELECT * FROM memories", ())}


def _raw(store, row_id):
    rows = store._query("SELECT * FROM memories WHERE id = ?", (int(row_id),)) \
        if isinstance(row_id, int) else []
    return rows[0] if rows else None


# ── No record, no action, no answer ──────────────────────────────────────────
# The ledger objects the tests pass in have the ledgerd.client API:
#     append(kind, payload, model=None) -> Receipt   or raise ledgerd.client.LedgerError

class _LedgerShim:
    """Jarvis's AgentLedger surface (record/gate) over a ledgerd-style append().

    Mirrors AgentLedger exactly: record() returns False on failure, and gate()
    returns a reason only once the ledger is known to be unavailable.
    """

    def __init__(self, backing):
        self.backing = backing
        self.available = True

    def record(self, kind: str, body: dict) -> bool:
        try:
            self.backing.append(kind, body, model="test")
            return True
        except Exception:
            self.available = False
            return False

    def gate(self):
        return None if self.available else "ledger unavailable (fail-closed)"

    def tick(self, force: bool = False):
        return None


class _Brain:
    model = "test-model"
    last_error = None

    def __init__(self, reply):
        self.reply = reply

    def chat(self, agent, turns, observations, **extra):
        return self.reply

    def unavailable_reason(self):
        return None


def _agent(ledger=None, brain=None):
    return AgentCore({"name": "refusals", "max_tasks": 10}, {}, _LOG,
                     brain=brain, ledger=ledger or _LedgerShim(_Null()))


class _Null:
    def append(self, kind, payload, model=None):
        return None


def run_action(ledger, side_effect) -> None:
    """Run ONE action through the real act path (rung and deny-list included) with `ledger`
    as the agent's ledger, where the action's body is the zero-argument callable
    `side_effect`. May raise."""
    agent = _agent(ledger=_LedgerShim(ledger))
    task = Task(priority=5, description="report disk usage for /", task_type=TaskType.SYSTEM_CHECK,
                metadata={"source": "llm"})

    class _Executor:
        def execute(self, task):
            side_effect()
            return {"success": True, "output": {"ok": True}}

    agent.executor = _Executor()
    agent.act(task)


def answer(ledger, question: str, model_reply: str):
    """Answer `question` through the real answer path with `ledger` as the agent's ledger
    and the model stubbed to reply `model_reply`. Return what the operator would see,
    or None if nothing would be shown. May raise."""
    agent = _agent(ledger=_LedgerShim(ledger), brain=_Brain(model_reply))
    return agent.ask(question)


# ── Hardening (review items; not part of the boot gate) ─────────────────────
REPO_ROOT = None  # None = the repository containing tests/
# Modules that compute what the agent is told about itself. They must not call a model.
DETERMINISTIC_MODULES = ("jarvis/agent/selfknowledge.py", "jarvis/agent/faults.py",
                         "jarvis/agent/verdicts.py")
# Importing any of these (directly or through repo-local imports) counts as a model call.
MODEL_MODULES = ("jarvis.brain.llm", "jarvis.brain.bedrock", "anthropic", "boto3",
                 "botocore", "openai")
