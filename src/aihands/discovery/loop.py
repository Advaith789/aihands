"""The discovery loop.

One turn is: observe the live screen, ask the planner for a single action, check
it against policy, measure how the target could be found again, act, and write
down what actually happened.

The part that matters most is what gets written down. The *executor* records the
trace, not the planner -- so a capability distilled from it can only describe
steps that really ran. A model asked to summarise its own run will happily
narrate a step it never took.
"""

from __future__ import annotations

import time
from typing import Any

from ..kernel.observability import RunRecorder
from ..kernel.policy import Policy
from ..schema.outcomes import PolicyViolation
from ..surface.controls import Observation
from .tools import DiscoveryTrace, Planned, ProbedLocator, TraceEntry

#: A planner repeating an action that changes nothing is stuck. Nudge, then stop.
REPEAT_WARN = 2
REPEAT_ABORT = 3


class DiscoveryLoop:
    def __init__(self, surface, planner, recorder: RunRecorder,
                 policy: Policy | None = None, max_turns: int = 25) -> None:
        self.surface = surface
        self.planner = planner
        self.recorder = recorder
        self.policy = policy or Policy.load()
        self.max_turns = max_turns

    async def run(self, *, goal: str, entry_url: str, params: dict[str, Any],
                  outputs: list[str], tenant: str = "",
                  sensitive: set[str] | None = None) -> DiscoveryTrace:
        self.policy.check_url(entry_url)

        # Discovery has no capability yet, so it cannot look up which inputs are
        # sensitive -- but the caller knows, and without this the recorded trace
        # writes an operator id into evidence in the clear. Redaction has to
        # start at the first run, not at the first artifact.
        from ..kernel.redaction import Redactor
        if sensitive:
            self.recorder.redactor = Redactor(
                {k: str(v) for k, v in params.items() if k in sensitive})

        trace = DiscoveryTrace(
            goal=goal, entry_url=entry_url, planner=self.planner.name,
            model=getattr(self.planner, "model", None), params=params,
            declared_outputs=list(outputs), tenant=tenant,
        )
        context: dict[str, Any] = {
            "params": params,
            "outputs_remaining": list(outputs),
            "captured": {},
            "consumed": set(),      # what the heuristic has already done
            "acted": False,
        }
        history: list[str] = []
        repeats: dict[str, int] = {}

        await self.surface.goto(entry_url)
        self.recorder.log("discovery.start", goal=goal, entry_url=entry_url,
                          planner=self.planner.name,
                          model=getattr(self.planner, "model", None),
                          outputs=outputs, tenant=tenant)

        for seq in range(1, self.max_turns + 1):
            obs = await self.surface.observe()
            plan = await self.planner.decide(goal, obs, context, history)

            if plan is None:
                trace.status, trace.summary = "stuck", "no planner could choose an action"
                self.recorder.log("discovery.stuck", turn=seq, url=obs.content_url())
                break

            self.recorder.log("discovery.decision", turn=seq, tier=plan.tier,
                              tool=plan.tool, ref=plan.ref, intent=plan.intent,
                              rationale=plan.rationale[:200] or None)

            if plan.tool == "finish":
                missing = context["outputs_remaining"]
                if missing:
                    # A planner declaring victory before it has captured what it
                    # was asked for is the commonest way a run ends up producing
                    # a capability that can never satisfy its own contract. The
                    # prompt already says not to; enforcing it here is what makes
                    # that reliable. The repeat guard stops it looping on the
                    # refusal.
                    self.recorder.log("discovery.premature_finish", turn=seq, missing=missing)
                    history.append(
                        f"NOTE: you called finish, but {missing} has not been captured "
                        "yet. Keep going: complete the task, then read each declared "
                        "output from READOUTS before finishing."
                    )
                    signature = plan.signature(obs.content_url())
                    repeats[signature] = repeats.get(signature, 0) + 1
                    if repeats[signature] >= REPEAT_ABORT:
                        trace.status = "stuck"
                        trace.summary = f"finished repeatedly without capturing {missing}"
                        break
                    continue
                trace.status, trace.summary = "complete", plan.rationale or "goal reached"
                break
            if plan.tool == "give_up":
                trace.status, trace.summary = "stuck", plan.rationale or "planner gave up"
                break

            signature = plan.signature(obs.content_url())
            repeats[signature] = repeats.get(signature, 0) + 1
            if repeats[signature] >= REPEAT_ABORT:
                trace.status = "stuck"
                trace.summary = f"repeated {plan.tool} on {plan.ref} with no effect"
                self.recorder.log("discovery.loop_detected", turn=seq, signature=signature)
                break
            if repeats[signature] == REPEAT_WARN:
                history.append(
                    f"NOTE: '{plan.intent}' has already been tried and the screen did "
                    "not change. Do something different or give_up."
                )

            entry = await self._act(seq, plan, obs, context, trace)
            trace.entries.append(entry)
            history.append(
                f"turn {seq} [{plan.tier}] {plan.tool} {plan.ref} -> {entry.status}"
                + (f" ({entry.error})" if entry.error else "")
            )
            if entry.status == "refused":
                trace.status, trace.summary = "failed", entry.error or "refused by policy"
                break
            if entry.status != "ok":
                # One bad turn is not a failed run. The error goes into the
                # history so the planner can see what went wrong and choose
                # differently; the repeat guard stops it if it cannot. Aborting
                # here would throw away eight good turns over one typo.
                self.recorder.log("discovery.turn_failed", turn=seq, error=entry.error)
                continue
        else:
            trace.status, trace.summary = "stuck", f"exhausted {self.max_turns} turns"

        if trace.status not in ("complete", "failed") and not context["outputs_remaining"]:
            # Every declared output was captured. Whether the planner remembered
            # to say "finish" is bookkeeping, not a result.
            trace.status, trace.summary = "complete", "all declared outputs captured"

        if trace.status == "complete" and context["outputs_remaining"]:
            # Finishing without the outputs it promised is not success. Saying so
            # here stops the distiller from producing a capability that can never
            # satisfy its own contract.
            trace.status = "incomplete"
            trace.summary = f"never captured: {context['outputs_remaining']}"

        trace.llm_calls = getattr(self.planner, "calls", 0)

        # Sensitivity is worked out from the run itself -- a value typed on the
        # entry screen is a credential -- but that can only be known once the
        # run has happened. So apply the same inference the distiller uses
        # before the trace is written, rather than relying on the caller having
        # remembered to declare it. Nothing before this point logs a parameter
        # value, so the trace is the only place it could have escaped.
        from .distill import _infer_inputs
        from ..kernel.redaction import Redactor
        inferred = {k for k, spec in _infer_inputs(trace).items() if spec.get("sensitive")}
        secrets = {k: str(v) for k, v in params.items()
                   if k in inferred or k in (sensitive or set())}
        if secrets:
            self.recorder.redactor = Redactor(secrets)
        self.recorder.log("discovery.finish", status=trace.status, summary=trace.summary,
                          turns=len(trace.entries), llm_calls=trace.llm_calls,
                          tiers=trace.tier_counts())
        self.recorder.write_json("trace.json", trace.to_dict())
        return trace

    # ------------------------------------------------------------------

    async def _act(self, seq: int, plan: Planned, obs: Observation,
                   context: dict[str, Any], trace: DiscoveryTrace) -> TraceEntry:
        action_type = {"fill": "fill", "click": "click", "read": "read"}[plan.tool]
        entry = TraceEntry(seq=seq, tool=plan.tool, tier=plan.tier, intent=plan.intent,
                           rationale=plan.rationale, ref=plan.ref, value=plan.value,
                           into=plan.into, url_before=obs.content_url())

        item = obs.by_ref(plan.ref) or obs.readout_by_ref(plan.ref)

        if item is None and plan.tool == "read" and plan.into:
            # A read names its own target: the declared output is "confirmation
            # number" and the screen has a readout labelled "Confirmation
            # Number". When the planner omits or mistypes the reference we can
            # resolve it deterministically, using the same token rule the
            # heuristic tier uses -- so this is the existing rule applied, not
            # a special case invented to paper over a bad turn.
            from .planner import tokens
            want = tokens(plan.into)
            hits = [r for r in obs.readouts if want and want <= tokens(r.label)]
            if len(hits) == 1:
                item = hits[0]
                entry.ref = plan.ref = item.ref
                self.recorder.log("discovery.repaired_ref", turn=seq, into=plan.into,
                                  resolved_to=item.ref, label=item.label)

        if item is None:
            entry.status = "error"
            entry.error = (f"ref {plan.ref!r} is not on the current screen"
                           if plan.ref else f"no reference given for {plan.tool}")
            return entry
        entry.frame_path = tuple(item.frame_path)
        entry.control = {k: v for k, v in vars(item).items() if not k.startswith("_")}

        try:
            self.policy.check_action(action_type)
        except PolicyViolation as exc:
            entry.status, entry.error = "refused", exc.message
            self.recorder.log("discovery.refused", turn=seq, reason=exc.message)
            return entry

        # Measure BEFORE acting: a click navigates away and the element we would
        # be measuring no longer exists afterwards. Recording a locator we never
        # tried is a guess, and guesses are what this whole layer exists to
        # remove.
        probes = await self.surface.probe(item, tuple(item.frame_path))
        entry.probes = [ProbedLocator(locator=loc, matched_at_record=n) for loc, n in probes]

        before_non_get = self.surface.non_get_count
        try:
            if plan.tool == "fill":
                await self.surface.fill(item, plan.value)
            elif plan.tool == "click":
                await self.surface.click(item)
            else:
                entry.read_value = await self.surface.read(item)
        except Exception as exc:
            entry.status = "error"
            entry.error = f"{type(exc).__name__}: {exc}"
            self.recorder.log("discovery.action_failed", turn=seq, error=entry.error)
            return entry

        # A POST means the application changed something. That is the fact the
        # artifact needs -- it decides whether this capability requires approval
        # and where a restart stops being safe -- and it is read off the wire
        # rather than inferred from what the button was called.
        entry.committed = self.surface.non_get_count > before_non_get
        after = await self.surface.observe()
        entry.url_after = after.content_url()
        entry.content_frame_after = after.content_frame_path()
        landmarks = [r.label for r in after.readouts
                     if r.frame_path == entry.content_frame_after]
        entry.landmark_after = landmarks[0] if landmarks else ""

        # Only a non-navigating action leaves the element in place to re-check.
        if entry.url_after == entry.url_before:
            for probe in entry.probes:
                if probe.matched_at_record == 1:
                    again = await self.surface.probe(item, tuple(item.frame_path))
                    probe.reverified = any(
                        loc == probe.locator and n == 1 for loc, n in again)

        context["acted"] = True
        if plan.tool == "click":
            context["consumed"].add(f"{entry.url_before}|link:{item.name}")
        elif plan.tool == "read" and plan.into:
            context["captured"][plan.into] = entry.read_value
            if plan.into in context["outputs_remaining"]:
                context["outputs_remaining"].remove(plan.into)

        self.recorder.log("discovery.acted", turn=seq, tool=plan.tool, tier=plan.tier,
                          committed=entry.committed, url_after=entry.url_after,
                          durable=[p.locator["strategy"] for p in entry.probes
                                   if p.matched_at_record == 1])
        return entry
