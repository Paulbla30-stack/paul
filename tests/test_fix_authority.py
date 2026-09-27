"""Regression tests for the permission spine fixes of 27 September 2026.

Every command in the CHANGE lists below classified as READ before the fix,
and at observer or proposer a READ runs with no proposal. The promise the
module makes is that a command that cannot be recognised as read-only is a
change; these pin the places where it did not keep it.
"""

import logging
import os
import tempfile
import unittest

from jarvis.agent import authority
from jarvis.agent.executor import TaskExecutor
from jarvis.agent.memory import AgentMemory
from jarvis.agent.planner import Task, TaskType

LOG = logging.getLogger("test.fix_authority")
LOG.addHandler(logging.NullHandler())
NO_HW = {"display": None, "input": None, "memory": None, "storage": None}


class Classify(unittest.TestCase):

    def read(self, *commands):
        for command in commands:
            self.assertEqual(authority.classify_command(command), authority.READ, command)

    def change(self, *commands):
        for command in commands:
            self.assertEqual(authority.classify_command(command), authority.CHANGE, command)


class TestTheReportedBypasses(Classify):
    """The five commands the finding confirmed as READ."""

    def test_the_reported_commands_are_changes(self):
        self.change("find /var/log -name '*.log' | xargs rm -f",
                    "ls /etc | xargs chmod 777",
                    "sort -o /etc/passwd /tmp/x",
                    "journalctl --vacuum-size=1M",
                    "awk 'BEGIN{system(\"rm -rf /x\")}'")


class TestXargs(Classify):

    def test_classified_by_the_command_it_runs(self):
        self.change("ls | xargs rm", "ls | xargs -0 -n 1 chmod 600",
                    "ls | xargs -I{} mv {} /tmp", "ls | xargs sudo rm",
                    "ls | xargs --max-args=1 rm")
        self.read("find . -name '*.log' | xargs grep ERROR",
                  "find . | xargs wc -l", "ls | xargs -n 1 -I{} stat {}")

    def test_no_command_is_echo(self):
        self.read("xargs", "echo x | xargs", "ls | xargs -0")

    def test_arguments_from_input_cannot_be_seen_so_sensitive_programs_are_changes(self):
        # xargs appends words from its input: this is sort -o /etc/passwd.
        self.change("echo -o /etc/passwd | xargs sort",
                    "ls | xargs sed -n p", "ls | xargs awk 1",
                    "ls | xargs date", "echo x | xargs env", "ls | xargs xargs rm")

    def test_an_unrecognised_xargs_option_is_a_change(self):
        self.change("ls | xargs --no-such-option cat")


class TestAwk(Classify):

    def test_plain_reads_stay_reads(self):
        self.read("awk '{print $1}' /etc/passwd", "awk '$5 > 80' f",
                  "awk -F: '{print $1}' /etc/passwd", "awk -F : -v n=3 '{print $n}' f",
                  "awk '{s+=$1} END {print s}' f",
                  "awk '$1 == \"a\" || $2 == \"b\" {print}' f")

    def test_running_a_command_is_a_change(self):
        self.change("awk 'BEGIN{system(\"id\")}'", "awk 'BEGIN { system (\"id\") }'",
                    "awk '{ \"date\" | getline d; print d }'",
                    "awk '{ getline line < \"/etc/shadow\" }'",
                    "awk '{ print | \"sh\" }'", "awk '/a|b/'")

    def test_print_redirected_to_a_file_is_a_change(self):
        self.change("awk '{print $1 > \"/tmp/x\"}' f", "awk '{print >> \"/tmp/x\"}' f",
                    "awk '{printf(\"%s\", $1) > \"/tmp/x\"}' f")

    def test_in_place_and_uninspectable_programs_are_changes(self):
        self.change("awk -i inplace '{print}' f", "awk --inplace '{print}' f",
                    "awk --include=inplace '{print}' f", "awk -f prog.awk f",
                    "awk -l ext '{print}' f", "awk -o/tmp/x '{print}' f",
                    "awk '@load \"filefuncs\"; {print}' f")


class TestSortAndUniq(Classify):

    def test_sort_output_is_a_change(self):
        self.change("sort -o /etc/passwd /tmp/x", "sort -o/etc/passwd x",
                    "sort -nro /x y", "sort --output=/x y", "sort --output /x y",
                    "sort --out=/x y", "sort --compress-program=sh y")
        self.read("sort -rn f", "sort -k2 -t: f", "sort -u f", "sort -k1o f")

    def test_uniq_with_an_output_operand_is_a_change(self):
        self.change("uniq in out", "uniq -c in out", "uniq --bogus in")
        self.read("uniq f", "uniq -c f", "sort f | uniq -d", "uniq -f 2 f")


class TestJournalctl(Classify):

    def test_writing_options_are_changes(self):
        for option in ("--vacuum-size=1M", "--vacuum-time=1d", "--vacuum-files=1",
                       "--rotate", "--flush", "--relinquish-var", "--sync",
                       "--setup-keys", "--update-catalog", "--cursor-file=/tmp/c"):
            self.change(f"journalctl {option}")

    def test_abbreviations_getopt_would_accept_are_changes(self):
        self.change("journalctl --vac=1M", "journalctl --rot", "journalctl --fl")

    def test_reading_stays_reading(self):
        self.read("journalctl -u jarvis -n 50", "journalctl --no-pager -p err",
                  "journalctl --since today --unit jarvis", "journalctl --cursor=abc")


class TestDateAndHostname(Classify):

    def test_setting_the_clock_is_a_change(self):
        self.change("date -s 2020-01-01", "date --set=2020-01-01", "date --s 2020",
                    "date -us 2020", "date 010100002020", "date -I 010100002020")
        self.read("date", "date +%s", "date -u +%FT%T", "date -d yesterday +%F",
                  "date -Iseconds", "date --iso-8601")

    def test_setting_the_hostname_is_a_change(self):
        self.change("hostname evil", "hostname -F /tmp/name", "hostname -b x",
                    "hostnamectl set-hostname evil", "hostnamectl hostname evil",
                    "hostnamectl set-location x")
        self.read("hostname", "hostname -f", "hostname -I", "hostnamectl",
                  "hostnamectl status", "hostnamectl hostname")


class TestIp(Classify):

    def test_writing_verbs_are_changes_including_abbreviations(self):
        self.change("ip link set eth0 down", "ip addr add 10.0.0.1/24 dev eth0",
                    "ip a a 10.0.0.1/24 dev eth0", "ip route del default",
                    "ip r d default", "ip addr flush dev eth0", "ip route restore",
                    "ip netns exec x rm y", "ip -batch cmds", "ip -force link set x up")

    def test_showing_is_reading(self):
        self.read("ip addr", "ip a", "ip route", "ip -j addr show dev eth0",
                  "ip route get 1.1.1.1", "ip -s link", "ip -br link show")


class TestFindSedSystemctlSysctlRpm(Classify):

    def test_find_predicates_that_write_or_run(self):
        self.change("find . -fls /tmp/x", "find . -fprint0 /tmp/x",
                    "find . -okdir rm {} ;", "find . -fprint /tmp/x")
        self.read("find /etc -name '*.conf'", "find / -xdev -size +100M")

    def test_sed_in_place_in_every_spelling(self):
        self.change("sed -i s/a/b/ f", "sed -i.bak s/a/b/ f", "sed -ni p f",
                    "sed -Ei s/a/b/ f", "sed --in-place s/a/b/ f", "sed --in-pl s/a/b/ f")

    def test_sed_scripts_that_write_or_run(self):
        self.change("sed -n w/tmp/x f", "sed 's/a/b/w /tmp/x' f",
                    "sed -e 'e rm -rf /x' f", "sed 's/a/id/e' f", "sed '1W /tmp/x' f",
                    "sed -f script.sed f", "sed --expression='w /tmp/x' f")
        self.read("sed s/a/b/ /etc/hosts", "sed -n '1,10p' f", "sed -n '/foo/,/bar/p' f",
                  "sed 's/new/old/g' f", "sed ':a;N;$!ba;s/\\n/ /g' f", "sed -n '$p' f",
                  "sed -E 's/(a|b)/x/g' f", "sed '1d' f", "sed -e 's/x/y/' -e '2q' f")

    def test_systemctl_verbs_not_known_to_read_are_changes(self):
        for verb in ("try-restart", "reload-or-restart", "link", "revert", "preset",
                     "set-default", "set-environment", "reboot", "poweroff", "kill",
                     "freeze", "clean", "daemon-reexec", "log-level"):
            self.change(f"systemctl {verb} jarvis")
        # An option whose argument is a read verb does not hide the real one.
        self.change("systemctl -p status restart jarvis", "systemctl --bogus status")
        self.read("systemctl status jarvis", "systemctl --no-pager -l status jarvis",
                  "systemctl list-units --type=service --state=failed", "systemctl",
                  "systemctl is-enabled jarvis", "systemctl show -p ActiveState jarvis")

    def test_sysctl_load_in_every_spelling(self):
        self.change("sysctl -pfoo", "sysctl -p/etc/sysctl.conf", "sysctl -qp",
                    "sysctl --sys", "sysctl --lo", "sysctl -qw a.b=1")
        self.read("sysctl -a", "sysctl -n kernel.hostname", "sysctl kernel.yama.ptrace_scope")

    def test_rpm_reads_only_in_query_mode(self):
        self.change("rpm -ivh x.rpm", "rpm -Uvh x.rpm", "rpm -Fvh x.rpm",
                    "rpm --import key", "rpm --rebuilddb", "rpm -E '%(id)'",
                    "rpm --eval '%(id)'", "rpm -qa --pipe sh", "rpm --setperms x")
        self.read("rpm -qa", "rpm -qi kernel", "rpm -q --qf '%{NAME}\\n' kernel",
                  "rpm -Va", "rpm -qf /etc/hosts")

    def test_file_and_ss(self):
        self.change("file -C -m magic", "file --compile", "ss -K dst 1.2.3.4",
                    "ss --kill", "ss -D /tmp/dump")
        self.read("file -b /etc/hosts", "ss -tlnp", "ss -s")


class TestThingsNotOnTheListStayChanges(Classify):
    """Named in the finding; none is on the read list, which is the point."""

    def test_writers_not_on_the_list(self):
        self.change("timedatectl set-time 12:00", "hwclock -w", "hwclock --systohc",
                    "mount -o remount,rw /", "git pull", "git checkout -- .",
                    "echo x | tee /etc/x", "dd if=/dev/zero of=/dev/xvda")


class TestTheShellAroundTheWords(Classify):
    """The command runs under /bin/sh -c, so what the shell does counts."""

    def test_substitutions_the_lexer_cannot_see(self):
        self.change("echo `rm -rf /x`", "echo \"$(rm -rf /x)\"", "cat <(rm -rf /x)",
                    "echo ${X:=1}", "echo $((1+1))")

    def test_words_the_shell_rewrites_into_options(self):
        self.change("sort {-o,/etc/passwd} x", "sort $'-o' /etc/passwd x",
                    "date $\"-s\" 2020")

    def test_separators_the_old_split_missed(self):
        self.change("cat x |& rm y", "cat x;(rm y)", "cat x&&(rm y)",
                    "cat x\nrm y", "cat x # note\nrm y")

    def test_quoted_text_is_still_quoted(self):
        self.read("echo '`not run`'", "echo '$(not run)'", "grep -E 'a{1,3}' f",
                  'grep -E "warning|critical" /var/log/jarvis.log | wc -l',
                  "echo $HOME", "cat < /etc/hosts")

    def test_a_program_named_by_path_must_be_a_system_one(self):
        self.change("/tmp/evil/cat x", "./cat x", "bin/cat x")
        self.read("/usr/bin/cat x", "/bin/ls /")

    def test_prefix_options_that_carry_a_command_or_write(self):
        self.change("env -S'rm -rf /x'", "env -iS'rm -rf /x'",
                    "env --split-string='rm -rf /x'", "time -o /tmp/cat cat",
                    "time --output=/etc/x cat y", "sudo -e /etc/hosts")
        self.read("env", "env -i cat x", "nice -n5 cat x", "time -p cat x")


class TestEnvironmentPrefixStaysAChange(Classify):
    """B2. The strip of NAME=value never ran; it must not start running.

    An environment prefix can run code: LD_PRELOAD loads a library into the
    command. So a command with one is a change, and it has to stay one.
    """

    def test_an_assignment_prefix_is_a_change(self):
        self.change("FOO=1 cat x", "LD_PRELOAD=/tmp/x.so cat /etc/hosts",
                    "env FOO=1 cat x", "sudo LD_PRELOAD=/tmp/x.so ls",
                    "ls | LD_PRELOAD=/tmp/x.so xargs")

    def test_the_dead_strip_is_gone(self):
        import inspect
        source = inspect.getsource(authority)
        self.assertFalse("[:0]" in source, "the always-false slice is still there")
        self.assertFalse("what = " in inspect.getsource(authority.review),
                         "review() still assigns an unused 'what'")


def task(task_type, **meta):
    return Task(priority=5, description="a task", task_type=task_type, metadata=meta)


class FakeView:
    def __init__(self):
        self.calls = []

    def act(self, kind, ref="", text=""):
        self.calls.append((kind, ref, text))
        return {"url": "https://example.com/", "title": "t", "text": "b",
                "fetched_at": 1790000000.0, "links": [], "fields": [],
                "has_password": False, "truncated": False, "status": 200}


class TestExecutorBackstopForEveryChangingTool(unittest.TestCase):
    """B4. The mandate check runs for any CHANGE tool, not only shell_command."""

    def executor(self, rung, grants=()):
        ex = TaskExecutor(dict(NO_HW), AgentMemory(), LOG,
                          shell_policy={"enabled": True, "timeout": 5})
        ex.rung = rung
        ex.grants = authority.normalise_grants(list(grants))
        return ex

    def test_maintenance_at_observer_is_refused_by_the_executor(self):
        got = self.executor("observer").execute(task(TaskType.MAINTENANCE))
        self.assertFalse(got["success"])
        self.assertIn("outside mandate", got["error"])

    def test_maintenance_at_proposer_is_refused_too(self):
        # The rule planner files these and they reach act() without review.
        got = self.executor("proposer").execute(task(TaskType.MAINTENANCE))
        self.assertFalse(got["success"])
        self.assertIn("outside mandate", got["error"])

    def test_maintenance_with_no_rung_is_refused(self):
        got = self.executor(None).execute(task(TaskType.MAINTENANCE))
        self.assertFalse(got["success"])
        self.assertIn("outside mandate", got["error"])

    def test_maintenance_at_actor_runs(self):
        self.assertTrue(self.executor("actor").execute(task(TaskType.MAINTENANCE))["success"])

    def test_browse_act_granted_at_proposer_is_not_refused(self):
        ex = self.executor("proposer", ["browse_act"])
        view = FakeView()
        ex.browser = view
        got = ex.execute(task(TaskType.BROWSE_ACT, kind="click", ref="L1"))
        self.assertNotIn("outside mandate", str(got.get("error") or ""))
        self.assertEqual(view.calls, [("click", "L1", "")])

    def test_browse_act_at_proposer_without_the_grant_is_refused(self):
        ex = self.executor("proposer")
        view = FakeView()
        ex.browser = view
        got = ex.execute(task(TaskType.BROWSE_ACT, kind="click", ref="L1"))
        self.assertFalse(got["success"])
        self.assertIn("outside mandate", got["error"])
        self.assertEqual(view.calls, [])

    def test_a_grant_is_not_honoured_at_observer(self):
        ex = self.executor("observer", ["browse_act"])
        view = FakeView()
        ex.browser = view
        got = ex.execute(task(TaskType.BROWSE_ACT, kind="click", ref="L1"))
        self.assertIn("outside mandate", got["error"])
        self.assertEqual(view.calls, [])

    def test_reading_tools_are_not_checked(self):
        ex = self.executor("observer")
        self.assertTrue(ex.execute(task(TaskType.SYSTEM_CHECK))["success"])
        self.assertTrue(ex.execute(task(TaskType.GOAL_STEP, goal="g"))["success"])

    def test_the_shell_path_is_unchanged(self):
        ex = self.executor("observer")
        self.assertTrue(ex.execute(task(TaskType.SHELL_COMMAND, command="echo hi"))["success"])
        path = os.path.join(tempfile.mkdtemp(), "should-not-exist")
        got = ex.execute(task(TaskType.SHELL_COMMAND, command=f"touch {path}"))
        self.assertIn("outside mandate", got["error"])
        self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
