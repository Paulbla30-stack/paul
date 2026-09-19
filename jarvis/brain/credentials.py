"""
API credential resolution for the brain.

Order (first hit wins):

1. ``ANTHROPIC_API_KEY`` in the environment
2. ``llm.api_key`` in configuration (discouraged; lands in a 0644 file)
3. ``llm.api_key_file`` (default ``/etc/jarvis/anthropic.key``, 0600)

On AWS the bootstrap service can populate the key file from AWS Secrets
Manager (``llm.api_key_secret``) or an SSM Parameter Store SecureString
(``llm.api_key_ssm_parameter``) using the instance role, so the secret never
appears in user data or cloud.yaml.
"""

import json
import os
import subprocess
from typing import Optional

# When a Secrets Manager secret is a JSON object, these fields are tried in
# order for the key itself.
SECRET_JSON_FIELDS = ("ANTHROPIC_API_KEY", "anthropic_api_key", "api_key", "apiKey", "key")

DEFAULT_KEY_FILE = "/etc/jarvis/anthropic.key"


def read_key_file(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        with open(path, "r") as f:
            key = f.read().strip()
    except OSError:
        return None
    return key or None


def resolve_api_key(llm_cfg: Optional[dict], env=None):
    """Return ``(key, source)``; ``(None, reason)`` when nothing is set."""
    env = os.environ if env is None else env
    llm_cfg = llm_cfg or {}

    key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return key, "env:ANTHROPIC_API_KEY"

    key = (llm_cfg.get("api_key") or "").strip() if isinstance(llm_cfg.get("api_key"), str) else ""
    if key:
        return key, "config:llm.api_key"

    path = llm_cfg.get("api_key_file") or DEFAULT_KEY_FILE
    key = read_key_file(path)
    if key:
        return key, f"file:{path}"

    return None, (f"no ANTHROPIC_API_KEY, llm.api_key or readable {path}")


def fetch_ssm_parameter(name: str, region: Optional[str] = None,
                        timeout: float = 20.0) -> Optional[str]:
    """Read a (Secure)String from SSM Parameter Store via the AWS CLI.

    Uses the instance role; no boto3 needed. Returns ``None`` on any
    failure (missing CLI, no permission, unknown parameter).
    """
    if not name:
        return None
    cmd = ["aws", "ssm", "get-parameter", "--name", name, "--with-decryption",
           "--query", "Parameter.Value", "--output", "text"]
    if region:
        cmd += ["--region", region]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _run_aws(args: list, region: Optional[str], timeout: float) -> Optional[str]:
    cmd = ["aws"] + args
    if region:
        cmd += ["--region", region]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def extract_secret_value(raw: Optional[str]) -> Optional[str]:
    """A secret may be the bare key or a JSON object holding it."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            doc = json.loads(raw)
        except ValueError:
            return raw
        if isinstance(doc, dict):
            for field in SECRET_JSON_FIELDS:
                value = doc.get(field)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            if len(doc) == 1:
                only = next(iter(doc.values()))
                if isinstance(only, str) and only.strip():
                    return only.strip()
            return None
    return raw


def fetch_secretsmanager_secret(secret_id: str, region: Optional[str] = None,
                                timeout: float = 20.0) -> Optional[str]:
    """Read a secret's string value from AWS Secrets Manager via the AWS CLI.

    ``secret_id`` is a name or ARN. Uses the instance role; no boto3
    needed. Returns ``None`` on any failure.
    """
    if not secret_id:
        return None
    raw = _run_aws(["secretsmanager", "get-secret-value", "--secret-id", secret_id,
                    "--query", "SecretString", "--output", "text"], region, timeout)
    return extract_secret_value(raw)


def install_key_file(key: str, path: str = DEFAULT_KEY_FILE) -> str:
    """Write the key to ``path`` with mode 0600 (atomic replace)."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    try:
        os.unlink(tmp)  # stale or planted leftovers; never write through them
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(key.strip() + "\n")
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path
