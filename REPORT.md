# Design report

The through-line: a model works out how to do a job through a UI **once**, that run is
distilled into a typed artifact, and from then on the job runs from the artifact with no
model deciding anything. Everything hard in this system is about what happens when reality
does not match the recording.

Numbers below are from the runs committed in `evidence/`.

---

## 1. Architecture

Five parts, and the layering is load-bearing rather than decorative:

```
schema/      capability, outcomes, results, bindings   — imports no browser
kernel/      policy, control, redaction, evidence
surface/     perception + Playwright                   — the only browser
discovery/   planner, loop, distiller                  — the only model
replay/      the production path                       — reaches neither
```

Two invariants hold it together and both are tested rather than promised. `schema/` imports
no browser library, which is what would let a desktop driver execute the same artifact.
`replay/` cannot reach a model client — a test walks the live import graph and fails if one
appears, and every result carries `llm_calls` so a caller can check the claim instead of
trusting it.

**Discovery is two stages, not one.** The loop produces a `Trace` — everything that
happened. A separate pure function distils that into a `Capability` — what to do again. The
extra module buys two things I wanted badly: I can improve how locators are chosen and
**re-distil last month's runs** with no browser and no spend, and the judgement that matters
most (is this artifact any good?) becomes testable without a live page. There are 11 tests
on the distiller that never open a browser.

**The planner is a ladder, not a model.** Most steps in a back-office flow are not
decisions — typing a member id into the field labelled "Member ID" is pattern matching, and
paying a model to re-derive it every time is spending money to be slower and less
predictable. So deterministic rules go first, every one of them guarded by *exactly one*:
one field matches this parameter, one button on this form. The moment a screen is ambiguous
the rules decline and the model decides. A heuristic that guesses under ambiguity is worse
than no heuristic, because it is confidently wrong.

Whether that assumption holds is measurable, not asserted — every step records which tier
decided it, and the split ships inside the artifact. On Lake Mendota the rules handled all
9 steps and the model was never called. On Presidio, where the same product calls the field
"Shareholder Number", the rules did 7 and the model did 2 — exactly the two they could not
recognise. The claim worth making to a bank is that **model spend scales with the number of
capabilities, not the number of invocations.**

**Trade-off I accepted:** the run and the operator console live in one process. Taking
control means driving the same live browser, and that browser is an object in memory — a
queue and a second service would be more architecture and strictly less handoff.

---

## 2. Artifact schema

A capability is `inputs → steps → outputs`, plus the things that make it reviewable:
`outcomes`, `safety`, `recovery`, `approval`, `provenance`.

Four decisions I would defend hardest:

**Outcomes are a sibling of steps, not a catch block.** What the application may legitimately
answer is part of the contract a caller reads. They live in the artifact rather than in
engine heuristics because what counts as a business answer is a property of the application,
not of our runtime — and they are shared per app, so a new capability inherits the whole
vocabulary of answers instead of rediscovering what "No member found" looks like.

**Values are bindings, never literals.** Recording `M-1001` produces something that can only
open one member's record. `{{ input.member_id }}` produces something an agent can call. The
grammar is closed — only that one form resolves, no expression evaluation — because an
artifact is data that arrived from a model, and loading it must never execute logic.

**`commits` and `risk` are different fields.** `commits` is read off the wire: did this
action produce a non-GET request. `risk` is the operator's policy question: should a human
bless this. A read-only click on a link is neither, a submit is both, and conflating them
breaks in both directions. This started as one field and the split came from a real bug —
`fill` was in my "mutating" set, so "first mutation" landed on step 1 and made the restart
path dead.

**Inputs and outputs are inferred, not declared.** The distiller reads the contract off the
recording: a numeric value becomes `number`, `M-1001` becomes the pattern `M\-\d+`, and a
value typed on the entry screen before the flow goes anywhere is a **credential** — which is
why an operator id ends up `sensitive` with no example and no pattern written into the file
without anyone remembering to say so. `\d+` rather than `\d{4}` deliberately: we have one
example, and a pattern that rejects a valid member id is worse than no pattern. The operator
can still override any of it; they simply should not have to write down what the recording
already shows.

The schema emits JSON Schema for both sides, so a capability **is** a tool definition rather
than something a wrapper has to describe by hand.

---

## 3. Determinism & error handling

Replay is deterministic because of four choices, each of which failed at least once first.

**Targeting is measured, not guessed.** At record time the loop builds every plausible
locator for the element it is about to act on and *runs each one against the live page*.
Only candidates that resolved to exactly one element survive into the artifact; if none do,
the distiller refuses to compile rather than saving a step replay cannot repeat. On replay
the candidates are tried best-first, and a target matching several elements is **refused,
not resolved** — picking one of three matching rows in a banking UI is how you action the
wrong member's account. Ambiguity is therefore escalatable but never retryable: running the
same locator again matches the same three elements.

**Waiting is a declared condition polled to a deadline, never a sleep.** A frameset taught
me this the hard way: submitting a form inside a frame never navigates the top-level
document, so `wait_for_load_state` returned instantly and the next observation read the
*previous* screen — intermittently. The same frameset makes `page.url` a liar, so URL
assertions name the frame they mean.

**Checkpoints are conjunctions.** Also learned the hard way: posting the sub-account form
renders either the receipt or a "second approval required" interstitial **at the same URL**,
so a URL-only checkpoint passed for both and the run walked on believing it had succeeded.
Committing steps now assert the URL *and* something only the successful screen shows.

**The error taxonomy asks one question of every condition: who needs to act on this?**

| | who acts | examples |
|---|---|---|
| `BUSINESS` | the caller — nothing is broken | no such member, deposit below the minimum, this record is frozen, already done |
| `RECOVERABLE` | nobody yet — bounded retry with declared remediation | transient 503, session expiry, unexpected interstitial |
| `HARD` | an engineer or an operator | checkpoint missed, ambiguous locator, policy, not approved |

Two placements are deliberate. A **validation refusal is a business answer** — the
institution declining an amount is the answer to the question that was asked, and filing it
under failures buries a routine result in an alert queue. And **permission splits in two**:
`RECORD_NOT_PERMITTED` (this member is frozen — an answer about the member) versus
`OPERATOR_NOT_AUTHORISED` (our own misconfiguration). Same HTTP status, opposite
dispositions.

One more distinction that took me a wrong turn to find: the *condition* can be recoverable
while the *run* is not. A dual-approval interstitial is recoverable — a person can approve
it. But if nobody is attending, returning `RECOVERABLE` to a caller invites a retry loop
that hits the same wall forever and never asks anyone. So a blocked run reports `HARD` with
the code preserved and the intervention request attached.

Recovery is a ladder and every rung is bounded: **locator fallback → step retry → capability
restart → human.** The restart rung has a rule with teeth — it is legal only *before* the
first risky committing step. Past that we cannot tell from the client whether the POST
landed, and replaying it could open a second account. And restart-safety is governed by the
operator's own risk policy rather than by our reading of HTTP verbs, because signing in
POSTs too and treating every POST as unrepeatable would make the whole rung dead.

Measured, from `evidence/`: happy path **506 ms, 0 model calls**; the transient recovers in
1.48 s without telling the caller; all four business answers come back as successful calls
with `error: null`.

**UI drift**, secondarily: every result reports how many steps resolved on their first
candidate. A rising fallback count is an early warning that an application changed under us,
visible before anything actually breaks.

---

## 4. Heterogeneity & multi-tenant

**Surface.** The model never sees HTML or a selector. It sees a flat list of controls with a
role, a name, and enough context to tell two similar ones apart — and it names things by
reference, never by markup. That perception is a union of two passes: the semantic one reads
what the page declares as a control, and a behavioural one finds what merely *behaves* like
one — an `onclick`, a tabindex, a pointer cursor. The behavioural pass matters because these
applications are full of table rows that navigate when clicked and are not buttons, links or
anything else queryable. A fixed list of control-ish tags only ever finds the first kind, and
needs a code change every time an unfamiliar app turns up; "has a click handler" is a
property, so it generalises.

Anything found that way gets a name **synthesised from its column header** — `"M-1001"
(column "Member ID")` — because an unnameable control cannot be targeted durably, and that
invented name becomes the `column_text` locator strategy that opens a member record with no
id, no href and no button anywhere on the row. A second strategy, `row_label`, addresses the
value cell of a `Label | Value` row, which is what these record screens are made of.

The seam is `observe / resolve / act`. A desktop driver over Windows UIA or macOS AX
implements the same three methods and the artifact does not change — role-and-name is the
same concept there. I did not build one.

**Tenants.** A capability is recorded once and each institution gets a small overlay
describing only what it calls things. The load-bearing decision is what an overlay *cannot*
do: it may not add, remove or reorder steps, change an action, or touch the contract. It may
only add locator candidates, repoint the host, and narrow safety. So a reviewer who approved
the base does not have to re-approve every tenant — the behaviour they signed is provably
intact, and the approval carries over with the overlay recorded so the inheritance is
auditable rather than invisible. Aliases match on the *recorded locator* rather than on step
ids, so an overlay survives the capability being re-recorded and renumbered.

That path had a bug worth reporting: appending aliases naively put a tenant's proper
role-and-name locator *after* the base's last-resort CSS path, so Presidio resolved through
a brittle structural selector and **passed by luck** while the two tenants happened to share
markup. Candidates are now ordered by confidence. Its 3-of-9 first-choice rate is not a
defect — it correctly says six of nine controls differ between the two institutions.

---

## 5. Escalation & handoff

**Detect.** Anything the artifact does not declare an answer for, plus ambiguity, a missed
checkpoint, an unrecoverable session loss, and any risky step that has not cleared its gates.

**Route.** An intervention request carrying the capability, the step, why it stopped, the
frame URL, a screenshot, and what the system *believed* was on screen. That last one earns
its place: when the screenshot and the observation disagree, the disagreement is the bug, and
you cannot see it with only one of them. Inputs are redacted.

**Transfer.** The executor awaits a control token before every action. That single await is
the entire mechanism — without it, automation and the person both drive the same page and
race each other into a double submit. The human works in the same browser, with a mouse,
freely. I considered routing them through the recorded action vocabulary — it makes auditing
trivial — and rejected it: "take control" that can only do the four things automation can do
is not taking control. The cost is that we must observe rather than mediate, so events are
pushed out of the page through a binding. Buffering them in the page loses everything the
moment a click navigates, which here is every click that matters.

**Resume:** `CONTINUE`, `RETRY_STEP`, `HUMAN_COMPLETED`, `ABORT`. There is deliberately no
"skip" — skipping voids the checkpoints the capability was approved under, and the run
continues believing it reached a state nobody reached. `HUMAN_COMPLETED` says something
safer: the state was reached by other means, now go and verify it. A test proves an operator
who claims a step is done while the screen says otherwise gets `CHECKPOINT_FAILED`.

Unattended runs do not block on an operator who may not exist — they write the intervention
and return. `evidence/runs/replay_handoff_*` is a real pause, takeover and resume: same
browser, same cookies, same half-finished form.

The operator console is deliberately minimal and single-user, and I have not built auth,
queueing or multi-operator routing. The mechanism and the control-transfer model are real;
the console around them is not production.

---

## 6. Safety

**Policy is configuration, never a parameter.** `policy.yaml` is loaded at process start.
No tool argument, artifact field or model output can widen it, and a capability's own safety
block can only *intersect* with it. If a capability could grant itself hosts or action
types, every guarantee here would be decoration, because the artifact is authored by a model.
Defaults are deny-shaped: a permissive default that everyone ships is not a control.

**A risky step needs three independent keys**: the policy permits it, a human has signed
this exact version, and the caller explicitly opted in. Any one missing and a person decides
on the live session instead. Policy may *raise* a step's risk and never lower it — the
recorder's classification is a hint from a model, the operator's patterns are a rule, and
when they disagree the rule wins.

**Approval binds to a hash of the steps, inputs and outputs.** Reword the description and it
stays approved; change a click target and it silently reverts to draft. Without that,
"approved" is a sticker that survives someone changing what it was stuck to. Read-only
capabilities need no signature at all — friction on a balance lookup is how safety controls
get switched off.

**Redaction happens at the write boundary**, in a single pass, so a new log statement cannot
leak by omission — which is how these things actually leak. Sensitive values become a stable
token rather than asterisks, so an incident review can still correlate two runs by the same
operator without ever seeing who they were. Shape-based patterns catch what nobody declared.
A test asserts a live run never writes a sensitive parameter in the clear, anywhere in its
evidence directory — and it caught a real leak: redaction started at the first *artifact*,
so the first *run*, the one that goes into evidence, wrote the operator id in plain text.

---

## 7. Cuts

**Stretch goals: I took two.** The brief says pick at most one or two, so:

*Approval (draft → approved).* Fifteen lines, and it is the one that matters most here —
these institutions already run maker-checker, so a control that says "one party records, a
different party signs" needs no explaining to the people who would operate it.

*Cross-tenant reuse.* Section 3.7 asks for a credible answer to hundreds of tenants running
the same vendor product. A thin working overlay is cheaper than a page of argument and much
harder to hand-wave.

I did not take the other four — code generation, assisted fallback, multi-run stability, or
building the catalogue out into an agent-facing product. There is a small HTTP surface
because the operator console needs a backend and because it is how I demonstrate a typed
invocation; it is not a capability marketplace and I have not treated it as a third stretch
goal.

**One thing I built that nothing in the brief asks for.** The planner is a ladder:
deterministic rules first, the model only when they cannot decide. That is my own idea, not
a requirement, and I want to be straight about it rather than let it pass as scope. I kept
it because model cost is the first objection this system will meet in a room full of
operations people, and "it depends how many capabilities you have, not how many times you
run them" is a much better answer with a measurement attached. It is about 120 lines and it
does not sit on the replay path at all.

**Cut deliberately.** A desktop surface — the protocol is shaped for it, nothing is built.
Surface feature negotiation, which is ceremony with one implementation. Queues, services and
any multi-tenant infrastructure. Auth and multi-operator routing on the console: section 3.6
says to mock the operator UI and make the mechanism real, so the page is a plain list with
four buttons and the control-transfer model underneath it is the part I spent time on. Loops
and conditional branches in a recorded flow, which is why the target application has no
pagination — it would force control flow into the schema for no gain here.

**Cut on principle.** A model fallback when replay fails. It is an obvious feature and it
quietly destroys the one property the whole system exists to provide.

**Known weaknesses.** Checkpoints are derived from URL changes plus one landmark, which is
thin for a step that changes nothing visible. The rules tier is tuned to
labelled-field-and-submit-button flows — true of this class of software, unproven beyond it.
Overlay aliases are hand-written; they should be proposed by diffing a short exploratory run
against a second tenant. And the drift signal is reported but nothing acts on it.

**What I would build next**, in order: candidate locators re-probed on a schedule against
live tenants, so drift raises a ticket before a capability breaks; overlay proposals
generated rather than authored; and a second surface — the accessibility tree over a desktop
application — because that is the claim in section 4 that is currently argued rather than
demonstrated.
