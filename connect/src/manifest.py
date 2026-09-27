"""The catalogue, and how any call against it is checked.

A manifest says everything a connection may do: its one host, its
operations, the exact shape of every parameter, and its limits. Catalogue
manifests are written here and reviewed in a commit; nothing at run time can
add an operation or widen a parameter. Every call names an operation and
parameters, and check_call() refuses anything the manifest does not allow --
an unknown key, a value out of range, a string that is not on the list. The
broker runs that check itself and does not trust whoever called it.

Stage 1 is keyless and public: weather for places Paul saves, the UK bank
holidays, and the metadata of his own twenty Zenodo records.
"""

import hashlib
import json
from typing import Any, Dict

READ = "READ"
CHANGE = "CHANGE"

# Paul's published estate: the twenty records in aws/scripts/zenodo_plan.json.
# Reading any other record is not this connection's business.
PAULS_RECORDS = (
    "21243118", "21245130", "21245205", "21249771", "21260388", "21264935",
    "21279944", "21282804", "21340572", "21357450", "21443120", "21445040",
    "21445071", "21455540", "21515861", "21515932", "21516401", "22045477",
    "22164015", "22771015",
)

CATALOGUE: Dict[str, dict] = {
    "open_meteo": {
        "label": "Weather (Open-Meteo)",
        "kind": "rest_get",
        "host": "api.open-meteo.com",
        "auth": "none",
        # A location is personal data even without a name on it.
        "data_class": "personal",
        "limits": {"calls_per_hour": 6, "max_bytes": 64 * 1024, "timeout_s": 10},
        "operations": {
            "forecast": {
                "effect": READ,
                "params": {
                    # Two decimal places is about a kilometre: enough for
                    # weather, not enough to name a house.
                    "lat": {"type": "coord", "min": 49.0, "max": 61.0},
                    "lon": {"type": "coord", "min": -9.0, "max": 2.5},
                    "days": {"type": "int", "min": 1, "max": 7},
                },
            },
        },
    },
    "uk_bank_holidays": {
        "label": "UK bank holidays (GOV.UK)",
        "kind": "rest_get",
        "host": "www.gov.uk",
        "auth": "none",
        "data_class": "public",
        "limits": {"calls_per_hour": 2, "max_bytes": 256 * 1024, "timeout_s": 10},
        "operations": {
            "list": {
                "effect": READ,
                "params": {
                    "division": {"type": "enum", "values": [
                        "england-and-wales", "scotland", "northern-ireland"]},
                },
            },
        },
    },
    "zenodo": {
        "label": "Paul's Zenodo records",
        "kind": "rest_get",
        "host": "zenodo.org",
        "auth": "none",
        "data_class": "public",
        "limits": {"calls_per_hour": 30, "max_bytes": 256 * 1024, "timeout_s": 10},
        "operations": {
            "record": {
                "effect": READ,
                "params": {"record": {"type": "enum", "values": list(PAULS_RECORDS)}},
            },
        },
    },
}


class CallRefused(ValueError):
    """The call does not fit the manifest. Says why, never echoes a value."""


def manifest_sha256(manifest: dict) -> str:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get(connection_id: str) -> dict:
    if not isinstance(connection_id, str) or connection_id not in CATALOGUE:
        raise CallRefused("unknown connection")
    return CATALOGUE[connection_id]


def check_call(connection_id: str, operation: str, params: Any) -> Dict[str, Any]:
    """The checked, normalised parameters, or CallRefused.

    Unknown operations and unknown keys are refused, missing keys are
    refused, and every value is checked against its declared type. What
    comes back is what the connector uses: nothing the caller sent survives
    unless the manifest named it.
    """
    manifest = get(connection_id)
    ops = manifest["operations"]
    if not isinstance(operation, str) or operation not in ops:
        raise CallRefused("unknown operation")
    spec = ops[operation]
    if spec.get("effect", CHANGE) != READ:
        # Stage 1 has no approval path, so it has no changes.
        raise CallRefused("changes are not available at this stage")
    if not isinstance(params, dict):
        raise CallRefused("parameters must be an object")
    declared = spec.get("params") or {}
    extra = set(params) - set(declared)
    if extra:
        raise CallRefused("unknown parameter")
    out = {}
    for name, rule in declared.items():
        if name not in params:
            raise CallRefused(f"missing parameter {name}")
        out[name] = _check_value(name, params[name], rule)
    return out


def _check_value(name: str, value: Any, rule: dict):
    kind = rule["type"]
    if kind == "enum":
        if not isinstance(value, str) or value not in rule["values"]:
            raise CallRefused(f"{name} is not one of the allowed values")
        return value
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise CallRefused(f"{name} must be a whole number")
        if not rule["min"] <= value <= rule["max"]:
            raise CallRefused(f"{name} is out of range")
        return value
    if kind == "coord":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CallRefused(f"{name} must be a number")
        value = round(float(value), 2)
        if value != value or not rule["min"] <= value <= rule["max"]:
            raise CallRefused(f"{name} is out of range")
        return value
    raise CallRefused(f"{name} has an unsupported type")
