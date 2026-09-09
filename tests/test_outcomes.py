"""Properties the outcome taxonomy has to hold."""

from __future__ import annotations

import pytest

from aihands.schema import outcomes as o

BUSINESS = [c for c in o.Code if o.CATEGORY[c] is o.Category.BUSINESS]
HARD = [c for c in o.Code if o.CATEGORY[c] is o.Category.HARD]


def test_every_code_is_classified():
    assert set(o.CATEGORY) == set(o.Code)


def test_only_none_means_success():
    assert [c for c in o.Code if o.CATEGORY[c] is o.Category.SUCCESS] == [o.Code.NONE]


def test_a_validation_refusal_is_an_answer_not_a_failure():
    # The mistake the brief calls out: the institution declining an amount is
    # the answer to the question that was asked.
    assert o.CATEGORY[o.Code.VALIDATION_REJECTED] is o.Category.BUSINESS
    assert o.CATEGORY[o.Code.RECORD_NOT_FOUND] is o.Category.BUSINESS
    assert o.CATEGORY[o.Code.ALREADY_SATISFIED] is o.Category.BUSINESS


def test_permission_is_split_by_who_is_refused():
    # A frozen record is an answer about the record; an unauthorised operator
    # is our own misconfiguration.
    assert o.CATEGORY[o.Code.RECORD_NOT_PERMITTED] is o.Category.BUSINESS
    assert o.CATEGORY[o.Code.OPERATOR_NOT_AUTHORISED] is o.Category.HARD


@pytest.mark.parametrize("code", BUSINESS, ids=lambda c: c.value)
def test_a_business_answer_is_never_retried_or_escalated(code):
    assert code not in o.RETRYABLE and code not in o.ESCALATABLE


@pytest.mark.parametrize("code", HARD, ids=lambda c: c.value)
def test_a_hard_failure_is_never_silently_retried(code):
    assert code not in o.RETRYABLE


def test_retryable_conditions_are_all_recoverable():
    assert all(o.CATEGORY[c] is o.Category.RECOVERABLE for c in o.RETRYABLE)


def test_ambiguity_escalates_but_is_never_retried():
    # Running the same locator again matches the same three elements.
    assert o.Code.CONTROL_AMBIGUOUS in o.ESCALATABLE
    assert o.Code.CONTROL_AMBIGUOUS not in o.RETRYABLE


@pytest.mark.parametrize("exc,code", [
    (o.PolicyViolation, o.Code.POLICY_VIOLATION),
    (o.NotApproved, o.Code.NOT_APPROVED),
    (o.InvalidInput, o.Code.INVALID_INPUT),
    (o.ControlNotFound, o.Code.CONTROL_NOT_FOUND),
    (o.ControlAmbiguous, o.Code.CONTROL_AMBIGUOUS),
    (o.CheckpointFailed, o.Code.CHECKPOINT_FAILED),
    (o.RestartUnsafe, o.Code.RESTART_UNSAFE),
    (o.OperatorAborted, o.Code.OPERATOR_ABORTED),
])
def test_each_exception_carries_its_code_and_category(exc, code):
    raised = exc("something happened", step_id="s3")
    assert raised.code is code
    assert raised.category is o.CATEGORY[code]
    assert raised.retryable == (code in o.RETRYABLE)
    assert raised.escalatable == (code in o.ESCALATABLE)
