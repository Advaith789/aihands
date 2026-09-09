"""Policy is the operator's, and nothing the agent produces can widen it."""

from __future__ import annotations

import pytest

from aihands.kernel.policy import Policy, decide_risky
from aihands.schema.capability import Click, Fill, Navigate, Read, Safety, Step, Wait
from aihands.schema.outcomes import PolicyViolation

POLICY = Policy.load("policy.yaml")


def _step(action, intent="do a thing", risk="safe", commits=False):
    return Step(id="s1", intent=intent, action=action, risk=risk, commits=commits)


def test_allowlisted_hosts_pass():
    for url in ("http://127.0.0.1:8099/login", "http://127.0.0.1:8098/desk"):
        POLICY.check_url(url)


@pytest.mark.parametrize("url", [
    "https://example.com/login",
    "http://127.0.0.1:9999/desk",
    "http://evil.internal:8099/x",
    "https://127.0.0.1.attacker.com:8099/x",
    "http://127.0.0.2:8099/x",
])
def test_everything_else_is_refused(url):
    with pytest.raises(PolicyViolation):
        POLICY.check_url(url)


def test_declared_actions_are_permitted():
    for action in ("navigate", "click", "fill", "read", "wait"):
        POLICY.check_action(action)


@pytest.mark.parametrize("action", ["evaluate", "download", "upload", "screenshot", "eval"])
def test_undeclared_actions_are_refused(action):
    with pytest.raises(PolicyViolation):
        POLICY.check_action(action)


@pytest.mark.parametrize("intent", [
    "submit the request to open the sub-account",
    "approve the transfer",
    "delete the standing order",
])
def test_policy_raises_risk_on_its_own_verbs(intent):
    # The recorder's classification is a hint from a model; the operator's
    # patterns are a rule. When they disagree the rule wins -- which is the
    # only safe direction when the hint is the thing being guarded.
    assert POLICY.classify_risk(_step(Click(), intent, commits=True)) == "risky"


@pytest.mark.parametrize("intent", [
    "open the member record",
    "search for the member",
    "read the balance",
])
def test_ordinary_navigation_stays_safe(intent):
    assert POLICY.classify_risk(_step(Click(), intent)) == "safe"


def test_read_only_actions_are_never_risky():
    for action in (Navigate(url="http://127.0.0.1:8099/x"), Read(into="x")):
        assert POLICY.classify_risk(_step(action, "approve everything")) == "safe"


def test_a_step_recorded_as_risky_is_never_downgraded():
    assert POLICY.classify_risk(_step(Fill(value="x"), "type a note", risk="risky")) == "risky"


def test_a_capability_can_narrow_hosts_but_not_widen_them(capability):
    narrowed = POLICY.intersect(capability.model_copy(update={
        "safety": Safety(allowed_hosts=("127.0.0.1:8099",), max_steps=5)}))
    assert narrowed.allowed_hosts == frozenset({"127.0.0.1:8099"})
    assert narrowed.max_steps == 5

    widened = POLICY.intersect(capability.model_copy(update={
        "safety": Safety(allowed_hosts=("evil.example.com",), max_steps=999)}))
    assert widened.allowed_hosts == frozenset()
    assert widened.max_steps <= POLICY.max_steps


def test_an_empty_capability_allowlist_inherits_the_deployment(capability):
    inherited = POLICY.intersect(capability.model_copy(
        update={"safety": Safety(allowed_hosts=(), max_steps=40)}))
    assert inherited.allowed_hosts == POLICY.allowed_hosts


@pytest.mark.parametrize("approved,opted_in,allowed,needs_confirm", [
    (True,  True,  True,  False),
    (True,  False, False, True),
    (False, True,  False, True),
    (False, False, False, True),
])
def test_a_risky_step_needs_both_keys(capability, approved_capability, approved,
                                      opted_in, allowed, needs_confirm):
    from aihands.schema.capability import Approval
    # Built explicitly rather than read off disk: whether the checked-in
    # artifact happens to be signed right now is not what this test is about.
    cap = approved_capability if approved else capability.model_copy(
        update={"approval": Approval(status="draft")})
    step = _step(Click(), "submit the request", risk="risky", commits=True)
    decision = decide_risky(POLICY, cap, step, opted_in)
    assert decision.allowed is allowed
    assert decision.needs_confirmation is needs_confirm


def test_a_safe_step_needs_no_keys_at_all(approved_capability):
    assert decide_risky(POLICY, approved_capability, _step(Read(into="x")), False).allowed


def test_block_policy_refuses_outright_rather_than_asking(approved_capability):
    blocking = Policy(allowed_hosts=POLICY.allowed_hosts,
                      allowed_actions=POLICY.allowed_actions,
                      risky_patterns=POLICY.risky_patterns,
                      risky_action_policy="block")
    step = _step(Click(), "submit the request", risk="risky", commits=True)
    decision = decide_risky(blocking, approved_capability, step, True)
    assert not decision.allowed and not decision.needs_confirmation


def test_a_missing_policy_file_denies_everything():
    empty = Policy.load("does/not/exist.yaml")
    with pytest.raises(PolicyViolation):
        empty.check_url("http://127.0.0.1:8099/login")
    with pytest.raises(PolicyViolation):
        empty.check_action("click")
