"""Human-in-the-loop escalation, on the same live session.

Not a mock: the run really pauses mid-flow, a person really acts on the page
the automation was using, and the automation really resumes from where it
stopped -- same browser, same cookies, same half-finished form.
"""

from __future__ import annotations

import asyncio

import pytest

from aihands.kernel.control import Resume, SessionControl, State
from aihands.kernel.policy import Policy
from aihands.replay.engine import ReplayEngine
from aihands.schema.outcomes import Category, Code

OP = {"operator_id": "OP-77"}
NEEDS_APPROVAL = {**OP, "member_id": "M-1005", "deposit": 15000}


def _attended(surface, capability, control):
    return ReplayEngine(surface, Policy.load("policy.yaml"), control=control,
                        escalation_mode="block", allow_risky=True, goal=capability.name)


async def _until_paused(control: SessionControl, timeout: float = 25.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if control.escalations and control.state is State.PAUSED:
            return control.escalations[-1]
        await asyncio.sleep(0.05)
    raise AssertionError("run never paused for a human")


async def _human_authorises(surface):
    """What a person does on the page the automation is sitting on."""
    obs = await surface.observe()
    field = next(c for c in obs.controls if "approver" in c.name.lower())
    await surface.fill(field, "OP-99")
    button = next(c for c in obs.controls if c.role == "button")
    await surface.click(button)


async def test_a_human_takes_the_session_and_hands_it_back(surface, approved, reset):
    reset()
    control = SessionControl("handoff-test")
    engine = _attended(surface, approved, control)
    task = asyncio.create_task(engine.run(approved, NEEDS_APPROVAL))

    escalation = await _until_paused(control)
    assert escalation.code == Code.UNEXPECTED_INTERSTITIAL.value
    assert escalation.step_id and escalation.frame_url
    # The intervention carries enough context to act on without asking anyone.
    assert escalation.evidence.get("screenshot") and escalation.evidence.get("observation")

    control.take_control("operator@mendota")
    assert control.state is State.HUMAN

    await surface.start_human_capture()
    await _human_authorises(surface)
    actions = await surface.collect_human_actions()
    for action in actions:
        control.record_human_action(action)
    assert actions, "what the human did must land in the audit trail"

    control.hand_back(Resume.CONTINUE, "operator@mendota")
    result = await asyncio.wait_for(task, timeout=30)

    assert result.category is Category.SUCCESS, result.error
    assert result.outputs["confirmation_number"].startswith("SAV-")
    assert result.control.escalated and result.control.escalations == 1
    assert result.control.human_actions == len(actions)
    assert result.llm_calls == 0, "a handoff must not put a model back in the loop"


async def test_automation_cannot_act_while_a_person_holds_the_session(surface, approved, reset):
    reset()
    control = SessionControl("lease-test")
    task = asyncio.create_task(_attended(surface, approved, control).run(approved, NEEDS_APPROVAL))
    await _until_paused(control)
    control.take_control("operator@mendota")

    # The executor awaits this token before every action. While a human holds
    # it, automation waits -- that single await is the whole control transfer.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(control.await_control(), timeout=0.4)

    control.hand_back(Resume.ABORT, "operator@mendota")
    result = await asyncio.wait_for(task, timeout=30)
    assert result.code is Code.OPERATOR_ABORTED


async def test_claiming_a_step_was_done_manually_is_verified_not_believed(
        surface, approved, reset):
    reset()
    control = SessionControl("verify-test")
    task = asyncio.create_task(_attended(surface, approved, control).run(approved, NEEDS_APPROVAL))
    await _until_paused(control)
    control.take_control("operator@mendota")

    # The operator says they finished the step, but they did not: the approval
    # screen is still on the page. Skipping here would silently void the
    # checkpoint the capability was approved under, so we check instead.
    control.hand_back(Resume.HUMAN_COMPLETED, "operator@mendota")
    result = await asyncio.wait_for(task, timeout=30)
    assert result.category is Category.HARD
    assert result.code is Code.CHECKPOINT_FAILED


async def test_control_transitions_are_recorded_in_order(surface, approved, reset):
    reset()
    control = SessionControl("audit-test")
    task = asyncio.create_task(_attended(surface, approved, control).run(approved, NEEDS_APPROVAL))
    await _until_paused(control)
    control.take_control("operator@mendota")
    await surface.start_human_capture()
    await _human_authorises(surface)
    control.hand_back(Resume.CONTINUE, "operator@mendota")
    await asyncio.wait_for(task, timeout=30)

    states = [(t.frm.value, t.to.value) for t in control.transitions]
    assert states[0] == ("AUTOMATION", "PAUSED")
    assert states[1] == ("PAUSED", "HUMAN")
    assert states[2] == ("HUMAN", "AUTOMATION")
    assert control.transitions[1].actor == "operator@mendota"
