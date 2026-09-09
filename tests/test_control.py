"""Ownership of one live session."""

from __future__ import annotations

import asyncio

import pytest

from aihands.kernel.control import Owner, Resume, SessionControl, State


async def _escalate(control):
    task = asyncio.create_task(control.escalate(
        code="UNEXPECTED_INTERSTITIAL", reason="needs a second approver",
        step_id="s8", capability_ref="cap@1", goal="open a sub-account",
        frame_url="http://127.0.0.1:8099/x", evidence={"screenshot": "x.png"}))
    while control.state is not State.PAUSED:
        await asyncio.sleep(0.01)
    return task


def test_a_fresh_run_belongs_to_automation():
    control = SessionControl("r1")
    assert control.state is State.AUTOMATION and control.owner is Owner.AUTOMATION
    assert not control.held_by_human


async def test_automation_passes_through_when_it_holds_the_session():
    control = SessionControl("r1")
    await asyncio.wait_for(control.await_control(), timeout=0.5)


async def test_escalating_pauses_and_blocks_automation():
    control = SessionControl("r1")
    task = await _escalate(control)
    assert control.owner is Owner.NOBODY
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(control.await_control(), timeout=0.2)
    control.hand_back(Resume.CONTINUE)
    assert await asyncio.wait_for(task, timeout=1) is not None


async def test_taking_control_names_the_operator():
    control = SessionControl("r1")
    task = await _escalate(control)
    esc = control.take_control("operator@mendota")
    assert control.state is State.HUMAN and control.owner is Owner.HUMAN
    assert esc.operator == "operator@mendota"
    control.hand_back(Resume.CONTINUE, "operator@mendota")
    await asyncio.wait_for(task, timeout=1)


def test_control_cannot_be_taken_from_a_running_automation():
    with pytest.raises(RuntimeError):
        SessionControl("r1").take_control("operator")


def test_control_cannot_be_handed_back_when_nobody_took_it():
    with pytest.raises(RuntimeError):
        SessionControl("r1").hand_back(Resume.CONTINUE)


@pytest.mark.parametrize("decision", [Resume.CONTINUE, Resume.RETRY_STEP,
                                      Resume.HUMAN_COMPLETED])
async def test_every_resume_except_abort_returns_the_session(decision):
    control = SessionControl("r1")
    task = await _escalate(control)
    control.take_control("op")
    control.hand_back(decision, "op")
    assert control.state is State.AUTOMATION and control.owner is Owner.AUTOMATION
    await asyncio.wait_for(control.await_control(), timeout=0.5)
    await asyncio.wait_for(task, timeout=1)


async def test_abort_ends_the_run_and_still_unblocks_the_waiter():
    control = SessionControl("r1")
    task = await _escalate(control)
    control.take_control("op")
    control.hand_back(Resume.ABORT, "op")
    assert control.state is State.ABORTED
    # The waiter must be released even on abort, or the run hangs forever
    # instead of failing cleanly.
    await asyncio.wait_for(control.await_control(), timeout=0.5)
    await asyncio.wait_for(task, timeout=1)


async def test_what_the_human_did_is_recorded():
    control = SessionControl("r1")
    task = await _escalate(control)
    control.take_control("op")
    control.record_human_action({"kind": "click", "name": "Authorise"})
    control.record_human_action({"kind": "change", "name": "Approver ID"})
    control.hand_back(Resume.CONTINUE, "op")
    await asyncio.wait_for(task, timeout=1)
    assert control.report()["human_actions"] == 2
    assert all("at" in a for a in control.escalations[-1].human_actions)


async def test_transitions_are_recorded_in_order_with_actors():
    control = SessionControl("r1")
    task = await _escalate(control)
    control.take_control("operator@mendota")
    control.hand_back(Resume.CONTINUE, "operator@mendota")
    await asyncio.wait_for(task, timeout=1)
    moves = [(t.frm.value, t.to.value) for t in control.transitions]
    assert moves == [("AUTOMATION", "PAUSED"), ("PAUSED", "HUMAN"),
                     ("HUMAN", "AUTOMATION")]
    assert control.transitions[1].actor == "operator@mendota"


async def test_the_escalation_carries_enough_context_to_act_on():
    control = SessionControl("r1")
    task = await _escalate(control)
    esc = control.escalations[-1]
    for field in ("code", "reason", "step_id", "capability_ref", "goal", "frame_url"):
        assert getattr(esc, field), f"{field} missing from the intervention request"
    assert esc.evidence.get("screenshot")
    assert esc.to_dict()["escalation_id"].startswith("esc_")
    control.hand_back(Resume.CONTINUE)
    await asyncio.wait_for(task, timeout=1)


def test_a_clean_run_reports_no_escalation():
    control = SessionControl("r1")
    control.finish()
    assert control.state is State.DONE
    assert control.report() == {"escalated": False, "escalations": 0,
                                "human_actions": 0, "final_owner": "nobody"}
