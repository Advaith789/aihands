"""Outcome taxonomy.

The assignment names one design mistake explicitly: treating a legitimate
business answer as a crash. So the taxonomy is built around a single question
asked of every condition -- **who needs to act on this?**

  BUSINESS   the caller needs to know. Nothing is broken. "This member is
             frozen" is as valid an answer as a confirmation number, and an
             agent that receives it can decide what to do next.

  RECOVERABLE  nobody needs to know yet. The system can try again, within a
             budget, and if it succeeds the caller never hears about it.

  HARD       an engineer or an operator needs to look. Retrying will not help
             and proceeding is unsafe.

Two places where this taxonomy deliberately differs from the obvious one:

1. A validation refusal is a BUSINESS outcome, not a failure. "The opening
   deposit must be at least $25" is the application answering the question that
   was asked. The caller supplied a number and the institution declined it --
   that is information, and an agent can act on it by supplying a different
   number. Filing it under failures buries a routine answer in the alert queue.

2. Permission is split in two, because one word hides two very different
   situations. If the RECORD cannot be acted on -- this member is frozen --
   that is a business answer about the member. If the OPERATOR is not
   authorised, that is our own misconfiguration and no amount of retrying or
   caller-side cleverness fixes it. Same HTTP status, opposite dispositions.
"""

from __future__ import annotations

from enum import Enum


class Category(str, Enum):
    SUCCESS = "SUCCESS"
    BUSINESS = "BUSINESS"
    RECOVERABLE = "RECOVERABLE"
    HARD = "HARD"


class Code(str, Enum):
    NONE = "NONE"

    # --- business answers -------------------------------------------------
    RECORD_NOT_FOUND = "RECORD_NOT_FOUND"
    ALREADY_SATISFIED = "ALREADY_SATISFIED"        # the goal was already true
    VALIDATION_REJECTED = "VALIDATION_REJECTED"    # the institution declined the input
    RECORD_NOT_PERMITTED = "RECORD_NOT_PERMITTED"  # this record may not be acted on

    # --- recoverable ------------------------------------------------------
    TRANSIENT_UNAVAILABLE = "TRANSIENT_UNAVAILABLE"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SLOW_LOAD = "SLOW_LOAD"
    UNEXPECTED_INTERSTITIAL = "UNEXPECTED_INTERSTITIAL"
    CONTROL_NOT_FOUND = "CONTROL_NOT_FOUND"        # may simply not have rendered yet

    # --- hard -------------------------------------------------------------
    INVALID_INPUT = "INVALID_INPUT"
    CHECKPOINT_FAILED = "CHECKPOINT_FAILED"
    CONTROL_AMBIGUOUS = "CONTROL_AMBIGUOUS"
    OPERATOR_NOT_AUTHORISED = "OPERATOR_NOT_AUTHORISED"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    NOT_APPROVED = "NOT_APPROVED"
    RESTART_UNSAFE = "RESTART_UNSAFE"
    SURFACE_ERROR = "SURFACE_ERROR"
    OPERATOR_ABORTED = "OPERATOR_ABORTED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


CATEGORY: dict[Code, Category] = {
    Code.NONE: Category.SUCCESS,
    Code.RECORD_NOT_FOUND: Category.BUSINESS,
    Code.ALREADY_SATISFIED: Category.BUSINESS,
    Code.VALIDATION_REJECTED: Category.BUSINESS,
    Code.RECORD_NOT_PERMITTED: Category.BUSINESS,
    Code.TRANSIENT_UNAVAILABLE: Category.RECOVERABLE,
    Code.SESSION_EXPIRED: Category.RECOVERABLE,
    Code.SLOW_LOAD: Category.RECOVERABLE,
    Code.UNEXPECTED_INTERSTITIAL: Category.RECOVERABLE,
    Code.CONTROL_NOT_FOUND: Category.RECOVERABLE,
    Code.INVALID_INPUT: Category.HARD,
    Code.CHECKPOINT_FAILED: Category.HARD,
    Code.CONTROL_AMBIGUOUS: Category.HARD,
    Code.OPERATOR_NOT_AUTHORISED: Category.HARD,
    Code.POLICY_VIOLATION: Category.HARD,
    Code.NOT_APPROVED: Category.HARD,
    Code.RESTART_UNSAFE: Category.HARD,
    Code.SURFACE_ERROR: Category.HARD,
    Code.OPERATOR_ABORTED: Category.HARD,
    Code.BUDGET_EXHAUSTED: Category.HARD,
}

#: Conditions where trying the same thing again can genuinely produce a
#: different answer. Ambiguity is deliberately absent: a locator that matched
#: three elements will match three elements next time too. Retrying it is not
#: recovery, it is hoping.
RETRYABLE: frozenset[Code] = frozenset({
    Code.TRANSIENT_UNAVAILABLE,
    Code.SLOW_LOAD,
    Code.CONTROL_NOT_FOUND,
    Code.UNEXPECTED_INTERSTITIAL,
})

#: Conditions a human can resolve on the live session but automation cannot.
ESCALATABLE: frozenset[Code] = frozenset({
    Code.CONTROL_NOT_FOUND,
    Code.CONTROL_AMBIGUOUS,
    Code.UNEXPECTED_INTERSTITIAL,
    Code.SESSION_EXPIRED,
    Code.CHECKPOINT_FAILED,
    Code.RESTART_UNSAFE,
})


class AihandsError(Exception):
    """Engine-level condition carrying a taxonomy code.

    Never raised across the caller boundary for a business outcome: those are
    returned as results. This exists for control flow inside the engine.
    """

    code: Code = Code.SURFACE_ERROR

    def __init__(self, message: str, code: Code | None = None, step_id: str | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.step_id = step_id

    @property
    def category(self) -> Category:
        return CATEGORY[self.code]

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    @property
    def escalatable(self) -> bool:
        return self.code in ESCALATABLE


class PolicyViolation(AihandsError):
    code = Code.POLICY_VIOLATION


class NotApproved(AihandsError):
    code = Code.NOT_APPROVED


class InvalidInput(AihandsError):
    code = Code.INVALID_INPUT


class ControlNotFound(AihandsError):
    code = Code.CONTROL_NOT_FOUND


class ControlAmbiguous(AihandsError):
    code = Code.CONTROL_AMBIGUOUS


class CheckpointFailed(AihandsError):
    code = Code.CHECKPOINT_FAILED


class RestartUnsafe(AihandsError):
    code = Code.RESTART_UNSAFE


class OperatorAborted(AihandsError):
    code = Code.OPERATOR_ABORTED
