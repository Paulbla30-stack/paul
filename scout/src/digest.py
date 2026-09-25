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


def build(records: list[dict], *, run_stats: dict, threshold: float,
          proposals: list | None = None, approve_url: str = "",
          mint=None) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body).

    `proposals` are drafts awaiting Paul's decision. Each gets a link to the
    accept page. The link authorises *deciding*, not approving: there is no
    URL in this email that posts anything. See src/approve_app.py.
    """
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

    proposals = proposals or []
    if proposals:
        lines += ["", "AWAITING YOUR DECISION", ""]
        for p in proposals:
            link = f"{approve_url}?t={mint(p.id)}" if (approve_url and mint) else "(no link)"
            lines.append(f"- {p.kind} on {p.network}, in reply to: {p.target_title}")
            lines.append(f"  draft: {p.draft[:160]}{'…' if len(p.draft) > 160 else ''}")
            lines.append(f"  decide: {link}")
            lines.append("")
        lines.append("Nothing posts unless you open one of those and approve it.")
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
    ] + ([
        "The scout finds and logs. It does not post anywhere, and it has not",
        "drafted anything. Every line above is a link for you to judge.",
    ] if not proposals else [
        f"The scout has drafted {len(proposals)} post"
        f"{'s' if len(proposals) != 1 else ''} and posted nothing. Nothing goes",
        "out unless you open it and approve it. If you do nothing, it lapses.",
    ])
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
    prop_html = ""
    if proposals:
        items = []
        for p in proposals:
            link = f"{approve_url}?t={mint(p.id)}" if (approve_url and mint) else ""
            snippet = html.escape(p.draft[:200]) + ("…" if len(p.draft) > 200 else "")
            btn = (f'<a href="{html.escape(link, quote=True)}" '
                   f'style="display:inline-block;margin-top:8px;padding:9px 16px;'
                   f'background:#0b3d91;color:#fff;border-radius:6px;'
                   f'text-decoration:none;font:600 13px system-ui">Review and decide</a>'
                   ) if link else ""
            items.append(
                f'<li style="margin:0 0 16px 0">'
                f'<span style="font:11px monospace;color:#666">{html.escape(p.kind)} '
                f'&middot; {html.escape(p.network)}</span><br>'
                f'<span style="font:13px system-ui;color:#333">in reply to '
                f'{html.escape(p.target_title)}</span>'
                f'<div style="white-space:pre-wrap;background:#fafaf8;'
                f'border-left:3px solid #0b3d91;padding:10px;margin-top:6px;'
                f'font:13px system-ui;color:#222">{snippet}</div>{btn}</li>')
        prop_html = (
            '<hr style="border:0;border-top:1px solid #ddd;margin:18px 0">'
            '<p style="font:600 14px system-ui;color:#111">Awaiting your decision</p>'
            f'<ul style="list-style:none;padding:0;margin:0">{"".join(items)}</ul>'
            '<p style="font:12px system-ui;color:#666">Nothing posts unless you '
            'open one of those and approve it.</p>')

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
        f'{prop_html}'
        '<hr style="border:0;border-top:1px solid #ddd;margin:18px 0">'
        f'<p style="font:12px/1.5 system-ui,sans-serif;color:#666">'
        f"Scanned {run_stats['fetched']} items from "
        f'{html.escape(", ".join(run_stats["sources_ok"]))}.{fail_html} '
        f"{run_stats['new']} new, {run_stats['above']} above threshold. "
        f"Chain entries: {run_stats.get('chain_entries','?')}.<br><br>"
        + ("The scout finds and logs. It does not post anywhere, and it has not "
           "drafted anything. Every line above is a link for you to judge."
           if not proposals else
           f"The scout has drafted {len(proposals)} post"
           f"{'s' if len(proposals) != 1 else ''} and posted nothing. Nothing goes "
           "out unless you open it and approve it. If you do nothing, it lapses.")
        + "</p></div>"
    )
    return subject, text, html_body
