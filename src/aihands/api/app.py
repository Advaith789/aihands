"""The agent-facing API and the operator console.

Two audiences, one process:

  * an AI agent calls /api/capabilities to see what it can do and
    /api/capabilities/{id}/invoke to do it. That surface is deliberately shaped
    like a tool catalogue -- typed args in, typed result out, business outcomes
    distinguishable from failures without reading prose.

  * a human uses the console when a run gets stuck. Taking control means driving
    the same live browser the automation was using, which is why the registry of
    running sessions lives in this process rather than behind a queue.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..kernel.control import Resume, SessionControl
from ..kernel.observability import RunRecorder, new_run_id
from ..kernel.policy import Policy
from ..replay.engine import ReplayEngine
from ..surface.web_surface import WebSurface
from . import store

STATIC = Path(__file__).resolve().parent / "static"


class InvokeRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    #: Risky steps need the caller to say so explicitly, on top of the
    #: capability being approved. Two independent keys for one lock.
    allow_risky: bool = False
    #: Attended runs open a visible browser and wait for a human when they get
    #: stuck. Unattended runs return an intervention request instead of
    #: blocking on an operator who may not exist.
    attended: bool = False


class ApproveRequest(BaseModel):
    approved_by: str


class ResumeRequest(BaseModel):
    decision: str
    operator: str = "operator"


def build_api(policy_path: str = "policy.yaml") -> FastAPI:
    app = FastAPI(title="aihands", docs_url="/api/docs", redoc_url=None)
    registry = store.RunRegistry()

    # ---------------- catalogue ----------------

    @app.get("/api/capabilities")
    async def list_capabilities() -> list[dict[str, Any]]:
        return [store.summarise(cap) for cap, _ in store.load_all().values()]

    @app.get("/api/capabilities/{capability_id}")
    async def get_capability(capability_id: str) -> dict[str, Any]:
        try:
            cap, _ = store.load(capability_id)
        except KeyError:
            raise HTTPException(404, f"no capability {capability_id!r}")
        return {**store.summarise(cap),
                "steps": [{"id": s.id, "intent": s.intent, "action": s.action.type,
                           "risk": s.risk, "commits": s.commits,
                           "locator": s.target.candidates[0].strategy if s.target else None,
                           "fallbacks": (len(s.target.candidates) - 1) if s.target else 0,
                           "expect": s.expect.value if s.expect else None}
                          for s in cap.steps]}

    @app.post("/api/capabilities/{capability_id}/approve")
    async def approve(capability_id: str, body: ApproveRequest) -> dict[str, Any]:
        try:
            return store.summarise(store.approve(capability_id, body.approved_by))
        except KeyError:
            raise HTTPException(404, f"no capability {capability_id!r}")

    @app.post("/api/capabilities/{capability_id}/revoke")
    async def revoke(capability_id: str) -> dict[str, Any]:
        try:
            return store.summarise(store.revoke(capability_id))
        except KeyError:
            raise HTTPException(404, f"no capability {capability_id!r}")

    # ---------------- invocation ----------------

    @app.post("/api/capabilities/{capability_id}/invoke")
    async def invoke(capability_id: str, body: InvokeRequest) -> dict[str, Any]:
        try:
            cap, _ = store.load(capability_id)
        except KeyError:
            raise HTTPException(404, f"no capability {capability_id!r}")

        run_id = new_run_id("api")
        recorder = RunRecorder(run_id)
        control = SessionControl(run_id)
        # Attended runs are visible on purpose: an operator cannot take over a
        # session they cannot see.
        surface = await WebSurface.launch(headless=not body.attended)
        active = store.ActiveRun(run_id=run_id, capability_id=capability_id,
                                 control=control, surface=surface, goal=cap.name)
        registry.add(active)

        engine = ReplayEngine(
            surface, Policy.load(policy_path), recorder=recorder, control=control,
            escalation_mode="block" if body.attended else "fail",
            allow_risky=body.allow_risky, goal=cap.name)

        async def execute() -> None:
            try:
                active.result = await engine.run(cap, body.params)
            except Exception as exc:                      # never leave a run dangling
                active.error = f"{type(exc).__name__}: {exc}"
            finally:
                if not body.attended or control.state.value in ("DONE", "ABORTED"):
                    await surface.close()

        if body.attended:
            # Returns immediately so the console can show the run and, if it
            # pauses, offer it to a human. Blocking the HTTP request until a
            # person happens to notice would be a worse API than either.
            asyncio.create_task(execute())
            return {"run_id": run_id, "status": "running", "attended": True}

        await execute()
        if active.error:
            raise HTTPException(500, active.error)
        return active.result.model_dump(mode="json")

    # ---------------- interventions ----------------

    @app.get("/api/interventions")
    async def interventions() -> list[dict[str, Any]]:
        return registry.open_interventions()

    @app.post("/api/interventions/{run_id}/take")
    async def take(run_id: str, body: ResumeRequest) -> dict[str, Any]:
        try:
            run = registry.get(run_id)
        except KeyError:
            raise HTTPException(404, f"no active run {run_id!r}")
        try:
            esc = run.control.take_control(body.operator)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        # From here the person drives the real browser directly. We watch rather
        # than mediate, so whatever they do lands in the same audit trail.
        await run.surface.start_human_capture()
        return {"run_id": run_id, "state": run.state(), "escalation": esc.to_dict()}

    @app.post("/api/interventions/{run_id}/resume")
    async def resume(run_id: str, body: ResumeRequest) -> dict[str, Any]:
        try:
            run = registry.get(run_id)
        except KeyError:
            raise HTTPException(404, f"no active run {run_id!r}")
        try:
            decision = Resume(body.decision)
        except ValueError:
            raise HTTPException(400, f"decision must be one of {[r.value for r in Resume]}")

        for action in await run.surface.collect_human_actions():
            run.control.record_human_action(action)
        run.control.hand_back(decision, body.operator)
        return {"run_id": run_id, "state": run.state(), "decision": decision.value}

    @app.get("/api/runs")
    async def runs(limit: int = 25) -> list[dict[str, Any]]:
        live = [{"run_id": r.run_id, "capability_id": r.capability_id,
                 "state": r.state(), "live": True, "started_at": r.started_at}
                for r in registry.all() if r.result is None and r.error is None]
        return live + store.recent_runs(limit)

    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: str) -> dict[str, Any]:
        import json
        path = store.EVIDENCE_DIR / run_id / "result.json"
        if path.exists():
            return json.loads(path.read_text())
        try:
            run = registry.get(run_id)
        except KeyError:
            raise HTTPException(404, f"no run {run_id!r}")
        return {"run_id": run_id, "state": run.state(), "live": True,
                "escalations": [e.to_dict() for e in run.control.escalations],
                "transitions": [vars(t) | {"frm": t.frm.value, "to": t.to.value,
                                           "owner": t.owner.value}
                                for t in run.control.transitions]}

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "capabilities": len(store.load_all()),
                "active_runs": len(registry.all())}

    @app.get("/", response_class=HTMLResponse)
    async def console() -> HTMLResponse:
        return HTMLResponse((STATIC / "index.html").read_text())

    return app


app = build_api()
