# heartbeat-framework.org: rolling in the new pages

Paul, 27 September 2026: "Roll the new pages in."

- `live/` is an untouched mirror of the site as served on 27 September: 47 pages, plus the stylesheet, script, sitemap, `llms.txt` and the feed.
- `release/` is what would be published: 49 pages. It is built by `python3 site/build.py`, which also checks it.
- `../docs/site-drafts-2026-09-27/README.md` explains every privacy-page change and its source.

## What the build changes

1. **Analytics.** The plain Cloudflare analytics tag on every page becomes the consent-aware loader. The opt-out on the privacy page therefore works site-wide, and Global Privacy Control is honoured.
2. **New pages.** It adds `/privacy/` (corrected), `/mission/` and `/moat/`.
3. **Navigation.** Mission goes first in the footer's About column and in the mobile menu. Every footer gets "Visit counting: opt out".
4. **Links.** The contact form gets a privacy link, and the argument page links to the Moat.
5. **Indexes.** The sitemap and `llms.txt` list the new pages.
6. **Challenge script removed.** Cloudflare's edge-injected challenge script is stripped from the mirror. It is not part of the site.

## Checked

- `build.py`: 49 pages, 0 problems.
- Chromium, at 1280 px and 375 px, on the home, privacy, mission, moat, contact and argument pages: no script errors and no sideways scroll.
- The opt-out: the box is ticked by default, unticking stores `off`, and the next page then loads no analytics.

## Before publishing (Paul)

1. Resolve the five `[CONFIRM]` markers on the privacy page. `build.py` prints the count.
2. Confirm the 12-month enquiry retention and the "999 / NHS 111" line.
3. In Cloudflare, make sure Web Analytics is the manual snippet, not automatic injection. Otherwise the beacon loads regardless of the opt-out.
4. Publishing needs the Cloudflare connector signed in, or an upload by Paul. Once published, the change is live on the public site and is not on any chain.

## Not changed, but worth knowing

The live argument page says "Seven registered predictions". The drafts' checks counted eleven across the estate: four in The Cultural Safety Case, four in The Second Pair of Hands and three in After the Machine. That is Paul's call; it was out of scope for this roll-in.
