"""Tests for the agent's durable operational memory.

The failure this guards against is amnesia: before this store the agent's
notes were a deque in a process, so a restart erased everything it had
worked out about its own machine.
"""

import logging
import os
import tempfile
import unittest

from jarvis.agent.core import AgentCore
from jarvis.agent.executor import check_command_allowed, normalise_shell_policy
from jarvis.agent.store import MemoryStore, NullStore, build_store
from jarvis.brain.llm import BaseBrain

LOG = logging.getLogger("test")
NO_HW = {"display": None, "input": None, "memory": None, "storage": None}


class Brain(BaseBrain):
    def _make_client(self):
        return object()

    def _system_text(self):
        return "system"


def temp_db():
    return os.path.join(tempfile.mkdtemp(), "memory.db")


class TestMemoryStore(unittest.TestCase):

    def setUp(self):
        self.store = MemoryStore(temp_db(), max_rows=50, logger=LOG)

    def test_opens_and_reports_available(self):
        self.assertTrue(self.store.available)
        self.assertTrue(self.store.stats()["available"])

    def test_remembers_and_recalls(self):
        self.store.remember("the root disk is 8G", kind="fact")
        self.assertEqual([m["text"] for m in self.store.recent(5)], ["the root disk is 8G"])

    def test_repeating_a_memory_refreshes_one_row(self):
        first = self.store.remember("nginx is not installed")
        again = self.store.remember("nginx is not installed")
        self.assertFalse(first["repeat"])
        self.assertTrue(again["repeat"])
        self.assertEqual(again["seen"], 2)
        self.assertEqual(self.store.stats()["entries"], 1)

    def test_blank_text_stores_nothing(self):
        self.assertIsNone(self.store.remember("   "))
        self.assertEqual(self.store.stats()["entries"], 0)

    def test_provenance_is_kept(self):
        self.store.remember("operator said so", kind="note", source="operator")
        row = self.store.recent(1)[0]
        self.assertEqual(row["source"], "operator")

    def test_unknown_kind_and_source_fall_back(self):
        self.store.remember("odd one", kind="nonsense", source="nonsense")
        row = self.store.recent(1)[0]
        self.assertEqual(row["kind"], "note")
        self.assertEqual(row["source"], "brain")

    def test_recent_filters_by_kind(self):
        self.store.remember("a note", kind="note")
        self.store.remember("a fact", kind="fact")
        self.assertEqual([m["text"] for m in self.store.recent(5, kind="fact")], ["a fact"])

    def test_search_requires_every_term(self):
        self.store.remember("the root disk is 8G and 23% used")
        self.store.remember("the model is Qwen3 on bedrock")
        self.assertEqual(len(self.store.search("root disk")), 1)
        self.assertEqual(self.store.search("root Qwen3"), [])

    def test_search_with_no_terms_returns_nothing(self):
        self.store.remember("something")
        self.assertEqual(self.store.search("   "), [])

    def test_forget_removes_one(self):
        row = self.store.remember("temporary")
        self.assertTrue(self.store.forget(row["id"]))
        self.assertEqual(self.store.stats()["entries"], 0)
        self.assertFalse(self.store.forget(row["id"]))

    def test_prune_drops_oldest_and_keeps_pinned(self):
        self.store.remember("keep me always", pinned=True)
        for i in range(30):
            self.store.remember(f"filler {i}")
        self.store.prune(10)
        texts = [m["text"] for m in self.store.recent(50)]
        self.assertIn("keep me always", texts)
        self.assertLessEqual(len(texts), 10)
        self.assertNotIn("filler 0", texts)

    def test_survives_reopening_the_same_file(self):
        path = temp_db()
        first = MemoryStore(path, logger=LOG)
        first.remember("learned before the restart", kind="fact")
        first.close()
        second = MemoryStore(path, logger=LOG)
        self.assertEqual([m["text"] for m in second.recent(5)], ["learned before the restart"])

    def test_unwritable_path_degrades_instead_of_raising(self):
        store = MemoryStore("/proc/definitely/not/writable/memory.db", logger=LOG)
        self.assertFalse(store.available)
        self.assertIsNone(store.remember("anything"))
        self.assertEqual(store.recent(5), [])
        self.assertIn("reason", store.stats())


class TestBuildStore(unittest.TestCase):

    def test_disabled_config_gives_a_null_store(self):
        store = build_store({"enabled": False}, LOG)
        self.assertIsInstance(store, NullStore)
        self.assertIsNone(store.remember("ignored"))
        self.assertEqual(store.stats()["enabled"], False)

    def test_enabled_config_gives_a_real_store(self):
        store = build_store({"enabled": True, "path": temp_db()}, LOG)
        self.assertTrue(store.available)


class TestAgentMemoryIntegration(unittest.TestCase):

    def setUp(self):
        self.path = temp_db()

    def agent(self):
        a = AgentCore({"name": "t", "profile": "cloud"}, dict(NO_HW), LOG,
                      store=MemoryStore(self.path, logger=LOG))
        a.planner._boot_tasks_generated = True
        return a

    def test_notes_are_restored_after_a_restart(self):
        first = self.agent()
        first.remember("the box has 2 cores")
        first.store.close()
        second = self.agent()                       # a fresh process, same disk
        self.assertIn("the box has 2 cores", list(second.notes))

    def test_facts_are_not_put_in_the_note_cache(self):
        a = self.agent()
        a.remember("a stored fact", kind="fact")
        self.assertEqual(list(a.notes), [])
        self.assertEqual(len(a.store.recent(5, kind="fact")), 1)

    def test_remembering_nothing_is_a_no_op(self):
        a = self.agent()
        self.assertIsNone(a.remember(""))
        self.assertEqual(a.store.stats()["entries"], 0)

    def test_status_reports_the_store(self):
        a = self.agent()
        self.assertTrue(a.get_status()["memory_store"]["available"])

    def test_agent_without_a_store_still_works(self):
        a = AgentCore({"name": "t", "profile": "cloud"}, dict(NO_HW), LOG)
        a.planner._boot_tasks_generated = True
        self.assertIsNone(a.remember("goes nowhere durable"))
        self.assertEqual(list(a.notes), ["goes nowhere durable"])

    def test_pinned_memory_reaches_the_model(self):
        a = self.agent()
        a.remember("the witness bucket is append-only", kind="fact", pinned=True)
        context = Brain({}, LOG).build_context(a, {})
        self.assertIn("the witness bucket is append-only", context["pinned_memory"])

    def test_unpinned_memory_does_not_crowd_the_context(self):
        a = self.agent()
        a.remember("an ordinary fact", kind="fact")
        context = Brain({}, LOG).build_context(a, {})
        self.assertNotIn("pinned_memory", context)


class TestMemoryIsNotShellEditable(unittest.TestCase):

    def setUp(self):
        self.policy = normalise_shell_policy({"enabled": True}, LOG)

    def test_the_memory_database_is_denied(self):
        for command in ("sqlite3 /var/lib/jarvis/memory.db 'delete from memories'",
                        "rm /var/lib/jarvis/memory.db",
                        "cat /var/lib/jarvis/memory.db"):
            self.assertIsNotNone(check_command_allowed(command, self.policy), command)

    def test_ordinary_commands_still_run(self):
        self.assertIsNone(check_command_allowed("df -h /", self.policy))


if __name__ == "__main__":
    unittest.main()
