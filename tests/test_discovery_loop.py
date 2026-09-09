"""The loop's own guarantees, independent of which planner is driving.

The planner is the part we cannot fully control -- it is a model, or a set of
rules, and either can be wrong. So the contract the loop enforces has to hold
regardless of what the planner says.
"""

from __future__ import annotations

from aihands.discovery.loop import DiscoveryLoop
from aihands.discovery.planner import HeuristicPlanner
from aihands.discovery.tools import Planned
from aihands.kernel.observability import RunRecorder
from aihands.kernel.policy import Policy

GOAL = ("Sign in to the servicing desk, find the member, open a savings "
        "sub-account and read the confirmation number.")
PARAMS = {"operator_id": "OP-77", "member_id": "M-1001", "deposit": "500"}


class AlwaysFinishes:
    """A planner that declares victory immediately."""

    name = "stub"

    async def decide(self, goal, obs, context, history):
        return Planned("finish", intent="done", rationale="I think I am finished",
                       tier="stub")


class NeverDecides:
    name = "stub"

    async def decide(self, goal, obs, context, history):
        return None


async def _run(surface, planner, tmp_path, outputs):
    loop = DiscoveryLoop(surface, planner, RunRecorder("loop-test", root=tmp_path),
                         Policy.load("policy.yaml"), max_turns=15)
    return await loop.run(goal=GOAL, entry_url="http://127.0.0.1:8099/login",
                          params=PARAMS, outputs=outputs)


async def test_a_planner_cannot_declare_success_without_the_outputs(surface, reset, tmp_path):
    reset()
    trace = await _run(surface, AlwaysFinishes(), tmp_path, ["confirmation_number"])
    # Accepting this would produce a capability that promises an output no step
    # ever produces -- it would fail on its first real invocation instead of at
    # record time, which is far more expensive to debug.
    assert trace.status == "stuck"
    assert "confirmation_number" in trace.summary


async def test_finishing_is_allowed_once_nothing_is_outstanding(surface, reset, tmp_path):
    reset()
    trace = await _run(surface, AlwaysFinishes(), tmp_path, [])
    assert trace.status == "complete"


async def test_a_planner_with_no_move_stops_rather_than_guessing(surface, reset, tmp_path):
    reset()
    trace = await _run(surface, NeverDecides(), tmp_path, ["confirmation_number"])
    assert trace.status == "stuck"
    assert trace.entries == []


async def test_the_executor_records_the_trace_not_the_planner(surface, reset, tmp_path):
    reset()
    trace = await _run(surface, HeuristicPlanner(), tmp_path, ["confirmation_number"])
    assert trace.status == "complete"
    # Every entry describes an action that actually ran against the page: it
    # has a url it started from and locators that were measured there. A
    # planner's own summary of its run cannot produce either.
    for entry in trace.entries:
        assert entry.url_before and entry.probes
        assert any(p.matched_at_record == 1 for p in entry.probes)
    # And the wire, not the button label, decided what changed state.
    assert any(e.committed for e in trace.entries)
