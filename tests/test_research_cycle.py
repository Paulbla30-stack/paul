"""The round with Paul: plan, talk it through, sign off, work, come back,
together again. And Vigil's "one mind, many paths": hypotheses he proposes
on, that only Paul settles. Scripted model, fake browser; no network."""

import json
import unittest

from vigil.agent import research as R
from vigil.agent.research_tools import ResearchTools
from tests.test_research import Script, make
from tests.test_research_tools import Browser, Sandbox

PLAN = {"threads": [{"question": "Do handover checklists reduce errors?", "why": "results", "value": 0.7,
                     "steps": [{"goal": "find studies", "tool": "search", "count": 1,
                                "input": "handover checklist errors", "output": "links", "risk": "none"},
                               {"goal": "read the best one", "tool": "read", "count": 1,
                                "input": "from search", "output": "its findings", "risk": "none"}]}],
        "hypotheses": ["Checklists cut omissions", "Verbal handover at the bedside matters more"],
        "clarification": None}
TALK = {"reply": "The plan reads two sources first; the second hypothesis needs its own search later."}
LEARN = {"findings": [{"text": "Checklists cut omissions by about 30%", "source": 0, "value": 0.6,
                       "hypothesis": 1, "stance": "supports"},
                      {"text": "Bedside handover showed no difference in one trial", "source": 1, "value": 0.5,
                       "hypothesis": 2, "stance": "weakens"},
                      {"text": "An unrelated aside", "source": 0, "value": 0.2,
                       "hypothesis": 99, "stance": "supports"}]}
REPORT = {"summary": "Two sources read. The checklist line holds so far; the bedside line is weaker.",
          "paths": [{"label": "Test the bedside hypothesis", "description": "Search for more trials."},
                    {"label": "Drop bedside, go deeper on checklists", "description": "Read two more studies."}]}


def start(*replies):
    script = Script(PLAN, *replies)
    reg, agent = make(think=script)
    reg.tools = ResearchTools(Browser(), Sandbox())
    pid = reg.create("Handovers", "What makes night handovers safe?")["id"]
    reg.run_cycle(pid)
    return reg, agent, script, pid


def run_until_paused(reg, pid, limit=6):
    for _ in range(limit):
        out = reg.run_cycle(pid)
        if out.get("paused"):
            return out
    return out


class TestTheRound(unittest.TestCase):

    def test_plan_talk_sign_off_work_come_back_and_round_again(self):
        reg, agent, script, pid = start(TALK, LEARN, REPORT,
                                        {"threads": [{"question": "Is bedside handover studied in nursing?",
                                                      "why": "results",
                                                      "steps": [{"goal": "search", "tool": "search", "count": 1,
                                                                 "input": "bedside handover nursing trial"}]}]})
        p = reg.project(pid)
        self.assertEqual(p["pause_reason"], R.PLAN_CHECK)
        self.assertEqual([h["text"] for h in p["hypotheses"]],
                         ["Checklists cut omissions", "Verbal handover at the bedside matters more"])

        p = reg.discuss(pid, "Why read before searching the bedside idea?")
        self.assertEqual([d["role"] for d in p["discussion"]], ["paul", "vigil"])
        self.assertIn("second hypothesis", p["discussion"][1]["text"])
        self.assertEqual(script.calls[1][0], reg.DISCUSS_SYSTEM)

        reg.check_plan(pid, "approve")
        out = run_until_paused(reg, pid)
        self.assertEqual(out["paused"], R.BACK, "he comes back when the signed-off plan is done")
        p = reg.project(pid)
        self.assertIn(p["id"], [w["id"] for w in reg.state()["waiting_for_paul"]])
        report = p["report"]
        self.assertEqual([a["tool"] for a in report["attempted"]], ["search", "read"])
        self.assertIn("model_calls", report["spent"])
        by = {h["id"]: h for h in report["by_hypothesis"]}
        self.assertEqual((len(by[1]["supports"]), len(by[2]["weakens"])), (1, 1))
        self.assertTrue(by[1]["supports"][0]["source"], "every finding carries its source")
        self.assertEqual(report["summary"], REPORT["summary"])
        self.assertEqual([o["label"] for o in report["paths"]],
                         ["Test the bedside hypothesis", "Drop bedside, go deeper on checklists"])
        self.assertTrue(any("back with results" in s for s, _ in agent.sent))

        p = reg.answer(pid, "Test the bedside hypothesis")
        self.assertEqual((p["status"], p["phase"], p["plan_state"]), ("active", "plan", "reported"))
        out = reg.run_cycle(pid)
        self.assertIn("Test the bedside hypothesis", script.calls[-1][1], "his pick steers the next plan")
        self.assertIn("talked_with_paul", json.loads(script.calls[-1][1]))
        self.assertEqual(out.get("paused"), R.PLAN_CHECK,
                         "each round is signed off, even inside the allowance left")

    def test_findings_carry_their_hypothesis_onto_the_ledger(self):
        reg, agent, script, pid = start(LEARN, REPORT)
        reg.check_plan(pid, "approve")
        reg.run_cycle(pid)
        learn = [b for k, b in agent.ledger.entries if k == "action" and b.get("phase") == "learn"][-1]
        self.assertEqual(learn["result"]["by_hypothesis"], {"1:supports": 1, "2:weakens": 1},
                         "an unknown hypothesis id is not a tag")

    def test_a_report_is_made_even_without_the_model(self):
        reg, agent, script, pid = start(LEARN)          # nothing queued for the report
        reg.check_plan(pid, "approve")
        out = run_until_paused(reg, pid)
        self.assertEqual(out["paused"], R.BACK)
        report = reg.project(pid)["report"]
        self.assertIsNone(report["summary"])
        self.assertEqual([o["label"] for o in report["paths"]], ["Carry on", "Close the project"])
        self.assertEqual(len(report["attempted"]), 2)

    def test_what_is_blocked_is_said(self):
        reg, agent, script, pid = start(LEARN, REPORT)
        reg.check_plan(pid, "approve")
        reg.tools.browser.up = False
        out = run_until_paused(reg, pid)
        self.assertEqual(out["paused"], R.BACK)
        blocked = reg.project(pid)["report"]["blocked"]
        self.assertTrue(any("search" in b and "not answering" in b for b in blocked), blocked)


class TestHypotheses(unittest.TestCase):

    def test_three_open_at_most(self):
        reg, agent, script, pid = start()
        reg.add_hypothesis(pid, "Staffing levels explain most of it")
        with self.assertRaises(R.Refused):
            reg.add_hypothesis(pid, "Shift length explains most of it")
        with self.assertRaises(R.Refused):
            reg.add_hypothesis(pid, "checklists cut omissions")

    def test_vigil_proposes_and_only_paul_settles(self):
        reg, agent, script, pid = start()
        reg._take_hypotheses(pid, {"hypothesis_updates": [{"id": 2, "propose": "dropped", "why": "no support"},
                                                          {"id": 1, "propose": "closed"}]})
        hs = {h["id"]: h for h in reg.project(pid)["hypotheses"]}
        self.assertEqual((hs[2]["status"], hs[2]["proposed"]), ("open", "dropped"), "a proposal, not a closure")
        self.assertIsNone(hs[1]["proposed"])
        p = reg.settle_hypothesis(2, "confirm", "agreed")
        self.assertEqual({h["id"]: h["status"] for h in p["hypotheses"]}[2], "dropped")
        ruling = [b for k, b in agent.ledger.entries if k == "verdict" and b.get("research") == "hypothesis"][-1]
        self.assertEqual((ruling["by"], ruling["status"]), ("paul", "dropped"))
        with self.assertRaises(R.Refused):
            reg.settle_hypothesis(1, "confirm")
        p = reg.settle_hypothesis(2, "reopen")
        self.assertEqual({h["id"]: h["status"] for h in p["hypotheses"]}[2], "open")

    def test_another_projects_hypothesis_cannot_be_proposed_on(self):
        reg, agent, script, pid = start()
        other = reg.create("Other", "something else entirely about sleep")["id"]
        reg._take_hypotheses(other, {"hypothesis_updates": [{"id": 1, "propose": "supported"}]})
        self.assertIsNone({h["id"]: h for h in reg.project(pid)["hypotheses"]}[1]["proposed"])


class TestTalking(unittest.TestCase):

    def test_the_conversation_holds_no_private_context_and_is_a_thought(self):
        reg, agent, script, pid = start(TALK)
        reg.discuss(pid, "How are you getting on?")
        brief = json.loads(script.calls[1][1])
        self.assertEqual(set(brief), {"title", "direction", "status", "phase", "pause", "plan", "hypotheses",
                                      "findings_quoted_from_outside", "last_report", "conversation"})
        thought = [b for k, b in agent.ledger.entries if k == "thought"][-1]
        self.assertEqual(thought["research"], "discussion")
        self.assertEqual(len(thought["asked_sha256"]), 64)

    def test_nothing_to_say_is_refused_and_a_bad_reply_still_answers(self):
        reg, agent, script, pid = start()
        with self.assertRaises(R.Refused):
            reg.discuss(pid, "   ")
        p = reg.discuss(pid, "hello")                 # the script has nothing queued: "{}"
        self.assertEqual(p["discussion"][-1]["role"], "vigil")
        self.assertTrue(p["discussion"][-1]["text"])


class TestRoutes(unittest.TestCase):

    def test_discuss_and_hypothesis_routes(self):
        from tests.test_research import TestRoutes as Base
        case = Base("test_create_steer_and_read")
        case.setUp()
        try:
            reg = case.agent.research
            reg._think = Script(TALK)
            status, p = case.call("/research/create", {"title": "H", "direction": "safe handovers"})
            status, got = case.call("/research/hypothesis/add", {"id": p["id"], "text": "Checklists help"})
            self.assertEqual((status, got["hypotheses"][0]["text"]), (200, "Checklists help"))
            hid = got["hypotheses"][0]["id"]
            status, got = case.call("/research/hypothesis", {"hypothesis": hid, "action": "drop"})
            self.assertEqual(got["hypotheses"][0]["status"], "dropped")
            status, err = case.call("/research/hypothesis", {"hypothesis": hid, "action": "confirm"})
            self.assertEqual(status, 409)
            status, got = case.call("/research/discuss", {"id": p["id"], "text": "What next?"})
            self.assertEqual((status, got["discussion"][-1]["role"]), (200, "vigil"))
        finally:
            case.doCleanups()


if __name__ == "__main__":
    unittest.main()
