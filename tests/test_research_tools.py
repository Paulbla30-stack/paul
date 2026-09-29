"""Research tools: the action plan Paul checks, the fence it becomes, and the
code sandbox. A scripted model, a fake browser and a fake systemd-run; no
network and no real model."""

import json
import subprocess
import unittest

from vigil.agent import research as R
from vigil.agent.research_tools import ResearchTools, _unwrap
from vigil.agent.sandbox import Busy, CodeSandbox, SandboxUnavailable
from tests.test_research import Clock, Script, make


class Done:
    def __init__(self, code=0, out=b"", err=b""):
        self.returncode, self.stdout, self.stderr = code, out, err


class TestSandbox(unittest.TestCase):

    def sandbox(self, runner=None, **cfg):
        return CodeSandbox(cfg, runner=runner or (lambda *a, **k: Done(0, b"42\n")),
                           which=lambda name: "/usr/bin/" + name, exists=lambda path: True)

    def test_the_fence_is_in_the_command(self):
        argv = self.sandbox().command("u")
        joined = " ".join(argv)
        for prop in ("DynamicUser=yes", "PrivateNetwork=yes", "IPAddressDeny=any", "ProtectSystem=strict",
                     "NoNewPrivileges=yes", "MemoryMax=384M", "MemorySwapMax=0", "RuntimeMaxSec=60",
                     "InaccessiblePaths=-/var/lib/vigil", "InaccessiblePaths=-/etc/vigil",
                     "CapabilityBoundingSet="):
            self.assertIn(prop, argv, prop)
        self.assertEqual(argv[-4:], ["/usr/bin/python3.11", "-I", "-S", "-"],
                         "isolated, no site: standard library only, script on stdin")
        self.assertNotIn("--quiet", joined)

    def test_limits_are_capped_whatever_config_says(self):
        sb = self.sandbox(memory_mb=4096, timeout_s=9999)
        self.assertEqual((sb.limits["memory_mb"], sb.limits["timeout_s"]), (512, 120))

    def test_no_systemd_run_means_no_run_never_an_unsandboxed_one(self):
        ran = []
        sb = CodeSandbox({}, runner=lambda *a, **k: ran.append(a), which=lambda n: None, exists=lambda p: True)
        self.assertIn("systemd-run", sb.available())
        with self.assertRaises(SandboxUnavailable):
            sb.run("print(1)")
        self.assertEqual(ran, [])

    def test_the_script_goes_in_on_stdin_and_output_comes_back(self):
        seen = {}

        def runner(argv, **k):
            seen.update(k)
            return Done(0, b"42\n", b"Running as unit: x\nFinished with result: success\n")
        got = self.sandbox(runner).run("print(6*7)")
        self.assertEqual(seen["input"], b"print(6*7)")
        self.assertEqual((got["exit"], got["stdout"], got["stderr"], got["ended"]), (0, "42\n", "", "success"))
        self.assertIsNone(got["stopped"])
        self.assertEqual(len(got["sha256"]), 64)

    def test_a_kill_for_memory_or_time_is_said_plainly(self):
        oom = self.sandbox(lambda *a, **k: Done(1, b"", b"Finished with result: oom-kill\n")).run("x")
        self.assertEqual(oom["stopped"], "stopped: it went over the memory limit")
        slow = self.sandbox(lambda *a, **k: Done(1, b"", b"Finished with result: timeout\n")).run("x")
        self.assertTrue(slow["timed_out"])

        def hang(*a, **k):
            raise subprocess.TimeoutExpired("systemd-run", 1)
        self.assertTrue(self.sandbox(hang).run("x")["timed_out"])

    def test_one_run_at_a_time(self):
        sb = self.sandbox()
        sb._one.acquire()
        with self.assertRaises(Busy):
            sb.run("print(1)")
        sb._one.release()
        self.assertEqual(sb.run("print(1)")["exit"], 0)

    def test_empty_or_huge_scripts_are_refused(self):
        with self.assertRaises(ValueError):
            self.sandbox().run("  ")
        with self.assertRaises(ValueError):
            self.sandbox().run("x" * 30_000)


class Journal:
    def __init__(self):
        self.rows = []

    def record(self, did, url="", outcome="ok", **k):
        self.rows.append((did, url, outcome, k.get("by")))


class Browser:
    """Serves a DuckDuckGo-like results page and plain pages."""

    def __init__(self, up=True):
        self.up = up
        self.opened = []
        self.journal = Journal()

    def available(self):
        return self.up

    def open(self, url):
        self.opened.append(url)
        if "search.brave.com" in url or "duckduckgo" in url:
            return {"url": url, "links": [
                {"ref": "L1", "text": "Handover checklists cut errors",
                 "href": "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fchecklists&rut=x"},
                {"ref": "L2", "text": "Brave settings", "href": "https://search.brave.com/settings"},
                {"ref": "L5", "text": "Brave home", "href": "https://brave.com/"},
                {"ref": "L3", "text": "SBAR study", "href": "https://journal.example/sbar"},
                {"ref": "L4", "text": "again", "href": "https://example.org/checklists"}]}
        return {"url": url, "title": "A page", "text": "Checklists reduced handover omissions by 30%."}


class Sandbox:
    def __init__(self):
        self.scripts = []

    def available(self):
        return None

    def run(self, code):
        self.scripts.append(code)
        return {"exit": 0, "stdout": "mean 0.3\n", "stderr": "", "stopped": None, "sha256": "ab" * 32}


class TestTools(unittest.TestCase):

    def test_duckduckgo_redirects_are_unwrapped_too(self):
        tools = ResearchTools(Browser(), search_url="https://html.duckduckgo.com/html/?q={q}")
        self.assertEqual(tools.search("x")[0]["source"], "https://example.org/checklists")

    def test_search_returns_real_destinations_not_the_engine(self):
        tools = ResearchTools(Browser())
        got = tools.search("night handover checklist")
        self.assertEqual([g["source"] for g in got], ["https://example.org/checklists", "https://journal.example/sbar"])
        self.assertIn("q=night+handover+checklist", tools.browser.opened[0])
        self.assertEqual(tools.browser.journal.rows[0][3], "research", "shows in the daily debrief as research")

    def test_unwrap(self):
        self.assertEqual(_unwrap("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.org%2Fx"), "https://a.org/x")
        self.assertEqual(_unwrap("https://a.org/page?q=https://b.org"), "https://a.org/page?q=https://b.org")

    def test_availability_is_said(self):
        self.assertIn("not switched on", ResearchTools(None).available("search"))
        self.assertIn("not answering", ResearchTools(Browser(up=False)).available("read"))
        self.assertIn("not a research tool", ResearchTools(Browser()).available("post"))
        with self.assertRaises(ValueError):
            ResearchTools(Browser()).read("file:///etc/passwd")


PLAN = {"threads": [{"question": "Do handover checklists reduce errors?", "why": "results", "value": 0.7,
                     "steps": [
                         {"goal": "find studies on handover checklists", "tool": "search", "count": 3,
                          "input": "handover checklist errors | SBAR handover study", "output": "titles and links",
                          "risk": "none"},
                         {"goal": "read the best study", "tool": "read", "count": 2, "input": "from search",
                          "output": "the study's findings", "risk": "none"},
                         {"goal": "average the reported reductions", "tool": "code", "count": 1,
                          "input": "numbers from the pages", "output": "a mean", "risk": "none"},
                         {"goal": "post a question on a forum", "tool": "post", "count": 1, "input": "x"}]}],
        "clarification": None}
FINDINGS = {"findings": [{"text": "Checklists cut omissions by about 30%", "source": 0, "value": 0.6}]}


def make_tools():
    return ResearchTools(Browser(), Sandbox())


class TestActionPlan(unittest.TestCase):

    def start(self, *replies):
        script = Script(PLAN, *replies)
        reg, agent = make(think=script)
        reg.tools = make_tools()
        p = reg.create("Handovers", "What makes night handovers safe?")
        out = reg.run_cycle(p["id"])
        return reg, agent, script, p["id"], out

    def test_the_brainstorm_writes_a_plan_and_waits_for_paul(self):
        reg, agent, script, pid, out = self.start()
        self.assertEqual(out["paused"], R.PLAN_CHECK)
        self.assertIn("search:", script.calls[0][0], "the model is told which tools exist")
        p = reg.project(pid)
        self.assertEqual([st["tool"] for st in p["plan"]], ["search", "read", "code"],
                         "a tool that is not a research tool never reaches the plan")
        self.assertEqual(p["plan"][0]["count"], 2, "one use per query written in the plan")
        self.assertEqual([st["dry"] for st in p["plan"]], ["new_action", "waits", "waits"])
        self.assertEqual(reg.tools.browser.opened, [], "a dry run uses nothing")
        self.assertIn(p["id"], [w["id"] for w in reg.state()["waiting_for_paul"]])
        self.assertTrue(any("plan ready" in s for s, _ in agent.sent))
        self.assertEqual(reg.run_cycle(pid)["ran"], False, "nothing runs while it waits")

    def test_paul_approves_with_his_counts_and_it_goes_on_the_ledger(self):
        reg, agent, script, pid, _ = self.start()
        code_step = reg.project(pid)["plan"][2]["id"]
        p = reg.check_plan(pid, "approve", counts={str(code_step): 0}, note="skip the code for now")
        self.assertEqual((p["status"], p["phase"], p["plan_state"]), ("active", "learn", "approved"))
        self.assertEqual([st["status"] for st in p["plan"]], ["approved", "approved", "dropped"])
        self.assertEqual(p["allowance"], {"search": 4, "read": 5, "code": 0}, "the plan plus a little room")
        verdicts = [b for k, b in agent.ledger.entries if k == "verdict" and b.get("research") == "plan"]
        self.assertEqual((verdicts[-1]["ruling"], verdicts[-1]["by"]), ("approved", "paul"))
        self.assertEqual(len(verdicts[-1]["plan_sha256"]), 64)

    def test_then_he_goes_off_and_does_it_inside_the_fence(self):
        reg, agent, script, pid, _ = self.start(FINDINGS)
        reg.check_plan(pid, "approve")
        out = reg.run_cycle(pid)
        self.assertEqual(out["tools"], {"search": 2, "read": 2}, "at most four uses in one Learn")
        opened = reg.tools.browser.opened
        self.assertIn("q=handover+checklist+errors", opened[0])
        self.assertIn("q=SBAR+handover+study", opened[1], "the queries are exactly the approved ones")
        self.assertEqual(opened[2:], ["https://example.org/checklists", "https://journal.example/sbar"])
        self.assertEqual(out["findings"], 1)
        p = reg.project(pid)
        self.assertEqual(p["used"], {"search": 2, "read": 2, "code": 0})
        self.assertEqual(p["plan"][0]["status"], "done")
        learn = [b for k, b in agent.ledger.entries if k == "action" and b.get("phase") == "learn"]
        self.assertEqual(learn[-1]["result"]["tools"], {"search": 2, "read": 2})

    def test_code_is_written_from_evidence_and_run_in_the_sandbox(self):
        reg, agent, script, pid, _ = self.start({"code": "print(sum([0.3])/1)"}, FINDINGS)
        reg.check_plan(pid, "approve", counts={str(st["id"]): 0 for st in reg.project(pid)["plan"][:2]})
        out = reg.run_cycle(pid)
        self.assertEqual(out["tools"], {"code": 1})
        self.assertEqual(reg.tools.sandbox.scripts, ["print(sum([0.3])/1)"])
        self.assertIn("Standard library only", script.calls[1][0])
        runs = reg.project(pid)["runs"]
        self.assertEqual((runs[0]["exit"], runs[0]["stdout"]), (0, "mean 0.3\n"))

    def test_sending_it_back_needs_a_note_and_the_note_shapes_the_next_plan(self):
        reg, agent, script, pid, _ = self.start()
        with self.assertRaises(R.Refused):
            reg.check_plan(pid, "return")
        p = reg.check_plan(pid, "return", note="look at NHS guidance, not forums")
        self.assertEqual((p["status"], p["phase"], p["plan"]), ("active", "plan", []))
        self.assertEqual(p["threads"][0]["status"], "returned")
        reg.run_cycle(pid)
        self.assertIn("look at NHS guidance", script.calls[-1][1])
        self.assertIsNone(reg._get(pid)["plan_note"], "a note is used once")

    def test_a_later_plan_inside_the_allowance_goes_ahead_and_one_outside_waits(self):
        more = {"threads": [{"question": "What does SBAR add over a checklist?", "why": "results",
                             "steps": [{"goal": "search SBAR", "tool": "search", "count": 1, "input": "SBAR vs checklist"}]}]}
        wider = {"threads": [{"question": "Is there a dataset of handover incidents?", "why": "intrigue",
                              "caught_by": "a footnote",
                              "steps": [{"goal": "count incidents", "tool": "code", "count": 2, "input": "x"}]}]}
        reg, agent, script, pid, _ = self.start()
        reg.check_plan(pid, "approve")
        with reg.store.lock:
            reg.store.run("UPDATE projects SET phase='plan' WHERE id=?", (pid,))
        script.replies[:] = [more]
        self.assertNotIn("paused", reg.run_cycle(pid))
        self.assertEqual(reg.project(pid)["plan"][-1]["status"], "approved")
        with reg.store.lock:
            reg.store.run("UPDATE projects SET phase='plan', allowance=? WHERE id=?",
                          (json.dumps({"search": 4, "read": 5, "code": 0}), pid))
        script.replies[:] = [wider]
        self.assertEqual(reg.run_cycle(pid)["paused"], R.PLAN_CHECK)

    def test_the_allowance_is_a_hard_stop(self):
        reg, agent, script, pid, _ = self.start(FINDINGS)
        reg.check_plan(pid, "approve", allowance={"search": 1, "read": 0, "code": 0})
        out = reg.run_cycle(pid)
        self.assertEqual(out["tools"], {"search": 1})
        self.assertEqual(len(reg.tools.browser.opened), 1)

    def test_an_unavailable_tool_shows_in_the_dry_run(self):
        script = Script(PLAN)
        reg, agent = make(think=script)
        reg.tools = ResearchTools(Browser(up=False), None)
        p = reg.create("H", "safe handovers")
        reg.run_cycle(p["id"])
        dry = [(st["dry"], st["dry_why"]) for st in reg.project(p["id"])["plan"]]
        self.assertEqual(dry[0][0], "unavailable")
        self.assertIn("sandbox is not switched on", dry[2][1])

    def test_a_plan_can_only_be_checked_when_one_is_waiting(self):
        reg, agent = make()
        p = reg.create("H", "d")
        with self.assertRaises(R.Refused):
            reg.check_plan(p["id"], "approve")

    def test_without_tools_nothing_changes(self):
        reg, agent = make(think=Script(PLAN))
        p = reg.create("H", "d")
        out = reg.run_cycle(p["id"])
        self.assertNotIn("paused", out)
        self.assertEqual(reg.project(p["id"])["plan"], [])

    def test_an_old_database_gains_the_new_columns(self):
        import os
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.db")
            db = sqlite3.connect(path)
            db.executescript(R.ResearchStore.SCHEMA.split("CREATE TABLE IF NOT EXISTS plan_steps")[0])
            db.close()
            store = R.ResearchStore(path)
            cols = {r[1] for r in store.db.execute("PRAGMA table_info(projects)")}
            self.assertTrue({"plan_state", "allowance", "plan_version", "plan_note"} <= cols)


class TestPlanRoute(unittest.TestCase):

    def test_the_route_approves_and_refuses(self):
        from tests.test_research import TestRoutes
        case = TestRoutes("test_create_steer_and_read")
        case.setUp()
        try:
            reg = case.agent.research
            reg._think = Script(PLAN)
            reg.tools = make_tools()
            status, p = case.call("/research/create", {"title": "H", "direction": "safe handovers"})
            reg.run_cycle(p["id"])
            status, err = case.call("/research/plan", {"id": p["id"], "action": "return"})
            self.assertEqual(status, 409)
            status, got = case.call("/research/plan", {"id": p["id"], "action": "approve",
                                                       "counts": {}, "allowance": {"code": 1}})
            self.assertEqual((status, got["plan_state"], got["allowance"]["code"]), (200, "approved", 1))
        finally:
            case.doCleanups()


if __name__ == "__main__":
    unittest.main()
