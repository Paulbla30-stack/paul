"""Tests for the scout.

    python3 -m pytest tests/ -q     (or) python3 tests/test_scout.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import tomllib                                                   # noqa: E402
from chain import Chain, LocalChainStore, entry_hash             # noqa: E402
from scoring import Scorer, proximity_match, _tokens             # noqa: E402
from sources import safe_url, sanitise, utc                      # noqa: E402
import digest                                                    # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = tomllib.load(open(os.path.join(ROOT, "config.toml"), "rb"))
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------- sanitising
def test_control_characters_are_stripped():
    assert "\x07" not in sanitise("bell\x07here")
    assert sanitise("a\x00b") == "a b"


def test_zero_width_and_bidi_are_removed():
    # These are how hidden text is smuggled into an innocuous-looking string.
    assert sanitise("safe​word") == "safeword"
    assert "‮" not in sanitise("safe‮txet")


def test_length_is_capped():
    assert len(sanitise("x" * 10_000, 100)) == 100


def test_non_http_urls_are_dropped():
    assert safe_url("javascript:alert(1)") == ""
    assert safe_url("data:text/html;base64,AAAA") == ""
    assert safe_url("https://example.com/ok") == "https://example.com/ok"


def test_timestamps_are_always_utc_aware():
    for v in (1758000000, "2026-09-21T10:00:00Z", "2026-09-21"):
        assert utc(v).tzinfo is not None


# ----------------------------------------------------------------- matching
def test_proximity_matches_natural_word_order():
    # The whole reason proximity matching exists.
    assert proximity_match(_tokens("the governance of AI in healthcare"),
                           ["ai", "governance", "healthcare"], 8)


def test_proximity_respects_the_window():
    far = "AI " + "filler " * 30 + "governance " + "filler " * 30 + "healthcare"
    assert not proximity_match(_tokens(far), ["ai", "governance", "healthcare"], 8)


def test_tier_a_stays_exact():
    s = Scorer(CFG)
    # tier_a is phrase-matched: a loose scatter of the words must not fire.
    hit = s.score(title="The Heartbeat Framework", body="",
                  source_weight=1.0, published=NOW, now=NOW)
    miss = s.score(title="a framework for heartbeat monitoring", body="",
                   source_weight=1.0, published=NOW, now=NOW)
    assert "Heartbeat Framework" in hit.matched_keywords
    assert "Heartbeat Framework" not in miss.matched_keywords


# ------------------------------------------------------------------ scoring
def test_title_beats_body():
    s = Scorer(CFG)
    t = s.score(title="automation bias", body="", source_weight=1.0,
                published=NOW, now=NOW)
    b = s.score(title="", body="automation bias", source_weight=1.0,
                published=NOW, now=NOW)
    assert t.total > b.total


def test_negative_keywords_sink_a_job_advert():
    s = Scorer(CFG)
    r = s.score(title="Hiring: clinical decision support engineer",
                body="Salary competitive, apply now.",
                source_weight=1.0, published=NOW, now=NOW)
    assert r.total == 0.0
    assert r.negatives


def test_a_named_term_still_outranks_a_penalty():
    # A tier_a match should survive one incidental negative word.
    s = Scorer(CFG)
    r = s.score(title="DCB0160 and the cultural safety case",
                body="Posted by a sponsored account.",
                source_weight=1.0, published=NOW, now=NOW)
    assert r.total >= CFG["run"]["digest_threshold"]


def test_diminishing_returns_stop_keyword_stuffing():
    s = Scorer(CFG)
    stuffed = " ".join(CFG["keywords"]["tier_c"])
    one_strong = s.score(title="DCB0129", body="", source_weight=1.0,
                         published=NOW, now=NOW)
    many_weak = s.score(title="", body=stuffed, source_weight=1.0,
                        published=NOW, now=NOW)
    assert one_strong.total > many_weak.total


def test_recency_decays_but_has_a_floor():
    s = Scorer(CFG)
    fresh = s.score(title="DCB0129", body="", source_weight=1.0,
                    published=NOW, now=NOW)
    old = s.score(title="DCB0129", body="", source_weight=1.0,
                  published=NOW - timedelta(days=400), now=NOW)
    assert old.total < fresh.total
    assert old.total >= fresh.total * CFG["scoring"]["decay_floor"] * 0.99


def test_no_match_scores_nothing():
    s = Scorer(CFG)
    assert s.score(title="Tomatoes", body="Gardening.", source_weight=1.0,
                   published=NOW, now=NOW).total == 0.0


def test_why_is_readable_and_names_the_keywords():
    s = Scorer(CFG)
    r = s.score(title="The cultural safety case", body="",
                source_weight=1.0, published=NOW, now=NOW)
    assert "cultural safety case" in r.why()
    assert "title" in r.why()


# -------------------------------------------------------------------- chain
def _chain():
    p = tempfile.mktemp(suffix=".jsonl")
    return Chain(LocalChainStore(p)), p


def test_chain_verifies_when_untouched():
    c, p = _chain()
    for i in range(6):
        c.append({"url": f"https://example.com/{i}", "score": i})
    r = c.verify()
    os.unlink(p)
    assert r["ok"] and r["entries"] == 6


def test_first_entry_links_to_genesis():
    c, p = _chain()
    e = c.append({"url": "https://example.com/1"})
    os.unlink(p)
    assert e["prev_hash"] == "0" * 64 and e["seq"] == 1


def test_tampering_is_detected_at_the_right_entry():
    c, p = _chain()
    for i in range(5):
        c.append({"url": f"https://example.com/{i}", "score": i})
    lines = open(p).read().splitlines()
    lines[2] = lines[2].replace('"score":2', '"score":999')
    open(p, "w").write("\n".join(lines) + "\n")
    r = Chain(LocalChainStore(p)).verify()
    os.unlink(p)
    assert not r["ok"]
    assert r["first_bad_seq"] == 3
    assert r["entries_verified_before_failure"] == 2


def test_removing_an_entry_is_detected():
    c, p = _chain()
    for i in range(5):
        c.append({"url": f"https://example.com/{i}"})
    lines = open(p).read().splitlines()
    del lines[2]
    open(p, "w").write("\n".join(lines) + "\n")
    r = Chain(LocalChainStore(p)).verify()
    os.unlink(p)
    assert not r["ok"] and r["kind"] == "sequence gap"


def test_entry_hash_is_stable_for_the_same_input():
    a = entry_hash(1, "0" * 64, "2026-09-21T00:00:00+00:00", {"b": 1, "a": 2})
    b = entry_hash(1, "0" * 64, "2026-09-21T00:00:00+00:00", {"a": 2, "b": 1})
    assert a == b          # key order must not change the hash


# ------------------------------------------------------------------- digest
def _rec(**kw):
    d = {"source": "hackernews", "title": "T", "url": "https://example.com",
         "author": "a", "published": "2026-09-20T00:00:00+00:00",
         "score": 12.0, "why": "matched x (title)"}
    d.update(kw)
    return d


def test_digest_escapes_untrusted_title():
    stats = {"fetched": 1, "new": 1, "above": 1, "sources_ok": ["hackernews"],
             "sources_failed": [], "chain_entries": 1}
    _, text, html_body = digest.build(
        [_rec(title='<script>alert(1)</script>')], run_stats=stats, threshold=8.0)
    assert "<script>" not in html_body
    assert "&lt;script&gt;" in html_body
    assert "<script>" in text        # plain text is not markup; that is fine


def test_digest_reports_failed_sources():
    stats = {"fetched": 1, "new": 1, "above": 1, "sources_ok": ["medrxiv"],
             "sources_failed": ["arxiv"], "chain_entries": 1}
    _, text, html_body = digest.build([_rec()], run_stats=stats, threshold=8.0)
    assert "arxiv" in text and "arxiv" in html_body


def test_digest_says_it_has_not_drafted_anything():
    stats = {"fetched": 1, "new": 1, "above": 1, "sources_ok": ["medrxiv"],
             "sources_failed": [], "chain_entries": 1}
    _, text, _ = digest.build([_rec()], run_stats=stats, threshold=8.0)
    assert "does not post" in text and "drafted" in text


# ------------------------------------------------------------------- config
def test_reddit_is_disabled():
    # Approval is required, so the brief says skip it.
    assert CFG["sources"]["reddit"]["enabled"] is False


def test_no_linkedin_anywhere():
    for root, _, files in os.walk(os.path.join(ROOT, "src")):
        for f in files:
            if f.endswith(".py"):
                assert "linkedin" not in open(os.path.join(root, f)).read().lower()


def test_hn_uses_the_relevance_endpoint():
    # /search_by_date discards relevance and returns the firehose.
    assert CFG["sources"]["hackernews"]["endpoint"].endswith("/search")


def test_arxiv_endpoint_is_https():
    # http:// returns 200 with an empty feed, which looks like "no papers".
    assert CFG["sources"]["arxiv"]["endpoint"].startswith("https://")


def test_arxiv_prefers_rss_over_the_flaky_api():
    # RSS is what arXiv publishes for "new submissions" and it is reliable.
    # The Atom API 406s under load and is fallback only, for wide lookbacks.
    src = open(os.path.join(ROOT, "src", "sources", "arxiv.py")).read()
    assert "rss.arxiv.org" in src
    assert src.index("_from_rss(cat") < src.index("_from_api(cfg")


def test_arxiv_drops_revisions_but_keeps_cross_lists():
    from sources.arxiv import KEEP_ANNOUNCE
    assert "new" in KEEP_ANNOUNCE and "cross" in KEEP_ANNOUNCE
    assert "replace" not in KEEP_ANNOUNCE


def test_arxiv_abstract_is_extracted_from_the_rss_preamble():
    from sources.arxiv import _abstract
    d = "arXiv:2609.21194v1 Announce Type: new  Abstract: The real text here."
    assert _abstract(d).strip() == "The real text here."
    assert _abstract("no marker present") == "no marker present"


# ----------------------------------------------------------------- moltbook
def _code_without_docstrings(path: str) -> str:
    """Source with all docstrings removed.

    The docstrings deliberately discuss the write endpoints in order to
    explain why they are not used, so a naive text search over the whole
    file finds its own documentation and fails.
    """
    import ast
    tree = ast.parse(open(path).read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body.pop(0)
    return ast.unparse(tree)


def test_moltbook_is_enabled_and_read_only():
    assert CFG["sources"]["moltbook"]["enabled"] is True
    code = _code_without_docstrings(
        os.path.join(ROOT, "src", "sources", "moltbook.py"))
    # No write path, no credentials. This is the whole safety property.
    for forbidden in ("api_key", "Authorization", "Bearer", "data=",
                      "create_post", "/feed", "/vote", "/comment"):
        assert forbidden not in code, \
            f"moltbook.py must stay read-only: found {forbidden!r} in code"


def test_no_source_module_can_write_anywhere():
    # Stage 1 never posts. If this fails, something has gained a write path.
    import glob
    for f in glob.glob(os.path.join(ROOT, "src", "sources", "*.py")):
        src = open(f).read()
        assert "urlopen" not in src or "sources/__init__" in f or "http_" in src, f
    base = open(os.path.join(ROOT, "src", "sources", "__init__.py")).read()
    # http_get is the only egress, and only lesswrong passes data= (GraphQL read).
    assert base.count("urlopen") == 1


def test_moltbook_highlight_markers_are_stripped():
    from sources.moltbook import _clean
    assert _clean("⟦HL⟧Healthcare⟦/HL⟧ AI") == "Healthcare AI"


def test_moltbook_items_are_marked_as_machine_written():
    # The digest must not present agent output as human conversation.
    src = open(os.path.join(ROOT, "src", "sources", "moltbook.py")).read()
    assert '"written_by": "agent"' in src



# ------------------------------------------------------------ the first run
def test_first_run_sweeps_wider_than_a_normal_run():
    # With nothing stored there is no backlog to de-duplicate against, so a
    # narrow window would hand over a near-empty digest and quietly lose
    # everything published before today.
    assert (CFG["run"]["first_run_lookback_days"]
            > CFG["run"]["lookback_days"])


def test_first_run_is_detected_from_an_empty_chain():
    import tempfile
    from chain import Chain, LocalChainStore
    path = tempfile.mktemp(suffix=".jsonl")
    store = LocalChainStore(path)
    assert store.head()[0] == 0                 # first run
    Chain(store).append({"x": 1})
    assert store.head()[0] != 0                 # and never again
    os.remove(path)


def test_threshold_is_reachable_by_one_on_topic_title():
    """The original threshold of 8 was unreachable and would have sent nothing.

    One broad term in a title must clear the bar; one body mention must not.
    """
    from datetime import datetime, timezone
    s = Scorer(CFG)
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    thr = CFG["run"]["digest_threshold"]
    in_title = s.score(title="A sociotechnical review of healthcare AI",
                       body="", source_weight=1.2, published=now, now=now)
    in_body = s.score(title="An unrelated title", body="mentions healthcare AI once",
                      source_weight=1.2, published=now, now=now)
    assert in_title.total >= thr, f"{in_title.total} < {thr}: nothing would send"
    assert in_body.total < thr, f"{in_body.total} >= {thr}: too noisy"


def test_a_week_old_paper_keeps_most_of_its_score():
    # 8%/day halved a week-old preprint, which is wrong for this material.
    from datetime import datetime, timedelta, timezone
    s = Scorer(CFG)
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    fresh = s.score(title="healthcare AI", body="", source_weight=1.0,
                    published=now, now=now)
    week = s.score(title="healthcare AI", body="", source_weight=1.0,
                   published=now - timedelta(days=7), now=now)
    assert week.total / fresh.total > 0.75


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    bad = 0
    for n, f in fns:
        try:
            f()
            print(f"  pass  {n}")
        except AssertionError as e:
            bad += 1
            print(f"  FAIL  {n}  {e}")
        except Exception as e:
            bad += 1
            print(f"  ERROR {n}  {type(e).__name__}: {e}")
    print(f"\n{len(fns)-bad}/{len(fns)} passed")
    raise SystemExit(1 if bad else 0)