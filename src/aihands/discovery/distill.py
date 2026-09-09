"""Distil one recorded run into one reusable capability.

A separate stage, and a pure function. Two things follow from that, and both
are worth the extra module:

  * We can improve how locators are chosen and **re-distil last month's runs**
    without re-running a browser or paying a model again.
  * It is testable without either. Deciding whether an artifact is any good is
    the most important judgement in this system, and it should not require a
    live page to exercise.

What it does, in order: keep only the locators that were measured to be
unambiguous, turn the literals that came from parameters back into bindings,
derive a checkpoint from what the page actually did, and refuse to emit a
capability that could never verify itself.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..kernel.policy import Policy
from ..schema.capability import (
    Approval,
    Capability,
    Click,
    Condition,
    Fill,
    InputSpec,
    KnownOutcome,
    OutputSpec,
    ProbeResult,
    Provenance,
    Read,
    Recovery,
    Retry,
    Safety,
    Step,
    Target,
)
from .tools import DiscoveryTrace, TraceEntry

OUTCOMES_DIR = Path("capabilities/outcomes")


class DistillRefused(Exception):
    """The run did not produce something worth saving."""


# ---------------------------------------------------------------------------


def _parameterise(value: Any, params: dict[str, Any]) -> Any:
    """Turn a recorded literal back into the parameter it came from.

    This is what makes the artifact a capability rather than a macro. Recording
    "M-1001" produces something that can only ever open one member's record;
    recording {{ input.member_id }} produces something an agent can call.
    Longest values first, so a short value that happens to be a substring of a
    longer one cannot shadow it.
    """
    if not isinstance(value, str):
        return value
    for key, raw in sorted(params.items(), key=lambda kv: -len(str(kv[1]))):
        literal = str(raw)
        if literal and literal in value:
            value = value.replace(literal, f"{{{{ input.{key} }}}}")
    return value


def _parameterise_deep(node: Any, params: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        return {k: _parameterise_deep(v, params) for k, v in node.items()}
    if isinstance(node, list):
        return [_parameterise_deep(v, params) for v in node]
    return _parameterise(node, params)


def _durable(entry: TraceEntry, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep only locators measured to resolve to exactly one element.

    A candidate that matched three things at record time was never going to
    work on replay. Dropping it here rather than letting replay discover it is
    the whole reason probing exists. Best confidence first; a re-verified
    candidate wins a tie because it survived the page changing under it.
    """
    survivors = [p for p in entry.probes if p.matched_at_record == 1]
    survivors.sort(key=lambda p: (-(p.locator.get("confidence", 0.0)),
                                  0 if p.reverified else 1))
    return [_parameterise_deep(p.locator, params) for p in survivors]


def _url_checkpoint(entry: TraceEntry, params: dict[str, Any],
                    frame_path: tuple[str, ...]) -> Condition | None:
    """A checkpoint derived from what the page actually did.

    Only the path, with parameter values replaced by a wildcard. Keeping the
    host out is what lets the same capability run against a second tenant on a
    different origin; keeping the values out is what lets it run for a
    different member.
    """
    if not entry.url_after or entry.url_after == entry.url_before:
        return None
    path = urlparse(entry.url_after).path
    if not path or path == "/":
        return None
    for raw in sorted((str(v) for v in params.values()), key=len, reverse=True):
        if raw and raw in path:
            path = path.replace(raw, "\x00")
    pattern = re.escape(path).replace("\x00", "[^/?&]+")
    return Condition(kind="frame_url_matches", value=pattern + r"(\?|$)",
                     frame_path=frame_path)


def _output_description(trace: DiscoveryTrace, name: str) -> str:
    """Describe an output by the label it was actually read from, so a caller
    reading the catalogue sees the institution's own wording."""
    for entry in trace.entries:
        if entry.tool == "read" and entry.into == name and entry.status == "ok":
            label = (entry.control or {}).get("label") or (entry.control or {}).get("name")
            if label:
                return f"{label}, read from the confirmation screen"
    return ""


def _checkpoints(entry: TraceEntry, params: dict[str, Any],
                 frame_path: tuple[str, ...]) -> tuple[Condition, ...]:
    """What must be true for this step to have worked.

    The url alone is not enough for anything that posts. Submitting the
    sub-account form renders either the receipt or a "second approval required"
    interstitial, and both live at the same url -- so a url check passes for
    both and the run walks on believing it succeeded. Pairing it with something
    only the successful screen shows is what makes the assertion mean anything.
    """
    conditions: list[Condition] = []
    url = _url_checkpoint(entry, params, frame_path)
    if url is not None:
        conditions.append(url)
    if entry.committed and entry.landmark_after:
        conditions.append(Condition(kind="text_present", value=entry.landmark_after,
                                    frame_path=frame_path))
    return tuple(conditions)


def _fill_checkpoint(step_target: Target, value: str,
                     frame_path: tuple[str, ...]) -> Condition:
    """A fill navigates nowhere, so nothing about the url can confirm it worked.

    Without this, every step that supplies a parameter was unverified -- replay
    would type a member id into a box that silently rejected it and carry on
    searching for nothing. Asserting the box now holds what we typed costs one
    read and closes that gap.
    """
    return Condition(kind="control_value_equals", target=step_target, value=value,
                     frame_path=frame_path)


# ---------------------------------------------------------------------------
# Inferring the input contract
# ---------------------------------------------------------------------------

_SECRET_WORDS = re.compile(r"(?i)\b(password|passcode|pin|secret|token|ssn|security)\b")


def _shape(value: str) -> str | None:
    r"""Generalise a recorded value into the pattern of values like it.

    "M-1001" describes one member. `M-\d{4}` describes the shape every member id
    on this system has, which is what a caller needs in order to know what is
    valid before spending a browser session finding out. Only emitted for values
    that actually have a structure -- a free-text note has no shape worth
    asserting, and a wrong pattern is worse than none because it rejects valid
    input.
    """
    parts, structured = [], False
    for run in re.findall(r"[A-Za-z]+|\d+|[^A-Za-z\d]+", value):
        if run.isdigit():
            # \d+ rather than \d{4}. We have exactly one example; inferring an
            # exact length from it is overfitting, and a pattern that rejects a
            # valid member id is worse than no pattern at all.
            parts.append(r"\d+")
            structured = True
        elif run.isalpha():
            # Letters are held literally: an id prefix like "M-" or "CLM-" is
            # part of the format, not a variable field.
            parts.append(re.escape(run))
        else:
            parts.append(re.escape(run))
            structured = True
    if not structured or len(value) > 24:
        return None
    if not any(c.isdigit() for c in value):
        return None
    return "".join(parts)


def _infer_inputs(trace: DiscoveryTrace) -> dict[str, dict[str, Any]]:
    """Work out the parameter contract from what the run actually did.

    Everything here is read off the recording rather than declared by hand:
    which screen a value was typed on, what the field was called, what the
    value looked like. The caller can still override any of it, but the default
    should not be "untyped string with no description".
    """
    entry_screen = urlparse(trace.entry_url).path
    inferred: dict[str, dict[str, Any]] = {}

    for name, value in trace.params.items():
        text = str(value)
        spec: dict[str, Any] = {}

        used = next((e for e in trace.entries
                     if e.tool == "fill" and e.status == "ok" and text in str(e.value)), None)

        # A value typed on the entry screen, before the flow has gone anywhere,
        # is a credential -- that screen is the sign-in screen by definition.
        # This is why an operator id is treated as sensitive without anyone
        # having to remember to say so.
        on_entry = bool(used and urlparse(used.url_before).path == entry_screen)
        field_type = (used.control or {}).get("type", "") if used else ""
        field_name = (used.control or {}).get("name", "") if used else ""
        if field_type == "password" or on_entry or _SECRET_WORDS.search(f"{name} {field_name}"):
            spec["sensitive"] = True

        if re.fullmatch(r"-?\d+(\.\d+)?", text.replace(",", "").lstrip("$")):
            spec["type"] = "number"
        elif not spec.get("sensitive"):
            # A pattern on a credential would leak its shape into the artifact,
            # which is the one place a secret's shape should not be written down.
            pattern = _shape(text)
            if pattern:
                spec["pattern"] = pattern

        if used and used.intent:
            spec["description"] = used.intent[0].upper() + used.intent[1:]
        inferred[name] = spec
    return inferred


def _load_outcomes(app: str) -> tuple[list[KnownOutcome], tuple[str, ...]]:
    path = OUTCOMES_DIR / f"{app}.json"
    if not path.exists():
        return [], ()
    blob = json.loads(path.read_text())
    frame = tuple(blob.get("frame_path", ()))
    outcomes = []
    for raw in blob["outcomes"]:
        detect = dict(raw["detect"])
        detect.setdefault("frame_path", frame)
        outcomes.append(KnownOutcome.model_validate({**raw, "detect": detect}))
    return outcomes, frame


# ---------------------------------------------------------------------------


def distill_capability(
    trace: DiscoveryTrace, *, capability_id: str, name: str, description: str,
    policy: Policy, app: str = "mendota", version: str = "1.0.0",
    input_specs: dict[str, dict[str, Any]] | None = None,
) -> Capability:
    if trace.status != "complete":
        raise DistillRefused(
            f"run did not complete ({trace.status}: {trace.summary}); "
            "a capability is only ever distilled from a successful run")

    usable = [e for e in trace.entries if e.status == "ok"]
    if not usable:
        raise DistillRefused("the run recorded no successful actions")

    params = trace.params
    steps: list[Step] = []
    reads: list[str] = []

    for index, entry in enumerate(usable, start=1):
        candidates = _durable(entry, params)
        if not candidates and entry.tool != "finish":
            raise DistillRefused(
                f"turn {entry.seq} ({entry.intent!r}) produced no locator that "
                "resolved to exactly one element; refusing to record a step "
                "replay could not reliably repeat")

        frame = tuple(entry.frame_path)
        target = Target(candidates=candidates, frame_path=frame,
                        describe=entry.intent,
                        probe=ProbeResult(
                            matched_at_record=1,
                            reverified=next((p.reverified for p in entry.probes
                                             if p.matched_at_record == 1), None)))

        if entry.tool == "fill":
            bound_value = _parameterise(entry.value, params)
            action: Any = Fill(value=bound_value)
        elif entry.tool == "click":
            action = Click()
        else:
            action = Read(into=entry.into)
            reads.append(entry.into)

        step = Step(
            id=f"s{index}", intent=entry.intent, action=action, target=target,
            # The checkpoint belongs to the frame the URL was read from, which
            # is not necessarily the frame the control lived in.
            expect=(_checkpoints(entry, params, tuple(entry.content_frame_after))
                    or ((_fill_checkpoint(target, bound_value, frame),)
                        if entry.tool == "fill" else ())),
            commits=entry.committed,
            # A step that navigates is worth one retry: the commonest cause of a
            # miss is that the next screen had not rendered yet, and that is
            # exactly what a bounded retry is for.
            retry=Retry(max_attempts=2) if entry.tool == "click" else None,
        )
        # Policy has the last word on risk. The recorder's opinion is a hint
        # from a model; the operator's patterns are a rule.
        risk = policy.classify_risk(step)
        steps.append(step.model_copy(update={"risk": risk}))

    if not any(s.expect for s in steps):
        raise DistillRefused(
            "the run produced no verifiable checkpoint, so the capability could "
            "never confirm it reached the state it claims")

    # Inferred from the run, then overridden by anything the caller stated.
    # The operator always has the last word; they simply should not have to
    # write down what the recording already shows.
    inferred = _infer_inputs(trace)
    overrides = input_specs or {}
    inputs = []
    for key, value in params.items():
        spec = {**inferred.get(key, {}), **overrides.get(key, {})}
        if spec.get("sensitive"):
            # An example is a convenience for a caller. For a credential it is
            # a disclosure, and the artifact is the one file that gets committed.
            example = None
        elif spec.get("type") == "number":
            example = float(str(value).replace(",", "").lstrip("$"))
        else:
            example = value
        inputs.append(InputSpec(name=key, example=example, **spec))
    inputs = tuple(inputs)
    outputs = tuple(
        OutputSpec(name=out,
                   description=_output_description(trace, out) or
                               f"Read from the {out.replace('_', ' ')} field")
        for out in trace.declared_outputs if out in reads
    )
    outcomes, _ = _load_outcomes(app)
    host = urlparse(trace.entry_url).netloc

    return Capability(
        capability_id=capability_id, version=version, name=name, description=description,
        entry_url=trace.entry_url, inputs=inputs, outputs=outputs,
        steps=tuple(steps), outcomes=tuple(outcomes),
        safety=Safety(allowed_hosts=(host,), max_steps=max(20, len(steps) * 2)),
        recovery=Recovery(),
        # Always draft. A capability a model just authored has not been read by
        # anyone, and this one can move money.
        approval=Approval(status="draft"),
        provenance=Provenance(
            recorded_at=datetime.now(timezone.utc).isoformat(),
            planner=trace.planner, model=trace.model, tenant=trace.tenant,
            steps_by_tier=trace.tier_counts(),
        ),
    )
