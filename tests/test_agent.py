"""Tests for Jarvis Agent Core."""

import unittest
import logging

from jarvis.agent.core import AgentCore
from jarvis.agent.planner import Task, TaskPlanner, TaskType, TaskStatus
from jarvis.agent.executor import TaskExecutor
from jarvis.agent.memory import AgentMemory


class TestAgentMemory(unittest.TestCase):
    """Tests for AgentMemory."""

    def test_store_and_recall(self):
        mem = AgentMemory(max_entries=100)
        mem.store("test", {"key": "value"})
        entries = mem.recall("test")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["data"]["key"], "value")

    def test_max_entries_eviction(self):
        mem = AgentMemory(max_entries=5)
        for i in range(10):
            mem.store("test", {"i": i})
        self.assertEqual(len(mem), 5)

    def test_recall_all(self):
        mem = AgentMemory(max_entries=100)
        mem.store("cat1", {"a": 1})
        mem.store("cat2", {"b": 2})
        entries = mem.recall_all()
        self.assertEqual(len(entries), 2)

    def test_search(self):
        mem = AgentMemory(max_entries=100)
        mem.store("test", {"msg": "hello world"})
        mem.store("test", {"msg": "goodbye"})
        results = mem.search("hello")
        self.assertEqual(len(results), 1)

    def test_search_by_tags(self):
        mem = AgentMemory(max_entries=100)
        mem.store("test", {"a": 1}, tags=["important"])
        mem.store("test", {"b": 2}, tags=["minor"])
        results = mem.search_by_tags(["important"])
        self.assertEqual(len(results), 1)

    def test_clear_category(self):
        mem = AgentMemory(max_entries=100)
        mem.store("keep", {"a": 1})
        mem.store("remove", {"b": 2})
        mem.clear("remove")
        self.assertEqual(len(mem), 1)
        self.assertEqual(len(mem.recall("keep")), 1)
        self.assertEqual(len(mem.recall("remove")), 0)

    def test_clear_all(self):
        mem = AgentMemory(max_entries=100)
        mem.store("a", {"x": 1})
        mem.store("b", {"y": 2})
        mem.clear()
        self.assertEqual(len(mem), 0)

    def test_get_summary(self):
        mem = AgentMemory(max_entries=100)
        mem.store("cat1", {"a": 1})
        mem.store("cat1", {"b": 2})
        mem.store("cat2", {"c": 3})
        summary = mem.get_summary()
        self.assertEqual(summary["total_entries"], 3)
        self.assertEqual(summary["categories"]["cat1"], 2)
        self.assertEqual(summary["categories"]["cat2"], 1)


class TestTaskPlanner(unittest.TestCase):
    """Tests for TaskPlanner."""

    def test_add_and_get_task(self):
        mem = AgentMemory()
        planner = TaskPlanner(mem)
        planner._boot_tasks_generated = True  # Skip boot tasks

        task = Task(priority=5, description="Test task")
        planner.add_task(task)
        result = planner.get_next_task()
        self.assertIsNotNone(result)
        self.assertEqual(result.description, "Test task")
        self.assertEqual(result.status, TaskStatus.RUNNING)

    def test_priority_ordering(self):
        mem = AgentMemory()
        planner = TaskPlanner(mem)
        planner._boot_tasks_generated = True

        planner.add_task(Task(priority=10, description="Low priority"))
        planner.add_task(Task(priority=1, description="High priority"))
        planner.add_task(Task(priority=5, description="Medium priority"))

        t1 = planner.get_next_task()
        t2 = planner.get_next_task()
        t3 = planner.get_next_task()

        self.assertEqual(t1.description, "High priority")
        self.assertEqual(t2.description, "Medium priority")
        self.assertEqual(t3.description, "Low priority")

    def test_empty_queue_returns_none(self):
        mem = AgentMemory()
        planner = TaskPlanner(mem)
        planner._boot_tasks_generated = True
        self.assertIsNone(planner.get_next_task())

    def test_boot_tasks_generated(self):
        mem = AgentMemory()
        planner = TaskPlanner(mem)
        # First call should generate boot tasks
        task = planner.get_next_task()
        self.assertIsNotNone(task)
        self.assertTrue(planner._boot_tasks_generated)

    def test_handle_failure_retry(self):
        mem = AgentMemory()
        planner = TaskPlanner(mem)
        planner._boot_tasks_generated = True

        task = Task(priority=5, description="Failing task", max_retries=3)
        planner.handle_failure(task, "some error")
        self.assertEqual(task.retry_count, 1)
        self.assertEqual(task.status, TaskStatus.PENDING)
        self.assertEqual(len(planner.pending_tasks), 1)

    def test_handle_failure_max_retries(self):
        mem = AgentMemory()
        planner = TaskPlanner(mem)
        planner._boot_tasks_generated = True

        task = Task(priority=5, description="Failing task", max_retries=1)
        task.retry_count = 1  # Already at max
        planner.handle_failure(task, "final error")
        self.assertEqual(task.status, TaskStatus.FAILED)

    def test_add_goal(self):
        mem = AgentMemory()
        planner = TaskPlanner(mem)
        planner.add_goal("Test goal", priority=3)
        self.assertEqual(len(planner.goals), 1)
        self.assertEqual(planner.goals[0]["description"], "Test goal")


class TestTask(unittest.TestCase):
    """Tests for Task dataclass."""

    def test_to_dict(self):
        task = Task(priority=5, description="Test", task_type=TaskType.SYSTEM_CHECK)
        d = task.to_dict()
        self.assertEqual(d["description"], "Test")
        self.assertEqual(d["type"], "system_check")
        self.assertEqual(d["priority"], 5)

    def test_ordering(self):
        t1 = Task(priority=1, description="First")
        t2 = Task(priority=10, description="Last")
        self.assertLess(t1, t2)


class TestAgentCore(unittest.TestCase):
    """Tests for AgentCore."""

    def setUp(self):
        self.config = {
            "name": "TestAgent",
            "max_tasks": 50,
        }
        self.hardware = {
            "display": None,
            "input": None,
            "memory": None,
            "storage": None,
        }
        self.logger = logging.getLogger("test")

    def test_init(self):
        agent = AgentCore(self.config, self.hardware, self.logger)
        self.assertEqual(agent.name, "TestAgent")
        self.assertFalse(agent.running)

    def test_get_status(self):
        agent = AgentCore(self.config, self.hardware, self.logger)
        status = agent.get_status()
        self.assertEqual(status["name"], "TestAgent")
        self.assertFalse(status["running"])
        self.assertEqual(status["cycle_count"], 0)

    def test_observe_no_hardware(self):
        agent = AgentCore(self.config, self.hardware, self.logger)
        obs = agent.observe()
        self.assertIn("timestamp", obs)
        self.assertIn("cycle", obs)

    def test_add_goal(self):
        agent = AgentCore(self.config, self.hardware, self.logger)
        agent.add_goal("Test goal", priority=3)
        self.assertEqual(len(agent.planner.goals), 1)

    def test_run_cycle(self):
        agent = AgentCore(self.config, self.hardware, self.logger)
        result = agent.run_cycle()
        self.assertEqual(result["cycle"], 1)
        # Should have processed a boot task
        self.assertIn("action", result)

    def test_shutdown(self):
        agent = AgentCore(self.config, self.hardware, self.logger)
        agent.running = True
        agent.shutdown()
        self.assertFalse(agent.running)


if __name__ == "__main__":
    unittest.main()
