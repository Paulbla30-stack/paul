# What is yours to do, 23 September 2026

Everything that could be deployed from this session has been. This is the
remainder: the things that need your hands, your account console, or your
ruling, with the reason each one is yours rather than mine.

Ordered by what actually blocks something.

---

## 1. Bedrock will not run an Anthropic model until you submit the use case form

**Blocking. This is the one that matters today.**

This is not a permissions problem in the role, and it is not a region problem.
I tested it directly rather than reasoning about it:

```
us.anthropic.claude-sonnet-4-5-20250929-v1:0
  -> ResourceNotFoundException: Model use case details have not been
     submitted for this account. Fill out the Anthropic use case details
     form before ...

us.anthropic.claude-3-haiku-20240307-v1:0
  -> ResourceNotFoundException: Access denied. This Model is marked by
     provider as Legacy and you have not been actively using the model ...
```

Twelve Anthropic models list as ACTIVE in us-west-2 and sixteen inference
profiles exist. Listing is not access. The account has never submitted the
form, so every one of them refuses at invoke time.

**What to do:** AWS console, region **us-west-2** (Oregon) → Bedrock → Model
access → Anthropic. There is a use case details form; fill it in once and it
applies to the whole Anthropic catalogue for the account. It is a short form
about what you are building. Approval is usually immediate or same-day.

**What it unblocks:** the scout is drafting on
`qwen.qwen3-235b-a22b-2507-v1:0` via converse, because that is what the account
can actually reach. The planner is on catalog Qwen3-32B for the same reason.
Neither is a considered choice about quality; both are what was available.

**And the thing you were actually asking about.** You said you were thinking of
putting an Anthropic key into another agent on the platform, and then thought
better of it — Jarvis is the OS, not a program sitting on top of one. That
instinct is right and this is how you act on it: the form puts the good models
*behind Jarvis's own IAM role*, where the vigil gates them, the ledger records
them and the permission spine applies. A raw key in a second agent gets you the
same model with none of that, and a second thing that can act without a record.
Same capability, no spine.

---

## 2. One Terraform import, because I created a policy by hand

I added the inline policy `jarvis-scout-read` to the instance role directly, so
the Marketing tab could read the scout's proposals. It is declared in
`aws/terraform/main.tf:211` under the same name, so the next `terraform apply`
will try to create something that already exists and fail.

```
cd aws/terraform
terraform import 'aws_iam_role_policy.scout_read[0]' \
  jarvis-0cebbd6512f7ecc9f8838558a3:jarvis-scout-read
terraform plan        # expect: no changes to scout_read
```

The `[0]` is because the resource has `count = var.scout_table == "" ? 0 : 1`.
`var.scout_table` defaults to `jarvis-scout`, which is the live table, so the
count is 1 and the index is 0.

If `plan` shows a diff after the import, tell me what it says rather than
applying it — it would mean the hand-made policy and the declared one differ,
and I would rather find out which of the two is wrong than let apply pick.

---

## 3. The boot gate is on, so know how to get past it

`jarvis.service` now runs `/usr/local/bin/jarvis-refusals` as `ExecStartPre`.
The 51 standing refusals run against the code that is about to start, and the
agent does not start unless they all hold. It logged
`[refusals] all standing refusals hold` on the last boot and the unit is
`active`.

Two ways it stops a boot, and they mean different things:

| journal line | meaning | what to do |
|---|---|---|
| `REFUSED` | the suite ran, an invariant does not hold | the code is wrong; do not bypass |
| `UNCHECKED` | the suite could not run at all | the runner is broken; fix the runner |

Jarvis argued that `UNCHECKED` should fail open with an alert, because a full
disk is not its fault. I did not take that, and told it why: if a missing
pytest means "start anyway", then deleting one package is the entire bypass.
That reasoning is written into the top of the script itself.

**To start it without the gate** — a decision, not a workaround:

```
systemctl edit jarvis      # add an empty  ExecStartPre=   to clear it
systemctl start jarvis
```

The pre-gate unit is also kept at `/etc/systemd/system/jarvis.service.nogate`
if you would rather swap the file back.

---

## 4. The Cloudflare tunnel token from 18 September is still unrotated

`cloudflared` is running on the box (pid 46791, listening on 127.0.0.1:20241).
I cannot see from here whether the token was rotated, so I am not claiming it
was not — only that I have no evidence it was, and it was exposed five days
ago.

Cloudflare dashboard → Zero Trust → Networks → Tunnels → the tunnel → refresh
the token, then update `/hbf/*` in SSM Parameter Store and restart
`cloudflared`. I do not have Cloudflare access, so this one is entirely yours.

Whatever you do, do not paste the token into this chat, and do not run it
through a shell with `set -x` on. That is exactly how the last one leaked.

---

## 5. SES is still in the sandbox — probably fine, but know the edge

Verified this morning:

```
production access: False    enforcement: HEALTHY

heartbeat-framework.org                      DOMAIN          verified
paulblatherwick@heartbeat-framework.org      EMAIL_ADDRESS   verification FAILED
Paulbla30@hotmail.com                        EMAIL_ADDRESS   verified
```

The digest reaches you — you confirmed that yesterday — because the **domain**
identity is verified, and in the sandbox a verified domain covers every
recipient at that domain. The individual address identity says FAILED because
the confirmation link was never clicked; it is dangling and does nothing. You
can delete it in the SES console or ignore it. It is not what is delivering
your mail.

**The edge:** in the sandbox the scout can only ever mail a verified address.
The moment you want it to reply to somebody, or mail anyone who is not you,
that request silently fails. If that is on the roadmap, request production
access now (SES console → Account dashboard → Request production access) —
approval takes a day or two, so asking after you need it is the wrong order.
If the scout is only ever going to mail you, leave it in the sandbox; the
sandbox is a safety rail here, not an obstacle.

---

## 6. There is no rollback image, and taking one costs money

The estate is one AMI and one snapshot, and they are the same thing:

```
ami-081cc1cf288f89af4   jarvis-agent-al2023-x86_64-20260922-141955   2026-09-22
snap-09e98d5adbf093372  8 GiB                                        2026-09-22
```

That is the image the box was built *from*. There is no image of the box as it
stands now — with the boot gate, the documents endpoint, the Marketing tab and
everything else from the last two days. If the root volume goes, the recovery
path is a rebuild from source, not a restore.

Taking one is a `CreateImage` call, an 8 GiB snapshot, and about $0.40 a month
in snapshot storage. **It is a billed resource, so it is your call and I have
not done it.** Say the word and it takes a minute.

Worth saying plainly: I do not know who deleted the previous images. It was
not me and I have no record of it.

---

## 7. Jarvis has been stuck on your gas and electricity bills, and it is not its fault

I found this while telling it what had landed. It raised it unprompted, worked
out on its own that the reads were futile, and was right.

**What you saw:** you uploaded a British Gas bill and an Octopus bill and asked
it to read them. Nothing useful came back.

**What is actually happening.** Three goals are live, at priority 5, above
every standing goal it has:

```
5 once  Read the uploaded file /var/lib/jarvis/uploads/british-gas.txt and report what is in it
5 once  Read /var/lib/jarvis/uploads/british-gas.txt again and say what dates are in it
5 once  Read the newly uploaded octopus.txt and report what is in it
```

`/var/lib/jarvis/uploads` is empty. Nothing named british-gas or octopus exists
anywhere under `/var/lib/jarvis`. And the journal shows exactly what that costs:

```
10:28:20  Brain chose the task that just succeeded again (Check if
          /var/lib/jarvis/uploads/octopus.txt exists); idling and leaving
          the next 1 cycle(s) to the rule planner
10:28:47  ... the next 2 cycle(s) ...
10:31:31  ... the next 4 cycle(s) ...
10:35:07  ... the next 8 cycle(s) ...
```

The repeat-suppression is working — it backs off rather than spinning. But the
goal never retires, so it comes back, every boot, for ever.

**Why the files are gone, and it is not an upload bug.** I tested the upload
path directly: a 77-byte probe to `https://127.0.0.1:8443/upload` returned
HTTP 200 and the file landed and was recorded. The mechanism is fine. (I
removed the probe afterwards.)

The cause is a rebuild. The snapshot on the account was made from instance
**i-091c77c6079228ca2**; this box is **i-016f9f37fe6ca2ba8**. `memory_backup`
carries `memory.db` — which is where runtime goals live — to the new box. It
does not carry `/var/lib/jarvis/uploads`. So your instruction survived the
rebuild and the document it was about did not, and nothing anywhere said so.

**What I have built, and what I have not.** Goals restored at start now get
their named paths checked. If a path is not there, it logs a warning naming
the path and writes one note into the deque the model reads every cycle. Eighteen
new tests, including one whose whole job is to fail if a future edit makes
this retire anything.

Live on the box as of 10:43 this morning, from the journal at boot:

```
Restored 3 operator goal(s) from durable memory
WARNING  Goal names a path that is not on this machine:
         /var/lib/jarvis/uploads/british-gas.txt
WARNING  Goal names a path that is not on this machine:
         /var/lib/jarvis/uploads/british-gas.txt
```

Two of the three, not three. The third says *"the newly uploaded
octopus.txt"* — a bare filename, not a path — and a bare filename is not
something this can check without guessing which directory you meant. That is
the right call rather than a gap, but you should know it is two out of three
and not assume the check is exhaustive.

It does **not** retire the goals. I asked Jarvis what the rule should be and it
argued against automatic retirement, in terms worth quoting:

> *absence today is not absence forever, and unilaterally discarding operator
> intent risks eroding trust* ... *when such a goal persists beyond a
> reasonable threshold it should trigger a proposal to retire it, citing the
> discrepancy. That keeps agency with the operator while acknowledging
> reality.*

It then filed exactly that proposal, for the three goals above. **It is waiting
for your ruling.** Approve it and they go; decline and they stay and you will
at least now be told why they cannot be met.

I also asked whether the better fix was to back the uploads up instead. Both of
us came down against: it costs storage, widens what leaves the box, and buys an
illusion of continuity. Its words: *truth is cheaper than storage.* If you
disagree, that is a decision to make deliberately and it is a small change.

**The practical bit:** re-upload the two bills through the UI on port 8443 and
they will be read. With `document_ocr` now actually reaching the executor for
the first time (see below), a photograph or a scan will work too, not just a
`.txt`.

---

## 8. One stale thing in the instance user data, which is not currently biting

The EC2 user data still names a Bedrock model that was deleted:

```
model: arn:aws:bedrock:us-west-2:485964361844:imported-model/jfu3j2ssmqvx
```

Both imported models were removed to stop the per-model-minute billing. The
live planner is fine — the journal says
`provider=bedrock model=qwen.qwen3-235b-a22b-2507-v1:0`, so the config file is
winning — but user data is in `CONFIG_KEYS` and a future boot order change
would make that dead ARN the effective setting. Worth tidying next time you
edit the launch template; not worth a reboot on its own.

---

## 9. Four more things waiting on a ruling from you

These are from my working notes for this session, not from anything in the
repository, so treat the details as mine to re-confirm rather than as a record:

- **18 meta-description rewrites** on the site, drafted and not yet approved.
- **10 draft questions** the scout wants answered before it scores.
- **The §5.1 conversation-memory decision.** Whether chat turns live as a named
  exception inside the existing store, or get a sovereign store of their own.
  Jarvis proposed 7-day retention for them. This one is genuinely a design
  fork, not a preference, and it is the kind of thing the CLAUDE.md rule says
  gets asked rather than decided for it.
- **The shared artifact.** `89toZqjrBmirJg8jusQNKY` is the old version; v8 is
  at `EfjLjaeCzRNFeNmDjR2pj9`. Anyone you sent the first link to still has the
  old one.

---

## What I did, for the record

Deployed and verified live rather than assumed:

- **scout stack** — CloudFormation updated. SES condition now permits
  `scout@heartbeat-framework.org`; `jarvis-scout-role` has `bedrock:InvokeModel`
  for one model id. Invoked it: *"drafting enabled:
  qwen.qwen3-235b-a22b-2507-v1:0 via converse, at or above 6.0, 3 per run"*.
- **the source budget fired on its first day.** medRxiv spent its 90s and
  reported TRUNCATED with 23 of an unknown total, instead of reporting a
  partial sweep as a complete one. That is the bug class this project keeps
  coming back to, caught by a guard rather than by someone noticing.
- **agent code, ledgerd, refusal suite** installed; config replaced. Ledger
  verifies INTACT at 8,547 entries; its newest entry carries
  `by: {code, model, provider}`.
- **the config passthrough fix** — `AgentCore` is handed the `agent:` block
  alone, so `documents:`, `marketing:` and `cloud.document_ocr` never reached
  it. `document_ocr` has said `enabled: true` since the day it was added and
  had **never once** been read; every scan you uploaded came back declined for
  a reason that was true of the code and false of the config. Fixed, with a
  test that catches the bug class rather than the three instances.
- **live now**, checked on the box this morning on the loopback API:
  `/marketing` → `{"reachable": true, "enabled": true}`;
  `/documents` → `{"dir": "/var/lib/jarvis/documents",
  "formats": ["pdf", "docx", "md", "txt"]}`.

1,526 tests pass.

(I put 1,323 in an earlier commit message today. That was wrong -- it was a
stale figure carried forward rather than a count I took. The real numbers are
1,508 before today's last change and 1,526 after it.)

**Not on any chain:** the CloudFormation update, the IAM policy I added by
hand, and the SSM deploys were done with your credentials from a coding agent.
They are in git history and they are in this document. They are not ledgered
events and git history should not be read as if they were.
