"""Regenerate everything in evidence/ from real runs.

One command, so a reviewer can reproduce every claim rather than take the
committed output on trust. It costs a handful of model calls -- the LLM-only
discovery is the run that has to be genuine.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aihands.api import store
from aihands.discovery.distill import distill_capability
from aihands.discovery.loop import DiscoveryLoop
from aihands.discovery.planner import LadderPlanner, LLMPlanner
from aihands.kernel.control import Resume, SessionControl, State
from aihands.kernel.observability import RunRecorder, new_run_id
from aihands.kernel.policy import Policy
from aihands.replay.engine import ReplayEngine
from aihands.schema.capability import Approval, Capability
from aihands.surface.web_surface import WebSurface
from aihands.tenancy import load_for

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "evidence" / "runs"
CAP_ID = "mendota.member.open_savings_subaccount"
GOAL = ("Sign in to the servicing desk, find the member with the given member id, "
        "and open a savings sub-account for them with the given opening deposit. "
        "Then read the confirmation number from the receipt.")
SPECS = {"member_id": {"pattern": r"M-\d{4}", "description": "Member to service"},
         "operator_id": {"sensitive": True, "description": "Operator signing in"},
         "deposit": {"type": "number", "description": "Opening deposit in dollars"}}
OP = {"operator_id": "OP-77"}


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def reset(port: int = 8099) -> None:
    urllib.request.urlopen(urllib.request.Request(
        f"http://127.0.0.1:{port}/admin/reset", method="POST")).read()


def approved(cap: Capability) -> Capability:
    return cap.model_copy(update={"approval": Approval(
        status="approved", approved_by="reviewer@mendota",
        approved_at=datetime.now(timezone.utc).isoformat(),
        approved_steps_hash=cap.steps_hash())})


async def discovery(label: str, planner, port: int, tenant: str = "") -> tuple:
    reset(port)
    recorder = RunRecorder(new_run_id(label), root=RUNS)
    surface = await WebSurface.launch(headless=True)
    try:
        loop = DiscoveryLoop(surface, planner, recorder, Policy.load(ROOT / "policy.yaml"))
        trace = await loop.run(goal=GOAL, entry_url=f"http://127.0.0.1:{port}/login",
                               params={**OP, "member_id": "M-1001", "deposit": "500"},
                               outputs=["confirmation_number"], tenant=tenant,
                               sensitive={"operator_id"})
    finally:
        await surface.close()
    print(f"  {label:26} {trace.status:10} turns={len(trace.entries):2} "
          f"llm={trace.llm_calls:2} tiers={trace.tier_counts()}")
    return trace, recorder


async def replay(label: str, cap: Capability, params: dict, port: int = 8099,
                 attended: bool = False) -> None:
    reset(port)
    recorder = RunRecorder(new_run_id(label), root=RUNS)
    control = SessionControl(recorder.run_id)
    surface = await WebSurface.launch(headless=True)
    try:
        engine = ReplayEngine(surface, Policy.load(ROOT / "policy.yaml"),
                              recorder=recorder, control=control, allow_risky=True,
                              escalation_mode="block" if attended else "fail",
                              goal=cap.name)
        task = asyncio.create_task(engine.run(cap, params))
        if attended:
            while not (control.escalations and control.state is State.PAUSED):
                await asyncio.sleep(0.05)
            control.take_control("operator@mendota")
            await surface.start_human_capture()
            obs = await surface.observe()
            await surface.fill(next(c for c in obs.controls if "approver" in c.name.lower()),
                               "OP-99")
            await surface.click(next(c for c in obs.controls if c.role == "button"))
            for action in await surface.collect_human_actions():
                control.record_human_action(action)
            control.hand_back(Resume.CONTINUE, "operator@mendota")
        result = await task
    finally:
        await surface.close()
    print(f"  {label:26} {result.category.value:9} {result.code.value:24} "
          f"{result.duration_ms:5}ms llm={result.llm_calls} "
          f"{result.outputs or (result.outcome.name if result.outcome else '')}")


async def main() -> None:
    load_env()
    if RUNS.exists():
        shutil.rmtree(RUNS)
    RUNS.mkdir(parents=True)
    policy = Policy.load(ROOT / "policy.yaml")

    print("discovery")
    # The run that satisfies "the discovery run has to be real": no rules
    # anywhere, every decision made by the model against a live surface.
    await discovery("discovery_llm_only", LLMPlanner(), 8099)
    # The same goal through the ladder, for the tier split.
    trace, _ = await discovery("discovery_ladder", LadderPlanner(LLMPlanner()), 8099)
    # A second institution, where the rules cannot match the renamed fields.
    await discovery("discovery_presidio", LadderPlanner(LLMPlanner()), 8098, "presidio")

    capability = distill_capability(
        trace, capability_id=CAP_ID, name="Open a savings sub-account for a member",
        description="Finds a member and opens a savings sub-account, returning the "
                    "confirmation number.",
        policy=policy, app="mendota", input_specs=SPECS)
    path = ROOT / "capabilities" / f"{CAP_ID}.json"
    path.write_text(json.dumps(capability.model_dump(mode="json"), indent=2))
    print(f"  compiled -> {path.relative_to(ROOT)} ({len(capability.steps)} steps, draft)")

    print("\nreplay")
    await replay("replay_draft_refused", capability, {**OP, "member_id": "M-1001",
                                                      "deposit": 500})
    signed = approved(capability)
    path.write_text(json.dumps(signed.model_dump(mode="json"), indent=2))
    await replay("replay_success", signed, {**OP, "member_id": "M-1001", "deposit": 500})
    await replay("replay_not_found", signed, {**OP, "member_id": "M-9999", "deposit": 500})
    await replay("replay_not_permitted", signed, {**OP, "member_id": "M-1002", "deposit": 500})
    await replay("replay_validation", signed, {**OP, "member_id": "M-1001", "deposit": 10})
    await replay("replay_transient", signed, {**OP, "member_id": "M-1004", "deposit": 250})
    await replay("replay_bad_input", signed, {**OP, "member_id": "NOT-AN-ID", "deposit": 500})
    await replay("replay_escalation", signed, {**OP, "member_id": "M-1005", "deposit": 15000})
    await replay("replay_handoff", signed, {**OP, "member_id": "M-1005", "deposit": 15000},
                 attended=True)
    await replay("replay_presidio", load_for(signed, "presidio"),
                 {**OP, "member_id": "M-1001", "deposit": 500}, port=8098)

    print(f"\n{len(list(RUNS.iterdir()))} runs in evidence/runs/")


if __name__ == "__main__":
    asyncio.run(main())
