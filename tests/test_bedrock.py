"""Tests for the Bedrock brain against a fake bedrock-runtime client."""

import json
import logging
import unittest
from unittest import mock

from openclaw.agent.core import AgentCore
from openclaw.agent.planner import TaskType
from openclaw.brain import build_brain
from openclaw.brain.bedrock import BedrockBrain, JSON_INSTRUCTIONS
from openclaw.brain.llm import BaseBrain, ClaudeBrain
from openclaw.main import OpenClawSystem, load_config

try:
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:  # pragma: no cover
    ClientError = NoCredentialsError = None

needs_boto = unittest.skipUnless(ClientError is not None, "botocore not installed")

NO_HW = {"display": None, "input": None, "memory": None, "storage": None}
LOG = logging.getLogger("test")


def plan(**overrides):
    d = {"reasoning": "Check disk first.", "task_type": "shell_command",
         "description": "Measure root usage", "priority": 2, "command": "echo bedrock",
         "goal": "Keep root under 80%", "completed_goals": [], "note": ""}
    d.update(overrides)
    return d


def converse_response(text, stop="end_turn", in_tok=100, out_tok=30):
    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}},
            "stopReason": stop, "usage": {"inputTokens": in_tok, "outputTokens": out_tok}}


class FakeBedrock:
    """Stands in for boto3's bedrock-runtime client."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0) if self.responses else converse_response(json.dumps(plan()))
        if isinstance(item, Exception):
            raise item
        return item


def client_error(code, message="boom"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "Converse")


def make_brain(responses, **cfg):
    config = {"model": "meta.llama3-3-70b-instruct-v1:0", "region": "eu-west-2",
              "max_retries": 0}
    config.update(cfg)
    fake = FakeBedrock(responses)
    return BedrockBrain(config, LOG, client=fake), fake


def make_agent(brain, shell=None):
    agent = AgentCore({"name": "t", "profile": "cloud"}, dict(NO_HW), LOG,
                      brain=brain, shell_policy=shell)
    agent.planner._boot_tasks_generated = True
    agent.add_goal("Keep root under 80%", 3)
    return agent


class TestBedrockBrain(unittest.TestCase):

    def test_plan_request_shape(self):
        brain, fake = make_brain([converse_response(json.dumps(plan()))])
        agent = make_agent(brain)
        decision = brain.plan(agent, agent.observe())
        self.assertEqual(decision.task.task_type, TaskType.SHELL_COMMAND)
        self.assertEqual(decision.task.metadata["command"], "echo bedrock")
        call = fake.calls[0]
        self.assertEqual(call["modelId"], "meta.llama3-3-70b-instruct-v1:0")
        self.assertEqual(call["inferenceConfig"], {"maxTokens": 4096, "temperature": 0.2})
        self.assertTrue(call["system"][0]["text"].endswith(JSON_INSTRUCTIONS))
        self.assertIn("Keep root under 80%", call["messages"][0]["content"][0]["text"])
        self.assertEqual(brain.stats["input_tokens"], 100)
        self.assertEqual(brain.thinking, "omit")
        self.assertEqual(brain.status()["provider"], "bedrock")
        self.assertEqual(brain.status()["region"], "eu-west-2")

    def test_prose_and_fences_around_json_are_tolerated(self):
        text = "Sure! Here is my plan:\n```json\n" + json.dumps(plan()) + "\n```\nDone."
        brain, fake = make_brain([converse_response(text)])
        agent = make_agent(brain)
        decision = brain.plan(agent, {})
        self.assertEqual(decision.task.description, "Measure root usage")
        self.assertEqual(len(fake.calls), 1)
        nested = json.dumps(plan(reasoning='braces { } and "quotes" inside'))
        self.assertEqual(BaseBrain._parse_plan("x " + nested + " y")["priority"], 2)

    def test_invalid_json_is_re_asked_once(self):
        brain, fake = make_brain([converse_response("I think we should look at disk usage."),
                                  converse_response(json.dumps(plan(task_type="observation",
                                                                    command="")))])
        agent = make_agent(brain)
        decision = brain.plan(agent, {})
        self.assertEqual(decision.task.task_type, TaskType.OBSERVATION)
        self.assertEqual(len(fake.calls), 2)
        retry = fake.calls[1]["messages"]
        self.assertEqual([m["role"] for m in retry], ["user", "assistant", "user"])
        self.assertIn("not a valid JSON object", retry[2]["content"][0]["text"])
        self.assertEqual(brain.stats["calls"], 1)          # one budget slot
        self.assertEqual(brain.stats["input_tokens"], 200)  # both requests counted

    def test_still_invalid_after_retry_falls_back(self):
        brain, fake = make_brain([converse_response("nope")] * 4)
        agent = make_agent(brain)
        self.assertIsNone(brain.plan(agent, {}))
        self.assertIn("invalid plan", brain.last_error)
        self.assertEqual(len(fake.calls), 2)
        result = agent.run_cycle()  # rule planner takes over
        self.assertTrue(result["action"].startswith("Goal step:"))
        self.assertEqual(len(fake.calls), 4)

    def test_truncation_and_content_filter(self):
        brain, _ = make_brain([converse_response('{"reasoning": "cut', stop="max_tokens")])
        self.assertIsNone(brain.plan(make_agent(brain), {}))
        self.assertEqual(brain.stats["truncated"], 1)
        brain, fake = make_brain([converse_response("", stop="guardrail_intervened")])
        self.assertIsNone(brain.plan(make_agent(brain), {}))
        self.assertEqual(brain.stats["refusals"], 1)
        self.assertIn("guardrail", brain.last_error)
        self.assertEqual(len(fake.calls), 1)  # a filtered reply is not re-asked

    @needs_boto
    def test_error_classification(self):
        cases = [
            ("ModelNotReadyException", lambda b: (not b.available(), "loading")),
            ("ThrottlingException", lambda b: (b.stats["rate_limited"] == 1, "throttled")),
            ("AccessDeniedException", lambda b: (b.status()["disabled_reason"] is not None, "access denied")),
            ("ResourceNotFoundException", lambda b: (b.status()["disabled_reason"] is not None, "not found")),
            ("ValidationException", lambda b: (not b.available(), "rejected")),
            ("ServiceUnavailableException", lambda b: (not b.available(), "ServiceUnavailable")),
        ]
        for code, check in cases:
            brain, _ = make_brain([client_error(code)])
            self.assertIsNone(brain.plan(make_agent(brain), {}), code)
            ok, needle = check(brain)
            self.assertTrue(ok, code)
            self.assertIn(needle, brain.last_error, code)
        brain, _ = make_brain([NoCredentialsError()])
        self.assertIsNone(brain.plan(make_agent(brain), {}))
        self.assertIn("credentials", brain.status()["disabled_reason"])

    @needs_boto
    def test_imported_model_not_ready_then_ready(self):
        brain, fake = make_brain([client_error("ModelNotReadyException"),
                                  converse_response(json.dumps(plan()))],
                                 model="arn:aws:bedrock:eu-west-2:1:imported-model/abc",
                                 bedrock={"not_ready_backoff": 0})
        agent = make_agent(brain, shell={"enabled": True})
        first = agent.run_cycle()
        self.assertTrue(first["action"].startswith("Goal step:"))  # rule planner while loading
        second = agent.run_cycle()
        self.assertEqual(second["action"], "Measure root usage")
        self.assertIn("bedrock", second["result"]["output"]["stdout"])
        self.assertEqual(fake.calls[-1]["modelId"], "arn:aws:bedrock:eu-west-2:1:imported-model/abc")

    def test_ask(self):
        brain, fake = make_brain([converse_response("All quiet.")])
        agent = make_agent(brain)
        self.assertEqual(agent.ask("status?"), "All quiet.")
        self.assertNotIn(JSON_INSTRUCTIONS, fake.calls[0]["system"][0]["text"])

    def test_no_region_disables(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            brain = BedrockBrain({"model": "x"}, LOG)
        self.assertIsNone(brain.client)
        self.assertIn("region", brain.status()["disabled_reason"])

    def test_without_boto3(self):
        with mock.patch("openclaw.brain.bedrock.boto3", None):
            brain = BedrockBrain({"model": "x", "region": "eu-west-2"}, LOG)
        self.assertIsNone(brain.client)
        self.assertIn("boto3", brain.status()["disabled_reason"])


class TestFactory(unittest.TestCase):

    def test_build_brain_selects_provider(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            self.assertIsInstance(build_brain({"provider": "anthropic"}, LOG), ClaudeBrain)
        with mock.patch("openclaw.brain.bedrock.boto3", None):
            b = build_brain({"provider": "bedrock", "model": "m"}, LOG, region="eu-west-2")
        self.assertIsInstance(b, BedrockBrain)
        self.assertEqual(b.region, "eu-west-2")
        with self.assertRaises(ValueError):
            build_brain({"provider": "ollama"}, LOG)

    def test_system_wiring_picks_bedrock_and_instance_region(self):
        cfg = load_config("/nonexistent")
        cfg["llm"].update(enabled=True, provider="bedrock", model="meta.llama3-3-70b-instruct-v1:0")
        cfg["cloud"]["instance"] = {"region": "eu-west-2"}
        fake_client = FakeBedrock([])
        with mock.patch("openclaw.brain.bedrock.boto3") as boto:
            boto.client.return_value = fake_client
            brain = OpenClawSystem(cfg, LOG).build_brain()
        self.assertIsInstance(brain, BedrockBrain)
        self.assertEqual(brain.region, "eu-west-2")
        self.assertIs(brain.client, fake_client)
        self.assertEqual(boto.client.call_args.args[0], "bedrock-runtime")
        self.assertEqual(brain.key_source, "instance-role/bedrock:eu-west-2")


if __name__ == "__main__":
    unittest.main()
