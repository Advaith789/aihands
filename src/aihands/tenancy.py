"""Cross-tenant reuse.

Hundreds of institutions run the same vendor product, branded and configured
differently. Re-recording per tenant would mean hundreds of near-identical
artifacts drifting apart on their own, with no way to tell a deliberate
difference from an accident.

So a capability is recorded once against a reference instance and each tenant
gets a small overlay describing only what that institution calls things.

The load-bearing decision is what an overlay **cannot** do. It may not add,
remove or reorder steps, change an action, alter inputs or outputs, or widen
safety. It may only:

  * add locator candidates -- "where the base looks for X, this tenant also
    has Y";
  * point at a different host and entry url;
  * narrow safety further.

That keeps the base artifact the single source of truth for *what the
capability does*, and confines tenant variance to *how its controls are found*.
A reviewer who approved the base does not have to re-approve every tenant,
because no overlay can change the behaviour they signed.

Aliases match on the recorded locator rather than on step ids, so an overlay
survives the capability being re-recorded and its steps renumbered.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .schema.capability import Approval, Capability, Safety, Step, Target

OVERLAY_DIR = Path("capabilities/overlays")


class Alias(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: A subset of the recorded locator's fields. Every named field must match.
    match: dict[str, Any]
    #: A complete locator to try as well. Appended, never substituted -- the
    #: base candidate keeps working where it still applies, which is what makes
    #: one artifact serve both tenants rather than forking into two.
    add: dict[str, Any]

    def applies(self, candidate: dict[str, Any]) -> bool:
        return all(candidate.get(k) == v for k, v in self.match.items())


class Overlay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: str
    entry_url: str | None = None
    allowed_hosts: tuple[str, ...] = ()
    aliases: list[Alias] = Field(default_factory=list)

    @classmethod
    def load(cls, tenant: str, directory: Path | None = None) -> "Overlay":
        path = (directory or OVERLAY_DIR) / f"{tenant}.json"
        if not path.exists():
            raise KeyError(f"no overlay for tenant {tenant!r}")
        return cls.model_validate(json.loads(path.read_text()))

    # ------------------------------------------------------------------

    def _apply_target(self, target: Target | None) -> Target | None:
        if target is None:
            return None
        candidates = [c.model_dump(mode="json") for c in target.candidates]
        extra: list[dict[str, Any]] = []
        for alias in self.aliases:
            if any(alias.applies(c) for c in candidates):
                if alias.add not in candidates and alias.add not in extra:
                    extra.append(alias.add)
        if not extra:
            return target
        # Order by confidence, not by arrival. Appending naively puts a tenant's
        # proper role+name alias AFTER the base's last-resort CSS path, so
        # replay resolves through a brittle structural selector that only
        # happens to work while the two tenants share markup. It succeeds, which
        # is worse than failing -- the fragility is invisible until the day the
        # vendor restyles one institution.
        merged = candidates + extra
        merged.sort(key=lambda c: -float(c.get("confidence", 0.0)))
        return Target.model_validate({**target.model_dump(mode="json"),
                                      "candidates": merged})

    def apply(self, capability: Capability) -> Capability:
        """Specialise a base capability for this tenant.

        Steps are rebuilt one for one -- same ids, same order, same actions --
        so the shape a reviewer approved is preserved by construction rather
        than by a check that could be forgotten.
        """
        steps = tuple(
            Step.model_validate({**s.model_dump(mode="json"),
                                 "target": (self._apply_target(s.target).model_dump(mode="json")
                                            if s.target else None)})
            for s in capability.steps
        )
        assert [s.id for s in steps] == [s.id for s in capability.steps]

        hosts = capability.safety.allowed_hosts
        if self.allowed_hosts:
            # Replacement, not union: pointing a capability at another
            # institution should not leave the first one's host allowed.
            hosts = tuple(self.allowed_hosts)

        specialised = capability.model_copy(update={
            "steps": steps,
            "entry_url": self.entry_url or capability.entry_url,
            "safety": Safety(allowed_hosts=hosts,
                             max_steps=capability.safety.max_steps),
        })

        # Adding a locator candidate changes the steps hash, which would revoke
        # the base approval for every tenant -- defeating the whole point of
        # recording once. The approval carries over instead, because what the
        # reviewer signed is provably intact: same ids, same order, same
        # actions, same contract, and an overlay is operator configuration
        # rather than model output. The inheritance is recorded so it can be
        # audited rather than assumed.
        if capability.is_approved():
            return specialised.model_copy(update={"approval": Approval(
                status="approved",
                approved_by=capability.approval.approved_by,
                approved_at=capability.approval.approved_at,
                approved_steps_hash=specialised.steps_hash(),
                derived_via_overlay=self.tenant)})
        return specialised


def load_for(capability: Capability, tenant: str | None) -> Capability:
    if not tenant:
        return capability
    return Overlay.load(tenant).apply(capability)
