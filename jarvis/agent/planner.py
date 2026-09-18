"""
Jarvis Task Planner

Manages task creation, prioritization, and scheduling for the agent.
"""

import time
import heapq
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskType(Enum):
    SYSTEM_CHECK = "system_check"
    HARDWARE_PROBE = "hardware_probe"
    SECURITY_SCAN = "security_scan"
    USER_COMMAND = "user_command"
    MAINTENANCE = "maintenance"
    OBSERVATION = "observation"
    GOAL_STEP = "goal_step"
    CLOUD_PROBE = "cloud_probe"
    SHELL_COMMAND = "shell_command"


# Boot-task profiles: what the agent should look at first depends on
# where it is running.  "bare-metal" is the bootable ISO with a screen
# and keyboard; "cloud" is a headless EC2 instance.
PROFILES = ("bare-metal", "cloud")


@dataclass(order=True)
class Task:
    """A single executable task for the agent."""
    priority: int
    description: str = field(compare=False)
    task_type: TaskType = field(compare=False, default=TaskType.SYSTEM_CHECK)
    status: TaskStatus = field(compare=False, default=TaskStatus.PENDING)
    created_at: float = field(compare=False, default_factory=time.time)
    metadata: dict = field(compare=False, default_factory=dict)
    subtasks: list = field(compare=False, default_factory=list)
    retry_count: int = field(compare=False, default=0)
    max_retries: int = field(compare=False, default=3)

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "type": self.task_type.value,
            "status": self.status.value,
            "priority": self.priority,
            "created_at": self.created_at,
            "retry_count": self.retry_count,
            **({"command": self.metadata["command"]} if self.metadata.get("command") else {}),
            **({"source": self.metadata["source"]} if self.metadata.get("source") else {}),
        }


class TaskPlanner:
    """
    Plans and schedules tasks for the agent to execute.

    Uses a priority queue to manage tasks, with lower priority numbers
    executed first (0 = highest priority).
    """

    def __init__(self, memory, profile: str = "bare-metal"):
        self.memory = memory
        self.profile = profile if profile in PROFILES else "bare-metal"
        self.pending_tasks: list[Task] = []  # heapq
        self.goals: list[dict] = []
        self._boot_tasks_generated = False

    def add_goal(self, description: str, priority: int = 5):
        """Add a high-level goal that generates sub-tasks."""
        self.goals.append({
            "description": description,
            "priority": priority,
            "created_at": time.time(),
            "completed": False,
        })

    @staticmethod
    def _norm(text: str) -> str:
        return " ".join((text or "").split()).lower()

    def complete_goal(self, description: str) -> bool:
        """Mark a goal complete.

        Exact (case- and whitespace-insensitive) match first; otherwise a
        substring match only when it identifies exactly one open goal and
        the supplied text is long enough not to be accidental.
        """
        wanted = self._norm(description)
        if not wanted:
            return False
        open_goals = [g for g in self.goals if not g["completed"]]
        exact = [g for g in open_goals if self._norm(g["description"]) == wanted]
        match = exact[0] if exact else None
        if match is None and len(wanted) >= 8:
            partial = [g for g in open_goals
                       if wanted in self._norm(g["description"])
                       or self._norm(g["description"]) in wanted]
            if len(partial) == 1:
                match = partial[0]
        if match is None:
            return False
        match["completed"] = True
        match["completed_at"] = time.time()
        return True

    def open_goals(self) -> list:
        return [g for g in self.goals if not g["completed"]]

    def add_task(self, task: Task):
        """Add a task to the priority queue."""
        heapq.heappush(self.pending_tasks, task)

    def get_next_task(self) -> Optional[Task]:
        """Get the highest-priority pending task."""
        # Generate boot tasks on first call
        if not self._boot_tasks_generated:
            self._generate_boot_tasks()
            self._boot_tasks_generated = True

        while self.pending_tasks:
            task = heapq.heappop(self.pending_tasks)
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.RUNNING
                return task
        return None

    def generate_task(self, observations: dict) -> Optional[Task]:
        """Generate a new task based on observations."""
        # Check for input events that need handling
        input_events = observations.get("input_events", [])
        for event in input_events:
            if event.get("type") == "key" and event.get("value") == 1:
                task = Task(
                    priority=1,
                    description=f"Handle key press: {event.get('code', '?')}",
                    task_type=TaskType.USER_COMMAND,
                    metadata={"event": event},
                )
                self.add_task(task)
                return task

        # Check memory pressure
        mem_stats = observations.get("memory")
        if mem_stats and isinstance(mem_stats, dict):
            used_pct = mem_stats.get("used_percent", 0)
            if used_pct > 90:
                task = Task(
                    priority=0,
                    description="Critical: Memory usage above 90%",
                    task_type=TaskType.MAINTENANCE,
                    metadata={"memory_used_pct": used_pct},
                )
                self.add_task(task)
                return task

        # Check storage health
        storage = observations.get("storage")
        if storage and isinstance(storage, list):
            for dev in storage:
                if dev.get("health") == "failing":
                    task = Task(
                        priority=1,
                        description=f"Storage device failing: {dev.get('name')}",
                        task_type=TaskType.MAINTENANCE,
                        metadata={"device": dev},
                    )
                    self.add_task(task)
                    return task

        # Work toward goals
        for goal in self.goals:
            if not goal["completed"]:
                task = Task(
                    priority=goal["priority"],
                    description=f"Goal step: {goal['description']}",
                    task_type=TaskType.GOAL_STEP,
                    metadata={"goal": goal["description"]},
                )
                self.add_task(task)
                return task

        return None

    def handle_failure(self, task: Task, error: str):
        """Handle a failed task - retry or escalate."""
        task.retry_count += 1
        task.status = TaskStatus.PENDING

        if task.retry_count < task.max_retries:
            # Retry with lower priority
            task.priority = min(task.priority + 1, 10)
            task.metadata["last_error"] = error
            self.add_task(task)
        else:
            task.status = TaskStatus.FAILED
            self.memory.store(
                category="failed_task",
                data={
                    "description": task.description,
                    "error": error,
                    "retries": task.retry_count,
                },
            )

    def _generate_boot_tasks(self):
        """Generate initial tasks when the agent first starts."""
        if self.profile == "cloud":
            boot_tasks = self._cloud_boot_tasks()
        else:
            boot_tasks = self._bare_metal_boot_tasks()
        for task in boot_tasks:
            self.add_task(task)

    def _cloud_boot_tasks(self) -> list:
        """Headless instance: identify the cloud environment, then audit."""
        return [
            Task(
                priority=0,
                description="Identify cloud instance (IMDS)",
                task_type=TaskType.CLOUD_PROBE,
            ),
            Task(
                priority=1,
                description="Check system memory status",
                task_type=TaskType.SYSTEM_CHECK,
            ),
            Task(
                priority=1,
                description="Enumerate storage devices",
                task_type=TaskType.HARDWARE_PROBE,
            ),
            Task(
                priority=2,
                description="Run boot security scan",
                task_type=TaskType.SECURITY_SCAN,
            ),
        ]

    def _bare_metal_boot_tasks(self) -> list:
        """Bootable ISO: enumerate the physical hardware first."""
        return [
            Task(
                priority=0,
                description="Enumerate hardware devices",
                task_type=TaskType.HARDWARE_PROBE,
            ),
            Task(
                priority=1,
                description="Check system memory status",
                task_type=TaskType.SYSTEM_CHECK,
            ),
            Task(
                priority=1,
                description="Enumerate storage devices",
                task_type=TaskType.HARDWARE_PROBE,
            ),
            Task(
                priority=2,
                description="Check display/framebuffer status",
                task_type=TaskType.HARDWARE_PROBE,
            ),
            Task(
                priority=2,
                description="Verify input device availability",
                task_type=TaskType.HARDWARE_PROBE,
            ),
            Task(
                priority=3,
                description="Run boot security scan",
                task_type=TaskType.SECURITY_SCAN,
            ),
        ]
