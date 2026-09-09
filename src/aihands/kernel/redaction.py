"""Redaction at the boundary.

Everything that leaves the process -- log lines, saved results, DOM snapshots,
screenshots' companion text -- goes through here first. Redacting at the point
of writing rather than at each call site means a new log statement cannot leak
by omission, which is how these things actually leak.

Two sources of truth about what is sensitive:

  * the capability says so. An input marked `sensitive` never appears in the
    clear anywhere, including in the artifact that recorded the run.
  * shape-based patterns catch what nobody declared -- a card number pasted
    into a notes field is still a card number.

Sensitive values become a stable token rather than a row of asterisks. Two runs
that used the same member id produce the same token, so an incident review can
still correlate them without ever seeing the value.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..schema.capability import Capability

# Shape-based catches. Deliberately conservative: a false positive costs a
# masked log line, a false negative costs a disclosure.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card", re.compile(r"\b\d(?:[ -]?\d){12,18}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
)


def token(value: str) -> str:
    return f"<redacted:{hashlib.sha256(value.encode()).hexdigest()[:8]}>"


class Redactor:
    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        # One compiled alternation rather than a pass per secret: scrubbing is
        # then a single O(n) sweep over the text no matter how many parameters
        # are marked sensitive, and it happens on every log line.
        self._map = {v: token(v) for v in (secrets or {}).values() if v}
        self._literal = (
            re.compile("|".join(re.escape(v) for v in sorted(self._map, key=len, reverse=True)))
            if self._map else None
        )

    def text(self, value: str) -> str:
        if not value:
            return value
        if self._literal:
            value = self._literal.sub(lambda m: self._map[m.group(0)], value)
        for _, pattern in PATTERNS:
            value = pattern.sub(lambda m: token(m.group(0)), value)
        return value

    def value(self, node: Any) -> Any:
        if isinstance(node, str):
            return self.text(node)
        if isinstance(node, dict):
            return {k: self.value(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [self.value(v) for v in node]
        return node

    def dumps(self, obj: Any) -> str:
        return json.dumps(self.value(obj), default=str, ensure_ascii=False)


def redactor_for(capability: Capability | None, params: dict[str, Any]) -> Redactor:
    if capability is None:
        return Redactor()
    sensitive = capability.sensitive_inputs()
    return Redactor({k: str(v) for k, v in params.items() if k in sensitive})
