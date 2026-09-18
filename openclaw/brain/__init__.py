"""
OpenClaw Brain

The LLM planner. ``ClaudeBrain`` looks at what the agent observed, what it
has done recently and what goals it holds, and decides the next task via
the Claude API. Everything degrades to the rule-based ``TaskPlanner`` when
the model is unavailable, so the agent never stops working because a
network call failed.

Importing this package does not require the ``anthropic`` SDK; it is
imported lazily when a brain is constructed.
"""

from openclaw.brain.credentials import resolve_api_key  # noqa: F401
from openclaw.brain.llm import ClaudeBrain, Decision, PLAN_SCHEMA  # noqa: F401
