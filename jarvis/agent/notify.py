"""The one way the agent can reach its operator when he is not looking at it.

Everything the agent learns is stuck inside the box until it can say so. It
noticed a full-ish disk, five scan findings and a run of failed logins, and sat
on all of it, because the only way it could speak was for someone to open the
UI and ask. A colleague who only speaks when spoken to is not a colleague.

So this is a channel out. It is also, deliberately, the narrowest channel that
does the job, because the same property that makes it useful -- it reaches a
person's pocket at 3am -- is what makes it worth constraining:

* **One destination, fixed at boot.** Taken from Secrets Manager or SSM by the
  bootstrap, exactly like the LLM key and the tunnel token. The model cannot
  read it, set it, or send anywhere else. "Send a message" is not a capability
  the planner has; "tell the operator" is, and the operator is already decided.
* **The hard gate is satisfied structurally, not waived.** The constitution says
  never publish or send anything externally without approval. The approval here
  is Paul choosing one destination and these limits, in advance, in config he
  controls and the agent cannot edit. That is what makes this not an exception.
* **Proportionality is code, not a request.** A prompt asking a model to be
  judicious is a hope. A rate limit is a fact. Severity floor, a gap between
  messages, a cap per hour, quiet hours, and deduplication by subject: all
  enforced here, below the model, on the way out.
* **Every attempt is recorded**, sent or not, with the reason it was held.

The failure mode this is built against is not the agent going quiet. It is the
agent becoming something Paul mutes -- at which point the channel is worse than
useless, because it looks like it works.
"""

import logging
import os
import re
import time
from typing import Optional

SEVERITIES = ("info", "notice", "alert")
# What each level is for, in the words the planner is given:
#   info   - worth recording, not worth a buzz; never sends on its own
#   notice - Paul would want to know today
#   alert  - Paul would want to know now, and it overrides quiet hours

DEFAULT_MIN_SEVERITY = "notice"
DEFAULT_MAX_PER_HOUR = 4
DEFAULT_MIN_GAP_S = 900          # 15 minutes between messages
DEFAULT_DEDUPE_WINDOW_S = 21600  # the same subject once per 6 hours
DEFAULT_QUIET = (22, 7)          # local hours [start, end); alerts ignore it
DEFAULT_TZ = "Europe/London"
SMS_LIMIT = 320                  # two segments; longer gets truncated, not split

DEFAULT_DESTINATION_FILE = "/etc/jarvis/notify.dest"


def _severity_rank(name: str) -> int:
    try:
        return SEVERITIES.index((name or "").lower())
    except ValueError:
        return SEVERITIES.index("notice")


def read_destination_file(path: Optional[str]) -> Optional[str]:
    """The destination is on disk 0600, put there by the bootstrap."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            value = f.read().strip()
        return value or None
    except OSError:
        return None


def looks_like_phone(value: str) -> bool:
    return bool(re.fullmatch(r"\+[1-9]\d{7,14}", (value or "").strip()))


def looks_like_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", (value or "").strip()))


class Notifier:
    """Decides whether a message goes out, then sends it.

    The backend is injectable so the rules can be tested without an AWS
    account and without sending anyone a text at 3am.
    """

    def __init__(self, config: Optional[dict] = None,
                 logger: Optional[logging.Logger] = None,
                 backend=None, clock=time.time):
        cfg = dict(config or {})
        self.log = logger or logging.getLogger("jarvis.notify")
        self.clock = clock
        self.enabled = bool(cfg.get("enabled", False))
        self.channel = str(cfg.get("channel") or "sns_sms").lower()
        self.region = cfg.get("region")
        self.sender_id = str(cfg.get("sender_id") or "Jarvis")[:11]
        self.from_address = cfg.get("from_address") or ""

        # The destination never comes from the model: file first, then config.
        self.destination = (read_destination_file(
            cfg.get("destination_file") or DEFAULT_DESTINATION_FILE)
            or str(cfg.get("destination") or "").strip())

        self.min_severity = str(cfg.get("min_severity") or DEFAULT_MIN_SEVERITY).lower()
        self.max_per_hour = max(0, int(cfg.get("max_per_hour", DEFAULT_MAX_PER_HOUR)))
        self.min_gap_s = max(0, float(cfg.get("min_gap_s", DEFAULT_MIN_GAP_S)))
        self.dedupe_window_s = max(0, float(cfg.get("dedupe_window_s",
                                                    DEFAULT_DEDUPE_WINDOW_S)))
        quiet = cfg.get("quiet_hours", DEFAULT_QUIET)
        self.quiet_hours = tuple(quiet) if isinstance(quiet, (list, tuple)) and len(quiet) == 2 else None
        self.timezone = str(cfg.get("timezone") or DEFAULT_TZ)

        self._sent_at: list = []
        self._recent_keys: dict = {}
        self._backend = backend
        self.stats = {"sent": 0, "held": 0, "failed": 0}
        self.last_error: Optional[str] = None
        self.last_sent_at: Optional[float] = None

    # ---- what the health check and the status endpoint can say ----------

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.destination and self.channel != "none")

    def destination_hint(self) -> Optional[str]:
        """Enough to recognise it, not enough to be a contact detail leak."""
        d = self.destination
        if not d:
            return None
        if looks_like_email(d):
            name, _, domain = d.partition("@")
            return f"{name[:2]}***@{domain}"
        if looks_like_phone(d):
            return f"{d[:3]}***{d[-3:]}"
        return d[:4] + "***"

    def status(self) -> dict:
        return {"enabled": self.enabled, "configured": self.configured,
                "channel": self.channel, "destination": self.destination_hint(),
                "min_severity": self.min_severity,
                "max_per_hour": self.max_per_hour,
                "quiet_hours": list(self.quiet_hours) if self.quiet_hours else None,
                "timezone": self.timezone,
                "sent_last_hour": len(self._recent_window()),
                "last_sent_at": self.last_sent_at,
                "last_error": self.last_error, "stats": dict(self.stats)}

    # ---- the rules ------------------------------------------------------

    def _recent_window(self) -> list:
        now = self.clock()
        self._sent_at = [t for t in self._sent_at if now - t < 3600]
        return self._sent_at

    def _in_quiet_hours(self) -> bool:
        if not self.quiet_hours:
            return False
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            hour = datetime.fromtimestamp(self.clock(), ZoneInfo(self.timezone)).hour
        except Exception:
            from datetime import datetime
            hour = datetime.fromtimestamp(self.clock()).hour
        start, end = int(self.quiet_hours[0]), int(self.quiet_hours[1])
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end   # the window crosses midnight

    def hold_reason(self, severity: str, key: Optional[str]) -> Optional[str]:
        """Why this message must not go out, or None if it may."""
        if not self.enabled:
            return "notifications disabled"
        if not self.destination:
            return "no destination configured"
        if self.channel == "none":
            return "no channel configured"
        rank = _severity_rank(severity)
        if rank < _severity_rank(self.min_severity):
            return f"{severity} is below the {self.min_severity} floor"
        now = self.clock()
        if key and self.dedupe_window_s:
            last = self._recent_keys.get(key)
            if last is not None and now - last < self.dedupe_window_s:
                mins = int((self.dedupe_window_s - (now - last)) / 60)
                return f"already sent about '{key}'; not again for {mins} min"
        if self.max_per_hour and len(self._recent_window()) >= self.max_per_hour:
            return f"hourly cap reached ({self.max_per_hour})"
        if self.min_gap_s and self._sent_at:
            gap = now - max(self._sent_at)
            if gap < self.min_gap_s:
                return f"only {int(gap)}s since the last message (gap {int(self.min_gap_s)}s)"
        # An alert is exactly the thing quiet hours should not swallow.
        if severity != "alert" and self._in_quiet_hours():
            return f"quiet hours {self.quiet_hours[0]}:00-{self.quiet_hours[1]}:00 {self.timezone}"
        return None

    # ---- sending --------------------------------------------------------

    @staticmethod
    def compose(subject: str, body: str = "", limit: int = SMS_LIMIT) -> str:
        """One message. A text nobody reads to the end is not a message."""
        subject = " ".join((subject or "").split())
        body = " ".join((body or "").split())
        text = f"Jarvis: {subject}" if subject else "Jarvis"
        if body:
            text = f"{text}\n{body}"
        if len(text) > limit:
            text = text[:limit - 1].rstrip() + "…"
        return text

    def send(self, subject: str, body: str = "", severity: str = "notice",
             key: Optional[str] = None) -> dict:
        """Try to reach the operator. Returns a verdict, never raises.

        The verdict is the point: a held message is a normal outcome, with a
        reason the agent can record and the operator can read later.
        """
        key = key or (subject or "").strip().lower()[:80]
        held = self.hold_reason(severity, key)
        if held:
            self.stats["held"] += 1
            self.log.info("Notification held (%s): %s", held, subject)
            return {"sent": False, "held": True, "reason": held,
                    "severity": severity, "subject": subject}
        text = self.compose(subject, body)
        try:
            backend = self._backend or self._build_backend()
            backend(self.destination, text)
        except Exception as exc:
            self.stats["failed"] += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.log.warning("Notification failed: %s", self.last_error)
            return {"sent": False, "held": False, "reason": self.last_error,
                    "severity": severity, "subject": subject}
        now = self.clock()
        self._sent_at.append(now)
        self._recent_keys[key] = now
        self.last_sent_at = now
        self.last_error = None
        self.stats["sent"] += 1
        self.log.info("Notification sent (%s): %s", severity, subject)
        return {"sent": True, "held": False, "reason": None, "severity": severity,
                "subject": subject, "chars": len(text)}

    # ---- backends -------------------------------------------------------

    def _build_backend(self):
        """Resolved late so importing this module needs no AWS anything."""
        channel = self.channel
        if channel == "sns_sms":
            return self._sns_sms
        if channel == "sns_topic":
            return self._sns_topic
        if channel == "ses_email":
            return self._ses_email
        raise RuntimeError(f"unknown notification channel: {channel}")

    def _sns(self):
        import boto3
        return boto3.client("sns", region_name=self.region or None)

    def _sns_sms(self, destination: str, text: str):
        attrs = {"AWS.SNS.SMS.SMSType": {"DataType": "String",
                                         "StringValue": "Transactional"}}
        if self.sender_id:
            attrs["AWS.SNS.SMS.SenderID"] = {"DataType": "String",
                                             "StringValue": self.sender_id}
        self._sns().publish(PhoneNumber=destination, Message=text,
                            MessageAttributes=attrs)

    def _sns_topic(self, destination: str, text: str):
        self._sns().publish(TopicArn=destination, Message=text,
                            Subject="Jarvis")

    def _ses_email(self, destination: str, text: str):
        import boto3
        subject, _, body = text.partition("\n")
        boto3.client("ses", region_name=self.region or None).send_email(
            Source=self.from_address or destination,
            Destination={"ToAddresses": [destination]},
            Message={"Subject": {"Data": subject[:200]},
                     "Body": {"Text": {"Data": body or subject}}})


def build_notifier(config: Optional[dict], logger: Optional[logging.Logger] = None,
                   backend=None) -> Notifier:
    """From the cloud.notify block. Absent means a notifier that holds
    everything and says why, which is better than one that raises."""
    cloud = (config or {}).get("cloud") or {}
    return Notifier(cloud.get("notify") or {}, logger, backend=backend)
