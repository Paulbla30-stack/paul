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

import re
import shlex
from typing import Optional

OBSERVER = "observer"
PROPOSER = "proposer"
ACTOR = "actor"
RUNGS = (OBSERVER, PROPOSER, ACTOR)

READ = "read"
CHANGE = "change"

# Which tools only look, and which merely report, is declared once in
# vigil.agent.tools and read from there. It used to be duplicated here, and
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
        from vigil.agent import tools
        return kind in tools.read_only_names() or kind in tools.reporting_names()
    except Exception:
        # An unreadable register makes everything a change, which costs a
        # proposal and never a surprise.
        return False

# Programs that only read. The list is deliberately short: anything not on
# it is a change, so a missing entry costs a proposal, never a surprise.
#
# "Only read" is a claim about the program as it is invoked, not about its
# name. Several of these have a mode that writes -- sort -o, sed -i, date -s,
# journalctl --vacuum-size, awk's system() -- and each such program is either
# checked by _writes() below or it is not on this list. Found 27 September
# 2026: awk, sort, journalctl and xargs were here with no check at all, so
# "ls /etc | xargs chmod 777" classified as reading.
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

# Flags that turn a reader into a writer. find's predicates are whole words
# and cannot be abbreviated or clustered, so an exact match is a full check.
_WRITING_FLAGS = {
    "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir",
             "-fprint", "-fprint0", "-fprintf", "-fls"),
}

# A pipe into one of these writes, whatever came before it.
_WRITE_SINKS = frozenset({"tee", "dd", "sponge", "install", "cp", "mv", "rm", "mkdir",
                          "touch", "chmod", "chown", "chgrp", "ln", "truncate"})

# Control operators that separate one simple command from the next.
_OPERATORS = frozenset({";", "|", "||", "&", "&&", "(", ")"})
_REDIRECTS = frozenset({">", ">>", ">|", "<>", "&>", ">&"})
# Input redirections: they read, so they are dropped along with their target.
_INPUT_REDIRECTS = frozenset({"<", "<<", "<<<"})
_PUNCTUATION = "();<>|&\n"

# Words that prefix a command without being one.
_PREFIXES = frozenset({"sudo", "env", "nohup", "nice", "time", "command", "exec"})

# A program named by path is only the program its name says when the path is
# a system directory. /tmp/x/cat is whatever somebody put there.
_SYSTEM_BIN = frozenset({"/bin", "/usr/bin", "/sbin", "/usr/sbin"})


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


# --- grants -----------------------------------------------------------------
#
# A grant opens ONE capability at the proposer rung. Paul's decision,
# 23 September 2026: he wanted Vigil to act in the browser rather than file a
# card for every click.
#
# The obvious way to give him that was to set rung: actor. It would also have
# opened shell_command and maintenance -- the whole machine -- which is not
# what he asked for and is not something anybody should acquire as a side
# effect of wanting to click a link. The rung answers "what may this agent be";
# a grant answers "and this one thing as well", narrowly, on the record.
#
# GRANTABLE is why this is not simply "actor with extra steps". A grant may
# name only capabilities whose blast radius is already fenced somewhere other
# than the spine. browse_act qualifies because the thing it drives is a
# separate process, under a separate uid, inside Chromium's own sandbox, with
# no credentials, no access to the ledger or memory, and no route to the
# metadata service. Nothing that changes THIS machine is grantable, ever, and
# a grant naming one is dropped rather than honoured.
GRANTABLE = frozenset({"browse_act"})


def normalise_grants(value) -> frozenset:
    """The capabilities a config value actually opens. Fails closed.

    Same discipline as normalise_rung and for the same reason: every
    transformation applied before a privilege lookup is a second spelling of
    the privilege. Exact strings, no folding, no trimming, and anything not in
    GRANTABLE is discarded -- silently for the caller, loudly for the reader,
    because the alternative is a config typo that grants nothing while looking
    like it granted something.
    """
    if isinstance(value, str) or not hasattr(value, "__iter__"):
        return frozenset()
    out = set()
    for item in value:
        if type(item) is str and item in GRANTABLE:
            out.add(item)
    return frozenset(out)


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


def _long_hits(word: str, names, exact=()) -> bool:
    """True when ``word`` is one of the long options ``names``.

    Also true for an abbreviation of one, because GNU getopt accepts any
    unambiguous prefix: "sort --out=/etc/passwd" is "sort --output". ``exact``
    lists real options that happen to be prefixes of a writing one, such as
    journalctl --cursor beside --cursor-file; an exact match of those wins.
    """
    if not word.startswith("--") or word == "--":
        return False
    name = word.split("=", 1)[0]
    if name in exact:
        return False
    return any(n == name or (len(name) > 2 and n.startswith(name)) for n in names)


def _short_hits(word: str, letters: str, takes: str = "") -> bool:
    """True when a short-option cluster such as -nro includes one of ``letters``.

    A letter in ``takes`` consumes the rest of the word as its argument, so
    scanning stops there: in "sort -k1o" the o belongs to the key.
    """
    if not word.startswith("-") or word.startswith("--") or word == "-":
        return False
    for ch in word[1:]:
        if ch in letters:
            return True
        if ch in takes:
            return False
    return False


def _operands(rest, flags="", takes="", optional="",
              long_flags=(), long_takes=(), long_optional=()):
    """The non-option words of an argument list, or None if an option is unknown.

    For programs whose safety depends on what the operands are. An option not
    named here returns None, and the caller treats None as a change: guessing
    whether an unfamiliar option takes an argument is how an operand gets
    mistaken for one, or the other way round.

    ``takes`` and ``long_takes`` consume the next word when no argument is
    attached; ``optional`` and ``long_optional`` only ever take an attached one.
    """
    operands, i = [], 0
    while i < len(rest):
        word = rest[i]
        i += 1
        if word == "--":
            operands.extend(rest[i:])
            break
        if word.startswith("--"):
            name, eq, _ = word.partition("=")
            if name in long_flags and not eq:
                continue
            if name in long_optional:
                continue
            if name in long_takes:
                if not eq:
                    i += 1
                continue
            return None
        if word.startswith("-") and len(word) > 1:
            for j, ch in enumerate(word[1:], 1):
                if ch in flags:
                    continue
                if ch in optional:
                    break
                if ch in takes:
                    if j == len(word) - 1:
                        i += 1
                    break
                return None
            continue
        operands.append(word)
    return operands


def _date_writes(rest) -> bool:
    # date sets the clock with -s/--set, and also with a bare MMDDhhmm operand.
    # Only a +FORMAT operand is a read.
    ops = _operands(rest, flags="uR", takes="dfr", optional="I",
                    long_flags=("--utc", "--universal", "--rfc-email", "--rfc-2822",
                                "--debug", "--help", "--version"),
                    long_takes=("--date", "--file", "--reference", "--rfc-3339",
                                "--resolution"),
                    long_optional=("--iso-8601",))
    return ops is None or len(ops) > 1 or any(not op.startswith("+") for op in ops)


def _hostname_writes(rest) -> bool:
    # Any operand sets the hostname (or, after -y, the NIS domain); -F and -b
    # set it from a file.
    ops = _operands(rest, flags="aAdfiIsyhV",
                    long_flags=("--alias", "--all-fqdns", "--domain", "--fqdn", "--long",
                                "--ip-address", "--all-ip-addresses", "--short",
                                "--yp", "--nis", "--help", "--version"))
    return ops is None or bool(ops)


_HOSTNAMECTL_SHOW = frozenset({"hostname", "icon-name", "chassis", "deployment", "location"})


def _hostnamectl_writes(rest) -> bool:
    # "hostnamectl hostname" shows; "hostnamectl hostname NAME" sets.
    ops = _operands(rest, flags="hj", takes="HM",
                    long_flags=("--static", "--transient", "--pretty", "--no-ask-password",
                                "--help", "--version"),
                    long_takes=("--host", "--machine", "--json"))
    if ops is None:
        return True
    if not ops or ops == ["status"]:
        return False
    return not (len(ops) == 1 and ops[0] in _HOSTNAMECTL_SHOW)


_IP_FLAGS = frozenset({"-4", "-6", "-0", "-B", "-s", "-stats", "-statistics", "-d",
                       "-details", "-j", "-json", "-p", "-pretty", "-br", "-brief",
                       "-o", "-oneline", "-h", "-human", "-human-readable", "-r",
                       "-resolve", "-a", "-all", "-t", "-timestamp", "-ts", "-tshort",
                       "-c", "-color", "-V", "-Version"})
_IP_TAKES = frozenset({"-n", "-netns", "-f", "-family", "-l", "-loops", "-rc", "-rcvbuf"})
_IP_READ_VERBS = frozenset({"show", "list", "lst", "ls", "sh", "get", "help"})


def _ip_writes(rest) -> bool:
    # ip accepts abbreviations of its verbs ("ip a a" is "ip addr add"), so the
    # verb has to be one known to read rather than not one known to write.
    # -batch runs commands from a file; netns exec runs any command at all.
    i = 0
    while i < len(rest) and rest[i].startswith("-"):
        word = rest[i]
        i += 1
        if word.split("=", 1)[0] in _IP_FLAGS:
            continue
        if word in _IP_TAKES:
            i += 1
            continue
        return True
    words = rest[i:]
    if len(words) < 2:
        return False                      # "ip addr", "ip route": show
    return words[1] not in _IP_READ_VERBS


_SYSTEMCTL_READ_VERBS = frozenset({
    "status", "show", "cat", "help", "list-units", "list-unit-files", "list-sockets",
    "list-timers", "list-jobs", "list-dependencies", "list-machines", "list-automounts",
    "list-paths", "is-active", "is-enabled", "is-failed", "is-system-running",
    "get-default", "show-environment"})


def _systemctl_writes(rest) -> bool:
    # A verb known to read, rather than one not known to write: systemctl has
    # dozens of writing verbs (try-restart, link, preset, set-default, reboot
    # ...) and a list of them is always one short.
    ops = _operands(rest, flags="alqrTfih", takes="tpPsHMno",
                    long_flags=("--all", "--full", "--failed", "--user", "--system",
                                "--global", "--quiet", "--plain", "--no-legend",
                                "--no-pager", "--no-ask-password", "--no-block",
                                "--value", "--recursive", "--reverse", "--after",
                                "--before", "--with-dependencies", "--show-types",
                                "--show-transaction", "--help", "--version"),
                    long_takes=("--type", "--state", "--property", "--host", "--machine",
                                "--lines", "--output", "--timestamp", "--root"))
    if ops is None:
        return True
    return bool(ops) and ops[0] not in _SYSTEMCTL_READ_VERBS


_JOURNALCTL_WRITES = ("--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate",
                      "--flush", "--relinquish-var", "--smart-relinquish-var", "--sync",
                      "--setup-keys", "--update-catalog", "--cursor-file")


def _journalctl_writes(rest) -> bool:
    return any(_long_hits(w, _JOURNALCTL_WRITES, exact=("--cursor",)) for w in rest)


def _sort_writes(rest) -> bool:
    # -o writes the output file; --compress-program runs a program.
    return any(_short_hits(w, "o", takes="kStT")
               or _long_hits(w, ("--output", "--compress-program")) for w in rest)


def _uniq_writes(rest) -> bool:
    # uniq INPUT OUTPUT writes OUTPUT.
    ops = _operands(rest, flags="cdDiuz", takes="fsw",
                    long_flags=("--count", "--repeated", "--ignore-case", "--unique",
                                "--zero-terminated", "--help", "--version"),
                    long_takes=("--skip-fields", "--skip-chars", "--check-chars"),
                    long_optional=("--all-repeated", "--group"))
    return ops is None or len(ops) > 1


def _sysctl_writes(rest) -> bool:
    if any("=" in w for w in rest):
        return True
    ops = _operands(rest, flags="aAbeNnqXxohVd", takes="r",
                    long_flags=("--all", "--binary", "--ignore", "--names", "--values",
                                "--quiet", "--deprecated", "--help", "--version"),
                    long_takes=("--pattern",))
    return ops is None


def _file_writes(rest) -> bool:
    # -C compiles a magic file and writes it; -p resets access times.
    return any(_short_hits(w, "Cp", takes="efFmP")
               or _long_hits(w, ("--compile", "--preserve-date")) for w in rest)


def _ss_writes(rest) -> bool:
    # -K closes sockets; -D dumps raw diagnostics to a file.
    return any(_short_hits(w, "KD", takes="FNfA")
               or _long_hits(w, ("--kill", "--diag")) for w in rest)


_RPM_LONG = frozenset({
    "--query", "--verify", "--querytags", "--showrc", "--version", "--help", "--all",
    "--file", "--package", "--list", "--info", "--configfiles", "--docfiles", "--state",
    "--requires", "--provides", "--whatrequires", "--whatprovides", "--changelog",
    "--last", "--queryformat", "--qf", "--scripts", "--triggers", "--nosignature",
    "--nodigest", "--root", "--dbpath", "--quiet", "--verbose", "--nofiles", "--nodeps",
    "--dump", "--licensefiles", "--conflicts", "--obsoletes", "--recommends",
    "--suggests", "--supplements", "--enhances", "--changes", "--xml", "--noghost",
    "--noconfig", "--filesbypkg", "--checksig"})
_RPM_QUERY_MODE = frozenset({"--query", "--verify", "--querytags", "--showrc",
                             "--version", "--help", "--checksig"})


def _rpm_writes(rest) -> bool:
    # Reads only in query or verify mode. --eval and --define expand macros,
    # which can run a shell; --pipe runs one outright.
    query = False
    for word in rest:
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            if name not in _RPM_LONG:
                return True
            query = query or name in _RPM_QUERY_MODE
        elif word.startswith("-") and len(word) > 1:
            if word[1] in "qV":
                query = True
            elif set(word[1:]) & set("iUFeED"):
                return True
            if set(word[1:]) - set("qVailfcdsRpvhgK"):
                return True
    return not query


_SED_LONG_FLAGS = frozenset({"--quiet", "--silent", "--regexp-extended", "--separate",
                             "--unbuffered", "--null-data", "--zero-terminated",
                             "--binary", "--posix", "--debug", "--sandbox",
                             "--follow-symlinks", "--help", "--version"})


def _sed_writes(rest) -> bool:
    # -i edits in place; -f reads a script that cannot be inspected here; the
    # script itself can write (w, W, s///w) or run a command (e, s///e).
    scripts, operands, i = [], [], 0
    while i < len(rest):
        word = rest[i]
        i += 1
        if word == "--":
            operands.extend(rest[i:])
            break
        if word.startswith("--"):
            name, eq, value = word.partition("=")
            if name in _SED_LONG_FLAGS and not eq:
                continue
            if name in ("--expression", "--line-length"):
                if not eq:
                    value = rest[i] if i < len(rest) else ""
                    i += 1
                if name == "--expression":
                    scripts.append(value)
                continue
            return True
        if word.startswith("-") and len(word) > 1:
            for j, ch in enumerate(word[1:], 1):
                if ch in "nrEsuzb":
                    continue
                if ch in "el":
                    arg = word[j + 1:]
                    if not arg:
                        arg = rest[i] if i < len(rest) else ""
                        i += 1
                    if ch == "e":
                        scripts.append(arg)
                    break
                return True
            continue
        operands.append(word)
    if not scripts and operands:
        scripts.append(operands[0])
    return any(_sed_script_writes(script) for script in scripts)


_SED_PLAIN = frozenset("=dDgGhHxnNpPzF")


def _sed_script_writes(script: str) -> bool:
    """True when a sed script can write a file or run a command.

    A small parser rather than a search for the letters, because "w" and "e"
    are also ordinary characters inside a regex or a replacement: s/new/old/
    reads. Anything it does not recognise counts as writing.
    """
    s, i, n = script, 0, len(script)

    def delimited(pos, delim):
        """Index just past the next unescaped ``delim`` at or after ``pos``."""
        while pos < n:
            if s[pos] == "\\":
                pos += 2
                continue
            if s[pos] == delim:
                return pos + 1
            pos += 1
        return -1

    def to_line_end(pos):
        while pos < n and s[pos] != "\n":
            pos += 2 if s[pos] == "\\" else 1
        return pos

    while i < n:
        ch = s[i]
        if ch in " \t\n;{}!":
            i += 1
            continue
        if ch == "#":
            i = to_line_end(i)
            continue
        # Addresses: line numbers, $, steps, ranges and regexes.
        if ch.isdigit() or ch in "$,~+":
            i += 1
            continue
        if ch in "/\\":
            if ch == "\\":
                if i + 1 >= n:
                    return True
                i += 1
            i = delimited(i + 1, s[i])
            if i < 0:
                return True
            while i < n and s[i] in "IM":
                i += 1
            continue
        i += 1
        if ch in "ewW":
            return True
        if ch in _SED_PLAIN:
            continue
        if ch in "qQlL":
            while i < n and (s[i].isdigit() or s[i] in " \t"):
                i += 1
            continue
        if ch in "aic":
            i = to_line_end(i)
            continue
        if ch in "rR":
            i = to_line_end(i)
            continue
        if ch in ":btTv":
            while i < n and s[i] not in ";\n":
                i += 1
            continue
        if ch in "sy":
            if i >= n or s[i] in "\\\n":
                return True
            delim = s[i]
            i = delimited(i + 1, delim)
            if i < 0:
                return True
            i = delimited(i, delim)
            if i < 0:
                return True
            if ch == "s":
                while i < n and s[i] not in ";\n}":
                    if s[i] in "ewW":
                        return True
                    if not (s[i].isdigit() or s[i] in "gpiImM \t"):
                        return True
                    i += 1
            continue
        return True
    return False


_AWK_SAFE_SHORT = "bcCgMnNOPrsStVh"
_AWK_SAFE_LONG = frozenset({"--characters-as-bytes", "--traditional", "--copyright",
                            "--gen-pot", "--help", "--bignum", "--use-lc-numeric",
                            "--non-decimal-data", "--optimize", "--no-optimize",
                            "--posix", "--re-interval", "--sandbox", "--lint",
                            "--version", "--csv"})


def _awk_writes(rest) -> bool:
    # A program file (-f), an include (-i inplace), an extension (-l) or a
    # profile or dump file (-o, -p, -d) cannot be inspected or writes a file,
    # so any option not known to be harmless is a change.
    programs, operands, i = [], [], 0
    while i < len(rest):
        word = rest[i]
        i += 1
        if word == "--":
            operands.extend(rest[i:])
            break
        if word.startswith("--"):
            name, eq, value = word.partition("=")
            if name in _AWK_SAFE_LONG:
                continue
            if name in ("--field-separator", "--assign", "--source"):
                if not eq:
                    value = rest[i] if i < len(rest) else ""
                    i += 1
                if name == "--source":
                    programs.append(value)
                continue
            return True
        if word.startswith("-") and len(word) > 1:
            if word[1] in "Fve":
                arg = word[2:]
                if not arg:
                    arg = rest[i] if i < len(rest) else ""
                    i += 1
                if word[1] == "e":
                    programs.append(arg)
                continue
            if set(word[1:]) - set(_AWK_SAFE_SHORT):
                return True
            continue
        operands.append(word)
    if not programs and operands:
        programs.append(operands[0])
    return any(_awk_program_writes(program) for program in programs)


def _awk_program_writes(program: str) -> bool:
    """True when awk program text can run a command or write a file.

    system() runs a command; getline and any pipe read from or write to one;
    print or printf followed by > or >> writes a file; @load and @include pull
    in code from elsewhere. "||" is logical or, never a pipe, so it is taken
    out first. Everything else awk can do is reading and arithmetic, which is
    why awk '{print $1}' and awk '$5 > 80' stay reads.
    """
    if re.search(r"\bsystem\s*\(", program) or "getline" in program:
        return True
    if "|" in program.replace("||", ""):
        return True
    if re.search(r"\bprintf?\b.*>", program, re.S):
        return True
    return "@load" in program or "@include" in program


# xargs options. Letters in _XARGS_TAKES consume the next word when nothing
# is attached; e, i and l only ever take an attached value.
_XARGS_FLAGS = "0oprtx"
_XARGS_TAKES = "adEILnPs"
_XARGS_OPTIONAL = "eil"
_XARGS_LONG_FLAGS = ("--null", "--open-tty", "--interactive", "--no-run-if-empty",
                     "--verbose", "--exit", "--show-limits", "--help", "--version")
_XARGS_LONG_TAKES = ("--arg-file", "--delimiter", "--max-args", "--max-procs",
                     "--max-chars", "--process-slot-var")
_XARGS_LONG_OPTIONAL = ("--max-lines", "--replace", "--eof")


def _xargs_command(rest):
    """The command xargs will run, or None if its options cannot be read."""
    i = 0
    while i < len(rest):
        word = rest[i]
        if word == "--":
            return rest[i + 1:]
        if not word.startswith("-") or word == "-":
            return rest[i:]
        i += 1
        if word.startswith("--"):
            name, eq, _ = word.partition("=")
            if name in _XARGS_LONG_FLAGS and not eq:
                continue
            if name in _XARGS_LONG_OPTIONAL:
                continue
            if name in _XARGS_LONG_TAKES:
                if not eq:
                    i += 1
                continue
            return None
        for j, ch in enumerate(word[1:], 1):
            if ch in _XARGS_FLAGS:
                continue
            if ch in _XARGS_OPTIONAL:
                break
            if ch in _XARGS_TAKES:
                if j == len(word) - 1:
                    i += 1
                break
            return None
    return []


def _xargs_writes(rest) -> bool:
    """xargs is classified by the command it runs.

    With no command it runs echo, which reads. Otherwise the command must be
    one that reads whatever it is given, because xargs appends arguments from
    its input that nothing here can see: "echo -o /etc/passwd | xargs sort"
    is sort -o. So a program whose safety depends on its arguments -- every
    program _writes() checks -- is a change under xargs, and so is a prefix
    such as env, which can take a command from its input.
    """
    inner = _xargs_command(rest)
    if inner is None:
        return True
    if not inner:
        return False
    head = inner[0]
    if head in _PREFIXES or "=" in head:
        return True
    if _program(head) in _ARGUMENT_SENSITIVE:
        return True
    return _classify_words(inner) == CHANGE


_CHECKS = {
    "awk": _awk_writes, "date": _date_writes, "file": _file_writes,
    "hostname": _hostname_writes, "hostnamectl": _hostnamectl_writes, "ip": _ip_writes,
    "journalctl": _journalctl_writes, "rpm": _rpm_writes, "sed": _sed_writes,
    "sort": _sort_writes, "ss": _ss_writes, "sysctl": _sysctl_writes,
    "systemctl": _systemctl_writes, "uniq": _uniq_writes, "xargs": _xargs_writes,
}

# Programs whose classification depends on their arguments.
_ARGUMENT_SENSITIVE = frozenset(_CHECKS) | frozenset(_WRITING_FLAGS)


def _writes(program: str, rest) -> bool:
    """True when this invocation of a listed program is not a read."""
    for flag in _WRITING_FLAGS.get(program, ()):
        if any(word == flag or word.startswith(flag + "=") for word in rest):
            return True
    check = _CHECKS.get(program)
    return bool(check and check(rest))


def _prefix_option_runs(prefix: str, word: str) -> bool:
    """True when an option to a prefix word itself runs or writes something.

    env -S splits its argument into a command line, so "env -S'rm -rf /x'"
    carries a whole command inside what looks like an option. GNU time -o
    writes a file. sudo -e edits one.
    """
    if prefix == "env":
        return _short_hits(word, "S", takes="uC") or _long_hits(word, ("--split-string",))
    if prefix == "time":
        return word not in ("-p", "-q", "-v", "--portability", "--quiet", "--verbose")
    if prefix == "sudo":
        return _short_hits(word, "e") or _long_hits(word, ("--edit",))
    return False


def _hidden_expansion(text: str) -> bool:
    """True when the shell would run or rewrite something the lexer cannot see.

    The command runs under /bin/sh -c, and the lexer below only splits words.
    It does not see a command substitution inside double quotes
    (echo "$(rm -rf /x)"), backticks, process substitution, ANSI-C quoting
    that spells an option ($'\\x2do'), or brace expansion
    (sort {-o,/etc/passwd} x is sort -o /etc/passwd x). Each of those is a
    change, whatever program it appears under. Plain $NAME is allowed: it
    expands to one word the planner cannot set without an assignment, which
    is itself a change.
    """
    projected, i, quote, n = [], 0, None, len(text)
    while i < n:
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
            projected.append("_")
            i += 1
            continue
        if ch == "\\":
            projected.append("__")
            i += 2
            continue
        if ch == "`" or text.startswith("$(", i) or text.startswith("${", i):
            return True
        if quote == '"':
            if ch == '"':
                quote = None
            projected.append("_")
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            projected.append("_")
            i += 1
            continue
        if text.startswith("$'", i) or text.startswith('$"', i):
            return True
        if text.startswith("<(", i) or text.startswith(">(", i):
            return True
        projected.append(ch)
        i += 1
    return bool(re.search(r"\{[^{}\s]*(?:,|\.\.)[^{}\s]*\}", "".join(projected)))


def _tokenise(text: str):
    """Shell tokens, with operators separated and quoting respected.

    Splitting the raw string on punctuation would treat the pipe inside
    grep -E "warn|crit" as a command separator; the lexer does not.

    A newline separates commands in sh, so it is punctuation here rather than
    whitespace. Comments are not recognised: "#" stays part of a word, which
    at worst keeps text the shell would ignore -- the safe direction, where
    treating it as a comment would hide the next line from the check.
    """
    lexer = shlex.shlex(text, posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace_split = True
    lexer.whitespace = " \t"
    lexer.commenters = ""
    return list(lexer)


def _is_punctuation(token: str) -> bool:
    return bool(token) and all(ch in _PUNCTUATION for ch in token)


def _simple_commands(tokens):
    """Split a token list on control operators into simple commands.

    The lexer groups adjacent operator characters, so ";(" and "|&" arrive
    as single tokens. Any run of operator characters with no redirection in
    it separates commands; input redirections are dropped with their target.
    """
    current, out = [], []
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token in _INPUT_REDIRECTS:
            skip = True
        elif token in _OPERATORS or _is_punctuation(token):
            if current:
                out.append(current)
            current = []
        else:
            current.append(token)
    if current:
        out.append(current)
    return out


def _program(word: str) -> str:
    """The program a command word names, or "" when a path makes it uncertain."""
    directory, slash, base = word.rpartition("/")
    if slash and directory not in _SYSTEM_BIN:
        return ""
    return base


def _classify_words(words) -> str:
    """READ or CHANGE for one simple command, already split into words."""
    while words and words[0] in _PREFIXES:
        prefix, words = words[0], words[1:]
        while words and words[0].startswith("-"):
            if _prefix_option_runs(prefix, words[0]):
                return CHANGE
            words = words[1:]
    if not words:
        return READ
    # A leading NAME=value is an assignment for the command's environment.
    # It stays a change: LD_PRELOAD=/tmp/x.so cat runs /tmp/x.so, and there
    # is no short list of variables that are safe to set.
    if "=" in words[0]:
        return CHANGE
    program = _program(words[0])
    if program in _WRITE_SINKS or program not in READ_ONLY_COMMANDS:
        return CHANGE
    if _writes(program, words[1:]):
        return CHANGE
    return READ


def classify_command(command: str) -> str:
    """READ when every part of the command only looks; CHANGE otherwise."""
    text = str(command or "").strip()
    if not text:
        return CHANGE
    if _hidden_expansion(text):
        return CHANGE
    try:
        tokens = _tokenise(text)
    except ValueError:                      # unbalanced quotes: do not guess
        return CHANGE
    for token in tokens:
        if token in _REDIRECTS:
            return CHANGE
        # An operator run with a redirection inside it (";>", ">(") writes or
        # substitutes; one that is not a plain input redirection is unknown.
        if _is_punctuation(token) and ">" in token:
            return CHANGE
        if _is_punctuation(token) and "<" in token and token not in _INPUT_REDIRECTS:
            return CHANGE

    for words in _simple_commands(tokens):
        if _classify_words(words) == CHANGE:
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


def review(task, rung: str, granted=()) -> Decision:
    """Decide whether this task is within the mandate.

    ``granted`` is the operator's list of individually opened capabilities.
    It is checked after the rung and only ever widens a single named tool; it
    cannot open anything outside GRANTABLE, so it can never stand in for the
    actor rung.
    """
    rung = normalise_rung(rung)
    kind = classify_task(task)
    if kind == READ or rung == ACTOR:
        return Decision(True, rung, kind)
    tool = getattr(getattr(task, "task_type", None), "value", None) or ""
    # Only at proposer. A grant widens the rung Paul actually runs at; it is
    # not a way round the rung below it. Someone who sets observer has said
    # "this agent only looks", and a line left behind in a config file should
    # not quietly overrule that -- the rung is the ceiling and a grant raises
    # one tile of it, not the floor.
    if rung == PROPOSER and tool in normalise_grants(granted):
        return Decision(True, rung, kind,
                        reason=f"{tool} was opened by the operator at this rung")
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
