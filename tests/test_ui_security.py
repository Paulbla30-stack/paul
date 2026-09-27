"""The 27 September security review's web-UI fixes (L5 in
docs/security-review-2026-09-27.md): a nonce CSP on the page, no inline
handlers, the token out of localStorage, cross-site cookie POSTs refused, and a
logout that ends every session."""

import os
import re
import tempfile
import unittest
import urllib.error

from jarvis.cloud import session
from jarvis.cloud.headless import HeadlessRunner, UI_HTML_PATH, UI_NONCE_PLACEHOLDER
from tests.test_ui import LOG, call, make_agent


class TestUiSecurity(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.runner = HeadlessRunner(
            make_agent(), LOG, interval=0, status_port=None, token="t0k", token_file=None,
            token_persist_file=None, session_key_file=os.path.join(d, "session.key"),
            ui={"enabled": True, "host": "127.0.0.1", "port": 0, "tls": False,
                "tls_dir": os.path.join(d, "tls"), "upload_dir": os.path.join(d, "up")})
        self.port = self.runner.start_ui_server()
        self.addCleanup(self.runner.stop_status_server)
        self.base = f"http://127.0.0.1:{self.port}"
        self.host = f"127.0.0.1:{self.port}"

    def _headers(self, path):
        import urllib.request
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.headers, r.read().decode()

    def _cookie(self):
        value = self.runner.new_session_cookie().split(";")[0]
        return value

    def _post(self, path, body=b"{}", **headers):
        h = {"Content-Type": "application/json"}
        h.update({k.replace("_", "-"): v for k, v in headers.items()})
        return call(self.base, path, data=body, headers=h)

    # ---- the page ------------------------------------------------------

    def test_each_page_load_gets_its_own_nonce_and_a_strict_policy(self):
        first, html1 = self._headers("/ui")
        second, html2 = self._headers("/ui")
        csp1, csp2 = first["Content-Security-Policy"], second["Content-Security-Policy"]
        n1 = re.search(r"'nonce-([^']+)'", csp1).group(1)
        n2 = re.search(r"'nonce-([^']+)'", csp2).group(1)
        self.assertNotEqual(n1, n2)
        self.assertIn(f'<script nonce="{n1}">', html1)
        self.assertNotIn(UI_NONCE_PLACEHOLDER, html1)
        for needed in ("default-src 'none'", "form-action 'none'", "base-uri 'none'",
                       "frame-ancestors 'none'", "object-src 'none'"):
            self.assertIn(needed, csp1)
        script_src = re.search(r"script-src ([^;]+)", csp1).group(1)
        self.assertNotIn("unsafe-inline", script_src)
        self.assertNotIn("unsafe-eval", script_src)

    def test_the_page_has_no_inline_handlers_or_script_urls(self):
        with open(UI_HTML_PATH, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIsNone(re.search(r"\son[a-z]+\s*=\s*[\"']", html),
                          "an inline on*= handler would not run under the CSP")
        self.assertNotIn("javascript:", html.lower())
        self.assertEqual(html.count("<script"), 1)
        self.assertIn(f'<script nonce="{UI_NONCE_PLACEHOLDER}">', html)

    def test_the_page_never_stores_the_token_in_local_storage(self):
        with open(UI_HTML_PATH, encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotIn('localStorage.setItem("jarvis.token"', html)
        self.assertIn('localStorage.removeItem("jarvis.token")', html)

    # ---- cross-site requests ---------------------------------------------

    def test_a_cookie_post_from_another_site_is_refused(self):
        cookie = self._cookie()
        for extra in ({"Sec-Fetch-Site": "cross-site"},
                      {"Sec-Fetch-Site": "same-site"},
                      {"Origin": "https://evil.example"},
                      {"Origin": "null"},
                      {}):
            with self.assertRaises(urllib.error.HTTPError, msg=str(extra)) as cm:
                self._post("/goal", b"x", Cookie=cookie, **{
                    k.replace("-", "_"): v for k, v in extra.items()})
            self.assertEqual(cm.exception.code, 403, extra)
        descriptions = [g["description"] for g in self.runner.agent.planner.goals]
        self.assertNotIn("x", descriptions)

    def test_a_cookie_post_from_the_page_itself_is_accepted(self):
        cookie = self._cookie()
        _, status = self._post("/goal", b"a goal", Cookie=cookie, Sec_Fetch_Site="same-origin")
        self.assertEqual(status, 200)
        _, status = self._post("/goal", b"a goal", Cookie=cookie,
                               Origin=f"http://{self.host}")
        self.assertEqual(status, 200)

    def test_a_bearer_token_does_not_need_an_origin(self):
        # Scripts on the box (jarvis_chat.py) authenticate with the header,
        # which no other site can make a browser send.
        _, status = self._post("/goal", b"a goal", Authorization="Bearer t0k")
        self.assertEqual(status, 200)

    def test_a_valid_cookie_beside_a_stale_token_is_still_the_session(self):
        cookie = self._cookie()
        _, status = self._post("/goal", b"a goal", Cookie=cookie, Authorization="Bearer old",
                               Sec_Fetch_Site="same-origin")
        self.assertEqual(status, 200)

    # ---- logout ------------------------------------------------------------

    def test_logout_ends_every_session_not_just_this_one(self):
        other_device = self._cookie()
        this_device = self._cookie()
        self._post("/logout", b"", Cookie=this_device, Sec_Fetch_Site="same-origin")
        for cookie in (this_device, other_device):
            self.assertFalse(self.runner.session_valid(cookie))
        # and it survives a restart
        again = HeadlessRunner(make_agent(), LOG, token="t0k", token_file=None,
                               token_persist_file=None,
                               session_key_file=self.runner.session_key_file)
        self.assertFalse(again.session_valid(other_device))

    def test_a_cross_site_logout_only_clears_that_browsers_cookie(self):
        cookie = self._cookie()
        self._post("/logout", b"", Cookie=cookie, Origin="https://evil.example")
        self.assertTrue(self.runner.session_valid(cookie))

    def test_a_login_after_logout_works(self):
        self._post("/logout", b"", Cookie=self._cookie(), Sec_Fetch_Site="same-origin")
        self.assertTrue(self.runner.session_valid(self._cookie()))


class TestSessionNotBefore(unittest.TestCase):

    def test_a_session_issued_before_the_line_is_refused(self):
        key = "k" * 64
        value = session.issue(key, now=1000)
        self.assertTrue(session.verify(key, value, now=2000))
        self.assertTrue(session.verify(key, value, now=2000, not_before=1000))
        self.assertFalse(session.verify(key, value, now=2000, not_before=1001))

    def test_the_line_round_trips_and_is_private(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "session.key.not_before")
            self.assertIsNone(session.read_not_before(path))
            self.assertTrue(session.write_not_before(path, 1234.9))
            self.assertEqual(session.read_not_before(path), 1234.0)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
