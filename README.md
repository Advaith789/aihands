# aihands

Give an AI agent hands: let a model work out how to do a job inside a UI that has no API,
save what it did as a reusable capability, then run that capability from then on with no
model in the loop at all.

The target here is a credit union back-office desk I wrote for this — server-rendered, a
real `<frameset>`, no test ids, and records that misbehave on purpose. Two institutions run
it with different branding, which is how the cross-tenant story gets exercised rather than
described.

---

## Setup

Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

cp .env.example .env        # put your OPENAI_API_KEY in it; .env is gitignored
```

The key is needed for **discovery only**. Replay never reads it, and there is a test that
walks the import graph to prove the replay path cannot reach a model client at all.

### Start the two institutions

```bash
aihands app --tenant mendota  --port 8099   # Lake Mendota Credit Union   (Madison, WI)
aihands app --tenant presidio --port 8098   # Presidio Federal CU         (San Francisco)
```

Sign in with any operator id, e.g. `OP-77`. Reset the seeded data any time:

```bash
curl -X POST http://127.0.0.1:8099/admin/reset
```

### Records that misbehave, on purpose

| member | what it does |
|---|---|
| `M-1001` | the happy path |
| `M-1002` | frozen — servicing is refused |
| `M-1003` | already has a savings sub-account, so the goal is already met |
| `M-1004` | first record load 503s, the second works |
| `M-1005` | fund it over the threshold and a second approver is demanded |
| `M-9999` | does not exist |

Deposits under $25 are rejected. `POST /admin/expire` kills the session mid-flow.

---

## The demo path

Four commands, start to finish.

**1. Discover — a model drives the real UI**

```bash
aihands discover \
  --goal "Sign in to the servicing desk, find the member with the given member id, and open \
a savings sub-account for them with the given opening deposit. Then read the confirmation \
number from the receipt." \
  --entry-url http://127.0.0.1:8099/login \
  --param operator_id=OP-77 --param member_id=M-1001 --param deposit=500 \
  --output confirmation_number \
  --planner ladder \
  --distill-to mendota.member.open_savings_subaccount \
  --name "Open a savings sub-account for a member" \
  --description "Finds a member and opens a savings sub-account, returning the confirmation number."
```

`--planner llm` makes every decision a model decision. `--planner heuristic` uses only rules
and needs no API key, which is how the whole pipeline stays runnable in CI. Every artifact
records which tiers decided which steps, so an offline run can never be mistaken for a
model-driven one.

**2. Approve — because it changes state**

It comes out as `draft`. A capability that only reads replays freely; one that can open an
account needs a person to read it first.

```bash
aihands approve mendota.member.open_savings_subaccount --by you@example.com
```

**3. Replay — with parameters the recording never saw, and no model**

```bash
aihands replay mendota.member.open_savings_subaccount \
  --param operator_id=OP-77 --param member_id=M-1005 --param deposit=750 --allow-risky
```

**4. Run the same artifact against the other institution**

```bash
aihands replay mendota.member.open_savings_subaccount --tenant presidio \
  --param operator_id=OP-77 --param member_id=M-1001 --param deposit=500 --allow-risky
```

Try `M-9999` for a business answer, `M-1004` for a transient it recovers from, and
`--param deposit=15000` for the one that stops and asks for a human.

### Console and agent API

```bash
aihands console          # http://127.0.0.1:8100
```

The catalogue at `GET /api/capabilities` emits JSON Schema for inputs and outputs, so a
capability is directly usable as a tool definition. `POST /api/capabilities/{id}/invoke`
runs one. Add `"attended": true` and a stuck run waits for a person instead of returning an
intervention request.

### Tests and evidence

```bash
pytest -q                          # 186 tests
python scripts/make_evidence.py    # regenerates everything in evidence/ from real runs
```

---

## How it works

```mermaid
flowchart TB
  G["A goal, in English"] --> A1
  A1["MODE A · DISCOVERY<br/>a model drives the real UI<br/>runs once per job"]
  A1 --> A2["Distil the run into a capability file"]
  A2 --> Q{"Can it change money?"}
  Q -->|no| B1
  Q -->|yes| S["A person reads the steps and signs"]
  S --> B1
  B1["MODE B · REPLAY<br/>follow the saved steps<br/>no model, every time after"]
  B1 --> O["Success · a business answer · or a failure"]
  B1 -.->|cannot continue| H["A person takes the live session"]
  H -.->|hands back| B1
```

Discovery is slow, costs a few cents, and happens **once**. Replay is fast, free, and
happens forever. The file in the middle is the product.

The reason for the split is not cost, it is **determinism**. A model asked the same
question twice may answer differently, and nobody lets that near a real account. So the
model runs once, under supervision, and what it worked out becomes something a reviewer can
read and a machine can repeat exactly.

---

### Teaching it a job — discovery

![Discovery sequence](docs/discovery.png)

```mermaid
sequenceDiagram
  autonumber
  participant Person
  participant Discovery
  participant Model
  participant Surface
  participant App as Legacy app

  Person->>Discovery: goal in English + parameters
  loop until the goal is met
    Discovery->>Surface: what is on screen?
    Surface-->>Discovery: named controls, no markup
    Discovery->>Model: this screen — what next?
    Model-->>Discovery: one action
    Discovery->>Surface: measure how to find that control again
    Discovery->>Surface: now do it
    Surface->>App: click / type / read
    App-->>Surface: next screen
  end
  Discovery->>Discovery: distil the recording into a capability
  Discovery-->>Person: saved as draft, awaiting approval
```

The model never sees markup — only named controls, and it refers to them by reference. It
could not write a selector if it wanted to.

Step 6 is the one that matters. We measure how to find that control again *before* acting
on it, because a click navigates away and the element is gone. A description we never tried
is a guess.

---

### Running it — replay

![Replay sequence](docs/replay.png)

```mermaid
sequenceDiagram
  autonumber
  participant Agent
  participant Replay
  participant Policy
  participant Surface
  participant App
  participant Human

  Agent->>Replay: invoke(capability, params)
  Replay->>Policy: check allowlist, approval, typed inputs
  Policy-->>Replay: ok — no browser touched yet
  loop for each step
    Replay->>Surface: resolve the recorded target
    Surface->>App: click / type / read
    App-->>Surface: next screen
    Surface-->>Replay: what happened
    Replay->>Replay: known outcome? then checkpoint
  end
  alt cannot proceed safely
    Replay->>Human: intervention request + evidence
    Human->>App: works in the same live session
    Human-->>Replay: hand back
    Replay->>Replay: verify, then carry on
  end
  Replay-->>Agent: success, business answer, or failure
```

Steps 2 and 3 happen before a browser is opened, so a capability that is unapproved, points
somewhere it should not, or was called with a bad member id costs nothing to refuse.

Inside the loop the order matters: we ask what the application *said* before asserting where
we *are*. "No member found" is an answer; treating it as a broken assertion would bury a
routine result in an alert queue.

---

### The distiller

The piece between the two modes, and the one that makes a recording reusable.

A recording is a list of things that happened to one member on one afternoon. A capability
is a contract. Turning one into the other is four jobs:

| | |
|---|---|
| **Parameterise** | `"M-1001"` becomes `{{ input.member_id }}`, and `/member/M-1001` becomes a pattern. Without this the recipe can only ever open one member's record. |
| **Keep only what was measured** | Every locator was tried against the live page while recording. Anything that matched more than one element is dropped, and if a step has nothing left the whole run is refused rather than saved. |
| **Derive the checks** | What must be true after each step: the URL we reached, the text only the right screen shows, or — for typing — that the field now holds what we typed. |
| **Infer the contract** | Types, patterns and descriptions are read off the run. A value typed on the sign-in screen is treated as a credential, so it gets no example and no pattern written into the file. |

It is a pure function with no browser and no model in it, which is why we can improve how
locators are chosen and re-distil last month's recordings for free — and why the judgement
that matters most here is testable without a live page.

---

### Where the data goes

```mermaid
flowchart TD
  UI[Legacy UI] -->|screen| P[Perception]
  P -->|controls + readouts| PL[Planner<br/>rules, then a model]
  PL -->|one action| EX[Executor]
  EX -->|click / type / read| UI
  EX --> TR[Trace + measured locators]
  TR --> DS[Distiller]
  DS --> CAP[(Capability file)]
  PAR[Params from an agent] --> RE[Replay engine]
  CAP --> RE
  RE --> UI
  RE --> RES[Result: success, answer, or failure]
  RE --> EV[Evidence: log, screenshots]
```

The design decisions, the trade-offs, and the things I got wrong are in
[REPORT.md](REPORT.md). Real output from real runs is in [evidence/](evidence/).

---

## Layout

```
target_app/            the stand-in: two credit unions, one legacy codebase
policy.yaml            the deployment allowlist. The agent cannot touch it.
capabilities/          artifacts, plus per-app outcomes and per-tenant overlays
src/aihands/
  schema/              capability, outcomes, results, bindings — imports no browser
  kernel/              policy, control, redaction, evidence
  surface/             perception + Playwright — the only place a browser exists
  discovery/           planner, loop, distiller — the only place a model exists
  replay/              the production path
  api/                 agent API + operator console
evidence/runs/         committed output from real runs
```
