# Evidence

Everything here is output from real runs against the target application. Regenerate all of
it with `python scripts/make_evidence.py` — it takes about a minute and a few cents of model
spend.

`capability.json` is the artifact the discovery runs produced and the replay runs executed.

Each run directory holds `log.jsonl` (append-only, redacted, one record per line),
`result.json` or `trace.json`, and — where a run stopped — a screenshot, a DOM snapshot, and
what the system *believed* was on screen at that moment.

| run | what it shows |
|---|---|
| `01_discovery_llm_only` | **The required real discovery run.** Every decision made by the model, no rules involved. 10 turns, 12 model calls. |
| `02_discovery_ladder` | The same goal with rules first. 9 turns, **0 model calls** — the rules handled the whole flow. |
| `03_discovery_presidio` | The same goal at the second institution, where the fields are named differently. 7 steps by rules, **2 by the model** — exactly the two it could not recognise. |
| `04_replay_refused_while_draft` | A capability that changes state, not yet signed. Refused before a browser is opened. |
| `05_replay_success` | The happy path. ~500 ms, `llm_calls: 0`, returns a confirmation number. |
| `06_answer_member_not_found` | "No such member" comes back as a **successful call with a business answer**, not an error. |
| `07_answer_record_not_permitted` | A frozen record. Detected from the server's 403, not from the wording on the page. |
| `08_answer_deposit_rejected` | A validation refusal — also a business answer, not a failure. |
| `09_recovered_from_transient` | The record service 503s, the engine reloads and carries on. The caller is never told. |
| `10_refused_bad_input` | A member id that fails its pattern. Refused before anything is spent. |
| `11_stopped_needs_a_human` | Unattended, over the dual-approval threshold. Stops and writes an intervention request with screenshot and context. |
| `12_human_took_over_and_resumed` | The same situation attended: the run pauses, a person works in **the same live browser**, hands back, and the run completes. |
| `13_same_capability_other_tenant` | The artifact recorded against one institution, replayed against the other through a tenant overlay. |

Every replay result carries `llm_calls`, asserted zero on that path, so the "no model in the
loop" claim is checkable rather than something to take on trust.
