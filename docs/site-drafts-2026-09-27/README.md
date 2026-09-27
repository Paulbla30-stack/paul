# heartbeat-framework.org drafts, 27 September 2026

These are drafts only. Nothing was published, submitted or changed online, and nothing was done in the AWS account. When these pages go live, that will be a direct change to the live site. It will not be recorded on any chain.

The files follow the live theme v2 template (26 Sep 2026): the same head, `header.top`, `nav.main`, mobile menu, `footer.site`, `/style.css` and `/site.js`. Links are root-relative with a trailing slash. All three files parse with every tag closed. **None of them has been looked at in a browser**, because this machine has none. The privacy drafter tested an earlier version of the opt-out box in headless Chromium: it loaded on first visit, stopped after unticking, came back after re-ticking, and did not scroll sideways at 375px. My no-JavaScript change has not been tested.

## 1. The files and where they go

| File | Live URL | What it is |
|---|---|---|
| `privacy.html` | `/privacy/index.html` → https://heartbeat-framework.org/privacy/ | The corrected privacy page. It replaces the live page, which says "No analytics scripts". |
| `mission.html` | `/mission/index.html` → https://heartbeat-framework.org/mission/ | New Mission page |
| `moat.html` | `/moat/index.html` → https://heartbeat-framework.org/moat/ | New page, "The Only Moat Is the Record". The breadcrumb reads Home › Argument › The moat. |

**Before publishing, search every file for `[CONFIRM`.** The privacy page has five visible markers where a fact is still unknown, numbered here in page order (see section 8). Resolve each one and delete the marker. Do not publish a marker.

## 2. Short mission statement (for the home page hero)

Recommended:

> Deployment risk is capability multiplied by culture. Regulation assesses the first term; this estate instruments the second — get the human team in order first, because whatever a machine integrates into, it amplifies. Every instrument is published open access under CC BY 4.0, so the organisation deploying AI can govern it on its own terms.

The first two sentences are the live home line, which condenses The Cultural Safety Case. The clause after the dash comes from the Before the Machine cover.

Alternatives:
- **Descriptive** (from the Reading Map v1.4 opening line): "Team psychology for the 24-hour organisation, extended to the mixed human–machine shift. Twenty-two open-access records by a registered mental health nurse — the papers argue; the instruments do."
- **Purposive** (from The Tenant's Record §5): "The instruments a care organisation needs to deploy and govern its own AI are written, published, and open access. The last respectable excuse for tenancy — we couldn't govern it ourselves — is now a download." Only use this with the "operations moat still stands" caveat and the "rent the sockets, own the gravity" distinction nearby. Without them it reads as "SaaS is dead", and the paper's §0 says that reading "has deleted the argument".

## 3. Three moat bullets (for the home or sovereignty page)

- **Free to copy, by design.** Every record is open access under CC BY 4.0, with the Glass Ledger's code under MIT. The estate exists so that "we couldn't govern it ourselves" is no longer a reason to rent, so there is no fee and no registration wall.
- **The record is what stays.** Twenty-two records deposited on Zenodo since 7 July 2026, an md5 checksum on every file, and one named author answering for all of it: a registered mental health nurse with sixteen years in clinical practice.
- **Built to fit, and built to fail in public.** A Reading Map and an eight-stage Instrument Deck that name every source by DOI, a ledger anyone can verify with a public key, and predictions registered in advance (four in The Cultural Safety Case, four in The Second Pair of Hands, three in After the Machine), so the estate can be proved wrong in print.

These differ from the earlier draft in three ways:
- "Seven predictions" was wrong. The Second Pair of Hands §9 registers four more (P1 Training, P2 Monitoring, P3 Users, P4 Organisations; `21340572.pdf.txt` lines 528–556).
- "Twenty-two DOIs" became "twenty-two records", because the Reading Map alone has three version DOIs and a concept DOI.
- "Make … a download" was reworded.

## 4. Navigation, footer and other site-wide changes

All three drafts already carry the first three changes below. **Every other page needs the same edits at the same time**, or navigation will be inconsistent and the opt-out will not work.

1. **Analytics loader (a blocker).** On every page, replace
   `<script defer src="https://static.cloudflareinsights.com/beacon.min.js" data-cf-beacon='{"token": "…"}'></script>`
   with the one-line loader at the end of `mission.html` or `moat.html`. The loader loads the beacon only when the visitor has not opted out, the browser sends no Global Privacy Control signal, and site storage is readable. The privacy page's opt-out box does nothing on any page that still has the plain tag.
   - This only works if the tag is written into the site's own HTML files. The live tag has no version number or ray ID in it, which looks like Cloudflare's manual snippet rather than automatic injection [inference].
   - Before relying on that, check two things: grep the site source for `beacon.min.js`, and check the Web Analytics setting in the Cloudflare dashboard. If Cloudflare injects the tag automatically, switch it to manual first. Otherwise the beacon loads twice, or loads regardless of the opt-out.
2. **Footer "About" column.** Add `<a href="/mission/">Mission</a><br>` as the first link. **Mobile menu `.small` row:** add `<a href="/mission/">Mission</a> · ` first. The primary nav stays at seven items. If you want Mission in the primary nav, put it before Argument.
3. **Footer `.bottom` first span.** Change it to `Papers CC BY 4.0 · Code MIT · <a href="/privacy/#analytics">Visit counting: opt out</a>`. This puts the information and the objection route on every page, including the first page a visitor opens (PECR Sch A1 para 5(1)(d)–(e) and 5(3); ICO: "sufficient information in a prominent location").
4. **/contact/.** There is no privacy link near the form today. Next to "Send enquiry", add: `How I handle what you send: <a href="/privacy/">Privacy</a>.`
5. **/argument/.** Add a link to `/moat/`, because the breadcrumb names Argument as its parent. `mission.html` already links to it. Add `/mission/` and `/moat/` to `sitemap.xml`, and optionally a "Mission →" link in the home hero next to "The argument".
6. Do **not** author the Cloudflare challenge script (`/cdn-cgi/challenge-platform/…/jsd/main.js`) into any page. Cloudflare adds it at the edge with a fresh ray ID on every response.

## 5. Privacy page: every change from the live page, with sources

The live page is dated "LAST UPDATED · 25 AUGUST 2026", and its full text is in the TEMPLATE brief. Evidence files are in `scratchpad/priv/`.

**Page metadata**

1. **Description, og:description and JSON-LD** now name the actual contents: enquiries, analytics and how to object, the security check and its cookie, storage, and rights.
2. **`<style>html:not(.js) #hbf-pref{display:none}</style>`** hides the opt-out box when JavaScript is off, because the box does nothing without it (legal N, voice 4). A `hidden` attribute would not work here, because `.pref{display:grid}` in `/style.css` overrides it.
3. **Mobile menu and footer** gain Mission and "Visit counting: opt out" (section 4).
4. **LAST UPDATED** is set to 27 September 2026. Change it to the publication date.

**Opening**

5. Removed "it collects nothing about you while you read them". The beacon loads on all 13 live pages fetched, including /privacy/ (ESTATE §7.9).
6. Added contact details (ICO Art 13 list: identity and contact details).
7. "No advertising and no tracking across other sites" became "nothing on this site is used for advertising or to follow you to other sites". Cloudflare's IP threat scoring works across its whole network, so the old wording claimed too much (legal B).
8. The opening now announces the visit count and the security check.

**Enquiries**

9. Added the contact-preference field ("Written reply" or "Callback"), which the live form collects (`live_contact.html` lines 56–58; accuracy 12).
10. Added a `[CONFIRM]` marker for anything else the database stores with an enquiry, such as timestamp, IP or user-agent. The D1 schema has not been seen (legal blocker 4).
11. Email enquiries are covered by the same purpose and basis, because the site publishes a mailto address.
12. Providing data is voluntary (ICO: statutory or contractual requirement).
13. New sentence on where the data comes from, including third parties named in someone else's enquiry (legal J; Art 14).
14. On sensitive data received despite the request, the page now says it is used only to reply and is deleted with the enquiry (legal K). It also adds: "This address is not watched for urgent help… call 999; in England, NHS 111 also has a mental health option." That line is **your call**. Wales and Scotland route mental health crisis calls differently, so keep the "in England" qualifier or reword it.

**Analytics (new section, `id="analytics"`)**

15. Purpose: "how often each page is read" (legal M), not "how many people".
16. What the script sends: page URL, referrer, browser/OS and versions, timings, memory figures, a one-off page-view ID, and IP and user-agent. Source: captured request in `probe_2026-09-27.json` and `beacon.min.js`.
17. **Corrected:** the exit request was described as an "empty signal". It is actually a Core Web Vitals report that includes the page element each measurement relates to. `beacon.min.js` builds it with `sendBeacon` and `WebVitalsV2`, with `lcpEntry`, `interactionTarget` and `largestShiftTarget` (accuracy 2). The LEGAL brief was wrong on this too.
18. "I do not see individual visitors and cannot pick one out" became "The dashboard shows me totals… not individual visits, and I do not try to identify anyone from it". On a low-traffic site, a rare referrer combined with a country can narrow things down (legal M).
19. Cloudflare's no-fingerprinting statement now keeps its qualifier, "for analytics" (cloudflare.com/web-analytics; accuracy 7, legal C).
20. Retention: 7 days at full detail, then about a 10% sample, with the dashboard showing six months (Web Analytics FAQ).
21. "Cloudflare does not publish…" became "I have found no published period…". An absence was not proved (accuracy 15). The page also says no statement of the storage location was found.
22. Legal basis: PECR Sch A1 para 5 (statistics exception), with the conditions stated, plus UK GDPR legitimate interests (Sch A1 text in `priv/src/www_legislation_gov_uk_uksi_2003_2426_schedule_A1.txt`, lines 88–99).
23. Opt-out tick-box, on by default. It stores `hbf-analytics=off` in localStorage, honours GPC, and treats blocked storage as off. The ICO accepts a default-on toggle but not browser settings alone. Storing the choice falls under para 4(2)(e)(ii).
24. Added "it stays until you clear this browser's storage" (legal M).
25. The email sentence became "an email cannot switch it off for you; the box can" (voice 6).
26. The first sentence became "Every page loads… unless you switch it off below" (voice 5).
27. Added a `<noscript>` line (legal N).

**Security check and cookie (new section, `id="cookie"`)**

28. The heading changed from "The one cookie" to "The security check and its cookie". One probe saw only `cf_clearance`. Cloudflare's cookies page says Bot Fight Mode also sets `__cf_bm`, which expires after 30 minutes of inactivity (`privacy_research/src/cf_cookies.md` lines 39–41). The page now says `cf_clearance` "is the cookie seen when this site was tested" and that `__cf_bm` may also be set (accuracy 4).
29. The section now opens with what the script does: it examines browser characteristics and sends the result to Cloudflare. PECR covers access to the device as well as storage on it (legal D, accuracy 6).
30. Cookie facts: HttpOnly, one year, possible US processing. Source: probe expiry 1822072063, plus the JavaScript Detections and cookies pages.
31. **Exemption.** "Strictly necessary for the security of the site" became a reliance on para 4 for storage and access strictly necessary "to protect the site and the information sent through it, and to prevent or detect fraud and technical faults". That is heads (a), (c) and (d), taken from the legal review. Head (b), the security of the visitor's own device, was dropped as the weakest fit.
    - Accuracy 5 suggested (c) only. I followed the legal review's wider set, because each head is quoted from the Schedule and the page cites para 4 without claiming a single head.
    - **This remains arguable.** The ICO judges "strictly necessary" from the user's side, and a static site can be served without JavaScript Detections. The option that removes the doubt entirely is switching off Bot Fight Mode / JavaScript Detections in Cloudflare.
32. Added that Cloudflare uses this data as a controller in its own right for its network (Cloudflare Privacy Policy §6, `privacypolicy.cf.txt` lines 69 and 110; legal D).
33. Kept "There are no analytics, advertising or marketing cookies". Dropped "That is why this site does not ask you to accept cookies", because it was a legal conclusion the page does not need.
34. Automated decisions moved into their own paragraph, with the bot check's automatic challenge disclosed (legal I).

**Recipients**

35. Cloudflare is named for hosting, the database, email routing (MX records → `route1/2/3.mx.cloudflare.net`), analytics and the security check.
36. Cloudflare's role is now split: processor for hosting, the database, email routing and the analytics and logs shown to me; controller for its own network security (legal E, accuracy 8, voice 1).
37. Database location is a `[CONFIRM]` marker offering the two wordings (legal H). Setting only the `weur` hint guarantees nothing (D1 data-location docs).
38. Email services are a `[CONFIRM]` marker, with the old page's Brevo + Google wording kept as the draft text. This is unverified; see section 8.
39. "Nothing you send is sold, shared…" became "…not passed to anyone beyond the services named here unless the law requires it" (legal F).

**International transfers (new section)**

40. Cloudflare: the UK–US data bridge (S.I. 2023/1028), with EU SCCs plus the UK Addendum as fallback (Cloudflare DPA v6.4).
41. Brevo, marked `[CONFIRM]`: France and Belgium are covered by UK adequacy for EEA states (DPA 2018 Sch 21 para 5(1)(a)). Onward transfers go to providers "outside the EEA, including in the United States and India", corrected from "United States" only. Brevo DPA Annex 2 lists Brevo CRM Solutions Limited in India under SCCs (`priv/src/brevo_terms.txt` line 471; accuracy 9).
42. Google, marked `[CONFIRM]`: keep this only if the mailbox is Workspace.
43. A copy of the safeguards is available on request (ICO).

**Not collected, and logs**

44. "No fingerprinting" was removed. The security check reads browser signals, and the page now says so (legal C, accuracy 6, voice 2).
45. Deleted "that is a function of how the web is served rather than anything this site adds". The site did add bot protection and analytics (legal C).
46. Log periods: "expire within days … up to 30 days" contradicted itself. It now reads "short-lived: the longest, the record of email routed to this address, is kept for up to 30 days" (accuracy 13, voice 3). The 24-hour and 7-day figures were dropped, because they hold only on the Free plan, which is unconfirmed.

**Retention**

47. "A reasonable period" became 12 months after the last message, with deletion from the database and the mailbox (including deleted items), a check every three months, and a cross-reference to analytics retention. **This is a proposal for you to accept or change** (ICO storage limitation: a justified period, a policy, periodic review).

**Rights**

48. New "Your right to object" section, kept separate from the other rights (ICO: "clearly and separately").
49. Added restriction to the rights. Portability is left out, because it does not apply to legitimate interests.
50. Added "free of charge", "I will reply within one month", and "if I cannot do what you ask, I will tell you why" (legal L).
51. Complaints to me by email or the contact form, acknowledged within 30 days, with progress and outcome reported. Source: DPA 2018 s.164A, in force 19 June 2026 (S.I. 2026/82 reg 3(a)). Added "I will treat anything that reads as a complaint as one" (legal M).
52. **Regulator.** S.I. 2026/1015 reg 2 abolishes the office of Information Commissioner and transfers its functions to the Information Commission on **30 September 2026** (`priv/si2026_1015.xml`). The page now reads "the Information Commissioner's Office until 30 September 2026, the Information Commission from that date", which is true whichever side of that date it is published. That ico.org.uk remains the regulator's address after the change has **not been checked**.

**Kept unchanged because still accurate:** the enquiry purpose and basis, no mailing lists, the note on clinical information, deletion on request, and the right to complain to the regulator.

**Scripts**

53. The plain beacon tag was replaced by the loader, and the opt-out script was added. Both are inline, which the site's CSP allows (`'unsafe-inline'`). The Cloudflare challenge script is not authored.

## 6. Mission and moat: corrections applied, and the ones I rejected

**Mission (`mission.html`)**
- "The estate began on the ward, not in the laboratory" was replaced with "written by a registered mental health nurse from sixteen years of clinical practice, for the people who run 24-hour care". Paper No. 1 says its evidence comes "mostly in acute hospitals, laboratories, and aviation" (`21243118.pdf.txt` line 55), and its subject is residential care, not wards. I used "for the people who run 24-hour care" rather than the accuracy review's "for the ward" for that reason.
- "What goes wrong on these wards is a preview" became "the failures documented in care are previews of failures now visible at national scale" (The Long Handover abstract, `21245205.pdf.txt` lines 21–23).
- The Deck order is fixed. The Deck runs "Gate · Screen · Specify · Oversee · Trial · Audit · Measure · Record", "bound in the order a deployment actually meets them" (`deck.pdf.txt` lines 7 and 19), which is not the Reading Map's order.
- The Mixed Shift list is restored in full: "do not sleep, do not gossip, do not take breaks, and do not die" (`21245130.pdf.txt` lines 9–10).
- "With code under MIT" became "with the Glass Ledger's code under MIT".
- "The board buying or governing a deployment" became "Whoever is buying or governing AI deployments" (the Reading Map's row).
- The Floor Test is now described as testing six of the series' claims (C1–C6), not the registered predictions.
- Predictions now read four in The Cultural Safety Case, four in The Second Pair of Hands and three in After the Machine.
- "Most of the papers" became "Many of the papers". By a grep for tier markers, 12 of the 20 extracted texts have one. The other 8 have none: The Second Pair of Hands, Risk Screen, Before the Machine, Floor Test, Glass Ledger, What the Signature Certifies, and both implementation papers.
- "Damped its errors" became "damped organisational error", the abstract's own words.
- The duplicate `DOI` card key became `DOI · ALL VERSIONS`.
- "Argument and synthesis" is restored as a quotation from The Floor Test abstract (line 9). The earlier drafter's note had wrongly called it unverified.
- The mission sentence "so that no organisation has to rent its governance from the supplier it is meant to be governing" had no source. It is now "so that 'we couldn't govern it ourselves' stops being a reason to rent", from The Tenant's Record §5.
- Added a link to `/moat/`.
- **Rejected: voice review, mission L42** ("Every current regime for governing AI" is broader than the source). That sentence is The Cultural Safety Case's own abstract, verbatim (`21443120.pdf.txt` lines 67–71). The paper's §3 narrows the survey to UK health and care, but its abstract does not. The page follows the abstract. Narrow it if you prefer.

**Moat (`moat.html`)**
- The beacon tag was replaced with the loader (blocker).
- Correctable: the section now lists four + four + three predictions.
- "Instruments declare their evidence tier" became "Many of the papers declare the evidence tier behind their claims". The Risk Screen, Floor Test and Glass Ledger have no tier marker.
- CC BY 4.0: the conditions apply only when the material is shared (§3). The page now reads "Anyone who passes it on…" and "Every copy that is passed on…", and "A service that publishes an adapted Risk Screen" replaces "adapts".
- Versions: only the Reading Map has several versions on Zenodo. The page now says "the Reading Map's three versions are all still readable".
- Deck DOIs: the DOIs sit on the section dividers, and the facsimile pages carry title and author. The Deck's photocopy line is now attributed to the Deck.
- "Lift one page, and its cross-references still point to the rest" became "A single worksheet can be lifted; the cross-referenced system it belongs to cannot be, without re-deriving it".
- Card key "FIRST DEPOSIT" became "RECORD". The trilogy and The Character Pathway share the 7 July creation date.
- Added Mission and the opt-out link to the menu and footer.
- Slug and title are kept: `/moat/`, "The Only Moat Is the Record". The Tenant's Record uses "moat" to mean a barrier the estate removes by publishing. A title claiming the estate has an exclusive moat would contradict that. The page claims only what copying cannot take.
- **Not yet rendered:** `ul.map` with `span.r` exists in `/style.css` (lines 202–203, and a single column at phone width, line 235) but no live page uses it.

## 7. Errors on the live site outside these three files (not changed here)

- Home ("Four doors") and /argument/ (meta description) say **"seven registered predictions"**. It should be at least eleven (see section 3).
- /argument/ and /papers/ credit the five standing rules to The Mixed Shift. The five appear together only in the Reading Map. The Mixed Shift §3.3 says "Machines report absences, humans assign meaning".
- /instruments/ says "Every one of them can be run on a real unit without new software". That is false for the Glass Ledger, which is Python code.
- The Reading Map says "All records… collected in the Zenodo community heartbeat-framework", but the community holds 17. Five records are missing from it: the Instrument Deck, The Tenant's Record, What the Signature Certifies, and both implementation papers.
- /story/ is out of date. It says "Seventeen papers" and also "twenty documents", and it omits four records.
- /implementation/ says "Stage 2, read and write". Learning Outside the Model rev. 7 says "Stage 1, read-only".
- /papers/ numbers records "01–22" in deposit order. That is a site-made scheme, which may conflict with the rule against inventing numbering.

## 8. Unverified: needs the Cloudflare dashboard, your accounts, or you

The Cloudflare connector is not authorised in this session. It needs authorising in claude.ai connector settings before any of the Cloudflare items can be checked from here.

1. **The D1 database: EU jurisdiction or only the `weur` hint?** This decides `[CONFIRM]` marker 2, in the Cloudflare bullet.
2. **What the enquiry handler stores alongside the form fields** (timestamp, IP, user-agent). Check the D1 schema. This is marker 1, in the enquiries section.
3. **Which service sends enquiry replies or notifications, and which mailbox receives them.** This is markers 3, 4 and 5: the email bullet and the Brevo and Google transfer bullets. What is known:
   - MX points to Cloudflare Email Routing, which forwards to a destination address.
   - The mailbox is whatever that forwards to. If it forwards to a personal Outlook/Hotmail or personal Gmail address, that provider is a separate controller, not your processor, and the wording must change [inference].
   - SPF lists only Amazon SES. `scout/config.toml` (lines 249–256) shows why: SES was verified for the domain on 23 September for the scout digest, with Easy DKIM on random-token selectors. That explains the SPF record, but says nothing about enquiry mail.
   - Brevo has a verification TXT record and receives DMARC reports. Whether it actually sends anything is unknown.
   - If SES sends enquiry mail, add: "Replies are sent through Amazon Web Services' email service (Amazon SES), which acts as my processor", plus a transfer line.
   - If the mailbox is personal Gmail, use: "My mailbox is provided by Google, which handles it as a separate controller under its own terms and privacy policy, not on my instructions", and drop the Google transfer bullet.
   - Brevo's DPA does not mention the UK IDTA or the UK Addendum.
4. **Is Bot Fight Mode / JavaScript Detections on, and is the beacon injected or hand-written?** The cookie was observed. The setting behind it is inferred.
5. **Does Cloudflare act as a processor for Web Analytics?** The statistics exception depends on it. The only support is the "Customer Logs" definition in Cloudflare's Privacy Policy (data shown in the customer's dashboard). Write that reasoning into a short legitimate interests assessment.
6. **Which Cloudflare plan, and are Workers Logs on for `/api/enquiry`?** The "up to 30 days" ceiling assumes nothing visible to you is kept longer.
7. **Data Privacy Framework listings** for Cloudflare and Google were not checked on the DPF site. The page relies on the companies' own statements.
8. **Where Web Analytics data is stored, and how long Cloudflare keeps raw IP and user-agent.** Nothing is published. The page says so.
9. **Whether ico.org.uk stays the regulator's address after 30 September.**
10. **The NMC PIN** has not been checked against the register. The pages rely on /founder/.

## 9. What you must confirm before publishing

1. Every `[CONFIRM` marker in `privacy.html` is resolved and deleted (section 8, items 1–3).
2. The analytics loader is on **every** page at the same moment the new privacy page goes live, and Web Analytics is set to the manual snippet. Until then the new privacy text is itself untrue.
3. **Retention: 12 months** after the last message, with a check **every three months**. Accept or change.
4. **LAST UPDATED:** set to the actual publication date.
5. **The crisis line** in the enquiries section: keep it, reword it, or remove it.
6. **Whether to keep bot protection.** If you keep Bot Fight Mode / JavaScript Detections, accept that the para 4 exemption is arguable. If you switch it off, remove the security-cookie section.
7. **"What I commit to"** on the Mission page is in your first-person voice: open access stays, field returns welcomed, nothing for sale. Approve the wording.
8. Mission and moat publish together with the footer and menu edits on every page, the /argument/ link, and the sitemap entries.
9. **CLAUDE.md is out of date** (not edited here):
   - "Twenty Zenodo records": Zenodo returns 22.
   - The Reading Map DOI `21516401` is v1.3. v1.4 is `22994740`, and all versions sit under `21445055`.
   - "P1–P3" as paper designations collides with the registered predictions P1–P4 in The Cultural Safety Case and The Second Pair of Hands.
