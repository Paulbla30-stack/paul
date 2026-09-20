"""Tests for the Bedrock brain against a fake bedrock-runtime client."""

import json
import logging
import unittest
from unittest import mock

from jarvis.agent.core import AgentCore
from jarvis.agent.planner import TaskType
from jarvis.brain import build_brain
from jarvis.brain.bedrock import (BedrockBrain, JSON_INSTRUCTIONS, NO_THINK_PREFILL,
                                    render_prompt, guess_template, strip_thinking)
from jarvis.brain.llm import BaseBrain, ClaudeBrain
from jarvis.main import JarvisSystem, load_config

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
    """Stands in for boto3's bedrock-runtime client (converse and invoke_model)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.invoke_calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0) if self.responses else converse_response(json.dumps(plan()))
        if isinstance(item, Exception):
            raise item
        return item

    def invoke_model(self, **kwargs):
        import io
        self.invoke_calls.append({**kwargs, "body": json.loads(kwargs["body"])})
        item = self.responses.pop(0) if self.responses else {"generation": json.dumps(plan()), "stop_reason": "stop"}
        if isinstance(item, Exception):
            raise item
        return {"body": io.BytesIO(json.dumps(item).encode()), "contentType": "application/json"}


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
                                  {"generation": json.dumps(plan()), "stop_reason": "stop"}],
                                 model="arn:aws:bedrock:eu-west-2:1:imported-model/abc",
                                 bedrock={"not_ready_backoff": 0})
        agent = make_agent(brain, shell={"enabled": True})
        first = agent.run_cycle()
        self.assertTrue(first["action"].startswith("Goal step:"))  # rule planner while loading
        self.assertIn("loading", brain.last_error)
        second = agent.run_cycle()
        self.assertEqual(second["action"], "Measure root usage")
        self.assertIn("bedrock", second["result"]["output"]["stdout"])
        self.assertEqual(fake.calls, [])  # imported models never go through Converse
        self.assertEqual(fake.invoke_calls[-1]["modelId"], "arn:aws:bedrock:eu-west-2:1:imported-model/abc")

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
        with mock.patch("jarvis.brain.bedrock.boto3", None):
            brain = BedrockBrain({"model": "x", "region": "eu-west-2"}, LOG)
        self.assertIsNone(brain.client)
        self.assertIn("boto3", brain.status()["disabled_reason"])


class TestTheSystemBlock(unittest.TestCase):
    """The bare model has no system prompt, and Converse will not take an empty one.

    botocore validates system[0].text at a minimum length of one before the
    request leaves the box, so sending "" raises ParamValidationError, which
    the error handler reads as "Bedrock unreachable" and backs the brain off
    for thirty seconds. The lab's base variant is every dial at level 0 --
    no system prompt, no context -- so the one comparison the lab exists to
    make broke when this estate moved from an imported model to a catalog
    one. It failed as a transport error, which is why it looked like the
    network rather than like a bug.
    """

    def test_an_empty_system_prompt_sends_no_system_block(self):
        brain, fake = make_brain([converse_response("bare")])
        brain._chat("", [{"role": "user", "content": "who are you?"}], structured=False)
        self.assertNotIn("system", fake.calls[0])

    def test_a_whitespace_only_system_prompt_counts_as_none(self):
        brain, fake = make_brain([converse_response("bare")])
        brain._chat("   \n  ", [{"role": "user", "content": "hi"}], structured=False)
        self.assertNotIn("system", fake.calls[0])

    def test_a_real_system_prompt_is_still_sent(self):
        brain, fake = make_brain([converse_response("hello")])
        brain._chat("You are Jarvis.", [{"role": "user", "content": "hi"}], structured=False)
        self.assertEqual(fake.calls[0]["system"], [{"text": "You are Jarvis."}])

    def test_botocore_itself_rejects_the_empty_block(self):
        """Not a guess about the API: the client refuses it without a call."""
        try:
            import boto3
            from botocore.exceptions import ParamValidationError
        except ImportError:                                  # pragma: no cover
            self.skipTest("boto3 not installed")
        client = boto3.client("bedrock-runtime", region_name="us-west-2",
                              aws_access_key_id="x", aws_secret_access_key="y")
        with self.assertRaises(ParamValidationError):
            client.converse(modelId="m", system=[{"text": ""}],
                            messages=[{"role": "user", "content": [{"text": "hi"}]}],
                            inferenceConfig={"maxTokens": 10, "temperature": 0.2})


class TestImportedModelInvoke(unittest.TestCase):
    ARN = "arn:aws:bedrock:us-west-2:1:imported-model/abc123"

    def test_render_prompt_templates(self):
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "plan"}]
        chatml = render_prompt("chatml", "SYS", msgs)
        self.assertTrue(chatml.startswith("<|im_start|>system\nSYS<|im_end|>\n<|im_start|>user\nhi<|im_end|>\n"))
        self.assertTrue(chatml.endswith("<|im_start|>user\nplan<|im_end|>\n<|im_start|>assistant\n"))
        llama = render_prompt("llama3", "SYS", msgs)
        self.assertIn("<|start_header_id|>system<|end_header_id|>\n\nSYS<|eot_id|>", llama)
        self.assertTrue(llama.endswith("<|start_header_id|>assistant<|end_header_id|>\n\n"))
        mistral = render_prompt("mistral", "SYS", msgs)
        self.assertTrue(mistral.startswith("[INST] SYS\n\nhi [/INST] hello</s>[INST] plan [/INST]"))
        self.assertEqual(guess_template("arn:aws:bedrock:us-west-2:1:imported-model/x"), "chatml")
        self.assertEqual(guess_template("my-llama-3-1-8b"), "llama3")
        self.assertEqual(guess_template("Mixtral-8x7B"), "mistral")

    def test_auto_selects_invoke_for_imported_arn(self):
        brain, fake = make_brain([], model=self.ARN)
        self.assertEqual(brain.api, "invoke")
        self.assertEqual(brain.chat_template, "chatml")
        self.assertEqual(brain.status()["api"], "invoke")
        catalog, _ = make_brain([])
        self.assertEqual(catalog.api, "converse")
        forced, _ = make_brain([], model=self.ARN, bedrock={"api": "converse", "chat_template": "llama3"})
        self.assertEqual((forced.api, forced.chat_template), ("converse", "llama3"))

    def test_plan_via_invoke_openai_shape(self):
        resp = {"choices": [{"text": json.dumps(plan(task_type="observation", command="")),
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 210, "completion_tokens": 40}}
        brain, fake = make_brain([resp], model=self.ARN)
        agent = make_agent(brain)
        decision = brain.plan(agent, agent.observe())
        self.assertEqual(decision.task.task_type, TaskType.OBSERVATION)
        self.assertEqual(fake.calls, [])  # converse never used
        call = fake.invoke_calls[0]
        self.assertEqual(call["modelId"], self.ARN)
        body = call["body"]
        self.assertEqual(body["max_tokens"], 4096)
        self.assertTrue(body["prompt"].startswith("<|im_start|>system\n"))
        self.assertIn(JSON_INSTRUCTIONS.strip(), body["prompt"])
        # plans are always asked with thinking off (Qwen3-style prefill)
        self.assertTrue(body["prompt"].endswith("<|im_start|>assistant\n" + NO_THINK_PREFILL))
        self.assertEqual(brain.stats["input_tokens"], 210)
        self.assertEqual(brain.stats["output_tokens"], 40)

    def test_plan_via_invoke_llama_shape_and_truncation(self):
        brain, fake = make_brain([{"generation": json.dumps(plan()), "prompt_token_count": 5,
                                   "generation_token_count": 7, "stop_reason": "stop"}], model=self.ARN)
        agent = make_agent(brain, shell={"enabled": True})
        result = agent.run_cycle()
        self.assertEqual(result["action"], "Measure root usage")
        self.assertEqual(brain.stats["input_tokens"], 5)
        brain, fake = make_brain([{"generation": '{"reasoning": "cut', "stop_reason": "length"}], model=self.ARN)
        self.assertIsNone(brain.plan(make_agent(brain), {}))
        self.assertEqual(brain.stats["truncated"], 1)
        self.assertEqual(len(fake.invoke_calls), 1)  # truncated replies are not re-asked

    def test_invoke_re_asks_with_rendered_history(self):
        brain, fake = make_brain([{"generation": "Let me think...", "stop_reason": "stop"},
                                  {"generation": json.dumps(plan(task_type="none", command="")), "stop_reason": "stop"}],
                                 model=self.ARN)
        decision = brain.plan(make_agent(brain), {})
        self.assertTrue(decision.idle)
        self.assertEqual(len(fake.invoke_calls), 2)
        second = fake.invoke_calls[1]["body"]["prompt"]
        self.assertIn("<|im_start|>assistant\nLet me think...<|im_end|>", second)
        self.assertIn("not a valid JSON object", second)

    def test_chat_via_invoke(self):
        brain, fake = make_brain([{"choices": [{"text": "All fine.", "finish_reason": "stop"}]}], model=self.ARN)
        agent = make_agent(brain)
        self.assertEqual(agent.chat([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                                     {"role": "user", "content": "status?"}]), "All fine.")
        prompt = fake.invoke_calls[0]["body"]["prompt"]
        self.assertIn("<|im_start|>assistant\nhello<|im_end|>", prompt)
        self.assertNotIn(JSON_INSTRUCTIONS.strip(), prompt)
        # chat in "auto" mode leaves the first call natural (no prefill)
        self.assertTrue(prompt.endswith("<|im_start|>assistant\n"))

    def test_strip_thinking(self):
        self.assertEqual(strip_thinking("<think>\nplan...\n</think>\n\n{\"a\": 1}"), ('{"a": 1}', True, False))
        self.assertEqual(strip_thinking("plain"), ("plain", False, False))
        self.assertEqual(strip_thinking("<think>never closed"), ("", True, True))
        self.assertEqual(strip_thinking(""), ("", False, False))

    def test_think_block_is_stripped_before_plan_parse(self):
        text = "<think>\nThe disk goal is open.\n</think>\n\n" + json.dumps(plan(task_type="observation", command=""))
        brain, fake = make_brain([{"choices": [{"text": text, "finish_reason": "stop"}]}], model=self.ARN)
        decision = brain.plan(make_agent(brain), {})
        self.assertEqual(decision.task.task_type, TaskType.OBSERVATION)
        self.assertEqual(len(fake.invoke_calls), 1)  # no JSON re-ask needed

    def test_unfinished_think_block_counts_as_truncated(self):
        brain, fake = make_brain([{"generation": "<think>still reasoning about", "stop_reason": "length"}], model=self.ARN)
        self.assertIsNone(brain.plan(make_agent(brain), {}))
        self.assertEqual(brain.stats["truncated"], 1)
        # an unclosed think block with stop=stop is still an empty answer -> truncated, not re-asked
        brain, fake = make_brain([{"generation": "<think>oops", "stop_reason": "stop"}], model=self.ARN)
        self.assertIsNone(brain.plan(make_agent(brain), {}))
        self.assertEqual(len(fake.invoke_calls), 1)

    def test_thinking_modes(self):
        turns = [{"role": "user", "content": "hi"}]
        # off: every call gets the prefill
        brain, fake = make_brain([{"generation": "hey", "stop_reason": "stop"}], model=self.ARN, bedrock={"thinking": "off"})
        self.assertEqual(brain.think_mode, "off")
        make_agent(brain).chat(turns)
        self.assertTrue(fake.invoke_calls[0]["body"]["prompt"].endswith(NO_THINK_PREFILL))
        # on: chat is never prefilled, output think block stripped, plans still forced off
        brain, fake = make_brain([{"generation": "<think>x</think>\nhey", "stop_reason": "stop"},
                                  {"generation": json.dumps(plan(task_type="none", command="")), "stop_reason": "stop"},
                                  {"generation": "again", "stop_reason": "stop"}],
                                 model=self.ARN, bedrock={"thinking": True})
        self.assertEqual(brain.think_mode, "on")
        agent = make_agent(brain)
        self.assertEqual(agent.chat(turns), "hey")
        self.assertFalse(fake.invoke_calls[0]["body"]["prompt"].endswith(NO_THINK_PREFILL))
        brain.plan(agent, {})
        self.assertTrue(fake.invoke_calls[1]["body"]["prompt"].endswith(NO_THINK_PREFILL))
        agent.chat(turns)
        self.assertFalse(fake.invoke_calls[2]["body"]["prompt"].endswith(NO_THINK_PREFILL))
        # auto: first chat natural; once a think block is seen, later chats are prefilled
        brain, fake = make_brain([{"generation": "<think>x</think>\nhey", "stop_reason": "stop"},
                                  {"generation": "again", "stop_reason": "stop"}], model=self.ARN)
        self.assertEqual(brain.think_mode, "auto")
        agent = make_agent(brain)
        self.assertEqual(agent.chat(turns), "hey")
        self.assertFalse(fake.invoke_calls[0]["body"]["prompt"].endswith(NO_THINK_PREFILL))
        agent.chat(turns)
        self.assertTrue(fake.invoke_calls[1]["body"]["prompt"].endswith(NO_THINK_PREFILL))
        self.assertEqual(brain.status()["thinking"], "auto")
        # non-chatml templates never get the prefill
        brain, fake = make_brain([{"generation": json.dumps(plan()), "stop_reason": "stop"}],
                                 model=self.ARN, bedrock={"chat_template": "llama3", "thinking": "off"})
        brain.plan(make_agent(brain), {})
        self.assertNotIn("<think>", fake.invoke_calls[0]["body"]["prompt"])


class TestFactory(unittest.TestCase):

    def test_build_brain_selects_provider(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            self.assertIsInstance(build_brain({"provider": "anthropic"}, LOG), ClaudeBrain)
        with mock.patch("jarvis.brain.bedrock.boto3", None):
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
        with mock.patch("jarvis.brain.bedrock.boto3") as boto:
            boto.client.return_value = fake_client
            brain = JarvisSystem(cfg, LOG).build_brain()
        self.assertIsInstance(brain, BedrockBrain)
        self.assertEqual(brain.region, "eu-west-2")
        self.assertIs(brain.client, fake_client)
        self.assertEqual(boto.client.call_args.args[0], "bedrock-runtime")
        self.assertEqual(brain.key_source, "instance-role/bedrock:eu-west-2")


if __name__ == "__main__":
    unittest.main()
