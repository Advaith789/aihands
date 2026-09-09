"""The compiler, exercised without a browser or a model.

Deciding whether an artifact is any good is the most important judgement in the
system. Keeping that decision in a pure function is what lets it be tested
directly instead of inferred from whether a live run happened to pass.
"""

from __future__ import annotations

import pytest

from aihands.discovery.distill import DistillRefused, distill_capability
from aihands.discovery.tools import DiscoveryTrace, ProbedLocator, TraceEntry
from aihands.kernel.policy import Policy

POLICY = Policy.load("policy.yaml")


def _probe(strategy: str, matched: int, **fields) -> ProbedLocator:
    base = {"role_name": {"role": "textbox", "name": "Member ID", "exact": True,
                          "confidence": 0.9},
            "css": {"value": "form > input", "confidence": 0.3}}[strategy]
    return ProbedLocator(locator={"strategy": strategy, **base, **fields},
                         matched_at_record=matched)


def _trace(entries: list[TraceEntry], status: str = "complete") -> DiscoveryTrace:
    return DiscoveryTrace(goal="open a sub-account",
                          entry_url="http://127.0.0.1:8099/login", planner="ladder",
                          model="gpt-4o-mini", params={"member_id": "M-1001"},
                          declared_outputs=[], status=status, entries=entries)


def _fill(seq: int = 1, **kw) -> TraceEntry:
    defaults = dict(seq=seq, tool="fill", tier="heuristic", intent="type the member id",
                    rationale="", value="M-1001", frame_path=("main",),
                    content_frame_after=("main",),
                    url_before="http://127.0.0.1:8099/desk/main",
                    url_after="http://127.0.0.1:8099/search?member_id=M-1001",
                    probes=[_probe("role_name", 1), _probe("css", 1)])
    return TraceEntry(**{**defaults, **kw})


def _compile(trace):
    return distill_capability(trace, capability_id="t.cap", name="t", description="d",
                              policy=POLICY, app="mendota")


def test_literals_become_bindings():
    cap = _compile(_trace([_fill()]))
    # A recorded literal produces something that can only ever serve one member.
    assert cap.steps[0].action.value == "{{ input.member_id }}"
    assert cap.declared_bindings() == {"member_id"}


def test_ambiguous_locators_never_reach_the_artifact():
    entry = _fill(probes=[_probe("role_name", 3), _probe("css", 1)])
    cap = _compile(_trace([entry]))
    strategies = [c.strategy for c in cap.steps[0].target.candidates]
    assert "role_name" not in strategies, "a locator that matched 3 elements was kept"
    assert strategies == ["css"]


def test_a_step_with_no_durable_locator_refuses_to_compile():
    entry = _fill(probes=[_probe("role_name", 4), _probe("css", 0)])
    with pytest.raises(DistillRefused, match="resolved to exactly one"):
        _compile(_trace([entry]))


def test_candidates_are_ordered_by_confidence():
    cap = _compile(_trace([_fill()]))
    scores = [c.confidence for c in cap.steps[0].target.candidates]
    assert scores == sorted(scores, reverse=True)


def test_checkpoints_are_host_free_and_value_free():
    cap = _compile(_trace([_fill()]))
    pattern = cap.steps[0].expect[0].value
    # No host, so a second institution on another origin can reuse it; no member
    # id, so a different member can.
    assert "127.0.0.1" not in pattern and "M-1001" not in pattern
    assert "[^/?&]+" in pattern or "/search" in pattern


def test_a_committing_step_gets_more_than_a_url_checkpoint():
    entry = _fill(tool="click", intent="submit the request", value="", committed=True,
                  landmark_after="Confirmation Number",
                  url_after="http://127.0.0.1:8099/member/M-1001/subaccount")
    cap = _compile(_trace([entry]))
    kinds = {c.kind for c in cap.steps[0].expect}
    # A post can render the receipt or an interstitial at the same url.
    assert kinds == {"frame_url_matches", "text_present"}


def test_policy_can_raise_risk_but_the_recorder_cannot_lower_it():
    entry = _fill(tool="click", intent="submit the request to open the account",
                  value="", committed=True, landmark_after="Confirmation Number",
                  url_after="http://127.0.0.1:8099/member/M-1001/subaccount")
    cap = _compile(_trace([entry]))
    assert cap.steps[0].risk == "risky"
    assert cap.steps[0].commits and cap.steps[0].blocks_restart


def test_an_incomplete_run_never_becomes_a_capability():
    with pytest.raises(DistillRefused, match="did not complete"):
        _compile(_trace([_fill()], status="stuck"))


def test_a_fill_is_verified_even_though_it_navigates_nowhere():
    # Typing changes no url, so nothing derived from the url can confirm it.
    # Without a check of its own, every step that supplies a parameter goes
    # unverified -- replay would type a member id into a box that silently
    # rejected it and carry on searching for nothing.
    cap = _compile(_trace([_fill(url_after="http://127.0.0.1:8099/desk/main")]))
    assert [c.kind for c in cap.steps[0].expect] == ["control_value_equals"]
    assert cap.steps[0].expect[0].value == "{{ input.member_id }}"


def test_a_run_with_no_checkpoint_is_refused():
    # A click that went nowhere and changed nothing: there is no state to
    # assert, so the capability could never confirm it did anything.
    entry = _fill(tool="click", value="", intent="click something inert",
                  url_after="http://127.0.0.1:8099/desk/main")
    with pytest.raises(DistillRefused, match="verifiable checkpoint"):
        _compile(_trace([entry]))


def test_compiled_capabilities_are_always_draft():
    cap = _compile(_trace([_fill()]))
    assert cap.approval.status == "draft"
    assert cap.provenance.planner == "ladder" and cap.provenance.model == "gpt-4o-mini"


def test_the_tier_split_is_recorded_so_the_cost_claim_is_measurable():
    entries = [_fill(1), _fill(2, tier="llm",
                     url_after="http://127.0.0.1:8099/member/M-1001")]
    cap = _compile(_trace(entries))
    assert cap.provenance.steps_by_tier == {"heuristic": 1, "llm": 1}
