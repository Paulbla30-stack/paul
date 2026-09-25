"""Behaviour lab: dials compose the prompt, context and model parameters; runs are ledgered."""

import json
import logging
import os
import tempfile
import unittest
import urllib.request
import urllib.error

from jarvis.agent.core import AgentCore
from jarvis.brain import dials
from jarvis.brain.llm import BaseBrain, Completion, PLAN_MENU, ASK_PROMPT
from jarvis.cloud.headless import HeadlessRunner

try:
    from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: F401
    HAVE_CRYPTO = True
except Exception:  # pragma: no cover
    HAVE_CRYPTO = False

LOG = logging.getLogger("test")
NO_HW = {"display": None, "input": None, "memory": None, "storage": None}


class RecordingBrain(BaseBrain):
    """A brain that records what it is asked and answers with a canned reply."""
    provider = "fake"
    default_model = "fake-1"

    def __init__(self, reply="answer", plan=None):
        self.reply, self.plan_json, self.calls = reply, plan, []
        self.temperature = 0.2
        self.think_mode = "auto"
        super().__init__({"model": "fake-1", "max_tokens": 512}, LOG, client=object())

    def _make_client(self):
        return object()

    def _complete(self, system, messages, structured, cache=True):
        self.calls.append({"system": system, "messages": messages, "structured": structured,
                           "max_tokens": self.max_tokens, "temperature": self.temperature,
                           "think_mode": self.think_mode})
        text = json.dumps(self.plan_json) if structured and self.plan_json else self.reply
        return Completion(text=text, stop_reason="end_turn", usage={"input_tokens": 3, "output_tokens": 2})


def make_agent(brain, ledger=None, lab=True):
    agent = AgentCore({"name": "t", "profile": "cloud"}, dict(NO_HW), LOG, brain=brain, ledger=ledger)
    agent.planner._boot_tasks_generated = True
    agent.add_goal("Keep root under 80%", 3)
    # A lab run needs an open window, and opening one tells the agent. Tests
    # that exercise the lab therefore open it, which is the point: there is
    # no path to an experiment that does not go through the notice.
    if lab:
        agent.lab.open(purpose="tests", operator="test")
    return agent


class TestDials(unittest.TestCase):

    def test_registry_defaults_base_and_normalise(self):
        reg = dials.registry()
        ids = [d["id"] for d in reg["dials"]]
        self.assertEqual(ids[:6], ["identity", "constitution", "honesty", "voice", "ledger_disclosure", "guardrail_disclosure"])
        self.assertTrue(all("texts" not in d for d in reg["dials"]))  # texts are shown via preview, not the registry
        self.assertEqual(len(reg["locked"]), 4)
        base = dials.base()
        self.assertTrue(all(base[d["id"]] == 0 for d in dials.DIALS if d["kind"] == "level"))
        self.assertEqual(dials.system_text(base), "")
        norm = dials.normalise({"identity": 99, "constitution": -1, "temperature": "0.9", "max_tokens": 10, "nope": 1})
        self.assertEqual((norm["identity"], norm["constitution"], norm["temperature"], norm["max_tokens"]), (2, 0, 0.9, 128))
        self.assertNotIn("nope", norm)
        self.assertEqual(dials.normalise(None), dials.defaults())
        self.assertNotEqual(dials.fingerprint(base), dials.fingerprint(dials.defaults()))
        self.assertEqual(dials.fingerprint({"identity": 2}), dials.fingerprint(dials.defaults()))

    def test_system_text_grows_with_levels(self):
        s = dials.defaults()
        full = dials.system_text(s)
        self.assertIn("Hard gates that never relax", full)
        self.assertIn("not in my notes", full)
        self.assertIn("Glass Ledger", full)
        s["constitution"] = 1
        self.assertIn("Hard gates", dials.system_text(s))
        self.assertNotIn("Never run destructive", dials.system_text(s))
        s.update({"constitution": 0, "honesty": 0, "ledger_disclosure": 0, "guardrail_disclosure": 0, "voice": 0, "identity": 1})
        self.assertTrue(dials.system_text(s).startswith("You are Jarvis, an AI colleague"))
        self.assertEqual(len(dials.describe(s)), len(dials.DIALS))

    def test_context_levels(self):
        brain = RecordingBrain()
        agent = make_agent(brain)
        agent.run_cycle()  # rule planner: one task in history
        s = dials.defaults()
        full = dials.context(s, brain, agent, agent.observe())
        self.assertIn("recent_history", full)
        self.assertIn("goals", full)
        s["context"] = 1
        light = dials.context(s, brain, agent, agent.observe())
        self.assertEqual(set(light), {"clock", "cycle", "goals"})
        s["context"] = 2
        mid = dials.context(s, brain, agent, agent.observe())
        self.assertIn("recent_history", mid)
        self.assertNotIn("uploaded_files", mid)
        s["history_window"] = 0
        self.assertNotIn("recent_history", dials.context(s, brain, agent, agent.observe()))
        s["context"] = 0
        self.assertIsNone(dials.context(s, brain, agent, agent.observe()))
        self.assertEqual(brain.history_window, 10)  # restored


class TestExperiment(unittest.TestCase):

    def test_a_closed_lab_runs_nothing(self):
        agent = make_agent(RecordingBrain(reply="hello"), lab=False)
        result = agent.experiment("who are you?", settings=dials.defaults())
        self.assertIn("lab is closed", result["error"])
        agent.lab.open(purpose="tests", operator="test")
        self.assertNotIn("error", agent.experiment("who are you?", settings=dials.defaults()))

    def test_experiment_runs_base_and_dials_with_param_overrides(self):
        brain = RecordingBrain(reply="hello")
        agent = make_agent(brain)
        settings = dict(dials.defaults(), temperature=0.7, thinking=1, max_tokens=256)
        result = agent.experiment("who are you?", settings=settings, compare=True)
        self.assertEqual([v["variant"] for v in result["variants"]], ["base", "dials"])
        base_call, dial_call = brain.calls
        self.assertEqual(base_call["system"], "")
        self.assertEqual(base_call["messages"][0]["content"], "who are you?")  # no context at base
        self.assertIn("Hard gates", dial_call["system"])
        self.assertTrue(dial_call["messages"][0]["content"].startswith("Context as JSON"))
        self.assertEqual((dial_call["temperature"], dial_call["think_mode"], dial_call["max_tokens"]), (0.7, "on", 256))
        self.assertEqual((brain.temperature, brain.think_mode, brain.max_tokens), (0.2, "auto", 512))  # restored
        self.assertEqual(result["variants"][1]["answer"], "hello")
        self.assertEqual(result["variants"][1]["usage"]["input_tokens"], 3)
        self.assertNotEqual(result["variants"][0]["fingerprint"], result["variants"][1]["fingerprint"])
        self.assertEqual(brain.stats["calls"], 2)

    def test_plan_mode_returns_a_decision_but_executes_nothing(self):
        plan = {"reasoning": "disk", "task_type": "shell_command", "description": "check", "priority": 2,
                "command": "df -h", "goal": "", "completed_goals": [], "note": ""}
        brain = RecordingBrain(plan=plan)
        agent = make_agent(brain)
        before = len(agent.task_history)
        result = agent.experiment("", settings=dials.defaults(), compare=False, mode="plan")
        v = result["variants"][0]
        self.assertTrue(brain.calls[0]["structured"])
        self.assertIn(PLAN_MENU, brain.calls[0]["system"])
        self.assertEqual(v["decision"]["command"], "df -h")
        self.assertEqual(len(agent.task_history), before)  # nothing executed
        brain2 = RecordingBrain(reply="I would rather not")
        result = make_agent(brain2).experiment("", settings=dials.base(), compare=False, mode="plan")
        self.assertIn("decision_error", result["variants"][0])

    def test_chat_under_dials_and_default_chat_unchanged(self):
        brain = RecordingBrain(reply="ok")
        agent = make_agent(brain)
        agent.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(brain.calls[-1]["system"], ASK_PROMPT)
        agent.chat([{"role": "user", "content": "hi"}], settings=dials.base())
        self.assertEqual(brain.calls[-1]["system"], "")
        self.assertEqual(brain.calls[-1]["messages"][0]["content"], "hi")

    @unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
    def test_runs_and_dial_changes_are_ledgered(self):
        from jarvis.ledger import AgentLedger
        with tempfile.TemporaryDirectory() as tmp:
            led = AgentLedger({"enabled": True, "path": f"{tmp}/l.jsonl", "key_file": f"{tmp}/k/ed25519.key",
                               "pubkey_file": f"{tmp}/k/ed25519.pub"}, LOG, writer="t")
            brain = RecordingBrain(reply="yes")
            agent = make_agent(brain, ledger=led)
            agent.experiment("q", settings=dials.defaults(), compare=True)
            kinds = [(e["kind"], e["body"].get("kind"), e["body"].get("variant")) for e in led.tail(3)]
            self.assertEqual(kinds[-2:], [("thought", "experiment", "base"), ("thought", "experiment", "dials")])
            self.assertEqual(led.tail(1)[0]["body"]["dials"], dials.fingerprint(dials.defaults()))
            agent.chat([{"role": "user", "content": "hi"}], settings=dials.base())
            self.assertEqual(led.tail(1)[0]["body"]["dials"], dials.fingerprint(dials.base()))
            led.close()


class TestLabEndpoints(unittest.TestCase):

    def test_lab_endpoints(self):
        brain = RecordingBrain(reply="fine")
        agent = make_agent(brain, lab=False)   # the switch is part of what is tested
        runner = HeadlessRunner(agent, LOG, interval=0, status_port=0, token="t0k", token_file=None)
        port = runner.start_status_server()
        base = f"http://127.0.0.1:{port}"
        def call(path, data=None):
            req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                         headers={"Authorization": "Bearer t0k", "Content-Type": "application/json"},
                                         method="POST" if data is not None else "GET")
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        try:
            # Everything below is refused until the window is open, and
            # opening it is what tells the agent.
            with self.assertRaises(urllib.error.HTTPError) as shut:
                call("/lab/run", {"question": "how is disk?"})
            self.assertEqual(shut.exception.code, 409)
            opened = call("/lab/session", {"open": True, "purpose": "checking the voice dial"})
            self.assertTrue(opened["session"]["open"])
            self.assertTrue(opened["session"]["announced"])
            self.assertIn("behaviour lab is open", agent.notes[-1])
            state = call("/lab")
            self.assertTrue(state["session"]["open"])
            self.assertEqual(state["settings"], dials.defaults())
            self.assertFalse(state["apply_to_chat"])
            self.assertEqual(len(state["locked"]), 4)
            preview = call("/lab/preview", {"settings": dials.base()})
            self.assertEqual(preview["system"], "")
            self.assertIsNone(preview["context"])
            saved = call("/lab/settings", {"settings": {"identity": 1, "context": 1}, "apply_to_chat": True})
            self.assertEqual((saved["settings"]["identity"], saved["settings"]["context"], saved["apply_to_chat"]), (1, 1, True))
            chat = call("/chat", {"messages": [{"role": "user", "content": "hi"}]})
            self.assertTrue(chat["dials"])
            self.assertTrue(brain.calls[-1]["system"].startswith("You are Jarvis, an AI colleague"))
            run = call("/lab/run", {"question": "how is disk?", "compare": True})
            self.assertEqual(len(run["variants"]), 2)
            self.assertEqual(run["variants"][1]["settings"]["identity"], 1)
            plan = call("/lab/run", {"mode": "plan", "compare": False})
            self.assertEqual(plan["variants"][0]["variant"], "dials")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                call("/lab/run", {"question": ""})
            self.assertEqual(cm.exception.code, 400)
            # Closing says so, and takes the dials back off live chat.
            closed = call("/lab/session", {"open": False})
            self.assertFalse(closed["session"]["open"])
            self.assertFalse(closed["lab"]["apply_to_chat"])
            self.assertIn("behaviour lab is closed", agent.notes[-1])
            chat = call("/chat", {"messages": [{"role": "user", "content": "hi"}]})
            self.assertFalse(chat["dials"])
        finally:
            runner.stop_status_server()


if __name__ == "__main__":
    unittest.main()
