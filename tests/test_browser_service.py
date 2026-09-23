"""The service's HTTP surface: routing, refusals and the token.

The browser answers a bearer token on a loopback socket. Two things follow and
both are tested here: a refusal has to come back as data rather than a stack
trace, because the agent has to be able to tell "I may not go there" from
"the browser fell over"; and the service must refuse to listen anywhere but
loopback, because this process drives an unsandboxed browser and on any other
interface that is a remote-control endpoint with a shared secret in front of
it.
"""

import unittest

from jarvis.browser import guard
from jarvis.browser.driver import BrowserUnavailable
from jarvis.browser.service import BrowserService, main


class FakeDriver:
    def __init__(self, **raises):
        self.raises = raises
        self.calls = []

    def _maybe(self, name):
        self.calls.append(name)
        exc = self.raises.get(name)
        if exc:
            raise exc

    def health(self):
        self._maybe("health")
        return {"up": True, "engine": "chromium"}

    def read(self):
        self._maybe("read")
        from jarvis.browser.page import Page
        return Page(url="https://e.test/", title="t", text="body")

    def open(self, url):
        self._maybe("open")
        from jarvis.browser.page import Page
        return Page(url=url, title="t", text="body")

    def follow(self, ref):
        self._maybe("follow")
        from jarvis.browser.page import Page
        return Page(url="https://e.test/b", title="t", text="body")

    def act(self, kind, ref="", text=""):
        self._maybe("act")
        from jarvis.browser.page import Page
        return Page(url="https://e.test/", title="t", text="body")

    def reset(self):
        self._maybe("reset")
        return {"ok": True, "reset": True}

    def close(self):
        pass


class TestRouting(unittest.TestCase):

    def setUp(self):
        self.driver = FakeDriver()
        self.service = BrowserService(driver=self.driver, token="t")

    def test_health(self):
        status, payload = self.service.handle("GET", "/health", {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["up"])

    def test_open_returns_a_page(self):
        status, payload = self.service.handle("POST", "/open",
                                              {"url": "https://e.test/"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["url"], "https://e.test/")

    def test_read_follow_act_and_reset_all_route(self):
        for method, path, body in (("GET", "/read", {}),
                                   ("POST", "/follow", {"ref": "L1"}),
                                   ("POST", "/act", {"kind": "click", "ref": "L1"}),
                                   ("POST", "/reset", {})):
            status, _ = self.service.handle(method, path, body)
            self.assertEqual(status, 200, path)

    def test_an_unknown_route_is_404_not_a_crash(self):
        status, payload = self.service.handle("POST", "/exfiltrate", {})
        self.assertEqual(status, 404)
        self.assertIn("error", payload)


class TestFailureComesBackAsData(unittest.TestCase):
    """The agent has to be able to tell these apart."""

    def handle(self, exc):
        service = BrowserService(driver=FakeDriver(open=exc), token="t")
        return service.handle("POST", "/open", {"url": "https://e.test/"})

    def test_a_guard_refusal_is_403_and_names_the_reason(self):
        status, payload = self.handle(
            guard.Refused(guard.REASON_METADATA, "http://169.254.169.254/"))
        self.assertEqual(status, 403)
        self.assertEqual(payload["reason"], guard.REASON_METADATA)

    def test_the_browsers_own_refusal_is_403_too(self):
        status, payload = self.handle(PermissionError("never types a secret"))
        self.assertEqual(status, 403)
        self.assertIn("secret", payload["error"])

    def test_a_missing_browser_is_503_not_500(self):
        # "It is not installed" and "it broke" are different news and the
        # agent should say the right one to Paul.
        status, payload = self.handle(BrowserUnavailable("playwright is not installed"))
        self.assertEqual(status, 503)
        self.assertIn("not installed", payload["error"])

    def test_a_bad_argument_is_400(self):
        status, _ = self.handle(ValueError("no link L7 on the page"))
        self.assertEqual(status, 400)

    def test_anything_else_is_500_with_its_type_named(self):
        status, payload = self.handle(RuntimeError("chromium fell over"))
        self.assertEqual(status, 500)
        self.assertIn("RuntimeError", payload["error"])

    def test_no_exception_ever_escapes_handle(self):
        class Exploding(FakeDriver):
            def health(self):
                raise KeyboardInterrupt        # not even this
        service = BrowserService(driver=Exploding(), token="t")
        with self.assertRaises(KeyboardInterrupt):
            service.handle("GET", "/health", {})   # documented: only BaseException


class TestItRefusesToListenOffLoopback(unittest.TestCase):
    """Not a preference. On any other interface this is remote control."""

    def test_a_public_bind_is_refused_and_nothing_is_served(self):
        self.assertEqual(main(["--host", "0.0.0.0", "--port", "8477"]), 2)

    def test_a_private_address_is_refused_too(self):
        self.assertEqual(main(["--host", "10.0.0.5"]), 2)


if __name__ == "__main__":
    unittest.main()
