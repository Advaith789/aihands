"""Parameter substitution and input validation.

Substitution is a whole-model dump/replace/revalidate rather than string
surgery on the way past. That costs one serialisation per step -- irrelevant
next to a browser round trip -- and buys the guarantee that a bound step is
still a schema-valid step. A partially substituted artifact never exists.
"""

from __future__ import annotations

import re
from typing import Any, TypeVar

from .capability import BINDING, Capability, Step
from .outcomes import InvalidInput

T = TypeVar("T")


def bind_text(text: str, params: dict[str, Any]) -> str:
    """Resolve every {{ input.x }} in a string.

    An unknown name is an error rather than an empty string. Silently
    substituting nothing is how a search runs against a blank member id and
    returns somebody else's record.
    """

    def sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in params:
            raise InvalidInput(f"step references undeclared parameter {key!r}")
        value = params[key]
        return "" if value is None else str(value)

    return BINDING.sub(sub, text)


def _walk(node: Any, params: dict[str, Any]) -> Any:
    if isinstance(node, str):
        return bind_text(node, params)
    if isinstance(node, list):
        return [_walk(v, params) for v in node]
    if isinstance(node, dict):
        return {k: _walk(v, params) for k, v in node.items()}
    return node


def bind_step(step: Step, params: dict[str, Any]) -> Step:
    return Step.model_validate(_walk(step.model_dump(mode="json"), params))


def validate_inputs(capability: Capability, params: dict[str, Any]) -> dict[str, Any]:
    """Check and coerce parameters before a browser is touched.

    Doing this first is not tidiness. A capability that opens an account should
    never get halfway in and then discover the amount was the string "abc" --
    by then it may already have created something.
    """
    declared = {spec.name: spec for spec in capability.inputs}

    unknown = set(params) - set(declared)
    if unknown:
        raise InvalidInput(f"unknown parameter(s): {sorted(unknown)}")

    bound: dict[str, Any] = {}
    for name, spec in declared.items():
        raw = params.get(name)
        if isinstance(raw, str):
            # Callers pass values through shells, forms and JSON. Trimming here
            # means " M-1001 " is the member id it obviously is, rather than a
            # pattern failure the caller cannot see.
            raw = raw.strip()
        # An empty string is not a value. Treating it as one sends a blank
        # credential to a login form and reports the resulting confusion as a
        # missed checkpoint three steps later.
        if raw is None or (isinstance(raw, str) and not raw):
            if spec.required:
                raise InvalidInput(f"missing required parameter {name!r}")
            continue
        try:
            if spec.type == "number":
                value: Any = float(raw)
            elif spec.type == "boolean":
                value = bool(raw) if not isinstance(raw, str) else raw.strip().lower() in ("1", "true", "yes")
            else:
                value = str(raw)
        except (TypeError, ValueError) as exc:
            raise InvalidInput(f"parameter {name!r} is not a valid {spec.type}: {raw!r}") from exc

        if spec.pattern and not re.fullmatch(spec.pattern, str(value)):
            raise InvalidInput(f"parameter {name!r} does not match {spec.pattern!r}")
        bound[name] = value

    # An artifact that references a parameter it never declared would fail at a
    # random point mid-run. Catching it here makes it a load-time error.
    missing = capability.declared_bindings() - set(declared)
    if missing:
        raise InvalidInput(f"capability references undeclared parameter(s): {sorted(missing)}")
    return bound
