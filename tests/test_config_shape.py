"""The deployed config file has a shape, and nothing was checking it.

22 September: a `documents:` block was added at column 0, just after
`cloud.ui`. In YAML that ends the mapping it lands in, so `cloud.estate`,
`cloud.notify`, `cloud.memory_backup` and `cloud.document_ocr` all became
children of `documents` instead. 23 September: a `marketing:` block was added
the same way and adopted them again.

Neither raised. Nothing validates this file, the code reads each section with
a .get() that returns None just as quietly for "moved" as for "absent", and
1,314 tests passed both times. The one that matters is `notify`: it is the
only way the agent can reach Paul when nobody is looking at the UI, and it
would have gone quiet with no error anywhere.

So the file's shape is now an assertion rather than a convention.
"""

import os
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "rootfs", "etc", "jarvis", "config-aws.yaml")

# Where each section has to live for the code that reads it to find it.
# Written as full paths, because "it is in the file somewhere" is exactly the
# check that passed twice while the file was wrong.
REQUIRED = {
    "cloud": ("ui", "estate", "notify", "memory_backup", "document_ocr"),
    "agent": ("name", "profile", "rung"),
}
REQUIRED_TOP = ("agent", "cloud", "ledger", "llm", "memory", "documents",
                "marketing")


class TestTheSectionsAreWhereTheCodeLooks(unittest.TestCase):

    def setUp(self):
        with open(CONFIG, encoding="utf-8") as fh:
            self.cfg = yaml.safe_load(fh)

    def test_every_top_level_section_is_present(self):
        missing = [k for k in REQUIRED_TOP if k not in self.cfg]
        self.assertEqual(missing, [], f"missing at column 0: {missing}")

    def test_every_nested_section_is_under_its_own_parent(self):
        wrong = []
        for parent, children in REQUIRED.items():
            block = self.cfg.get(parent)
            self.assertIsInstance(block, dict, f"{parent} is not a mapping")
            wrong += [f"{parent}.{c}" for c in children if c not in block]
        self.assertEqual(wrong, [], f"not where the code reads them: {wrong}")

    def test_the_notifier_is_where_it_can_be_found(self):
        # Called out on its own because it is the one that goes silent: no
        # exception, no log line, just an agent that stops being able to
        # reach anybody.
        notify = (self.cfg.get("cloud") or {}).get("notify")
        self.assertIsInstance(notify, dict, "cloud.notify has moved or gone")
        for key in ("channel", "destination_file", "min_severity",
                    "max_per_hour", "quiet_hours"):
            self.assertIn(key, notify, key)

    def test_no_top_level_section_has_adopted_another_ones_children(self):
        """The actual failure mode, stated directly.

        A column-0 key dropped into the middle of the file ends the mapping
        it lands in and takes everything indented below it. The symptom is a
        top-level section holding keys that belong to a different parent.
        """
        for parent, children in REQUIRED.items():
            for other, block in self.cfg.items():
                if other == parent or not isinstance(block, dict):
                    continue
                stolen = [c for c in children if c in block]
                self.assertEqual(
                    stolen, [],
                    f"{other} has adopted {stolen}, which belong to {parent}")

    def test_the_two_newest_sections_hold_only_their_own_keys(self):
        self.assertEqual(set(self.cfg["documents"]), {"dir"})
        self.assertEqual(set(self.cfg["marketing"]),
                         {"enabled", "table", "region"})


class TestTheAgentReadsWhatTheFileSays(unittest.TestCase):
    """Parsing it is not the same as the code finding it."""

    def test_the_agent_builds_with_the_real_file_and_keeps_its_wiring(self):
        import logging
        with open(CONFIG, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        from jarvis.agent.core import AgentCore
        agent = AgentCore(dict(cfg.get("agent") or {}, **{
            k: cfg[k] for k in ("documents", "marketing") if k in cfg}),
            {"display": None, "input": None, "memory": None, "storage": None},
            logging.getLogger("test"))
        self.assertEqual(agent.document_dir, cfg["documents"]["dir"])
        self.assertIsNotNone(agent.marketing, "marketing view was not built")
        self.assertEqual(agent.marketing.table_name, cfg["marketing"]["table"])


if __name__ == "__main__":
    unittest.main()


class TestTheAgentIsHandedWhatItReads(unittest.TestCase):
    """AgentCore gets the `agent:` block alone.

    Anything it reads with config.get() has to be copied in by main.py, and
    a section that is not copied is simply absent -- which looks exactly like
    "switched off", with no error anywhere. document_ocr said `enabled: true`
    in this file and had never once reached the executor.
    """

    def setUp(self):
        import ast
        import inspect
        from jarvis.agent import core
        self.core_src = inspect.getsource(core)
        with open(os.path.join(ROOT, "jarvis", "main.py"), encoding="utf-8") as fh:
            self.main_src = fh.read()
        with open(CONFIG, encoding="utf-8") as fh:
            self.cfg = yaml.safe_load(fh)
        # Every key AgentCore.__init__ reads straight off its own config.
        tree = ast.parse(self.core_src)
        self.read = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "config"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)):
                self.read.add(node.args[0].value)

    @staticmethod
    def _keys_anywhere(node, out=None):
        out = set() if out is None else out
        if isinstance(node, dict):
            for k, v in node.items():
                out.add(k)
                TestTheAgentIsHandedWhatItReads._keys_anywhere(v, out)
        return out

    def test_nothing_configured_in_the_file_fails_to_reach_the_agent(self):
        """The bug class, stated exactly.

        A key the agent reads, which somebody has configured somewhere in
        this file, but which main.py never copies into the agent's own
        block. It is present, it is set, and the agent cannot see it -- which
        is indistinguishable from off. Keys that are simply not in the file
        are not this: they fall back to their defaults on purpose.
        """
        in_file = self._keys_anywhere(self.cfg)
        in_agent = set(self.cfg.get("agent") or {})
        lost = [k for k in sorted(self.read)
                if k in in_file and k not in in_agent
                and f'"{k}"' not in self.main_src and f"'{k}'" not in self.main_src]
        self.assertEqual(lost, [],
                         f"configured but never handed to the agent: {lost}")

    def test_the_three_that_were_wrong_are_handed_over(self):
        # document_ocr lives at cloud.document_ocr and said enabled: true;
        # it had never reached the executor. documents and marketing are new
        # top-level sections with the same fault.
        for key in ("document_ocr", "documents", "marketing"):
            self.assertIn(f'"{key}"', self.main_src,
                          f"main.py does not hand {key} to the agent")

    def test_the_marketing_view_is_built_from_the_agents_own_config(self):
        # core reads this one through build_view rather than config.get, so
        # the check above cannot see it.
        self.assertIn("build_view(config", self.core_src)
