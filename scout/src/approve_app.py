"""The accept function.

A Lambda Function URL that shows Paul a draft and takes his decision.
Nothing Jarvis proposes can reach a network without passing through here.

Why it is shaped this way:

  * **GET shows, POST decides.** Mail clients and enterprise link scanners
    fetch URLs found in email to check them; Outlook and Gmail both do. If
    approving were a GET, the scanner would approve on Paul's behalf before
    he opened the message. GET here is strictly read-only and safe to
    prefetch: it renders the draft. The decision is a POST from that page.
  * **One link per proposal, not two.** The emailed token authorises
    *deciding*, not approving. There is no URL anywhere that means "yes".
  * **The token is not the decision.** The proposal's own status is checked
    by a conditional write, so a forwarded or replayed link cannot approve
    something twice, or approve something already rejected or lapsed.
  * **Every decision is hash-chained**, including rejections. What was
    proposed, what was decided, and when, is checkable afterwards by
    someone who does not trust the writer.

The page shows the draft verbatim. Paul is approving exact text, not a
summary of it.
"""

from __future__ import annotations

import html
import json
import logging
import os
import urllib.parse
from datetime import datetime, timezone

import approval
from chain import Chain, DynamoChainStore
from proposals import AlreadyDecided, ProposalStore, PENDING

log = logging.getLogger("jarvis.scout.approve")
logging.getLogger().setLevel(logging.INFO)

CSS = """
*{box-sizing:border-box}
body{margin:0;padding:24px 16px;background:#f6f6f4;color:#1a1a1a;
 font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif}
.w{max-width:620px;margin:0 auto}
.card{background:#fff;border:1px solid #e0e0dc;border-radius:10px;padding:20px;margin-bottom:16px}
h1{font-size:19px;margin:0 0 4px}
.sub{color:#666;font-size:13px;margin:0 0 20px}
.lbl{font:600 11px/1 system-ui;letter-spacing:.06em;text-transform:uppercase;color:#888;margin:0 0 6px}
.draft{white-space:pre-wrap;background:#fafaf8;border-left:3px solid #0b3d91;
 padding:14px;border-radius:0 6px 6px 0;font-size:14px}
.meta{color:#555;font-size:13px}
.meta a{color:#0b3d91}
ul{margin:6px 0 0;padding-left:20px;font-size:13px;color:#555}
.row{display:flex;gap:10px;margin-top:8px}
button{flex:1;padding:13px;border:0;border-radius:8px;font:600 15px system-ui;cursor:pointer}
.yes{background:#0b3d91;color:#fff}
.no{background:#fff;color:#a00;border:1px solid #d8c4c4}
.note{color:#777;font-size:12px;margin-top:14px;text-align:center}
.big{font-size:17px;font-weight:600;margin:0 0 6px}
@media(prefers-color-scheme:dark){
 body{background:#16161a;color:#e8e8e6}
 .card{background:#1e1e24;border-color:#33333c}
 .draft{background:#242430;border-left-color:#6f9bff}
 .meta,.sub,ul{color:#a8a8ad}.meta a{color:#8fb4ff}
 .no{background:#1e1e24;color:#ff9d9d;border-color:#4a3434}}
"""


def _page(title: str, body: str, status: int = 200) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            # The page posts only to itself and loads nothing external.
            "Content-Security-Policy":
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
        },
        "body": f"<!doctype html><html lang=en><head><meta charset=utf-8>"
                f"<meta name=viewport content='width=device-width,initial-scale=1'>"
                f"<meta name=robots content='noindex,nofollow'>"
                f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
                f"<body><div class=w>{body}</div></body></html>",
    }


def _message(head: str, detail: str, status: int = 200) -> dict:
    return _page(head, f"<div class=card><p class=big>{html.escape(head)}</p>"
                       f"<p class=meta>{html.escape(detail)}</p></div>", status)


def _confirm_page(p, token: str) -> dict:
    discl = "".join(f"<li>{html.escape(d)}</li>" for d in (p.discloses or []))
    return _page(
        "Approve this post?",
        f"""
<h1>Jarvis would like to post this</h1>
<p class=sub>Nothing has been posted. It goes out only if you approve it below.</p>

<div class=card>
  <p class=lbl>{html.escape(p.kind)} on {html.escape(p.network)}</p>
  <p class=meta>In reply to <a href="{html.escape(p.target_url, quote=True)}"
     rel="noopener noreferrer nofollow">{html.escape(p.target_title or p.target_url)}</a></p>
</div>

<div class=card>
  <p class=lbl>The exact text that would be posted</p>
  <div class=draft>{html.escape(p.draft)}</div>
</div>

<div class=card>
  <p class=lbl>Why Jarvis thinks it is worth posting</p>
  <p class=meta>{html.escape(p.rationale)}</p>
  {'<p class=lbl style="margin-top:14px">Disclosures in the draft</p><ul>' + discl + '</ul>' if discl else ''}
</div>

<div class=card>
  <form method=post>
    <input type=hidden name=t value="{html.escape(token, quote=True)}">
    <div class=row>
      <button class=yes name=action value=approve type=submit>Approve and post</button>
      <button class=no  name=action value=reject  type=submit>Reject</button>
    </div>
  </form>
  <p class=note>Expires {html.escape((p.expires_at or '')[:10])}. If you do nothing, it lapses unposted.</p>
</div>
""")


def _form(event) -> dict:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8", "replace")
    return {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}


def handler(event, context):
    table = os.environ["SCOUT_TABLE"]
    secret = approval.load_secret()
    store = ProposalStore(table)

    http = (event.get("requestContext") or {}).get("http") or {}
    method = (http.get("method") or "GET").upper()

    if method == "GET":
        token = (event.get("queryStringParameters") or {}).get("t", "")
    elif method == "POST":
        token = _form(event).get("t", "")
    else:
        return _message("Not allowed", "Use the link from your digest email.", 405)

    try:
        payload = approval.verify(secret, token)
    except approval.BadToken as e:
        log.warning("bad token: %s", e)
        reason = {"expired": "That link has expired. The proposal lapses unposted.",
                  "signature": "That link is not valid.",
                  "malformed": "That link is not valid."}.get(str(e), "That link is not valid.")
        return _message("Link not valid", reason, 400)

    p = store.get(payload["p"])
    if p is None:
        return _message("Not found", "No such proposal. It may have been cleared.", 404)

    if method == "GET":
        if p.status != PENDING:
            return _message(f"Already {p.status}",
                            f"Decided {(p.decided_at or '')[:16]}. Nothing further to do.")
        return _confirm_page(p, token)

    # POST — the decision.
    action = _form(event).get("action", "")
    if action not in ("approve", "reject"):
        return _message("Nothing done", "No decision was submitted.", 400)

    try:
        decided = store.decide(p.id, action, who="paul")
    except AlreadyDecided as e:
        existing = e.proposal
        return _message(f"Already {existing.status if existing else 'decided'}",
                        "This one was decided already. Nothing has changed.")

    # Record it where it cannot be quietly rewritten, rejections included.
    try:
        Chain(DynamoChainStore(table)).append({
            "event": f"proposal.{action}",
            "proposal_id": decided.id,
            "network": decided.network,
            "target_url": decided.target_url,
            "draft_sha256": __import__("hashlib").sha256(
                decided.draft.encode("utf-8")).hexdigest(),
            "decided_by": decided.decided_by,
            "decided_at": decided.decided_at,
        })
    except Exception as e:                                   # noqa: BLE001
        # The decision stands; the record of it failed. Say so rather than
        # pretending, and leave it in the log for the next run to notice.
        log.error("decision recorded in table but NOT chained: %s", e)

    if action == "approve":
        # Deliberately vague about timing. Moltbook returns a timed challenge
        # on creation (5 minutes, 30 seconds for submolts) which must be
        # solved before the content is visible, so the send cannot be
        # deferred to the next scheduled run. See README "Posting is a timed
        # two-step". Until Stage 2 exists, nothing sends at all.
        return _message(
            "Approved",
            "Recorded. Nothing has been posted yet: the sender is not built.")
    return _message("Rejected", "Nothing was posted, and nothing will be.")
