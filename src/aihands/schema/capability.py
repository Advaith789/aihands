"""The capability artifact.

A capability is the durable, typed, versioned answer to "how do I do this job
through this application's user interface". It is not a transcript of what a
model said, and it is not a Playwright script.

Four rules shape the whole file:

1. **Nothing here imports a browser.** A capability describes intent and
   identification, never DOM calls. That is what allows a different surface --
   an accessibility-tree driver, a desktop UIA driver -- to execute the same
   artifact without the schema changing.

2. **Substitution is a closed grammar.** Only ``{{ input.name }}`` resolves.
   There is no expression evaluation, so loading an artifact can never execute
   logic. An artifact is data that arrived from a model; it is not code.

3. **Outcomes are a sibling of steps, not a catch block.** What the application
   might legitimately answer is part of the contract a caller reads, not an
   implementation detail of the engine.

4. **Approval binds to a hash of the steps.** Otherwise "approved" is a sticker
   that survives someone changing what it was stuck to.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .outcomes import CATEGORY, Category, Code

SCHEMA_VERSION = "1.0.0"

#: The entire substitution grammar. Deliberately not a template engine.
BINDING = re.compile(r"\{\{\s*input\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Targeting
# ---------------------------------------------------------------------------


class RoleNameLocator(Base):
    """Accessibility role plus accessible name. The first choice: it is what the
    application already publishes for screen readers, so it survives restyling
    and is the same concept on a desktop accessibility API."""

    strategy: Literal["role_name"] = "role_name"
    role: str
    name: str
    exact: bool = True
    confidence: float = 0.90


class LabelLocator(Base):
    """A form control identified by its visible label. Nearly as durable as
    role+name and often more readable in the artifact."""

    strategy: Literal["label"] = "label"
    text: str
    confidence: float = 0.85


class ColumnTextLocator(Base):
    """A cell identified by its text and the column it sits under.

    This exists because legacy result tables are full of controls the page never
    named -- a <td> with a click handler and nothing else. The column header is
    what a human reads to know what the cell means, it is stable across
    restyling and re-sorting, and it is the only durable handle such an element
    has. Without this strategy those rows can only be addressed positionally,
    which breaks the moment a row is inserted."""

    strategy: Literal["column_text"] = "column_text"
    text: str
    column: str
    confidence: float = 0.80


class RowLabelLocator(Base):
    """The value cell of a `Label | Value` row.

    Back-office record views are built almost entirely out of two-column rows:
    "Confirmation Number | SAV-88200", "Status | frozen". The value has no id,
    no class worth trusting and no accessible name -- but the label beside it is
    the most stable thing on the screen, because it is what the operator reads.
    Addressing the value positionally breaks the moment a row is inserted;
    addressing it by its label does not."""

    strategy: Literal["row_label"] = "row_label"
    label: str
    confidence: float = 0.85


class LinkTextLocator(Base):
    strategy: Literal["link_text"] = "link_text"
    text: str
    confidence: float = 0.75


class CssLocator(Base):
    """Last resort, recorded so replay degrades instead of dying. Marked low
    confidence so it is never chosen while anything better still resolves."""

    strategy: Literal["css"] = "css"
    value: str
    confidence: float = 0.30


Locator = Annotated[
    Union[RoleNameLocator, LabelLocator, ColumnTextLocator, RowLabelLocator,
          LinkTextLocator, CssLocator],
    Field(discriminator="strategy"),
]


class ProbeResult(Base):
    """What we measured about a locator at record time.

    Recording a locator we never tried is a guess. These fields are the
    difference between "this should work" and "this resolved to exactly one
    element on the real page, and still did after the page changed".
    """

    matched_at_record: int
    #: Re-measured after the action ran. None when the action navigated away --
    #: the element genuinely no longer exists, and claiming we verified it
    #: would be inventing evidence. Only non-navigating actions can be
    #: re-verified in place, so this is honest about what was actually checked.
    reverified: bool | None = None


class Target(Base):
    """How to find one control, best candidate first."""

    candidates: list[Locator] = Field(min_length=1)
    frame_path: tuple[str, ...] = ()
    probe: ProbeResult | None = None
    # Ambiguity is a defect in the locator, not a transient condition. Picking
    # the first of three matches in a banking UI is how you action the wrong
    # member's account, so the default refuses.
    on_ambiguous: Literal["fail", "first"] = "fail"
    describe: str = ""


# ---------------------------------------------------------------------------
# Conditions -- used for checkpoints and for outcome detection alike
# ---------------------------------------------------------------------------


class Condition(Base):
    kind: Literal[
        "text_present", "text_absent",
        "control_present", "control_absent",
        "frame_url_matches",
        # The field now holds what we typed. Typing navigates nowhere, so a
        # url-derived checkpoint cannot cover a fill -- which left half the
        # steps in a flow unverified, including every one that supplies a
        # parameter. This is the cheapest possible assertion: we already know
        # what we typed and the box will tell us what it holds.
        "control_value_equals",
        # The status the server returned for this frame's document. A 503 means
        # the same thing on every application, in every language, at every
        # tenant; the words on the error page do not.
        "frame_status_is",
    ]
    value: str | None = None
    target: Target | None = None
    # Which frame to look in. Framesets make the top-level URL meaningless, so
    # a URL assertion has to name the frame it means.
    frame_path: tuple[str, ...] = ()

    @field_validator("value")
    @classmethod
    def _regex_compiles(cls, v: str | None, info):
        if v and info.data.get("kind") == "frame_url_matches":
            re.compile(v)
        return v


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class Navigate(Base):
    type: Literal["navigate"] = "navigate"
    url: str


class Click(Base):
    type: Literal["click"] = "click"


class Fill(Base):
    type: Literal["fill"] = "fill"
    value: str          # may carry {{ input.x }}


class Read(Base):
    type: Literal["read"] = "read"
    into: str           # the output name this populates
    extract: str | None = None   # optional regex, first group wins


class Wait(Base):
    type: Literal["wait"] = "wait"
    until: Condition
    timeout_ms: int = 8000


Action = Annotated[
    Union[Navigate, Click, Fill, Read, Wait], Field(discriminator="type")
]

#: Whether an action *could* commit a change is not knowable from its type
#: alone. Typing into a field changes nothing; clicking might navigate or might
#: post a new account. So commitment is recorded per step (`Step.commits`) by
#: whoever observed what the action actually did, and these two sets only say
#: which actions are candidates.
POSSIBLY_COMMITTING = frozenset({"click"})
READ_ONLY_ACTIONS = frozenset({"navigate", "read", "wait", "fill"})


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


class Retry(Base):
    max_attempts: int = 2
    backoff_ms: int = 300


class Step(Base):
    id: str
    intent: str                    # human sentence; what a reviewer reads
    action: Action
    target: Target | None = None
    require: Condition | None = None       # asserted before acting
    #: Asserted after acting. A conjunction, because one condition is often not
    #: enough to tell success from a lookalike: a form POST can render the
    #: receipt OR an approval interstitial at the SAME url, so a url-only
    #: checkpoint passes for both and the run continues believing it finished.
    expect: tuple[Condition, ...] = ()
    retry: Retry | None = None
    risk: Literal["safe", "risky"] = "safe"
    timeout_ms: int = 8000

    #: Did this step change the institution's state? Two different things
    #: depend on the answer and neither can use `risk` instead:
    #:
    #:   * approval -- a capability that commits nothing needs no signature;
    #:   * restart safety -- once something has been committed we cannot tell
    #:     from the client whether it landed, so replaying from the top could
    #:     do it twice.
    #:
    #: `risk` is a policy question ("should a human bless this?"). `commits` is
    #: a physical one ("can this be safely repeated?"). A read-only click on a
    #: link is neither; a submit is both.
    commits: bool = False

    @property
    def mutating(self) -> bool:
        return self.commits

    @property
    def blocks_restart(self) -> bool:
        """Whether replaying from step one could repeat this step harmfully.

        `commits` is read off the wire and answers "did this POST". That alone
        is too blunt: signing in POSTs, and treating it as unrepeatable would
        make the restart path dead for every capability that has a login.

        So the second half of the test is the operator's own risk policy. If
        the deployment's patterns call a step risky, it is a step the
        institution considers state-changing, and repeating it needs a person.
        That puts the judgement with the human who wrote the policy rather than
        with our inference about HTTP verbs.
        """
        return self.commits and self.risk == "risky"


# ---------------------------------------------------------------------------
# Contract: what goes in, what comes out, what might happen instead
# ---------------------------------------------------------------------------


class InputSpec(Base):
    name: str
    type: Literal["string", "number", "boolean"] = "string"
    required: bool = True
    pattern: str | None = None
    description: str = ""
    example: Any = None
    # Marked values never reach an artifact, a log or a screenshot in the clear.
    sensitive: bool = False


class OutputSpec(Base):
    name: str
    type: Literal["string", "number", "boolean"] = "string"
    required: bool = True
    description: str = ""
    sensitive: bool = False


class KnownOutcome(Base):
    """Something the application may legitimately answer instead of succeeding.

    Declared in the artifact rather than detected by heuristics in the engine,
    because what counts as a business answer is a property of the application,
    not of our runtime.
    """

    name: str
    code: Code
    detect: Condition
    message: str = ""
    #: What to do before trying again, for RECOVERABLE outcomes. Re-running the
    #: same action is usually wrong: after a transient 503 the control we
    #: clicked is no longer on screen, so "click it again" resolves nothing.
    #: Reloading the frame is what a person would do, and it is the only
    #: remediation that is always safe -- it repeats a GET, never a commit.
    remediate: Literal["none", "reload"] = "none"
    max_attempts: int = 2
    # Category is derived from the code. A capability may narrow a code to a
    # stricter category, never loosen one into SUCCESS.
    category_override: Category | None = None

    @property
    def category(self) -> Category:
        return self.category_override or CATEGORY[self.code]


# ---------------------------------------------------------------------------
# Safety, recovery, provenance
# ---------------------------------------------------------------------------


class Safety(Base):
    """Per-capability constraints. These may only ever be narrower than the
    deployment policy -- a capability cannot grant itself permission the
    operator did not."""

    allowed_hosts: tuple[str, ...] = ()
    max_steps: int = 40


class Recovery(Base):
    global_max_retries: int = 4
    allow_restart: bool = True
    #: A restart replays from step one. That is safe only while nothing has been
    #: committed: once a mutating step has run we cannot tell from the client
    #: whether it landed, and replaying it could open a second account. Past
    #: that point the only correct move is a human.
    restart_only_before_first_mutation: bool = True


class Provenance(Base):
    recorded_at: str
    planner: str                    # "ladder" | "llm_only" | "heuristic_only"
    model: str | None = None
    run_id: str = ""
    tenant: str = ""
    #: How many steps each tier decided. The cost story, measured rather than
    #: asserted.
    steps_by_tier: dict[str, int] = Field(default_factory=dict)


class Approval(Base):
    status: Literal["draft", "approved"] = "draft"
    approved_by: str | None = None
    approved_at: str | None = None
    #: The hash the signature covers. If the steps change, this no longer
    #: matches and the capability silently reverts to draft.
    approved_steps_hash: str | None = None
    #: Set when this artifact is a tenant specialisation of an approved base.
    #:
    #: An overlay may only ADD locator candidates -- it cannot change a step, an
    #: action, an input or an output -- and it is written by an operator, not by
    #: a model, which puts it on the same trust boundary as policy.yaml. So the
    #: behaviour a reviewer signed is provably unchanged and the approval
    #: carries over. Recording which overlay produced it keeps that inheritance
    #: auditable instead of invisible.
    derived_via_overlay: str | None = None


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


class Capability(Base):
    schema_version: str = SCHEMA_VERSION
    capability_id: str
    version: str = "1.0.0"
    name: str
    description: str

    entry_url: str
    inputs: tuple[InputSpec, ...] = ()
    outputs: tuple[OutputSpec, ...] = ()
    steps: tuple[Step, ...] = Field(min_length=1)
    outcomes: tuple[KnownOutcome, ...] = ()

    safety: Safety = Safety()
    recovery: Recovery = Recovery()
    approval: Approval = Approval()
    provenance: Provenance | None = None

    # -- identity -------------------------------------------------------

    @property
    def ref(self) -> str:
        return f"{self.capability_id}@{self.version}"

    def steps_hash(self) -> str:
        """Covers everything a reviewer would have read: the steps, the inputs
        they take and the outputs they promise. Not provenance, not the
        approval block itself, and not the description -- rewording a sentence
        should not invalidate a signature, but changing a click target must."""
        payload = {
            "steps": [s.model_dump(mode="json") for s in self.steps],
            "inputs": [i.model_dump(mode="json") for i in self.inputs],
            "outputs": [o.model_dump(mode="json") for o in self.outputs],
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    # -- safety ---------------------------------------------------------

    @property
    def mutating(self) -> bool:
        return any(s.commits for s in self.steps)

    @property
    def first_mutation_index(self) -> int | None:
        """Index of the first step that a restart must not repeat. Beyond this
        point, replaying from the top could act twice."""
        return next((i for i, s in enumerate(self.steps) if s.blocks_restart), None)

    def is_approved(self) -> bool:
        """Approved *and* unchanged since. Both halves matter: the second is
        what makes the first mean anything."""
        return (
            self.approval.status == "approved"
            and self.approval.approved_steps_hash == self.steps_hash()
        )

    def requires_approval(self) -> bool:
        """Read-only capabilities replay freely. Anything that can change the
        institution's state needs a human signature first -- maker-checker,
        which is how these institutions already work."""
        return self.mutating

    # -- contract -------------------------------------------------------

    def input_schema(self) -> dict[str, Any]:
        """JSON Schema for the parameters. This is what makes the artifact
        directly usable as an agent tool definition rather than something a
        wrapper has to describe by hand."""
        props: dict[str, Any] = {}
        required: list[str] = []
        for spec in self.inputs:
            entry: dict[str, Any] = {"type": spec.type}
            if spec.description:
                entry["description"] = spec.description
            if spec.pattern:
                entry["pattern"] = spec.pattern
            if spec.example is not None:
                entry["examples"] = [spec.example]
            props[spec.name] = entry
            if spec.required:
                required.append(spec.name)
        return {
            "type": "object", "properties": props,
            "required": required, "additionalProperties": False,
        }

    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {o.name: {"type": o.type, "description": o.description}
                           for o in self.outputs},
            "required": [o.name for o in self.outputs if o.required],
            "additionalProperties": False,
        }

    def sensitive_inputs(self) -> frozenset[str]:
        return frozenset(i.name for i in self.inputs if i.sensitive)

    def declared_bindings(self) -> set[str]:
        """Every {{ input.x }} the steps reference. Used to prove the artifact
        cannot ask for a parameter it never declared."""
        found: set[str] = set()
        for step in self.steps:
            blob = json.dumps(step.model_dump(mode="json"))
            found.update(BINDING.findall(blob))
        return found
