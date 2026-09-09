# Design report

A model works out how to do a job through a UI **once**. That run is distilled into a typed
artifact. From then on the job runs from the artifact with no model deciding anything. The hard
part is what happens when reality does not match the recording.

Numbers are from `evidence/`; its README says what each run shows.

---

## 1. Architecture

```
schema/      capability, outcomes, results, bindings   — imports no browser
kernel/      policy, control, redaction, evidence
surface/     perception + Playwright                   — the only browser
discovery/   planner, loop, distiller                  — the only model
replay/      the production path                       — reaches neither
```

Two invariants, both tested rather than promised: `schema/` imports no browser library, which is
what would let a desktop driver execute the same artifact, and `replay/` cannot reach a model
client — a test walks the live import graph, and every result carries `llm_calls` so a caller can
verify rather than trust.

**Discovery is two stages.** The loop produces a trace of what happened; a separate *pure*
function distils it into a capability. So I can improve locator selection and re-distil old runs
with no browser and no spend, and the judgement that matters most — is this artifact any good —
is testable without a live page.

**The planner is a ladder.** Most steps are not decisions: typing a member id into the field
labelled "Member ID" is pattern matching. Rules go first, each guarded by *exactly one* — one
field matches this parameter, one button on this form. On ambiguity they decline and the model
decides. A heuristic that guesses under ambiguity is worse than none, because it is confidently
wrong.

That is measured, not asserted: each step records which tier decided it and the split ships in
the artifact. At Lake Mendota the rules did all 9 steps and the model was never called; at
Presidio, where the same product says "Shareholder Number", they did 7 and the model did 2 —
exactly the two they could not match. **Model spend scales with the number of capabilities, not
the number of invocations.**

**Trade-off:** the run and the console share a process, because taking control means driving the
same live browser and that browser is an object in memory. A queue and a second service would be
more architecture and strictly less handoff.

---

## 2. Artifact schema

`inputs → steps → outputs`, plus what makes it reviewable: `outcomes`, `safety`, `recovery`,
`approval`, `provenance`. Four decisions:

**Outcomes are a sibling of steps, not a catch block.** What the application may legitimately
answer is part of the contract a caller reads, and it belongs to the application rather than our
runtime — so outcomes are shared per app, and a new capability inherits the vocabulary instead of
rediscovering it.

**Values are bindings, never literals.** `M-1001` opens one member's record;
`{{ input.member_id }}` is something an agent can call. The grammar is closed, with no expression
evaluation, because an artifact is data that arrived from a model and loading it must never
execute logic.

**`commits` and `risk` are separate.** `commits` is read off the wire — did this action produce a
non-GET request. `risk` is the operator's question: should a human bless this. A read-only click
is neither; a submit is both. This began as one field, and the split came from a real bug: `fill`
was in my "mutating" set, so "the first committing step" landed on step 1 and killed the restart
path.

**The input contract is inferred.** A numeric value becomes `number`; `M-1001` becomes `M\-\d+`;
a value typed on the entry screen, before the flow goes anywhere, is a **credential** — which is
how an operator id ends up sensitive with no example and no pattern in a committed file, without
anyone remembering to say so. `\d+` not `\d{4}`: one example cannot establish a length, and a
pattern that rejects a valid id is worse than none.

The schema emits JSON Schema for both sides, so a capability **is** a tool definition.

---

## 3. Determinism & error handling

**Targeting is measured, not guessed.** While recording, the loop builds every plausible locator
for the element it is about to act on and runs each against the live page. Only those matching
exactly one element survive; if none do, the distiller refuses to save the run. On replay,
candidates are tried best-first and a target matching several is **refused, not resolved** —
picking one of three matching rows is how you action the wrong member's account. Ambiguity is
escalatable but never retryable: the same locator matches the same three next time.

**Waiting is a declared condition polled to a deadline, never a sleep.** With a frameset,
submitting a form never navigates the top-level document, so page-level waiting returns instantly
and the next observation reads the *previous* screen — intermittently. The same frameset makes
`page.url` a liar, so URL assertions name their frame.

**Every acting step is verified.** Posting the form renders either the receipt or a "second
approval required" interstitial **at the same URL**, so a URL-only check passes for both and the
run walks on believing it succeeded; committing steps now assert the URL *and* something only the
successful screen shows. And URL-derived checks left every `fill` unverified — five of nine steps
— because typing navigates nowhere, so replay could type a member id into a box that silently
rejected it and search for nothing. A fill now asserts the field holds what we typed. A required
output that comes back empty is a failure too.

**Where the server knows the answer, we ask it rather than the page.** A 503 and a 403 mean the
same thing at every tenant in every language; the wording does not. So transient and permission
outcomes key on response status, and only what the server reports as an ordinary 200 — "no member
found", "below the minimum" — is matched on text.

| | who acts | examples |
|---|---|---|
| `BUSINESS` | the caller — nothing is broken | no such member, below the minimum, record frozen |
| `RECOVERABLE` | nobody yet — bounded retry, declared remediation | transient 503, session expiry, interstitial |
| `HARD` | an engineer or an operator | checkpoint missed, ambiguous locator, not approved |

Two placements are deliberate. A **validation refusal is a business answer** — declining an
amount answers the question asked, and filing it under failures buries a routine result in an
alert queue. And **permission splits in two**: `RECORD_NOT_PERMITTED` (this member is frozen — an
answer about the member) versus `OPERATOR_NOT_AUTHORISED` (our misconfiguration).

The *condition* can be recoverable while the *run* is not. A dual-approval interstitial is
recoverable — a person can approve it — but unattended, returning `RECOVERABLE` invites a retry
loop that hits the same wall forever and never asks anyone. A blocked run reports `HARD`, code
preserved, intervention attached.

**Recovery is a ladder, every rung bounded:** locator fallback → step retry → capability restart →
human. Restart is legal only *before* the first risky committing step: past that we cannot tell
whether the POST landed, and replaying could open a second account. That line is drawn by the
operator's risk policy, not our reading of HTTP verbs — signing in POSTs too, and treating every
POST as unrepeatable would make the rung dead.

Measured: happy path **~510 ms, 0 model calls**; the transient recovers in 1.45 s without telling
the caller. On **drift**: every result reports how many steps resolved on their first candidate,
so a rising fallback count warns that an application changed before anything breaks.

---

## 4. Heterogeneity & multi-tenant

**Surface.** The model never sees markup — only controls with a role, a name and enough context
to tell two similar ones apart, referenced by id. Perception is a union: a semantic pass reads
what the page declares as a control, a behavioural pass finds what merely *behaves* like one — a
click handler, a tabindex, a pointer cursor. That matters because these applications are full of
table rows that navigate when clicked and are not buttons or links. A fixed list of control-ish
tags finds only the first kind and needs a code change per unfamiliar app; "has a click handler"
is a property, so it generalises. The honest cost is false positives on pages that style ordinary
text as clickable.

Anything found that way is **named from its column header** — `"M-1001" (column "Member ID")` —
because an unnameable control cannot be targeted durably. That invented name becomes the
`column_text` strategy, which opens a member record with no id, href or button on the row. A
second strategy, `row_label`, addresses the value cell of a `Label | Value` row.

The seam is `observe / resolve / act`. A desktop driver over Windows UIA or macOS AX implements
the same three methods and the artifact does not change — role-and-name is the same concept
there. I did not build one.

**Tenants.** Recorded once; each institution gets an overlay describing only what it calls
things. The load-bearing decision is what an overlay *cannot* do: no adding, removing or
reordering steps, no changing an action, no touching the contract. It may only add locator
candidates, repoint the host, and narrow safety. So a reviewer who approved the base does not
re-approve every tenant — the behaviour they signed is provably intact, and the approval carries
over with the overlay recorded so the inheritance is auditable. Aliases match on the *recorded
locator*, not step ids, so an overlay survives re-recording and renumbering.

One bug worth reporting: appending aliases naively put a tenant's role-and-name locator *after*
the base's last-resort CSS path, so Presidio resolved through a brittle structural selector and
**passed by luck** while the two shared markup. Candidates are now ordered by confidence.

---

## 5. Escalation & handoff

**Detect.** Anything the artifact declares no answer for, plus ambiguity, a missed checkpoint, an
unrecoverable session loss, and any risky step that has not cleared its gates.

**Route.** An intervention carrying the capability, the step, why it stopped, the frame URL, a
screenshot, and what the system *believed* was on screen. That last one earns its place: when the
screenshot and the observation disagree, the disagreement is the bug. Inputs are redacted.

**Transfer.** The executor awaits a control token before every action. That single await is the
mechanism — without it, automation and the person both drive the page and race into a double
submit. The human works in the same browser, with a mouse, freely. I considered routing them
through the recorded action vocabulary, which makes auditing trivial, and rejected it: "take
control" that can only do the four things automation can do is not taking control. The cost is
that we observe rather than mediate, so events leave the page through a binding — buffering them
in the page loses everything the moment a click navigates, which here is every click that matters.

**Resume:** `CONTINUE`, `RETRY_STEP`, `HUMAN_COMPLETED`, `ABORT` — deliberately no "skip", which
would void the checkpoints the capability was approved under while the run continues believing it
reached a state nobody reached. `HUMAN_COMPLETED` says something safer: reached by other means,
now verify. A test proves an operator who claims a step is done while the screen says otherwise
gets `CHECKPOINT_FAILED`.

Unattended runs write the intervention and return rather than blocking on an operator who may not
exist. `evidence/runs/12_human_took_over_and_resumed` is a real pause, takeover and resume: same
browser, same cookies, same half-finished form. The console is a mock, as §3.6 asks — a list and
four buttons; the control-transfer model underneath is the part I spent time on.

---

## 6. Safety

**Policy is configuration, never a parameter.** `policy.yaml` loads at process start. No tool
argument, artifact field or model output can widen it; a capability's own safety block can only
*intersect*. If a capability could grant itself hosts or action types, every guarantee here would
be decoration, because the artifact is authored by a model. Defaults are deny-shaped.

**A risky step needs three independent keys**: policy permits it, a human signed this exact
version, and the caller opted in. Any one missing and a person decides on the live session. Policy
may *raise* a step's risk, never lower it — the recorder's classification is a hint from a model,
the operator's patterns are a rule.

**Approval binds to a hash of the steps, inputs and outputs.** Reword the description and it stays
approved; change a click target and it reverts to draft. Otherwise "approved" is a sticker that
survives someone changing what it was stuck to. Read-only capabilities need no signature — friction
on a balance lookup is how controls get switched off.

**Redaction happens at the write boundary**, in one pass, so a new log statement cannot leak by
omission. Sensitive values become a stable token, so a review can correlate two runs by the same
operator without seeing who they were. A test asserts a live run never writes a sensitive parameter
in the clear — and it caught a real leak: redaction began at the first *artifact*, so the first
*run*, the one that goes into evidence, wrote the operator id in plain text.

**Limits.** The allowlist stops the automation leaving the institution; it says nothing about
acting on the wrong *record* inside it — authorising the request belongs to the calling agent and
I have not built it. Whether a committing step counts as *risky* is decided by matching verb
patterns against a sentence a model wrote, so a blandly described action slips through; it would be
better derived from the request itself. Approval detects an edited artifact, not an application
changing under one that is still byte-identical — the likelier failure at scale. And redaction
masks what is declared plus what matches a known shape, so a secret in an undeclared field with an
unusual format would pass, which is why the target application holds no real data.

---

## 7. Cuts

**Two stretch goals.** *Approval* — fifteen lines, and the one that matters most here, because
these institutions already run maker-checker. *Cross-tenant reuse* — §3.7 wants a credible answer
to hundreds of tenants on one vendor product, and a thin working overlay is harder to hand-wave
than a page of argument. I did not take the other four. There is a small HTTP surface because the
console needs a backend and it is how I show a typed invocation; it is not a capability catalogue
and I have not counted it as a third.

**One thing nothing in the brief asks for:** the tiered planner. My own idea, and I would rather
say so than let it pass as scope. Model cost is the first objection this system will meet in a room
of operations people, and "it depends how many capabilities you have, not how many times you run
them" is a better answer with a measurement attached. ~120 lines, none on the replay path.

**Cut deliberately:** a desktop surface (the protocol is shaped for it, nothing built), surface
feature negotiation, queues and services, console auth and multi-operator routing, and loops or
conditional branches in a recorded flow — which is why the target app has no pagination. **Cut on
principle:** a model fallback when replay fails, an obvious feature that quietly destroys the one
property the system exists to provide.

**Known weaknesses.** The rules tier is tuned to labelled-field-and-submit-button flows: true of
this class of software, unproven beyond it. The behavioural perception pass will pick up false
positives. Overlay aliases are hand-written. Drift is reported but nothing acts on it.

**Next, in order.** Locators re-probed on a schedule against live tenants, so drift raises a ticket
before a capability breaks. Overlay aliases proposed by diffing a short exploratory run against a
second tenant rather than authored. And a second surface — the accessibility tree over a desktop
application — because that is the claim in section 4 currently argued rather than demonstrated.
