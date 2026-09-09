"""What a replay returns.

One shape, three meanings, never conflated:

  {status: ok,     outputs: {...}}                    the job was done
  {status: ok,     outcome: {...}}                    the institution answered
  {status: failed, error: {...}}                      somebody needs to look

A caller branches on `category` first and never has to parse prose. `outcome`
and `error` are mutually exclusive by construction, so there is no state where
a result is both an answer and a failure.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .outcomes import Category, Code


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(Model):
    screenshot: str | None = None
    dom_snapshot: str | None = None
    observation: str | None = None
    #: Path to the intervention request, when the run stopped because it needs
    #: a person rather than because something is broken.
    intervention: str | None = None


class BusinessOutcome(Model):
    """A legitimate answer that is not the requested success."""

    name: str
    code: Code
    message: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class ErrorDetail(Model):
    """Enough to debug without opening a terminal: what step, what we expected,
    what we actually saw. The assignment asks for exactly these three."""

    code: Code
    message: str
    step_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    evidence: Evidence | None = None


class StepReport(Model):
    step_id: str
    status: str                      # ok | recovered | skipped | failed
    attempts: int = 1
    duration_ms: int = 0
    resolved_by: str | None = None   # which locator strategy actually worked
    candidate_index: int | None = None


class ControlReport(Model):
    """Who held the session, and whether a human ever had to."""

    escalated: bool = False
    escalations: int = 0
    human_actions: int = 0
    final_owner: str = "automation"


class DriftReport(Model):
    """How hard replay had to work to find things.

    Steps that resolved on their first candidate mean the recording is still
    accurate. A rising fallback count is the early warning that an application
    changed under us -- visible before anything actually breaks.
    """

    steps_resolved: int = 0
    first_choice: int = 0
    fell_back: int = 0

    @property
    def stability(self) -> float | None:
        return (self.first_choice / self.steps_resolved) if self.steps_resolved else None


class ReplayResult(Model):
    run_id: str
    capability_id: str
    capability_version: str
    started_at: str
    duration_ms: int

    category: Category
    code: Code
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome: BusinessOutcome | None = None
    error: ErrorDetail | None = None

    steps: list[StepReport] = Field(default_factory=list)
    control: ControlReport = Field(default_factory=ControlReport)
    drift: DriftReport = Field(default_factory=DriftReport)

    #: Asserted zero on this path and carried into the result so a caller can
    #: verify the claim rather than trust it.
    llm_calls: int = 0

    @property
    def ok(self) -> bool:
        return self.category in (Category.SUCCESS, Category.BUSINESS)

    def summary(self) -> str:
        if self.category == Category.SUCCESS:
            return f"ok in {self.duration_ms}ms, outputs={list(self.outputs)}"
        if self.category == Category.BUSINESS:
            return f"ok ({self.outcome.name if self.outcome else self.code.value}) in {self.duration_ms}ms"
        step = f" at {self.error.step_id}" if self.error and self.error.step_id else ""
        return f"{self.category.value}: {self.code.value}{step}"
