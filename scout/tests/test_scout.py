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
