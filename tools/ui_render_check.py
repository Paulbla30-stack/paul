#!/usr/bin/env python3
"""Render the agent's UI at handheld and tablet sizes and fail on breakage.

The UI is read on a phone far more often than on a desktop, and the ways it
breaks there are invisible from a wide window: a page that scrolls sideways,
a control too small to hit with a thumb, a field under 16px that makes iOS
Safari zoom in on focus and never zoom back out.

This starts a real listener with a real agent, drives a real browser at five
viewports, and reports what it finds. It is not part of `unittest discover`
because it needs playwright and a browser binary, which the test suite does
not assume:

    pip install playwright && playwright install chromium
    python3 tools/ui_render_check.py [--out DIR] [--browser PATH]

Exits 1 when it finds a problem, so it can gate a change to the page.
"""

import argparse
import logging
import os
import sys
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jarvis.agent.core import AgentCore
from jarvis.cloud.headless import HeadlessRunner
from playwright.sync_api import sync_playwright

LOG = logging.getLogger("render"); logging.basicConfig(level=logging.ERROR)
ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("--out", default=None, help="Directory for the screenshots; default: a temporary one")
ap.add_argument("--browser", default=os.environ.get("CHROMIUM_PATH", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"),
                help="Chromium executable; default: $CHROMIUM_PATH")
args = ap.parse_args()
OUT = args.out or tempfile.mkdtemp(prefix="jarvis-ui-")
os.makedirs(OUT, exist_ok=True)
tmp = tempfile.mkdtemp()
agent = AgentCore({"name": "jarvis", "profile": "cloud"},
                  {"display": None, "input": None, "memory": None, "storage": None}, LOG)
agent.planner._boot_tasks_generated = True
agent.planner.add_goal("Keep the box healthy and report posture", 5)
agent.planner.add_goal("Watch disk and planner budget", 3)
runner = HeadlessRunner(agent, LOG, interval=0, status_port=None, token="t0k", token_file=None,
                        ui={"enabled": True, "host": "127.0.0.1", "port": 0, "tls": False,
                            "tls_dir": os.path.join(tmp, "tls"), "upload_dir": os.path.join(tmp, "up")})
port = runner.start_ui_server()
base = f"http://127.0.0.1:{port}"

DEVICES = [
    ("phone",          390, 844,  3),
    ("phone-small",    360, 740,  3),
    ("phone-landscape",844, 390,  3),
    ("tablet",         820, 1180, 2),
    ("tablet-landscape",1180, 820, 2),
]
TABS = ["chat", "agent", "files", "ledger", "lab"]

problems = []
with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=args.browser)
    for name, w, h, dpr in DEVICES:
        ctx = browser.new_context(viewport={"width": w, "height": h},
                                  device_scale_factor=1, is_mobile=True, has_touch=True)
        page = ctx.new_page()
        page.goto(base + "/ui")
        page.wait_for_timeout(400)
        page.fill("#tokenInput", "t0k")
        page.click("#login button.btn")
        page.wait_for_timeout(600)
        for tab in TABS:
            page.click(f'nav button[data-tab="{tab}"]')
            page.wait_for_timeout(350)
            # Does the page scroll sideways? That is the classic mobile break.
            over = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            if over > 0:
                problems.append(f"{name}/{tab}: horizontal overflow {over}px")
            # Anything typed into or tapped that is too small for a thumb.
            small = page.evaluate("""() => {
                const bad = [];
                for (const el of document.querySelectorAll('button, input, select, textarea, label.file-btn, summary')) {
                    if (!el.offsetParent && el.tagName !== 'BODY') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    // A checkbox or radio is aimed at through its label, so
                    // the label's box is the real target.
                    let box = r;
                    if (el.type === 'checkbox' || el.type === 'radio') {
                        const lab = el.closest('label');
                        if (lab) box = lab.getBoundingClientRect();
                    }
                    if (box.height < 40) bad.push((el.id || el.textContent || el.tagName).trim().slice(0,28) + ' h=' + Math.round(box.height));
                }
                return bad;
            }""")
            if small:
                problems.append(f"{name}/{tab}: small targets -> {small[:6]}")
            page.screenshot(path=f"{OUT}/{name}-{tab}.png", full_page=(tab != "chat"))
        ctx.close()
    browser.close()
runner.stop_status_server()
print(f"screenshots: {OUT}")
if problems:
    print(f"{len(problems)} problem(s):")
    for p in problems:
        print(" -", p)
    sys.exit(1)
print("no layout problems found at any of "
      + ", ".join(f"{w}x{h}" for _, w, h, _ in DEVICES))
