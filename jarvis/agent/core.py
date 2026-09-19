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
                 brain=None, shell_policy: Optional[dict] = None, ledger=None):
        self.config = config
        self.hardware = hardware
        self.log = logger.getChild("agent")
        self.name = config.get("name", "Jarvis")
        self.max_tasks = config.get("max_tasks", 100)
        self.profile = config.get("profile", "bare-metal")

        # Sub-components
        self.memory = AgentMemory(max_entries=1000)
        self.planner = TaskPlanner(self.memory, profile=self.profile)
        self.executor = TaskExecutor(hardware, self.memory, self.log,
                                     shell_policy=shell_policy)
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
        # Brain notes live here, not in the evictable AgentMemory, so they
        # survive however busy the loop gets.
        self.notes = deque(maxlen=config.get("notes_limit", 20))
        # Files handed to the agent through the UI / API; the brain sees them.
        self.uploads = deque(maxlen=50)

        # State
        self.running = False
        self.cycle_count = 0
        self.current_task: Optional[Task] = None
        self.task_history: list[dict] = []

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

    def _apply_decision(self, decision) -> Optional[Task]:
        """Turn a brain Decision into agent state: goals, notes, task."""
        for goal in decision.completed_goals:
            if self.planner.complete_goal(goal):
                self.log.info("Goal completed: %s", goal)
                self.memory.store(category="goal_completed",
                                  data={"description": goal, "cycle": self.cycle_count})
        if decision.note:
            self.notes.append(decision.note)
            self.memory.store(category="llm_note",
                              data={"note": decision.note, "cycle": self.cycle_count})
        self.last_thought = {
            "cycle": self.cycle_count,
            "reasoning": decision.reasoning,
            "task": decision.task.to_dict() if decision.task else None,
            "completed_goals": list(decision.completed_goals),
            "timestamp": time.time(),
        }
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
        answer = self.brain.chat(self, turns, self.observe(), settings=settings)
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

    def record_upload(self, name: str, path: str, size: int) -> dict:
        """Register an operator-uploaded file so the brain can act on it."""
        entry = {"name": name, "path": path, "size": size, "uploaded_at": time.time()}
        self.uploads.append(entry)
        self.memory.store(category="upload", data=entry)
        self.notes.append(f"Operator uploaded {name} ({size} bytes) at {path}")
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
        }

    def shutdown(self):
        """Stop the agent loop."""
        self.log.info("Agent shutdown requested")
        self.running = False
