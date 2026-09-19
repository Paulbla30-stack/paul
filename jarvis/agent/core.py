"""
Jarvis Agent Core

The central autonomous agent that plans tasks, executes them, and
maintains state/memory across interactions.
"""

import time
import hashlib
import json
import logging
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
                 store=None, notifier=None):
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
        self.executor = TaskExecutor(hardware, self.memory, self.log,
                                     shell_policy=shell_policy,
                                     notifier=self.notifier)
        # Optional LLM planner (jarvis.brain.ClaudeBrain). When present it
        # is consulted before the rule-based planner; when it cannot answer
        # the rule-based planner takes over for that cycle.
        self.brain = brain
        self.last_thought: dict = {}
        # The Glass Ledger: a signed, hash-chained journal of every decision,
        # action and outcome. With fail_closed the agent does not act, plan
        # or answer while it cannot record. The brain never reads it.
        self.ledger = ledger if ledger is not None else NullLedger()
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
        self.notes = deque(maxlen=config.get("notes_limit", 20))
        self._restore_notes()
        # Files handed to the agent through the UI / API; the brain sees them.
        self.uploads = deque(maxlen=50)
        # The permission spine: what this agent is allowed to *be*, above the
        # deny-list's floor of what it may never run. Default is propose, so a
        # change becomes a card for the operator rather than an action.
        from jarvis.agent import authority as _authority
        self.rung = _authority.normalise_rung(config.get("rung"))
        self.executor.rung = self.rung
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
        """Refill the note cache from durable memory after a restart."""
        try:
            recovered = [m["text"] for m in
                         reversed(self.store.recent(self.notes.maxlen, kind="note"))]
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
        if kind == "note":
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
        verdict = authority.review(decision.task, self.rung)
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
                self.ledger.record("gate", {"cycle": self.cycle_count, "gate": "shell_policy",
                                            "command": action.get("command", "")[:1000],
                                            "reason": str(policy)[:300]})
            self.ledger.record("outcome", outcome)
            # A message that left the machine is a different kind of event
            # from a command that ran on it, so it gets its own entry rather
            # than an output digest. Only the destination *hint* goes in: the
            # ledger is copied to an Object Lock bucket nobody can delete from
            # for thirty days, and the operator's phone number is not a thing
            # to publish there.
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
        self.task_history.append({
            "task": task.to_dict(),
            "result": result,
            "timestamp": time.time(),
        })

        return result

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
        cycle_result = {"cycle": self.cycle_count}

        # 1. Observe
        observations = self.observe()
        cycle_result["observations"] = observations

        # 2. Plan
        task = self.planner.get_next_task()
        if task is None:
            task = self.plan(observations)
        if task is None:
            cycle_result["action"] = "idle"
            return cycle_result

        # 3. Act
        result = self.act(task)
        cycle_result["action"] = task.description
        cycle_result["result"] = result

        # 4. Reflect
        self.reflect(task, result)

        return cycle_result

    def add_goal(self, description: str, priority: int = 5):
        """Add a high-level goal for the agent to work toward."""
        self.planner.add_goal(description, priority)
        self.memory.store(
            category="goal",
            data={"description": description, "priority": priority},
        )
        self.ledger.record("action", {"cycle": self.cycle_count, "actor": "operator",
                                      "action": "add_goal", "description": str(description)[:500],
                                      "priority": priority})

    def complete_goal(self, description: str) -> bool:
        """Mark a goal as satisfied (operator or brain)."""
        done = self.planner.complete_goal(description)
        if done:
            self.memory.store(category="goal_completed",
                              data={"description": description, "cycle": self.cycle_count})
            self.ledger.record("action", {"cycle": self.cycle_count, "actor": "operator",
                                          "action": "complete_goal",
                                          "description": str(description)[:500]})
        return done

    def think(self) -> dict:
        """Force one LLM planning step and run the chosen task immediately."""
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

    def chat(self, turns, settings: Optional[dict] = None) -> str:
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
        answer = self.brain.chat(self, turns, self.observe(), **extra)
        answer, paths = self._check_answer_paths(answer) if answer else (answer, None)
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
        if settings is not None:
            from jarvis.brain import dials
            body["dials"] = dials.fingerprint(settings)
        self.ledger.record("thought", body)
        return answer or ("Brain could not answer: "
                          + (self.brain.unavailable_reason() or self.brain.last_error
                             or "unavailable"))

    def experiment(self, question: str, settings: Optional[dict] = None, compare: bool = True,
                   mode: str = "answer") -> dict:
        """Behaviour-lab run: the question under the dials and, optionally, the base model.

        Nothing is executed and no agent state changes; each variant is
        ledgered as a thought with its dial fingerprint so the return address
        of any behaviour change is on the record.
        """
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
                "model": result.get("model")})
        return result

    def withdraw_goal(self, description: str) -> bool:
        """Take back an instruction. Recorded, like giving one."""
        text = (description or "").strip()
        if not text or not self.planner.complete_goal(text):
            return False
        self.log.info("Goal withdrawn by operator: %s", text[:200])
        self.remember(f"Operator withdrew the goal: {text[:200]}",
                      kind="goal", source="operator")
        self.ledger.record("action", {"cycle": self.cycle_count, "actor": "operator",
                                      "action": "withdraw_goal", "description": text[:300]})
        return True

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
        }

    def shutdown(self):
        """Stop the agent loop."""
        self.log.info("Agent shutdown requested")
        self.running = False
