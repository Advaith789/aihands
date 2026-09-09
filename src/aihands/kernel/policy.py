"""Deployment policy.

One rule shapes this file: **the policy is configuration, never a parameter.**
It is loaded from disk at process start. No tool argument, artifact field or
model output can widen it, and a capability's own safety block can only
intersect with it.

If a capability could grant itself hosts or action types, every guarantee below
would be decoration, because the artifact is authored by a language model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

from ..schema.capability import Capability, Step
from ..schema.outcomes import Code, PolicyViolation

DEFAULT_PATH = Path("policy.yaml")


@dataclass(frozen=True)
class Policy:
    allowed_hosts: frozenset[str] = frozenset()
    allowed_actions: frozenset[str] = frozenset()
    risky_patterns: tuple[re.Pattern[str], ...] = ()
    risky_action_policy: str = "confirm"      # block | confirm | allow
    max_steps: int = 40
    max_run_seconds: int = 180

    # -- loading --------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Policy":
        p = Path(path or DEFAULT_PATH)
        raw = yaml.safe_load(p.read_text()) if p.exists() else {}
        return cls(
            allowed_hosts=frozenset(raw.get("allowed_hosts", [])),
            allowed_actions=frozenset(raw.get("allowed_actions", [])),
            risky_patterns=tuple(re.compile(x) for x in raw.get("risky_intent_patterns", [])),
            risky_action_policy=raw.get("risky_action_policy", "confirm"),
            max_steps=int(raw.get("max_steps", 40)),
            max_run_seconds=int(raw.get("max_run_seconds", 180)),
        )

    def intersect(self, capability: Capability) -> "Policy":
        """Apply a capability's own limits. Intersection only -- a host the
        deployment did not allow cannot become allowed because an artifact
        says so."""
        hosts = self.allowed_hosts
        if capability.safety.allowed_hosts:
            hosts = hosts & frozenset(capability.safety.allowed_hosts)
        return Policy(
            allowed_hosts=hosts,
            allowed_actions=self.allowed_actions,
            risky_patterns=self.risky_patterns,
            risky_action_policy=self.risky_action_policy,
            max_steps=min(self.max_steps, capability.safety.max_steps),
            max_run_seconds=self.max_run_seconds,
        )

    # -- checks ---------------------------------------------------------

    def check_url(self, url: str) -> None:
        host = urlparse(url).netloc
        if host not in self.allowed_hosts:
            raise PolicyViolation(
                f"host {host!r} is not on the allowlist", Code.POLICY_VIOLATION
            )

    def check_action(self, action_type: str) -> None:
        if action_type not in self.allowed_actions:
            raise PolicyViolation(
                f"action {action_type!r} is not permitted", Code.POLICY_VIOLATION
            )

    def classify_risk(self, step: Step) -> str:
        """Policy may RAISE a step's risk, never lower it.

        The recorder's classification is a hint from a model. The operator's
        patterns are a rule. When they disagree, the rule wins -- which is the
        only direction that is safe when the hint is the thing being guarded.
        """
        if step.risk == "risky":
            return "risky"
        if not step.commits and step.action.type not in ("click", "fill"):
            return "safe"
        blob = f"{step.intent}"
        if any(p.search(blob) for p in self.risky_patterns):
            return "risky"
        return "safe"


@dataclass
class RiskDecision:
    allowed: bool
    needs_confirmation: bool = False
    reason: str = ""
    detail: dict[str, str] = field(default_factory=dict)


def decide_risky(
    policy: Policy, capability: Capability, step: Step, caller_opted_in: bool
) -> RiskDecision:
    """Whether a risky step may proceed unattended.

    Three things must line up: the operator's policy permits it, a human has
    signed this exact version of the capability, and the caller explicitly
    asked for a risky invocation. Any one missing means a person decides on the
    live session instead of the automation deciding for them.
    """
    if policy.classify_risk(step) == "safe":
        return RiskDecision(allowed=True)

    if policy.risky_action_policy == "block":
        return RiskDecision(False, reason="policy blocks risky actions outright")
    if policy.risky_action_policy == "allow":
        return RiskDecision(True)

    if not capability.is_approved():
        why = ("capability has never been approved"
               if capability.approval.status == "draft"
               else "approval does not match the current steps")
        return RiskDecision(False, needs_confirmation=True, reason=why)
    if not caller_opted_in:
        return RiskDecision(
            False, needs_confirmation=True,
            reason="caller did not opt in to risky actions (allow_risky=false)",
        )
    return RiskDecision(True)
