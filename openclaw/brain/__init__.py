"""
OpenClaw Brain

The LLM planner. A brain looks at what the agent observed, what it has
done recently and what goals it holds, and decides the next task. Two
providers share one loop:

- ``ClaudeBrain``  - Claude via the Claude API (``llm.provider: anthropic``)
- ``BedrockBrain`` - any Amazon Bedrock model, including your own imported
  weights, via the Converse API (``llm.provider: bedrock``)

Everything degrades to the rule-based ``TaskPlanner`` when the model is
unavailable, so the agent never stops working because a network call
failed. Neither the ``anthropic`` SDK nor ``boto3`` is needed to import
this package; they load when the matching brain is constructed.
"""

from openclaw.brain.credentials import resolve_api_key  # noqa: F401
from openclaw.brain.llm import (BaseBrain, ClaudeBrain, Decision,  # noqa: F401
                                PLAN_SCHEMA, build_brain)
