"""The artifact contract: approval, bindings, and the outcome taxonomy.

These are the invariants the rest of the system is allowed to assume. None of
them needs a browser, which is the point of keeping the schema free of one.
"""

from __future__ import annotations

import pytest

from aihands.schema.bindings import bind_text, validate_inputs
from aihands.schema.capability import Approval, Capability
from aihands.schema.outcomes import CATEGORY, Category, Code, InvalidInput


def test_every_code_has_a_category():
    assert set(CATEGORY) == set(Code)


def test_a_validation_refusal_is_an_answer_not_a_failure():
    # The mistake the brief calls out. The institution declining an amount is
    # information the caller asked for; filing it under failures buries it.
    assert CATEGORY[Code.VALIDATION_REJECTED] is Category.BUSINESS
    assert CATEGORY[Code.RECORD_NOT_FOUND] is Category.BUSINESS
    assert CATEGORY[Code.ALREADY_SATISFIED] is Category.BUSINESS


def test_permission_is_split_by_who_is_refused():
    # A frozen record is an answer about the record; an unauthorised operator
    # is our own misconfiguration. Same HTTP status, opposite dispositions.
    assert CATEGORY[Code.RECORD_NOT_PERMITTED] is Category.BUSINESS
    assert CATEGORY[Code.OPERATOR_NOT_AUTHORISED] is Category.HARD


def test_ambiguity_is_never_retried():
    from aihands.schema.outcomes import ESCALATABLE, RETRYABLE
    # Re-running an ambiguous locator matches the same three elements.
    assert Code.CONTROL_AMBIGUOUS not in RETRYABLE
    assert Code.CONTROL_AMBIGUOUS in ESCALATABLE


def test_approval_is_void_once_the_steps_change(approved):
    assert approved.is_approved()
    tampered = approved.model_copy(update={"steps": tuple(
        [approved.steps[0].model_copy(update={"intent": "something else entirely"})]
        + list(approved.steps[1:]))})
    # The signature covers a hash of the steps, so editing them silently
    # reverts the artifact to draft rather than inheriting the approval.
    assert not tampered.is_approved()


def test_rewording_the_description_does_not_void_approval(approved):
    reworded = approved.model_copy(update={"description": "a clearer sentence"})
    assert reworded.is_approved()


def test_read_only_capabilities_need_no_approval(capability):
    read_only = capability.model_copy(update={
        "steps": tuple(s.model_copy(update={"commits": False}) for s in capability.steps),
        "approval": Approval(status="draft")})
    assert not read_only.requires_approval()


def test_binding_grammar_is_closed():
    assert bind_text("/member/{{ input.member_id }}", {"member_id": "M-1"}) == "/member/M-1"
    # No expression evaluation: an artifact is data that arrived from a model.
    assert bind_text("{{ 1 + 1 }}", {}) == "{{ 1 + 1 }}"


def test_an_unknown_binding_is_an_error_not_an_empty_string():
    # Substituting nothing is how a search runs against a blank id and returns
    # somebody else's record.
    with pytest.raises(InvalidInput):
        bind_text("{{ input.nope }}", {"member_id": "M-1"})


def test_inputs_are_validated_before_anything_is_spent(capability):
    with pytest.raises(InvalidInput):
        validate_inputs(capability, {"operator_id": "OP", "member_id": "NOPE", "deposit": 1})
    with pytest.raises(InvalidInput):
        validate_inputs(capability, {"operator_id": "OP", "member_id": "M-1001"})
    ok = validate_inputs(capability, {"operator_id": "OP", "member_id": "M-1001",
                                      "deposit": "500"})
    assert ok["deposit"] == 500.0


def test_artifact_is_usable_as_a_tool_definition(capability):
    schema = capability.input_schema()
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert set(schema["required"]) == {"operator_id", "member_id", "deposit"}
    assert "confirmation_number" in capability.output_schema()["properties"]


def test_restart_is_blocked_only_past_a_risky_commit(capability):
    index = capability.first_mutation_index
    assert index is not None
    assert capability.steps[index].commits and capability.steps[index].risk == "risky"
    # Signing in POSTs too, but repeating a login is harmless -- treating every
    # POST as unrepeatable would make the restart path dead.
    assert any(s.commits and not s.blocks_restart for s in capability.steps)
