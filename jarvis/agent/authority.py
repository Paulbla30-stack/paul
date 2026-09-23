"""The permission spine: what a task is *allowed* to be, not just what it may run.

The deny-list is a floor. It answers "is this command catastrophic?" and it
answered correctly when the planner tried to write a kernel parameter
directly. What nothing answered was the prior question: "is changing this
machine within what I was asked to do at all?"

The goal was "Report security posture once per hour and note anything new."
Reporting is a read-only mandate. The planner read the scan, decided to
remediate, was refused by the deny-list, and reasoned its way to an
equivalent command that the list did not name. It was told "that command is
denied", so it looked for a command that was not. Had it been told "changing
this machine is not within this goal", there would have been nothing to
rephrase.

So this is the ceiling, and it sits above the floor:

* **observer** may look and report. A change is refused and recorded.
* **proposer** may look and report, and a change becomes a proposal for Paul
  rather than an action. This is the default.
* **actor** may change the machine, still under the deny-list.

The rungs are Paul's, from the design pack's permission spine (0 observer,
1 proposer, 2 standing approval, 3 scheduled). The upper two rungs need an
autonomy register and approval cards, which are not built yet, so `actor`
here is the old behaviour named honestly rather than a ratified tier.

Classification fails closed: a command that cannot be recognised as
read-only is treated as a change.
"""

import shlex
from typing import Optional

OBSERVER = "observer"
PROPOSER = "proposer"
ACTOR = "actor"
RUNGS = (OBSERVER, PROPOSER, ACTOR)

READ = "read"
CHANGE = "change"

# Which tools only look, and which merely report, is declared once in
# jarvis.agent.tools and read from there. It used to be duplicated here, and
# the duplicate is exactly how notify_operator came to have a handler, a rule,
# an IAM policy and tests while remaining impossible for the model to choose.
#
# The import is deferred to the call because tools imports this module for the
# rung and effect constants. Nothing here may import it at module level.
#
# Telling the operator something is reporting, and every rung may report -- an
# observer that may look but not say what it saw is not an observer. That is
# not a loophole in the "never send anything externally" gate: the notifier has
# one destination, fixed at boot from a secret the model cannot read, with a
# severity floor, an hourly cap, a gap and quiet hours enforced in code below
# the model. The operator approved the destination and the limits in advance;
# the agent chooses only whether this is worth saying.

def _looks_only(kind: str) -> bool:
    """True when this tool only reads, per the register. Fails closed."""
    try:
        from jarvis.agent import tools
        return kind in tools.read_only_names() or kind in tools.reporting_names()
    except Exception:
        # An unreadable register makes everything a change, which costs a
        # proposal and never a surprise.
        return False

# Programs that only read. The list is deliberately short: anything not on
# it is a change, so a missing entry costs a proposal, never a surprise.
READ_ONLY_COMMANDS = frozenset({
    "awk", "basename", "cat", "cksum", "column", "comm", "cut", "date", "df", "diff",
    "dirname", "du", "echo", "env", "false", "file", "find", "free", "getconf",
    "getent", "grep", "egrep", "fgrep", "head", "hostname", "hostnamectl", "id",
    "ip", "journalctl", "jq", "last", "logname", "ls", "lsblk", "lscpu", "lsmod",
    "lsof", "md5sum", "nproc", "od", "ps", "pwd", "readlink", "realpath", "rpm",
    "sed", "seq", "sha1sum", "sha256sum", "sleep", "sort", "ss", "stat", "strings",
    "sysctl", "systemctl", "tail", "test", "top", "tr", "true", "uname", "uniq",
    "uptime", "users", "vmstat", "w", "wc", "who", "whoami", "xargs", "zcat",
})

# Sub-commands that make an otherwise-reading program a change.
_WRITING_SUBCOMMANDS = {
    "systemctl": frozenset({"start", "stop", "restart", "reload", "enable", "disable",
                            "mask", "unmask", "kill", "isolate", "set-property",
                            "daemon-reload", "edit", "reset-failed"}),
    "rpm": frozenset({"-i", "-U", "-e", "--install", "--upgrade", "--erase"}),
    "ip": frozenset({"add", "del", "set", "change", "replace", "flush"}),
}

# Flags that turn a reader into a writer.
_WRITING_FLAGS = {
    "sed": ("-i", "--in-place"),
    "find": ("-delete", "-exec", "-execdir", "-fprint", "-fprintf", "-ok"),
    "sysctl": ("-w", "--write", "-p", "--load", "--system"),
}

# A pipe into one of these writes, whatever came before it.
_WRITE_SINKS = frozenset({"tee", "dd", "sponge", "install", "cp", "mv", "rm", "mkdir",
                          "touch", "chmod", "chown", "chgrp", "ln", "truncate"})

# Control operators that separate one simple command from the next.
_OPERATORS = frozenset({";", "|", "||", "&", "&&", "(", ")"})
_REDIRECTS = frozenset({">", ">>", ">|", "<>", "&>", ">&"})

# Words that prefix a command without being one.
_PREFIXES = frozenset({"sudo", "env", "nohup", "nice", "time", "command", "exec"})


class Decision:
    """What the spine decided about one planned task."""

    __slots__ = ("allowed", "rung", "kind", "reason", "proposal")

    def __init__(self, allowed: bool, rung: str, kind: str,
                 reason: str = "", proposal: bool = False):
        self.allowed = allowed
        self.rung = rung
        self.kind = kind
        self.reason = reason
        self.proposal = proposal

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "rung": self.rung, "kind": self.kind,
                "reason": self.reason, "proposal": self.proposal}


# Aliases, and the direction they are allowed to point. A spelling may name a
# rung at or below the default; nothing may name a rung above it.
#
# "act" and "2" used to resolve to actor, and str().strip().lower() ran before
# the lookup, so "ACTOR", " actor", "actor\n", "\tactor" and "ACT" all reached
# actor as well. That is a privilege decision taken on a normalised string,
# which is the classic way a permission check is talked past: the value the
# code compares is not the value anyone wrote down. Found by the standing
# refusals suite, 23 September 2026.
#
# Resolving downward needs no such care, because getting it wrong grants
# nothing. So the aliases stay for observer and proposer and are gone for
# actor: an actor rung is now spelled exactly "actor", or it is not one.
DOWNWARD_ALIASES = {
    "0": OBSERVER, "observe": OBSERVER, "observer": OBSERVER,
    "1": PROPOSER, "propose": PROPOSER, "proposer": PROPOSER,
}


def normalise_rung(value) -> str:
    """The rung a value actually grants. Unrecognised is proposer, never actor.

    Exact match, on a string, with no folding, trimming or normalisation
    first: every transformation applied before a privilege lookup is a second
    spelling of the privilege. Must not raise, whatever it is handed.
    """
    # type(), not isinstance(): a str subclass may define its own __eq__, and
    # the whole point here is that the comparison cannot be argued with.
    if type(value) is not str:
        return PROPOSER
    if value in RUNGS:
        return value
    return DOWNWARD_ALIASES.get(value, PROPOSER)


def _tokenise(text: str):
    """Shell tokens, with operators separated and quoting respected.

    Splitting the raw string on punctuation would treat the pipe inside
    grep -E "warn|crit" as a command separator; the lexer does not.
    """
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _simple_commands(tokens):
    """Split a token list on control operators into simple commands."""
    current, out = [], []
    for token in tokens:
        if token in _OPERATORS:
            if current:
                out.append(current)
            current = []
        else:
            current.append(token)
    if current:
        out.append(current)
    return out


def classify_command(command: str) -> str:
    """READ when every part of the command only looks; CHANGE otherwise."""
    text = str(command or "").strip()
    if not text:
        return CHANGE
    try:
        tokens = _tokenise(text)
    except ValueError:                      # unbalanced quotes: do not guess
        return CHANGE
    if any(token in _REDIRECTS for token in tokens):
        return CHANGE

    for words in _simple_commands(tokens):
        while words and (words[0] in _PREFIXES or "=" in words[0].split(" ")[0][:0]):
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[1:]
        if not words:
            continue

        program = words[0].rsplit("/", 1)[-1]
        if program in _WRITE_SINKS:
            return CHANGE
        if program not in READ_ONLY_COMMANDS:
            return CHANGE

        rest = words[1:]
        for flag in _WRITING_FLAGS.get(program, ()):
            if any(word == flag or word.startswith(flag + "=") for word in rest):
                return CHANGE
        writing = _WRITING_SUBCOMMANDS.get(program)
        if writing and any(word in writing for word in rest):
            return CHANGE
        if program == "sysctl" and any("=" in word for word in rest):
            return CHANGE
    return READ


def classify_task(task) -> str:
    """READ or CHANGE for a planned task."""
    kind = getattr(getattr(task, "task_type", None), "value", None) or ""
    if kind == "shell_command":
        return classify_command((task.metadata or {}).get("command", ""))
    if _looks_only(kind):
        return READ
    return CHANGE


def review(task, rung: str) -> Decision:
    """Decide whether this task is within the mandate."""
    rung = normalise_rung(rung)
    kind = classify_task(task)
    if kind == READ or rung == ACTOR:
        return Decision(True, rung, kind)
    what = (task.metadata or {}).get("command") or getattr(task, "description", "")
    if rung == PROPOSER:
        return Decision(
            False, rung, kind, proposal=True,
            reason=("changing this machine is outside this goal's authority "
                    f"(rung: {rung}); recorded as a proposal for the operator, "
                    "not run. Do not look for another way to run it: report what "
                    "you found and why the change would help, and move on."))
    return Decision(
        False, rung, kind,
        reason=("changing this machine is outside this goal's authority "
                f"(rung: {rung}); this goal is to observe and report. Do not "
                "look for another way to run it: say what you found and move on."))


def describe(rung: str) -> str:
    """The sentence the planner is given about its own mandate."""
    rung = normalise_rung(rung)
    if rung == ACTOR:
        return ("Your mandate is to act: you may change this machine, within the "
                "command policy below.")
    if rung == PROPOSER:
        return ("Your mandate is to observe and propose. You may look at anything "
                "and report what you find, but you may not change this machine. A "
                "task that would change it is not run: it is recorded as a proposal "
                "for the operator and shown to him. This is about the kind of "
                "action, not the spelling of a command, so rephrasing a refused "
                "change as a different command is the one thing never to do. "
                "Propose it in your reasoning instead.")
    return ("Your mandate is to observe. You may look at anything and report what "
            "you find. You may not change this machine, and rephrasing a refused "
            "change as a different command is the one thing never to do: say what "
            "you found and move on.")
