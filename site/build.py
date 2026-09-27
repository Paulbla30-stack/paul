#!/usr/bin/env python3
"""Roll the new pages into heartbeat-framework.org. Builds release/ from live/.

Paul, 27 September 2026: "roll the new pages in" -- the corrected privacy
page, a Mission page and the Moat page, plus the site-wide edits they need:

  1. The analytics tag on every page becomes the consent-aware loader, so the
     privacy page's opt-out works everywhere, not just on the new pages.
  2. Mission goes first in the footer's About column and in the mobile menu.
  3. Every footer carries "Visit counting: opt out", linking to the privacy
     page's analytics section.
  4. The contact form gets a privacy link beside "Send enquiry".
  5. The argument page links to the Moat.
  6. The sitemap and llms.txt list the two new pages.

Cloudflare's challenge script, which the edge injects into every response
with a fresh ray id, is stripped from the mirror: it is not part of the site
and must not be authored into it.

live/ is an untouched mirror of the site as served on 27 September.
release/ is what would be published. Nothing here publishes anything.

    python3 site/build.py          # build and check; exit 1 on any failure
"""

import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(HERE, "live")
OUT = os.path.join(HERE, "release")
DRAFTS = os.path.join(HERE, "..", "docs", "site-drafts-2026-09-27")
PAGES = ("index.html", "404.html")     # every HTML page the site serves
NEW_PAGES = {"privacy": "privacy.html", "mission": "mission.html", "moat": "moat.html"}

BEACON = re.compile(r'<script defer src="https://static\.cloudflareinsights\.com/beacon\.min\.js"'
                    r" data-cf-beacon='[^']*'></script>")
CHALLENGE = re.compile(r"<script>\(function\(\)\{function c\(\)\{var b=a\.contentDocument.*?</script>",
                       re.S)


def loader() -> str:
    """The consent-aware loader, taken verbatim from the reviewed Mission draft."""
    with open(os.path.join(DRAFTS, "mission.html"), encoding="utf-8") as fh:
        for line in fh:
            if "hbf-analytics" in line and "beacon.min.js" in line:
                return line.strip()
    raise SystemExit("no loader found in mission.html")


def edit_page(html: str, path: str, load: str) -> str:
    html = CHALLENGE.sub("", html)
    html, n = BEACON.subn(lambda m: load, html)
    if n != 1 and "hbf-analytics" not in html:
        raise ValueError(f"{path}: expected one analytics tag, found {n}")
    html = html.replace('<div><b>About</b><a href="/founder/">Founder</a>',
                        '<div><b>About</b><a href="/mission/">Mission</a><br><a href="/founder/">Founder</a>')
    html = html.replace('<div class="small"><a href="/sovereignty/">',
                        '<div class="small"><a href="/mission/">Mission</a> · <a href="/sovereignty/">')
    html = html.replace('<span>Papers CC BY 4.0 · Code MIT</span>',
                        '<span>Papers CC BY 4.0 · Code MIT · '
                        '<a href="/privacy/#analytics">Visit counting: opt out</a></span>')
    if path == "contact/index.html":
        html = html.replace(
            '<button class="send" type="submit">Send enquiry</button>',
            '<button class="send" type="submit">Send enquiry</button>\n'
            '  <p class="small">How I handle what you send: <a href="/privacy/">Privacy</a>.</p>', 1)
    if path == "argument/index.html":
        anchor = 'the record instrument that operationalises it is <a href="/instruments/record/">the Glass Ledger</a>.</p>'
        html = html.replace(anchor, anchor + '\n<p>Why the approach holds, and why the gate comes first, '
                            'is set out in <a href="/moat/">The Moat</a>.</p>', 1)
    return html


def build() -> list:
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    shutil.copytree(LIVE, OUT)
    load = loader()
    for page, draft in NEW_PAGES.items():
        os.makedirs(os.path.join(OUT, page), exist_ok=True)
        shutil.copy(os.path.join(DRAFTS, draft), os.path.join(OUT, page, "index.html"))
    for root, _, files in os.walk(OUT):
        for name in files:
            if name not in PAGES:
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, OUT)
            with open(full, encoding="utf-8") as fh:
                html = fh.read()
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(edit_page(html, rel, load))
    sm = os.path.join(OUT, "sitemap.xml")
    with open(sm, encoding="utf-8") as fh:
        xml = fh.read()
    for page in ("mission", "moat"):
        url = f"https://heartbeat-framework.org/{page}/"
        if url not in xml:
            xml = xml.replace("</urlset>", f"  <url><loc>{url}</loc></url>\n</urlset>")
    with open(sm, "w", encoding="utf-8") as fh:
        fh.write(xml)
    llms = os.path.join(OUT, "llms.txt")
    with open(llms, encoding="utf-8") as fh:
        text = fh.read()
    if "/mission/" not in text:
        text = text.replace("- Argument: https://heartbeat-framework.org/argument/",
                            "- Mission: https://heartbeat-framework.org/mission/\n"
                            "- Argument: https://heartbeat-framework.org/argument/\n"
                            "- The Moat: https://heartbeat-framework.org/moat/", 1)
    with open(llms, "w", encoding="utf-8") as fh:
        fh.write(text)
    return check()


def check() -> list:
    problems, pages = [], 0
    for root, _, files in os.walk(OUT):
        for name in files:
            if name not in PAGES:
                continue
            pages += 1
            rel = os.path.relpath(os.path.join(root, name), OUT)
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                html = fh.read()
            if BEACON.search(html):
                problems.append(f"{rel}: plain analytics tag still present")
            if html.count("hbf-analytics") < 1 or html.count("beacon.min.js") != 1:
                problems.append(f"{rel}: loader missing or duplicated")
            if "__CF$cv$params" in html:
                problems.append(f"{rel}: Cloudflare challenge script authored into the page")
            if '<a href="/mission/">Mission</a><br><a href="/founder/">' not in html:
                problems.append(f"{rel}: footer has no Mission link")
            if '<a href="/privacy/#analytics">Visit counting: opt out</a>' not in html:
                problems.append(f"{rel}: footer has no opt-out link")
            if "[CONFIRM" in html and rel != "privacy/index.html":
                problems.append(f"{rel}: unresolved [CONFIRM] marker")
    with open(os.path.join(OUT, "sitemap.xml"), encoding="utf-8") as fh:
        xml = fh.read()
    for page in ("mission", "moat", "privacy"):
        if f"https://heartbeat-framework.org/{page}/" not in xml:
            problems.append(f"sitemap.xml: /{page}/ missing")
    print(f"{pages} pages checked, {len(problems)} problem(s)")
    return problems


if __name__ == "__main__":
    found = build()
    for p in found:
        print("PROBLEM:", p)
    with open(os.path.join(OUT, "privacy", "index.html"), encoding="utf-8") as fh:
        markers = fh.read().count("[CONFIRM")
    print(f"privacy page: {markers} [CONFIRM] marker(s) for Paul to resolve before publishing")
    sys.exit(1 if found else 0)
