# Security review: today's live system and the proposed chat-and-browser features

27 September 2026. For Paul. Nothing in this review has been built or changed.

## How it was done

The review looked at two things:
- the system as it runs today, at commit `3bf8aa0`;
- the features proposed for chat: browsing from chat, chat commands, media, generated files and downloads, copy buttons, and chat sessions.

Reviewers approached it from four directions: prompt injection, the web UI, the browser and media, and the infrastructure. They proposed **55 possible attacks**. The **22 rated high or critical** were each given to an independent checker. The checker confirmed or corrected every one, and in several cases lowered the severity. That is recorded below.

I rechecked the headline live finding myself on the box. The output is quoted in L1.

Most of the serious findings sit in features that **do not exist yet**. The chat-browsing loop and the planner's browse steps are the main ones. Five problems exist **today**.

---

## 1. Live today

### L1. The browser's kernel-level network block is not working (confirmed)

The browser unit is meant to stop the browser reaching:
- the AWS metadata service;
- private network addresses.

It lists those ranges under `IPAddressDeny`. But the line before them is `IPAddressAllow=any`. systemd checks the allow list first and grants access on any match, so the deny lines are never reached. From the box, read-only:

```
IPAddressAllow=::/0 0.0.0.0/0
IPAddressDeny=fc00::/7 10.0.0.0/8 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10 172.16.0.0/12 224.0.0.0/4 ff00::/8 fe80::/64
man systemd.resource-control:
  • Access is granted when the checked IP address matches an entry in the IPAddressAllow= list.
  • Otherwise, access is denied when the checked IP address matches an entry in the IPAddressDeny= list.
  • Otherwise, access is granted.
```

**What protects it now.** Only the browser's own address check in `guard.py`, which has known gaps. The metadata service requires session tokens (IMDSv2), which blunts the simplest route.

**Fix.** Delete the one `IPAddressAllow=any` line. Public browsing stays allowed, because anything not denied is allowed. The metadata service and private ranges become blocked at the kernel. Loopback stays open, so the browser's own API keeps working.

Add two checks:
- a deploy-time check that the unit never allows `any` while it denies the metadata range;
- a live check that the browser's user really cannot connect to those addresses.

This is Jarvis's browser, so Jarvis is asked first. It is a tightening.

### L2. The shell's protection for secret files matches text, not the files actually read

**What happens.** The shell refuses commands that name the protected files by their literal path: the runner token, the signing key, the session key, the UI key and the memory database. But it checks the command as written, before the shell expands it. A command written in a way that only resolves to those files when it runs would pass the check. Jarvis runs as root, so file permissions give no second line of defence. And the first part of any output goes onto the ledger, which cannot be deleted for 30 days.

**Why it is rated high, not critical.** The model would have to be steered into issuing such a command, and its instructions forbid reading secrets. That is a behavioural barrier, not a hard one. It gets weaker as soon as browsing feeds more outside text into the planner.

**A related gap.** One command on the read-only list can make DNS lookups. That makes it an outbound channel that needs no browser at all.

**Fix, lasting.** Run shell commands as a separate non-root user, with the secret directories hidden from it.

**Fix, until then:**
- resolve every path a command would actually touch before deciding, and refuse if any is protected;
- redact secret-looking output before it reaches the ledger or memory;
- take the DNS-capable command off the read-only list.

This concerns Jarvis's permissions, so Jarvis is asked first. Stopping Jarvis running as root is your decision.

### L3. The Cloudflare tunnel token exposed on 18 September: no evidence it was ever rotated

**The risk.** Anyone holding that token could run their own tunnel connector. Cloudflare could then send your UI traffic to their machine instead of the box, including your login. Cloudflare Access checks who *you* are. It does not check which machine answers.

**What to do (only you can; I have no Cloudflare access):**
1. In Zero Trust → Networks → Tunnels, refresh the token **and** disconnect any connectors that are still attached. A refresh alone leaves an existing rogue connector connected.
2. Put the new token in **Secrets Manager `jarvis/tunnel-token`**. That is where the box reads it. The note I gave you on 23 September said SSM `/hbf/*`, and that was wrong.
3. Restart `cloudflared`.
4. Then rotate the UI session key and the runner token, in case either was captured.
5. Turn on Access's binding cookie and shorten the session length.

Afterwards, Cloudflare should list only the box as a connector.

### L4. The root key and the admin key, again, plus the fact that nothing alerts you

This is the finding from the findings report, section 2. The reviewer adds three things.

1. **CloudTrail's 90-day event history does record what happens, and can't be edited.** But nothing reads it, and nothing tells you.
2. **The fix should include alerts to your notify channel** for:
   - use of the root user;
   - new access keys;
   - secret reads;
   - deletions of images, snapshots or buckets;
   - logging being turned off;
   - any ledger write from anyone other than Jarvis's own role.
3. **A trail kept in this same account can be stopped by the admin key.** Only one run from a separate management account is tamper-proof.

### L5. The web UI has no safety net if anything ever injects script into it

Four gaps sit together:
- The UI page sends **no Content-Security-Policy**.
- The **runner token is kept in the browser's localStorage**, even after a cookie login. That token is as good as root control of Jarvis.
- **State-changing requests don't check where they came from.**
- **Logging out doesn't revoke the session cookie.** It stays valid for up to 30 days.

The reviewers found **no way to inject script today**. Every field that shows outside text is written as plain text. So this is not a working attack now.

But every proposed feature adds new places that display outside text: browser cards, session titles, copy buttons, file cards. One mistake in any of them would be a complete takeover. So this has to be fixed **before** those features are built:
- a strict per-response CSP;
- the token out of localStorage;
- origin and content-type checks on every POST;
- sessions that die on logout or rotation.

Jarvis is asked first, because this changes how it is authenticated.

### Lower-rated items that are live today

| item | rating | fix |
|---|---|---|
| Jarvis's own notes can become pinned "standing fact" through repetition, so outside text can be laundered into memory. This is the same family as finding 3.2 in the findings report. | medium | Don't promote brain-written notes by repetition alone. |
| A chat answer is stored as if it came from you (`source="operator"`), even when a web page informed it. | medium | Label page-informed answers as web-derived, with citations. |
| When a browsed page is shortened for the planner, its closing marker is cut off, so the page text's boundary is lost. | medium | Store a citation line, or re-wrap the text. |
| Browser downloads have no size cap and are never cleaned up, so a page could fill the 2 GB box. | medium | Cap and clean up downloads. |
| A page that repeatedly crashes the browser can push its service into staying failed. | medium | Remember the crashing URL and back off. |
| The browser's request filter doesn't see WebSocket connections, and with L1 open nothing else stops them. | low–medium | Route WebSockets through the same check, and fix L1. |
| A browsed URL containing a secret, such as a reset link, is written into the ledger, the browser journal and the logs. | low today | Keep only scheme, host and path. |
| The UI listens on every interface, so exposure rests on the firewall staying closed. | medium | Bind to loopback when the tunnel is on. |
| `/status` answers without a token on loopback, including the diary and schedule. | low | Require the token. |
| Any token holder can make the browser act as "Paul pressing the button", which skips the posting gate. | low today | Covered by CC-2 below. |

---

## 2. Rules the proposed features must follow

These are requirements, not options. Each one has a test that proves it.

### Browsing from chat

This is where the four critical findings are. The danger is that one model call could hold your private context (diary, memory, notes, past conversations, uploads), read an outside page that tries to steer it, and be able to open a URL. That combination is how a hostile page could get your private details sent out inside an address. Today it is closed only by accident: a bug stops planner browse steps from carrying a URL at all. **Fixing that bug without these controls would open it.**

- **CB-1, the main control: split the call.** The model call that chooses where to browse gets only:
  - your latest message;
  - the current page;
  - the four browsing tools.

  It never gets your diary, memory, notes, past conversations or uploads. The final answer is written by a separate call that has full context but no tools.
- **CB-2.** A fixed list of four tool names, enforced in code. Clicking and typing, the shell, and anything else are never offered in chat.
- **CB-3.** Only open addresses you typed yourself, word for word, or links on the page. The model may never make up an address after it has read any outside text.
- **CB-4.** Scrolling moves by one fixed screen, so the scroll amount can't be used to signal information out.
- **CB-5.** Refuse to browse while the browser is signed in to anything, or while that isn't known.
- **CB-6.** Record a refused address as host, hash and length only, never the full text. The full text *is* the attack, and it would otherwise sit on the ledger for 30 days.
- **CB-7.** Keep the daily browser debrief out of the loop.
- **CB-8.** A chat turn finishes inside Cloudflare's 100-second limit, and a retried turn doesn't run twice.

### Chat commands that act as you

- **CC-1.** Commands are only recognised when you type them on your device. A model's reply can never issue one.
- **CC-2.** Anything that would post or send needs a confirmation tied to that exact page and element. Before any signed-in browser profile exists, it also needs a passkey. A cookie on its own is not enough.
- **CC-3.** A second press shows the real label of what will be clicked, taken from the page on the server, not from the model.

### Media and YouTube

- **MD-1.** Video plays **on your phone or computer, not on the box**. Jarvis finds the video and shows you a card with a Play button. The box browser is muted, has no sound device, and is too small: one YouTube page pushed it to its memory limit today.
- **MD-2.** A signed-in Google profile on the box is ruled out for normal use. If you ever want one, it would have to be:
  - a throwaway account, never your main one;
  - usable only for pages you open yourself;
  - with every model-driven step in a separate profile that has no cookies.

### Files and downloads

- **FL-1.** Jarvis never produces a file type that a browser would run as a page (html, svg, xml). Everything is served as a download.
- **FL-2.** Downloads are always saved as files, never opened in the UI.
- **FL-3.** Spreadsheet cells that would start a formula (`=`, `+`, `-`, `@`) are neutralised, and ordinary numbers are left alone.
- **FL-4.** Invisible control characters are stripped from documents. This also fixes a current bug where some characters make a broken .docx.
- **FL-5.** Word files never link to outside content.
- **FL-6.** Composing a file goes through Jarvis's recorded action path. If the ledger can't record it, the file isn't written.

### Copy buttons

- **CP-1.** What you see is exactly what gets copied. Invisible and direction-reversing characters are removed from both, and the button says when it removed any.
- **CP-2.** There is no one-click copy on page-sourced text. Code that came from a page shows a confirmation with the exact text and line count first.
- **CP-3.** The same clean-up happens on the server, so Jarvis, the Browser tab and your clipboard all see the same text. Jarvis is asked first, because this changes what it reads.

### Sessions

- **SS-1.** Every stored field is shown as plain text. Links are http or https only.
- **SS-2.** Stored and imported sessions are checked against a fixed shape, and an import is refused if any record fails.
- **SS-3.** Sessions stay on your device. The CSP and the token change from L5 ship **before** sessions do.

---

## 3. What no control here removes

- **The final answer still sees outside text alongside your private context**, even without tools. A hostile page could get Jarvis to *suggest* you click a bad link or copy a bad command. Plain-text rendering and the copy confirmation reduce this; they don't remove it.
- **Steering a model is a matter of probability.** Even with the split, a small amount of information could leak through allowed navigation. It is bounded by the fixed scroll and a per-hour cap on browse steps.
- **A visibly bad command.** If a model is steered into showing you one, no character clean-up catches it. Read commands before you run them.
- **While Jarvis runs as root, anything on the box can claim to be "Paul pressing the button"** to the browser service. Only the move off root fixes that.
- **Chat and browsing records on the ledger** can't be deleted for 30 days. That is a standing privacy cost.
- **Some things have to be checked on your phone,** not assumed:
  - that downloads work there;
  - how long the phone keeps stored sessions;
  - how your terminal handles pasting.

---

## 4. Who decides

**Yours:**
- **L3:** rotating the tunnel token, and the session key and runner token after it.
- **L4:** the root key, the admin key's permissions, alerts, and a separate trail.
- Whether chat-browsing addresses go onto the 30-day ledger at all.
- Whether a signed-in browser profile is ever enabled. My recommendation: never on the box.
- Where sessions are stored. My recommendation: on your device only.
- Moving Jarvis off root. That is the lasting fix under L2 and under the "pressing the button" risk.

**Put to Jarvis first, because they change its behaviour, permissions, memory or ledger:**
- L1, L2 and L5;
- every CB rule;
- the note-promotion change;
- the label on page-informed answers;
- the clean-up of page text (CP-3);
- new ledger fields for chat.

**Off-chain.** The review's reads of the box and the account are not on Jarvis's chain. It used Jarvis's browser only through the media probe, which Jarvis was told about at 15:14 UTC.
