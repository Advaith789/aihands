"""Capability catalogue and the registry of runs in flight.

The catalogue is a directory of JSON files. Not a database: the whole point of
an artifact is that it is a reviewable file a human can read in a pull request,
and putting it behind a schema migration would take that away.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..kernel.control import SessionControl
from ..schema.capability import Approval, Capability

CAPABILITY_DIR = Path("capabilities")
EVIDENCE_DIR = Path("evidence/runs")


def _files() -> list[Path]:
    return sorted(p for p in CAPABILITY_DIR.glob("*.json") if p.name != "_index.json")


def load_all() -> dict[str, tuple[Capability, Path]]:
    out: dict[str, tuple[Capability, Path]] = {}
    for path in _files():
        try:
            cap = Capability.model_validate(json.loads(path.read_text()))
        except Exception:
            continue          # a malformed file is not a reason to hide the rest
        out[cap.capability_id] = (cap, path)
    return out


def load(capability_id: str) -> tuple[Capability, Path]:
    found = load_all().get(capability_id)
    if not found:
        raise KeyError(capability_id)
    return found


def approve(capability_id: str, approved_by: str) -> Capability:
    """Sign this exact version.

    The signature covers a hash of the steps, so editing the artifact
    afterwards silently invalidates it rather than silently inheriting it. That
    is the difference between an approval and a sticker.
    """
    cap, path = load(capability_id)
    signed = cap.model_copy(update={"approval": Approval(
        status="approved", approved_by=approved_by,
        approved_at=datetime.now(timezone.utc).isoformat(),
        approved_steps_hash=cap.steps_hash())})
    path.write_text(json.dumps(signed.model_dump(mode="json"), indent=2))
    return signed


def revoke(capability_id: str) -> Capability:
    cap, path = load(capability_id)
    reverted = cap.model_copy(update={"approval": Approval(status="draft")})
    path.write_text(json.dumps(reverted.model_dump(mode="json"), indent=2))
    return reverted


def summarise(cap: Capability) -> dict[str, Any]:
    """What an agent needs to decide whether to call this, and how.

    The input and output schemas are emitted as JSON Schema, so the artifact is
    directly usable as a tool definition rather than something a wrapper has to
    describe by hand.
    """
    return {
        "capability_id": cap.capability_id,
        "version": cap.version,
        "name": cap.name,
        "description": cap.description,
        "input_schema": cap.input_schema(),
        "output_schema": cap.output_schema(),
        "outcomes": [{"name": o.name, "code": o.code.value,
                      "category": o.category.value, "message": o.message}
                     for o in cap.outcomes],
        "steps": len(cap.steps),
        "commits_state": cap.mutating,
        "requires_approval": cap.requires_approval(),
        "approved": cap.is_approved(),
        "approval": cap.approval.model_dump(mode="json"),
        "provenance": cap.provenance.model_dump(mode="json") if cap.provenance else None,
        "entry_url": cap.entry_url,
    }


# ---------------------------------------------------------------------------


@dataclass
class ActiveRun:
    """A run in flight, and the handle a human needs to take it over."""

    run_id: str
    capability_id: str
    control: SessionControl
    surface: Any
    goal: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    result: Any = None
    error: str | None = None

    def state(self) -> str:
        return self.control.state.value


class RunRegistry:
    """Runs the console can act on. In-process on purpose.

    Taking control means driving the *same live browser* the automation is
    using. That browser is an object in this process, so the thing that hands it
    over has to be here too. A queue and a second service would be more
    architecture and strictly less handoff.
    """

    def __init__(self) -> None:
        self._runs: dict[str, ActiveRun] = {}

    def add(self, run: ActiveRun) -> None:
        self._runs[run.run_id] = run

    def get(self, run_id: str) -> ActiveRun:
        if run_id not in self._runs:
            raise KeyError(run_id)
        return self._runs[run_id]

    def all(self) -> list[ActiveRun]:
        return list(self._runs.values())

    def open_interventions(self) -> list[dict[str, Any]]:
        out = []
        for run in self._runs.values():
            for esc in run.control.escalations:
                if esc.resolved_at is None:
                    out.append({"run_id": run.run_id, "capability_id": run.capability_id,
                                "state": run.state(), "goal": run.goal, **esc.to_dict()})
        return out


def recent_runs(limit: int = 25) -> list[dict[str, Any]]:
    """Finished runs, newest first, read straight from the evidence directory."""
    if not EVIDENCE_DIR.exists():
        return []
    rows: list[dict[str, Any]] = []
    for directory in sorted(EVIDENCE_DIR.iterdir(), reverse=True):
        result = directory / "result.json"
        if not result.exists():
            continue
        try:
            data = json.loads(result.read_text())
        except Exception:
            continue
        drift = data.get("drift") or {}
        resolved = drift.get("steps_resolved") or 0
        rows.append({
            "run_id": data.get("run_id", directory.name),
            "capability_id": data.get("capability_id"),
            "category": data.get("category"), "code": data.get("code"),
            "outputs": data.get("outputs") or {},
            "outcome": (data.get("outcome") or {}).get("name"),
            "duration_ms": data.get("duration_ms"), "llm_calls": data.get("llm_calls"),
            "started_at": data.get("started_at"),
            "escalated": (data.get("control") or {}).get("escalated", False),
            "stability": (drift.get("first_choice", 0) / resolved) if resolved else None,
        })
        if len(rows) >= limit:
            break
    return rows
