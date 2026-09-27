# Research tab: Paul's brief

27 September 2026. This records what Paul asked for, so the design can be checked against it. It is not a design, and nothing has been built.

## What he asked for

- A new **Research tab** in Jarvis's web UI, where research projects are created.
- Jarvis **breaks problems down, researches them, and learns from the results**.
- **Memory plays a big part.** It guides Jarvis's thinking and the direction a project takes.
- Every project runs the same four-step cycle: **Plan → Learn → Elevate → Review**, then back to Plan.
- There is a **value function and a taste function**, used to test and learn new and novel things.
- It is **shared**. Paul and Jarvis both work in it. Paul gives a direction. Jarvis then runs automated work within that direction and tests his own ideas. Paul can join in at any point.
- **Either of them can start a project.** Paul describes one. Jarvis can also propose one, and it becomes a proposal card that Paul accepts, edits or declines before any work is done. This uses the permission spine's existing proposal pattern. (Paul, 27 September.)

## Stopping and pausing (Paul, 27 September)

Jarvis must be able to stop or pause a project himself, and say why, in four situations:

- **Time out.** A project has a time and cost budget. When the budget runs out, the project stops. It records where it got to, what it spent and what is still open, so that Paul can extend it, redirect it or close it.
- **Clarification.** When Jarvis cannot go on without Paul's judgement (an ambiguous direction, a choice between routes of similar value, or something outside what Paul asked for), he pauses the project and puts one specific question to Paul. Work waits for the answer rather than guessing.
- **Not working.** Jarvis pauses when the evidence says he is stuck. Signs include several cycles with no new finding that holds up, the same approach failing repeatedly, and spending that keeps rising while value stays flat. He says what he tried, why he thinks it is not working, and what he would try instead, then waits for Paul's decision.
- **Breakthrough.** When a finding scores unusually high on value, or passes its checks and changes the direction of the project, Jarvis stops before building on it. He tells Paul with the evidence, so that Paul can review it before it becomes a lesson or steers the next cycle. A claimed breakthrough gets more checking, not less.

Every pause and stop shows on the project in the Research tab. Each is recorded on the ledger with its reason, and the reason is also a lesson for the Review stage. Paul can resume, redirect or close the project from the same place. Jarvis never resumes a project that was paused for clarification or for a breakthrough without Paul's answer.

## A real pull, or a waste of time (Paul, 27 September)

Jarvis must learn to tell a line of work that is genuinely pulling somewhere from one that is wasting time.

- **Judged on outcomes, not on how interesting it feels.** Each thread is scored afterwards on what it actually produced: findings that held up, lessons Paul accepted, questions it closed. That score is set against what the thread cost in time and model calls. The score feeds the value function.
- **Intrigue counts, as instinct (Paul: "often interesting and intrigue is an instinctual pull").** Being drawn to something before there is a result is a real signal, not noise, and must not be squeezed out by short-term scoring. So:
  - **Intrigue has a protected budget.** A set share of each project's time and calls is kept for following intrigue with no result demanded up front. Results-led work cannot spend it.
  - **Intrigue gets a longer horizon.** A thread followed on intrigue is not written off as waste after a few quiet cycles. It is parked, not closed, and credited later if it turns out to have led somewhere, even in a different project.
  - **Jarvis says when intrigue is the reason.** He records "I'm following this because it's intriguing" and what caught him, as a hunch rather than a finding. Hunches already exist in his memory (`jarvis/agent/hunches.py`) as hypotheses, not facts.
  - **His instinct is calibrated over time.** How often his intrigue eventually pays off is measured across projects, alongside Paul's. The aim is to sharpen the instinct, not replace it with a score. A thread that stays merely novel for a long time with nothing behind it still moves slowly towards "waste", but on a horizon measured in weeks, not cycles.
  - **Paul's intrigue counts too.** When Paul says "that's interesting, look into it", the thread gets the same protected treatment.
- **Paul's verdicts calibrate it.** "That was worth it" and "that was a waste of time" are recorded, with a reason where he gives one. Over time Jarvis's own estimate of pull should agree with Paul's. The rate at which they agree is measured and shown, so everyone can see whether his judgement is actually improving.
- **What he learns carries forward.** Patterns of waste (a kind of source, a kind of question, an approach) become lessons that the Plan stage reads first. They pass through the same Elevate checks as any other lesson.

## No endless loops (Paul, 27 September)

Jarvis must never go round in circles. These limits are enforced in code, and are not left to the model's judgement:

- **Hard caps.** Each project and each thread has maximum cycles, model calls and wall-clock time. At a cap, the project times out as described above.
- **Repeat detection.** Before each step, Jarvis compares it with what he has already done: the same question, the same search, the same source, or a plan that differs only in wording. A repeat is refused and counted.
- **No progress means stop.** If a few cycles pass with no new finding that holds up, the project pauses as "not working". Rephrasing the question does not reset the count.
- **Revisits need a reason.** Going back to a thread that was paused or closed needs something new: a new finding, a new source, or Paul's say-so. The reason is recorded.
- **Everything is visible.** Loops that were caught, and repeats that were refused, show on the project and on the ledger, so a pattern of looping becomes a lesson rather than a hidden cost.

## How the brief is read (agreed on 27 September; Paul replied "Cool thanks")

- **Plan.** Split the question into sub-questions, and say what would count as an answer to each.
- **Learn.** Gather evidence from the browser, uploaded documents and, later, connections. Every item is recorded against the project with its source.
- **Elevate.** Turn what held up into lessons Jarvis keeps and uses. A lesson has to be backed by a source or a check; Jarvis saying something is never enough. The promotion defects in the findings report (sections 3.1 and 3.2) are the reason for that rule.
- **Review.** Record what worked, what failed and what is still open. The review feeds the next Plan.
- **Value.** How much a line of work would help, weighed against its time and cost.
- **Taste.** How new or surprising something is compared with what Jarvis already knows, so that he explores rather than circles.
- **Learning.** Paul's verdicts on findings calibrate both value and taste.

## Constraints already known

- Each project has a budget cap, because every step costs model calls.
- Page text is untrusted, and the chat-browsing controls in the security review apply.
- Nothing is sent or posted without Paul's approval.
- Jarvis is consulted before anything that changes how he thinks or what he remembers.
- This is designed after the connections plan, because both touch the tools, the UI and memory.
