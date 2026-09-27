"""The operator's settings survive a rebuilt cloud.yaml (the 27 September failure)."""

import os
import tempfile
import unittest

import yaml

from vigil.cloud import local_settings

# cloud.yaml as it was on the box that night: the launch data's stale name and
# dead model had been hand-edited to the working ones.
HAND_EDITED = {"agent": {"name": "jarvis", "profile": "cloud", "operator_name": "Paul"},
               "llm": {"enabled": True, "provider": "bedrock", "model": "qwen.qwen3-235b-a22b-2507-v1:0"},
               "ledger": {"writer": "jarvis", "anchor": {"bucket": "jarvis-ledger-1-jarvis"}}}
# What the bootstrap rebuilds from launch data.
REBUILT = {"agent": {"name": "paul-life-agent", "profile": "cloud"},
           "llm": {"enabled": True, "provider": "bedrock",
                   "model": "arn:aws:bedrock:us-west-2:1:imported-model/deleted"},
           "ledger": {"writer": "jarvis", "anchor": {"bucket": "jarvis-ledger-1-jarvis"}}}


class TestLocalSettings(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name, data=None):
        p = os.path.join(self.tmp.name, name)
        if data is not None:
            with open(p, "w") as fh:
                yaml.safe_dump(data, fh)
        return p

    def test_derive_carries_what_the_operator_chose(self):
        got = local_settings.derive(HAND_EDITED)
        self.assertEqual(got, {"agent": {"name": "Vigil", "operator_name": "Paul"},
                               "llm": {"provider": "bedrock", "model": "qwen.qwen3-235b-a22b-2507-v1:0"},
                               "ledger": {"writer": "jarvis"}})

    def test_the_writer_falls_back_to_the_old_name(self):
        old = {"agent": {"name": "jarvis"}}
        self.assertEqual(local_settings.derive(old)["ledger"], {"writer": "jarvis"})

    def test_a_rebuilt_cloud_yaml_cannot_undo_them(self):
        config = os.path.join(os.path.dirname(__file__), "..", "rootfs", "etc", "vigil", "config-aws.yaml")
        cloud = self.path("cloud.yaml", REBUILT)
        local = self.path("local.yaml")
        self.assertEqual(local_settings.main(["derive", self.path("old.yaml", HAND_EDITED), local]), 0)
        with open(local) as fh:
            self.assertTrue(fh.read().startswith("# The operator's own settings"))
        eff = local_settings.effective(config, cloud, local)
        self.assertEqual((eff["name"], eff["operator_name"], eff["model"], eff["ledger_writer"]),
                         ("Vigil", "Paul", "qwen.qwen3-235b-a22b-2507-v1:0", "jarvis"))
        without = local_settings.effective(config, cloud, None)
        self.assertEqual((without["name"], without["model"][-7:]), ("paul-life-agent", "deleted"),
                         "without local.yaml the launch data wins: the failure this prevents")

    def test_a_missing_local_yaml_is_skipped(self):
        config = os.path.join(os.path.dirname(__file__), "..", "rootfs", "etc", "vigil", "config-aws.yaml")
        eff = local_settings.effective(config, self.path("cloud.yaml", REBUILT), self.path("absent.yaml"))
        self.assertEqual(eff["name"], "paul-life-agent")

    def test_the_service_loads_it_last(self):
        unit = os.path.join(os.path.dirname(__file__), "..", "aws", "systemd", "vigil.service")
        line = [l for l in open(unit) if l.startswith("ExecStart=")][0]
        self.assertLess(line.index("/etc/vigil/cloud.yaml"), line.index("/etc/vigil/local.yaml"))

    def test_the_migration_restores_cloud_yaml_on_rollback(self):
        script = open(os.path.join(os.path.dirname(__file__), "..", "aws", "scripts", "migrate_to_vigil.sh")).read()
        self.assertIn("cloud.yaml.pre-vigil.$STAMP", script)
        self.assertIn("local_settings derive", script)
        self.assertIn('[ "$got" = "Vigil True" ]', script, "the check needs the brain, not just the name")


if __name__ == "__main__":
    unittest.main()
