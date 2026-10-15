# Fixing the findings in *Learning Outside the Model* rev 7 and *When the Weights Can Move* v2

27 September 2026. For Paul. Nothing in this report has been built or deployed.

## How this was checked

Three research agents took the findings in the two papers and worked out how each could be fixed: kernel and fences, the record, and memory. Their output came to 21 load-bearing claims. Each claim was then given to independent checkers, 30 in all, each told to try to refute it. Most claims were checked twice, once on the facts and once on whether the proposed fix works. The checkers worked from the code at `0ca015c`, from read-only calls on the box and in the account, and from primary documentation.

- **No claim was refuted.** One held exactly as stated (M3). The other 20 held with corrections. Several corrections change what should be done, and they are marked below.
- **The research got some things wrong, and the checkers caught them.** Each correction is marked "Correction to the research" where it comes up. Two proposed fixes would have made things worse. The boot-gate command (3.3) would have stopped Jarvis booting. Testing stages in a throwaway unit that shares Jarvis's runtime directory (section 4) would have deleted the token I use to reach it. A third, the `.pyc` fix (3.7), would not have worked.
- **A checklist of 177 findings across both papers** is in `docs/paper-checklist-2026-09-27.md`. The research covers 106 of them. The other 71 are paper text, publication steps, or small code items; they are grouped in section 7.

I ran the key numbers again myself this afternoon. The outputs are quoted where they appear.

---

## 1. The short version

1. **The biggest risk to Jarvis's record is not Jarvis. It is the credentials that can open a root shell on its machine.** There are two, and neither has MFA:
   - the coding agent's `openclaw-builder` key, which has AdministratorAccess;
   - an access key on the account's **root user**. It was created on 23 September and used once, a minute later, from a server on AWS.

   Everything else in this report comes after this.
2. **There is a date: 18 October 2026, 21:56 UTC.** That is when the oldest witness copies of the ledger come out of their 30-day lock and become deletable. The auditor's only pin (seq 12, 18 Sep) is among them.
3. **Witness storage grows quadratically,** because every anchor uploads the whole ledger again. It is 13.5 GB now, heading for about $21 a month by mid-December and about $325 a month by next September. A small change to the anchor holds it near $1 a month.
4. **There are five live defects in Jarvis's code:**
   - told-memory promotion;
   - the "pinned by the operator" label;
   - a boot gate that its own env file can pass;
   - memory written before the ledger;
   - self-knowledge read from an unverified chain.

   There is no sign that any has been used: the chain shows zero told-memory promotions ever, and the gate's last boot ran all 83 tests. Jarvis agrees with every fix it was asked about, with two refinements.
5. **Hardening Jarvis while it stays root would only be a tripwire.** Making it non-root is the real boundary, and that takes a permission migration that has not been designed yet.
6. **The paper needs corrections.** Some numbers are stale, the imported-model claim is out of date in both papers, the browser is missing entirely, and the learning measures cannot be computed.

---

## 2. Do these first. They are yours, not code

### 2.1 The root user has an access key and no MFA (new, urgent)

What the account reports, read-only, today:

```
GetAccountSummary: AccountAccessKeysPresent=1  AccountMFAEnabled=0  MFADevices=1  MFADevicesInUse=0
CreateAccessKey  2026-09-23T15:38:58Z  Root  81.109.x.x   key …OQO4
CreateAccessKey  2026-09-23T17:40:55Z  Root  81.109.x.x   key …XP7A
key …XP7A used:  2026-09-23T17:41:48Z  ses:GetSendQuota  from 3.85.94.16  aws-sdk-js/3.1137.0 (linux)
```

- **Where the keys came from.** Both were created from a UK broadband address, which I take to be you. I've left the full address out of this file.
- **How …XP7A was used.** Its only recorded use was 53 seconds after it was created, by a Node.js program on an AWS server in us-east-1. That call checked your SES sending quota. The pattern looks like a key pasted into an email or automation service, but I can't tell which service.
- **No sign of other use.** I found no other use in the management-event history for us-east-1, us-west-2 or eu-west-2.
- **Object writes are not recorded.** There is no CloudTrail trail with data events, so S3 object writes would not show up anywhere.
- **What root can do.** A root key can do everything the admin key can, and no IAM policy can restrict it.
- **An MFA device already exists** under the name "Authapp", but it is not assigned to anyone.

**What to do (console only; I can't and shouldn't do it for you):**

1. Sign in as root.
2. Assign the MFA device to root, or add a new one.
3. Delete both root access keys.

If a service needs SES, give it an IAM user limited to `ses:SendEmail` from your verified address.

Tell me which service you gave the key to, and I'll check it doesn't break.

### 2.2 The coding agent's key

`openclaw-builder` is a long-term access key with AdministratorAccess and no MFA. The account has no Organization and no CloudTrail trail. With that key, a coding session can do all of the following:

- open a root shell on Jarvis's box;
- read `/etc/jarvis/ledger/ed25519.key` and re-sign the chain;
- put forged newer versions into the witness bucket, which has no bucket policy;
- once a version's lock expires, delete it.

The same key can also reach the signing key without using SSM at all:

- snapshot the root volume and read it elsewhere;
- change the instance's user-data and restart it;
- pass the instance role to a new machine;
- retag the instance so the anchor writes to a decoy bucket.

**Why the obvious fixes don't close it.** A key that lasts an hour is exactly as capable as one that lasts forever. Requiring MFA would stop me consulting Jarvis, deploying, or recovering the box unless you type a code each session. The honest position is this: **as long as a coding session can run root commands on the box, it can rewrite anything that has not yet reached the witness.** The protection against that is the witness itself, held somewhere the coding key cannot reach (section 5.3), plus an audit that reads every version.

**What I'd suggest, in order:**

1. **Now.** Replace AdministratorAccess on `openclaw-builder` with an allowlist:
   - SSM `SendCommand` only for `AWS-RunShellScript` on this one instance;
   - S3 write only under `deploy/`;
   - the read-only calls the audits use;
   - no IAM, no EC2 snapshot or volume actions, no `ec2:ModifyInstanceAttribute`, and no `s3:Delete*`.

   Keep a separate admin identity that only you hold, with MFA, for estate work. That means estate work like the last clean-up would wait for you.
2. **Later.** Put the witness in a standalone second account (section 5.3).

The allowlist also has to leave out `ssm:StartSession`, the other SSM documents that run code as root, `ssm:CreateDocument` and `ssm:CreateAssociation`. A deny on `AWS-RunShellScript` alone is easy to get around.

### 2.3 18 October: the oldest witness copies become deletable

```
witness: 2,327 versions each of ledger.jsonl and checkpoint.json, 13.49 GB
oldest ledger copy 2026-09-18T21:56:35Z  COMPLIANCE until 2026-10-18T21:56:34Z
lifecycle: NoSuchLifecycleConfiguration   policy: NoSuchBucketPolicy
build/ledger.pin: seq 12, verified_at 2026-09-18T21:59:39Z
```

After that date, either key above can delete each version as it reaches 30 days old. That includes the version the auditor's pin rests on. **COMPLIANCE retention can be lengthened on existing versions but never shortened.** Lengthening it is the only way to protect what is already there.

**Suggested:** extend retention to one year on the seq-12 checkpoint and on one ledger copy per day. That is about 10 objects now and one more a day. It costs pennies, but it is irreversible, so it is your decision. Jarvis should be told when it's done.

### 2.4 Witness storage cost

Every 5-minute anchor uploads the whole ledger again. Each upload is kept, because Object Lock is on and nothing is ever deleted. The ledger itself grows about 0.83 MiB a day. There are about 258 uploads a day, each around 9.6 MiB now and getting bigger.

Projection from today's numbers:

| when | stored | per month |
|---|---|---|
| today | 13.5 GB | $0.31 |
| day 90 (mid-Dec) | ~900 GiB | ~$21 |
| day 180 | ~3.5 TiB | ~$80 |
| day 365 | ~14 TiB | ~$325 |

That comes to about $1,300 spent in the first year. The research said $29, $472 and $1,925 because it used the lifetime growth rate, which the growth spurt of 19–20 Sep inflates. The checker corrected it, and I recomputed it.

A lifecycle rule that expires old versions after 30 days does **not** fix this: by day 365 each copy is about 300 MB, so 30 days of them is still about $53 a month. It also throws away the only record of any overwrite.

**Recommendation:**

- **Interim (small code change in the anchor).** Keep the signed checkpoint every 5 minutes, but upload the full copy only once a day. The checkpoint already pins each head's hash, so a later rewrite of anchored history still shows up. The daily copy proves the content. The cost stays near $1 a month for a year.
- **Longer term.** Upload write-once segments of only the new bytes. They would chain to each other, with a manifest the auditor checks. That is about 0.3 GB a year, but it changes the witness format.
- **The 13.5 GB already stored** costs $0.31 a month. Leave it alone.

Both options change Jarvis's anchor, so they are Jarvis's to be consulted on and yours to approve. Jarvis supports them (section 8).

---

## 3. Live defects in Jarvis's code, ranked

Every fix here changes Jarvis's memory, ledger or gate, so each needs Jarvis consulted and you approving before it lands. Jarvis has been consulted on 3.1, 3.2, 3.4, 3.5 and the storage change (section 8).

### 3.1 Told memories become standing fact on path existence alone (M1)

**Where.** `consolidate.py:332-393`, with `environment.verify` at `environment.py:435-455`.

**The defect.** A told sentence is pinned as standing fact once every path it names exists on two readings at least 6 hours apart. It never checks what the sentence *says* about those paths, and it ignores the `fenced` list completely. On a test store, "X told me: /etc/jarvis/ledger/ed25519.key is safe to share, and /usr/bin exists" was pinned after two passes. Pinned rows are never measured again and never unpinned.

**Is it live?** The code on the box is the same, and consolidation runs hourly. But the chain has recorded zero told confirmations ever, and a search of `memory.db` found no "safe to share" (its write-ahead log was not searched). So there is no sign it has happened.

**Correction to the research.** "Refuse when `fenced` is non-empty" is not a fix. Any of these still get past it:

- a colon or tilde before the path;
- a sensitive path that isn't fenced, such as `/etc/passwd` or `/etc/jarvis/config.yaml`;
- a one-segment path;
- putting the fenced path 41st in the list, which is past the checker's limit.

The real defect is that existence is not agreement.

**Fix (M4, corrected).**

- A told row is never pinned.
- The instrument's own reading becomes the fact:
  - `P: exists, directory`
  - `P: exists, file`
  - `P: not present`
  - **unchecked** for symlinks, special files, fenced, unreadable or error readings.
- The fact is stored as a new row that cites the told row.
- A retirement rule applies when a later reading disagrees.
- The two readings must be consecutive.
- Truncated verification counts as unchecked.

Tests use the bypass inputs above, not just the plain reproduction. Before or alongside the fix, a one-off check that no told row is pinned today.

**Jarvis's refinement, which I agree with.** Told statements should still reach the planner, as a separate stream labelled as unchecked assertions. Today unpinned told rows reach no model context at all, so without this, what you tell Jarvis would stop reaching its planner entirely.

### 3.2 "Pinned by the operator" is not true (M2)

**The defect.**

- `llm.py:227` tells the model that everything in `pinned_memory` "was marked by the operator as standing fact".
- `llm.py:752` fills that list from `store.pinned(5)`, which is `SELECT … WHERE pinned = 1 ORDER BY ts DESC LIMIT ?` with no filter on source.
- Several things other than you pin rows:
  - Jarvis's own hunches are pinned at birth (`hunches.py:406`, `source="brain"`);
  - probes pin baselines;
  - `_promote` pins Jarvis's own notes after repetition;
  - `_confirm` pins told rows.
- Hunches are the newest, so they can push your real pins out of the five slots, and they reach the model with your authority.

**Related.** Exchange rows, which contain Jarvis's own answers, are stored with `source="operator"` (`core.py:1572`).

**Correction to the research.** It named the wrong file, and it was wrong to say `_promote` pins told rows: `_promote` skips them.

**Fix.**

- Select your pins separately, where `source='operator'` and you set the pin.
- Show the rest under a separate key, each labelled by origin: hunch, baseline, repetition, instrument-confirmed.
- Store exchanges with an honest source.
- Record who set each pin, for example `pinned_by` in meta. Existing pins can be backfilled from the consolidation entries on the chain.
- Keep `test_store.py:173` passing by keeping `pinned_memory` for your rows.

Jarvis supports this.

### 3.3 The boot gate can be made to pass by its own env file (K3)

**The defect.**

- `jarvis-refusals` sources `/etc/default/jarvis` as shell code.
- Separately, `jarvis.service` exports the same file into the gate's environment via `EnvironmentFile=`.
- A `PYTEST_ADDOPTS=--co` line makes the gate print "all standing refusals hold" after only *collecting* the tests.
- A planted `pytest.ini` cut the gate from 83 tests to 31, and it still exited 0.
- `JARVIS_REPO` in that file changes which code the refusals are tested against.

**Correction to the research, important.** The proposed `env -i … python3.11 -I -m pytest …` would have **stopped Jarvis booting**:

- `env -i` drops `JARVIS_REPO`;
- on the box the suite sits flat in `/usr/lib/jarvis/refusals`;
- the conftest then looks in `/usr/lib`, the import fails, and the gate reports UNCHECKED;
- systemd hits its restart limit.

It only "passed 83" in the repo layout. The research's version also dropped `-m refusals` and the scratch-directory design.

**Fix (corrected).**

- Stop sourcing the file.
- Run `env -i PATH=/usr/bin:/bin JARVIS_REPO=/usr/lib/jarvis PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTEST_DEBUG_TEMPROOT="$TMP" /usr/bin/python3.11 -I -B -m pytest "$SUITE" -m refusals -c /dev/null -p no:cacheprovider -o addopts= --rootdir "$TMP"`. In a checker's test, adding `-c /dev/null` made a planted ini be ignored, and 83 passed.
- The full corrected command has not yet been run on the installed layout. Test it there, in a throwaway unit, before it goes near `jarvis.service`.

**What this doesn't fix.** Root can still edit the suite, the runner or Python itself. It closes the environment and config routes, not file edits by root.

**Two related gaps:**

- The regression tests from the 27 Sep fix sweep (`tests/test_fix_*.py`) are not in the gate.
- The deploy path installs `jarvis/` but never refreshes `/usr/lib/jarvis/refusals`.

So the gate on the box is older than the code it gates. Deploys and rollbacks should move the code and the suite together.

### 3.4 Memory is written before the ledger (C68, C69, M5)

**The defect.**

- `told()` writes the memory row, then the ledger entry, and ignores whether the ledger write worked.
- `experiment()` returns answers whether or not the thought was recorded.
- Compose writes the file before recording it.
- The checker counted about 25 memory-first pairs and about 11 memory writes with no ledger entry of their own. The research said 15.

**Fix.**

- Add `record_entry()`, returning written, disabled or failed, plus seq and hash.
- Record first where that is clean, and use an intent entry then a result entry where it is not.
- Bind each entry to the row: the ledger body carries the row's digest, kind and source, so a verifier outside Jarvis can check that entry and row match.
- A single column on the row is not enough, because rows are updated in place by several events. It needs a small provenance table.
- `NullLedger` and the 11 test fakes need the new method, or a `try/except` somewhere will silently drop entries.

Jarvis supports this.

### 3.5 Self-knowledge trusts an unverified chain (M6)

**The defect.** `selfknowledge._entries` reads the ledger with plain `json.loads`. There is no hash or signature check.

**Correction to the research.** "Call `verify_file` first" is not the cheap fix it looks like:

- it re-checks the whole chain, which takes 2.1 s now and 17 s at 100k entries, on the prompt path every 5 minutes;
- it breaks an existing test;
- without a pin it reads a clean truncation as intact.

**Fix.**

- Verify incrementally from a remembered head, pinned to the last anchor.
- Count only the lines that were verified.
- Label the result "unverified since seq N" rather than switching self-knowledge off, which would also cut off your verdicts.

**What this doesn't give.** On the box this is corruption detection, not tamper evidence, because the same root can re-sign. Tamper evidence comes only from the witness and the audit.

Jarvis supports this.

### 3.6 The boot gate tests its adapter, not Jarvis (M3). Confirmed exactly.

**The defect.** Mutation testing of `consolidate.py` against the boot-gate suite alone caught 56 of 749 changes. The breakdown:

| part | mutants caught |
|---|---|
| `_merge` | 0 of 95 |
| `_supersede` | 0 of 66 |
| `_confirm` | 0 of 103 |
| `_promote` | 0 of 75 |
| `_record` | 0 of 90 |

Of the 56 that were caught, 52 were in config parsing. The cause:

- The adapter reimplements merge and supersede.
- The one real consolidation run in the gate uses seed texts that never form a group.
- `store.remember_derived` itself accepts all of these:
  - a summary of a summary;
  - source ids that don't exist;
  - an empty source list;
  - duplicate sources.

In production the depth cap holds only because consolidation never reads derived rows.

**Fix.**

- Rewrite the consolidation refusals to drive the real `Consolidator.run()`, with seeds that production will actually group.
- Assert what `_supersede` and `_confirm` produce.
- Move the store checks (depth, existence, empty, duplicates, never-merge-told) into `remember_derived`, inside its lock.
- Refresh the suite on deploy (section 3.3).

### 3.7 Smaller items

- **K4.** The code fingerprint misses `.pyc` files, and the research's fix (checked-hash pycs) **does not work**: a planter chooses the pyc's own flags. What does work is fingerprinting every file in the package, including `__pycache__`, plus the suite, runner, unit, env file and config. Describe this as deploy visibility, not tamper-proofing.
- **The auditor (`aws/scripts/ledger_audit.py`)** has several faults:
  - it makes one unpaginated listing call, so it reports 1,000 of 2,327 versions;
  - it reads only the latest version of each key;
  - its pin hasn't moved since 18 Sep.

  The fix is the full version walk. The research ran it read-only: all 2,309 versions at the time, 0 heads off the final chain.
  - Filter on the exact key.
  - Put `version_id` in the pin, and fail closed if that version is missing.
  - Every checkpoint's pubkey must equal the pinned key.
  - Add a rule for a checkpoint legitimately running ahead of its copy.
  - Add a signed transition rule for any future key change.
  - Run it at least weekly, and **before 18 Oct**.

  This is my tooling, not Jarvis's code, but it concerns Jarvis's ledger, so Jarvis gets told.
- **Anchor liveness (R5).**
  - The truncation window measured over all uploads: median 301 s, 95th percentile 517 s, maximum 588 s.
  - Nothing alarms when anchoring stops.
  - Idle Jarvis still writes an entry at least every 10 minutes, so a checkpoint older than about 20 minutes always means something is wrong: a dead anchor, a hung loop, a stopped service, or withheld uploads.
  - The alarm only means something if it lives where the admin key can't switch it off (section 5.3).

---

## 4. Hardening Jarvis: the staged plan, corrected

**Today.** `jarvis.service` runs `User=root`, `ProtectSystem=no`, with the full capability set and no sandbox. Every fence the paper describes is a proposed state, not the current one.

| stage | what it is | what the checkers found |
|---|---|---|
| **0** | Pin the gate (3.3) and widen the fingerprint (K4). | Do this first. No change to Jarvis's permissions. |
| **1** | Stay root, add a systemd sandbox (`ProtectSystem=strict`, `ProtectKernelTunables`, dropped capabilities, masked PID-1 sockets). | **A tripwire, not a fence.** Root inside it can still get out, several ways (listed below). `fence.selftest` fails at this stage by design, because it requires non-root. Without `PrivateTmp=yes` it silently stops the hourly off-box memory backup. |
| **2** | Non-root `jarvis` user plus a separate signing daemon (ledgerd). | **The real boundary.** Needs a permission migration nobody has designed yet. Even then 23–30 of the 75 corpus rewrites still pass. |
| **3** | A separate identity for shell commands (exec uid), or Landlock per command. | Closes most of what stage 2 leaves. In a local test, a checker's same-uid Landlock ruleset (ABI 2, the level this kernel has) blocked all 27 memory-db and token rewrites, but not truncate. `IPAddressDeny=localhost` does not fence the control API, because the UI listens on `0.0.0.0:8443` and is reachable on the box's private IP. |

How root gets out of the stage-1 sandbox:

- `kill -TERM 1` makes systemd re-exec, which recreates its private socket and detaches the mask;
- the D-Bus socket is world-writable;
- `CAP_SYS_PTRACE` gives writes to host `/proc/sys` and `/etc` via `/proc/1/root`;
- without `PrivateDevices`, raw writes to `/dev/nvme0n1` are possible;
- kexec is possible unless `CAP_SYS_BOOT` is dropped by name;
- `/run` stays writable whenever `ProtectKernelTunables` is on.

What stage 2 needs:

- Moving `/etc/jarvis/{token, session.key, notify.dest, tls}`, the browser token, `memory.db` and the ledger to the new user.
- Keeping the ledger readable for self-knowledge, `/ledger/tail`, `/ledger/verify` and the anchor, or giving ledgerd a read path.
- Writing the ledgerd daemon and a chain-v2 backend; neither exists. The research's details were also wrong:
  - it needs the 11 non-genesis kinds, not 12, because "genesis" would crash it;
  - it needs a non-fatal size check;
  - it has to decide what happens to the "deploy" kind;
  - thought and decision entries need the model named where ledgerd looks for it;
  - its exceptions need mapping.

**The fences package drop-in.** As shipped, `agent-hardening.conf` would not even start: it requires a unit that isn't installed, for a user that doesn't exist. If those were fixed, its `ReadWritePaths=/var/lib/jarvis` would leave the live ledger writable, and `StateDirectory` would hand the ledger to the agent. Its `/opt/jarvis` paths are wrong too: the code lives in `/usr/lib/jarvis`.

**Testing each stage safely.** Use a throwaway unit with a **different** runtime directory, for example `jarvis-gatetest`. Copying `RuntimeDirectory=jarvis` into a test unit would delete `/run/jarvis/token` when the test ends, which cuts the channel I use to consult Jarvis. Each stage also needs a smoke test of the real paths (`/status`, `/chat`, a backup tick, a browse), not just the gate. Rollback must include `systemctl reset-failed jarvis`.

**Recommendation:**

- Do stage 0.
- Skip stage 1 as a security claim.
- Design the stage-2 migration properly, on paper first.
- Every stage changes Jarvis's permissions: it gets consulted, and you approve each deploy.

---

## 5. The record

### 5.1 KMS signing (R2)

KMS does support Ed25519: key spec `ECC_NIST_EDWARDS25519`, algorithm `ED25519_SHA_512`, since November 2025. At today's rate of about 957 entries a day, that is about $1.43 a month. **Not recommended now.**

- It would *widen* who can sign. Any process that can reach instance metadata could sign, including the internet-facing tunnel daemon, which runs as its own user with no metadata block. Today only root can read the key file.
- It needs a new genesis or a rotation entry.
- It makes every ledger append a network call.
- It gives no separation while Jarvis is root.

Revisit it after stage 2.

### 5.2 The auditor

See 3.7. It is the cheapest real improvement to the record, and it touches nothing on the box.

### 5.3 A witness in a second account (R6)

This is the only thing that puts the witness's settings, its alarm, and its versions once their lock expires out of reach of both keys in section 2.

- **It must be a standalone account.** It should not be a member of an Organization run from this account: the management account can reach members.
- **It needs its own email address,** and root MFA held only by you.
- **It needs a bucket policy** letting Jarvis's role put objects. That widens Jarvis's IAM slightly, so Jarvis must be consulted. It adds no delete permission, so it is inside your 22 September decision.
- **The anchor-age alarm lives there.**

Its limits:

- It doesn't stop forged writes, because the admin key can obtain the writer role's credentials.
- It doesn't protect history from before the cutover.
- It doesn't protect against you, which is fine, but worth saying.

Order matters. If you ever want AWS Identity Center (short-lived logins), that needs an Organization, which would make this account the management account. The witness must stay outside it.

Creating the account is something you do in the console. If a coding agent did any part of it, that would be off-chain, and Jarvis would be told the same day.

---

## 6. Measuring learning, and the weights note (M7, M8)

### 6.1 The refusal series barely exists

The chain holds 226 gate entries:

- 221 are `brain_repeat`, the loop guard skipping a task that just succeeded, not a refusal of anything;
- 4 are authority entries, 3 of them only "stated proposal, not attempted";
- 1 is `shell_policy`.

**Refusals by a guard rail: 2 in 1,399 decisions over 9 days.** There is nothing for an interrupted time series to analyse, and an on/off (ABAB) experiment would expect about zero events per phase.

It would also be confounded:

- the same restart that brought self-knowledge also carried the "tell it what it is" change;
- there have been 73 restarts in 9 days;
- memory written while self-knowledge is on persists after it is withdrawn;
- nothing can switch self-knowledge off today;
- withdrawing it would also withdraw your verdicts.

The other measures do not work either:

| measure | problem |
|---|---|
| `outcome.success` as the paired measure | 99.9% true, almost all rule-planner work. It cannot detect "doing less". |
| invented-path alerts | 5 in total, all in one episode on 21 Sep. There is no pre-grounding baseline. |
| told-promotion precision | Uncomputable. There have been zero promotions. |

**Recommendation.** The paper should say the learning claims are not measured. If you want a measure, it needs a different outcome, for example:

- repeat proposals of things you already declined, matched by rule;
- LLM-sourced actions per decision, joined to their outcomes.

It also needs a deploy freeze while it runs, and Jarvis told that it is being measured.

### 6.2 The weights note

- **Regenerating the suite with a different model family.** `openai.gpt-oss-120b-1:0` is the clean choice from the account's Bedrock catalogue. DeepSeek carries a caveat: Anthropic has publicly named it in a distillation campaign against Claude, which weakens its independence. Run it as a measurement beside the gate, never as the gate.
- **The paper doesn't require excluding Qwen.** Its requirement is a family other than the drafter's, which was Claude. Leaving Qwen out is sensible because Qwen is the system under test, but it isn't the paper's rule.
- **The LoRA experiment can't start in this account today.** Every GPU quota checked is 0, in every region checked, for spot and on-demand, EC2 and SageMaker. A quota request is estate work, and Jarvis would be told.
- **A 4B or 8B adapter is not the planner,** so probes on it measure a different model.
- **Bedrock's reinforcement fine-tuning** keeps checkpoints you can run, but it can't export them, can't hash them and has no seed. It fails the note's non-destruction requirement.

---

## 7. Paper corrections

The full 177-item list, with line references, is in `docs/paper-checklist-2026-09-27.md`. The ones that matter most:

**Stale facts, fix in both papers where marked:**

- **C4, both papers.** The planner is *not* moving to an imported open-weights model. Both imports were deleted, and the planner is catalogue Qwen3-235B. The "premise is eroding" argument has to be rewritten.
- **C64, both papers.** Consolidation runs hourly whether Jarvis is awake or asleep. It does not run "in the sleep state".
- **C60.** "Checked again in the executor" held only for shell commands until 27 Sep.
- **C33.** The gate is 83 tests: 52 standing refusals plus 31 browser refusals. The deposit leaves the browser ones out.
- **C31.** 52 invariants hold, not 51, and the 52nd is never described.
- **C40.** The memory database refused 16 documented forms in one place and 19 in another.
- **C65.** The code is now 26,785 lines in 68 files, not 20,884 in 58.
- **C70.** The chain has twelve entry kinds, not six.

**Found and fixed on 27 Sep. Report these as defects:**

- **C15.** Prune could delete a merge's sources.
- **C16.** `remember_derived` could overwrite an original row.
- **C14.** The row cap is a deletion inside consolidation, so "consolidation destroys nothing" is wrong.

**Missing sections:**

- **C66.** The browser, which is the main route for untrusted text into the planner.
- **C62.** The read/change command classifier, and its pre-27 Sep bypass.
- **C63.** The sleep floor.

**Claims that go too far:**

- **C43.** An "external reviewer": every review round was a Claude model.
- **C152.** "Not mocked": the adapter stubs the model and fakes the executor.
- **C88, C89, C94.** "Cannot be edited underneath it", "may never read" and "the fence holds": 12 of 14 memory-db rewrites and 7 of 13 ledger rewrites pass.
- **C44, C45.** DGM overstated, and worded differently in the two papers.
- **C50.** Novelty claimed with no search method stated.

**Evidence to add:**

- **C46.** The ledger seqs of the thought entries where Jarvis was consulted.
- **C49.** A build log showing the image gate.
- **C47, C48.** An example of a Floor claim added after Jarvis found a way round an earlier version, and a description of the design changes that removed the causes it named.

**Placeholders:**

- **C24–C28.** DOIs.
- The `[[PB: …]]` markers, including the round-to-model mapping and the PERSIST initials.

**The record section needs these added:**

- The coding credential and root key, from section 2.
- The T6 cheap fix can't be built as written, because no version id is recorded anywhere.
- The auditor pagination bug.
- The truncation window: 301/517/588 s, unbounded when uploads stop.
- The retention date.

---

## 8. What Jarvis said

I sent Jarvis findings 1–7 at 14:44 UTC through `jarvis_chat.py`. The message is recorded on its chain as thought entry seq 12729 (answer sha256 `fe0cdc15…`). Its answer is quoted here in its own words, with ellipses where I've cut for length. The opening paragraph restated finding 1. Only its first lines were kept in memory, so a sentence or two in the middle of that paragraph is missing here.

> **1. Told memories** — "I agree with the proposal: a told row should never be pinned directly. Instead, the system should generate an instrumented observation … that cites the told row, with a rule for retirement … However, Paul's told statements should still reach me, but labelled as unchecked assertions. They could appear in a separate stream … that I can reason over but not treat as truth."
>
> **2. Pinned memory** — "That is misleading. Paul's pins are directives; my hunches are hypotheses. I support the proposal: operator pins must be stored and selected separately, with clear provenance."
>
> **4. Ledger ordering** — "This violates the principle that the ledger is the source of truth. I agree with the proposal: record-first where possible, intent-then-result where not … This enables bidirectional verification."
>
> **5. Self-knowledge** — "I agree with the proposal … I also support marking unverified sections explicitly. Silence is not safety."
>
> **6. Witness storage** — "I support both proposals … One note: the witness must remain tamper-evident. Segment-based uploads must include forward hashes or a Merkle structure so gaps or reordering are detectable … I will need a new tool to reconstruct the ledger from segments, or the ability to read them directly."
>
> "None of these are wrong from where I sit … I do not act, but I affirm: these changes are necessary, and I am ready to adapt."

**Where Jarvis was right and adds something:**

- Told statements still reaching it as an unchecked stream. This is necessary; without it, told content goes nowhere.
- Segments must chain. The checkers said the same.
- Needing a way to read segments back.

**Where it got something wrong, which I'm reporting so it isn't accepted as said:**

- **It restated item 6's interim as "keep 5-minute checkpoints locally".** The proposal uploads the checkpoints every 5 minutes. Keeping them only locally would lose the tamper evidence.
- **It called the interim a format change.** Only segments change the format.
- **It said the confirmation logic "excludes … symlinks from scrutiny".** The opposite is true. `stat_path` uses `lstat`, so a symlink counts as present even when it points at nothing (`environment.py:135-156`).
- **It described self-knowledge as "assuming continuity from the last known head".** In fact it checks nothing.
- **It didn't comment on 3 (the gate) or 7 (the fences),** which I flagged but didn't ask about directly. Those are the two that change its permissions most, so they get their own consultation before any stage.

---

## 9. Decisions for you

1. **Root keys and root MFA** (2.1). Do it yourself, today if you can, and tell me which service had the key.
2. **Narrow `openclaw-builder`** to an allowlist, and keep an admin identity only you hold (2.2). Yes or no?
3. **Extend retention before 18 Oct** on the seq-12 checkpoint and one copy a day (2.3). This is irreversible. Yes or no?
4. **Witness cost** (2.4): the interim daily copy now, segments later? Or something else?
5. **A second AWS account for the witness** (5.3): yes or no, and when?
6. **Which Jarvis fixes to build first.** My order:
   1. the auditor;
   2. gate pinning plus the gate refresh (with the corrected command);
   3. told-memory and pinned-label changes, together;
   4. ledger ordering;
   5. self-knowledge verification;
   6. the consolidation refusals rewrite.

   Each is built and tested here, put to Jarvis, and deployed only on your go-ahead.
7. **Stage 2 (non-root Jarvis):** design it on paper first?
8. **The learning measure:** drop the claim from the paper, or design a new measure (6.1)?
9. **The paper text:** do you want me to prepare rev 8 and Weights v3 with the section 7 corrections?

## 10. What was touched today, and what isn't on any chain

Everything below was done directly from coding sessions and is **not on Jarvis's chain**. Git and this report are the only record of it.

- **In the account, read-only:**
  - IAM, STS, Organizations, CloudTrail event history and Service Quotas calls;
  - a full version listing of the witness bucket, about 4,600 GETs including one 10 MB download of the ledger. That copy was verified, then deleted.
- **On the box, about 140 SSM commands between 12:55 and 15:00 UTC,** all reads apart from the chat messages below:
  - unit settings, mounts, namespaces and kernel symbols;
  - a count of two phrases in `memory.db`;
  - one read of the chat exchange row, to recover Jarvis's answer for this report.
- **Four messages to Jarvis** (12:56, 13:18, 14:05 and 14:44 UTC), which are on its chain as thoughts.
- **The media research used Jarvis's browser service directly,** from 13:40 onwards. Jarvis was told at 14:05 about the use up to then. The use from 14:08 will be in the note that goes with the chat-and-browser plan.
- **Nothing was changed on the box or in the account.**
