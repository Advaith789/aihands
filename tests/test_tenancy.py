"""Overlays may specialise a capability, never change what it does."""

from __future__ import annotations

import pytest

from aihands.tenancy import Overlay


@pytest.fixture
def presidio():
    return Overlay.load("presidio")


def test_behaviour_is_preserved_exactly(capability, presidio):
    out = presidio.apply(capability)
    assert [s.id for s in out.steps] == [s.id for s in capability.steps]
    assert [s.action.type for s in out.steps] == [s.action.type for s in capability.steps]
    assert [s.commits for s in out.steps] == [s.commits for s in capability.steps]
    assert [s.risk for s in out.steps] == [s.risk for s in capability.steps]
    assert out.inputs == capability.inputs and out.outputs == capability.outputs


def test_an_overlay_only_adds_locator_candidates(capability, presidio):
    out = presidio.apply(capability)
    for base, tenant in zip(capability.steps, out.steps):
        if base.target is None:
            continue
        base_set = {c.model_dump_json() for c in base.target.candidates}
        tenant_set = {c.model_dump_json() for c in tenant.target.candidates}
        assert base_set <= tenant_set, f"{base.id} lost a candidate"


def test_candidates_stay_ordered_by_confidence(capability, presidio):
    # Appending naively puts a tenant's role+name alias after the base's
    # last-resort CSS path, so replay resolves through a brittle selector that
    # only works while the two tenants happen to share markup.
    for step in presidio.apply(capability).steps:
        if step.target is None:
            continue
        scores = [c.confidence for c in step.target.candidates]
        assert scores == sorted(scores, reverse=True), step.id


def test_safety_is_repointed_not_widened(capability, presidio):
    out = presidio.apply(capability)
    assert out.safety.allowed_hosts == ("127.0.0.1:8098",)
    assert capability.safety.allowed_hosts[0] not in out.safety.allowed_hosts


def test_approval_carries_over_but_only_from_an_approved_base(approved, capability, presidio):
    assert presidio.apply(approved).is_approved()
    assert presidio.apply(approved).approval.derived_via_overlay == "presidio"
    assert not presidio.apply(capability.model_copy(
        update={"approval": capability.approval.model_copy(update={"status": "draft"})}
    )).is_approved()
