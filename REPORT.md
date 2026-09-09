# Design report

A model works out how to do a job through a UI **once**. That run is distilled into a typed
artifact. From then on the job runs from the artifact with no model deciding anything.
Everything hard here is about what happens when reality does not match the recording.

Numbers below are from the runs committed in `evidence/`. The fastest way to check any claim
in here is `evidence/README.md`, which says what each run demonstrates.

---

## 1. Architecture

```
schema/      capability, outcomes, results, bindings   — imports no browser
kernel/      policy, control, redaction, evidence
surface/     perception + Playwright                   — the only browser
discovery/   planner, loop, distiller                  — the only model
replay/      the production path                       — reaches neither
```

Two invariants hold this together, and both are tested rather than promised. `schema/`
imports no browser library, which is what would let a desktop driver execute the same
artifact. `replay/` cannot reach a model client — a test walks the live import graph and
fails if one appears, and every result carries `llm_calls` so a caller can check the claim
instead of trusting it.

**Discovery is two stages.** The loop produces a trace — everything that happened. A
separate pure function distils that into a capability — what to do again. That split buys
two things: I can improve how locators are chosen and re-distil old runs with no browser and
no spend, and the judgement that matters most (is this artifact any good?) becomes testable
without a live page.

**The planner is a ladder.** Most steps are not decisions — typing a member id into the
field labelled "Member ID" is pattern matching, and paying a model to re-derive it every
time is spending money to be slower and less predictable. Deterministic rules go first, each
guarded by *exactly one*: one field matches this parameter, one button on this form. The
moment a screen is ambiguous the rules decline and the model decides. A heuristic that
guesses under ambiguity is worse than none, because it is confidently wrong.

Whether that holds is measured, not asserted: every step records which tier decided it, and
the split ships inside the artifact. On Lake Mendota the rules handled all 9 steps and the
model was never called. On Presidio, where the same product calls the field "Shareholder
Number", the rules did 7 and the model did 2 — exactly the two they could not recognise. The
claim worth making to a bank is that **model spend scales with the number of capabilities,
not the number of invocations.**

**Trade-off accepted:** the run and the operator console share a process. Taking control
means driving the same live browser, and that browser is an object in memory. A queue and a
second service would be more architecture and strictly less handoff.

---

## 2. Artifact schema

A capability is `inputs → steps → outputs`, plus what makes it reviewable: `outcomes`,
`safety`, `recovery`, `approval`, `provenance`. Four decisions I would defend hardest.

**Outcomes are a sibling of steps, not a catch block.** What the application may legitimately
answer is part of the contract a caller reads. They live in the artifact because what counts
as a business answer is a property of the application, not of our runtime — and they are
shared per app, so a new capability inherits the vocabulary instead of rediscovering what
"No member found" looks like.

**Values are bindings, never literals.** Recording `M-1001` produces something that can only
open one member's record; `{{ input.member_id }}` produces something an agent can call. The
grammar is closed — no expression evaluation — because an artifact is data that arrived from
a model, and loading it must never execute logic.

**`commits` and `risk` are different fields.** `commits` is read off the wire: did this
action produce a non-GET request. `risk` is the operator's policy question: should a human
bless this. A read-only click is neither, a submit is both. This started as one field, and
the split came from a real bug — `fill` was in my "mutating" set, so "the first committing
step" landed on step 1 and killed the restart path entirely.

**The input contract is inferred, not declared.** A numeric value becomes `number`; `M-1001`
becomes the pattern `M\-\d+`; a value typed on the entry screen, before the flow goes
anywhere, is a **credential** — which is why an operator id ends up sensitive, with no
example and no pattern written into a committed file, without anyone remembering to say so.
`\d+` rather than `\d{4}` deliberately: one example is not enough to infer a length, and a
pattern that rejects a valid member id is worse than no pattern. Anything can be overridden;
nobody should have to write down what the recording already shows.

The schema emits JSON Schema for both sides, so a capability **is** a tool definition rather
than something a wrapper has to describe.

---

## 3. Determinism & error handling

**Targeting is measured, not guessed.** While recording, the loop builds every plausible
locator for the element it is about to act on and runs each against the live page. Only
those resolving to exactly one element survive; if none do, the distiller refuses to save a
step replay cannot repeat. On replay, candidates are tried best-first and a target matching
several elements is **refused, not resolved** — picking one of three matching rows in a
banking UI is how you action the wrong member's account. Ambiguity is therefore escalatable
but never retryable: the same locator matches the same three elements next time.

**Waiting is a declared condition polled to a deadline, never a sleep.** A frameset taught me
this: submitting a form inside a frame never navigates the top-level document, so
`wait_for_load_state` returned instantly and the next observation read the *previous* screen,
intermittently. The same frameset makes `page.url` a liar, so URL assertions name their frame.

**Every acting step is verified.** Two lessons. Posting the sub-account form renders either
the receipt or a "second approval required" interstitial **at the same URL**, so a URL-only
check passed for both and the run walked on believing it had succeeded — committing steps now
assert the URL *and* something only the successful screen shows. And deriving checks from URL
changes left every `fill` unverified, five of nine steps, because typing navigates nowhere;
replay would have typed a member id into a box that silently rejected it and gone on
searching for nothing. A fill now asserts the field holds what we typed. A required output
that comes back empty is a failure too — it satisfies the contract on paper and hands the
caller nothing.

**Where the server knows the answer, we ask it rather than the page.** A 503 and a 403 mean
the same thing on every application, in every language, at every tenant; the wording on the
error page does not. So the transient and permission outcomes key on the response status, and
only what the server reports as an ordinary 200 — "no member found", "below the minimum" — is
matched on text.

**The taxonomy asks one question of every condition: who needs to act on this?**

| | who acts | examples |
|---|---|---|
| `BUSINESS` | the caller — nothing is broken | no such member, deposit below the minimum, record frozen, already done |
| `RECOVERABLE` | nobody yet — bounded retry with declared remediation | transient 503, session expiry, unexpected interstitial |
| `HARD` | an engineer or an operator | checkpoint missed, ambiguous locator, policy, not approved |

Two placements are deliberate. A **validation refusal is a business answer** — the institution
declining an amount is the answer to the question asked, and filing it under failures buries a
routine result in an alert queue. And **permission splits in two**: `RECORD_NOT_PERMITTED`
(this member is frozen — an answer about the member) versus `OPERATOR_NOT_AUTHORISED` (our own
misconfiguration). Same status, opposite dispositions.

One distinction took a wrong turn to find: the *condition* can be recoverable while the *run*
is not. A dual-approval interstitial is recoverable — a person can approve it. But unattended,
returning `RECOVERABLE` invites a retry loop that hits the same wall forever and never asks
anyone. So a blocked run reports `HARD` with the code preserved and the intervention attached.

**Recovery is a ladder, every rung bounded:** locator fallback → step retry → capability
restart → human. Restart is legal only *before* the first risky committing step: past that we
cannot tell from the client whether the POST landed, and replaying it could open a second
account. Restart safety is governed by the operator's risk policy rather than our reading of
HTTP verbs, because signing in POSTs too and treating every POST as unrepeatable would make
the rung dead.

Measured: happy path **510 ms, 0 model calls**; the transient recovers in 1.45 s without
telling the caller; all four business answers return as successful calls with `error: null`.

**UI drift**, secondarily: every result reports how many steps resolved on their first
candidate. A rising fallback count warns that an application changed before anything breaks.

---

## 4. Heterogeneity & multi-tenant

**Surface.** The model never sees markup — only a flat list of controls with a role, a name
and enough context to tell two similar ones apart, referenced by id. Perception is a union of
two passes: the semantic one reads what the page declares as a control, the behavioural one
finds what merely *behaves* like one — a click handler, a tabindex, a pointer cursor. That
second pass matters because these applications are full of table rows that navigate when
clicked and are not buttons, links or anything queryable. A fixed list of control-ish tags
finds only the first kind and needs a code change per unfamiliar app; "has a click handler"
is a property, so it generalises. The honest cost is false positives — plenty of pages style
ordinary text as clickable — which a fixed list cannot have.

Anything found that way is **named from its column header** — `"M-1001" (column "Member ID")`
— because an unnameable control cannot be targeted durably. That invented name becomes the
`column_text` strategy that opens a member record with no id, no href and no button on the
row. A second strategy, `row_label`, addresses the value cell of a `Label | Value` row, which
is what these record screens are made of.

The seam is `observe / resolve / act`. A desktop driver over Windows UIA or macOS AX
implements the same three methods and the artifact does not change — role-and-name is the
same concept there. I did not build one.

**Tenants.** A capability is recorded once; each institution gets an overlay describing only
what it calls things. The load-bearing decision is what an overlay *cannot* do: it may not
add, remove or reorder steps, change an action, or touch the contract. It may only add
locator candidates, repoint the host, and narrow safety. So a reviewer who approved the base
does not re-approve every tenant — the behaviour they signed is provably intact, and the
approval carries over with the overlay recorded so the inheritance is auditable. Aliases
match on the *recorded locator* rather than step ids, so an overlay survives re-recording and
renumbering.

That path had a bug worth reporting: appending aliases naively put a tenant's role-and-name
locator *after* the base's last-resort CSS path, so Presidio resolved through a brittle
structural selector and **passed by luck** while the two tenants shared markup. Candidates are
now ordered by confidence. Its 3-of-9 first-choice rate is not a defect — it correctly says
six of nine controls differ between the institutions.

---

## 5. Escalation & handoff

**Detect.** Anything the artifact declares no answer for, plus ambiguity, a missed checkpoint,
an unrecoverable session loss, and any risky step that has not cleared its gates.

**Route.** An intervention carrying the capability, the step, why it stopped, the frame URL, a
screenshot, and what the system *believed* was on screen. That last one earns its place: when
the screenshot and the observation disagree, the disagreement is the bug, and one of them
alone cannot show you that. Inputs are redacted.

**Transfer.** The executor awaits a control token before every action. That single await is
the mechanism — without it, automation and the person both drive the page and race each other
into a double submit. The human works in the same browser, with a mouse, freely. I considered
routing them through the recorded action vocabulary, which makes auditing trivial, and
rejected it: "take control" that can only do the four things automation can do is not taking
control. The cost is that we observe rather than mediate, so events are pushed out of the page
through a binding — buffering them in the page loses everything the moment a click navigates,
which here is every click that matters.

**Resume:** `CONTINUE`, `RETRY_STEP`, `HUMAN_COMPLETED`, `ABORT`. Deliberately no "skip" —
skipping voids the checkpoints the capability was approved under and the run continues
believing it reached a state nobody reached. `HUMAN_COMPLETED` says something safer: the state
was reached by other means, now verify it. A test proves an operator who claims a step is done
while the screen says otherwise gets `CHECKPOINT_FAILED`.

Unattended runs do not block on an operator who may not exist — they write the intervention
and return. `evidence/runs/12_human_took_over_and_resumed` is a real pause, takeover and
resume: same browser, same cookies, same half-finished form.

The console is a mock, as §3.6 asks: a list and four buttons, no auth, one operator. The
mechanism and the control-transfer model underneath it are the part I spent time on.

---

## 6. Safety

**Policy is configuration, never a parameter.** `policy.yaml` loads at process start. No tool
argument, artifact field or model output can widen it, and a capability's own safety block can
only *intersect* with it. If a capability could grant itself hosts or action types, every
guarantee here would be decoration, because the artifact is authored by a model. Defaults are
deny-shaped: a permissive default that everyone ships is not a control.

**A risky step needs three independent keys**: policy permits it, a human has signed this exact
version, and the caller explicitly opted in. Any one missing and a person decides on the live
session. Policy may *raise* a step's risk and never lower it — the recorder's classification is
a hint from a model, the operator's patterns are a rule, and when they disagree the rule wins.

**Approval binds to a hash of the steps, inputs and outputs.** Reword the description and it
stays approved; change a click target and it silently reverts to draft. Without that,
"approved" is a sticker that survives someone changing what it was stuck to. Read-only
capabilities need no signature — friction on a balance lookup is how safety controls get
switched off.

**Redaction happens at the write boundary**, in a single pass, so a new log statement cannot
leak by omission — which is how these things actually leak. Sensitive values become a stable
token rather than asterisks, so a review can correlate two runs by the same operator without
seeing who they were. A test asserts a live run never writes a sensitive parameter in the
clear anywhere in its evidence, and it caught a real leak: redaction began at the first
*artifact*, so the first *run* — the one that goes into evidence — wrote the operator id in
plain text.

**Where this model stops working.** Four limits I would want stated before anyone relied on it.

The allowlist is a host list, so it stops the automation leaving the institution — it does
nothing about acting on the wrong *record* inside it. A capability approved for member
servicing can be invoked with any member id the caller supplies, and nothing here checks that
the caller was entitled to that member. Authorisation of the *request* belongs to the agent
calling us, and I have not built it.

Risk classification leans on wording. `commits` is read off the wire and is solid, but whether
a committing step counts as *risky* is decided by matching the operator's verb patterns against
a sentence a model wrote. An action described blandly enough slips through as safe. It would be
better derived from the request itself — which endpoint, which method — and that is where I
would take it next.

Approval covers the steps, not the world they run in. The hash detects an edited artifact. It
cannot detect the application changing underneath an artifact that is still byte-identical, and
that is the likelier failure at scale. Drift is measured and reported; nothing acts on it yet.

And redaction is a boundary, not a guarantee. It masks what the capability declares sensitive
plus what matches a known shape. A secret in an undeclared field with an unusual format would
pass through — which is why the target application has no real data in it and why the tests
assert the absence rather than trusting the implementation.

---

## 7. Cuts

**Stretch goals: I took two.** *Approval* — fifteen lines, and the one that matters most here,
because these institutions already run maker-checker and need no explanation of why one party
records and a different party signs. *Cross-tenant reuse* — §3.7 asks for a credible answer to
hundreds of tenants on one vendor product, and a thin working overlay is cheaper than a page of
argument and harder to hand-wave. I did not take the other four. There is a small HTTP surface
because the console needs a backend and it is how I show a typed invocation; it is not a
capability catalogue and I have not counted it as a third.

**One thing I built that nothing in the brief asks for.** The tiered planner. That is my own
idea, and I would rather say so than let it pass as scope. I kept it because model cost is the
first objection this system will meet in a room of operations people, and "it depends how many
capabilities you have, not how many times you run them" is a better answer with a measurement
attached. About 120 lines, and none of it on the replay path.

**Cut deliberately.** A desktop surface — the protocol is shaped for it, nothing is built.
Surface feature negotiation, which is ceremony with one implementation. Queues, services, any
multi-tenant infrastructure. Auth and multi-operator routing on the console. Loops and
conditional branches in a recorded flow, which is why the target application has no pagination:
it would force control flow into the schema for no gain here.

**Cut on principle.** A model fallback when replay fails. An obvious feature that quietly
destroys the one property the system exists to provide.

**Known weaknesses.** The rules tier is tuned to labelled-field-and-submit-button flows — true
of this class of software, unproven beyond it. Perception's behavioural pass will pick up false
positives on pages that style ordinary text as clickable. Overlay aliases are hand-written.
And drift is reported but nothing acts on it.

**What I would build next**, in order: locators re-probed on a schedule against live tenants so
drift raises a ticket before a capability breaks; overlay aliases proposed by diffing a short
exploratory run against a second tenant rather than authored; and a second surface — the
accessibility tree over a desktop application — because that is the claim in section 4 that is
currently argued rather than demonstrated.
