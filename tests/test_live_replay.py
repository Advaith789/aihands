"""Replay against the real target application.

Every runtime condition the brief names, exercised on a live UI rather than a
mock -- because the interesting failures here are things a mock cannot have.
"""

from __future__ import annotations

import pytest

from aihands.kernel.policy import Policy
from aihands.replay.engine import ReplayEngine
from aihands.schema.outcomes import Category, Code

OP = {"operator_id": "OP-77"}


async def _run(surface, capability, params, **kw):
    engine = ReplayEngine(surface, Policy.load("policy.yaml"), allow_risky=True,
                          goal=capability.name, **kw)
    return await engine.run(capability, params)


async def test_happy_path_returns_typed_outputs(surface, approved, reset):
    reset()
    r = await _run(surface, approved, {**OP, "member_id": "M-1001", "deposit": 500})
    assert r.category is Category.SUCCESS
    assert r.outputs["confirmation_number"].startswith("SAV-")
    assert r.llm_calls == 0


@pytest.mark.parametrize("member,deposit,code", [
    ("M-9999", 500, Code.RECORD_NOT_FOUND),
    ("M-1002", 500, Code.RECORD_NOT_PERMITTED),
    ("M-1003", 500, Code.ALREADY_SATISFIED),
    ("M-1001", 10, Code.VALIDATION_REJECTED),
])
async def test_business_answers_are_successes_not_failures(surface, approved, reset,
                                                           member, deposit, code):
    reset()
    r = await _run(surface, approved, {**OP, "member_id": member, "deposit": deposit})
    assert r.category is Category.BUSINESS, r.error
    assert r.code is code
    assert r.ok, "a business outcome is a successful call with a different answer"
    assert r.error is None and r.outcome is not None


async def test_a_transient_failure_is_recovered_without_telling_the_caller(
        surface, approved, reset):
    reset()
    # M-1004's first record load 503s and the second succeeds.
    r = await _run(surface, approved, {**OP, "member_id": "M-1004", "deposit": 250})
    assert r.category is Category.SUCCESS
    assert any(s.status == "recovered" for s in r.steps)


async def test_blocked_pending_human_is_not_reported_as_retryable(surface, approved, reset):
    reset()
    # Over the tenant's threshold, so a second approver is required.
    r = await _run(surface, approved, {**OP, "member_id": "M-1005", "deposit": 15000})
    assert r.code is Code.UNEXPECTED_INTERSTITIAL
    # The CONDITION is recoverable; the RUN is not. Handing a caller
    # "recoverable" invites a retry loop that hits the same wall forever.
    assert r.category is Category.HARD
    assert r.error.evidence.intervention, "an intervention request must be attached"


async def test_bad_input_is_refused_before_a_browser_is_touched(surface, approved):
    r = await _run(surface, approved, {**OP, "member_id": "NOT-AN-ID", "deposit": 500})
    assert r.code is Code.INVALID_INPUT
    assert r.steps == []


async def test_an_unapproved_state_changing_capability_will_not_replay(surface, capability):
    from aihands.schema.capability import Approval
    draft = capability.model_copy(update={"approval": Approval(status="draft")})
    r = await _run(surface, draft, {**OP, "member_id": "M-1001", "deposit": 500})
    assert r.code is Code.NOT_APPROVED
    assert r.steps == []


async def test_an_offsite_capability_is_refused(surface, approved):
    r = await _run(surface, approved.model_copy(
        update={"entry_url": "https://example.com/login"}), {})
    assert r.code is Code.POLICY_VIOLATION


async def test_sensitive_parameters_never_reach_the_evidence(surface, approved, reset, tmp_path):
    from aihands.kernel.observability import RunRecorder, new_run_id
    reset()
    recorder = RunRecorder(new_run_id("redaction"), root=tmp_path)
    await _run(surface, approved, {**OP, "member_id": "M-1001", "deposit": 500},
               recorder=recorder)
    written = "\n".join(p.read_text() for p in recorder.dir.rglob("*")
                        if p.is_file() and p.suffix in (".json", ".jsonl", ".txt"))
    assert "OP-77" not in written, "an operator id marked sensitive leaked into evidence"
    assert "<redacted:" in written


async def test_one_artifact_serves_a_second_institution(surface, approved, reset):
    from aihands.tenancy import load_for
    reset(8098)
    r = await _run(surface, load_for(approved, "presidio"),
                   {**OP, "member_id": "M-1001", "deposit": 500})
    assert r.category is Category.SUCCESS
    assert r.outputs["confirmation_number"].startswith("SAV-")
    # Where the two institutions genuinely differ, replay falls through to the
    # tenant's alias. That count is the drift signal, not a defect.
    assert r.drift.fell_back > 0


async def test_discovery_redacts_from_the_very_first_run(surface, reset, tmp_path):
    """Redaction must start at discovery, not at the first artifact.

    Discovery has no capability yet, so it cannot look up which inputs are
    sensitive -- and the naive consequence is that the very first recorded run,
    the one that goes into evidence, writes an operator id in the clear.
    """
    from aihands.discovery.loop import DiscoveryLoop
    from aihands.discovery.planner import HeuristicPlanner
    from aihands.kernel.observability import RunRecorder
    from aihands.kernel.policy import Policy

    reset()
    recorder = RunRecorder("redaction-probe", root=tmp_path)
    loop = DiscoveryLoop(surface, HeuristicPlanner(), recorder, Policy.load("policy.yaml"))
    await loop.run(goal="Sign in and find the member, then open a savings sub-account "
                        "and read the confirmation number.",
                   entry_url="http://127.0.0.1:8099/login",
                   params={"operator_id": "OP-77", "member_id": "M-1001", "deposit": "500"},
                   outputs=["confirmation_number"], sensitive={"operator_id"})

    written = "\n".join(p.read_text() for p in recorder.dir.rglob("*") if p.is_file())
    assert "OP-77" not in written
    assert "<redacted:" in written
    assert "M-1001" in written, "only what was declared sensitive should be masked"
