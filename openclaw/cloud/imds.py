"""
EC2 Instance Metadata Service (IMDSv2) client.

Uses only the standard library so it works on a minimal AMI without boto3.
Every call is best-effort: when the agent runs somewhere that is not EC2
the client reports ``is_available() == False`` and the accessors return
``None`` / empty containers instead of raising.
"""

import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

DEFAULT_IMDS_URL = "http://169.254.169.254"
TOKEN_PATH = "/latest/api/token"
TOKEN_TTL_HEADER = "X-aws-ec2-metadata-token-ttl-seconds"
TOKEN_HEADER = "X-aws-ec2-metadata-token"

# Metadata keys exposed in summary(); value is the IMDS path under
# /latest/meta-data/.  Missing keys (e.g. public-ipv4 on a private
# instance) are simply omitted.
SUMMARY_PATHS = {
    "instance_id": "instance-id",
    "instance_type": "instance-type",
    "ami_id": "ami-id",
    "availability_zone": "placement/availability-zone",
    "region": "placement/region",
    "hostname": "hostname",
    "local_ipv4": "local-ipv4",
    "public_ipv4": "public-ipv4",
    "mac": "mac",
    "iam_role": "iam/security-credentials/",
}


class IMDSClient:
    """Small IMDSv2 client.

    ``base_url`` can be pointed at a fake server for tests or at a
    non-EC2 environment; ``OPENCLAW_IMDS_URL`` overrides the default.
    """

    def __init__(self, base_url: Optional[str] = None, timeout: float = 2.0,
                 token_ttl: int = 21600):
        self.base_url = (base_url or os.environ.get("OPENCLAW_IMDS_URL")
                         or DEFAULT_IMDS_URL).rstrip("/")
        self.timeout = timeout
        self.token_ttl = token_ttl
        self._token: Optional[str] = None
        self._token_expiry = 0.0
        self._available: Optional[bool] = None

    # ---- low-level -----------------------------------------------------

    def _request(self, method: str, path: str, headers: Optional[dict] = None,
                 data: Optional[bytes] = None) -> Optional[str]:
        req = urllib.request.Request(self.base_url + path, method=method,
                                     headers=headers or {}, data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and path != TOKEN_PATH:
                # Token expired or rejected – force a refresh next time
                self._token = None
            return None
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def _get_token(self) -> Optional[str]:
        now = time.time()
        if self._token and now < self._token_expiry:
            return self._token
        token = self._request("PUT", TOKEN_PATH,
                              headers={TOKEN_TTL_HEADER: str(self.token_ttl)})
        if token:
            self._token = token.strip()
            # refresh a little early
            self._token_expiry = now + max(self.token_ttl - 60, 30)
        else:
            self._token = None
        return self._token

    def get(self, path: str) -> Optional[str]:
        """GET an arbitrary IMDS path (e.g. ``/latest/meta-data/ami-id``)."""
        token = self._get_token()
        if token is None:
            self._available = False
            return None
        self._available = True
        if not path.startswith("/"):
            path = "/latest/meta-data/" + path
        return self._request("GET", path, headers={TOKEN_HEADER: token})

    # ---- high-level ----------------------------------------------------

    def is_available(self) -> bool:
        """True when an IMDSv2 token can be obtained."""
        if self._available is None:
            self._available = self._get_token() is not None
        return bool(self._available)

    def identity(self) -> dict:
        """Parsed instance identity document, or ``{}``."""
        raw = self.get("/latest/dynamic/instance-identity/document")
        if not raw:
            return {}
        try:
            doc = json.loads(raw)
            return doc if isinstance(doc, dict) else {}
        except ValueError:
            return {}

    def user_data(self) -> Optional[str]:
        """Raw user data string, or ``None`` when absent."""
        return self.get("/latest/user-data")

    def tags(self) -> dict:
        """Instance tags (requires "allow tags in metadata" on the instance)."""
        listing = self.get("tags/instance")
        if not listing:
            return {}
        tags = {}
        for key in listing.splitlines():
            key = key.strip()
            if not key:
                continue
            value = self.get(f"tags/instance/{key}")
            if value is not None:
                tags[key] = value.strip()
        return tags

    def summary(self) -> dict:
        """Compact description of the instance for the agent's memory."""
        if not self.is_available():
            return {}
        info = {"provider": "aws"}
        for key, path in SUMMARY_PATHS.items():
            value = self.get(path)
            if value is None:
                continue
            value = value.strip()
            if key == "iam_role":
                # listing of role names; normally exactly one
                value = value.splitlines()[0] if value else ""
                if not value:
                    continue
            info[key] = value
        return info
