"""Planners: three tiers, tried in order of cost.

    heuristic  ->  LLM  ->  human

The point is not that the model is bad. It is that most steps in a back-office
flow are not decisions at all -- typing a member id into the field labelled
"Member ID" is pattern matching, and paying a model to re-derive it on every
capability is spending money to be slower and less predictable.

So the heuristic goes first and is allowed to be *narrow*. Every rule it has is
guarded by "exactly one": exactly one field matches this parameter, exactly one
button is on this form. The moment a screen is ambiguous the heuristic declines
and the model decides. That escalation is the design -- a heuristic that guesses
under ambiguity is worse than no heuristic, because it is confidently wrong.

Whether the assumption behind the heuristic actually holds is measurable: every
step records which tier decided it.
"""

from __future__ import annotations

import json
import os
import re
from collections import deque
from typing import Any, Protocol

from ..surface.controls import Control, Observation
from .tools import PLAN_SCHEMA, Planned

_WORD = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> frozenset[str]:
    return frozenset(_WORD.findall((text or "").lower()))


class Planner(Protocol):
    name: str

    async def decide(self, goal: str, obs: Observation, context: dict[str, Any],
                     history: list[str]) -> Planned | None:
        ...


# ---------------------------------------------------------------------------
# Tier 1
# ---------------------------------------------------------------------------


class HeuristicPlanner:
    """Deterministic rules over the same screen the model would see.

    Returns None -- meaning "I decline" -- rather than guessing. Every caller
    treats None as "escalate", so declining is always safe and never silently
    stalls the run.
    """

    name = "heuristic"

    def _content_frame(self, obs: Observation) -> tuple[str, ...]:
        """Rules apply to the frame the operator is working in.

        A frameset's navigation frame is full of links that look like plausible
        next steps and never are. Delegated to Observation so there is exactly
        one answer to this question in the system.
        """
        return obs.content_frame_path()

    async def decide(self, goal: str, obs: Observation, context: dict[str, Any],
                     history: list[str]) -> Planned | None:
        params: dict[str, Any] = context["params"]
        remaining: list[str] = context["outputs_remaining"]
        done: set[str] = context["consumed"]
        frame = self._content_frame(obs)

        here = [c for c in obs.controls if c.frame_path == frame]
        reads = [r for r in obs.readouts if r.frame_path == frame]

        # R1 -- fill a field whose name contains this parameter's words.
        #       "deposit" matches "Opening Deposit"; it does not match
        #       "Shareholder Number", and that is the point: a tenant that
        #       renames its fields drops through to the model rather than
        #       being typed into the wrong box.
        # No "already filled this" bookkeeping: the empty-value check below is
        # the real guard, and it is correct even when a flow doubles back to a
        # screen it has already visited. Remembering across screens made the
        # loop unable to recover from its own wrong turn.
        for key, value in params.items():
            fields = [c for c in here
                      if c.role in ("textbox", "searchbox", "spinbutton")
                      and tokens(key) and tokens(key) <= tokens(c.name)]
            if len(fields) == 1 and not fields[0].value:
                return Planned("fill", ref=fields[0].ref, value=str(value),
                               intent=f"enter the {key.replace('_', ' ')}",
                               rationale=f"exactly one field matches {key!r}", tier=self.name)

        # R2 -- a results row whose text is exactly a parameter value. This is
        #       how the unnamed <td onclick> gets opened without a model.
        for key, value in params.items():
            rows = [c for c in here if c.via != "semantic" and c.name.strip() == str(value)]
            if len(rows) == 1:
                return Planned("click", ref=rows[0].ref,
                               intent=f"open the record for {value}",
                               rationale=f"one row matches the {key!r} value exactly",
                               tier=self.name)

        # R3 -- capture a declared output that is now visible.
        for want in list(remaining):
            hits = [r for r in reads if tokens(want) and tokens(want) <= tokens(r.label)]
            if len(hits) == 1:
                return Planned("read", ref=hits[0].ref, into=want,
                               intent=f"read the {want.replace('_', ' ')}",
                               rationale=f"one readout is labelled like {want!r}",
                               tier=self.name)

        # R4 -- one link on this screen whose words appear in the goal.
        # Link suppression is scoped to the screen it happened on. A link is
        # only "already tried" here, not everywhere -- the same words can name a
        # different destination two pages later.
        screen = obs.content_url()
        links = [c for c in here if c.role == "link"
                 and (tokens(c.name) & tokens(goal))
                 and f"{screen}|link:{c.name}" not in done]
        if len(links) == 1:
            return Planned("click", ref=links[0].ref,
                           intent=links[0].name.lower(),
                           rationale="the only link on this screen the goal mentions",
                           tier=self.name)

        # R5 -- one button, and nothing left to type into. Deliberately last:
        #       submitting before the form is filled is the expensive mistake.
        empty = [c for c in here if c.role in ("textbox", "searchbox", "spinbutton")
                 and not c.value]
        buttons = [c for c in here if c.role == "button"]
        if len(buttons) == 1 and not empty:
            return Planned("click", ref=buttons[0].ref, intent=buttons[0].name.lower(),
                           rationale="the only button, and every field is filled",
                           tier=self.name)

        # R6 -- everything asked for has been captured.
        if not remaining and context.get("acted"):
            return Planned("finish", intent="goal reached",
                           rationale="every declared output has been captured",
                           tier=self.name)

        return None    # decline -> escalate


# ---------------------------------------------------------------------------
# Tier 2
# ---------------------------------------------------------------------------

SYSTEM = """\
You operate a legacy back-office web application through its user interface, \
one action per turn, to accomplish a goal.

You are shown a list of CONTROLS and READOUTS with reference ids in [brackets]. \
Act only on those references. You never write CSS selectors, XPath, or any other \
markup detail -- reference ids are the only way to name something, and the system \
converts them into durable locators for you.

Rules:
- One action per turn. Choose the single next step that makes progress.
- Only use a [ref] that appears in the CURRENT observation. They change every turn.
- Prefer filling every field on a form before submitting it.
- A control marked "clickable" is a row or cell that acts like a button; those are \
how legacy tables open records.
- Call read for each declared output once its value is visible in READOUTS. Set ref to that readout's id and into to the output name. A read without a ref does nothing.
- Call finish only when every declared output has been captured.
- Call give_up if the screen shows an error or dead end you cannot act on. Do not \
repeat an action that already failed to change the screen.
"""


class LLMPlanner:
    """OpenAI, in strict structured-output mode.

    Structured outputs rather than prompt-and-parse: the action shape is enforced
    by the API, so an unparseable turn is not a failure mode we have to write
    recovery code for. That also means the model's job is purely the decision,
    which is the only part worth paying for.
    """

    name = "llm"

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 history_window: int = 8) -> None:
        from openai import OpenAI

        self.model = model or os.environ.get("AIHANDS_PLANNER_MODEL", "gpt-4o-mini")
        self._client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))
        # A bounded window, not the whole transcript. Discovery history grows
        # linearly, so unbounded context makes both memory and cost grow with
        # run length -- for a loop whose whole purpose is to be cheap, that is
        # the wrong shape. The recent turns are what disambiguate the screen.
        self.window = history_window
        self.calls = 0

    async def decide(self, goal: str, obs: Observation, context: dict[str, Any],
                     history: list[str]) -> Planned | None:
        recent = list(deque(history, maxlen=self.window))
        user = "\n\n".join(filter(None, [
            f"GOAL: {goal}",
            f"PARAMETERS: {json.dumps(context['params'], default=str)}",
            f"DECLARED OUTPUTS STILL NEEDED: {context['outputs_remaining']}",
            ("RECENT TURNS:\n" + "\n".join(recent)) if recent else "",
            (f"WHY YOU ARE BEING ASKED: {context['escalation_reason']}"
             if context.get("escalation_reason") else ""),
            obs.render(),
        ]))

        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {
                "name": "next_action", "strict": True, "schema": PLAN_SCHEMA}},
            temperature=0,
        )
        self.calls += 1
        data = json.loads(response.choices[0].message.content)
        return Planned(tool=data["tool"], ref=data["ref"], value=data["value"],
                       into=data["into"], intent=data["intent"],
                       rationale=data["rationale"], tier=self.name)


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


class LadderPlanner:
    """Try the cheap tier, fall through to the expensive one.

    The escalation reason is passed to the model, so it is told what the rules
    could not settle instead of starting from nothing. That turns a fallback
    into a handover.
    """

    name = "ladder"

    def __init__(self, llm: LLMPlanner | None) -> None:
        self.heuristic = HeuristicPlanner()
        self.llm = llm

    @property
    def model(self) -> str | None:
        return self.llm.model if self.llm else None

    @property
    def calls(self) -> int:
        return self.llm.calls if self.llm else 0

    async def decide(self, goal: str, obs: Observation, context: dict[str, Any],
                     history: list[str]) -> Planned | None:
        plan = await self.heuristic.decide(goal, obs, context, history)
        if plan is not None:
            return plan
        if self.llm is None:
            return None
        context = {**context,
                   "escalation_reason": "the deterministic rules found no unambiguous "
                                        "next action on this screen"}
        return await self.llm.decide(goal, obs, context, history)
