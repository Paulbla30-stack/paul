"""Tests for the Cloudflare Tunnel credential path.

The UI was reachable only by opening a port in the security group, which for
a phone on a changing address meant opening it to everyone. A tunnel dials
out instead, so there is no inbound rule to get wrong. What has to be right
here is the credential: the token *is* the tunnel, and anyone holding it can
re-point the hostname at their own machine and collect the logins meant for
this one.
"""

import os
import tempfile
import unittest

from jarvis.agent.executor import check_command_allowed, normalise_shell_policy
from jarvis.cloud import bootstrap, tunnel


class TestTokenFile(unittest.TestCase):

    def test_token_is_written_0600_as_an_environment_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cloudflared.env")
            tunnel.install_token_file("  a-tunnel-token  ", path)
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
            with open(path) as f:
                self.assertEqual(f.read(), "TUNNEL_TOKEN=a-tunnel-token\n")

    def test_rewriting_the_token_leaves_no_readable_leftover(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cloudflared.env")
            tunnel.install_token_file("first", path)
            tunnel.install_token_file("second", path)
            self.assertEqual(sorted(os.listdir(tmp)), ["cloudflared.env"])
            with open(path) as f:
                self.assertIn("second", f.read())

    def test_status_reports_absence_without_reading_the_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cloudflared.env")
            self.assertEqual(tunnel.status(path)["token_installed"], False)
            tunnel.install_token_file("a-secret-token", path)
            state = tunnel.status(path)
            self.assertTrue(state["token_installed"])
            self.assertEqual(state["mode"], "0o600")
            self.assertNotIn("a-secret-token", str(state))


class TestProvisioning(unittest.TestCase):

    def _config(self, **tunnel_cfg):
        return {"cloud": {"instance": {"region": "us-west-2"}},
                "tunnel": dict(tunnel_cfg)}

    def test_inline_token_is_moved_out_of_the_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cloudflared.env")
            config = self._config(token="inline-token")
            note = tunnel.provision_token(config, path)
            self.assertIn("inline tunnel token moved", note)
            self.assertNotIn("token", config["tunnel"])
            self.assertEqual(config["tunnel"]["token_file"], path)

    def test_secrets_manager_then_ssm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cloudflared.env")
            config = self._config(token_secret="jarvis/tunnel")
            note = tunnel.provision_token(
                config, path, fetch=lambda *a, **k: None,
                fetch_secret=lambda name, region=None: "from-secrets")
            self.assertIn("Secrets Manager jarvis/tunnel", note)
            with open(path) as f:
                self.assertIn("from-secrets", f.read())

            config = self._config(token_ssm_parameter="/jarvis/tunnel")
            note = tunnel.provision_token(
                config, path, fetch=lambda name, region=None: "from-ssm",
                fetch_secret=lambda *a, **k: None)
            self.assertIn("SSM /jarvis/tunnel", note)
            with open(path) as f:
                self.assertIn("from-ssm", f.read())

    def test_an_unreachable_secret_is_a_note_not_a_crash(self):
        """A box that cannot fetch its token still boots; you fix it over SSM."""
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(token_secret="jarvis/tunnel")
            note = tunnel.provision_token(config, os.path.join(tmp, "e"),
                                          fetch=lambda *a, **k: None,
                                          fetch_secret=lambda *a, **k: None)
            self.assertIn("not readable", note)

    def test_no_tunnel_configured_is_not_an_error(self):
        self.assertEqual(tunnel.provision_token({}), "no tunnel section")
        self.assertEqual(tunnel.provision_token({"tunnel": {"enabled": False}}),
                         "tunnel disabled")
        self.assertEqual(tunnel.provision_token({"tunnel": {}}),
                         "no tunnel token configured")

    def test_configured_reads_every_source(self):
        self.assertFalse(tunnel.configured({}))
        self.assertFalse(tunnel.configured({"tunnel": {}}))
        self.assertTrue(tunnel.configured({"tunnel": {"token": "x"}}))
        self.assertTrue(tunnel.configured({"tunnel": {"token_secret": "s"}}))
        self.assertTrue(tunnel.configured({"tunnel": {"token_ssm_parameter": "p"}}))
        self.assertFalse(tunnel.configured({"tunnel": {"token": "x", "enabled": False}}))


class TestOverlayNeverCarriesTheToken(unittest.TestCase):

    def test_strip_secrets_removes_an_inline_token(self):
        config = {"llm": {"api_key": "sk-x"}, "tunnel": {"token": "tok",
                                                         "token_secret": "s"}}
        bootstrap.strip_secrets(config)
        self.assertNotIn("api_key", config["llm"])
        self.assertNotIn("token", config["tunnel"])
        # the *reference* stays: it is not a secret, and the box needs it
        self.assertEqual(config["tunnel"]["token_secret"], "s")

    def test_tags_name_the_secret(self):
        for tag, key in (("jarvis:tunnel-token-secret", "token_secret"),
                         ("jarvis:tunnel-token-parameter", "token_ssm_parameter")):
            config = bootstrap.build_cloud_config(_FakeIMDS({tag: "value-here"}))
            self.assertEqual(config["tunnel"][key], "value-here")


class TestTheAgentCannotTakeTheTunnel(unittest.TestCase):

    def setUp(self):
        self.policy = normalise_shell_policy({"enabled": True})

    def test_the_token_and_the_binary_are_denied(self):
        for command in ("cat /etc/jarvis/cloudflared.env",
                        "grep TUNNEL /etc/jarvis/cloudflared.env",
                        "cp /etc/jarvis/cloudflared.env /tmp/t",
                        "cloudflared tunnel run --token stolen",
                        "sudo cloudflared tunnel list",
                        "/usr/local/bin/cloudflared tunnel route dns x y"):
            self.assertIsNotNone(check_command_allowed(command, self.policy),
                                 f"should be denied: {command}")

    def test_it_can_still_see_whether_the_tunnel_is_up(self):
        """Fenced from taking it, not from reporting on it."""
        for command in ("systemctl is-active cloudflared",
                        "systemctl status cloudflared",
                        "journalctl -u cloudflared -n 20 --no-pager"):
            self.assertIsNone(check_command_allowed(command, self.policy),
                              f"should be allowed: {command}")


class _FakeIMDS:
    """Just enough IMDS for build_cloud_config: tags and nothing else."""

    def __init__(self, tags):
        self._tags = tags

    def summary(self):
        return {"instance_id": "i-test", "region": "us-west-2"}

    def tags(self):
        return dict(self._tags)

    def user_data(self):
        return ""


if __name__ == "__main__":
    unittest.main()
