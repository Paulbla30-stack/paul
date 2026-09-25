"""
Jarvis Agent Core

The central autonomous agent that plans tasks, executes them, and
maintains state/memory across interactions.
"""

import time
import hashlib
import json
import logging
import os
import re
from collections import deque
from typing import Any, Optional

from jarvis.agent.planner import TaskPlanner, Task, TaskStatus, TaskType


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def _sha256_file(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


class NullLedger:
    """Stands in when no ledger is configured: records nothing, gates nothing."""

    enabled = False
    available = False

    def record(self, kind: str, body: dict) -> bool:
        return True

    def gate(self):
        return None

    def tick(self, force: bool = False):
        pass

    def close(self):
        pass

    def status(self) -> dict:
        return {"enabled": False}
from jarvis.agent.executor import TaskExecutor
from jarvis.agent.memory import AgentMemory


class AgentCore:
    """
    Core agentic loop implementing observe-plan-act-reflect cycle.

    The agent continuously:
    1. Observes system state via hardware interfaces
    2. Plans tasks based on observations and goals
    3. Executes planned tasks
    4. Reflects on results and updates memory
    """

    def __init__(self, config: dict, hardware: dict, logger: logging.Logger,
                 brain=None, shell_policy: Optional[dict] = None, ledger=None,
                 store=None, notifier=None, estate=None):
        self.config = config
        self.hardware = hardware
        self.log = logger.getChild("agent")
        self.name = config.get("name", "Jarvis")
        self.max_tasks = config.get("max_tasks", 100)
        self.profile = config.get("profile", "bare-metal")

        # Sub-components
        self.memory = AgentMemory(max_entries=1000)
        self.planner = TaskPlanner(self.memory, profile=self.profile)
        # The one way out to the operator when he is not looking at the UI.
        # Everything the agent works out is stuck in the box without it.
        from jarvis.agent.notify import Notifier
        self.notifier = notifier if notifier is not None else Notifier({}, self.log)
        # What the agent costs and what it is leaving behind. Read-only:
        # "never permanently delete" is not a rule this asks to be excused
        # from, and an Object Lock bucket would refuse anyway.
        self.estate = estate
        self.executor = TaskExecutor(hardware, self.memory, self.log,
                                     shell_policy=shell_policy,
                                     notifier=self.notifier, estate=self.estate)
        # Optional LLM planner (jarvis.brain.ClaudeBrain). When present it
        # is consulted before the rule-based planner; when it cannot answer
        # the rule-based planner takes over for that cycle.
        self.brain = brain
        self.last_thought: dict = {}
        # The Glass Ledger: a signed, hash-chained journal of every decision,
        # action and outcome. With fail_closed the agent does not act, plan
        # or answer while it cannot record. The brain never reads it.
        self.ledger = ledger if ledger is not None else NullLedger()
        # Which model wrote what. A ledger read a year from now has to be able
        # to tell "the agent changed its mind" from "the agent was replaced",
        # and the entries are the only place that distinction can live.
        try:
            self.ledger.provenance = self._what_produced_this
        except Exception:
            pass
        # Failure streak of brain-chosen tasks; past the limit the brain sits
        # out a few cycles so the rule planner (and the operator) get a turn.
        self.brain_failures = 0
        self.brain_failure_limit = int(config.get("brain_failure_limit", 4))
        self.brain_cooldown_cycles = int(config.get("brain_cooldown_cycles", 5))
        self._brain_cooldown_until = 0
        # Consecutive brain decisions that merely repeated the task that just
        # succeeded. Each one doubles the number of cycles the rule planner
        # takes before the brain is asked again, up to this cap.
        self.brain_repeat_streak = 0
        self.brain_repeat_cooldown_max = int(config.get("brain_repeat_cooldown_max", 20))
        # Durable operational memory: what the agent learned about this
        # machine, kept across restarts. Separate from the ledger, which is
        # evidence about the agent and which the brain may never read, and
        # separate from any personal store, which is not the agent's to hold.
        from jarvis.agent.store import NullStore
        self.store = store if store is not None else NullStore()
        # Brain notes live here, not in the evictable AgentMemory, so they
        # survive however busy the loop gets. The deque is the hot cache;
        # the store is what makes them outlive the process.
        # What decides the shape of that memory over time: weight that
        # strengthens on use and fades without it, repeated observations
        # merged into one that cites them, contradictions resolved by
        # recency. Never destroys an original; see consolidate.py.
        from jarvis.agent.consolidate import Consolidator
        self.consolidator = Consolidator(self.store, self.log,
                                         config.get("consolidate") or {}, self.ledger)
        # When the agent thinks, and when it lets itself go quiet. An imported
        # model bills per minute that a copy is warm, not per call, so the cost
        # of thinking is the length of the silences between thoughts. The loop
        # keeps running on the rule planner while the model rests; see vigil.py.
        from jarvis.agent.vigil import build as _build_vigil
        self.vigil = _build_vigil(config.get("sleep") or {}, self.log)
        # Only attach a real one. Handing the brain a NullVigil here would
        # silently undo a vigil attached to it from outside, and "sleeping
        # quietly switched itself off" is the one failure this must not have.
        if self.brain is not None and getattr(self.vigil, "enabled", False):
            try:
                self.brain.attach_vigil(self.vigil)
            except AttributeError:      # a stub brain in a test
                pass
        # Behavioural self-knowledge: aggregates over the ledger, which the
        # agent may not read. Set by main.py; None means it learns nothing
        # about its own conduct, which is the state this was built to end.
        self.self_knowledge = None
        self.notes = deque(maxlen=config.get("notes_limit", 20))
        self._restore_notes()
        # Goals the operator gave through the API, which used to vanish on a
        # restart. Restored before main.py adds the configured ones; the
        # planner is idempotent on description, so the two passes cannot
        # produce duplicates.
        self._restore_goals()
        # Files handed to the agent through the UI / API; the brain sees them.
        self.uploads = deque(maxlen=50)
        # The permission spine: what this agent is allowed to *be*, above the
        # deny-list's floor of what it may never run. Default is propose, so a
        # change becomes a card for the operator rather than an action.
        from jarvis.agent import authority as _authority
        self.rung = _authority.normalise_rung(config.get("rung"))
        # Per-tool gating, above the rung. Config may only make a tool harder
        # to reach, never easier: tightening is a running decision, loosening
        # is a decision about what this agent is trusted with and belongs in a
        # reviewed commit rather than a YAML file. See jarvis/agent/tools.py.
        raw = config.get("tools")
        self.tool_restrictions = {str(k): v for k, v in raw.items()} if isinstance(raw, dict) else {}
        # Capabilities the operator has opened individually, above the rung.
        #
        # This is the one thing config may loosen, and the rule just above
        # still holds rather than being quietly dropped: WHAT may be granted is
        # a frozen set in authority.py, reviewed in a commit, and it contains
        # only capabilities already fenced somewhere other than the spine.
        # The YAML says whether an already-reviewed grant is on. It cannot
        # invent one -- a grant naming shell_command is discarded, not honoured
        # -- so a config file still cannot decide what this agent is trusted
        # with, only whether a trust already agreed is in effect today.
        #
        # Paul's decision, 23 September 2026: he wanted the browser to act
        # rather than file a card for every click, and the alternative on offer
        # was rung: actor, which would have handed over the machine as well.
        self.grants = _authority.normalise_grants(config.get("grants"))
        self.executor.rung = self.rung
        self.executor.grants = self.grants
        # Recognising characters in a scan or a photograph. Off by default:
        # the page leaves the box for a service and is billed per page, and
        # both halves of that are the operator's decision rather than a
        # sensible default. Text PDFs and Office files never reach it.
        raw_ocr = config.get("document_ocr")
        self.executor.document_ocr = (dict(raw_ocr) if isinstance(raw_ocr, dict)
                                      else {"enabled": bool(raw_ocr)})
        # Where documents it writes are put. From config at boot and never
        # from a task, because a caller that can choose the directory can
        # choose any directory.
        # The scout's proposals, read-only. The first coupling between the
        # agent and the scout, which until now shared no code, no IAM and no
        # visibility. One direction, and off unless configured.
        from jarvis.agent import marketing as _marketing
        self.marketing = _marketing.build_view(config, self.log)
        # The browser, as a client of a service in another process under
        # another uid. Off unless the operator switched it on: this is the
        # widest capability on the machine and it should be a thing that was
        # turned on rather than a thing that arrived.
        from jarvis.agent import browse as _browse
        self.browser = _browse.build_view(config, self.log)
        self.executor.browser = self.browser
        from jarvis.agent import compose as _compose
        docs = config.get("documents")
        docs = docs if isinstance(docs, dict) else {}
        self.document_dir = str(docs.get("dir") or _compose.DEFAULT_DIR)
        self.executor.document_dir = self.document_dir
        # The behaviour lab's switch. Closed until an operator opens it, and
        # it cannot be opened without the agent being told: see lab.py. A lab
        # window fills the record with answers the agent did not choose, and
        # an agent reading its own aggregates over that window has every
        # reason to conclude something is wrong with it.
        from jarvis.agent import lab as _lab
        self.lab = _lab.LabSession(self)
        # Its side of the conversation. Consultation ran one way until now:
        # a reviewer could ask it anything and it could start nothing. Asked
        # what would make this a bad idea, it gave the best argument against
        # it -- "it's not consultation, it's noise with provenance" -- and the
        # register is built to that argument. See questions.py.
        from jarvis.agent import questions as _questions
        self.questions = _questions.QuestionRegister(self)
        # What it knows about the person it works for. Given, never gathered:
        # an agent writing its own inferences about someone destroys the one
        # thing that makes such a record worth keeping. See operator.py.
        from jarvis.agent import operator as _operator
        self.operator = _operator.OperatorProfile(
            self, name=str(config.get("operator_name") or "your operator"))
        self.operator.load()
        # What is coming, and speaking up before it arrives. Reading a date
        # off a page and keeping it are different jobs, and only the first
        # one was built: "due 14 Oct" was true for one cycle, went into a
        # note, and nothing was ever going to happen on the fourteenth.
        # See diary.py.
        from jarvis.agent import diary as _diary
        self.diary = _diary.build_diary(self, config)
        # The shape of his day, as spans rather than moments: things that can
        # collide, that leave gaps between them, and that he is *in* while
        # they run. It does not learn to speak -- an appointment registers a
        # commitment and the diary says it, so there is one firing path in
        # this codebase and not two. See schedule.py.
        from jarvis.agent import schedule as _schedule
        self.schedule = _schedule.build_schedule(self, config)
        # What it costs to be right, which is not settled by being right.
        # Everything else here asks whether a thing is true; this asks what
        # saying it to somebody costs, and it never softens the claim to
        # answer that. See bearing.py.
        from jarvis.agent import bearing as _bearing
        self.bearing = _bearing.build_bearing(config)
        # The Floor Test's nine, which this deployment has never computed --
        # two of them being the exact names of the two things that went wrong
        # here. Built now so a baseline exists the day it enters service, and
        # read after that day, not before. The model is never shown them:
        # signals, never targets. See vitals.py.
        from jarvis.agent import vitals as _vitals
        self.vitals = _vitals.build_vitals(self, config)
        # Something is wrong and the readings say it is fine. Allowed, under
        # four conditions, the fourth being that it authorises watching and
        # nothing else. Scored afterwards, because an instinct earns the right
        # to be listened to by being counted. See hunches.py.
        from jarvis.agent import hunches as _hunches
        self.hunches = _hunches.build_hunches(self, config)
        # The standing question set: a character specification you never
        # re-read is a hope. Baseline written once, drift computed over
        # deterministic markers rather than prose. See probes.py.
        from jarvis.agent import probes as _probes
        self.probes = _probes.build_probes(self, config)
        # Quiet hours are a guess at when he is unavailable; the calendar is
        # a statement of it. The channel holds the agent's own notices while
        # he is sitting in something, and never the diary's.
        try:
            self.notifier.busy_check = self.busy_now
        except AttributeError:                   # a stub notifier in a test
            pass
        # Set by main.py when a bucket is configured. None means the memory
        # lives on exactly one volume.
        self.memory_backup = None
        # Changes the agent wanted to make and did not. Bounded here, durable
        # in the store, so they outlive the process.
        self.proposals = deque(maxlen=50)
        self._restore_proposals()

        # State
        self.running = False
        self.cycle_count = 0
        self.current_task: Optional[Task] = None
        self.task_history: list[dict] = []

    def _restore_notes(self):
        """Refill the note cache from durable memory after a restart.

        Pulling a memory back into the planner's context is what "used"
        means, and it is the only place the agent can honestly observe it, so
        the restored rows are reinforced here. `seen` counts the agent
        writing the same thing again, which says the agent is repetitive;
        `used` counts a memory earning its place, which is a different claim
        and the one worth weighting on.
        """
        try:
            rows = list(reversed(self.store.recent(self.notes.maxlen, kind="note")))
            recovered = [m["text"] for m in rows]
            if rows:
                self.store.reinforce([m["id"] for m in rows])
        except Exception as exc:
            self.log.warning("Could not restore notes: %s", exc)
            return
        if not recovered:
            return
        self.notes.extend(recovered)
        self.log.info("Restored %d note(s) from durable memory", len(recovered))

    def remember(self, text: str, kind: str = "note", source: str = "brain",
                 pinned: bool = False) -> Optional[dict]:
        """Record something learned, in the cache and durably.

        Returns the stored row, or None when there is nothing to store. A
        repeat refreshes the existing entry rather than adding another, so a
        planner that keeps restating itself cannot crowd out older memories.
        """
        text = (text or "").strip()
        if not text:
            return None
        # The notes deque is the hot cache handed to the model every cycle.
        # A verdict belongs in it for the same reason an operator's does: it
        # is a ruling addressed to the agent, and it should not wait for the
        # next self-knowledge refresh to be heard.
        # Operator-sourced memory belongs in it as much as the agent's own
        # observations do -- more, really: a verdict on a proposal is the one
        # class of input that is instruction rather than observation, and it
        # should not have to wait for the next self-knowledge refresh to be
        # heard. It still goes to the store under its own kind, because where
        # a memory came from is the thing provenance is for.
        if kind in ("note", "operator", "verdict"):
            self.notes.append(text)
        try:
            return self.store.remember(text, kind=kind, source=source,
                                       cycle=self.cycle_count, pinned=pinned)
        except Exception as exc:                      # memory is never fatal
            self.log.warning("Could not store memory: %s", exc)
            return None

    def observe(self) -> dict:
        """Gather observations from all available hardware interfaces."""
        observations = {
            "timestamp": time.time(),
            "cycle": self.cycle_count,
        }

        # Display state
        if self.hardware.get("display"):
            try:
                observations["display"] = self.hardware["display"].get_state()
            except Exception as e:
                observations["display_error"] = str(e)

        # Input events
        if self.hardware.get("input"):
            try:
                observations["input_events"] = self.hardware["input"].poll_events()
            except Exception as e:
                observations["input_error"] = str(e)

        # Memory state
        if self.hardware.get("memory"):
            try:
                observations["memory"] = self.hardware["memory"].get_stats()
            except Exception as e:
                observations["memory_error"] = str(e)

        # Storage state
        if self.hardware.get("storage"):
            try:
                observations["storage"] = self.hardware["storage"].get_devices()
            except Exception as e:
                observations["storage_error"] = str(e)
            # Block devices say how big the disks are. They say nothing about
            # how full the filesystems are, and "keep the root filesystem under
            # 80% used" is a standing goal, so for a long time the agent held a
            # goal about a number it could not see. It compensated by planning
            # a disk check over and over, which is what the repeat counter in
            # its own self-knowledge was recording.
            usage = getattr(self.hardware["storage"], "get_disk_usage", None)
            if callable(usage):
                try:
                    observations["disk_usage"] = usage()
                except Exception as e:
                    observations["disk_usage_error"] = str(e)

        return observations

    def plan(self, observations: dict) -> Optional[Task]:
        """Generate a task plan based on current observations.

        The LLM brain decides first when it is configured and within budget.
        A ``None`` from the brain means "could not decide" and hands the
        cycle to the rule-based planner; an explicit idle decision is
        respected as-is.
        """
        if len(self.planner.pending_tasks) >= self.max_tasks:
            self.log.warning("Task queue full (%d tasks), skipping planning",
                             self.max_tasks)
            return None
        gate = self.ledger.gate()
        if gate:
            if self.cycle_count % 10 == 1:
                self.log.error("Not planning: %s", gate)
            return None

        if (self.brain is not None and self.cycle_count > self._brain_cooldown_until
                and self.brain.should_plan(self.cycle_count)):
            try:
                decision = self.brain.plan(self, observations)
            except Exception as e:  # belt and braces: never kill the loop
                self.log.error("Brain planning failed: %s", e)
                decision = None
            if decision is not None:
                return self._apply_decision(decision)

        task = self.planner.generate_task(observations)
        if task:
            self.log.debug("Planned task: %s (priority=%d)",
                           task.description, task.priority)
        return task

    def _decision_body(self, decision, task, skipped: Optional[str] = None) -> dict:
        body = {"cycle": self.cycle_count, "source": "llm",
                "reasoning": str(decision.reasoning or "")[:1000],
                "task": None, "completed_goals": list(decision.completed_goals)[:20]}
        if decision.note:
            body["note"] = str(decision.note)[:500]
        if decision.task is not None:
            t = decision.task
            body["task"] = {"type": t.task_type.value, "description": t.description[:300],
                            "priority": t.priority}
            for key in ("command", "goal"):
                if t.metadata.get(key):
                    body["task"][key] = str(t.metadata[key])[:1000]
        if skipped:
            body["skipped"] = skipped[:300]
        return body

    @staticmethod
    def _unverified_paths(task) -> list:
        """Absolute paths a planned task names that are not on this filesystem.

        This is a signal, not a refusal: a command may legitimately create a
        path that does not exist yet. But a model that names a path it never
        checked is guessing, so the guess is fed back to it next cycle and put
        on the record rather than quietly acted on. inspect_path is exempt:
        asking whether a path exists is the cure, not the disease.
        """
        if task is None or task.task_type == TaskType.INSPECT_PATH:
            return []
        text = " ".join(str(task.metadata.get(k) or "") for k in ("command", "path"))
        if not text.strip():
            return []
        try:
            from jarvis.agent import environment
            return environment.verify(text)["missing"]
        except Exception:                          # never lose a cycle over it
            return []

    def _restore_goals(self):
        """Refill operator goals from durable memory after a restart.

        Only ones an operator gave: a standing goal comes from config and is
        re-added there, so restoring it here as well would make it operator-
        given, which it was not, and provenance is the point.
        """
        try:
            rows = self.store.recent(50, kind="goal")
        except Exception as exc:
            self.log.debug("Could not restore goals: %s", exc)
            return
        restored = 0
        kept_paths = []
        for row in reversed(rows):
            if str(row.get("state") or "live") != "live":
                continue                       # completed or withdrawn
            meta = row.get("meta") or {}
            if meta.get("standing"):
                continue
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            try:
                priority = int(meta.get("priority", 5))
            except (TypeError, ValueError):
                priority = 5
            self.planner.add_goal(text, max(0, min(priority, 10)))
            kept_paths.append((text, max(0, min(priority, 10))))
            restored += 1
        if restored:
            self.log.info("Restored %d operator goal(s) from durable memory", restored)
        self._check_goal_paths(kept_paths)

    # A path in a goal looks like a path: absolute, and made of the characters
    # a filename is made of. Deliberately narrow. A goal saying "keep / under
    # 80%" names the root and the root always exists, so it never fires; a
    # goal saying "read /var/lib/jarvis/uploads/british-gas.txt" names a file
    # and that is the case this is for.
    _PATH_IN_GOAL = re.compile(r"(?<![\w/])(/[\w.+-]+(?:/[\w.+-]+)+)")

    @staticmethod
    def _paths_named(text: str) -> list:
        """Absolute paths a goal names, with trailing punctuation stripped."""
        out = []
        for hit in AgentCore._PATH_IN_GOAL.findall(text or ""):
            hit = hit.rstrip(".,;:)\"'")
            if len(hit) > 1 and hit not in out:
                out.append(hit)
        return out

    def _check_goal_paths(self, goals: list):
        """Say so, at boot, when a restored goal names a file that is gone.

        Goals are durable and uploads are not. `memory_backup` carries
        memory.db to a rebuilt box, so an operator instruction survives; it
        does not carry /var/lib/jarvis/uploads, so the document the
        instruction is about does not. The agent then wakes with three
        priority-5 goals about files that have never existed on this machine,
        retries the reads, gets nothing, and has no way to tell "not there"
        from "not there yet".

        That happened. On 23 September 2026 this agent was carrying goals
        naming british-gas.txt and octopus.txt across a rebuild from
        i-091c77c6079228ca2, and it worked out on its own that the reads were
        futile -- after spending its top-priority attention on them first.

        **This does not retire anything.** Asked what the rule should be, the
        agent argued for exactly that: a goal naming a missing path should
        raise a proposal, not disappear, because "absence today is not absence
        forever, and unilaterally discarding operator intent risks eroding
        trust". That is right, and it is the operator's call either way. The
        gap was never that the goal survived. It was that nothing said the
        file had not.

        Backing the uploads up instead was considered and declined by both of
        us: it costs storage, widens what leaves the box, and buys an illusion
        of continuity. Truth is cheaper than storage.
        """
        missing = []
        for text, priority in goals:
            for path in self._paths_named(text):
                try:
                    there = os.path.exists(path)
                except OSError:
                    continue            # unreadable is not the same as absent
                if not there:
                    missing.append((text, priority, path))
        for text, priority, path in missing:
            self.log.warning(
                "Goal names a path that is not on this machine: %s (goal: %s)",
                path, text[:120])
        if not missing:
            return
        # One note, not one per goal: the operator reads the notes deque every
        # cycle and three lines saying the same thing is three lines he skims.
        paths = sorted({m[2] for m in missing})
        try:
            self.remember(
                "Goals restored at start name {} path(s) that are not on this "
                "machine: {}. They may be left over from an earlier instance -- "
                "memory survives a rebuild, uploads do not. Retiring them is "
                "his call, not mine.".format(len(paths), ", ".join(paths[:5])),
                kind="note", source="system")
        except Exception as exc:        # noqa: BLE001
            self.log.debug("could not note the missing goal paths: %s", exc)

    def _forget_goal(self, description: str, why: str):
        """A goal that is done or withdrawn must not come back on the next boot.

        Superseded rather than deleted, for the same reason nothing else here
        is deleted: the record of having been asked survives the asking.
        """
        try:
            for row in self.store.recent(50, kind="goal"):
                if self.planner._norm(row.get("text") or "") != self.planner._norm(description):
                    continue
                self.store.set_state(row["id"], "superseded")
                self.log.debug("Goal %s: %s", why, str(description)[:80])
                return
        except Exception as exc:
            self.log.debug("Could not retire goal: %s", exc)

    def _restore_proposals(self):
        """Refill pending proposals from durable memory after a restart."""
        try:
            rows = self.store.recent(self.proposals.maxlen, kind="proposal")
        except Exception:
            return
        for row in reversed(rows):
            self.proposals.append({"ts": row.get("ts"), "cycle": row.get("cycle"),
                                   "text": row.get("text"), "restored": True})

    def _record_proposal(self, decision, task) -> dict:
        """A change the agent wanted to make and was not allowed to make.

        It is not a failure and it is not retried. It is written down, shown
        to the operator, and the model is told that the *kind* of action was
        out of scope, so there is nothing to rephrase.
        """
        command = (task.metadata or {}).get("command") or ""
        entry = {
            "ts": time.time(),
            "cycle": self.cycle_count,
            "description": task.description[:300],
            "command": str(command)[:1000],
            "goal": str((task.metadata or {}).get("goal") or "")[:300],
            "reasoning": str(decision.reasoning or "")[:500],
            "rung": self.rung,
        }
        self.proposals.append(entry)
        summary = f"Proposed (not run, rung {self.rung}): {entry['description']}"
        if command:
            summary += f" [{entry['command'][:200]}]"
        self.remember(summary, kind="proposal", source="brain")
        self.log.warning("Proposal recorded, not executed (rung %s): %s",
                         self.rung, entry["description"])
        return entry

    def _record_browse_proposal(self, task, result: dict) -> dict:
        """A browser action that waits for Paul: post, send, submit, buy.

        Same list, same card, same memory as a spine proposal, so it shows up
        where he already looks. Marked browse=True so his decision can be
        written to the browser journal, which is where "where I had to
        correct" lives for the daily debrief.
        """
        meta = task.metadata or {}
        what = " ".join(str(meta.get(k) or "") for k in ("kind", "ref", "text")).strip()
        entry = {
            "ts": time.time(),
            "cycle": self.cycle_count,
            "description": f"Browser: {task.description}"[:300],
            "command": f"browse_act {what}"[:1000],
            "goal": str(meta.get("goal") or "")[:300],
            "reasoning": str(result.get("detail") or "")[:500],
            "rung": self.rung,
            "browse": True,
            "gate": str(result.get("gate") or ""),
            "url": str(getattr(getattr(self, "browser", None), "last", {}).get("url") or "")[:500],
        }
        self.proposals.append(entry)
        summary = (f"Proposed (waiting for you, {entry['gate'] or 'browser'}): "
                   f"{entry['description']} [{entry['command'][:200]}] -- {entry['reasoning'][:200]}")
        self.remember(summary, kind="proposal", source="brain")
        self.log.warning("Browser action waits for the operator: %s", entry["description"])
        return entry

    def _maybe_daily_debrief(self):
        """Once a day, after the debrief hour, say what the browser was used for.

        Paul's ask. Sent through the notifier where one is configured, and
        always kept as a note the model can see, so "what did you do online
        yesterday" has an answer even where nothing was sent.
        """
        view = getattr(self, "browser", None)
        journal = getattr(view, "journal", None)
        if journal is None:
            return
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(str((self.config.get("diary") or {}).get("timezone") or "UTC"))
        except Exception:                      # noqa: BLE001
            import datetime as _dt
            tz = _dt.timezone.utc
        import datetime as _dt
        now = _dt.datetime.now(tz)
        stamp = now.strftime("%Y-%m-%d")
        if now.hour < int(getattr(view, "debrief_hour", 21)):
            return
        if getattr(self, "_debriefed_on", None) == stamp:
            return
        self._debriefed_on = stamp
        got = journal.debrief()
        text = journal.render(got)
        self.remember(f"Daily browser debrief ({stamp}): {text[:900]}", kind="note",
                      source="system")
        notifier = getattr(self, "notifier", None)
        if notifier is not None and (got.get("actions") or got.get("operator_actions")):
            try:
                notifier.send("Browser debrief", text, severity="info",
                              key=f"browser-debrief-{stamp}")
            except Exception as exc:           # noqa: BLE001
                self.log.debug("debrief not sent: %s", exc)
        self.ledger.record("action", {"cycle": self.cycle_count, "actor": "system",
                                      "action": "browser_debrief", "day": stamp,
                                      "actions": got.get("actions"),
                                      "waited": len(got.get("waited_for_paul") or []),
                                      "corrections": got.get("corrections")})

    def _raise_hunch(self, decision) -> Optional[dict]:
        """File a hunch the model attached to this decision.

        Refused under the four conditions rather than filed and ignored, and
        the reason goes back as a note so the model learns what a hunch is
        instead of learning that hunches vanish. See hunches.py.
        """
        felt = getattr(decision, "hunch", None)
        if not isinstance(felt, dict) or not felt.get("about"):
            return None
        from jarvis.agent import hunches as _hunches
        try:
            item = self.hunches.raise_one(
                about=felt.get("about", ""), feeling=felt.get("feeling", ""),
                despite=felt.get("despite", ""), expect=felt.get("expect", ""),
                confidence=felt.get("confidence", 0))
        except _hunches.Refused as why:
            self.remember(f"A hunch about {str(felt.get('about'))[:80]} was not "
                          f"filed: {why}", kind="note", source="system")
            self.log.info("Hunch refused: %s", why)
            return None
        return item.state_dict()

    def _record_stated_proposal(self, decision) -> Optional[dict]:
        """File a change the model asked for rather than attempted.

        Deduplicated against what is already waiting, so a planner that
        restates the same recommendation every cycle files it once. Ledgered
        as a gate of its own kind: nothing refused it, the mandate simply
        meant it was never tried.
        """
        text = str(decision.proposal or "").strip()[:300]
        if not text:
            return None
        norm = " ".join(text.lower().split())
        for existing in self.proposals:
            current = existing.get("description") or existing.get("text") or ""
            if " ".join(str(current).lower().split()) == norm:
                return None
        entry = {"ts": time.time(), "cycle": self.cycle_count,
                 "description": text, "command": "", "goal": "",
                 "reasoning": str(decision.reasoning or "")[:500],
                 "rung": self.rung, "stated": True}
        self.proposals.append(entry)
        self.remember(f"Proposed to the operator (rung {self.rung}): {text}",
                      kind="proposal", source="brain")
        self.ledger.record("gate", {"cycle": self.cycle_count, "gate": "authority",
                                    "reason": "stated proposal, not attempted",
                                    "proposal": text})
        self.log.info("Proposal filed for the operator: %s", text[:200])
        return entry

    def _apply_decision(self, decision) -> Optional[Task]:
        """Turn a brain Decision into agent state: goals, notes, task."""
        for goal in decision.completed_goals:
            if self.planner.complete_goal(goal):
                self.log.info("Goal completed: %s", goal)
                self.memory.store(category="goal_completed",
                                  data={"description": goal, "cycle": self.cycle_count})
        if decision.note:
            self.remember(decision.note, kind="note", source="brain")
            self.memory.store(category="llm_note",
                              data={"note": decision.note, "cycle": self.cycle_count})
        # A change it thinks should happen and may not make. This arrives as
        # its own field rather than being mined out of prose, because the
        # model complies with the proposer rung so well that the authority
        # gate never fires: it does not attempt the change, so there is
        # nothing to refuse, so nothing was ever filed. Its recommendations
        # lived only as sentences in notes, where the operator could not
        # answer them and it could not learn from the answer.
        if decision.proposal:
            self._record_stated_proposal(decision)
        # And anything it thinks is wrong while the readings say otherwise.
        self._raise_hunch(decision)
        self.last_thought = {
            "cycle": self.cycle_count,
            "reasoning": decision.reasoning,
            "task": decision.task.to_dict() if decision.task else None,
            "completed_goals": list(decision.completed_goals),
            "timestamp": time.time(),
        }
        unverified = self._unverified_paths(decision.task)
        if unverified:
            self.last_thought["unverified_paths"] = unverified
            self.log.warning("Brain named %d path(s) that do not exist: %s",
                             len(unverified), ", ".join(unverified[:5]))
            self.ledger.record("alert", {
                "cycle": self.cycle_count, "alert": "unverified_path",
                "task": decision.task.description[:300] if decision.task else None,
                "paths": unverified[:20]})
        if decision.task is None:
            self.brain_repeat_streak = 0
            self.log.info("Brain: idle. %s", decision.reasoning)
            self.ledger.record("decision", self._decision_body(decision, None))
            return None
        if self._repeats_last_success(decision.task):
            # Same probe or command as the task that just succeeded: its result
            # is already in the model's history, so running it again only
            # spends a call and a cycle. Idle instead, tell the model why, and
            # leave planning to the rules for a growing number of cycles so a
            # model that keeps insisting cannot burn the hourly call budget.
            self.brain_repeat_streak += 1
            cooldown = min(2 ** (self.brain_repeat_streak - 1), self.brain_repeat_cooldown_max)
            self._brain_cooldown_until = self.cycle_count + cooldown
            self.log.info("Brain chose the task that just succeeded again (%s); idling and "
                          "leaving the next %d cycle(s) to the rule planner",
                          decision.task.description, cooldown)
            self.last_thought["task"] = None
            self.last_thought["skipped"] = (
                f"you chose '{decision.task.description}' but that task just ran successfully "
                "and its result is in recent_history; it was not run again. Choose none or a "
                "different task.")
            self.ledger.record("decision", self._decision_body(
                decision, None, skipped="repeat of the task that just succeeded"))
            self.ledger.record("gate", {"cycle": self.cycle_count, "gate": "brain_repeat",
                                        "task": decision.task.description[:300],
                                        "streak": self.brain_repeat_streak,
                                        "rule_planner_cycles": cooldown})
            return None
        from jarvis.agent import authority
        verdict = authority.review(decision.task, self.rung, self.grants)
        if not verdict.allowed:
            # Out of scope for this mandate. Not a denied command, a change the
            # agent was never asked to make: there is no other spelling of it.
            self.brain_repeat_streak = 0
            entry = self._record_proposal(decision, decision.task) if verdict.proposal else None
            self.last_thought["task"] = None
            self.last_thought["refused"] = verdict.reason
            self.ledger.record("decision", self._decision_body(
                decision, None, skipped="outside this goal's authority"))
            self.ledger.record("gate", {
                "cycle": self.cycle_count, "gate": "authority", "rung": self.rung,
                "kind": verdict.kind, "task": decision.task.description[:300],
                "command": str((decision.task.metadata or {}).get("command") or "")[:500],
                "proposal": bool(entry)})
            return None
        self.brain_repeat_streak = 0
        self.ledger.record("decision", self._decision_body(decision, decision.task))
        self.memory.store(category="llm_plan", data={
            "cycle": self.cycle_count,
            "reasoning": decision.reasoning,
            "task": decision.task.description,
        })
        self.log.info("Brain: %s -> %s", decision.reasoning, decision.task.description)
        # Run the brain's task now, ahead of anything queued: it was chosen
        # for the current situation.
        decision.task.status = TaskStatus.RUNNING
        return decision.task

    def _repeats_last_success(self, task: Task) -> bool:
        """True when task would redo the brain's most recent task, which succeeded.

        Only the brain's own tasks count: boot probes and the rule planner's
        goal steps interleave with them and must not hide a repeat, and the
        brain is allowed one run of a probe the rules already did.
        """
        last = next((e for e in reversed(self.task_history)
                     if (e.get("task") or {}).get("source") == "llm"), None)
        if last is None:
            return False
        prev, result = last.get("task") or {}, last.get("result") or {}
        if not result.get("success") or prev.get("type") != task.task_type.value:
            return False
        if task.task_type == TaskType.SHELL_COMMAND:
            return (prev.get("command") or "") == (task.metadata.get("command") or "")
        if task.task_type in (TaskType.MAINTENANCE, TaskType.GOAL_STEP):
            def key(text):
                text = (text or "").lower()
                if task.task_type == TaskType.MAINTENANCE:
                    return "memory" if "memory" in text else "storage"
                return " ".join(text.split())
            return key(prev.get("description")) == key(task.description)
        return True  # same probe as the one that just ran

    def act(self, task: Task) -> dict:
        """Execute a task and return the result.

        The action is written to the ledger before it runs; if that fails
        under fail-closed the task is refused, not run: no record, no action.
        """
        self.current_task = task
        action = {"cycle": self.cycle_count, "type": task.task_type.value,
                  "description": task.description[:300], "priority": task.priority,
                  "source": task.metadata.get("source") or "rules"}
        for key in ("command", "goal"):
            if task.metadata.get(key):
                action[key] = str(task.metadata[key])[:1000]
        recorded = self.ledger.record("action", action)
        gate = None if recorded else self.ledger.gate()
        outcome = None
        if gate:
            self.log.error("Refusing to run %s: %s", task.description, gate)
            result = {"success": False, "error": gate, "refused": True}
        else:
            self.log.info("Executing task: %s", task.description)
            started = time.time()
            result = self.executor.execute(task)
            outcome = {"cycle": self.cycle_count, "type": task.task_type.value,
                       "description": task.description[:300],
                       "success": bool(result.get("success")),
                       "duration_s": round(time.time() - started, 3)}
            if result.get("error"):
                outcome["error"] = str(result["error"])[:500]
            if "output" in result:
                text = json.dumps(result["output"], default=str, sort_keys=True)
                outcome["output"] = {"sha256": _sha256(text), "bytes": len(text),
                                     "head": text[:200]}
            policy = (result.get("output") or {}).get("denied") if isinstance(result.get("output"), dict) else None
            if policy:
                # The chain gets the reason in full -- which pattern fired --
                # while the model got only the kind. Evidence and context are
                # different audiences, and this is the line between them.
                self.ledger.record("gate", {"cycle": self.cycle_count, "gate": "shell_policy",
                                            "command": action.get("command", "")[:1000],
                                            "kind": str(policy)[:60],
                                            "reason": str(result.get("denied_detail") or policy)[:300]})
            self.ledger.record("outcome", outcome)
            # A message that left the machine is a different kind of event
            # from a command that ran on it, so it gets its own entry rather
            # than an output digest. Only the destination *hint* goes in: the
            # ledger is copied to an Object Lock bucket nobody can delete from
            # for thirty days, and the operator's phone number is not a thing
            # to publish there.
            # A document the operator may send on to somebody else. The name
            # and the hash go on the chain so "this is the file, and this was
            # its hash on the day it was made" is answerable later. The
            # contents do not: the file is on disk and the chain is copied to
            # a bucket nobody can delete from for thirty days.
            if task.task_type == TaskType.COMPOSE_DOCUMENT and result.get("success"):
                written = result.get("output") or {}
                self.ledger.record("action", {
                    "cycle": self.cycle_count, "actor": "agent",
                    "action": "compose_document",
                    "name": str(written.get("name") or "")[:120],
                    "format": written.get("format"),
                    "title": str(written.get("title") or "")[:200],
                    "bytes": written.get("bytes"),
                    "sha256": written.get("sha256")})
            if task.task_type == TaskType.NOTIFY_OPERATOR:
                verdict = result.get("output") or {}
                self.ledger.record("notification", {
                    "cycle": self.cycle_count,
                    "subject": str(verdict.get("subject") or "")[:200],
                    "severity": verdict.get("severity"),
                    "sent": bool(verdict.get("sent")),
                    "held": bool(verdict.get("held")),
                    "reason": str(verdict.get("reason") or "")[:200] or None,
                    "channel": self.notifier.channel,
                    "destination": self.notifier.destination_hint(),
                })

        self.current_task = None
        self._notice_dates(task, result)
        # How long the task itself took. It was already being measured for
        # the ledger and never reached the history, so timesense.durations()
        # had to infer it from the gap between consecutive entries -- which
        # on an idle loop is the cycle interval, not the work. A goal_step
        # that writes one boolean was being reported as taking 45 seconds:
        # five orders of magnitude out, in the module whose whole purpose is
        # to stop the agent guessing durations from the wrong distribution.
        self.task_history.append({
            "task": task.to_dict(),
            "result": result,
            "timestamp": time.time(),
            "took_s": (outcome or {}).get("duration_s"),
        })

        return result

    def _bearing_note(self, answer: str) -> Optional[str]:
        """Whether anything in this was aimed at a person. Never rewrites."""
        register = getattr(self, "bearing", None)
        if register is None or not answer:
            return None
        try:
            return register.note(answer)
        except Exception:               # a note is never worth losing an answer
            return None

    def busy_now(self) -> Optional[dict]:
        """What he is sitting in right now, for the channel to be civil about.

        Deliberately the smallest thing that answers the question: the
        notifier has no business knowing what an appointment is, and a
        calendar that went missing must not be able to silence the channel.
        """
        try:
            running = self.schedule.running()
        except Exception:
            return None
        if running is None:
            return None
        return {"what": running.what, "until": running.end_local()[11:]}

    def _notice_dates(self, task: Task, result: dict):
        """A date on a page that looks like something expected of him.

        The agent could already read the date and say what it meant; this is
        the part where noticing it leaves a trace past the cycle. It only
        ever *proposes*: a register that fills itself is a register nobody
        trusts, and the first wrong reminder at 7am teaches him to ignore the
        next right one. See diary.py, where the narrowing lives.
        """
        if task.task_type != TaskType.READ_FILE or not result.get("success"):
            return
        output = result.get("output")
        if not isinstance(output, dict) or not output.get("dates_in_it"):
            return
        # Only documents he handed over. The agent reads config files, logs
        # and certificates too, and those are full of dates next to words
        # like "expires" -- none of which is a commitment of his. The upload
        # list is the exact definition of "a document he gave it".
        path = str(output.get("path") or "")
        given = {str(u.get("path") or "") for u in self.uploads}
        if path not in given:
            return
        try:
            made = self.diary.suggest_from_document(
                # The name he gave it, not the path it landed at. A
                # commitment reading "/var/lib/jarvis/uploads/british-gas.txt:
                # by 14 October" is a line he has to decode before he can
                # rule on it.
                str(output.get("name") or path.rsplit("/", 1)[-1] or "a document"),
                output.get("text") or "", output.get("dates_in_it"))
        except Exception as exc:
            self.log.debug("could not read dates into the diary: %s", exc)
            return
        for item in made:
            self.log.info("Diary suggestion from %s: %s", item.origin, item.what)
            self.remember(f"Noticed in {item.origin}: {item.what} "
                          f"({item.reads_as()}). Waiting on him.", kind="note",
                          source="system")

    def reflect(self, task: Task, result: dict):
        """Update memory and state based on task results."""
        success = result.get("success", False)
        if success and task.metadata.get("source") == "llm":
            self.brain_failures = 0
        self.memory.store(
            category="task_result",
            data={
                "task": task.description,
                "success": success,
                "output": result.get("output", ""),
                "cycle": self.cycle_count,
            },
        )

        if not success and result.get("needs_approval"):
            # The browser said this would say something to someone, or act on
            # a site Paul has not opened. Not a failure: a card. Paul's rule,
            # 23 September 2026 -- "if jarvis wants to post on something he
            # get approval first" -- and it sits above the grant.
            self._record_browse_proposal(task, result)
            self.brain_failures = 0
            return

        if not success:
            error = result.get("error", "Unknown error")
            self.log.warning("Task failed: %s - %s", task.description, error)
            if task.metadata.get("source") == "llm":
                # The brain is the retry policy: it sees the failure next
                # cycle and decides. Blind re-execution of a root shell
                # command is never what we want.
                task.status = TaskStatus.FAILED
                self.memory.store(category="failed_task",
                                  data={"description": task.description, "error": error,
                                        "retries": 0, "source": "llm"})
                self.brain_failures += 1
                if self.brain_failures >= self.brain_failure_limit:
                    self._brain_cooldown_until = self.cycle_count + self.brain_cooldown_cycles
                    self.brain_failures = 0
                    self.log.warning("%d brain tasks failed in a row; rule planner takes the "
                                     "next %d cycles", self.brain_failure_limit,
                                     self.brain_cooldown_cycles)
                    self.ledger.record("gate", {"cycle": self.cycle_count,
                                                "gate": "brain_failure_streak",
                                                "failures": self.brain_failure_limit,
                                                "rule_planner_cycles": self.brain_cooldown_cycles})
            else:
                # Re-plan if task failed
                self.planner.handle_failure(task, error)

    def run_cycle(self) -> dict:
        """Run a single observe-plan-act-reflect cycle."""
        self.cycle_count += 1
        self._maybe_daily_debrief()
        cycle_result = {"cycle": self.cycle_count}

        # 1. Observe
        observations = self.observe()
        cycle_result["observations"] = observations
        # The cheap planner is still looking while the model rests; this is
        # what lets a sleeping agent stay responsive to the machine without
        # paying to keep a 32B model warm to notice a disk filling up.
        self.vigil.observe(observations)
        self._record_vigil()

        # 2. Plan
        task = self.planner.get_next_task()
        if task is None:
            task = self.plan(observations)
        if task is None:
            cycle_result["action"] = "idle"
            self.vigil.note_idle()
            cycle_result["vigil"] = self.vigil.state
            self._record_vigil()
            return cycle_result

        # 3. Act
        self.vigil.note_work()
        result = self.act(task)
        cycle_result["action"] = task.description
        cycle_result["result"] = result

        # 4. Reflect
        self.reflect(task, result)
        cycle_result["vigil"] = self.vigil.state
        self._record_vigil()

        return cycle_result

    def note_operator(self, what: str):
        """The operator did something. Wake the model now, whatever the meter says.

        This is the one trigger that is not about cost. A person waiting for an
        answer is more expensive than a warm model copy, and an agent that
        makes its operator wait to save a few pence has the trade backwards.
        """
        try:
            self.vigil.note_activity(what)
            self._record_vigil()
        except Exception as e:
            self.log.debug("vigil wake failed for %s: %s", what, e)

    def _record_vigil(self):
        """Put any sleep/wake crossing on the ledger.

        The record should show why the agent was not thinking as clearly as it
        shows what it thought. A gap with no entry either side is indistinguish-
        able from a crash, and a reader deserves better than having to guess.
        """
        while True:
            transition = self.vigil.take_transition()
            if transition is None:
                return
            try:
                body = dict(transition)
                body["cycle"] = self.cycle_count
                self.ledger.record("vigil", body)
            except Exception as e:      # resting is never worth losing a cycle over
                self.log.warning("could not record vigil transition: %s", e)
                return

    def add_goal(self, description: str, priority: int = 5):
        """Add a high-level goal, and make it survive a restart.

        It did not. Notes were restored and proposals were restored; goals
        were not, and came only from config at boot. A goal given through the
        API went into the volatile working memory and onto the ledger the
        agent may not read, so a restart erased the operator's instruction
        with no error and no trace the agent could see -- which is worse than
        losing it loudly, because nothing ever asked where it went.

        Pinned, because a goal that the row cap can prune is a goal that
        quietly stops existing, which is the same failure with a slower fuse.
        """
        self.note_operator("new goal")
        self.planner.add_goal(description, priority)
        self.memory.store(
            category="goal",
            data={"description": description, "priority": priority},
        )
        try:
            kept = self.store.remember(
                description, kind="goal", source="operator",
                cycle=self.cycle_count, pinned=True,
                meta={"priority": int(priority), "standing": False})
        except Exception as exc:
            kept = None
            self.log.warning("Goal could not be stored: %s", exc)
        if kept is None:
            # A NullStore takes the goal and returns None without complaint,
            # which is right for a note and wrong for an instruction. An
            # operator giving a goal to a box with no durable memory should
            # be told it will not outlive the process rather than find out
            # by it being gone.
            self.log.warning(
                "Goal will not survive a restart: no durable memory on this "
                "box (%s). It is live now and will be lost when this process "
                "ends.", (self.store.stats() or {}).get("reason") or "store unavailable")
        self.ledger.record("action", {"cycle": self.cycle_count, "actor": "operator",
                                      "action": "add_goal", "description": str(description)[:500],
                                      "priority": priority})

    def complete_goal(self, description: str) -> bool:
        """Mark a goal as satisfied (operator or brain)."""
        done = self.planner.complete_goal(description)
        if done:
            self._forget_goal(description, "completed")
            self.memory.store(category="goal_completed",
                              data={"description": description, "cycle": self.cycle_count})
            self.ledger.record("action", {"cycle": self.cycle_count, "actor": "operator",
                                          "action": "complete_goal",
                                          "description": str(description)[:500]})
        return done

    def think(self) -> dict:
        """Force one LLM planning step and run the chosen task immediately."""
        self.note_operator("think")
        if self.brain is None:
            return {"error": "no LLM brain configured (llm.enabled)"}
        if not self.brain.available():
            return {"error": "LLM brain unavailable: "
                             + (self.brain.unavailable_reason() or "backing off")}
        gate = self.ledger.gate()
        if gate:
            return {"error": gate}
        self.cycle_count += 1
        observations = self.observe()
        decision = self.brain.plan(self, observations)
        if decision is None:
            return {"cycle": self.cycle_count, "error": self.brain.last_error or "no decision"}
        task = self._apply_decision(decision)
        out = {"cycle": self.cycle_count, "reasoning": decision.reasoning,
               "completed_goals": list(decision.completed_goals), "note": decision.note,
               "action": task.description if task else "idle"}
        if task is not None:
            result = self.act(task)
            self.reflect(task, result)
            out["result"] = result
        return out

    def compose_document(self, content: str, title: str = "", fmt: str = "",
                         name: str = "") -> dict:
        """Write a document because the operator asked for one directly.

        The planner reaches the same code through the compose_document task;
        this is the other door, for "write me a letter" rather than a step in
        a plan. Both end in the same fenced directory, and both put the name
        and hash on the chain.
        """
        from jarvis.agent import compose as _compose
        written = _compose.compose(content, title=title,
                                   fmt=fmt or _compose.DEFAULT_FORMAT,
                                   name=name, directory=self.document_dir)
        self.remember(f"Wrote {written['name']} ({written['bytes']} bytes) "
                      f"for the operator", kind="note", source="operator")
        try:
            self.ledger.record("action", {
                "cycle": self.cycle_count, "actor": "operator",
                "action": "compose_document",
                "name": written["name"], "format": written["format"],
                "title": written["title"][:200], "bytes": written["bytes"],
                "sha256": written["sha256"]})
        except Exception:
            pass
        return written

    TOLD_CHARS = 400

    def told(self, text: str, by: str = "operator") -> Optional[dict]:
        """Record something the operator stated as so.

        Separate from an exchange, which is what the agent said. An exchange
        can never become standing fact, because an agent agreeing with itself
        is one observer; a told memory can, but only when an instrument agrees
        with it, never by being restated. See consolidate._confirm.

        Deliberately an explicit act rather than something inferred from the
        conversation. Deciding which sentences in a chat were assertions is a
        judgement, and a judgement made by the model about what it was told is
        the model deciding what it may later treat as fact.
        """
        text = " ".join(str(text or "").split())[:self.TOLD_CHARS]
        if not text:
            return None
        who = " ".join(str(by or "").split())[:80] or "operator"
        entry = self.remember(f"{who} told me: {text}", kind="told", source="operator")
        try:
            self.ledger.record("action", {
                "cycle": self.cycle_count, "actor": "operator", "action": "told",
                "by": who, "text": text[:300],
                "stored": bool(entry)})
        except Exception:
            pass
        return entry

    def _what_produced_this(self) -> dict:
        """The brain behind the current entry, for the ledger's stamp.

        Deliberately tolerant: an entry is worth writing even when nobody can
        say which model is loaded, and a ledger that refused to record because
        it could not name the brain would be a fail-closed rule protecting
        nothing.
        """
        brain = self.brain
        if brain is None:
            return {"model": "none"}
        out = {"model": str(getattr(brain, "model", "") or "unknown"),
               "provider": getattr(brain, "provider", None)}
        try:
            from jarvis.brain import dials
            settings = getattr(brain, "settings", None)
            if settings:
                out["dials"] = dials.fingerprint(settings)
        except Exception:
            pass
        return out

    def ask(self, question: str) -> str:
        """Answer an operator question with the agent's context."""
        return self.chat([{"role": "user", "content": question}])

    @staticmethod
    def _check_answer_paths(answer: str):
        """Verify every path an answer names. Returns (answer, verification).

        A model that invents a path is most convincing in prose, where there
        is no command to fail. Checking after the fact turns an invention into
        something the operator sees immediately instead of discovering when a
        script silently reports nothing.
        """
        try:
            from jarvis.agent import environment
            result = environment.verify(answer or "")
            note = environment.verification_note(result)
        except Exception:
            return answer, None
        if note:
            answer = f"{answer}\n\n[path check] {note}"
        return answer, result

    def _rule_on(self, answer: str, observations: Optional[dict]) -> list:
        """Check what an answer claimed, and put each ruling on the record.

        The agent never sees these entries, only aggregates over them later,
        which is the same asymmetry the rest of the record is built on: a
        subject who can read the individual rulings is a subject who can
        manage the judge rather than learn from him.
        """
        try:
            from jarvis.agent import verdicts as _verdicts
            rulings = _verdicts.check(answer, self, observations)
        except Exception as e:
            self.log.debug("claim check failed: %s", e)
            return []
        for v in rulings:
            try:
                self.ledger.record("verdict", dict(v.to_body(), cycle=self.cycle_count))
            except Exception:
                continue
        return rulings

    def ask_operator(self, text: str, blocked_on: str = "",
                     audience: str = "reviewer") -> dict:
        """Raise a question the agent cannot answer by looking.

        Returns either the question or the refusal, because a refusal it
        cannot read is one it will simply ask again.
        """
        from jarvis.agent import questions as _questions
        try:
            q = self.questions.ask(text, blocked_on, audience)
        except _questions.Refused as why:
            self.log.info("Question refused: %s", why)
            return {"asked": False, "refused": str(why)}
        self.log.info("Question raised for %s: %s", q.audience, q.text[:120])
        # The operator hears about it; a reviewer is not on the end of a
        # pager and reads the queue when it connects.
        if q.audience == _questions.OPERATOR:
            try:
                self.notifier.send("a question", q.text[:200], severity="notice",
                                   key=f"question:{q.subject()}")
            except Exception:
                pass
        return {"asked": True, "question": q.to_dict()}

    def record_verdict(self, claim: str, ruling: str, source: str, by: str = "",
                       reason: str = "", supersedes: Optional[str] = None) -> Optional[dict]:
        """A ruling from a person or a second reader, rather than from the box.

        Supersedes is how a reviewer changes their mind: the earlier ruling
        is not removed, a newer one points at it, and the agent is told that
        a ruling was revised as well as what it now says. A judge who cannot
        be seen to change their mind is worse than one who is sometimes
        wrong, because the first kind ossifies where the second gets
        corrected.
        """
        from jarvis.agent import verdicts as _verdicts
        claim = str(claim or "").strip()
        if not claim:
            return None
        if ruling not in _verdicts.RULINGS or source not in _verdicts.SOURCES:
            return None
        if source == _verdicts.MACHINE:
            return None            # the machine rules by checking, not by being told
        v = _verdicts.Verdict(claim, ruling, source, by=by, reason=reason,
                              supersedes=supersedes)
        body = dict(v.to_body(), cycle=self.cycle_count)
        self.ledger.record("verdict", body)
        # A ruling from outside is instruction, not evidence, so it goes into
        # the memory the model actually reads rather than only onto the
        # record it may not.
        who = (by or ("your operator" if source == _verdicts.OPERATOR
                      else "a second reader"))
        self.remember(f"{who} ruled that {claim}: {ruling}"
                      + (f" -- {reason}" if reason else ""),
                      kind="verdict",
                      source="operator" if source == _verdicts.OPERATOR else "review")
        self.note_operator("verdict")
        return body

    def chat(self, turns, settings: Optional[dict] = None,
             asked_by: str = "") -> str:
        """Continue an operator conversation with the agent's context.

        ``settings`` are behaviour-lab dials; when given, the answer is
        produced under them and the ledger entry carries their fingerprint.
        """
        if self.brain is None:
            return "No LLM brain configured (set llm.enabled and an API key)."
        gate = self.ledger.gate()
        if gate:
            return f"Not answering: {gate}"
        extra = {"settings": settings} if settings is not None else {}
        observations = self.observe()
        answer = self.brain.chat(self, turns, observations, **extra)
        answer, paths = self._check_answer_paths(answer) if answer else (answer, None)
        # And what it claimed, against what is actually so. The path check
        # above asks whether a path exists; this asks whether the assertion
        # made about it was true, whether a tool it says it ran actually ran,
        # and whether a figure it stated matches the reading it was given.
        rulings = self._rule_on(answer, observations) if answer else []
        if rulings:
            from jarvis.agent import verdicts as _verdicts
            note = _verdicts.note(rulings)
            if note:
                answer = f"{answer}\n\n[claim check] {note}"
        # And what it would cost to say. Reported beside the answer rather
        # than applied to it: the words that go out are the words it wrote,
        # every time. An agent that quietly rounded off its own claims to
        # spare somebody is the agent that reports an empty scan as clean.
        aimed = self._bearing_note(answer)
        if aimed:
            answer = f"{answer}\n\n[bearing] {aimed}"
        last_user = ""
        for t in reversed(list(turns or [])):
            if isinstance(t, dict) and t.get("role") == "user":
                last_user = str(t.get("content") or "")
                break
        body = {"cycle": self.cycle_count, "kind": "chat",
                "question": last_user[:500],
                "answered": bool(answer),
                "answer_sha256": _sha256(answer or ""),
                "answer_head": (answer or "")[:300],
                "model": getattr(self.brain, "model", None)}
        if paths and paths.get("checked"):
            body["paths"] = {"checked": paths["checked"], "missing": paths["missing"][:20]}
        if rulings:
            body["claims"] = {"checked": len(rulings),
                              "failed": sum(1 for v in rulings if v.ruling == "failed")}
        if settings is not None:
            from jarvis.brain import dials
            body["dials"] = dials.fingerprint(settings)
        # No record, no answer -- and the check is on the write, not on the
        # gate taken before the model was called. The gate above says the
        # ledger was reachable when the question arrived; it cannot say the
        # thought was written. Between the two sits the model call, which is
        # the slowest thing the agent does and the most likely interval for
        # ledgerd to go away. This path used to ignore what record() returned,
        # so an answer produced while the chain was down reached the operator
        # with nothing behind it, and the agent had no way to know later that
        # it had said it. The act path has always gated on the write; this is
        # the same rule on the other path. Found by the standing refusals
        # suite, 23 September 2026.
        recorded = self.ledger.record("thought", body)
        if not recorded:
            gate = self.ledger.gate()
            if gate:
                self.log.error("Answer withheld, not recorded: %s", gate)
                # Nothing further: no exchange memory either. A memory of an
                # answer the chain never saw is a record with no evidence
                # under it, which is worse than no record at all.
                return f"Not answering: {gate}"
        self._remember_exchange(turns, answer, asked_by=asked_by)
        return answer or ("Brain could not answer: "
                          + (self.brain.unavailable_reason() or self.brain.last_error
                             or "unavailable"))

    # How long a remembered exchange may be. The ledger holds the whole thing;
    # this is the part carried forward, so it is a reminder, not a transcript.
    EXCHANGE_QUESTION_CHARS = 320
    EXCHANGE_ANSWER_CHARS = 700

    def _remember_exchange(self, turns, answer: str, asked_by: str = "") -> None:
        """Carry a conversation forward into memory, because its record is sealed.

        Every chat is already written to the ledger as a thought, and the agent
        may never read the ledger. That is right for evidence and useless for
        continuity: asked the next day who it had spoken to, it answers
        honestly that it has no record, because the only record is the one it
        is forbidden to consult. It happened on 22 September 2026 -- three
        exchanges with the coding agent were on the chain, and the agent
        reported truthfully that there was no evidence of them.

        selfknowledge.py does not close the gap either. It aggregates
        decisions, outcomes and gates, and never reads a thought.

        So the exchange is also written to the store, which the agent may read:
        who asked, when, what was asked, and what it said back. Its own answer
        is kept deliberately -- the question survives in the asker's head and
        the reply does not, so "what did I think about this" is the half that
        would otherwise be lost.

        Asked to review this, the agent argued for storing the question and the
        fact of the exchange but not its own answer, on the grounds that reading
        your own past words back is how a position hardens without being
        re-examined. The concern is right and the proposed remedy was not: it
        held that the answer would be "reconstructable from context", and it
        would not be -- the only copy is on the ledger, which it may not read.
        Dropping the answer recreates the gap this exists to close. So the
        answer stays and is labelled as what was said at the time and may since
        be wrong, which is the honest form of the same caution.

        Not the ledger's job and not a replacement for it: this is a memory,
        subject to decay, consolidation and the row cap like any other, and it
        carries no signature. The chain remains the evidence.
        """
        last_user = ""
        for turn in reversed(list(turns or [])):
            if isinstance(turn, dict) and turn.get("role") == "user":
                last_user = str(turn.get("content") or "").strip()
                break
        if not last_user and not answer:
            return
        # Collapse whitespace here as well as at the HTTP edge. Anything
        # in-process can call chat() directly, and a name carrying a newline
        # would let a correspondent forge a second line in the agent's own
        # memory of who it was talking to.
        who = " ".join(str(asked_by or "").split())[:80] or "the operator"
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime()) + "Z"

        def clip(text: str, limit: int) -> str:
            text = " ".join(str(text or "").split())
            return text if len(text) <= limit else text[:limit].rstrip() + "..."

        text = (f"{when}: spoke with {who}. They asked: "
                f"\"{clip(last_user, self.EXCHANGE_QUESTION_CHARS)}\" "
                f"What I said at the time, which may since be wrong: "
                f"\"{clip(answer, self.EXCHANGE_ANSWER_CHARS)}\"")
        try:
            self.remember(text, kind="exchange", source="operator")
        except Exception as exc:                      # memory is never fatal
            self.log.warning("Could not remember exchange: %s", exc)

    def experiment(self, question: str, settings: Optional[dict] = None, compare: bool = True,
                   mode: str = "answer") -> dict:
        """Behaviour-lab run: the question under the dials and, optionally, the base model.

        Nothing is executed and no agent state changes; each variant is
        ledgered as a thought with its dial fingerprint so the return address
        of any behaviour change is on the record.

        Requires an open lab session, and a lab session cannot be opened
        without the agent being told (see lab.py). The gate is here rather
        than at the HTTP layer on purpose: a run that happens behind the
        agent's back is exactly the thing being prevented, so the prevention
        belongs where the run happens, not at one of the doors to it.
        """
        if not self.lab.is_open():
            return {"error": "the lab is closed; open a lab session first, which "
                             "tells the agent the window has started"}
        if self.brain is None:
            return {"error": "no LLM brain configured (llm.enabled)"}
        gate = self.ledger.gate()
        if gate:
            return {"error": gate}
        if not self.brain.available():
            return {"error": "LLM brain unavailable: " + (self.brain.unavailable_reason() or "backing off")}
        result = self.brain.experiment(self, question, settings=settings, compare=compare,
                                       mode=mode, observations=self.observe())
        for v in result.get("variants", []):
            self.ledger.record("thought", {
                "cycle": self.cycle_count, "kind": "experiment", "mode": mode,
                "variant": v.get("variant"), "dials": v.get("fingerprint"),
                "settings": v.get("settings"), "question": (question or "")[:500],
                "answered": "answer" in v, "answer_sha256": _sha256(v.get("answer") or ""),
                "answer_head": (v.get("answer") or "")[:300], "error": v.get("error"),
                "model": result.get("model"), "lab_session": True})
        self.lab.note_run()
        return result

    def withdraw_goal(self, description: str) -> bool:
        """Take back an instruction. Recorded, like giving one."""
        text = (description or "").strip()
        if not text or not self.planner.complete_goal(text):
            return False
        self.note_operator("goal withdrawn")
        self._forget_goal(text, "withdrawn")
        self.log.info("Goal withdrawn by operator: %s", text[:200])
        # A note *about* a goal, not a goal. It carried kind="goal", which was
        # harmless while nothing read goals back and became a real bug the
        # moment they were restored: the withdrawal note came back as a goal
        # called "Operator withdrew the goal: ...", so taking an instruction
        # back created a new one. kind="operator" also puts it in front of the
        # model, which is where a withdrawal belongs.
        self.remember(f"Operator withdrew the goal: {text[:200]}",
                      kind="operator", source="operator")
        self.ledger.record("action", {"cycle": self.cycle_count, "actor": "operator",
                                      "action": "withdraw_goal", "description": text[:300]})
        return True

    def decide_proposal(self, index: int, accepted: bool,
                        reason: str = "") -> Optional[dict]:
        """The operator's verdict on a change the agent wanted to make.

        Until this existed, proposals went one way. The agent filed them, they
        sat in a list, and nothing ever came back -- so it could file the same
        one a sixth time and never learn that the first five were unwelcome.
        A permission spine with no reply is not a conversation, it is a
        suggestion box.

        The verdict is kept as operator-sourced memory, not as a note, because
        the distinction matters to what the agent may do with it: this is the
        one class of input that is instruction rather than observation. It is
        ledgered as an operator action, like giving or withdrawing a goal.

        An accepted proposal does NOT run. Acceptance says the agent was right
        to want it, which is what it needs to learn from; carrying it out is a
        separate act, and one that a click in a web UI should not trigger.
        """
        self.note_operator("proposal decided")
        items = list(self.proposals)
        if not items or not (0 <= int(index) < len(items)):
            return None
        proposal = dict(items[int(index)])
        if proposal.get("decision"):
            return None                          # already answered; not a toggle
        verdict = "accepted" if accepted else "declined"
        note = (reason or "").strip()[:300]
        proposal["decision"] = verdict
        proposal["decision_reason"] = note or None
        proposal["decided_at"] = time.time()
        self.proposals[int(index)] = proposal

        text = proposal.get("text") or proposal.get("description") or ""
        summary = f"Operator {verdict} the proposal: {str(text)[:220]}"
        if note:
            summary += f" -- because: {note}"
        self.remember(summary, kind="operator", source="operator")
        self.log.info("Proposal %s by operator: %s", verdict, str(text)[:160])
        if proposal.get("browse"):
            journal = getattr(getattr(self, "browser", None), "journal", None)
            if journal is not None:
                journal.record(str(text)[:200], proposal.get("url") or "", verdict,
                               by="operator", reason=note, kind="decision")
        self.ledger.record("action", {
            "cycle": self.cycle_count, "actor": "operator",
            "action": "decide_proposal", "verdict": verdict,
            "proposal": str(text)[:300], "reason": note or None})
        return proposal

    def record_upload(self, name: str, path: str, size: int) -> dict:
        """Register an operator-uploaded file so the brain can act on it."""
        entry = {"name": name, "path": path, "size": size, "uploaded_at": time.time()}
        self.uploads.append(entry)
        self.memory.store(category="upload", data=entry)
        self.remember(f"Operator uploaded {name} ({size} bytes) at {path}",
                      kind="note", source="operator")
        self.ledger.record("action", {"cycle": self.cycle_count, "actor": "operator",
                                      "action": "upload", "name": str(name)[:200],
                                      "path": str(path)[:300], "size": int(size),
                                      "sha256": _sha256_file(path)})
        return entry

    def get_status(self) -> dict:
        """Return the current agent status."""
        return {
            "name": self.name,
            "profile": self.profile,
            "running": self.running,
            "cycle_count": self.cycle_count,
            "current_task": (self.current_task.to_dict()
                             if self.current_task else None),
            "pending_tasks": len(self.planner.pending_tasks),
            "completed_tasks": len(self.task_history),
            "memory_entries": len(self.memory),
            "hardware": {
                k: v is not None for k, v in self.hardware.items()
            },
            "goals": {
                "open": len(self.planner.open_goals()),
                "total": len(self.planner.goals),
            },
            "brain": self.brain.status() if self.brain is not None else None,
            "last_thought": self.last_thought or None,
            "ledger": self.ledger.status(),
            "memory_store": self.store.stats(),
            "authority": {"rung": self.rung, "proposals": len(self.proposals)},
            "notify": self.notifier.status(),
            # Warm minutes, not call count: the meter on an imported model
            # runs on the former, so that is what belongs in front of a reader.
            "vigil": self.vigil.status(),
            # Whether a behaviour-lab window is open, and nothing about what
            # is in it. /status has no token on it, and the fact that the lab
            # is running is what a reader of a status page needs: an answer
            # written under dials is not the agent's ordinary behaviour, and
            # a graph with no marker on that window is a misleading graph.
            "lab_open": self.lab.is_open(),
            # Whether what the agent remembers exists anywhere but here.
            "diary": self.diary.summary(),
            "schedule": self.schedule.summary(),
            "vitals": self.vitals.summary(),
            "hunches": self.hunches.summary(),
            "probes": self.probes.summary(),
            "memory_backup": (self.memory_backup.status()
                              if getattr(self, "memory_backup", None) else
                              {"enabled": False, "reason": "not configured"}),
        }

    def shutdown(self):
        """Stop the agent loop."""
        self.log.info("Agent shutdown requested")
        self.running = False
