"""Deterministic replay -- the production execution path.

This module does not import, reference, or transitively reach any language
model client. That is a structural property, not a convention: a test walks the
import graph of this package and fails if one appears, and every result carries
`llm_calls` so a caller can verify rather than trust.

The execution order is deliberate. Everything that can refuse cheaply happens
before the browser is touched, because a capability that opens an account must
never get halfway in and then discover the amount was not a number.

  phase 1  policy, approval, surface -- no UI actions spent
  phase 2  typed input validation    -- no UI actions spent
  phase 3  walk the steps
  phase 4  collect typed outputs

Recovery is a ladder, and each rung is bounded:

  locator candidate fallback  ->  step retry  ->  capability restart  ->  human

with one rule that has no equivalent in a naive design: **a restart is only
legal while nothing has been committed.** Once a mutating step has run we
cannot tell from the client whether it landed, and replaying it could open a
second account. Past that point the only correct move is a person.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..kernel.control import Resume, SessionControl, State
from ..kernel.observability import RunRecorder, new_run_id
from ..kernel.policy import Policy, decide_risky
from ..kernel.redaction import redactor_for
from ..schema.bindings import bind_step, validate_inputs
from ..schema.capability import Capability, KnownOutcome, Step
from ..schema.outcomes import (
    RETRYABLE,
    AihandsError,
    Category,
    CheckpointFailed,
    Code,
    ControlAmbiguous,
    ControlNotFound,
    InvalidInput,
    NotApproved,
    OperatorAborted,
    PolicyViolation,
    RestartUnsafe,
)
from ..schema.results import (
    BusinessOutcome,
    ControlReport,
    DriftReport,
    ErrorDetail,
    Evidence,
    ReplayResult,
    StepReport,
)


class _Terminal(Exception):
    """Stop the run and return this result. Used for declared outcomes, which
    are answers rather than errors and must not travel as exceptions past the
    engine boundary."""

    def __init__(self, category: Category, code: Code,
                 outcome: BusinessOutcome | None = None,
                 error: ErrorDetail | None = None) -> None:
        self.category, self.code, self.outcome, self.error = category, code, outcome, error


class _RetryStep(Exception):
    pass


class _RecoverableOutcome(Exception):
    """A declared condition that may clear on its own. Carries the outcome so
    the engine knows how the artifact says to recover from it, rather than
    guessing."""

    def __init__(self, outcome) -> None:
        self.outcome = outcome


class _Restart(Exception):
    def __init__(self, code: Code, reason: str) -> None:
        self.code, self.reason = code, reason


class ReplayEngine:
    def __init__(self, surface, policy: Policy, *, recorder: RunRecorder | None = None,
                 control: SessionControl | None = None,
                 escalation_mode: str = "fail", allow_risky: bool = False,
                 goal: str = "") -> None:
        self.surface = surface
        self.policy = policy
        self.run_id = recorder.run_id if recorder else new_run_id("replay")
        self.recorder = recorder or RunRecorder(self.run_id)
        self.control = control or SessionControl(self.run_id)
        # "fail" is the right default for unattended production replay: block
        # forever waiting for an operator who may not exist is worse than
        # returning a failure with the intervention request attached.
        self.escalation_mode = escalation_mode
        self.allow_risky = allow_risky
        self.goal = goal

        self._values: dict[str, Any] = {}
        self._reports: list[StepReport] = []
        self._drift = DriftReport()
        self._retry_budget = 0

    # ==================================================================
    # entry point
    # ==================================================================

    async def run(self, capability: Capability, params: dict[str, Any]) -> ReplayResult:
        started, t0 = datetime.now(timezone.utc), time.monotonic()
        self.recorder.redactor = redactor_for(capability, params)
        self._retry_budget = capability.recovery.global_max_retries

        self.recorder.log(
            "replay.start", capability=capability.ref,
            schema_version=capability.schema_version,
            params=self.recorder.redactor.value(params),
            approved=capability.is_approved(), mutating=capability.mutating,
        )

        def finish(category: Category, code: Code, *, outputs=None,
                   outcome=None, error=None) -> ReplayResult:
            if self.control.state is State.AUTOMATION:
                self.control.finish()
            result = ReplayResult(
                run_id=self.run_id, capability_id=capability.capability_id,
                capability_version=capability.version, started_at=started.isoformat(),
                duration_ms=int((time.monotonic() - t0) * 1000),
                category=category, code=code, outputs=outputs or {},
                outcome=outcome, error=error, steps=list(self._reports),
                control=ControlReport(**self.control.report()), drift=self._drift,
                llm_calls=0,   # invariant of this module
            )
            self.recorder.write_json("result.json", result)
            self.recorder.log("replay.finish", summary=result.summary())
            return result

        # -- phase 1: refuse cheaply before touching anything -----------
        try:
            self.policy = self.policy.intersect(capability)
            self.policy.check_url(capability.entry_url)
            self._check_approval(capability)
        except (PolicyViolation, NotApproved) as exc:
            self.recorder.log("replay.refused", code=exc.code.value, message=exc.message)
            return finish(exc.category, exc.code,
                          error=ErrorDetail(code=exc.code, message=exc.message))

        # -- phase 2: typed inputs, still nothing spent -----------------
        try:
            bound = validate_inputs(capability, params)
        except InvalidInput as exc:
            self.recorder.log("replay.invalid_input", message=exc.message)
            return finish(Category.HARD, Code.INVALID_INPUT,
                          error=ErrorDetail(code=Code.INVALID_INPUT, message=exc.message))

        try:
            await self.surface.goto(capability.entry_url)
        except Exception as exc:
            # Almost always "the target application is not running". A stack
            # trace tells the caller nothing they can act on, and this is the
            # first error anybody is going to hit.
            message = (f"could not reach {capability.entry_url} — is the target "
                       f"application running? ({type(exc).__name__})")
            self.recorder.log("replay.unreachable", url=capability.entry_url,
                              error=str(exc)[:200])
            return finish(Category.HARD, Code.SURFACE_ERROR,
                          error=ErrorDetail(code=Code.SURFACE_ERROR, message=message))

        # -- phase 3: walk the steps ------------------------------------
        index, restarts = 0, 0
        while index < len(capability.steps):
            step = capability.steps[index]
            try:
                await self._execute(capability, step, bound, index)
                index += 1
            except _Terminal as term:
                return finish(term.category, term.code,
                              outputs=self._collect(capability, strict=False),
                              outcome=term.outcome, error=term.error)
            except _RetryStep:
                continue
            except _Restart as restart:
                if restarts >= capability.recovery.global_max_retries:
                    ev = await self.recorder.capture(self.surface, "restart_exhausted")
                    return finish(Category.HARD, Code.BUDGET_EXHAUSTED,
                                  error=ErrorDetail(
                                      code=Code.BUDGET_EXHAUSTED,
                                      message=f"{restart.reason} (restart budget exhausted)",
                                      step_id=step.id, evidence=Evidence(**ev)))
                restarts += 1
                self.recorder.log("replay.restart", attempt=restarts,
                                  reason=restart.reason, code=restart.code.value)
                self._values.clear()
                index = 0
                await self.surface.goto(capability.entry_url)
                continue
            except OperatorAborted as exc:
                ev = await self.recorder.capture(self.surface, f"abort_{step.id}")
                return finish(Category.HARD, Code.OPERATOR_ABORTED,
                              error=ErrorDetail(code=Code.OPERATOR_ABORTED,
                                                message=exc.message, step_id=step.id,
                                                evidence=Evidence(**ev)))
            except AihandsError as exc:
                ev = await self.recorder.capture(self.surface, f"fail_{step.id}")
                self._reports.append(StepReport(step_id=step.id, status="failed"))
                return finish(exc.category, exc.code,
                              error=ErrorDetail(code=exc.code, message=exc.message,
                                                step_id=step.id,
                                                expected=self._expected(step),
                                                observed=await self._observed(),
                                                evidence=Evidence(**ev)))

        # -- phase 4: typed outputs -------------------------------------
        try:
            outputs = self._collect(capability, strict=True)
        except AihandsError as exc:
            return finish(exc.category, exc.code,
                          error=ErrorDetail(code=exc.code, message=exc.message))
        return finish(Category.SUCCESS, Code.NONE, outputs=outputs)

    # ==================================================================
    # one step
    # ==================================================================

    async def _execute(self, cap: Capability, step: Step, params: dict[str, Any],
                       index: int) -> None:
        t0 = time.monotonic()
        self.policy.check_action(step.action.type)

        decision = decide_risky(self.policy, cap, step, self.allow_risky)
        if not decision.allowed:
            self.recorder.log("step.risky_blocked", step=step.id, reason=decision.reason)
            if decision.needs_confirmation:
                await self._escalate(cap, step, Code.NOT_APPROVED,
                                     f"risky step needs confirmation: {decision.reason}")
                # A human said continue; from here the step is authorised for
                # this run only. Nothing is written back to the artifact.
            else:
                raise PolicyViolation(decision.reason, Code.POLICY_VIOLATION, step.id)

        bound = bind_step(step, params)
        if bound.action.type == "navigate":
            self.policy.check_url(bound.action.url)

        if bound.require is not None and not await self.surface.wait_for(bound.require, 3000):
            raise CheckpointFailed(
                f"precondition not met before {step.id}: {self._describe(bound.require)}",
                Code.CHECKPOINT_FAILED, step.id)

        max_attempts = 1 + (step.retry.max_attempts if step.retry else 0)
        attempts = 0
        last: AihandsError | None = None
        resolution = None

        while attempts < max_attempts:
            attempts += 1
            await self.control.await_control()
            if self.control.state is State.ABORTED:
                raise OperatorAborted("operator aborted the run", Code.OPERATOR_ABORTED, step.id)
            try:
                resolution = None
                if bound.target is not None:
                    resolution = await self.surface.resolve(bound.target, step.timeout_ms)
                    self._drift.steps_resolved += 1
                    if resolution.candidate_index == 0:
                        self._drift.first_choice += 1
                    else:
                        self._drift.fell_back += 1
                value = await self.surface.act(resolution, bound.action)
                if bound.action.type == "read":
                    self._values[bound.action.into] = value
                # Checked inside the attempt loop on purpose. An action can
                # succeed and still land on a page that says "temporarily
                # unavailable" -- that is a condition to recover from, and
                # checking it outside the loop would skip the retry ladder
                # entirely.
                await self._raise_if_outcome(cap)
                self.recorder.log("step.ok", step=step.id, action=bound.action.type,
                                  intent=step.intent, attempts=attempts,
                                  resolved_by=resolution.strategy if resolution else None,
                                  candidate=resolution.candidate_index if resolution else None)
                last = None
                break
            except _RecoverableOutcome as rec:
                outcome = rec.outcome
                last = AihandsError(outcome.message or outcome.name, outcome.code, step.id)
                self.recorder.log("outcome.recoverable", step=step.id, attempt=attempts,
                                  name=outcome.name, code=outcome.code.value,
                                  remediate=outcome.remediate)
                if attempts < max(max_attempts, outcome.max_attempts) and self._retry_budget > 0:
                    self._retry_budget -= 1
                    if outcome.remediate == "reload":
                        await self.surface.reload_frame(tuple(outcome.detect.frame_path))
                    await self._sleep(300, attempts)
                    # The remediation may have cleared it entirely; re-check
                    # before spending another attempt on the action itself.
                    if not await self.surface.evaluate(outcome.detect):
                        last = None
                        self._reports.append(StepReport(step_id=step.id, status="recovered",
                                                        attempts=attempts))
                        return
                    continue
                break
            except AihandsError as exc:
                last = exc
                self.recorder.log("step.error", step=step.id, attempt=attempts,
                                  code=exc.code.value, message=exc.message)
                # A step that failed is often the application answering. Check
                # what it said before deciding we are broken.
                try:
                    await self._raise_if_outcome(cap)
                except _RecoverableOutcome as rec:
                    exc = AihandsError(rec.outcome.message or rec.outcome.name,
                                       rec.outcome.code, step.id)
                    last = exc
                if exc.code in RETRYABLE and attempts < max_attempts and self._retry_budget > 0:
                    self._retry_budget -= 1
                    await self._sleep(step.retry.backoff_ms if step.retry else 300, attempts)
                    continue
                break

        if last is not None:
            await self._handle_failure(cap, step, last, index)
            return

        for condition in bound.expect:
            if await self.surface.wait_for(condition, step.timeout_ms):
                continue
            # Before calling a missed checkpoint a failure, ask what the
            # application actually said. "Not permitted for this operator" is an
            # answer; reporting it as a broken assertion buries it.
            try:
                await self._raise_if_outcome(cap)
            except _RecoverableOutcome as rec:
                raise AihandsError(rec.outcome.message or rec.outcome.name,
                                   rec.outcome.code, step.id) from None
            raise CheckpointFailed(
                f"expected state not reached after {step.id}: {self._describe(condition)}",
                Code.CHECKPOINT_FAILED, step.id)

        self._reports.append(StepReport(
            step_id=step.id, status="recovered" if attempts > 1 else "ok",
            attempts=attempts, duration_ms=int((time.monotonic() - t0) * 1000),
            resolved_by=resolution.strategy if resolution else None,
            candidate_index=resolution.candidate_index if resolution else None))

    # ==================================================================
    # failure handling
    # ==================================================================

    async def _handle_failure(self, cap: Capability, step: Step,
                              exc: AihandsError, index: int) -> None:
        if exc.code is Code.SESSION_EXPIRED and cap.recovery.allow_restart:
            first_mutation = cap.first_mutation_index
            committed = (
                cap.recovery.restart_only_before_first_mutation
                and first_mutation is not None
                and index > first_mutation
            )
            if committed:
                # The decisive rule. We cannot see from here whether the
                # submitted change landed, so replaying it risks doing it
                # twice. A person has to look at the account.
                raise RestartUnsafe(
                    "session expired after a state-changing step; cannot safely "
                    "replay from the start because the change may already have "
                    "been committed", Code.RESTART_UNSAFE, step.id)
            raise _Restart(exc.code, "session expired before any mutation")

        if exc.escalatable:
            decision = await self._escalate(cap, step, exc.code, exc.message)
            if decision is Resume.RETRY_STEP:
                raise _RetryStep()
            if decision is Resume.HUMAN_COMPLETED:
                # Not "skip". The human asserts the state was reached by other
                # means, so we verify it before believing them -- an unverified
                # skip silently voids the checkpoint the capability was
                # approved under.
                for condition in step.expect:
                    if not await self.surface.evaluate(condition):
                        raise CheckpointFailed(
                            f"operator reported {step.id} complete but "
                            f"{self._describe(condition)} is still not satisfied",
                            Code.CHECKPOINT_FAILED, step.id)
                self._reports.append(StepReport(step_id=step.id, status="skipped",
                                                attempts=1))
                return
            if decision is Resume.ABORT:
                raise OperatorAborted("operator aborted at escalation",
                                      Code.OPERATOR_ABORTED, step.id)
            return   # CONTINUE
        raise exc

    async def _escalate(self, cap: Capability, step: Step | None,
                        code: Code, reason: str) -> Resume:
        evidence = await self.recorder.capture(
            self.surface, f"escalation_{step.id if step else 'run'}")
        self.recorder.log("escalation.raised", step=step.id if step else None,
                          code=code.value, reason=reason, evidence=evidence)

        if self.escalation_mode == "fail":
            # Unattended: record a complete intervention request and stop,
            # rather than blocking on an operator who may not be there.
            request = {
                "code": code.value, "reason": reason,
                "step_id": step.id if step else None,
                "capability_ref": cap.ref, "goal": self.goal,
                "frame_url": await self._content_url(), "evidence": evidence,
            }
            path = self.recorder.write_json("intervention.json", request)

            # The condition may be RECOVERABLE -- a person can approve the
            # transfer, dismiss the dialog, sign back in. The RUN is not.
            # Handing a caller "recoverable" here would invite a retry loop
            # that hits the same wall every time and never asks anyone. So the
            # category reports what is true of the run: automation cannot
            # finish this. The code still says exactly why, and the attached
            # intervention says a human can unblock it.
            raise _Terminal(
                Category.HARD, code,
                error=ErrorDetail(
                    code=code, message=f"{reason} (human intervention required)",
                    step_id=step.id if step else None,
                    expected=self._expected(step) if step else None,
                    observed=await self._observed(),
                    evidence=Evidence(intervention=str(path), **evidence),
                ),
            )

        esc = await self.control.escalate(
            code=code.value, reason=reason, step_id=step.id if step else None,
            capability_ref=cap.ref, goal=self.goal,
            frame_url=await self._content_url(), evidence=evidence)
        # Numbered rather than named by id: a reviewer opening the directory
        # should see which intervention came first, not a hex string. The id
        # itself is still inside the file.
        self.recorder.write_json(
            f"escalation_{len(self.control.escalations):02d}.json", esc.to_dict())
        self.recorder.log("escalation.resolved", escalation=esc.escalation_id,
                          decision=esc.decision.value if esc.decision else None,
                          operator=esc.operator, human_actions=len(esc.human_actions))
        return esc.decision or Resume.CONTINUE

    # ==================================================================
    # outcomes, outputs, helpers
    # ==================================================================

    async def _raise_if_outcome(self, cap: Capability) -> None:
        """If the application declared one of these states, that is the answer."""
        for outcome in cap.outcomes:
            if await self.surface.evaluate(outcome.detect):
                self.recorder.log("outcome.matched", name=outcome.name,
                                  code=outcome.code.value,
                                  category=outcome.category.value)
                body = BusinessOutcome(name=outcome.name, code=outcome.code,
                                       message=outcome.message)
                if outcome.category is Category.BUSINESS:
                    raise _Terminal(Category.BUSINESS, outcome.code, outcome=body)
                if outcome.category is Category.RECOVERABLE:
                    raise _RecoverableOutcome(outcome)
                raise _Terminal(Category.HARD, outcome.code,
                                error=ErrorDetail(code=outcome.code,
                                                  message=outcome.message or outcome.name))

    @staticmethod
    def _as_error(outcome: KnownOutcome) -> AihandsError:
        return AihandsError(outcome.message or outcome.name, outcome.code)

    def _collect(self, cap: Capability, *, strict: bool) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for spec in cap.outputs:
            if spec.name not in self._values:
                if spec.required and strict:
                    raise AihandsError(
                        f"capability promised output {spec.name!r} but no step produced it",
                        Code.CHECKPOINT_FAILED)
                continue
            raw = self._values[spec.name]
            if spec.required and not str(raw).strip():
                # A read that found the control but came back empty satisfies
                # the contract on paper and hands the caller nothing. That is a
                # failure, not a confirmation number.
                raise AihandsError(
                    f"output {spec.name!r} was read but is empty",
                    Code.CHECKPOINT_FAILED)
            try:
                out[spec.name] = (float(str(raw).replace(",", "").lstrip("$"))
                                  if spec.type == "number" else raw)
            except ValueError:
                if strict:
                    raise AihandsError(
                        f"output {spec.name!r} is not a {spec.type}: {raw!r}",
                        Code.CHECKPOINT_FAILED) from None
                out[spec.name] = raw
        return out

    def _check_approval(self, cap: Capability) -> None:
        if not cap.requires_approval():
            return          # read-only capabilities replay freely
        if cap.is_approved():
            return
        why = ("this capability changes state and has never been approved"
               if cap.approval.status == "draft"
               else "the approval on file does not match the current steps")
        raise NotApproved(f"{why}; refusing to replay unattended", Code.NOT_APPROVED)

    @staticmethod
    def _describe(condition) -> str:
        if condition.kind in ("control_present", "control_absent") and condition.target:
            return f"{condition.kind}({condition.target.describe or 'target'})"
        return f"{condition.kind}({condition.value!r})"

    @staticmethod
    def _expected(step: Step) -> str | None:
        if not step.expect:
            return None
        return " and ".join(ReplayEngine._describe(c) for c in step.expect)

    async def _observed(self) -> str | None:
        try:
            obs = await self.surface.observe()
            names = [c.name for c in obs.controls[:8]]
            return f"at {obs.content_url()} showing {names}"
        except Exception:
            return None

    async def _content_url(self) -> str | None:
        try:
            return (await self.surface.observe()).content_url()
        except Exception:
            return None

    @staticmethod
    async def _sleep(base_ms: int, attempt: int) -> None:
        import asyncio
        await asyncio.sleep((base_ms / 1000) * attempt)
