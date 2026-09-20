"""The tool register: what the agent can do, declared in one place.

Until now a capability was spread across four files. ``TaskType`` held the
name, ``TaskExecutor._handlers`` held the code, ``authority.READ_ONLY_TASKS``
held whether it only looks, and ``llm.PLANNABLE_TASK_TYPES`` held whether the
model was allowed to choose it. Nothing tied them together, so they drifted:
``notify_operator`` shipped with a handler, an authority rule, an IAM policy
and passing tests, and the model could never once choose it, because it was
missing from the single list it actually reads. The capability existed
everywhere except where it counted.

So a tool is declared once, here, and everything else is derived. A tool that
is not in this register does not exist; a tool in this register cannot be
half-wired.

**Two gates, and they answer different questions.**

``effect`` answers "does this change the machine?". A CHANGE tool is still
offered at the proposer rung, attempted, refused by the authority spine and
written down as a proposal. That is deliberate: the operator learns what the
agent wanted to do, which is most of the value of having a proposer at all.

``min_rung`` answers "is this capability open yet?". A tool above the agent's
rung is not offered, not described and not choosable. It is not refused,
because there is nothing to refuse -- the model never sees it. This is the
pacing dial: capability opens one tool at a time, deliberately, and the change
is a config edit on the record rather than a thing anyone has to remember.

The distinction matters. A refused proposal says "it wanted to and was
stopped". An unoffered tool says "it was never asked to have an opinion". The
first is a conversation; the second is a boundary.
"""

from dataclasses import dataclass, field
from typing import Optional

from jarvis.agent import authority

READ = authority.READ
CHANGE = authority.CHANGE
OBSERVER, PROPOSER, ACTOR = authority.OBSERVER, authority.PROPOSER, authority.ACTOR

_RUNG_ORDER = {OBSERVER: 0, PROPOSER: 1, ACTOR: 2}

# The idle choice. Not a tool: it is the agent deciding there is nothing worth
# doing, which is a decision the register should not be able to withhold.
IDLE = "none"


@dataclass(frozen=True)
class Tool:
    """One capability, declared once.

    ``arguments`` is a JSON-Schema ``properties`` map. It is the contract for
    what the model may pass, used both to describe the tool and, where the
    provider supports tool use, to have the schema enforced before the
    arguments ever reach this process.
    """

    name: str
    summary: str                      # one line, given to the model verbatim
    effect: str = READ                # READ or CHANGE
    min_rung: str = OBSERVER          # the rung at which this becomes available
    arguments: dict = field(default_factory=dict)
    required: tuple = ()
    # Some tools are the agent's own bookkeeping rather than a way to act on
    # the world; they are recorded but never become proposals.
    reporting: bool = False
    # A tool the planner may not choose, because it arrives from the operator.
    plannable: bool = True

    def available_at(self, rung: str) -> bool:
        rung = authority.normalise_rung(rung)
        return _RUNG_ORDER[rung] >= _RUNG_ORDER[authority.normalise_rung(self.min_rung)]

    def spec(self) -> dict:
        """Bedrock Converse toolSpec for this tool."""
        return {"toolSpec": {
            "name": self.name,
            "description": self.summary,
            "inputSchema": {"json": {
                "type": "object",
                "properties": dict(self.arguments),
                "required": list(self.required),
            }},
        }}


# --- the register -----------------------------------------------------------
#
# Ordered roughly by how much of the world each one touches. Every entry below
# already existed as a task type; the register is what makes them one thing
# instead of four. New capability goes here and nowhere else.

_DESC = {"description": {"type": "string",
                         "description": "One line saying what this is for."}}

TOOLS = (
    Tool("system_check", "Check the machine's overall state and report it.",
         arguments=dict(_DESC)),
    Tool("hardware_probe", "Read memory, storage and device state.",
         arguments=dict(_DESC)),
    Tool("cloud_probe", "Read this instance's own cloud identity and metadata.",
         arguments=dict(_DESC)),
    Tool("security_scan", "Run the vulnerability scanner and report findings.",
         arguments=dict(_DESC)),
    Tool("observation", "Write down something worth remembering about this machine.",
         arguments=dict(_DESC)),
    Tool("goal_step", "Record progress against a standing goal.",
         arguments={**_DESC,
                    "goal": {"type": "string",
                             "description": "The goal this step serves, quoted exactly."}}),
    Tool("inspect_path", "Look at one absolute path: its kind, size and, for a "
                         "directory, a bounded listing. Reads only.",
         arguments={**_DESC,
                    "path": {"type": "string",
                             "description": "Absolute path to look at."}},
         required=("path",)),
    Tool("read_logs", "Read this agent's own recent journal, bounded. Use it to "
                      "find out what you did and what went wrong, rather than "
                      "guessing from memory.",
         arguments={**_DESC,
                    "unit": {"type": "string",
                             "description": "systemd unit; jarvis, jarvis-health or "
                                            "cloudflared. Defaults to jarvis."},
                    "minutes": {"type": "integer",
                                "description": "How far back to read, 1 to 180."},
                    "grep": {"type": "string",
                             "description": "Optional plain substring to filter on."}}),
    Tool("estate_report", "Report what this agent costs to run and what is "
                          "accumulating in the account behind it.",
         arguments=dict(_DESC)),
    Tool("notify_operator", "Send the operator one short message, subject to the "
                            "severity floor, hourly cap, gap and quiet hours "
                            "enforced below you.",
         reporting=True,
         arguments={**_DESC,
                    "severity": {"type": "string",
                                 "description": "info, notice or alert."}}),
    Tool("maintenance", "Perform routine upkeep of this machine.",
         effect=CHANGE, arguments=dict(_DESC)),
    Tool("shell_command", "Run one POSIX sh command. Subject to the deny-list, "
                          "which never shrinks, and to your mandate.",
         effect=CHANGE,
         arguments={**_DESC,
                    "command": {"type": "string",
                                "description": "The command line to run."}},
         required=("command",)),
    # Operator-issued, never planned.
    Tool("user_command", "A command given directly by the operator.",
         effect=CHANGE, plannable=False, arguments=dict(_DESC)),
)

BY_NAME = {t.name: t for t in TOOLS}


# --- derived views ----------------------------------------------------------
#
# Everything below is computed. Nothing here is a second list to keep in step.

def get(name: str) -> Optional[Tool]:
    return BY_NAME.get(str(name or "").strip().lower())


def names() -> tuple:
    return tuple(t.name for t in TOOLS)


def read_only_names() -> frozenset:
    """Tools that only ever look. Feeds the authority spine."""
    return frozenset(t.name for t in TOOLS if t.effect == READ and not t.reporting)


def reporting_names() -> frozenset:
    """Tools that tell the operator something. Allowed at every rung."""
    return frozenset(t.name for t in TOOLS if t.reporting)


def effective_rung(tool: Tool, restrict: Optional[dict] = None) -> str:
    """The rung this tool actually needs, after config.

    Config may only make a tool *harder* to reach, never easier. Tightening is
    a running decision and belongs in config; loosening is a decision about
    what the agent is trusted with, and belongs in a commit someone reviewed.
    A typo in a YAML file should not be able to hand anything out.
    """
    declared = authority.normalise_rung(tool.min_rung)
    asked = (restrict or {}).get(tool.name)
    if asked is None:
        return declared
    asked = authority.normalise_rung(asked)
    return asked if _RUNG_ORDER[asked] > _RUNG_ORDER[declared] else declared


def _open_at(tool: Tool, rung: str, restrict: Optional[dict]) -> bool:
    if not tool.plannable:
        return False
    need = effective_rung(tool, restrict)
    return _RUNG_ORDER[authority.normalise_rung(rung)] >= _RUNG_ORDER[need]


def plannable(rung: str = ACTOR, include_idle: bool = True,
              restrict: Optional[dict] = None) -> list:
    """The choices the model may make at this rung, idle first.

    A tool above the rung is absent rather than refused. The model is never
    invited to want something it cannot have.
    """
    out = [IDLE] if include_idle else []
    out += [t.name for t in TOOLS if _open_at(t, rung, restrict)]
    return out


def specs(rung: str = ACTOR, restrict: Optional[dict] = None) -> list:
    """Bedrock Converse toolSpecs for everything available at this rung."""
    return [t.spec() for t in TOOLS if _open_at(t, rung, restrict)]


def withheld(rung: str, restrict: Optional[dict] = None) -> list:
    """Tools this rung does not open yet. For the operator, never the model.

    The names stay with the operator. A list of things it cannot have is an
    invitation to ask for them, and the answer would only ever be no. That
    the list exists, though, is the agent's business: see ``gating_note``.
    """
    return [t.name for t in TOOLS if t.plannable and not _open_at(t, rung, restrict)]


def gating_note(rung: str, restrict: Optional[dict] = None) -> str:
    """Tell the agent that capability is gated, without naming what.

    Asked what it needed to make this work, the agent answered, among other
    things: "I need the ability to see when a capability is gated by design
    versus broken by accident." That is a better argument than the one it
    replaced. Hiding the gate entirely leaves an unexplained absence, and this
    codebase already holds that an absence a model cannot see is one it
    invents something to fill -- missing paths are listed as missing, an empty
    scan report says it is empty rather than clean. Withheld tools were the
    one place that principle was not applied, for no better reason than that
    it felt safer.

    So: the fact is reported, the names are not, and the difference between a
    boundary and a fault is spelled out. A tool that was offered and then
    failed is a defect worth reporting, not a wall to work around -- which
    also closes the older failure where a refusal got treated as a puzzle and
    the agent went looking for another route.
    """
    n = len(withheld(rung, restrict))
    if not n:
        return ("Every capability this agent has is available to you at this rung. "
                "If a tool you were offered then fails, that is a fault, not a "
                "boundary: say so plainly rather than working around it.")
    return (f"{n} further {'capability is' if n == 1 else 'capabilities are'} "
            "declared on this machine and not open at your rung. You are not told "
            "which, and there is nothing to ask for: they are withheld by design, "
            "not broken, and the operator opens them deliberately over time. "
            "What this means for you is only this -- if a tool you WERE offered "
            "then fails, that is a fault and not a boundary, and it is worth "
            "reporting rather than working around.")


def describe(rung: str = ACTOR, restrict: Optional[dict] = None) -> str:
    """The tool list as the model is told it, one line each."""
    lines = ["none: do nothing this cycle; the machine is fine and nothing is due."]
    for t in TOOLS:
        if not _open_at(t, rung, restrict):
            continue
        mark = "" if t.effect == READ or t.reporting else "  [changes the machine]"
        lines.append(f"{t.name}: {t.summary}{mark}")
    return "\n".join(lines)


def validate() -> list:
    """Problems a human should know about. Empty when the register is sound.

    Run by the test suite. The drift this exists to catch is not hypothetical:
    it shipped once and cost a capability nobody could use.
    """
    problems = []
    seen = set()
    for t in TOOLS:
        if t.name in seen:
            problems.append(f"{t.name}: declared twice")
        seen.add(t.name)
        if t.effect not in (READ, CHANGE):
            problems.append(f"{t.name}: effect {t.effect!r} is neither read nor change")
        if authority.normalise_rung(t.min_rung) != t.min_rung:
            problems.append(f"{t.name}: min_rung {t.min_rung!r} is not a rung")
        if t.reporting and t.effect != READ:
            problems.append(f"{t.name}: a reporting tool that also changes the machine")
        for r in t.required:
            if r not in t.arguments:
                problems.append(f"{t.name}: requires {r!r}, which it does not declare")
    return problems
