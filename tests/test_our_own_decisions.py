"""The parts of this system nobody asked for.

The brief specifies a lot. These are the decisions I made on top of it, so
they are the ones that most need proving rather than asserting: the tiered
planner, the perception union, reading commitment off the network, inferring
the input contract, and refusing to restart after something was committed.
"""

from __future__ import annotations

import pytest

from aihands.kernel.policy import Policy
from aihands.replay.engine import ReplayEngine, _Restart
from aihands.schema.outcomes import AihandsError, Code, RestartUnsafe

POLICY = Policy.load("policy.yaml")


# ---------------------------------------------------------------------------
# The planner ladder
# ---------------------------------------------------------------------------

class NeverAsked:
    """Stands in for the model. Fails loudly if the rules escalate to it."""

    name = "llm"
    model = "stub"
    calls = 0

    async def decide(self, goal, obs, context, history):
        raise AssertionError("the rules escalated when they should have decided")


async def test_the_rules_handle_an_unambiguous_screen_without_the_model(surface, reset):
    from aihands.discovery.planner import LadderPlanner
    reset()
    await surface.goto("http://127.0.0.1:8099/login")
    obs = await surface.observe()
    plan = await LadderPlanner(NeverAsked()).decide(
        "sign in", obs,
        {"params": {"operator_id": "OP-77"}, "outputs_remaining": [],
         "consumed": set(), "acted": False}, [])
    assert plan is not None and plan.tier == "heuristic"
    assert plan.tool == "fill" and plan.value == "OP-77"


async def test_the_rules_decline_rather_than_guess_when_nothing_matches(surface, reset):
    from aihands.discovery.planner import HeuristicPlanner
    reset()
    await surface.goto("http://127.0.0.1:8099/login")
    obs = await surface.observe()
    # No parameter matches any field on this screen, and there is more than one
    # thing that could plausibly be next. Guessing here is worse than declining.
    plan = await HeuristicPlanner().decide(
        "do something", obs,
        {"params": {"unrelated_thing": "x"}, "outputs_remaining": ["nope"],
         "consumed": set(), "acted": False}, [])
    assert plan is None


def test_a_renamed_field_is_exactly_what_the_rules_cannot_match():
    from aihands.discovery.planner import tokens
    # This is why the model is still needed: same product, same field, and the
    # words do not overlap at all.
    assert tokens("member_id") <= tokens("Member ID")
    assert not tokens("member_id") <= tokens("Shareholder Number")


# ---------------------------------------------------------------------------
# Perception: what a list of tags would miss
# ---------------------------------------------------------------------------

async def test_a_control_with_no_semantics_is_found_and_named(surface, reset):
    reset()
    await surface.goto("http://127.0.0.1:8099/login")
    obs = await surface.observe()
    await surface.fill(obs.controls[0], "OP-77")
    await surface.click(next(c for c in obs.controls if c.role == "button"))
    obs = await surface.observe()
    await surface.fill(next(c for c in obs.controls if c.role == "textbox"), "M-1001")
    await surface.click(next(c for c in obs.controls if c.role == "button"))
    obs = await surface.observe()

    row = next((c for c in obs.controls if c.name == "M-1001"), None)
    assert row is not None, "the results row was invisible to perception"
    # No role, no href, no button -- found because it behaves like a control.
    assert row.via == "onclick"
    # And named from its column, because an unnameable control cannot be
    # targeted durably.
    assert row.context == 'column "Member ID"'

    # It is genuinely actionable, not just listed.
    await surface.click(row)
    assert "/member/M-1001" in (await surface.observe()).content_url()


async def test_values_are_reported_as_well_as_controls(surface, reset):
    reset()
    await surface.goto("http://127.0.0.1:8099/login")
    obs = await surface.observe()
    assert obs.readouts == [], "a login screen has nothing to read"


# ---------------------------------------------------------------------------
# Commitment, read off the network
# ---------------------------------------------------------------------------

async def test_typing_commits_nothing_and_signing_in_does(surface, reset):
    reset()
    await surface.goto("http://127.0.0.1:8099/login")
    obs = await surface.observe()

    before = surface.non_get_count
    await surface.fill(obs.controls[0], "OP-77")
    assert surface.non_get_count == before, "typing changed nothing but was counted"

    await surface.click(next(c for c in obs.controls if c.role == "button"))
    assert surface.non_get_count > before, "a sign-in posts and must be counted"


async def test_a_read_only_click_is_not_a_commit(surface, reset):
    reset()
    await surface.goto("http://127.0.0.1:8099/login")
    obs = await surface.observe()
    await surface.fill(obs.controls[0], "OP-77")
    await surface.click(next(c for c in obs.controls if c.role == "button"))
    obs = await surface.observe()
    await surface.fill(next(c for c in obs.controls if c.role == "textbox"), "M-1001")

    before = surface.non_get_count
    await surface.click(next(c for c in obs.controls if c.role == "button"))
    # Searching only fetches a page. Judging this from the action type would
    # call it a state change, which is how "the first committing step" ends up
    # on step one and the restart path dies.
    assert surface.non_get_count == before


# ---------------------------------------------------------------------------
# The status code, rather than the wording on the page
# ---------------------------------------------------------------------------

async def test_a_server_error_is_detected_by_status_not_by_english(surface, reset):
    from aihands.schema.capability import Condition
    reset()
    await surface.goto("http://127.0.0.1:8099/login")
    obs = await surface.observe()
    await surface.fill(obs.controls[0], "OP-77")
    await surface.click(next(c for c in obs.controls if c.role == "button"))
    # M-1004's first record load fails.
    await surface.goto("http://127.0.0.1:8099/member/M-1004")
    assert await surface.evaluate(Condition(kind="frame_status_is", value="503"))
    # And the second one does not, which is what makes it transient.
    await surface.goto("http://127.0.0.1:8099/member/M-1004")
    assert not await surface.evaluate(Condition(kind="frame_status_is", value="503"))


# ---------------------------------------------------------------------------
# The inferred input contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("M-1001", r"M\-\d+"),
    ("CLM-004211", r"CLM\-\d+"),
    ("a free text note", None),
    ("plaintext", None),
])
def test_only_structured_values_get_a_pattern(value, expected):
    from aihands.discovery.distill import _shape
    # A wrong pattern rejects valid input, which is worse than no pattern.
    assert _shape(value) == expected


def test_a_credential_gets_no_example_and_no_pattern(capability):
    operator = next(i for i in capability.inputs if i.name == "operator_id")
    assert operator.sensitive
    # The artifact is the one file that gets committed. An example here is a
    # disclosure, and a pattern leaks the shape of the secret.
    assert operator.example is None and operator.pattern is None


def test_the_contract_is_inferred_from_the_run(capability):
    member = next(i for i in capability.inputs if i.name == "member_id")
    deposit = next(i for i in capability.inputs if i.name == "deposit")
    assert member.pattern and member.example == "M-1001"
    assert deposit.type == "number"
    assert all(i.description for i in capability.inputs)


# ---------------------------------------------------------------------------
# Restarting is only safe before something was committed
# ---------------------------------------------------------------------------

async def test_a_restart_is_allowed_before_anything_was_committed(surface, approved_capability):
    engine = ReplayEngine(surface, POLICY)
    expired = AihandsError("session expired", Code.SESSION_EXPIRED)
    with pytest.raises(_Restart):
        await engine._handle_failure(approved_capability, approved_capability.steps[0],
                                     expired, 0)


async def test_a_restart_is_refused_once_something_may_have_landed(
        surface, approved_capability):
    engine = ReplayEngine(surface, POLICY)
    index = approved_capability.first_mutation_index
    assert index is not None
    expired = AihandsError("session expired", Code.SESSION_EXPIRED)
    # Past the commit we cannot tell from here whether the post went through,
    # so replaying from the top could open a second account.
    with pytest.raises(RestartUnsafe):
        await engine._handle_failure(approved_capability,
                                     approved_capability.steps[index + 1],
                                     expired, index + 1)


# ---------------------------------------------------------------------------
# Checkpoint coverage
# ---------------------------------------------------------------------------

def test_every_acting_step_is_verified(capability):
    unverified = [s.id for s in capability.steps
                  if not s.expect and s.action.type != "read"]
    assert not unverified, f"steps that act but are never checked: {unverified}"


def test_an_empty_required_output_is_not_a_success(capability):
    from aihands.schema.outcomes import AihandsError as Err
    engine = ReplayEngine.__new__(ReplayEngine)
    engine._values = {"confirmation_number": "   "}
    with pytest.raises(Err):
        engine._collect(capability, strict=True)


async def test_discovery_redacts_a_credential_nobody_declared(surface, reset, tmp_path):
    """The caller should not have to remember.

    Sensitivity is inferred from the run -- a value typed on the sign-in screen
    is a credential -- but that is only knowable after the run. If the inference
    is applied too late, the very first recording, the one that goes into
    evidence, writes the operator id in plain text.
    """
    from aihands.discovery.loop import DiscoveryLoop
    from aihands.discovery.planner import HeuristicPlanner
    from aihands.kernel.observability import RunRecorder

    reset()
    recorder = RunRecorder("inferred-redaction", root=tmp_path)
    loop = DiscoveryLoop(surface, HeuristicPlanner(), recorder, POLICY, max_turns=15)
    await loop.run(goal="Sign in and find the member, open a savings sub-account, "
                        "then read the confirmation number.",
                   entry_url="http://127.0.0.1:8099/login",
                   params={"operator_id": "OP-77", "member_id": "M-1001", "deposit": "500"},
                   outputs=["confirmation_number"])   # nothing declared sensitive

    written = "\n".join(p.read_text() for p in recorder.dir.rglob("*") if p.is_file())
    assert "OP-77" not in written, "an inferred credential still reached the evidence"
    assert "M-1001" in written, "only credentials should be masked"
