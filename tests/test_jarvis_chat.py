"""The chat helper must deliver a message as text, never as shell.

On 27 September 2026 backticks in a message to Jarvis were executed as root by
the box's shell. This runs the helper's real command line under bash, with
curl replaced by a stub that saves what it was sent, and a message built to
execute a canary if any shell reads it.
"""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "jarvis_chat", os.path.join(HERE, "..", "aws", "scripts", "jarvis_chat.py"))
chat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(chat)


class ChatStaysText(unittest.TestCase):
    def test_metacharacters_arrive_verbatim_and_nothing_runs(self):
        with tempfile.TemporaryDirectory() as d:
            canary = os.path.join(d, "ran")
            sink = os.path.join(d, "sent.json")
            message = ("backticks `touch %s` and $(touch %s) and ${HOME} and "
                       "'single' \"double\" ; | & > %s < \\ newline\nend"
                       % (canary, canary, canary))
            command = chat.build_command(message)
            # Stubs for the box: a token file, a curl that records its body,
            # and python3.11 mapped to whatever python runs these tests.
            bindir = os.path.join(d, "bin")
            os.mkdir(bindir)
            with open(os.path.join(bindir, "curl"), "w") as fh:
                fh.write("#!/bin/sh\nfor a in \"$@\"; do case \"$a\" in @*) "
                         "cp \"${a#@}\" %s;; esac; done\n"
                         "echo '{\"answer\": \"ok\"}'\n" % sink)
            os.symlink(subprocess.check_output(["which", "python3"], text=True).strip(),
                       os.path.join(bindir, "python3.11"))
            os.chmod(os.path.join(bindir, "curl"), 0o755)
            token = os.path.join(d, "token")
            with open(token, "w") as fh:
                fh.write("t")
            command = command.replace(chat.TOKEN, token)
            env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"])
            out = subprocess.run(["bash", "-c", command], env=env,
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("ok", out.stdout)
            self.assertFalse(os.path.exists(canary), "a shell executed part of the message")
            with open(sink) as fh:
                sent = json.load(fh)
            self.assertEqual(sent["messages"][0]["content"], message)

    def test_only_base64_reaches_the_command_line(self):
        command = chat.build_command("`id` $(id)")
        self.assertNotIn("`id`", command)
        self.assertNotIn("$(id)", command)


if __name__ == "__main__":
    unittest.main()
