# Jarvis connections: final plan

This is design and research only. I read the repo at `7d93fb1`, two security documents and a few public pages. I did not edit the repo, did not touch the box or AWS, and did not run `jarvis_chat.py` or `ssm_run.py`. **Nothing in this plan is on Jarvis's chain yet, and Jarvis has not been consulted.**

## Summary

- **What Paul asked for.** Paul asked what is missing, for a list of connections he can switch on, and for a way to add his own. Today Jarvis has none of these. Every outside link is hard-coded. There is no plugin, OAuth, MCP or webhook code. It cannot see his calendar, inbox, contacts or any structured feed.
- **The shape.** Connections run in small AWS Lambda "brokers" off the box. Paul's keys and sign-ins never reach the box, which runs Jarvis as root. Keys are entered only in the AWS console. Microsoft sign-in uses PKCE, with the secret parts held in a Lambda. Anything outward needs Paul's passkey, which the broker checks itself.
- **The first releases are feeds, not verbs.** Weather, bank holidays and his Zenodo records come first. Then his calendar through an ICS link in **busy-only** form. Then Telegram messages to Paul. Reading his mail headers and contacts comes later. Any action taken as Paul comes last, one at a time.
- **Custom connections.** One form covers all of them. Custom entries are read-only GET requests to a single host, with only the fields Paul lists kept. The validator rejects anything aimed at private networks, the metadata service or Arkin. A custom remote MCP server comes last.
- **Untrusted text.** The two critiques showed that the earlier plan let calendar titles and mail-derived answers reach the planner, memory and the ledger. This version closes those routes:
  - the planner sees busy times only;
  - text from services is read only by a call that has no private context and no tools;
  - turns that used a connection are flagged, and are stored and ledgered as hashes.
- **Effort and cost.** About 44–52 coding-agent days across nine stages. Expected running cost is under about $5 a month. Nothing is billed by the hour. DNS Firewall is billed per query, is Paul's call, and is required before mail reading.

## What I checked this pass (raw)

```
$ sed -n 73p;140-142p jarvis/agent/consolidate.py
NOT_EVIDENCE = frozenset({"exchange", "told"})
    return (row.get("kind") or "note") not in NOT_EVIDENCE        # keyed on kind, not source
$ sed -n 1470-1476p jarvis/agent/core.py   -> "question": last_user[:500], "answer_head": (answer or "")[:300]
$ sed -n 1572p jarvis/agent/core.py        -> self.remember(text, kind="exchange", source="operator")
$ sed -n 738-741p jarvis/brain/llm.py      -> item["error"] = str(result["error"])[...]; item["output"] = _truncate(...)
$ schedule.py _persist                     -> store.remember(..., source="operator", pinned=True, ...)
$ index.html:88                            -> nav { overflow-x:auto; ... }
$ main.tf:150-166  sns_sms: sns:Publish Resource "*"; sns_topic: Resource "arn:aws:sns:<region>:<acct>:*";
                   ses_email: ses:SendEmail Resource "*"
$ authority.py:82                          -> READ_ONLY_COMMANDS includes "getent"
$ executor.py:129  python -c blocked only if it mentions urllib|requests|httpx|socket|http.client  (not boto3)
$ notify.py:445    boto3.client("ses").send_email(       # SES v1 API
$ headless.py:1734 with self._lock: ...   headless.py:278 max_idle_wait default 300.0
$ curl login.microsoftonline.com/consumers/v2.0/.well-known/openid-configuration
  issuer .../9188040d-6c67-4c5b-b112-36a304b66dad/v2.0; authorization_response_iss_parameter_supported None;
  code_challenge_methods_supported None; revocation_endpoint None
$ SES developer guide (control-user-access): "ses:Recipients  Restricts the recipient addresses, which include
  the To:, "CC", and "BCC" addresses.  SendEmail, SendRawEmail"; "ses:FromAddress ... SendEmail, SendRawEmail, SendBounce"
$ Telegram Bot API: forward_origin, reply_to_message, quote, via_bot, caption present on Message;
  MessageEntity "text_link" (for clickable text URLs); Chat.type "private", "group", "supergroup" or "channel";
  also guest_bot_caller_user (not in either critique)
security_controls.md:149  A5(inj): add `note` to NOT_EVIDENCE, or require corroboration. Consult Jarvis.
```

This pass confirmed every repo claim that the two critiques rely on. It also found that the `sns_topic` branch is open to any topic in the account, not just one fixed topic. Evidence carried over from the earlier pass:

- Graph `Mail.ReadWrite` "does not include permission to send mail" but can delete.
- SES receiving is available in us-west-2.
- `GetSecretValue` accepts the `VersionStage` and tag conditions.

The feasibility reviewer fetched the following. I did not re-fetch them:

- `https://www.gov.uk/bank-holidays.json` returns 200 JSON;
- `lambda-url.us-west-2.on.aws` is on the Public Suffix List;
- AWS's `PutSecretValue` guidance of no more than once every 10 minutes, and 100 versions per secret.

---

## 1. What is missing today

| Area | Jarvis today (from code) | Missing | First route |
|---|---|---|---|
| Connection list, custom connections | None; `GRANTABLE={"browse_act"}` | Catalogue, custom form, instance store | This plan |
| Calendar | Internal diary and schedule; `notify.busy_check` waits for a calendar | Any outside calendar | ICS link, busy-only (S2); Graph `Calendars.ReadBasic` (S4) |
| Email | Outbound to Paul only (SES) | Inbox reading; mail-in | SES inbound on a subdomain (S4); Graph `Mail.ReadBasic` (S4) |
| Contacts | None | Everything | Graph `Contacts.Read` (S4) |
| Messaging | UI; SES/SNS to Paul | Phone chat both ways | Telegram out (S3), in by polling (S6) |
| Files | UI uploads | Cloud drive | OneDrive `Files.Read`, later [personal-account consent: check] |
| Tasks | Internal diary | To Do, Todoist | Graph `Tasks.Read`, later [check] |
| Weather, holidays | Browser only | Structured feeds | Open-Meteo; `gov.uk/bank-holidays.json` (URL verified by the feasibility reviewer) (S1) |
| Travel, places, vehicle | None | TfL, Darwin, postcodes.io, DVLA/MOT | S5 |
| Home, energy | None | Octopus; Home Assistant | Octopus S5; Home Assistant deferred (no read-only mode) |
| Money | Bill uploads only | Open Banking account information | Deferred; personal registration only, **never Arkin Engine Ltd** |
| Health | None | Wearables | Deferred; special-category data |
| His published estate | Operator scripts | Zenodo read | S1, limited to his 20 DOIs |
| MCP client | None | Custom remote MCP | S8, in the custom broker only |
| Untrusted-text marking | Browser pages only | General envelope for connections | S0 (Textract, upload and estate text: see Q6) |
| Ledger hygiene | `head` 200 characters, raw `error` 500 characters, `answer_head` 300, action `description` 300 / `goal` 1000 | Connection turns hashed | S0 |
| Open exits | SES to any recipient, SNS to any number or topic, `getent`, `python -c` with boto3 | Closed before any personal data arrives | S0 |

**Must stay absent:**

- anything Arkin: hard-denied in the validator, and the Microsoft personal-account tenant is required;
- password managers;
- WhatsApp and Signal;
- iCloud app-specific passwords, and CalDAV or IMAP passwords generally;
- stdio MCP anywhere;
- Zapier (a confused deputy);
- Google Maps Grounding (its terms forbid storing results).

## 2. The catalogue

Terms used in the table:

- **Sync:** code fetches on its own clock, and no model chooses the fetch.
- **R:** `connection_read`, chat turns only.
- **A:** `connection_act`, which needs a passkey approval every time and is never in `GRANTABLE`.
- **Planner sees:** what may reach any model call that holds tools.

| Stage | Connection | Auth | Read | Planner sees | Write | Use |
|---|---|---|---|---|---|---|
| S1 | Open-Meteo | none | forecast for saved places (2 d.p.) | numbers, and WMO codes mapped to fixed phrases | – | sync → `outside` block |
| S1 | GOV.UK bank holidays | none | dates and titles for his division | dates, fixed titles | – | sync |
| S1 | Zenodo (his 20 DOIs) | none | record metadata | – (UI and reading call only) | – | R from S3 |
| S2 | Calendar via ICS link (Outlook.com, Google, iCloud) | the URL is the secret | parsed off the box with `icalendar` and `recurring-ical-events` | **busy intervals only**: start, end, "busy" | – | sync → `outside_calendar`, `busy_now` |
| S3b | ICS "titles" mode (optional) | same | titles ≤80 characters to the UI; ≤40 to the reading call | still busy only | – | UI, reading call |
| S2 | RSS/Atom | none | title, date, link (≤20) | – | – | UI Reading panel only |
| S3 | Telegram out | bot token and chat id in one secret | – | – | `send_to_paul` only, fixed chat id | second fixed channel |
| S4 | Outlook.com via Graph `/consumers` | auth code + PKCE (S256 hard-coded) | `offline_access`, `Calendars.ReadBasic`, `Mail.ReadBasic` (no body or preview), `Contacts.Read` | busy intervals only | – | sync (calendar); R (headers, contacts) |
| S4 | Mail-in, SES inbound on a dedicated subdomain [which domain: Paul] | none of Paul's | text parts of messages he forwards; DKIM pass aligned to From, and DMARC PASS | – | – | R |
| S5 | Met Office DataHub, DVLA VES, DVSA MOT, Octopus, TfL, Darwin, postcodes.io | API keys, held in the broker | typed fields; registrations, stations and postcodes from Paul's lists | numbers and enums only | – | R |
| S6 | Telegram in | same bot; `getUpdates` polled by the broker | strict filter (§6) | a reduced-context chat turn | – | chat, with no approval power |
| S7 | Graph `create_event` | step-up consent to `Calendars.ReadWrite` | – | – | subject, start, end, location, reminder only; **no attendees** | A |
| Later | `Mail.ReadWrite` drafts (**also grants delete**), `Mail.Send` | step-up | – | – | Paul decides each separately | A |
| Later, one at a time | Google (a production personal-use app, not Testing), Home Assistant, Open Banking account information (payment initiation refused), health | per service | per service | – | – | Paul's decision |

## 3. Custom connections

**One manifest format.** A catalogue entry is a pre-filled manifest reviewed in a commit. A custom entry is the same form, filled in by Paul, with stricter defaults.

```
id: ^[a-z][a-z0-9_]{2,31}$          (exact match everywhere)
kind: ics | feed | rest_get | mcp_remote (S8)
label ≤60, note ≤120                (Paul's text; textContent only; never sent to a model as instructions)
host: one FQDN, https, port 443
auth: none | api_key_header | api_key_query | bearer
                                    (secret name is derived as jarvis/connections/custom/<id>; not a field)
data_class: public | personal       ("special" refused)
limits: calls_per_hour ≤12, max_bytes ≤256 KB fetched (ICS ≤2 MB fetched), ≤64 KB returned to the box, timeout_s ≤15
operations.<op>: effect READ|CHANGE (missing → CHANGE); method GET; path "/v1/x/{p}";
  params: {p: enum | int(min,max) | date(code-built: today|+Nd, N≤31) | string(≤200, regex, from_paul_only)}
  keep: ≤12 JSON pointers typed number|integer|bool|date|enum|short_text
sinks: ui_only (default) | outside_block (numbers, integers, bools, dates and enums mapped to fixed phrases;
  short_text refused)
```

**Rules:**

- **Kinds.** Custom kinds are GET-only and read-only. There is no custom POST or PUT, no IMAP, SMTP or CalDAV, no stdio MCP, no webhooks and no cookie scraping.
- **Default state.** A new connection starts off. Each operation is CHANGE until Paul marks it READ. A service's own claims, such as `readOnlyHint`, are ignored.
- **The secret is bound to the host.** Paul tags the secret `jarvis:host=<fqdn>` in the console. The broker refuses to fetch the secret unless that tag equals the manifest host.
- **The validator** is one source file under `jarvis/connections/`. `sam build` copies it into each Lambda, and a dev test, not the boot suite, checks that the copies are identical. It must stay valid on Python 3.11 (box) and 3.12 (Lambda). It enforces:
  - https only; no userinfo;
  - no IP literals, including v4-mapped, decimal and octal forms;
  - no `localhost`, `.internal` or `.local`, no metadata names, and not the tunnel host;
  - IDNA normalisation, with the punycode shown to Paul;
  - a hard deny on `thearkinsystem.co.uk`, its subdomains, and any account name ending in it.

  After DNS it checks `_is_public` on every address, then connects to the pinned IP with SNI and Host set. It allows no cross-host redirects, and at most two same-host redirects, each re-checked. The response is streamed and cut at the cap, with a gzip-bomb cap. Refusals are recorded as host, HMAC and length, before any DNS lookup.
- **Parameters.** Integers and dates come from Paul's message or from values built by code, within the manifest bounds. `from_paul_only` strings must be a verbatim substring of Paul's latest raw UI turn. They are refused in background cycles and in Telegram turns.
- **What survives.** Only the fields Paul maps survive, and they are dropped inside the broker otherwise.
- **Custom remote MCP (S8, in `connect-custom` only):**
  - Transport: a minimal stdlib JSON-RPC client that handles JSON and SSE replies (about 150 lines). Streamable HTTP, spec 2026-07-28, with `initialize` plus `Mcp-Session-Id` for older servers.
  - Auth: none, bearer, or OAuth with a pre-registered client. **The authorisation-server issuer and token endpoint are pinned in the manifest and never discovered** (RFC 9728 discovery is refused). No DCR or CIMD.
  - Tools default to CHANGE, and their string arguments are `from_paul_only`. The full tool list is shown to Paul untruncated, and he switches tools on one by one.
  - The enabled tool definitions are pinned by sha256. A change moves the connection to "Changed – review" and pauses it.
  - Only text content is kept from results. `resource_link`, embedded resources, sampling, roots, form elicitation, Tasks and MCP Apps are refused. URL-mode elicitation appears only as a link in the tab.
  - The planner sees `custom.<id>.<tool>` plus Paul's note, never the server's description.
  - **Residual, stated plainly:** the server's operator sees every argument and can change behaviour without changing definitions (the postmark-mcp pattern).

## 4. Architecture and data model

```
Box (jarvis.service, root)                     Off box: SAM app connect/ (Lambda, python3.12, outside the VPC)
 ├ jarvis/connections/{manifest,validate,       ├ connect-broker   catalogue ops; request_approval; disable
 │   client,sync,store}.py                      ├ connect-custom   custom kinds and MCP; add_draft; custom/* secrets only
 ├ jarvis/agent/untrusted.py                    ├ connect-approve  Function URL only; passkey enrol and assert; enabling
 ├ headless: _connections_tick,                 ├ connect-oauth    begin/finish; PKCE verifier; tid and /me checks
 │   _telegram_tick (own clocks), /connections  ├ DynamoDB jarvis-connections (PITR), jarvis-connection-approvals (TTL, PITR)
 └ cache /var/lib/jarvis/connections/<id>.json  └ Secrets Manager jarvis/connections/{cat,custom}/<id>, .../approve-enrol
   0600, typed records, ≤64 KB, overwritten
```

**The box's permissions:**

- `lambda:InvokeFunction` on `connect-broker`, `connect-custom` and `connect-oauth` only. **There is no invoke on `connect-approve`.**
- `GetItem` and `Query` on the connections table. No DynamoDB write, and no access to the approvals table.
- An explicit Deny on `secretsmanager:*` for `jarvis/connections/*`. This does not affect the three boot secrets.
- No outbound HTTP for connections.
- It can disable a connection through `broker.disable`. It **can enable keyless catalogue entries** with cookie auth and a same-origin POST. I say this plainly: those entries are reviewed in a commit, hold no credential, and send only Paul's saved places. Keyed and custom entries need the passkey.
- It creates drafts through `broker.add_draft` / `custom.add_draft`, and approval requests through `broker.request_approval`. Both validate against the manifest and write the tables themselves.

**The brokers enforce their own rules and do not trust the box:**

- They check `manifest_sha256` before touching a secret. Every argument is checked against the frozen manifest (enum membership, integer and date bounds, regex, length), and unknown keys are refused.
- The hourly caps are counted in DynamoDB on the broker side.
- **Effects.** Any effect other than READ is refused unless there is an approval item whose stored WebAuthn assertion **the broker verifies itself** against the enrolled public key and the challenge `H(approval_id‖payload_sha256‖nonce)`. It then consumes the item with a conditional update: `approved → used`, matching `payload_sha256`, not expired. Even a role that can write to the table cannot forge an approval. The only CHANGE the box may invoke without this is `send_to_paul`.
- **Credential handling.** Each call uses `GetSecretValue(..., VersionStage="AWSCURRENT")` explicitly. Secret values are scrubbed from responses in plain, URL-encoded and base64 forms.
- **Logging.** Handlers are wrapped so that exception text never reaches CloudWatch, because urllib errors carry URLs, and the TfL key and Telegram token are in URLs. Logs hold only the operation, the HMAC, bytes and status. Every log group has a set retention [period: Paul].
- **Isolation.** The two brokers have separate roles, so a custom manifest cannot reach the Graph token.
- **Box client settings:** `Config(connect_timeout=3, read_timeout=min(15, budget left), retries={"max_attempts":1})`. Lambda `Timeout` is 15 seconds or less.

**The sync path:**

1. `_connections_tick` runs on its own timestamp clock, like `_consolidation_tick`: calendar every 15–30 minutes, weather every few hours, one connection per tick.
2. With `runner._lock` held, it records the `action` for the non-plannable READ task `connection_sync`, then releases the lock.
3. It calls the broker without the lock.
4. It takes the lock again, records the `outcome` and writes the 0600 cache.
5. It returns counts only, so there is no output for history.
6. Readers:
   - `outside_calendar`, a separate context block, **not** `schedule.context()` and not `his_week`, with busy intervals only;
   - `core.busy_now()`, which drops data more than 2 hours stale;
   - the `outside` block, with numbers and fixed phrases;
   - the UI.
7. Sync makes no model call.

`_telegram_tick` runs every 30–60 seconds, even while the main loop is idle. `_wake` shortens the wait.

**Instances** are stored with these fields: `id`, `status` (draft | awaiting_approval | enabled | paused | needs_signin | changed | error | removed), the frozen `manifest`, `manifest_sha256`, `account_hint` (as verified by the Lambda), `scopes_granted`, `tooldefs_sha256`, `read_allowed`, `approval_id`, `last_ok`, `fail_count`.

**Config.** `cloud.connections` goes under `cloud:`. Add `("connections", ("cloud","connections"))` to the copy list in `main.py`, and pass it to `HeadlessRunner`. Its `enabled` flag can only tighten. The kill switch is checked by whichever process makes the call, box or broker.

**Memory on the t3.small.** There is no new resident process. `reflect()` and `task_history` keep a `cite()` stub for connection task types, never the output.

## 5. Secrets and OAuth

**Keys (API keys and ICS URLs):**

1. The card shows the secret name, the required tags (`jarvis:connection`, and `jarvis:host` for custom entries) and a console link. It says which console screen to use, because a mobile deep link is unverified.
2. Paul enters the value in the console only. **No page has an input field for a secret value**, and a boot-time refusal test enforces this against `index.html`.
3. Paul enables the connection on the passkey page, which checks that the secret exists and carries its tags.
4. **Chat guard.** Before anything is written to the ledger or memory, chat input is scanned for secret shapes: ICS publish URLs, the Telegram token pattern `\d{6,}:[A-Za-z0-9_-]{35}`, and long high-entropy strings. A match refuses the message, points Paul to the console, and ledgers only the question's HMAC.

**IAM:**

- **Broker roles:** `GetSecretValue` on their own prefix, with the condition `StringEquals secretsmanager:VersionStage = AWSCURRENT` (not `IfExists`) plus the tag condition.
- **`PutSecretValue`:** only for the oauth and broker roles, on OAuth secrets only.
- **Explicit Deny for every Jarvis role on:** Create, Delete, Update, **UpdateSecretVersionStage**, Restore, Rotate, Tag/Untag and resource-policy actions under the prefix.
- **Nobody holds `DeleteSecret`.** Paul deletes secrets in the console.

**OAuth (Graph first):**

- **Setup.** Paul registers an Entra app for "personal Microsoft accounts only". He creates the placeholder secret (`{}` plus tags) in the console **before** signing in, because no role holds `CreateSecret`. The redirect is exactly `https://<tunnel host>/connections/oauth/callback/microsoft`. [Client-secret expiry: check; add a diary reminder.]
- **Start.** `POST /connections/<id>/oauth/start` (auth plus same-origin):
  - sets a per-flow cookie `__Host-jarvis_oauth` (random, HttpOnly, Secure, SameSite=Lax, Path=/connections/oauth);
  - invokes `begin(id, provider, H(cookie))`, which creates a single-use 10-minute `state` bound to the provider and a PKCE S256 verifier, and returns only the authorise URL.
- **Callback.** It stays behind Cloudflare Access, but it is served **before Jarvis's own auth check**, the way `/ui` is. It is the same nonce'd, inert page. It strips the query with `replaceState` and never logs or ledgers `code` or `state`; `Referrer-Policy: no-referrer` is already set at `headless.py:600`. Once auth is back, the page POSTs `finish` same-origin.
- **Finish.** The Lambda checks:
  - `state`, the cookie hash and the provider;
  - `iss`, **only where the provider advertises it**. Microsoft consumers does not (verified above), so there `state` is bound to the provider-specific path instead;
  - it redeems the code with the verifier and client secret;
  - the ID token's `tid` equals `9188040d-6c67-4c5b-b112-36a304b66dad` (verified above as the consumers issuer), which keeps out work tenants, including any Arkin tenant;
  - `/me` matches the `expected_account` Paul typed;
  - the scopes granted.

  It stores the refresh token and sets `awaiting_approval`. The approve page shows **the account the Lambda verified**, never one the box supplied. The code is useless on the box.
- **Reconnect** returns the connection to `awaiting_approval` unless `/me` equals the previously approved account.
- **Token storage.** The access token lives only in warm Lambda memory. A rotated refresh token is written back **at most once a day**, keeping well inside the 100-version limit and the 10-minute guidance. This relies on the research claim that Microsoft does not revoke superseded refresh tokens [check with a live test in S4]. Old versions are unreadable through `AWSCURRENT`. L4 must narrow `openclaw-builder`.
- **Not offered:** device-code sign-in.
- **Revocation.** Microsoft's consumer endpoint has no RFC 7009 revocation (verified). Paul revokes on his account's consent page [URL `account.live.com/consent/Manage`: from memory, check]. Remove sets a tombstone and shows the console delete link.
- **Prerequisites:** L3 (rotate the tunnel token) and L4.

## 6. Permissions, untrusted output and the posting gate

**Tools.** Tools join `tools.TOOLS` with handlers and the drift test:

- `connection_read`: READ, OBSERVER, `ledger_head=False`, **chat loop only** (never selectable in an automatic cycle), from S3.
- `connection_act`: CHANGE, PROPOSER, `ledger_head=False`, from S7.
- `connection_sync`: non-plannable, with `source` set by code to `clock` or `operator`.

`GRANTABLE` is unchanged. Dispatch uses a frozen exact-string map from `"<id>.<op>"` to the manifest operation, built from enabled items with `read_allowed` set. For connection task types, code builds the action description (`connection_read <id>.<op>`), and `goal` is recorded as an HMAC plus a length.

**Three calls per connection turn** (this tightens the CB-1 split):

1. **The choosing call** is built from a fixed structure: Paul's latest raw message, the ids of enabled operations with Paul's notes, and `cite()` stubs. **None of** `your_operator`, `his_diary`, `his_week`, pinned memory, exchanges, notes, uploads, `proposals_awaiting_operator` or `recent_history`. Its arguments follow §3: enum, bounded int, code-built date, or `from_paul_only`.
2. **The reading call** gets the output inside the envelope, with none of the private sources above. Its only tools are "answer" and "propose". It has no browse, no shell, no free notify, and no read of another connection.
3. **The final answer call** gets full context and no tools.

Limits: at most 3 connection steps per turn. Each call's timeout is the smaller of 20 seconds and the time left in CB-8's 90-second budget. **CB-8 (turn ids and polling) is a hard prerequisite**, because three sequential Bedrock calls with a 120-second read timeout can pass Cloudflare's 100-second limit. There is also a cap of 20 reads per hour.

**Taint.** Code sets a taint flag on any turn in which a reading call ran. On a tainted turn:

- the `thought` entry keeps `answer_sha256` and drops `answer_head`, and `question` stays;
- the exchange is stored as kind `exchange_connection`, which joins `NOT_EVIDENCE` and is kept out of `recent_exchanges` in any call that holds tools;
- a proposal from the reading call takes a dedicated path. It records only `connection`, `op`, the payload HMAC and the approval id, not `_record_proposal`'s description, command and reasoning. The planner sees only "`<id>.<op>` awaiting approval".

**History.** Results of connection task types are replaced by a `cite()` stub, for example `connection_read outlook_mail.headers → ok, 14 items, hmac…`, **before** they enter `task_history`, `reflect()` or `recent_history`. Error codes replace the raw string in the result itself. **Browsed pages keep their current 800-character output for now.** Stubbing them would blind browsing, and moving pages to the reading call is a separate change to put to Jarvis.

**The envelope (`jarvis/agent/untrusted.py`).** It generalises `browse.py:61-128`, and `browse.envelope` becomes a wrapper around it.

- Markers read `BEGIN/END UNTRUSTED <PAGE|CALENDAR|FEED|MAIL|SERVICE> CONTENT source=<id>.<op>`.
- `defuse()` runs on keys and values: forged markers, control characters, bidi and zero-width characters.
- Enveloping Textract, upload and estate text is proposed separately to Jarvis (Q6).

**Memory:**

- Connection data is never stored by default.
- A note derived from a connection gets kind `connection_note`, which joins `NOT_EVIDENCE`. `is_evidence()` also refuses any `source` starting with `connection:`.
- **`note` joins `NOT_EVIDENCE`** (control A5(inj)). The alternative is to require corroboration. Jarvis is asked which (Q7).
- **"Remind me" on an outside event** opens an editable field. Paul confirms or edits the title, so the text genuinely becomes his, and it goes through `schedule.add` unchanged. `_persist` is not touched.

**busy_now.** An outside event counts as busy only if:

- Graph: `showAs` is busy or oof, **and** `responseStatus` is organiser or accepted;
- ICS: `PARTSTAT=ACCEPTED` or Paul is the organiser, **and** `TRANSP=OPAQUE`.

Each event holds notices for at most 3 hours, with a daily total cap. The hold reason is the fixed string "in a calendar event".

**Telegram in.** A message is accepted only if all of the following hold:

- `chat.type == "private"` and `from.id == chat.id ==` the pinned id;
- no `forward_origin`, `reply_to_message`, `quote`, `via_bot` or `guest_bot_caller_user`;
- text only, so a caption is refused;
- no business connection.

Everything else is dropped and counted. The field names are verified against the Bot API page. A Telegram turn gets a reduced context: no diary, no memory and no connection reads. It supplies no `from_paul_only` values and no destinations, and has no approval power. Paul should turn on Telegram two-step verification.

**UI rendering:** `textContent` only; http(s) link checks; HTML mail is never rendered; attachments are never fetched.

**Posting gate:**

- Anything not declared READ in a reviewed manifest, or marked READ by Paul, is outward.
- Destinations come verbatim from Paul's typed UI text or a list he set (CB-3). They never come from connection output or Telegram.
- **The approve page** (`connect-approve`, Function URL only):
  - It shows the request **as the broker resolved it from the frozen manifest**: host, method, path and fields. It never shows the box's description.
  - Text is cleaned the CP-1 way, with bidi and homoglyphs flagged and hosts shown in punycode.
  - The challenge `H(approval_id‖payload_sha256‖nonce)` is generated and stored server-side, and accepted once. Acts expire after 15 minutes, enabling after 24 hours.
  - The page has a strict nonce CSP, `frame-ancestors 'none'`, and no third-party scripts.
  - It needs `userVerification=required`.
  - Links to it carry no authority.
- **Passkey enrolment:**
  - The first enrolment needs a single-use code that Paul puts in `jarvis/connections/approve-enrol` through the console. Only the approve role can read it, and enrolment then closes.
  - A further passkey needs an assertion from an existing one, or a fresh console code.
  - Unauthenticated enrolment is refused once any credential exists.
  - Every enrolment emails Paul and writes an approvals-table row.
  - The RP ID is the full Function URL host, because `lambda-url.us-west-2.on.aws` is a public suffix. **Never delete and recreate the function**, since that orphans every passkey. Alternatively, use a custom domain [Paul].
- **`create_event` (S7).** The manifest allows only `subject`, `start`, `end`, `location` and `isReminderOn`. The broker strips or refuses `attendees`, `onlineMeeting` and `isOnlineMeeting`, and sets `responseRequested=false`. The consent card says the scope also allows deleting events.
- **Telegram out** is the only other pre-approved send. The broker ignores any destination argument.

**Closing the open exits (S0).** Each of these is a change to Jarvis's permissions, so each is put to Jarvis first.

- **SES:** `ses:SendEmail` gets `ForAllValues:StringEquals ses:Recipients = Paul's address` and `ses:FromAddress`. The keys are documented for SendEmail, and `notify.py` uses the v1 `send_email`. Confirm with `simulate-principal-policy`.
- **SNS:** `sns:Publish` is limited to one fixed topic ARN. Both `"*"` (sms) and `...:*` (topic) are too wide today.
- **Commands:** `getent` comes off the read-only list, and `boto3|botocore` joins the `python -c` egress pattern.
- **Shell deny additions:** `aws secretsmanager`, `aws lambda`, `aws dynamodb`, `/var/lib/jarvis/connections`, plus `SECRET_PATHS`.
- Root's ability to invoke the broker remains a stated residual.

## 7. Ledger and audit

Existing kinds only.

- **`action`** is written before the call, and the call is refused if the write fails: `{tool, connection, op, effect, source, args_hmac, args_len, manifest_sha256, data_class}`. The description is built by code, and `goal` is an HMAC plus a length.
- **`outcome`:** `{success, duration_s, out_hmac, bytes, items, broker_request_id}`, with no `head`. `error` is one of: `timeout | too_large | tls | http_4xx | http_5xx | auth | secret_withdrawn | host_refused | redirect_refused | parse | schema | rate | not_approved | arg_refused`.
- **HMAC.** All digests of arguments, outputs, payloads and refusals are HMAC-SHA256, with a key kept off the ledger (CB-6), so postcodes, number plates and dates cannot be brute-forced.
- **`gate` refusals:** host, HMAC and length, recorded before DNS.
- **Tainted chat turns:** the `thought` entry carries `answer_sha256` only (§6).
- **Lifecycle entries** (`action`, operator): added, read_allowed, enabled, op_toggled, paused, removed, authorised, secret_version_changed, manifest_changed, tooldefs_changed, passkey_enrolled. They carry the ARN, VersionId, scopes and hashes, never a value.
- **Acts:** proposal (payload HMAC) → approval (approval id, payload HMAC, credential id) → execution (`action`/`outcome`).
- **Stated plainly:** approvals, enrolments and broker calls happen **off Jarvis's chain**. Their witnesses are the approvals table (with point-in-time recovery) and CloudWatch. The box's entries are its record of those events, not proof of them.
- **Volume:** either every sync, about 50 KB a day, or state changes plus a daily roll-up. Paul's choice.

## 8. UI: the Connections tab

- **Placement.** A ninth tab in the existing nav. It already scrolls sideways (`overflow-x:auto`, verified), so no "More" menu is needed.
- **Code rules:**
  - new code goes in the existing nonce'd script;
  - `data-click` dispatch, with no `on*` attributes;
  - outside text through `textContent` only;
  - logos inline or as data URIs;
  - outside links as `target=_blank rel="noopener noreferrer"`, http(s) only.
- **Catalogue cards**, grouped Everyday, Calendar & mail, Messaging, Home & money, Custom. Each card shows:
  - what Jarvis can and cannot do;
  - a "Reads only" or "Asks you every time" badge;
  - the hosts contacted;
  - what it needs (nothing, a key, or a Microsoft sign-in);
  - the data sent out (for example "your postcode goes to postcodes.io");
  - the cost;
  - for scopes that can delete, a plain statement that they can.
- **My connections:**
  - status chips: Connected, Paused, Needs sign-in, Error, Changed – review;
  - a switch per operation, with a READ/ACT label;
  - hosts, secret name and version, verified masked account;
  - buttons: **Test (READ operations only)**, Allow reading, Pause, Reconnect, Remove.
- **Activity:** ledger entries filtered by connection.
- **Custom form:** a live validator, with the decision shown in words ("will contact api.example.com every 3 h and keep: date, type"), and the untruncated MCP tool review.
- **Notice:** Claude's own Gmail, Calendar, Drive, Zapier and Slack connectors are not Jarvis's.
- **Routes:**
  - `GET /connections`, `/connections/catalogue`, `/connections/<id>/activity`;
  - `GET /connections/oauth/callback/<provider>` (inert; served before auth);
  - `POST /connections/{add,<id>/pause|resume|remove|test|allow_read|ops|oauth/start,oauth/finish}`.

  Every POST needs auth plus `_same_origin`. The CSP is unchanged.

## 9. Build stages, effort and cost

Effort is in coding-agent days. Each stage is put to Jarvis before it lands, and deploys are told to it the same day.

| Stage | Contents | Effort | Running cost |
|---|---|---|---|
| **S0 prerequisites** | `untrusted.py`; connection-only history, `reflect()` and `task_history` stubs; taint flag and `exchange_connection`; `is_evidence` source check and `note` → `NOT_EVIDENCE` (if Jarvis agrees); `ledger_head` flag, error enum, code-built descriptions, HMAC digests; chat secret-shape guard; planner argument carry; CB-8; SES/SNS conditions, `getent`, `boto3` pattern, shell denies; boot refusal tests (`connection_act ∉ GRANTABLE`, no secret input field), stdlib and pytest only. **Paul:** L2, L3, L4 | 5–6 | 0 |
| **S1 framework + keyless** | `connect/` SAM app (`connect-broker`, tables, log retention, exception wrapping), box client with timeouts, three-step sync on its own clock, cache, `outside` block, read-only tab; Open-Meteo, bank holidays, Zenodo | 5–6 | Lambda free tier [unverified for this account]; DynamoDB pennies |
| **S2 ICS busy-only, feeds, custom GET, approve** | `connect-custom` (host-tag binding, argument checks), `connect-approve` (enrolment bootstrap, canonical page, CSP), keyed-secret flow; ICS busy-only via `icalendar` → `outside_calendar`, `busy_now` rules; custom `ics`/`feed`/`rest_get` | 7–8 | ~$0.40 per secret per month [list price unverified] |
| **S3 chat reads + Telegram out** | `connection_read` (chat only), three-call split, CB-2 map, dedicated proposal path; Telegram `send_to_paul` | 5–6 | +1 secret |
| **S3b titles mode (optional)** | titles to the UI and the reading call only | 1 | 0 |
| **S4 Graph read + mail-in** | **needs DNS Firewall live first**; `connect-oauth` (per-provider callback, flow cookie, `tid`, reconnect rule, daily write-back), Graph normalisers; SES receipt rule on a subdomain, DKIM/DMARC checks, text-only MIME, raw bucket unreadable by the box, lifecycle expiry [Paul] | 7–8 | +1–2 secrets; SES receiving and DNS Firewall per query [prices unverified] |
| **S5 keyed UK APIs** | Met Office, DVLA/MOT, Octopus, TfL, Darwin, postcodes.io | 2–3 | +1 secret each |
| **S6 Telegram in** | `_telegram_tick` every 30–60 s, strict filter, reduced context | 2–3 | 0 |
| **S7 first act** | broker-verified passkey execution, `create_event` without attendees | 5–6 | 0 |
| **S8 custom remote MCP** | stdlib client, pinned authorisation server, text-only results, definition pinning | 5–6 | 0 |

- **Total:** about 44–52 days.
- **Expected running cost:** under about $5 a month, with **nothing hourly-billed**. Network Firewall, which is hourly, is not proposed.
- **Off-box libraries** are built with `sam build --use-container`: `webauthn` (with cryptography, cbor2, pyOpenSSL), `icalendar` and `recurring-ical-events`. Nothing new is installed on the box.
- **Fallback if Paul declines Lambda deploys:** on-box sync for keyless feeds and busy-only ICS only, with the root residual stated. No mail or OAuth credential ever sits on the box.

## 10. Questions for Jarvis

These go through `jarvis_chat.py --file`, and its answers are reported verbatim, including where it disagrees and where it is wrong.

1. **`connection_sync`:** non-plannable, code-set `source`, its own clock, counts only. It cannot fetch on its own initiative.
2. **The tools:** their rungs, the fail-closed CHANGE default, not in `GRANTABLE`, per-connection "allow reading", and `connection_read` limited to chat.
3. **Context:** the new `outside` and `outside_calendar` blocks give it busy intervals and numbers only, never titles. Is busy-only enough to be useful?
4. **The three-call split:** the choosing call loses all private context, and the reading call loses private context, outbound tools and chaining.
5. **`from_paul_only`, code-built dates and CB-3:** Telegram turns get no destinations and a reduced context.
6. **The envelope and connection stubs.** Separately: should Textract, upload and estate text be enveloped, and should pages later move to the reading call?
7. **Memory:** the taint flag, `exchange_connection`, `connection_note`, the `connection:` source check, and **adding `note` to `NOT_EVIDENCE` versus requiring corroboration** (A5).
8. **Ledger:** HMAC digests, no `head`, the error enum, `answer_head` dropped on tainted turns, code-built descriptions, entries it writes about off-box events, and sync volume versus a roll-up.
9. **Approval:** it can no longer reach the approve function. Its proposals become approval requests executed off the box after Paul's passkey. It may disable a connection, and may enable keyless catalogue entries only. Does it want automatic disabling on anomalies it detects itself?
10. **Its own exits narrowed:** SES to Paul only, SNS to one topic, `getent` removed, the `boto3` pattern, and the new shell denies and `SECRET_PATHS`.
11. **`busy_now`:** accepted and opaque events only, a 3-hour cap per event, a fixed reason string, and the 2-hour staleness rule.
12. **Telegram:** as a second fixed channel, and later as a strictly filtered inbound voice that is not operator.
13. **Model budget:** three model calls per connection turn, against 60 per hour in the repo (120 reported live, unverified).

**Told the same day, not asked** (each said plainly to be off its chain):

- the SAM deploy, tables, secrets and log groups;
- IAM changes on the Lambda side;
- the SES receipt rule, MX record and bucket lifecycle;
- DNS Firewall;
- any narrowing of `openclaw-builder`.

## 11. Decisions for Paul

1. **Off-box brokers** (recommended) or on-box. On-box is acceptable only for keyless feeds and busy-only ICS while Jarvis runs as root.
2. **The catalogue and its order.** No Arkin, ever.
3. **Accounts and scopes:**
   - which account each service uses;
   - `Calendars.ReadBasic` or `.Read`;
   - `Mail.ReadBasic` only, or bodies;
   - whether `Mail.ReadWrite` is ever granted (it can delete mail);
   - whether `Mail.Send` is ever granted;
   - whether `Calendars.ReadWrite` is granted for S7 (it can delete events).
4. **The ICS link:** yes or no. Busy-only is the default; titles mode is available only after S3.
5. **Keys:** console only (recommended). Also the one-time enrolment code in the console.
6. **Approvals:** the passkey, expiry times (15 minutes for acts, 24 hours for enabling), and whether to use a custom domain for the approve page so its passkeys survive a redeploy.
7. **Ledger:** every sync, or state changes plus a daily roll-up.
8. **Outside processors:**
   - location to Open-Meteo, TfL and postcodes.io;
   - vehicle registrations to DVLA;
   - mail to Microsoft;
   - Telegram as a carrier of his messages.
9. **Estate:**
   - the SES inbound subdomain and MX record;
   - **DNS Firewall, billed per query, which is required before S4**;
   - the lifecycle expiry period on the raw-mail bucket (S3 deletes the objects, not Jarvis);
   - Lambda log retention;
   - no IMAP port 993;
   - no webhooks and no Access bypass.
10. **Prerequisites and a date:**
    - L2, L3 and L4 before any credential;
    - Telegram two-step verification;
    - a date for moving Jarvis off root, the only fix for risk 1.
11. **Deferred:** Google, Home Assistant, Open Banking (personal registration only, never Arkin Engine Ltd), health.

## 12. Risks

| Risk | Mitigation | What remains |
|---|---|---|
| 1. Root Jarvis, steered, invokes reads on enabled connections and sends the results out | Three-call split; bounded arguments; broker-side validation and caps; ReadBasic scopes; SES/SNS locked to Paul; `getent` and `boto3` patterns; DNS Firewall before S4 | Root can still invoke the broker and use open port 443. Only moving off root closes this |
| 2. Injection through calendar, mail or feed text | Planner gets busy-only data; the reading call has no private context or tools; envelope; taint flag | Compliance is probabilistic, and the answer to Paul can still be steered |
| 3. Invitation spam silences notices | Accepted and opaque events only; 3-hour cap; fixed reason | – |
| 4. Forged or phished approval | Broker verifies the assertion itself; challenge bound to the payload; canonical cleaned display; enrolment by console code | Loss of the passkey needs a new console code |
| 5. Approve Function URL is public | Inert GET; strict CSP; single-use challenges; reserved concurrency; logs | A public surface outside Cloudflare Access |
| 6. Stale refresh tokens in secret versions | `AWSCURRENT` passed explicitly with `StringEquals`; `UpdateSecretVersionStage` denied; daily write-back; L4 | Admin principals can still read them |
| 7. OAuth callback misuse or account swap | PKCE held off the box; flow cookie; `tid` and `/me` checks; reconnect returns to approval; L3 | – |
| 8. Manifest or MCP rug-pull | Definition pinning; pinned authorisation server; tools default to CHANGE; `from_paul_only`; text-only results | **The server can change behaviour and sees every argument** |
| 9. SSRF and DNS rebinding | Pinned IP; Lambda outside the VPC; validator before DNS | – |
| 10. Private text on the 30-day ledger | HMAC digests; no `head`; tainted-turn `thought` hashed; code-built descriptions; chat secret guard | Untainted chat turns keep `question[:500]` and `answer_head` as today |
| 11. Custom secret sent to the wrong host | Derived secret name plus the `jarvis:host` tag check | – |
| 12. Telegram impersonation or account takeover | Strict filter; reduced context; two-step verification | A taken-over account can still chat, but with no private context |
| 13. Mail-in spoofing | Aligned DKIM and DMARC PASS; subdomain MX; box cannot read the raw bucket | DMARC alignment when forwarding from Outlook.com is unverified |
| 14. Boot gate stops Jarvis starting | New refusal tests use the stdlib and pytest only; the identity check runs in dev tests | – |
| 15. Model budget, and Cloudflare's 100-second limit | Sync uses no model; 20 reads per hour; CB-8 required | – |
| 16. Off-chain activity mistaken for ledgered | Stated plainly; approvals table with point-in-time recovery | – |
| 17. Provider terms | – | Open-Meteo is non-commercial; GoCardless signups are closed; Entra secret expiry; ReadBasic may be too thin [check] |

**Still unverified:**

- whether SES v2 applies `ses:Recipients` the same way; `notify.py` uses v1, so run `simulate-principal-policy`;
- whether Microsoft keeps superseded refresh tokens valid (a live test in S4);
- the consumer consent-page URL;
- whether Outlook.com's busy-only publish option exists (from memory);
- the mobile console deep link;
- the Cloudflare Access cookie's SameSite setting, and whether the query string survives an Access re-login;
- DMARC and DKIM alignment when Paul forwards mail;
- the Lambda free tier for account 485964361844;
- prices for Secrets Manager, SES receiving and DNS Firewall;
- live state: rung, `max_calls_per_hour`, notify channel, and whether L1 and L5 are live.

---

## How each critique point was handled

**Security critique**

| # | Point | Handling |
|---|---|---|
| 1 | Titles reach the planner, ledger and memory | Accepted: busy-only in model context with tools; `outside_calendar` separate from `schedule.context()`; `outside_block` refuses `short_text`; titles only in S3b |
| 2 | The choosing call keeps private context | Accepted: fixed structure; bounded or code-built integers and dates; `from_paul_only` strings |
| 3 | Leaks via `answer_head`, exchanges, proposals; `NOT_EVIDENCE` keyed on kind | Accepted: taint flag, `exchange_connection`, dedicated proposal path, `connection_note` plus source check; `note` → `NOT_EVIDENCE` put to Jarvis as A5 with the corroboration alternative |
| 4 | Broker trusts the box | Accepted: broker-side argument checks, caps and approval consumption; Test runs READ only. Strengthened: the broker verifies the passkey assertion itself, because a Lambda cannot identify the IAM principal that invoked it |
| 5 | Enrolment and recovery | Accepted: console one-time code, assertion for further keys, user verification, notices |
| 6 | Approve-page abuse | Accepted: canonical broker-resolved display, CP-1 cleaning, server challenge, strict CSP |
| 7 | `create_event` sends invitations | Accepted: attendee fields stripped; consent card states delete |
| 8 | Open SES, SNS and `getent` exits | Accepted into S0. The `ses:Recipients` key is documented for SendEmail; the SNS topic branch was also found to be wildcarded. DNS Firewall is required before S4, but it is billed per query, so it is Paul's decision, and S4 waits on it |
| 9 | Invitations silence notices | Accepted |
| 10 | Custom secret not bound to host | Accepted: derived name plus host tag |
| 11 | MCP definition pinning overclaimed | Accepted: residual restated; pinned authorisation server; text-only results; CHANGE default |
| 12 | Telegram forwards and takeover | Accepted; field names now verified. Added `guest_bot_caller_user`, which the critique did not list |
| 13 | From check not enough | Accepted: aligned DKIM and DMARC, subdomain MX, raw bucket closed to the box |
| 14 | Plain hashes reversible | Accepted: HMAC throughout |
| 15 | Cycle and sync bypass chat protections | Accepted: `connection_read` chat-only; stubs before history; sync returns counts; codes in the result |
| 16 | Wrong-tenant sign-in; secret pasted in chat | Accepted; `tid` now verified from the discovery document; chat secret guard added |

**Feasibility critique**

| # | Point | Handling |
|---|---|---|
| 1 | CB-1 leaks in three places | Accepted; merged with security points 1–3 |
| 2 | "Never stored" is false; RAM | Accepted: stubs in `reflect()` and `task_history`; 64 KB cap to the box |
| 3 | `NOT_EVIDENCE` is kind-keyed | Accepted (verified) |
| 4 | "Remind me" relabels outside text as Paul's | Accepted with the first option: Paul confirms or edits the title, and `_persist` is unchanged |
| 5 | Sync cannot run outside the lock via `act()` | Accepted: three-step sync |
| 6 | Cycle clock is wrong | Accepted: separate ticks, and Telegram also runs while idle |
| 7 | Box can bypass the passkey | Accepted: no invoke on approve; `request_approval` and `add_draft` through the brokers; keyless enabling from the box stated plainly rather than routed through the passkey. It is a reviewed, credential-free catalogue entry, and adding a passkey step would add friction for almost no gain |
| 8 | First enrolment is trust-on-first-use | Accepted (same fix as security point 5) |
| 9 | OAuth fails in three places | Accepted: per-provider path, `iss` only where advertised (verified absent for consumers), S256 hard-coded, flow cookie, callback before auth, reconnect rule, verified account shown |
| 10 | Secrets Manager write-back and stage | Accepted: daily write-back, access token in Lambda memory, explicit `VersionStage`, `UpdateSecretVersionStage` denied, placeholder created by Paul. I rejected storing tokens in DynamoDB under KMS: it adds a KMS key and cost with no gain over warm memory plus a daily write |
| 11 | Boot gate | Accepted |
| 12 | Page stubs break browsing | Accepted: connections only; page change put to Jarvis |
| 13 | Action description and goal leak | Accepted |
| – | Timeouts, Lambda Timeout, CB-8 as hard prerequisite, `boto3` pattern, log retention and exception wrapping, CSP details, nav already scrolls, config passed to the runner, off-box libraries, minimal MCP client, SES lifecycle and DMARC | All accepted. The bucket lifecycle expiry is listed as Paul's decision, because it deletes data |
| – | Settled checks: bank-holidays URL, WebAuthn RP ID on a public suffix, no revocation endpoint | Adopted with the source noted. Busy-only Outlook publishing stays marked unverified, as the reviewer took it from memory |

**Files relied on:**

- /home/user/paul/CLAUDE.md
- /home/user/paul/docs/security-review-2026-09-27.md
- /tmp/claude-0/-home-user-paul/bbefcc4b-852e-5d28-9598-71ffb48726bb/scratchpad/security_controls.md
- /home/user/paul/jarvis/agent/consolidate.py
- /home/user/paul/jarvis/agent/core.py
- /home/user/paul/jarvis/agent/schedule.py
- /home/user/paul/jarvis/agent/notify.py
- /home/user/paul/jarvis/agent/authority.py
- /home/user/paul/jarvis/agent/executor.py
- /home/user/paul/jarvis/brain/llm.py
- /home/user/paul/jarvis/cloud/headless.py
- /home/user/paul/jarvis/ui/web/index.html
- /home/user/paul/aws/terraform/main.tf

**Downloaded pages** (in /tmp/claude-0/-home-user-paul/bbefcc4b-852e-5d28-9598-71ffb48726bb/scratchpad):

- ses_ck.html (SES developer guide)
- tg.html (Telegram Bot API)