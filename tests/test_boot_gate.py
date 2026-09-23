"""The standing refusals are a gate, not a description.

The architecture note said they ran as a boot gate and they did not: they were
a suite in the repository, run by hand, while the document told readers the
agent would not start if one failed. Seven were failing at the time. These
tests are about the wiring, so that sentence cannot quietly become untrue
again -- the gate is only a gate while the unit runs it and the image carries
the parts it needs.
"""

import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIT = os.path.join(ROOT, "aws", "systemd", "jarvis.service")
GATE = os.path.join(ROOT, "rootfs", "usr", "local", "bin", "jarvis-refusals")
PROVISION = os.path.join(ROOT, "aws", "scripts", "provision.sh")
SUITE = os.path.join(ROOT, "tests", "standing_refusals")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestTheUnitRunsIt(unittest.TestCase):

    def test_the_gate_is_on_execstartpre(self):
        lines = [ln.strip() for ln in read(UNIT).splitlines()
                 if ln.strip().startswith("ExecStartPre=")]
        self.assertEqual(lines, ["ExecStartPre=/usr/local/bin/jarvis-refusals"])

    def test_the_failure_is_not_swallowed(self):
        # "ExecStartPre=-/usr/local/bin/..." would run the gate and ignore the
        # verdict, which is the shape of a control that is not one.
        self.assertNotIn("ExecStartPre=-", read(UNIT))

    def test_it_runs_before_the_agent(self):
        text = read(UNIT)
        self.assertLess(text.index("ExecStartPre="), text.index("ExecStart="))


class TestTheGateFailsClosed(unittest.TestCase):
    """Every way of not running counts as a failure.

    A gate that skips when it cannot run is worse than no gate, because the
    operator believes there is one.
    """

    def run_gate(self, env=None, suite=SUITE):
        environ = dict(os.environ,
                       JARVIS_REFUSALS=suite,
                       JARVIS_REPO=ROOT,
                       JARVIS_PYTHON=sys.executable)
        environ.update(env or {})
        return subprocess.run(["bash", GATE], env=environ, capture_output=True,
                              text=True, timeout=300)

    def test_it_passes_against_the_real_code(self):
        # The control. Without it every assertion below is satisfied by a
        # script that always exits 1.
        got = self.run_gate()
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertIn("all standing refusals hold", got.stdout)

    def test_a_missing_suite_stops_the_boot(self):
        got = self.run_gate(suite=os.path.join(ROOT, "no-such-directory"))
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("no refusal suite", got.stderr)

    def test_a_missing_test_runner_stops_the_boot(self):
        # The agent argued this one should fail open, because a broken
        # dependency is not its conduct. It is also how you remove a gate with
        # one `rm`, which is why it does not.
        got = self.run_gate(env={"JARVIS_PYTHON": "/usr/bin/false"})
        self.assertNotEqual(got.returncode, 0)

    def test_an_empty_suite_stops_the_boot(self):
        import tempfile
        with tempfile.TemporaryDirectory() as empty:
            got = self.run_gate(suite=empty)
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("empty gate is not a gate", got.stderr)

    def test_could_not_check_is_not_reported_as_a_broken_refusal(self):
        """The half of the agent's objection that was right.

        Both stop the boot, but an operator woken at 3am needs to know whether
        an invariant broke or the runner never ran. Conflating them means
        debugging a refusal that never happened.
        """
        unchecked = self.run_gate(suite=os.path.join(ROOT, "no-such-directory"))
        self.assertEqual(unchecked.returncode, 2)
        self.assertIn("UNCHECKED", unchecked.stderr)
        self.assertNotIn("REFUSED", unchecked.stderr)

    def test_a_broken_refusal_says_so(self):
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            suite = os.path.join(tmp, "refusals")
            shutil.copytree(SUITE, suite)
            with open(os.path.join(suite, "test_cannot_hold.py"), "w") as fh:
                fh.write("import pytest\n"
                         "pytestmark = pytest.mark.refusals\n"
                         "def test_an_invariant_that_does_not_hold():\n"
                         "    assert False, 'this is what a broken refusal looks like'\n")
            got = self.run_gate(suite=suite)
        self.assertEqual(got.returncode, 1)
        self.assertIn("REFUSED", got.stderr)
        self.assertNotIn("UNCHECKED", got.stderr)

    def test_it_does_not_depend_on_tmp_being_usable(self):
        # A full /tmp on a box that has been up for months is the accidental
        # failure the agent named, and it is designed out rather than tolerated.
        script = read(GATE)
        self.assertIn("RUNTIME_DIRECTORY", script)
        self.assertIn("/var/tmp", script)


class TestTheImageCarriesWhatTheGateNeeds(unittest.TestCase):
    """The gate runs on the box, so its parts have to be on the box.

    None of this is installed by the agent package: the suite lives under
    tests/, ledgerd is a separate top-level package, and pytest was not on the
    image at all. Wiring the unit without these would give an instance that
    refuses to start for want of its own gate.
    """

    def setUp(self):
        self.provision = read(PROVISION)

    def test_the_gate_script_is_installed(self):
        self.assertIn("/usr/local/bin/jarvis-refusals", self.provision)

    def test_the_suite_is_installed(self):
        self.assertIn('cp -r "$SRC/tests/standing_refusals" "$PREFIX/refusals"',
                      self.provision)

    def test_the_ledgerd_package_the_suite_imports_is_installed(self):
        self.assertIn('cp -r "$SRC/ledgerd" "$PREFIX/ledgerd"', self.provision)
        self.assertIn("from ledgerd.client import",
                      read(os.path.join(SUITE, "test_standing_refusals.py")))

    def test_pytest_is_installed_and_its_absence_stops_the_build(self):
        self.assertRegex(self.provision, r"pip install[^\n]*pytest")
        self.assertIn("the boot gate could not run", self.provision)

    def test_the_build_runs_the_gate_before_making_an_image(self):
        # An image that cannot pass its own refusals must not become an image.
        # The alternative is an instance that boots into a failing gate, and
        # the first anyone hears of it is an agent that is not there.
        self.assertIn("/usr/local/bin/jarvis-refusals \\\n", self.provision)
        self.assertIn("the standing refusals do not hold; no image", self.provision)

    def test_the_gate_looks_where_the_image_puts_things(self):
        script = read(GATE)
        self.assertIn("/usr/lib/jarvis/refusals", script)
        self.assertIn("/usr/lib/jarvis", script)
        # The interpreter is the agent's own, not whatever python3 resolves to:
        # the image installs 3.11 beside a 3.9 system python.
        self.assertIn("/etc/default/jarvis", script)
        self.assertIn("JARVIS_PYTHON", script)


if __name__ == "__main__":
    unittest.main()
