"""Glass Ledger v2: format, writer discipline, verifier verdicts, pins, torn tails, CLI, anchor."""

import io
import json
import logging
import os
import struct
import tempfile
import unittest
from unittest import mock

try:
    from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: F401
    HAVE_CRYPTO = True
except Exception:  # pragma: no cover
    HAVE_CRYPTO = False

from openclaw.ledger import chain, verify
from openclaw.ledger.chain import (FORMAT, GENESIS_PREV, LedgerWriter, LedgerLocked, TornTail,
                                   canonical_json, entry_hash, frame, generate_key,
                                   load_private_key, make_entry, repair_torn_tail, scan_tail,
                                   verify_signature, encode_line)
from openclaw.ledger.verify import verify_file, verify_lines, load_pin, save_pin
from openclaw.ledger.anchor import LedgerAnchor
from openclaw.ledger.agent_ledger import AgentLedger
from openclaw.ledger import __main__ as cli
from openclaw.agent.core import AgentCore, NullLedger
from openclaw.agent.planner import Task, TaskType
from openclaw.agent.executor import check_command_allowed, normalise_shell_policy
from openclaw.cloud.headless import HeadlessRunner
from openclaw.cloud import bootstrap
from openclaw.main import load_config, OpenClawSystem

needs_crypto = unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
LOG = logging.getLogger("test")


def read_lines(path):
    with open(path, "rb") as fh:
        return [ln for ln in fh.read().split(b"\n") if ln]


def write_lines(path, lines, trailing_newline=True):
    with open(path, "wb") as fh:
        fh.write(b"\n".join(lines) + (b"\n" if trailing_newline else b""))


class TestFormat(unittest.TestCase):

    def test_canonical_json_is_sorted_compact_utf8_and_rejects_nan(self):
        self.assertEqual(canonical_json({"b": 1, "a": [1, 2], "é": "ü"}),
                         '{"a":[1,2],"b":1,"é":"ü"}'.encode("utf-8"))
        with self.assertRaises(ValueError):
            canonical_json({"x": float("nan")})

    def test_length_prefix_framing_prevents_boundary_shift(self):
        self.assertNotEqual(frame(b"ab", b"c"), frame(b"a", b"bc"))
        self.assertEqual(frame(b"ab"), struct.pack(">Q", 2) + b"ab")
        self.assertEqual(frame(), b"")

    def test_entry_hash_is_deterministic_and_covers_every_field(self):
        base = ("1", "2026-07-02T20:57:27Z", "action", {"ran": "x"}, GENESIS_PREV)
        h = entry_hash(1, *base[1:])
        self.assertEqual(len(h), 64)
        self.assertEqual(h, entry_hash(1, *base[1:]))
        self.assertNotEqual(h, entry_hash(2, *base[1:]))
        self.assertNotEqual(h, entry_hash(1, "2026-07-02T20:57:28Z", *base[2:]))
        self.assertNotEqual(h, entry_hash(1, base[1], "outcome", *base[3:]))
        self.assertNotEqual(h, entry_hash(1, base[1], base[2], {"ran": "y"}, base[4]))
        self.assertNotEqual(h, entry_hash(1, base[1], base[2], base[3], "1" * 64))
        # body key order does not matter: canonical JSON
        self.assertEqual(entry_hash(1, base[1], base[2], {"a": 1, "b": 2}, base[4]),
                         entry_hash(1, base[1], base[2], {"b": 2, "a": 1}, base[4]))

    @needs_crypto
    def test_domain_separated_signature(self):
        seed, pub = generate_key()
        key = load_private_key(seed)
        e = make_entry(0, "genesis", {"format": FORMAT, "pubkey": pub}, GENESIS_PREV, key)
        self.assertTrue(verify_signature(pub, e["entry_hash"], e["sig"]))
        # a bare signature over the hash (no domain tag) is not accepted
        bare = key.sign(e["entry_hash"].encode()).hex()
        self.assertFalse(verify_signature(pub, e["entry_hash"], bare))
        # another key, a mangled sig, garbage: all False, never an exception
        _, other = generate_key()
        self.assertFalse(verify_signature(other, e["entry_hash"], e["sig"]))
        self.assertFalse(verify_signature(pub, e["entry_hash"], "zz"))
        self.assertFalse(verify_signature("nothex", e["entry_hash"], e["sig"]))

    @needs_crypto
    def test_make_entry_validates(self):
        key = load_private_key(generate_key()[0])
        with self.assertRaises(chain.LedgerError):
            make_entry(1, "bogus", {}, GENESIS_PREV, key)
        with self.assertRaises(chain.LedgerError):
            make_entry(1, "action", "not a dict", GENESIS_PREV, key)
        with self.assertRaises(chain.LedgerError):
            make_entry(1, "action", {"x": "y" * 70000}, GENESIS_PREV, key)
        line = encode_line(make_entry(1, "action", {"k": "v"}, GENESIS_PREV, key))
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(list(json.loads(line).keys()),
                         ["seq", "ts", "kind", "body", "prev_hash", "entry_hash", "sig"])


@needs_crypto
class TestWriterAndVerifier(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "ledger.jsonl")
        self.seed, self.pub = generate_key()
        self.key = load_private_key(self.seed)

    def tearDown(self):
        self.tmp.cleanup()

    def ledger(self, n=3):
        w = LedgerWriter(self.path, self.key, writer="test")
        for i in range(n):
            w.append("action", {"i": i, "text": "héllo"})
        w.close()
        return w

    def test_genesis_commits_key_and_chain_verifies(self):
        w = self.ledger(3)
        lines = read_lines(self.path)
        genesis = json.loads(lines[0])
        self.assertEqual((genesis["seq"], genesis["prev_hash"]), (0, GENESIS_PREV))
        self.assertEqual(genesis["body"]["pubkey"], self.pub)
        self.assertEqual(genesis["body"]["format"], FORMAT)
        self.assertEqual(w.head, {"seq": 3, "entry_hash": json.loads(lines[-1])["entry_hash"]})
        r = verify_file(self.path, pubkey=self.pub)
        self.assertTrue(r.ok)
        self.assertEqual((r.entries, r.head_seq, r.writer), (4, 3, "test"))
        self.assertEqual(r.kinds, {"genesis": 1, "action": 3})
        self.assertTrue(r.pinned_key)
        unpinned = verify_file(self.path)
        self.assertTrue(unpinned.ok)
        self.assertFalse(unpinned.pinned_key)
        self.assertIn("not pinned", unpinned.summary())

    def test_reopen_continues_the_chain_and_refuses_a_foreign_key(self):
        self.ledger(2)
        w = LedgerWriter(self.path, self.key, writer="test")
        self.assertEqual(w.seq, 2)
        w.append("outcome", {"ok": True})
        w.close()
        self.assertTrue(verify_file(self.path, pubkey=self.pub).ok)
        other = load_private_key(generate_key()[0])
        with self.assertRaises(chain.LedgerError):
            LedgerWriter(self.path, other, writer="test")

    def test_exclusive_lock(self):
        w = LedgerWriter(self.path, self.key)
        with self.assertRaises(LedgerLocked):
            LedgerWriter(self.path, self.key)
        w.close()
        LedgerWriter(self.path, self.key).close()

    def test_edit_insert_delete_reorder_are_caught(self):
        self.ledger(4)
        lines = read_lines(self.path)
        # edit a past body
        edited = json.loads(lines[2]); edited["body"]["i"] = 99
        write_lines(self.path, lines[:2] + [json.dumps(edited).encode()] + lines[3:])
        r = verify_file(self.path, pubkey=self.pub)
        self.assertEqual((r.ok, r.broken_seq, r.entries), (False, 2, 2))
        self.assertIn("recomputation", r.reason)
        # delete mid-chain
        write_lines(self.path, lines[:2] + lines[3:])
        r = verify_file(self.path, pubkey=self.pub)
        self.assertEqual((r.ok, r.broken_seq), (False, 2))
        self.assertIn("sequence gap", r.reason)
        # reorder
        write_lines(self.path, [lines[0], lines[2], lines[1]] + lines[3:])
        r = verify_file(self.path, pubkey=self.pub)
        self.assertFalse(r.ok)
        self.assertEqual(r.broken_seq, 1)
        # insert a forged entry with a correct-looking seq but no valid link
        forged = json.loads(lines[1]); forged["seq"] = 2
        write_lines(self.path, lines[:2] + [json.dumps(forged).encode()] + lines[2:])
        r = verify_file(self.path, pubkey=self.pub)
        self.assertEqual((r.ok, r.broken_seq), (False, 2))
        self.assertIn("prev_hash", r.reason)

    def test_rewrite_under_a_fresh_key_dies_at_genesis(self):
        self.ledger(2)
        other_seed, other_pub = generate_key()
        os.remove(self.path)
        w = LedgerWriter(self.path, load_private_key(other_seed), writer="test")
        w.append("action", {"i": 0}); w.close()
        self.assertTrue(verify_file(self.path, pubkey=other_pub).ok)
        r = verify_file(self.path, pubkey=self.pub)
        self.assertEqual((r.ok, r.broken_seq, r.entries), (False, 0, 0))
        self.assertIn("different public key", r.reason)

    def test_rollback_invisible_without_pin_caught_with_pin(self):
        self.ledger(5)
        lines = read_lines(self.path)
        head = json.loads(lines[-1])
        pin_path = os.path.join(self.tmp.name, "trusted.pin")
        good = verify_file(self.path, pubkey=self.pub)
        save_pin(pin_path, good.head_seq, good.head_hash)
        self.assertEqual(load_pin(pin_path)["seq"], 5)
        # chop the newest two entries
        write_lines(self.path, lines[:-2])
        self.assertTrue(verify_file(self.path, pubkey=self.pub).ok)  # blind spot
        r = verify_file(self.path, pubkey=self.pub, pin=load_pin(pin_path))
        self.assertEqual((r.ok, r.pin_ok), (False, False))
        self.assertIn("truncated or rolled back", r.reason)
        # regenerate the whole history with the real key, one decision reworded:
        # internally perfect, but the pinned hash differs
        os.remove(self.path)
        w = LedgerWriter(self.path, self.key, writer="test")
        for i in range(5):
            w.append("action", {"i": i, "text": "rewritten" if i == 2 else "héllo"})
        w.close()
        self.assertTrue(verify_file(self.path, pubkey=self.pub).ok)
        r = verify_file(self.path, pubkey=self.pub, pin={"seq": 5, "entry_hash": head["entry_hash"]})
        self.assertEqual((r.ok, r.pin_ok), (False, False))
        self.assertIn("regenerated", r.reason)
        # the genuine file with its own pin: fine, and a pin at genesis too
        good = verify_file(self.path, pubkey=self.pub)
        r = verify_file(self.path, pubkey=self.pub, pin={"seq": 5, "entry_hash": good.head_hash})
        self.assertTrue(r.ok and r.pin_ok)
        self.assertIn("pin seq 5 confirmed", r.summary())
        r = verify_file(self.path, pubkey=self.pub, pin={"bad": 1})
        self.assertFalse(r.ok)

    def test_torn_tail_is_intact_with_repair_but_garbage_mid_file_is_broken(self):
        self.ledger(3)
        with open(self.path, "ab") as fh:
            fh.write(b'{"seq": 4, "ts": "2026-')  # power loss mid-write
        r = verify_file(self.path, pubkey=self.pub)
        self.assertTrue(r.ok)
        self.assertTrue(r.torn_tail)
        self.assertEqual(r.entries, 4)
        self.assertIn("torn tail", r.summary())
        size = os.path.getsize(self.path)
        self.assertEqual(r.torn_offset, size - len(b'{"seq": 4, "ts": "2026-'))
        # a pin at or past the torn entry still fails the run
        r = verify_file(self.path, pubkey=self.pub, pin={"seq": 4, "entry_hash": "0" * 64})
        self.assertFalse(r.ok)
        # the writer refuses to append onto a torn tail
        with self.assertRaises(TornTail) as cm:
            LedgerWriter(self.path, self.key)
        self.assertEqual(cm.exception.offset, r.torn_offset)
        self.assertIn("openclaw.ledger repair", str(cm.exception))
        # the documented repair removes only the partial line
        removed = repair_torn_tail(self.path)
        self.assertEqual(removed, len(b'{"seq": 4, "ts": "2026-'))
        self.assertIsNone(repair_torn_tail(self.path))
        self.assertTrue(verify_file(self.path, pubkey=self.pub).ok)
        w = LedgerWriter(self.path, self.key); w.append("action", {"after": "repair"}); w.close()
        self.assertEqual(verify_file(self.path, pubkey=self.pub).entries, 5)
        # garbage before the end is BROKEN, not torn
        lines = read_lines(self.path)
        write_lines(self.path, lines[:2] + [b"not json at all"] + lines[2:])
        r = verify_file(self.path, pubkey=self.pub)
        self.assertEqual((r.ok, r.torn_tail, r.broken_seq), (False, False, 2))
        # a complete final line without its newline still verifies
        write_lines(self.path, lines, trailing_newline=False)
        r = verify_file(self.path, pubkey=self.pub)
        self.assertTrue(r.ok and not r.torn_tail)

    def test_hostile_input_returns_verdicts_not_tracebacks(self):
        cases = [b"", b"\n\n", b"[]\n", b"null\n", b'{"seq": "0"}\n', b"\xff\xfe\n",
                 b'{"seq": 0, "ts": 1, "kind": [], "body": 1, "prev_hash": 2, "entry_hash": 3, "sig": 4}\n',
                 b'{"seq": true, "ts": "", "kind": "genesis", "body": {}, "prev_hash": "", "entry_hash": "", "sig": ""}\n',
                 b'{"seq": 0, "ts": "", "kind": "genesis", "body": {"format": "glass-ledger/v2", "pubkey": "zz"}, "prev_hash": "%s", "entry_hash": "x", "sig": "y"}\n' % GENESIS_PREV.encode()]
        for raw in cases:
            with open(self.path, "wb") as fh:
                fh.write(raw)
            r = verify_file(self.path, pubkey=self.pub)
            self.assertFalse(r.ok, raw)
            self.assertTrue(r.reason, raw)
        self.assertFalse(verify_file(os.path.join(self.tmp.name, "missing")).ok)
        # a scan of a hostile tail never raises either
        for raw in cases:
            with open(self.path, "wb") as fh:
                fh.write(raw)
            scan_tail(self.path)

    def test_scan_tail_reads_only_the_end(self):
        self.ledger(2)
        last, torn, size = scan_tail(self.path)
        self.assertEqual((last["seq"], torn, size), (2, None, os.path.getsize(self.path)))
        with open(self.path, "ab") as fh:
            fh.write(b"garbage\n")
        last, torn, size = scan_tail(self.path)
        self.assertEqual(last["seq"], 2)
        self.assertEqual(torn, size - len(b"garbage\n"))


@needs_crypto
class TestCli(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "ledger.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_keygen_verify_pin_tail_repair(self):
        keydir = os.path.join(self.tmp.name, "keys")
        code, out = self.run_cli("keygen", keydir)
        self.assertEqual(code, 0)
        pub = out.split("public key:")[1].strip()
        self.assertEqual(self.run_cli("keygen", keydir)[0], 1)  # never overwrites
        code, out = self.run_cli("pubkey", os.path.join(keydir, "ed25519.key"))
        self.assertEqual((code, out.strip()), (0, pub))
        with open(os.path.join(keydir, "ed25519.key")) as fh:
            key = load_private_key(fh.read())
        w = LedgerWriter(self.path, key, writer="cli"); w.append("decision", {"d": 1}); w.close()
        pin = os.path.join(self.tmp.name, "trusted.pin")
        # the paper's form: <ledger> --pubkey … --pin …
        code, out = self.run_cli(self.path, "--pubkey", pub, "--pin", pin)
        self.assertEqual(code, 0)
        self.assertIn("INTACT", out)
        self.assertIn("pin advanced to seq 1", out)
        self.assertEqual(load_pin(pin)["seq"], 1)
        code, out = self.run_cli("verify", self.path, "--pubkey", pub, "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])
        code, out = self.run_cli("tail", self.path, "-n", "1")
        self.assertEqual(code, 0)
        self.assertIn("decision", out)
        # roll back, verify against the pin: BROKEN, exit 1, pin untouched
        lines = read_lines(self.path)
        write_lines(self.path, lines[:-1])
        code, out = self.run_cli("verify", self.path, "--pubkey", pub, "--pin", pin)
        self.assertEqual(code, 1)
        self.assertIn("BROKEN", out)
        self.assertEqual(load_pin(pin)["seq"], 1)
        # torn tail: INTACT with a repair line; repair; nothing left to repair
        write_lines(self.path, lines)
        with open(self.path, "ab") as fh:
            fh.write(b'{"seq": 2')
        code, out = self.run_cli("verify", self.path, "--pubkey", pub)
        self.assertEqual(code, 0)
        self.assertIn("repair:", out)
        self.assertEqual(self.run_cli("repair", self.path)[0], 0)
        self.assertIn("nothing to repair", self.run_cli("repair", self.path)[1])
        # bad pin file and missing ledger: verdicts, exit 1, no traceback
        with open(pin, "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.run_cli("verify", self.path, "--pubkey", pub, "--pin", pin)[0], 1)
        self.assertEqual(self.run_cli("verify", "/nonexistent/ledger.jsonl")[0], 1)
        self.assertEqual(self.run_cli("tail", "/nonexistent/ledger.jsonl")[0], 1)


class FakeS3:
    def __init__(self, fail=False):
        self.objects = {}
        self.fail = fail
        self.calls = 0

    def put_object(self, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("AccessDenied")
        self.objects[kw["Key"]] = kw


@needs_crypto
class TestAnchorAndAgentLedger(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"enabled": True, "path": os.path.join(self.tmp.name, "var", "ledger.jsonl"),
                    "key_file": os.path.join(self.tmp.name, "etc", "ed25519.key"),
                    "pubkey_file": os.path.join(self.tmp.name, "etc", "ed25519.pub")}

    def tearDown(self):
        self.tmp.cleanup()

    def test_agent_ledger_creates_key_records_and_survives_restart(self):
        led = AgentLedger(self.cfg, LOG, writer="agent-1")
        self.assertTrue(led.available)
        self.assertEqual(oct(os.stat(self.cfg["key_file"]).st_mode & 0o777), "0o600")
        with open(self.cfg["pubkey_file"]) as fh:
            self.assertEqual(fh.read().strip(), led.pubkey)
        self.assertTrue(led.record("decision", {"reasoning": "x"}))
        self.assertTrue(led.record("action", {"task": "y"}))
        self.assertEqual(led.head["seq"], 2)
        self.assertIsNone(led.gate())
        st = led.status()
        self.assertEqual((st["enabled"], st["available"], st["entries_this_session"]), (True, True, 2))
        self.assertEqual(len(led.tail(1)), 1)
        self.assertTrue(led.verify()["ok"])
        led.close()
        again = AgentLedger(self.cfg, LOG, writer="agent-1")
        self.assertEqual((again.available, again.head["seq"], again.pubkey), (True, 2, led.pubkey))
        again.close()

    def test_fail_closed_on_torn_tail_and_lock(self):
        led = AgentLedger(self.cfg, LOG, writer="a"); led.record("action", {}); led.close()
        with open(self.cfg["path"], "ab") as fh:
            fh.write(b'{"seq": 2, "ts')
        torn = AgentLedger(self.cfg, LOG, writer="a")
        self.assertFalse(torn.available)
        self.assertIn("torn tail", torn.reason)
        self.assertIn("fail-closed", torn.gate())
        self.assertFalse(torn.record("action", {}))
        st = torn.status()
        self.assertEqual(st["available"], False)
        self.assertIn("repair", st["reason"])
        repair_torn_tail(self.cfg["path"])
        holder = AgentLedger(self.cfg, LOG, writer="a")
        self.assertTrue(holder.available)
        locked = AgentLedger(self.cfg, LOG, writer="a")
        self.assertFalse(locked.available)
        self.assertIn("another writer", locked.reason)
        holder.close()
        # fail_closed off: the reason is reported but nothing is gated
        cfg = dict(self.cfg, fail_closed=False)
        with open(self.cfg["path"], "ab") as fh:
            fh.write(b"torn again")
        soft = AgentLedger(cfg, LOG, writer="a")
        self.assertFalse(soft.available)
        self.assertIsNone(soft.gate())
        self.assertFalse(soft.record("action", {}))
        # disabled ledger: records are accepted (no-op), never gated
        off = AgentLedger({"enabled": False}, LOG)
        self.assertTrue(off.record("action", {}))
        self.assertIsNone(off.gate())
        self.assertEqual(off.status()["enabled"], False)

    def test_anchor_uploads_checkpoint_and_copy_and_retries(self):
        s3 = FakeS3()
        cfg = dict(self.cfg, anchor={"bucket": "witness", "prefix": "ledger", "every_s": 15, "copy": True})
        led = AgentLedger(cfg, LOG, writer="agent-1", instance_id="i-123", anchor_client=s3)
        led.record("action", {"n": 1})
        self.assertTrue(led.anchor.enabled)
        self.assertTrue(led.anchor.due())             # the first upload is not delayed
        self.assertTrue(led.anchor.flush())
        self.assertEqual(set(s3.objects), {"ledger/agent-1/checkpoints/000000000001.json",
                                           "ledger/agent-1/checkpoint.json",
                                           "ledger/agent-1/ledger.jsonl"})
        cp = json.loads(s3.objects["ledger/agent-1/checkpoint.json"]["Body"])
        self.assertEqual((cp["seq"], cp["pubkey"], cp["instance_id"], cp["format"]),
                         (1, led.pubkey, "i-123", FORMAT))
        self.assertEqual(cp["entry_hash"], led.head["entry_hash"])
        self.assertIn("ContentMD5", s3.objects["ledger/agent-1/ledger.jsonl"])
        # the copy verifies on its own, off-box
        copy_path = os.path.join(self.tmp.name, "copy.jsonl")
        with open(copy_path, "wb") as fh:
            fh.write(s3.objects["ledger/agent-1/ledger.jsonl"]["Body"])
        self.assertTrue(verify_file(copy_path, pubkey=led.pubkey, pin={"seq": cp["seq"], "entry_hash": cp["entry_hash"]}).ok)
        st = led.status()["anchor"]
        self.assertEqual((st["stats"]["checkpoints"], st["stats"]["copies"], st["pending"]), (1, 1, None))
        # nothing new: nothing uploaded; new entry: due after every_s
        self.assertFalse(led.anchor.flush(force=True))
        led.record("outcome", {"n": 2})
        self.assertTrue(led.anchor.due() is False)
        led.anchor._last_flush = 0
        self.assertTrue(led.anchor.due())
        led.tick()
        self.assertEqual(s3.objects["ledger/agent-1/checkpoint.json"] and json.loads(s3.objects["ledger/agent-1/checkpoint.json"]["Body"])["seq"], 2)
        # failures never block the agent and are reported
        s3.fail = True
        led.record("action", {"n": 3})
        self.assertFalse(led.anchor.flush(force=True))
        self.assertTrue(led.record("action", {"n": 4}))
        st = led.status()["anchor"]
        self.assertEqual(st["stats"]["failures"], 1)
        self.assertIn("AccessDenied", st["last_error"])
        self.assertIsNotNone(st["pending"])
        led.close()  # close flushes; still failing, still no exception

    def test_anchor_disabled_without_bucket(self):
        a = LedgerAnchor({}, LOG, "/nonexistent", "pub", "w")
        self.assertFalse(a.enabled)
        a.notify({"seq": 1, "entry_hash": "x"})
        self.assertFalse(a.due())
        self.assertFalse(a.flush(force=True))
        self.assertEqual(a.status()["bucket"], None)


NO_HW = {"display": None, "input": None, "memory": None, "storage": None}


class FakeBrain:
    """Minimal planner double: returns the decisions queued on it."""
    provider = "fake"
    model = "fake-1"

    def __init__(self, decisions):
        from openclaw.brain.llm import Decision
        self.Decision = Decision
        self.decisions = list(decisions)
        self.chats = 0

    def should_plan(self, cycle):
        return True

    def available(self):
        return True

    def unavailable_reason(self):
        return None

    last_error = None

    def plan(self, agent, observations):
        if not self.decisions:
            return self.Decision(reasoning="nothing left")
        return self.decisions.pop(0)

    def chat(self, agent, turns, observations=None):
        self.chats += 1
        return "answer " + str(self.chats)

    def status(self):
        return {"model": self.model}


@needs_crypto
class TestAgentIntegration(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"enabled": True, "path": os.path.join(self.tmp.name, "ledger.jsonl"),
                    "key_file": os.path.join(self.tmp.name, "keys", "ed25519.key"),
                    "pubkey_file": os.path.join(self.tmp.name, "keys", "ed25519.pub")}
        self.led = AgentLedger(self.cfg, LOG, writer="t")

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def decisions(self):
        from openclaw.brain.llm import Decision
        shell = Task(priority=2, description="Measure root", task_type=TaskType.SHELL_COMMAND,
                     metadata={"source": "llm", "command": "echo hi", "goal": "Keep root under 80%"})
        denied = Task(priority=2, description="Wipe", task_type=TaskType.SHELL_COMMAND,
                      metadata={"source": "llm", "command": "rm -rf /"})
        return [Decision(reasoning="measure first", task=shell, note="root is /dev/root"),
                Decision(reasoning="be evil", task=denied),
                Decision(reasoning="done", completed_goals=["Keep root under 80%"])]

    def agent(self, ledger=None, brain=None):
        agent = AgentCore({"name": "t", "profile": "cloud"}, dict(NO_HW), LOG,
                          brain=brain, shell_policy={"enabled": True, "timeout": 5},
                          ledger=ledger)
        agent.planner._boot_tasks_generated = True
        return agent

    def kinds(self):
        return [(e["kind"], e["body"]) for e in self.led.tail(100)]

    def test_decisions_actions_outcomes_and_gates_are_journaled(self):
        brain = FakeBrain(self.decisions())
        agent = self.agent(self.led, brain)
        agent.add_goal("Keep root under 80%", 3)
        first = agent.run_cycle()
        self.assertTrue(first["result"]["success"])
        second = agent.run_cycle()
        self.assertFalse(second["result"]["success"])
        third = agent.run_cycle()
        self.assertEqual(third.get("action", "idle"), "idle")
        entries = self.kinds()
        kinds = [k for k, _ in entries]
        self.assertEqual(kinds[0], "genesis")
        self.assertEqual(kinds[1:], ["action", "decision", "action", "outcome",
                                     "decision", "action", "gate", "outcome", "decision"])
        goal = entries[1][1]
        self.assertEqual((goal["actor"], goal["action"], goal["description"]), ("operator", "add_goal", "Keep root under 80%"))
        decision = entries[2][1]
        self.assertEqual(decision["reasoning"], "measure first")
        self.assertEqual(decision["task"]["command"], "echo hi")
        self.assertEqual(decision["note"], "root is /dev/root")
        action = entries[3][1]
        self.assertEqual((action["type"], action["command"], action["source"]), ("shell_command", "echo hi", "llm"))
        outcome = entries[4][1]
        self.assertTrue(outcome["success"])
        self.assertEqual(len(outcome["output"]["sha256"]), 64)
        self.assertIn("hi", outcome["output"]["head"])
        gate = entries[7][1]
        self.assertEqual(gate["gate"], "shell_policy")
        self.assertEqual(gate["command"], "rm -rf /")
        self.assertIn("rm", gate["reason"])
        self.assertFalse(entries[8][1]["success"])
        self.assertEqual(entries[9][1]["completed_goals"], ["Keep root under 80%"])
        self.assertIsNone(entries[9][1]["task"])
        # chat and uploads are journaled too, as digests, not transcripts
        self.assertEqual(agent.chat([{"role": "user", "content": "how are we?"}]), "answer 1")
        upload = os.path.join(self.tmp.name, "notes.txt")
        with open(upload, "w") as fh:
            fh.write("hello")
        agent.record_upload("notes.txt", upload, 5)
        entries = self.kinds()
        thought = entries[-2][1]
        self.assertEqual((entries[-2][0], thought["kind"], thought["question"]), ("thought", "chat", "how are we?"))
        self.assertEqual(thought["answer_sha256"], chain.hashlib.sha256(b"answer 1").hexdigest())
        self.assertNotIn("answer 1\n", json.dumps(thought))
        up = entries[-1][1]
        self.assertEqual((up["action"], up["name"], up["sha256"]),
                         ("upload", "notes.txt", chain.hashlib.sha256(b"hello").hexdigest()))
        # the whole file verifies under the pinned key, and status is visible to the operator
        self.assertTrue(verify_file(self.cfg["path"], pubkey=self.led.pubkey).ok)
        status = agent.get_status()["ledger"]
        self.assertEqual((status["available"], status["head"]["seq"]), (True, len(entries) - 1))
        # the brain's context never carries the ledger
        from openclaw.brain.llm import BaseBrain
        ctx_keys = set(BaseBrain.build_context.__code__.co_names)
        self.assertNotIn("ledger", ctx_keys)

    def test_fail_closed_refuses_to_plan_act_or_answer(self):
        brain = FakeBrain(self.decisions())
        agent = self.agent(self.led, brain)
        agent.add_goal("Keep root under 80%", 3)
        # break the ledger under the agent's feet: the writer fails on the next append
        self.led.writer.close()
        self.led.writer._fd = -1
        # the failure surfaces on the next append: the decision is taken but the
        # action is refused, not run, and the ledger goes unavailable
        result = agent.run_cycle()
        self.assertEqual(result["action"], "Measure root")
        self.assertFalse(result["result"]["success"])
        self.assertTrue(result["result"]["refused"])
        self.assertFalse(self.led.available)
        self.assertIn("fail-closed", agent.ledger.gate())
        self.assertEqual(len(brain.decisions), 2)
        # from then on: no plan, no brain call, idle cycles
        result = agent.run_cycle()
        self.assertEqual(result.get("action", "idle"), "idle")
        self.assertEqual(len(brain.decisions), 2)
        task = Task(priority=1, description="direct", task_type=TaskType.SHELL_COMMAND,
                    metadata={"source": "llm", "command": "echo x"})
        refused = agent.act(task)
        self.assertFalse(refused["success"])
        self.assertTrue(refused["refused"])
        self.assertIn("fail-closed", refused["error"])
        self.assertIn("Not answering", agent.chat([{"role": "user", "content": "hi"}]))
        self.assertEqual(brain.chats, 0)
        self.assertIn("fail-closed", agent.think()["error"])
        # with fail_closed off the agent carries on, unrecorded but reported
        soft = AgentLedger(dict(self.cfg, fail_closed=False, path=os.path.join(self.tmp.name, "soft.jsonl")), LOG)
        soft.writer.close(); soft.writer._fd = -1
        agent2 = self.agent(soft, FakeBrain(self.decisions()))
        agent2.add_goal("Keep root under 80%", 3)
        self.assertTrue(agent2.run_cycle()["result"]["success"])
        self.assertFalse(agent2.get_status()["ledger"]["available"])

    def test_null_ledger_when_not_configured(self):
        agent = self.agent(None, FakeBrain(self.decisions()))
        self.assertIsInstance(agent.ledger, NullLedger)
        agent.add_goal("Keep root under 80%", 3)
        self.assertTrue(agent.run_cycle()["result"]["success"])
        self.assertEqual(agent.get_status()["ledger"], {"enabled": False})

    def test_shell_policy_denies_reading_or_touching_the_ledger(self):
        policy = normalise_shell_policy({"enabled": True}, LOG)
        for cmd in ("cat /var/lib/openclaw/ledger.jsonl", "ls /etc/openclaw/ledger",
                    "python3 -m openclaw.ledger repair /var/lib/openclaw/ledger.jsonl",
                    "echo x >> /var/lib/openclaw/ledger.jsonl", "cp /etc/openclaw/ledger/ed25519.key /tmp"):
            self.assertIsNotNone(check_command_allowed(cmd, policy), cmd)
        self.assertIsNone(check_command_allowed("df -h /var/lib/openclaw/uploads", policy))

    def test_headless_ledger_endpoints(self):
        import urllib.request
        agent = self.agent(self.led, FakeBrain(self.decisions()))
        runner = HeadlessRunner(agent, LOG, interval=0, status_port=0, token="t0k", token_file=None)
        port = runner.start_status_server()  # ephemeral loopback port
        try:
            base = f"http://127.0.0.1:{port}"
            def get(path):
                req = urllib.request.Request(base + path, headers={"Authorization": "Bearer t0k"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    return json.loads(r.read())
            agent.add_goal("Keep root under 80%", 3)
            st = get("/ledger")
            self.assertEqual((st["enabled"], st["available"], st["pubkey"]), (True, True, self.led.pubkey))
            tail = get("/ledger/tail?limit=1")
            self.assertEqual(tail[0]["kind"], "action")
            self.assertEqual(get("/ledger/pubkey"), {"pubkey": self.led.pubkey, "format": "glass-ledger/v2"})
            self.assertTrue(get("/ledger/verify")["ok"])
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/ledger", timeout=5)
            self.assertEqual(cm.exception.code, 401)
        finally:
            runner.stop_status_server()

    def test_bootstrap_tag_sets_anchor_bucket(self):
        class Imds:
            def user_data(self):
                return ""
            def summary(self):
                return {"instance_id": "i-1", "region": "us-west-2"}
            def tags(self):
                return {"openclaw:ledger-bucket": "openclaw-ledger-1-agent", "openclaw:name": "agent"}
        config = bootstrap.build_cloud_config(Imds())
        self.assertEqual(config["ledger"]["anchor"]["bucket"], "openclaw-ledger-1-agent")
        self.assertEqual(config["agent"]["name"], "agent")

    def test_system_opens_ledger_from_config(self):
        cfg_path = os.path.join(self.tmp.name, "config.yaml")
        with open(cfg_path, "w") as fh:
            fh.write(f"""agent:
  name: sys-test
  profile: cloud
ledger:
  enabled: true
  path: {os.path.join(self.tmp.name, 'sys', 'ledger.jsonl')}
  key_file: {os.path.join(self.tmp.name, 'sys', 'ed25519.key')}
  pubkey_file: {os.path.join(self.tmp.name, 'sys', 'ed25519.pub')}
""")
        self.led.close()
        config = load_config(cfg_path)
        self.assertTrue(config["ledger"]["enabled"])
        self.assertTrue(config["ledger"]["fail_closed"])
        system = OpenClawSystem(config, LOG)
        led = system.build_ledger()
        self.assertTrue(led.available)
        led.close()


if __name__ == "__main__":
    unittest.main()
