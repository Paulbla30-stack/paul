"""The daily digest.

One line per item: what it is, where, and why it matched. The scout never
posts anywhere and never drafts anything in Stage 1 — this email is the
entire output, and Paul decides what to do with each line.

Plain text and HTML are both built. The HTML escapes every field, because
titles and author names are untrusted text from the open internet and this
email is rendered in a mail client.
"""

from __future__ import annotations

import html
from datetime import datetime


def _fmt_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d %b")
    except Exception:
        return "?"


SOURCE_LABEL = {
    "hackernews": "HN",
    "lesswrong": "LW",
    "arxiv": "arXiv",
    "medrxiv": "medRxiv",
    "reddit": "Reddit",
}


def build(records: list[dict], *, run_stats: dict, threshold: float) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body)."""
    n = len(records)
    today = datetime.now().strftime("%d %B %Y")
    subject = f"{n} item{'s' if n != 1 else ''} — {today}"

    lines = [
        f"{n} item{'s' if n != 1 else ''} above the threshold of {threshold:g}.",
        "",
    ]
    for i, r in enumerate(records, 1):
        src = SOURCE_LABEL.get(r["source"], r["source"])
        lines.append(f"{i}. [{src}] {r['title']}")
        lines.append(f"   {r['url']}")
        by = f" · {r['author']}" if r.get("author") else ""
        lines.append(f"   score {r['score']:g} · {_fmt_date(r['published'])}{by}")
        lines.append(f"   {r['why']}")
        lines.append("")

    lines += [
        "—",
        f"Scanned {run_stats['fetched']} items from "
        f"{', '.join(run_stats['sources_ok'])}"
        + (f" (failed: {', '.join(run_stats['sources_failed'])})"
           if run_stats.get("sources_failed") else "")
        + ".",
        f"{run_stats['new']} new, {run_stats['above']} above threshold.",
        f"Chain entries: {run_stats.get('chain_entries', '?')}.",
        "",
        "The scout finds and logs. It does not post anywhere, and it has not",
        "drafted anything. Every line above is a link for you to judge.",
    ]
    text = "\n".join(lines)

    rows = []
    for i, r in enumerate(records, 1):
        src = html.escape(SOURCE_LABEL.get(r["source"], r["source"]))
        title = html.escape(r["title"])
        url = html.escape(r["url"], quote=True)
        why = html.escape(r["why"])
        author = html.escape(r.get("author") or "")
        meta = f"score {r['score']:g} &middot; {html.escape(_fmt_date(r['published']))}"
        if author:
            meta += f" &middot; {author}"
        rows.append(
            f'<li style="margin:0 0 14px 0">'
            f'<span style="font:11px/1.4 monospace;color:#666">{src}</span><br>'
            f'<a href="{url}" style="font:600 15px/1.35 system-ui,sans-serif;'
            f'color:#0b3d91;text-decoration:none">{title}</a><br>'
            f'<span style="font:12px/1.5 system-ui,sans-serif;color:#555">{meta}</span><br>'
            f'<span style="font:12px/1.5 system-ui,sans-serif;color:#333">{why}</span>'
            f"</li>"
        )
    failed = run_stats.get("sources_failed") or []
    fail_html = (
        f' <span style="color:#a00">Failed: {html.escape(", ".join(failed))}.</span>'
        if failed else ""
    )
    html_body = (
        '<div style="max-width:640px;margin:0 auto;padding:16px">'
        f'<p style="font:600 14px/1.4 system-ui,sans-serif;color:#111">'
        f"{n} item{'s' if n != 1 else ''} above the threshold of {threshold:g}.</p>"
        f'<ol style="padding-left:18px;margin:0">{"".join(rows)}</ol>'
        '<hr style="border:0;border-top:1px solid #ddd;margin:18px 0">'
        f'<p style="font:12px/1.5 system-ui,sans-serif;color:#666">'
        f"Scanned {run_stats['fetched']} items from "
        f'{html.escape(", ".join(run_stats["sources_ok"]))}.{fail_html} '
        f"{run_stats['new']} new, {run_stats['above']} above threshold. "
        f"Chain entries: {run_stats.get('chain_entries','?')}.<br><br>"
        "The scout finds and logs. It does not post anywhere, and it has not "
        "drafted anything. Every line above is a link for you to judge.</p></div>"
    )
    return subject, text, html_body
