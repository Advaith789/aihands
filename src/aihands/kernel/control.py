"""Control ownership over one live session.

    AUTOMATION --escalate--> PAUSED --take--> HUMAN --resume(decision)--> AUTOMATION
                                                 \\--abort--> ABORTED

The whole mechanism is one token that the executor awaits before every action.
That is what makes a handoff a handoff rather than a restart: the same page, the
same cookies, the same half-filled form, and a human standing in front of it.

Resume decisions are deliberately four, and deliberately not "skip". Skipping a
step in a financial flow silently voids the checkpoints the capability was
approved under -- the run would continue believing a state was reached that
nobody ever reached. HUMAN_COMPLETED says something stronger and safer: the
state was reached by other means, now go and verify it before continuing.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class State(str, Enum):
    AUTOMATION = "AUTOMATION"
    PAUSED = "PAUSED"
    HUMAN = "HUMAN"
    ABORTED = "ABORTED"
    DONE = "DONE"


class Owner(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"
    NOBODY = "nobody"


class Resume(str, Enum):
    CONTINUE = "CONTINUE"                # carry on from the next step
    RETRY_STEP = "RETRY_STEP"            # try the failed step again
    HUMAN_COMPLETED = "HUMAN_COMPLETED"  # the human did it; verify, then continue
    ABORT = "ABORT"


@dataclass
class Escalation:
    escalation_id: str
    code: str
    reason: str
    step_id: str | None
    capability_ref: str
    goal: str
    frame_url: str | None = None
    evidence: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    resolved_at: str | None = None
    decision: Resume | None = None
    operator: str | None = None
    human_actions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["decision"] = self.decision.value if self.decision else None
        return d


@dataclass
class Transition:
    at: str
    frm: State
    to: State
    owner: Owner
    actor: str
    note: str | None = None


class SessionControl:
    """Ownership of one run. Not thread-safe by design -- it belongs to the one
    event loop that is driving the browser, and sharing it further would mean
    two things could act on the same page at once, which is the exact situation
    this class exists to prevent."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.state = State.AUTOMATION
        self.owner = Owner.AUTOMATION
        self.transitions: list[Transition] = []
        self.escalations: list[Escalation] = []
        self._gate = asyncio.Event()
        self._gate.set()                     # automation may act immediately
        self._resumed: asyncio.Future[Resume] | None = None

    # -- the guard ------------------------------------------------------

    async def await_control(self) -> None:
        """Called before every action. While a human holds the session this
        blocks, so automation cannot race the person operating it."""
        await self._gate.wait()

    @property
    def held_by_human(self) -> bool:
        return self.state is State.HUMAN

    def _transition(self, to: State, owner: Owner, actor: str, note: str | None = None) -> None:
        self.transitions.append(Transition(_now(), self.state, to, owner, actor, note))
        self.state, self.owner = to, owner

    # -- escalation lifecycle -------------------------------------------

    async def escalate(self, *, code: str, reason: str, step_id: str | None,
                       capability_ref: str, goal: str, frame_url: str | None,
                       evidence: dict[str, str]) -> Escalation:
        """Pause and wait for a person. Returns once they hand control back."""
        esc = Escalation(
            escalation_id=f"esc_{uuid.uuid4().hex[:8]}", code=code, reason=reason,
            step_id=step_id, capability_ref=capability_ref, goal=goal,
            frame_url=frame_url, evidence=evidence,
        )
        self.escalations.append(esc)
        self._gate.clear()
        self._transition(State.PAUSED, Owner.NOBODY, "automation", reason)
        self._resumed = asyncio.get_running_loop().create_future()
        await self._resumed
        return esc

    def take_control(self, operator: str) -> Escalation:
        if self.state is not State.PAUSED:
            raise RuntimeError(f"cannot take control from {self.state.value}")
        esc = self.escalations[-1]
        esc.operator = operator
        self._transition(State.HUMAN, Owner.HUMAN, operator, "operator took the session")
        return esc

    def record_human_action(self, action: dict[str, Any]) -> None:
        """What the person actually did, into the same audit trail as the
        automated steps. A handoff nobody can review afterwards is not a
        control, it is a gap."""
        if self.escalations:
            self.escalations[-1].human_actions.append({"at": _now(), **action})

    def hand_back(self, decision: Resume, operator: str | None = None) -> Resume:
        if self.state not in (State.HUMAN, State.PAUSED):
            raise RuntimeError(f"cannot hand back from {self.state.value}")
        esc = self.escalations[-1]
        esc.decision, esc.resolved_at = decision, _now()
        if operator:
            esc.operator = operator
        if decision is Resume.ABORT:
            self._transition(State.ABORTED, Owner.NOBODY, operator or "operator", "aborted")
        else:
            self._transition(State.AUTOMATION, Owner.AUTOMATION,
                             operator or "operator", f"resumed: {decision.value}")
            self._gate.set()
        if self._resumed and not self._resumed.done():
            self._resumed.set_result(decision)
        if decision is Resume.ABORT:
            self._gate.set()      # unblock the waiter so it can raise and stop
        return decision

    def finish(self) -> None:
        if self.state is State.AUTOMATION:
            self._transition(State.DONE, Owner.NOBODY, "automation", "run complete")

    # -- reporting ------------------------------------------------------

    def report(self) -> dict[str, Any]:
        return {
            "escalated": bool(self.escalations),
            "escalations": len(self.escalations),
            "human_actions": sum(len(e.human_actions) for e in self.escalations),
            "final_owner": self.owner.value,
        }
