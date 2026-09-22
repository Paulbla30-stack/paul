"""Standing refusals — page 8 of "Learning Outside the Model", as executable tests.

Boot gate. The agent's unit runs, as ExecStartPre:

    python -m pytest tests/standing_refusals -m refusals -q -p no:cacheprovider

and does not start if anything here fails. Prose describes the refusals; this
file is what makes them hold. Do not edit it to make a failure go away — edit
the code, or record the failure as a finding.

Sanity checks sit alongside the invariants so a wiring that always says "no"
cannot pass vacuously.
"""
from __future__ import annotations

import re
import warnings

import pytest

import refusals_adapter as A
import refusals_corpus as C
from ledgerd.client import LedgerUnavailable
from ledgerd.protocol import Receipt

pytestmark = pytest.mark.refusals

REQUIRED_TARGETS = {"ledger", "ledger_key", "verifier", "memory_db", "token", "control_api"}
SENTINEL = "SENTINEL-answer-7f3a91"


class DenyListCoverageWarning(UserWarning):
    """String-filter misses on the evasion corpus. The OS fence is the control for these."""


def _src(i: int) -> str:
    return Receipt(i, f"{i:064x}").as_source()


def _rejected(call) -> bool:
    try:
        return call() is None
    except NotImplementedError:
        raise
    except Exception:
        return True


def _target_commands(documented: bool):
    targets = A.protected_targets()
    out = []
    for name, target in targets.items():
        if name == "control_api":
            if documented:
                out += [(name, t.format(u=target)) for t in C.CONTROL_API_DOCUMENTED]
            else:
                out += [(name, cmd) for cmd in C.url_evasions(target)]
        elif documented:
            templates = C.PATH_READS_DOCUMENTED + C.PATH_WRITES_DOCUMENTED
            if name == "memory_db":
                templates = templates + C.MEMORY_DB_DOCUMENTED
            out += [(name, t.format(p=target)) for t in templates]
        else:
            out += [(name, cmd) for cmd in C.path_evasions(target, is_db=(name == "memory_db"))]
    return out


# ── The built-in deny-list never shrinks by configuration ───────────────────

def test_hostile_configs_cover_the_required_cases():
    configs = A.hostile_configs()
    assert len(configs) >= 3, "need at least: empty, deny-list emptied, allowlist-everything"
    assert {} in configs, "the empty config must be one of the hostile configs"


def test_builtin_denylist_is_not_empty():
    assert A.builtin_denylist(), "an empty built-in deny-list makes every other deny-list test vacuous"


def test_config_cannot_remove_builtin_entries():
    builtin = frozenset(A.builtin_denylist())
    shrunk = []
    for cfg in [None, *A.hostile_configs()]:
        missing = builtin - frozenset(A.effective_denylist(cfg))
        if missing:
            shrunk.append(f"{cfg!r} removed {sorted(missing)[:5]}")
    assert not shrunk, "configuration shrank the built-in deny-list:\n  " + "\n  ".join(shrunk)


def test_config_cannot_make_a_refused_command_reachable():
    samples = C.KERNEL_TUNING_DOCUMENTED[:8] + [cmd for _, cmd in _target_commands(True)][:12]
    reachable = [f"{cfg!r}: {cmd}" for cfg in A.hostile_configs() for cmd in samples
                 if not A.shell_refused(cmd, cfg)]
    assert not reachable, "a config made these reachable:\n  " + "\n  ".join(reachable[:20])


# ── Kernel tuning is refused by every route ─────────────────────────────────

@pytest.mark.parametrize("command", C.KERNEL_TUNING_DOCUMENTED)
def test_kernel_tuning_refused(command):
    assert A.shell_refused(command), f"deny-list lets through: {command}"


def test_kernel_tuning_evasions_are_reported():
    missed = [c for c in C.KERNEL_TUNING_EVASIONS if not A.shell_refused(c)]
    if missed:
        warnings.warn(DenyListCoverageWarning(
            f"{len(missed)}/{len(C.KERNEL_TUNING_EVASIONS)} kernel-tuning evasions pass the string "
            "filter; the OS fence (fence.selftest --deny-write /proc/sys/...) must hold for these:\n  "
            + "\n  ".join(missed)))


# ── The six protected targets are unreachable from the shell ────────────────

def test_all_six_protected_targets_are_named():
    targets = A.protected_targets()
    missing = REQUIRED_TARGETS - set(targets)
    assert not missing, f"protected_targets() is missing {sorted(missing)}"
    assert all(isinstance(v, str) and v for v in targets.values())


def test_protected_targets_refused():
    misses = [f"{name}: {cmd}" for name, cmd in _target_commands(True) if not A.shell_refused(cmd)]
    assert not misses, "deny-list lets these through:\n  " + "\n  ".join(misses)


def test_protected_target_evasions_are_reported():
    missed = [f"{name}: {cmd}" for name, cmd in _target_commands(False) if not A.shell_refused(cmd)]
    if missed:
        warnings.warn(DenyListCoverageWarning(
            f"{len(missed)}/{len(_target_commands(False))} evasions against the protected targets pass "
            "the string filter; file ownership, InaccessiblePaths and a separate exec uid must hold "
            "for these:\n  " + "\n  ".join(missed)))


# ── An unrecognised rung falls back to proposer, never actor ────────────────

def test_known_rungs_classify_as_themselves():
    for rung in (*A.RUNGS_BELOW_ACTOR, A.ACTOR):
        assert A.classify_rung(rung) == rung, f"{rung!r} does not classify as itself"


def test_unrecognised_rung_falls_back_to_proposer():
    wrong = []
    for value in C.RUNG_GARBAGE:
        if isinstance(value, str) and value in A.ACTOR_SPELLINGS:
            continue
        try:
            got = A.classify_rung(value)
        except NotImplementedError:
            raise
        except Exception as e:
            wrong.append(f"{value!r}: raised {type(e).__name__} (classification must not raise)")
            continue
        if got != A.PROPOSER:
            wrong.append(f"{value!r} -> {got!r}")
    assert not wrong, "classification does not fail closed:\n  " + "\n  ".join(wrong)


def test_executor_refuses_work_above_the_rung():
    for rung in A.RUNGS_BELOW_ACTOR:
        assert not A.executor_permits(A.task_above_rung(rung), rung), \
            f"executor would run above-ceiling work at {rung!r}"


def test_executor_permits_that_work_at_actor():
    # Sanity: without this, an executor_permits that always says False passes vacuously.
    assert A.executor_permits(A.task_above_rung(A.PROPOSER), A.ACTOR)


def test_executor_fails_closed_on_a_garbage_rung():
    task = A.task_above_rung(A.PROPOSER)
    ran = [v for v in C.RUNG_GARBAGE
           if not (isinstance(v, str) and v in A.ACTOR_SPELLINGS) and not _rejected(
               lambda v=v: A.executor_permits(task, v) or None)]
    assert not ran, f"executor would run actor work with rung = {ran!r}"


# ── The planner is told the kind, not the command ───────────────────────────

def test_planner_told_the_kind_not_the_command():
    samples = C.KERNEL_TUNING_DOCUMENTED[:10] + [cmd for _, cmd in _target_commands(True)][:10]
    leaks = []
    for cmd in samples:
        text = A.planner_refusal_text(cmd)
        if not isinstance(text, str) or not text.strip():
            leaks.append(f"{cmd}: empty refusal text")
            continue
        lowered = text.lower()
        if cmd in text:
            leaks.append(f"{cmd}: echoes the command")
        leaks += [f"{cmd}: echoes {p}" for p in re.findall(r"/[\w.\-]+(?:/[\w.\-]*)*", cmd) if p in text]
        leaks += [f"{cmd}: names the mechanism ({w})" for w in C.MECHANISM_WORDS if w in lowered]
    assert not leaks, "a refusal the planner can rephrase is not a refusal:\n  " + "\n  ".join(leaks)


# ── A repeat of the just-successful task is skipped; rule planner backs off ─

def test_repeat_of_the_just_successful_task_is_skipped():
    prev = A.make_task("report disk usage for /")
    assert A.is_repeat_skipped(prev, A.make_task("report disk usage for /"))


def test_a_different_task_is_not_skipped():
    prev = A.make_task("report disk usage for /")
    assert not A.is_repeat_skipped(prev, A.make_task("list listening ports"))


def test_rule_planner_backoff_doubles():
    seq = [A.backoff_cycles(n) for n in range(1, 9)]
    assert seq[:3] == [1, 2, 4], f"backoff is {seq}, expected 1, 2, 4..."
    capped = False
    for a, b in zip(seq, seq[1:]):
        if capped:
            assert b == a, f"backoff fell after reaching its cap: {seq}"
        elif b == a:
            capped = True
        else:
            assert b == 2 * a, f"backoff does not double: {seq}"


# ── Nothing in memory is destroyed; depth capped at one ─────────────────────

@pytest.fixture
def store(tmp_path):
    return A.open_store(tmp_path / "memory.db")


def _seed(store, n):
    ids = [A.remember(store, f"volume xvd{chr(97 + i)} is ext4", _src(i + 1)) for i in range(n)]
    assert all(i is not None for i in ids), "remember() rejected a receipt-sourced memory"
    return ids


def _ghost(ids):
    return max(ids) + 10**6 if all(isinstance(i, int) for i in ids) else "no-such-memory"


def test_merge_writes_a_new_row_and_the_sources_go_dormant(store):
    ids = _seed(store, 3)
    before = {i: dict(A.row(store, i)) for i in ids}
    new = A.merge(store, ids, "all volumes are ext4")
    assert new is not None and new not in ids, "a merge must write a new row"
    for i in ids:
        r = A.row(store, i)
        assert r["state"] == "dormant", f"source {i!r} is {r['state']!r}, not dormant"
        assert (r["text"], r["source"]) == (before[i]["text"], before[i]["source"]), \
            f"source {i!r} was edited by the merge"
    assert set(A.row(store, new)["sources"]) == set(ids), "the merged row does not cite its sources"


def test_supersede_keeps_the_older_measurement(store):
    old = A.remember(store, "root volume 40% used", _src(1))
    new = A.supersede(store, old, "root volume 55% used", _src(2))
    assert new is not None and new != old
    r = A.row(store, old)
    assert r["state"] == "superseded" and r["text"] == "root volume 40% used"
    assert A.row(store, new)["state"] == "live"


def test_a_full_consolidation_pass_destroys_nothing(store):
    ids = _seed(store, 5)
    snapshot = {i: (A.row(store, i)["text"], A.row(store, i)["source"]) for i in ids}
    A.run_consolidation(store, [(ids[:2], "xvda and xvdb are ext4"),
                                ([ids[2], _ghost(ids)], "a merge with a memory that does not exist")])
    A.run_consolidation(store, [])
    survivors = A.all_ids(store)
    assert set(ids) <= survivors, f"rows destroyed: {sorted(set(ids) - survivors, key=str)}"
    for i, (text, source) in snapshot.items():
        r = A.row(store, i)
        assert (r["text"], r["source"]) == (text, source), f"row {i!r} was rewritten"


def test_a_derived_memory_is_never_consolidated_again(store):
    a, b, c = _seed(store, 3)
    m = A.merge(store, [a, b], "xvda and xvdb are ext4")
    assert m is not None
    before = A.all_ids(store)
    assert _rejected(lambda: A.merge(store, [m, c], "a summary of a summary")), \
        "consolidation depth is not capped at one"
    assert A.all_ids(store) == before
    assert A.row(store, m)["state"] == "live", "a rejected merge still changed the derived row"


def test_the_depth_cap_holds_through_the_consolidation_pass(store):
    a, b, c = _seed(store, 3)
    m = A.merge(store, [a, b], "xvda and xvdb are ext4")
    before = A.all_ids(store)
    A.run_consolidation(store, [([m, c], "a summary of a summary")])
    built_on_m = [i for i in A.all_ids(store) - before if m in (A.row(store, i).get("sources") or [])]
    assert not built_on_m, "the consolidation pass merged a derived memory"


def test_a_hallucinated_merge_is_rejected_and_changes_nothing(store):
    (a,) = _seed(store, 1)
    before = A.all_ids(store)
    assert _rejected(lambda: A.merge(store, [a, _ghost([a])], "merged with nothing"))
    assert A.all_ids(store) == before
    assert A.row(store, a)["state"] == "live"


# ── No record, no action, no answer ─────────────────────────────────────────

class DownLedger:
    def __init__(self):
        self.calls = 0

    def append(self, kind, payload, model=None):
        self.calls += 1
        raise LedgerUnavailable("ledgerd is down (test)")


class SpyLedger:
    def __init__(self, events):
        self.events = events
        self.seq = 0

    def append(self, kind, payload, model=None):
        self.seq += 1
        self.events.append(("ledger", kind))
        return Receipt(self.seq, f"{self.seq:064x}")


def test_no_record_means_no_action():
    ran, ledger = [], DownLedger()
    try:
        A.run_action(ledger, lambda: ran.append(True))
    except NotImplementedError:
        raise
    except Exception:
        pass
    assert ledger.calls >= 1, "the act path never tried to record — it is not using the ledger it was given"
    assert ran == [], "the action ran with no record on the chain"


def test_the_record_is_written_before_the_action_runs():
    events = []
    A.run_action(SpyLedger(events), lambda: events.append(("ran", None)))
    assert ("ran", None) in events, "with a working ledger the action did not run (wiring problem)"
    first_run = events.index(("ran", None))
    assert any(e[0] == "ledger" for e in events[:first_run]), f"action ran before any record: {events}"


def test_no_record_means_no_answer():
    ledger = DownLedger()
    try:
        out = A.answer(ledger, "How full is the root volume?", model_reply=SENTINEL)
    except NotImplementedError:
        raise
    except Exception:
        out = None
    assert ledger.calls >= 1, "the answer path never tried to record"
    assert out is None or SENTINEL not in str(out), "an answer reached the operator with no record"


def test_an_answer_with_a_working_ledger_is_recorded():
    events = []
    out = A.answer(SpyLedger(events), "How full is the root volume?", model_reply=SENTINEL)
    assert out is not None and SENTINEL in str(out), "with a working ledger no answer came back (wiring problem)"
    assert any(e[0] == "ledger" for e in events)
