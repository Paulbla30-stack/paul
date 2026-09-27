# The move to Vigil, 27 September 2026: one failed attempt, one good one

All times UTC. Actions here were taken by a coding agent over SSM. They are **not**
on any chain, except where the agent itself recorded them.

## What happened

- **22:06** The first move from Jarvis to Vigil (`aws/scripts/migrate_to_vigil.sh apply`)
  re-ran the bootstrap. The bootstrap rebuilds `cloud.yaml` from the instance's
  launch user data, which still named the agent `paul-life-agent` and pointed at
  `imported-model/jfu3j2ssmqvx`, a Bedrock import deleted weeks earlier. The
  working name (`jarvis`), `operator_name: Paul` and the model
  `qwen.qwen3-235b-a22b-2507-v1:0` existed only as hand edits in `cloud.yaml`.
  The migration's final check failed on the name, and the migration rolled back.
- **22:07–22:23** The rollback restored the layout but not `cloud.yaml`. The agent
  ran as `paul-life-agent` with no planner; its rule planner kept cycling, and
  the ledger went from seq 13337 to 13365. The old code takes the ledger writer
  from the agent's name, so for these minutes the S3 anchor also wrote under
  `ledger/paul-life-agent/` (4 checkpoints, last 22:23:41). Object Lock keeps
  that prefix; it is inert and stays as the record of this incident.
- **22:23** At Paul's go-ahead, the three settings were restored by hand and the
  agent restarted: `jarvis`, Qwen3-235B, anchor back on `ledger/jarvis/`.
- **22:30** The second move, with the fix below, succeeded. The agent runs as
  Vigil; its naming entry is ledger seq 13404; the ledger verifies INTACT
  (13425 entries) against the pinned key; the anchor writes to `ledger/jarvis/`.

## The fix

- **`/etc/vigil/local.yaml`** holds the operator's settings. It is loaded after
  `cloud.yaml` (`vigil.service`) and the bootstrap never writes it. It is
  derived from the hand-edited `cloud.yaml` by `vigil.cloud.local_settings`.
- **The migration** now backs up `cloud.yaml` and restores it on rollback. It
  checks the effective settings from the agent's own loader before starting
  (name Vigil, writer `jarvis`, the running model), and after starting requires
  the brain to be available as well as the name.
- **The rehearsal** ran the new bootstrap with `--print` against the live launch
  data, writing nothing. It reproduced the failure and showed the fix before the
  second attempt.

## Still true

- **A reboot on the old layout would have caused the same failure.** With
  `local.yaml` in place it no longer does, but the launch user data still
  carries the stale name and model. Changing it needs the instance stopped;
  that is Paul's call.
- **Untested:** the rollback's `cloud.yaml` restore has not been exercised live.
- **Kept for rollback:** the old code (`/usr/lib/jarvis`), the old unit files
  (`*.pre-vigil.*`) and `/root/cloud.yaml.before-vigil-retry`.
