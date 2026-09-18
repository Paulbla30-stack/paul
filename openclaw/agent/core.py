"""
OpenClaw Agent Core

The central autonomous agent that plans tasks, executes them, and
maintains state/memory across interactions.
"""

import time
import logging
from typing import Any, Optional

from openclaw.agent.planner import TaskPlanner, Task
from openclaw.agent.executor import TaskExecutor
from openclaw.agent.memory import AgentMemory


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
                 brain=None, shell_policy: Optional[dict] = None):
        self.config = config
        self.hardware = hardware
        self.log = logger.getChild("agent")
        self.name = config.get("name", "OpenClaw")
        self.max_tasks = config.get("max_tasks", 100)
        self.profile = config.get("profile", "bare-metal")

        # Sub-components
        self.memory = AgentMemory(max_entries=1000)
        self.planner = TaskPlanner(self.memory, profile=self.profile)
        self.executor = TaskExecutor(hardware, self.memory, self.log,
                                     shell_policy=shell_policy)
        # Optional LLM planner (openclaw.brain.ClaudeBrain). When present it
        # is consulted before the rule-based planner; when it cannot answer
        # the rule-based planner takes over for that cycle.
        self.brain = brain
        self.last_thought: dict = {}

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

        if self.brain is not None and self.brain.should_plan(self.cycle_count):
            decision = self.brain.plan(self, observations)
            if decision is not None:
                return self._apply_decision(decision)

        task = self.planner.generate_task(observations)
        if task:
            self.log.debug("Planned task: %s (priority=%d)",
                           task.description, task.priority)
        return task

    def _apply_decision(self, decision) -> Optional[Task]:
        """Turn a brain Decision into agent state: goals, notes, task."""
        for goal in decision.completed_goals:
            if self.planner.complete_goal(goal):
                self.log.info("Goal completed: %s", goal)
                self.memory.store(category="goal_completed",
                                  data={"description": goal, "cycle": self.cycle_count})
        if decision.note:
            self.memory.store(category="llm_note",
                              data={"note": decision.note, "cycle": self.cycle_count})
        self.last_thought = {
            "cycle": self.cycle_count,
            "reasoning": decision.reasoning,
            "task": decision.task.to_dict() if decision.task else None,
            "completed_goals": list(decision.completed_goals),
            "timestamp": time.time(),
        }
        self.memory.store(category="llm_plan", data={
            "cycle": self.cycle_count,
            "reasoning": decision.reasoning,
            "task": decision.task.description if decision.task else "idle",
        })
        if decision.task is None:
            self.log.info("Brain: idle. %s", decision.reasoning)
            return None
        self.log.info("Brain: %s -> %s", decision.reasoning, decision.task.description)
        self.planner.add_task(decision.task)
        # Return it through the queue so status/priority bookkeeping matches
        # every other task.
        return self.planner.get_next_task()

    def act(self, task: Task) -> dict:
        """Execute a task and return the result."""
        self.current_task = task
        self.log.info("Executing task: %s", task.description)

        result = self.executor.execute(task)

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

    def complete_goal(self, description: str) -> bool:
        """Mark a goal as satisfied (operator or brain)."""
        done = self.planner.complete_goal(description)
        if done:
            self.memory.store(category="goal_completed",
                              data={"description": description, "cycle": self.cycle_count})
        return done

    def think(self) -> dict:
        """Force one LLM planning step and run the chosen task immediately."""
        if self.brain is None:
            return {"error": "no LLM brain configured (llm.enabled)"}
        if not self.brain.available():
            return {"error": "LLM brain unavailable: "
                             + (self.brain.last_error or self.brain.status().get("disabled_reason")
                                or "backing off")}
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
        if self.brain is None:
            return "No LLM brain configured (set llm.enabled and an API key)."
        answer = self.brain.ask(self, question, self.observe())
        return answer or ("Brain could not answer: "
                          + (self.brain.last_error or "unavailable"))

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
        }

    def shutdown(self):
        """Stop the agent loop."""
        self.log.info("Agent shutdown requested")
        self.running = False
