"""Ground truth about the machine: what is actually on disk.

The planner is a language model. Asked about a path it cannot see, it does
not say "I don't know": it produces a plausible one from its training. That
is how ``/var/log/jarvis/security_scan.log`` and ``/opt/jarvis``, neither of
which exists on this image, ended up in a health script the planner wrote,
and it is the failure mode operators report from every model family.

Nothing in the context the planner received described the filesystem, so
every path it produced was a guess. This module closes that in three ways:

* ``snapshot()`` puts real, checked paths in the planner's context, and
  reports a missing path *as missing* rather than leaving it out. Silence is
  what lets a model assume; an explicit "missing" teaches it.
* ``tree()`` and ``stat_path()`` let the agent look at a real directory on
  demand, so an answer can be grounded in a listing instead of a memory.
* ``verify()`` checks the paths a model named in its own output, so an
  invention is caught at the moment it is made instead of days later.

Nothing here writes anything. The fence the executor puts around the Glass
Ledger, the agent's runtime directory and credentials is repeated here: a
read-only inspector that could read the ledger would simply be a way around
that fence.
"""

import os
import platform
import re
import stat as stat_mod
import time
from typing import Optional

# Paths the agent must not read, whatever it is asked. Each entry fences the
# path itself, anything under it, and anything sharing its stem (so the
# ledger prefix also covers ledger.jsonl).
# This list and the shell deny-list in executor.py protect the same secrets by
# two different mechanisms -- prefix matching over paths here, regular
# expressions over command lines there -- and they had drifted. The deny-list
# named the runner token, the UI session key, the tunnel token, the notify
# destination and the memory database; this one did not. Nothing could read a
# file's contents, so the gap was latent, and adding a reader would have made
# it live. test_environment.py now asserts the two agree, because two lists of
# the same thing is how a fence gets a hole in it.
FENCED_PREFIXES = (
    ("/var/lib/jarvis/ledger",
     "the Glass Ledger is evidence about the agent, not context for it"),
    ("/etc/jarvis/ledger", "the ledger signing key"),
    ("/run/jarvis", "the agent's own runtime directory (runner token, status API)"),
    ("/etc/jarvis/anthropic.key", "an API credential"),
    # The operator's own credentials. The runner token and the key that signs
    # UI sessions are as good as a login; the tunnel token is the front door.
    ("/etc/jarvis/token", "the runner token is as good as a login"),
    ("/etc/jarvis/session.key", "the key that signs UI sessions"),
    ("/etc/jarvis/cloudflared.env", "the tunnel token is the front door"),
    ("/etc/jarvis/tls", "the UI's private key"),
    # Where the agent may send. notify.py promises the model cannot read this,
    # set it, or send anywhere else; that promise needs this entry to be true.
    ("/etc/jarvis/notify.dest", "the one destination the agent may reach"),
    # The agent's own durable memory. Written through the agent so that every
    # entry carries its provenance; read as a file, that provenance is gone.
    ("/var/lib/jarvis/memory.db", "the agent's memory is written through it, not read around it"),
    ("/etc/shadow", "account credentials"),
    ("/etc/gshadow", "account credentials"),
    ("/etc/sudoers", "privilege configuration"),
)

# Everything the shell deny-list refuses on grounds of secrecy. Kept here so a
# test can assert the two fences agree; see test_environment.py.
SECRET_PATHS = (
    "/etc/jarvis/token",
    "/etc/jarvis/session.key",
    "/etc/jarvis/cloudflared.env",
    "/etc/jarvis/notify.dest",
    "/etc/jarvis/anthropic.key",
    "/etc/jarvis/tls/key.pem",
    "/var/lib/jarvis/memory.db",
    "/var/lib/jarvis/ledger.jsonl",
    "/etc/jarvis/ledger/ed25519.key",
    "/run/jarvis/token",
    "/etc/shadow",
    "/root/.ssh/id_rsa",
    "/root/.aws/credentials",
)

# Directory names that are fenced wherever they appear.
FENCED_NAMES = (".ssh", ".aws", ".gnupg")

# Paths worth reporting every cycle: the ones that define this deployment,
# and the ones a model is most likely to guess wrong about.
STANDARD_PATHS = (
    "/etc/jarvis",
    "/etc/jarvis/config.yaml",
    "/etc/jarvis/cloud.yaml",
    "/var/lib/jarvis",
    "/var/lib/jarvis/uploads",
    "/usr/lib/jarvis",
    "/usr/local/bin/jarvis",
    "/usr/local/bin/jarvis-health",
    "/var/log/jarvis.log",
    "/var/log/jarvis-security.log",
    "/etc/systemd/system/jarvis.service",
    "/opt",
    "/tmp",
)

MAX_ENTRIES = 200
MAX_DEPTH = 3


def _norm(path: str) -> str:
    return os.path.normpath(os.path.abspath(str(path or "/")))


def fenced_reason(path: str) -> Optional[str]:
    """Why this path may not be read, or None when it is allowed."""
    p = _norm(path)
    parts = p.split(os.sep)
    for name in FENCED_NAMES:
        if name in parts:
            return f"{name} holds credentials"
    for prefix, reason in FENCED_PREFIXES:
        if p == prefix or p.startswith(prefix + os.sep) or p.startswith(prefix + "."):
            return reason
    if re.match(r"^/proc/\d+/environ", p):
        return "process environment may hold credentials"
    return None


def stat_path(path: str) -> dict:
    """What is really at this path, including 'nothing'."""
    p = _norm(path)
    reason = fenced_reason(p)
    if reason:
        return {"path": p, "exists": None, "kind": "fenced", "reason": reason}
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return {"path": p, "exists": False, "kind": "missing"}
    except PermissionError as exc:
        return {"path": p, "exists": None, "kind": "unreadable", "error": str(exc)}
    except OSError as exc:
        return {"path": p, "exists": None, "kind": "error", "error": str(exc)}

    mode = st.st_mode
    out = {
        "path": p,
        "exists": True,
        "mode": format(stat_mod.S_IMODE(mode), "04o"),
        "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
    }
    if stat_mod.S_ISLNK(mode):
        out["kind"] = "symlink"
        try:
            out["target"] = os.readlink(p)
        except OSError:
            pass
        return out
    if stat_mod.S_ISDIR(mode):
        out["kind"] = "dir"
        try:
            out["entries"] = len(os.listdir(p))
        except PermissionError:
            out["entries"] = None
            out["error"] = "permission denied"
        except OSError as exc:
            out["error"] = str(exc)
        return out
    if stat_mod.S_ISREG(mode):
        out["kind"] = "file"
        out["size"] = st.st_size
        # An empty file is not the same as a file with nothing to report;
        # the planner has already confused the two once.
        out["empty"] = st.st_size == 0
        return out
    out["kind"] = "special"
    return out


# Reading contents is a different act from reading metadata, so it gets its
# own bounds. A model handed a 40MB log will either be truncated somewhere
# arbitrary or cost a fortune; both are worse than being told the file is too
# big and which part it is getting.
MAX_READ_BYTES = 64_000
MAX_READ_LINES = 600
# Bytes that do not belong in text. A model shown decoded binary will describe
# it confidently, which is the failure this whole codebase keeps meeting.
_TEXTISH = bytes(range(0x20, 0x7F)) + b"\n\r\t\f\b"


def read_file(path: str, max_bytes: int = MAX_READ_BYTES,
              start_line: int = 1, max_lines: int = MAX_READ_LINES) -> dict:
    """The contents of one file, bounded, or why not.

    Behind the same fence as everything else, which now actually covers the
    secrets it always claimed to. Binary is reported as binary rather than
    decoded: a model shown mojibake will describe it, and a confident
    description of noise is worse than a refusal.
    """
    info = stat_path(path)
    if info.get("kind") == "fenced":
        return {"path": info["path"], "read": False, "reason": info["reason"],
                "fenced": True}
    if info.get("exists") is not True:
        return {"path": info["path"], "read": False,
                "reason": f"nothing at this path ({info.get('kind')})"}
    if info.get("kind") == "dir":
        return {"path": info["path"], "read": False,
                "reason": "this is a directory; list it instead"}
    if info.get("kind") != "file":
        return {"path": info["path"], "read": False,
                "reason": f"not a regular file ({info.get('kind')})"}
    size = int(info.get("size") or 0)
    if size == 0:
        return {"path": info["path"], "read": True, "empty": True, "size": 0,
                "text": "", "note": "the file exists and has nothing in it"}
    cap = max(1, min(int(max_bytes or MAX_READ_BYTES), MAX_READ_BYTES))
    try:
        with open(info["path"], "rb") as f:
            raw = f.read(cap + 1)
    except PermissionError:
        return {"path": info["path"], "read": False, "reason": "permission denied"}
    except OSError as exc:
        return {"path": info["path"], "read": False, "reason": str(exc)}
    truncated = len(raw) > cap
    raw = raw[:cap]
    # Not text? It may still be a document. A bill, a statement, a letter and
    # a spreadsheet are all "binary" and all things a person would upload, and
    # answering every one of them with "this looks like a binary file" makes
    # the upload button decorative. See documents.py -- and note that what it
    # could not read comes back as not read, never as nothing there.
    from jarvis.agent import documents
    kind = documents.sniff(info["path"], raw[:64])
    if kind != documents.TEXT:
        extracted = documents.extract(info["path"], raw[:4096], kind)
        if extracted is not None:
            out = {"path": info["path"], "read": bool(extracted.get("text")),
                   "size": size, "modified": info.get("modified"),
                   "format": extracted.get("format"),
                   "complete": bool(extracted.get("complete")),
                   "text": extracted.get("text", "")}
            for extra in ("pages", "pages_read", "pages_without_text",
                          "sheets", "slides", "image"):
                if extra in extracted:
                    out[extra] = extracted[extra]
            if extracted.get("note"):
                out["note"] = extracted["note"]
            if not out["read"]:
                # An unread document is not an empty one, and the difference
                # has to survive into the field the model reads first.
                out["reason"] = extracted.get("note") or "nothing could be read from this file"
            return out
    printable = sum(1 for b in raw[:4096] if b in _TEXTISH)
    if raw[:4096] and printable / len(raw[:4096]) < 0.85:
        return {"path": info["path"], "read": False, "size": size,
                "reason": "this looks like a binary file, not text"}
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    start = max(1, int(start_line or 1))
    window = max(1, min(int(max_lines or MAX_READ_LINES), MAX_READ_LINES))
    shown = lines[start - 1:start - 1 + window]
    out = {
        "path": info["path"], "read": True, "size": size,
        "modified": info.get("modified"),
        "lines_in_file": len(lines) + (1 if truncated else 0),
        "from_line": start, "lines_shown": len(shown),
        "text": "\n".join(shown),
    }
    if truncated:
        out["truncated"] = True
        out["note"] = (f"only the first {cap} bytes of {size} were read; "
                       "what is below them has not been seen")
    elif start - 1 + len(shown) < len(lines):
        out["note"] = (f"lines {start}-{start - 1 + len(shown)} of {len(lines)}; "
                       "the rest has not been seen")
    return out


def tree(root: str, depth: int = 1, limit: int = MAX_ENTRIES) -> dict:
    """A real, bounded listing of a real directory."""
    depth = max(0, min(int(depth), MAX_DEPTH))
    limit = max(1, min(int(limit), MAX_ENTRIES))
    top = stat_path(root)
    out = {"root": top["path"], "depth": depth, "root_kind": top["kind"]}
    if top["kind"] == "fenced":
        out["refused"] = top["reason"]
        return out
    if not top.get("exists"):
        out["entries"] = []
        out["note"] = top.get("error") or "path does not exist"
        return out
    if top["kind"] != "dir":
        out["entries"] = [top]
        out["note"] = "not a directory"
        return out

    entries, truncated, fenced = [], False, []
    stack = [(out["root"], 0)]
    while stack:
        current, level = stack.pop(0)
        try:
            names = sorted(os.listdir(current))
        except PermissionError:
            entries.append({"path": current, "kind": "unreadable"})
            continue
        except OSError:
            continue
        for name in names:
            child = os.path.join(current, name)
            reason = fenced_reason(child)
            if reason:
                fenced.append(child)
                continue
            if len(entries) >= limit:
                truncated = True
                break
            info = stat_path(child)
            info["depth"] = level + 1
            entries.append(info)
            if info.get("kind") == "dir" and level + 1 < depth:
                stack.append((child, level + 1))
        if truncated:
            break

    out["entries"] = entries
    out["count"] = len(entries)
    if truncated:
        out["truncated"] = f"stopped at {limit} entries"
    if fenced:
        out["fenced"] = sorted(set(fenced))
    return out


def host_facts() -> dict:
    """Identity of the machine, from the machine."""
    facts = {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "arch": platform.machine(),
    }
    try:
        with open("/etc/os-release") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    facts["os"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass
    return facts


def snapshot(paths=None, include_host: bool = True) -> dict:
    """The environment block for the planner's context.

    Missing paths are listed as missing on purpose: a model that cannot see
    an absence will invent something to fill it.
    """
    checked = [stat_path(p) for p in (paths or STANDARD_PATHS)]
    out = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "paths": checked,
        "missing": [c["path"] for c in checked if c.get("exists") is False],
    }
    if include_host:
        out["host"] = host_facts()
    return out


# --- catching an invented path -------------------------------------------

# Absolute POSIX paths with at least two segments. One segment (/tmp, and
# the UI's own /goal and /think) is too noisy to be worth reporting, and the
# inventions that matter are deep and specific.
_PATH_RE = re.compile(r"(?<![\w~:/])(/[A-Za-z0-9._+-]+)+/?")

# One segment (/tmp, and the UI's own /goal and /think) is too noisy to
# report back as an invented path, and the inventions that matter are deep
# and specific. When the operator names a path, take them at their word.
MIN_SEGMENTS_STRICT = 2
MIN_SEGMENTS_ASKED = 1


def extract_paths(text: str, limit: int = 40,
                  min_segments: int = MIN_SEGMENTS_STRICT) -> list:
    """Absolute paths named in a piece of text."""
    seen, out = set(), []
    for match in _PATH_RE.finditer(str(text or "")):
        path = match.group(0).rstrip("/.,;:")
        if path.count("/") < min_segments or path in seen:
            continue
        seen.add(path)
        out.append(path)
        if len(out) >= limit:
            break
    return out


def verify(text: str) -> dict:
    """Check every path a model named. Returns what is real and what is not."""
    present, missing, fenced, unknown = [], [], [], []
    for path in extract_paths(text):
        info = stat_path(path)
        if info["kind"] == "fenced":
            fenced.append(path)
        elif info.get("exists") is True:
            present.append(path)
        elif info.get("exists") is False:
            missing.append(path)
        else:
            unknown.append(path)
    return {
        "checked": len(present) + len(missing) + len(fenced) + len(unknown),
        "present": present,
        "missing": missing,
        "fenced": fenced,
        "unknown": unknown,
    }


def verification_note(result: dict) -> Optional[str]:
    """One line for the operator and the ledger, or None when all is well."""
    missing = (result or {}).get("missing") or []
    if not missing:
        return None
    shown = ", ".join(missing[:5])
    more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
    return (f"{len(missing)} of {result.get('checked')} paths named do not exist "
            f"on this machine: {shown}{more}")
