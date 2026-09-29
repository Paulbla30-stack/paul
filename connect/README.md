# connect: Vigil's connections, off his box

Stage 1 of `docs/connections-plan-2026-09-27.md`. **Built and tested. Not deployed, and not linked to Vigil.** Paul, 27 September: "Don't link anything up yet. Just build it first and off box. Go off the order list."

## What is here

| File | What it does |
|---|---|
| `template.yaml` | SAM stack. One Lambda (`vigil-connect-broker`), one DynamoDB table (`vigil-connections`), a log group with fixed retention, and a least-privilege role (own table and own logs only; no Secrets Manager). Nothing is billed by the hour. |
| `src/validate.py` | Decides whether a host is allowed. It must be https on port 443 and not an IP address in any spelling. Localhost, `.local`, `.internal` and cloud metadata names are refused. **Arkin (`thearkinsystem.co.uk`) is refused in code.** Every address the host resolves to must be public. |
| `src/fetch.py` | One bounded GET. It connects to the address that was checked, verifying TLS for the host name. Compressed responses are refused, and the body is capped while it streams. It follows at most two redirects, and only on the same host, re-checking each one. Errors carry a short code, never the URL. |
| `src/manifest.py` | The catalogue. Each connection names one host, its operations and the exact shape of every parameter. `check_call` refuses unknown operations, unknown keys and out-of-range values. |
| `src/connectors/` | Weather (Open-Meteo), UK bank holidays (GOV.UK) and Paul's twenty Zenodo records. Each keeps only typed fields: numbers, dates, enums and fixed phrases. |
| `src/app.py` | The broker. Its operations are `catalogue`, `call`, `disable` and `status`. |
| `tests/test_connect.py` | 23 tests. They use a fake table and a fake network; no AWS or internet is needed. |

## The rules it enforces itself (it does not trust the caller)

- **Off until Paul switches it on.** A connection is off until he lists it in the `EnabledConnections` stack parameter. Vigil can **switch a connection off** (`disable`, with a fixed reason); he cannot switch one on.
- **The manifest is pinned.** A call must quote the manifest's sha256. If the manifest has changed, the call is refused.
- **Hourly caps are counted in DynamoDB** by the broker, atomically.
- **Only typed records come back,** at most 64 KB. Text from a service that could carry instructions is dropped, or replaced with a fixed phrase.
- **Logs** hold the operation, the connection id, a result code and a byte count. They never hold a URL, a parameter value or an exception message.

## Tests

```
python3 -m pytest connect/tests -q      # 23 passed
```

The parsers were also run once against the live services on 27 September 2026:

- **GOV.UK bank holidays:** 83 events for England and Wales. The next three are Christmas Day, the Boxing Day substitute on 28 December, and New Year's Day. Four historic one-off holidays (8 May 2020, 3 June 2022, 19 September 2022 and 8 May 2023) came back as "bank holiday" because their titles are not on the fixed list. That is intended.
- **Zenodo:** record 21516401, "The Heartbeat Framework: Reading Map", version 1.3, cc-by-4.0, with its view and download counts.
- **Open-Meteo:** returned HTTP 429 to the build container's shared address. The weather parser is tested only against a sample in Open-Meteo's documented format, not live data.

## What is deliberately not done yet

- **Not deployed.** Deploying creates a Lambda, a table and a log group in Paul's AWS account. That is Paul's decision. Vigil is told the same day, and the deploy is not on any chain.
- **No link to Vigil.** The instance role has no `lambda:InvokeFunction`, the box has no client or sync tick, and there is no Connections tab. Linking these is a later step and is put to Vigil first.
- **Later stages** follow the plan's order: the calendar through an ICS link (busy times only), feeds, custom GET connections and the passkey approval page; then Telegram; then Outlook.
- **Box-side groundwork.** The plan's S0 prerequisites on the box (the untrusted-text envelope, the memory corroboration rule and narrowed exits) belong to linking. They are not in this stage.
