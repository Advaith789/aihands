"""The vocabulary a planner may speak.

Five verbs, and none of them is "write a selector". A planner names a control by
the reference it was just shown and says what it wants in human terms; the loop
is what turns that into a durable locator by measuring candidates against the
real page.

That division is the reason a model's guess about markup never reaches an
artifact. A model asked for a CSS selector will happily invent one that works
this afternoon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Tool = Literal["fill", "click", "read", "finish", "give_up"]

#: The JSON contract the model is held to. Enforced server-side by structured
#: outputs, so a malformed action is impossible rather than merely unlikely.
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tool", "ref", "value", "into", "intent", "rationale"],
    "properties": {
        "tool": {
            "type": "string",
            "enum": ["fill", "click", "read", "finish", "give_up"],
            "description": "fill/click act on a control; read captures a value; "
                           "finish when the goal is met; give_up when stuck.",
        },
        "ref": {
            "type": "string",
            "description": "The reference id of the control or readout to act on, "
                           "e.g. f2:c3 -- the text inside the square brackets in "
                           "the CONTROLS or READOUTS list, without the brackets. "
                           "Empty string for finish and give_up.",
        },
        "value": {
            "type": "string",
            "description": "Text to type, for fill. Empty otherwise.",
        },
        "into": {
            "type": "string",
            "description": "Which declared output this read populates. Empty otherwise.",
        },
        "intent": {
            "type": "string",
            "description": "One short sentence a human reviewer would read, "
                           "describing what this step accomplishes.",
        },
        "rationale": {
            "type": "string",
            "description": "Why this action, given the goal and what is on screen.",
        },
    },
}


@dataclass
class Planned:
    tool: Tool
    ref: str = ""
    value: str = ""
    into: str = ""
    intent: str = ""
    rationale: str = ""
    #: Which tier decided this. Recorded per step so the artifact can report
    #: how much of the flow needed a model at all -- the cost story, measured.
    tier: str = "heuristic"

    def signature(self, url: str) -> str:
        """Identifies a repeated no-op. A planner that keeps issuing the same
        action on the same screen is stuck, and saying so early is cheaper than
        burning the turn budget in silence."""
        return f"{url}|{self.tool}|{self.ref}|{self.value}"


@dataclass
class ProbedLocator:
    locator: dict[str, Any]
    matched_at_record: int
    reverified: bool | None = None


@dataclass
class TraceEntry:
    """What actually happened on one turn.

    The trace is written by the executor, not the planner: it records the action
    that ran and what the page did, so a capability distilled from it can only
    ever describe steps that really executed.
    """

    seq: int
    tool: str
    tier: str
    intent: str
    rationale: str
    ref: str = ""
    value: str = ""
    into: str = ""
    url_before: str = ""
    url_after: str = ""
    frame_path: tuple[str, ...] = ()
    #: Which frame `url_after` was read from. Not the same as `frame_path`:
    #: clicking Sign In on a plain page lands you in a frameset, so the control
    #: lived in the top document and the resulting URL belongs to a child frame.
    #: A checkpoint that confuses the two asserts the right URL against the
    #: wrong document and fails every time.
    content_frame_after: tuple[str, ...] = ()
    control: dict[str, Any] | None = None
    probes: list[ProbedLocator] = field(default_factory=list)
    read_value: Any = None
    #: A distinctive label present on the screen the action produced. The url
    #: says where we are; this says what we are looking at.
    landmark_after: str = ""
    committed: bool = False
    status: str = "ok"
    error: str | None = None


@dataclass
class DiscoveryTrace:
    goal: str
    entry_url: str
    planner: str
    model: str | None
    params: dict[str, Any]
    declared_outputs: list[str]
    tenant: str = ""
    entries: list[TraceEntry] = field(default_factory=list)
    status: str = "incomplete"
    summary: str = ""
    llm_calls: int = 0

    def tier_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.entries:
            if e.status == "ok":
                counts[e.tier] = counts.get(e.tier, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)
