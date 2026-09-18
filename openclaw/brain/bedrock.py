"""
BedrockBrain - the planner on Amazon Bedrock.

Same loop as ClaudeBrain, different transport: the Bedrock Runtime
``converse`` API through boto3 with the instance role, so no API key is
needed on the box. ``llm.model`` may be:

- a catalog model id      e.g. ``meta.llama3-3-70b-instruct-v1:0``
- an inference profile     e.g. ``eu.meta.llama3-3-70b-instruct-v1:0`` or its ARN
- your own imported model  ``arn:aws:bedrock:REGION:ACCOUNT:imported-model/ID``
  (Custom Model Import: Llama, Mistral or gpt-oss weights in safetensors)

Open models do not offer Claude's structured outputs, so the plan is asked
for as JSON in the prompt, extracted from whatever comes back, validated,
and re-asked once with the parse error if it was not usable. Imported
models are unloaded when idle and can answer ``ModelNotReadyException`` on
the first call after a pause; that is treated as a short backoff, not an
outage.

Two transports:

- ``converse`` (catalog models and inference profiles): the Converse API.
- ``invoke`` (Custom Model Import ARNs, which Converse rejects): InvokeModel
  with the conversation rendered through the model's chat template
  (``llm.bedrock.chat_template``: chatml for Qwen, llama3, mistral) and a
  ``{"prompt", "max_tokens", "temperature"}`` body. Both response shapes
  Bedrock uses for imported models are understood.
"""

import json
import logging
import os
from typing import Optional

from openclaw.brain.llm import BaseBrain, Completion, PLAN_SCHEMA

try:
    import boto3
    from botocore.exceptions import (BotoCoreError, ClientError,
                                     NoCredentialsError, NoRegionError)
except ImportError:  # pragma: no cover - exercised only without boto3
    boto3 = None
    BotoCoreError = ClientError = NoCredentialsError = NoRegionError = Exception

DEFAULT_BEDROCK_MODEL = "meta.llama3-3-70b-instruct-v1:0"

# Chat templates for InvokeModel on imported models. Each renders a system
# text and [{role, content}] turns into a single prompt ending with the
# assistant cue; the model's own EOS token ends generation.
CHAT_TEMPLATES = {
    "chatml": {   # Qwen 2/2.5/3, many fine-tunes
        "system": "<|im_start|>system\n{content}<|im_end|>\n",
        "user": "<|im_start|>user\n{content}<|im_end|>\n",
        "assistant": "<|im_start|>assistant\n{content}<|im_end|>\n",
        "cue": "<|im_start|>assistant\n",
    },
    "llama3": {   # Llama 3.x instruct
        "system": "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n{content}<|eot_id|>",
        "user": "<|start_header_id|>user<|end_header_id|>\n\n{content}<|eot_id|>",
        "assistant": "<|start_header_id|>assistant<|end_header_id|>\n\n{content}<|eot_id|>",
        "cue": "<|start_header_id|>assistant<|end_header_id|>\n\n",
    },
    "mistral": {  # Mistral / Mixtral instruct (system folded into first user turn)
        "system": "",
        "user": "[INST] {content} [/INST]",
        "assistant": " {content}</s>",
        "cue": "",
    },
}


def render_prompt(template: str, system: str, messages: list) -> str:
    """Render system + turns with a named chat template."""
    t = CHAT_TEMPLATES.get(template) or CHAT_TEMPLATES["chatml"]
    parts = []
    turns = [dict(m) for m in messages]
    if template == "mistral" and system:
        # No system slot: prepend to the first user message.
        for m in turns:
            if m["role"] == "user":
                m["content"] = system + "\n\n" + m["content"]
                break
    elif system:
        parts.append(t["system"].format(content=system))
    for m in turns:
        parts.append(t[m["role"]].format(content=m["content"]))
    parts.append(t["cue"])
    return "".join(parts)


def guess_template(model: str) -> str:
    m = (model or "").lower()
    if "llama" in m:
        return "llama3"
    if "mistral" in m or "mixtral" in m:
        return "mistral"
    return "chatml"
JSON_FIELDS = ", ".join(f'"{k}"' for k in PLAN_SCHEMA["required"])

JSON_INSTRUCTIONS = (
    "\n\nOutput format: reply with ONE JSON object and nothing else - no prose, no "
    "code fences. Fields, all required: " + JSON_FIELDS + ". task_type must be one of "
    + ", ".join(PLAN_SCHEMA["properties"]["task_type"]["enum"]) +
    ". priority is an integer 0-10. completed_goals is a JSON array of strings "
    "(use [] when none). command, goal, description and note are strings (use \"\" "
    "when not applicable)."
)


class BedrockBrain(BaseBrain):
    """Open-weight (or any Bedrock) model planner via the Converse API."""

    provider = "bedrock"
    default_model = DEFAULT_BEDROCK_MODEL

    def __init__(self, config: Optional[dict], logger: logging.Logger, client=None,
                 cycle_interval: Optional[float] = None, region: Optional[str] = None):
        cfg = dict(config or {})
        self.region = (cfg.get("region") or region or os.environ.get("AWS_REGION")
                       or os.environ.get("AWS_DEFAULT_REGION"))
        bedrock_cfg = cfg.get("bedrock") if isinstance(cfg.get("bedrock"), dict) else {}
        self.temperature = float(bedrock_cfg.get("temperature", 0.2))
        self.json_retries = max(0, int(bedrock_cfg.get("json_retries", 1)))
        self.not_ready_backoff = float(bedrock_cfg.get("not_ready_backoff", 45))
        model = cfg.get("model") or self.default_model
        api = str(bedrock_cfg.get("api") or "auto").lower()
        if api == "auto":
            api = "invoke" if ":imported-model/" in model else "converse"
        self.api = api if api in ("converse", "invoke") else "converse"
        self.chat_template = str(bedrock_cfg.get("chat_template") or guess_template(model)).lower()
        super().__init__(cfg, logger, client=client, cycle_interval=cycle_interval)
        self.key_source = f"instance-role/bedrock:{self.region or 'no-region'}"

    # ---- setup ----------------------------------------------------------

    def _make_client(self):
        if boto3 is None:
            self._disabled_reason = "boto3 not installed (pip install boto3)"
            self.log.warning("LLM brain disabled: %s", self._disabled_reason)
            return None
        if not self.region:
            self._disabled_reason = "no AWS region (set llm.region or AWS_REGION)"
            self.log.warning("LLM brain disabled: %s", self._disabled_reason)
            return None
        try:
            from botocore.config import Config
            cfg = Config(region_name=self.region,
                         read_timeout=float(self.config.get("timeout", 120)),
                         connect_timeout=10,
                         retries={"max_attempts": int(self.config.get("max_retries", 2)) + 1,
                                  "mode": "standard"})
            return boto3.client("bedrock-runtime", config=cfg)
        except Exception as e:
            self._disabled_reason = f"could not create bedrock-runtime client: {e}"
            self.log.warning("LLM brain disabled: %s", self._disabled_reason)
            return None

    def status(self) -> dict:
        out = super().status()
        out["provider"] = "bedrock"
        out["region"] = self.region
        out["api"] = self.api
        if self.api == "invoke":
            out["chat_template"] = self.chat_template
        return out

    # ---- transport ----------------------------------------------------------

    def _converse(self, system: str, messages: list) -> dict:
        return self.client.converse(
            modelId=self.model,
            system=[{"text": system}],
            messages=[{"role": m["role"], "content": [{"text": m["content"]}]} for m in messages],
            inferenceConfig={"maxTokens": self.max_tokens, "temperature": self.temperature},
        )

    @staticmethod
    def _text_of(response: dict) -> str:
        content = ((response or {}).get("output") or {}).get("message", {}).get("content") or []
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))

    @staticmethod
    def _usage_of(response: dict) -> dict:
        u = (response or {}).get("usage") or {}
        return {"input_tokens": int(u.get("inputTokens", 0) or 0),
                "output_tokens": int(u.get("outputTokens", 0) or 0)}

    def _invoke(self, system: str, messages: list) -> dict:
        """InvokeModel for imported models; normalised to the converse shape."""
        body = {"prompt": render_prompt(self.chat_template, system, messages),
                "max_tokens": self.max_tokens, "temperature": self.temperature}
        raw = self.client.invoke_model(modelId=self.model, body=json.dumps(body),
                                       contentType="application/json", accept="application/json")
        payload = raw.get("body")
        data = json.loads(payload.read() if hasattr(payload, "read") else payload or "{}")
        # Shape A (OpenAI-style completion): choices[0].text, usage.prompt_tokens
        # Shape B (Llama-style): generation, prompt_token_count, generation_token_count
        if isinstance(data.get("choices"), list) and data["choices"]:
            choice = data["choices"][0]
            text = choice.get("text", "")
            finish = choice.get("finish_reason") or choice.get("stop_reason") or "stop"
            usage = data.get("usage") or {}
            in_tok, out_tok = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        else:
            text = data.get("generation", "")
            finish = data.get("stop_reason") or "stop"
            in_tok, out_tok = data.get("prompt_token_count", 0), data.get("generation_token_count", 0)
        stop = "max_tokens" if str(finish).lower() in ("length", "max_tokens") else "end_turn"
        return {"output": {"message": {"content": [{"text": text}]}},
                "stopReason": stop,
                "usage": {"inputTokens": int(in_tok or 0), "outputTokens": int(out_tok or 0)}}

    def _chat(self, system: str, messages: list) -> dict:
        if self.api == "invoke":
            return self._invoke(system, messages)
        return self._converse(system, messages)

    def _complete(self, system: str, messages: list, structured: bool,
                  cache: bool = True) -> Completion:
        if structured:
            system = system + JSON_INSTRUCTIONS
        messages = [{"role": m["role"], "content": m["content"]} for m in messages]
        response = self._chat(system, messages)
        text = self._text_of(response)
        usage = self._usage_of(response)
        stop = response.get("stopReason") or "end_turn"

        if structured and stop == "end_turn":
            # Open models drift from the schema; validate and re-ask once
            # with the error. The retry shares this call's budget slot.
            # A truncated or filtered reply is not re-asked: the caller
            # reports it as such.
            attempts = 0
            while True:
                try:
                    self._parse_plan(text)
                    break
                except ValueError as e:
                    if attempts >= self.json_retries or stop != "end_turn":
                        break
                    attempts += 1
                    self.log.info("Bedrock plan was not valid JSON (%s); re-asking", e)
                    messages = messages + [
                        {"role": "assistant", "content": text or "(empty)"},
                        {"role": "user", "content":
                            f"That was not a valid JSON object ({e}). Reply again with "
                            f"ONLY the JSON object, fields {JSON_FIELDS}."},
                    ]
                    response = self._chat(system, messages)
                    text = self._text_of(response)
                    more = self._usage_of(response)
                    usage = {k: usage.get(k, 0) + more.get(k, 0) for k in usage}
                    stop = response.get("stopReason") or "end_turn"

        if stop == "max_tokens":
            return Completion(text=text, stop_reason="max_tokens", usage=usage)
        if stop == "content_filtered" or stop == "guardrail_intervened":
            return Completion(text=text, stop_reason="refusal", usage=usage,
                              refusal_category=stop)
        return Completion(text=text, stop_reason="end_turn", usage=usage)

    # ---- errors ----------------------------------------------------------------

    def _handle_error(self, e: Exception):
        if isinstance(e, ClientError):
            code = (e.response or {}).get("Error", {}).get("Code", "")
            msg = (e.response or {}).get("Error", {}).get("Message", str(e))
            if code == "ModelNotReadyException":
                self._backoff(self.not_ready_backoff, "imported model is loading (ModelNotReady)")
                return
            if code in ("ThrottlingException", "ServiceQuotaExceededException",
                        "TooManyRequestsException"):
                self.stats["rate_limited"] += 1
                self._backoff(60, f"Bedrock throttled ({code})")
                return
            if code in ("AccessDeniedException", "UnrecognizedClientException",
                        "InvalidSignatureException", "ExpiredTokenException"):
                self._disable(f"Bedrock access denied for {self.model}: {msg}")
                return
            if code in ("ResourceNotFoundException", "ModelNotFoundException"):
                self._disable(f"Bedrock model not found: {self.model}")
                return
            if code in ("ValidationException", "ModelErrorException"):
                self.last_error = f"Bedrock rejected the request: {msg}"
                self.log.error("LLM %s", self.last_error)
                if self._consecutive_errors >= 3:
                    self._disable("repeated Bedrock validation errors; check llm.model and region")
                else:
                    self._backoff(30, self.last_error)
                return
            if code in ("ModelTimeoutException", "ServiceUnavailableException",
                        "InternalServerException"):
                self._backoff(60, f"Bedrock error {code}")
                return
            self._backoff(60, f"Bedrock error {code or 'unknown'}: {msg}")
            return
        if isinstance(e, NoCredentialsError):
            self._disable("no AWS credentials (instance role missing?)")
            return
        if isinstance(e, NoRegionError):
            self._disable("no AWS region configured")
            return
        if isinstance(e, BotoCoreError):
            self._backoff(min(300, 30 * max(1, self._consecutive_errors)),
                          f"Bedrock unreachable: {e.__class__.__name__}")
            return
        self._backoff(60, f"unexpected error: {e.__class__.__name__}: {e}")
