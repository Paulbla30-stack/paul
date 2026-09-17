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

    def __init__(self, config: dict, hardware: dict, logger: logging.Logger):
        self.config = config
        self.hardware = hardware
        self.log = logger.getChild("agent")
        self.name = config.get("name", "OpenClaw")
        self.max_tasks = config.get("max_tasks", 100)
        self.profile = config.get("profile", "bare-metal")

        # Sub-components
        self.memory = AgentMemory(max_entries=1000)
        self.planner = TaskPlanner(self.memory, profile=self.profile)
        self.executor = TaskExecutor(hardware, self.memory, self.log)

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
        """Generate a task plan based on current observations."""
        if len(self.planner.pending_tasks) >= self.max_tasks:
            self.log.warning("Task queue full (%d tasks), skipping planning",
                             self.max_tasks)
            return None

        task = self.planner.generate_task(observations)
        if task:
            self.log.debug("Planned task: %s (priority=%d)",
                           task.description, task.priority)
        return task

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
        }

    def shutdown(self):
        """Stop the agent loop."""
        self.log.info("Agent shutdown requested")
        self.running = False
