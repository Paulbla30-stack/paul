"""Tests for the accept function.

The property under test is not "approval works". It is that nothing
reaches a network without Paul having deliberately said yes.

    python3 tests/test_approval.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import approval                                          # noqa: E402
from proposals import (AlreadyDecided, Proposal, PENDING, APPROVED,  # noqa: E402
                       REJECTED, EXPIRED, TERMINAL)

SECRET = "x" * 40
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _proposal(**kw):
    p = Proposal.new(
        kind="comment", network="moltbook",
        target_url="https://www.moltbook.com/post/abc",
        target_title="Clinical workflows are the real bottleneck",
        draft="The Cultural Safety Case (CC BY 4.0) argues deployment risk is "
              "capability x culture. Full text: https://doi.org/10.5281/zenodo.21443120 "
              "Disclosure: I am Paul Blatherwick's agent and this is his own paper.",
        rationale="Post argues model quality is not the bottleneck; the paper "
                  "makes the same argument with an instrument behind it.",
        discloses=["author is the operator", "post is automated"],
        now=NOW)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


# ------------------------------------------------------------------ tokens
def test_token_roundtrips():
    t = approval.mint(SECRET, "p1", now=NOW.timestamp())
    assert approval.verify(SECRET, t, now=NOW.timestamp())["p"] == "p1"


def test_token_default_scope_is_decide_not_approve():
    # There must be no URL in an email that means "yes".
    t = approval.mint(SECRET, "p1", now=NOW.timestamp())
    assert approval.verify(SECRET, t, now=NOW.timestamp())["a"] == "decide"


def test_token_rejects_wrong_secret():
    t = approval.mint(SECRET, "p1", now=NOW.timestamp())
    try:
        approval.verify("y" * 40, t, now=NOW.timestamp())
        raise AssertionError("accepted a token signed with the wrong key")
    except approval.BadToken:
        pass


def test_token_rejects_tampered_payload():
    t = approval.mint(SECRET, "p1", now=NOW.timestamp())
    body, sig = t.rsplit(".", 1)
    forged = approval.mint(SECRET, "p2", now=NOW.timestamp()).rsplit(".", 1)[0]
    try:
        approval.verify(SECRET, f"{forged}.{sig}", now=NOW.timestamp())
        raise AssertionError("accepted a swapped payload")
    except approval.BadToken:
        pass


def test_token_expires():
    t = approval.mint(SECRET, "p1", ttl_s=60, now=NOW.timestamp())
    try:
        approval.verify(SECRET, t, now=NOW.timestamp() + 61)
        raise AssertionError("accepted an expired token")
    except approval.BadToken as e:
        assert str(e) == "expired"


def test_garbage_tokens_are_rejected_not_crashed():
    for bad in ("", "nodot", "a.b", "....", "x" * 500, "?.!"):
        try:
            approval.verify(SECRET, bad, now=NOW.timestamp())
            raise AssertionError(f"accepted {bad!r}")
        except approval.BadToken:
            pass


# --------------------------------------------------------------- lifecycle
def test_new_proposal_starts_pending_and_unposted():
    p = _proposal()
    assert p.status == PENDING
    assert p.decided_at is None and p.decided_by is None


def test_proposal_expires_in_the_future_not_the_past():
    p = _proposal()
    assert datetime.fromisoformat(p.expires_at) > datetime.fromisoformat(p.created_at)


def test_terminal_states_do_not_include_pending_or_approved():
    # approved is not terminal: it still has to be sent.
    assert PENDING not in TERMINAL and APPROVED not in TERMINAL
    assert REJECTED in TERMINAL and EXPIRED in TERMINAL


def test_draft_is_stored_verbatim():
    # Paul approves exact text, never a summary of it.
    p = _proposal()
    assert "doi.org/10.5281/zenodo.21443120" in p.draft
    assert p.draft == _proposal().draft


# ------------------------------------------------- the GET/POST separation
class _FakeStore:
    """Records whether anything was mutated."""
    def __init__(self, p):
        self.p = p
        self.decisions = []

    def get(self, pid):
        return self.p if pid == self.p.id else None

    def decide(self, pid, action, who, now=None):
        if self.p.status != PENDING:
            raise AlreadyDecided(self.p)
        self.decisions.append((pid, action, who))
        self.p.status = APPROVED if action == "approve" else REJECTED
        self.p.decided_at = NOW.isoformat()
        self.p.decided_by = who
        return self.p


def _handler_with(monkey_store, monkey_chain=True):
    import approve_app
    approve_app.ProposalStore = lambda table: monkey_store
    approval._CACHED = SECRET
    if monkey_chain:
        class _NullChain:
            def __init__(self, *a, **k): pass
            def append(self, *a, **k): return {"seq": 1}
        approve_app.Chain = lambda store: _NullChain()
        approve_app.DynamoChainStore = lambda t: None
    os.environ["SCOUT_TABLE"] = "test-table"
    return approve_app


def _event(method, token, action=None):
    e = {"requestContext": {"http": {"method": method}}}
    if method == "GET":
        e["queryStringParameters"] = {"t": token}
    else:
        body = f"t={token}" + (f"&action={action}" if action else "")
        e["body"] = body
    return e


def test_GET_never_changes_anything():
    """The whole reason the gate is two-step.

    Mail clients and link scanners prefetch URLs found in email. If a GET
    could approve, the scanner would approve before Paul read the message.
    """
    p = _proposal()
    store = _FakeStore(p)
    app = _handler_with(store)
    token = approval.mint(SECRET, p.id)
    r = app.handler(_event("GET", token), None)
    assert r["statusCode"] == 200
    assert store.decisions == [], "a GET mutated state"
    assert p.status == PENDING
    assert "Approve and post" in r["body"]      # it rendered the form
    assert p.draft[:40] in r["body"] or "capability x culture" in r["body"]


def test_POST_with_approve_decides_once():
    p = _proposal()
    store = _FakeStore(p)
    app = _handler_with(store)
    token = approval.mint(SECRET, p.id)
    r = app.handler(_event("POST", token, "approve"), None)
    assert r["statusCode"] == 200
    assert store.decisions == [(p.id, "approve", "paul")]
    assert p.status == APPROVED


def test_replayed_POST_cannot_decide_twice():
    p = _proposal()
    store = _FakeStore(p)
    app = _handler_with(store)
    token = approval.mint(SECRET, p.id)
    app.handler(_event("POST", token, "approve"), None)
    r = app.handler(_event("POST", token, "approve"), None)
    assert len(store.decisions) == 1, "a replayed link decided twice"
    assert "Already" in r["body"]


def test_POST_without_an_action_does_nothing():
    p = _proposal()
    store = _FakeStore(p)
    app = _handler_with(store)
    r = app.handler(_event("POST", approval.mint(SECRET, p.id)), None)
    assert store.decisions == []
    assert r["statusCode"] == 400


def test_bad_token_cannot_decide():
    p = _proposal()
    store = _FakeStore(p)
    app = _handler_with(store)
    r = app.handler(_event("POST", "forged.token", "approve"), None)
    assert store.decisions == []
    assert r["statusCode"] == 400
    assert p.status == PENDING


def test_other_methods_are_refused():
    p = _proposal()
    store = _FakeStore(p)
    app = _handler_with(store)
    r = app.handler(_event("DELETE", approval.mint(SECRET, p.id)), None)
    assert r["statusCode"] == 405
    assert store.decisions == []


def test_page_escapes_a_hostile_draft():
    p = _proposal(draft="<script>alert(1)</script> and <img onerror=x>")
    store = _FakeStore(p)
    app = _handler_with(store)
    r = app.handler(_event("GET", approval.mint(SECRET, p.id)), None)
    assert "<script>alert" not in r["body"]
    assert "&lt;script&gt;" in r["body"]


def test_page_is_noindex_and_uncached():
    p = _proposal()
    app = _handler_with(_FakeStore(p))
    r = app.handler(_event("GET", approval.mint(SECRET, p.id)), None)
    assert r["headers"]["Cache-Control"] == "no-store"
    assert "noindex" in r["body"]
    assert "form-action 'self'" in r["headers"]["Content-Security-Policy"]


# ------------------------------------------------------------- digest honesty
def test_digest_footer_does_not_claim_nothing_was_drafted_when_it_was():
    """The email must not lie about what it is.

    The Stage 1 footer says the scout "has not drafted anything". Once
    proposals exist that is false, and a footer that contradicts the body
    it sits under is worse than no footer.
    """
    import digest
    stats = {"fetched": 1, "new": 1, "above": 1, "sources_ok": ["medrxiv"],
             "sources_failed": [], "chain_entries": 1}
    rec = {"source": "medrxiv", "title": "T", "url": "https://x", "author": "",
           "published": "2026-09-18T00:00:00+00:00", "score": 9.0, "why": "w"}
    p = _proposal()

    _, text, html_body = digest.build([rec], run_stats=stats, threshold=5.0,
                                      proposals=[p], approve_url="https://u/",
                                      mint=lambda i: "T")
    assert "drafted anything" not in text
    assert "drafted anything" not in html_body
    assert "posted nothing" in text

    _, text2, _ = digest.build([rec], run_stats=stats, threshold=5.0)
    assert "drafted anything" in text2          # still true when there are none


def test_digest_link_says_decide_not_approve():
    import digest
    stats = {"fetched": 1, "new": 1, "above": 1, "sources_ok": ["medrxiv"],
             "sources_failed": [], "chain_entries": 1}
    rec = {"source": "medrxiv", "title": "T", "url": "https://x", "author": "",
           "published": "2026-09-18T00:00:00+00:00", "score": 9.0, "why": "w"}
    _, text, html_body = digest.build(
        [rec], run_stats=stats, threshold=5.0, proposals=[_proposal()],
        approve_url="https://u/", mint=lambda i: "TOK")
    # No URL in the email may mean "yes".
    assert "approve" not in text.lower().split("decide:")[1].split("\n")[0]
    assert "Review and decide" in html_body



# ------------------------------------------------------------- the sending
import contextlib


@contextlib.contextmanager
def _stubbed(app, **attrs):
    """Patch module attributes and put them back.

    Without the restore, a stub set by one test leaks into every test that
    sorts after it — which is exactly how an earlier version of this file
    made the send-failure test pass by accident.
    """
    saved = {k: getattr(app, k) for k in attrs}
    for k, v in attrs.items():
        setattr(app, k, v)
    try:
        yield app
    finally:
        for k, v in saved.items():
            setattr(app, k, v)


def _approve_with_outcome(outcome):
    """Approve a proposal with the send stubbed to a given outcome."""
    p = _proposal()
    store = _FakeStore(p)
    store.table = type("T", (), {"update_item": lambda self, **kw: None})()
    app = _handler_with(store)
    with _stubbed(app,
                  _send=lambda proposal, table: outcome,
                  _record=lambda *a, **k: None,
                  _set_status=lambda *a, **k: None):
        r = app.handler(_event("POST", approval.mint(SECRET, p.id), "approve"), None)
    return r, p


def test_a_published_send_says_posted():
    r, _ = _approve_with_outcome({
        "status": "published", "published": True,
        "detail": "published (challenge solved by solver)",
        "url": "https://www.moltbook.com/post/abc"})
    assert "Posted" in r["body"]
    assert "moltbook.com/post/abc" in r["body"]


def test_created_but_unverified_is_never_reported_as_posted():
    """The failure mode worth a test of its own.

    Moltbook accepts the content, returns a challenge, and hides the content
    until it is answered. If the answer never lands, the content does not
    exist for anyone. Reporting that as "posted" would only surface weeks
    later when Paul goes looking for something that was never there.
    """
    r, _ = _approve_with_outcome({
        "status": "created_but_unverified", "published": False,
        "detail": "created, but the challenge could not be solved, so it is "
                  "not visible. It expires unposted."})
    body = r["body"]
    assert "Not posted" in body
    assert "not visible" in body
    # The word "Posted" must not appear as the headline.
    assert "<p class=big>Posted</p>" not in body


def test_rate_limited_is_reported_as_not_posted():
    r, _ = _approve_with_outcome({
        "status": "rate_limited", "published": False,
        "detail": "Moltbook rate limit: one post every 30 minutes."})
    assert "rate limited" in r["body"].lower()
    assert "<p class=big>Posted</p>" not in r["body"]


def test_a_send_that_throws_does_not_report_success():
    """The real _send must swallow nothing into a false success.

    The sender is stubbed to raise rather than left to reach AWS — a unit
    test that can touch the live network is not a unit test.
    """
    p = _proposal()
    store = _FakeStore(p)
    store.table = type("T", (), {"update_item": lambda self, **kw: None})()
    app = _handler_with(store)

    def boom(proposal, table):
        raise RuntimeError("ssm unreachable")

    with _stubbed(app, _record=lambda *a, **k: None,
                  _set_status=lambda *a, **k: None):
        # Exercise the real _send's own catch-all by breaking what it calls.
        import sender
        saved = sender.key_from_ssm
        sender.key_from_ssm = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("ssm unreachable"))
        try:
            r = app.handler(
                _event("POST", approval.mint(SECRET, p.id), "approve"), None)
        finally:
            sender.key_from_ssm = saved

    assert "<p class=big>Posted</p>" not in r["body"]
    assert "nothing was posted" in r["body"]


def test_rejecting_still_sends_nothing():
    p = _proposal()
    store = _FakeStore(p)
    app = _handler_with(store)
    called = []
    with _stubbed(app,
                  _send=lambda *a, **k: called.append(1) or {
                      "status": "published", "published": True, "detail": ""},
                  _record=lambda *a, **k: None,
                  _set_status=lambda *a, **k: None):
        r = app.handler(_event("POST", approval.mint(SECRET, p.id), "reject"), None)
    assert called == [], "rejecting must not reach the sender"
    assert "Rejected" in r["body"]


def test_only_the_sender_can_write_to_moltbook():
    """One write path in the project, and the scout cannot reach it.

    Checked against the parsed code rather than the raw text, so prose in a
    docstring explaining the write path is not mistaken for one.
    """
    import ast
    import glob
    src_dir = os.path.join(ROOT, "src")
    writers = []
    for f in sorted(glob.glob(os.path.join(src_dir, "**", "*.py"), recursive=True)):
        tree = ast.parse(open(f).read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)):
                    node.body[0] = ast.Pass()
        code = ast.unparse(tree)
        if 'method="POST"' in code or "'POST'" in code or '"POST"' in code:
            if "moltbook.com" in code or "BASE" in code:
                writers.append(os.path.basename(f))
    assert writers == ["sender.py"], f"write paths found in: {writers}"


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    bad = 0
    for n, f in fns:
        try:
            f(); print(f"  pass  {n}")
        except AssertionError as e:
            bad += 1; print(f"  FAIL  {n}  {e}")
        except Exception as e:
            bad += 1; print(f"  ERROR {n}  {type(e).__name__}: {e}")
    print(f"\n{len(fns)-bad}/{len(fns)} passed")
    raise SystemExit(1 if bad else 0)
