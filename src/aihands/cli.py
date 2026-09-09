"""Command line entry points.

Four verbs that mirror the system's four ideas: discover a flow with a model,
approve the artifact a human should read, replay it without a model, and open
the console when a run needs a person.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .api import store
from .discovery.distill import DistillRefused, distill_capability
from .discovery.loop import DiscoveryLoop
from .discovery.planner import HeuristicPlanner, LadderPlanner, LLMPlanner
from .kernel.observability import RunRecorder, new_run_id
from .kernel.policy import Policy
from .replay.engine import ReplayEngine
from .schema.capability import Capability
from .surface.web_surface import WebSurface
from .tenancy import load_for

CAPABILITY_DIR = Path("capabilities")


def _load_env(path: str = ".env") -> None:
    """Read .env if present. Secrets stay out of the repo and out of argv --
    a key on a command line ends up in shell history and process listings."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def _params(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--param expects key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key] = value
    return out


# ---------------------------------------------------------------------------


async def _discover(args) -> int:
    _load_env()
    policy = Policy.load(args.policy)

    if args.planner == "heuristic":
        planner = HeuristicPlanner()
    elif args.planner == "llm":
        planner = LLMPlanner(model=args.model)
    else:
        planner = LadderPlanner(LLMPlanner(model=args.model))

    specs = json.loads(Path(args.input_specs).read_text()) if args.input_specs else {}
    sensitive = {k for k, v in (specs or {}).items() if v.get("sensitive")}

    recorder = RunRecorder(new_run_id(f"discovery_{args.planner}"))
    surface = await WebSurface.launch(headless=not args.headed)
    try:
        loop = DiscoveryLoop(surface, planner, recorder, policy, max_turns=args.max_turns)
        trace = await loop.run(goal=args.goal, entry_url=args.entry_url,
                               params=_params(args.param), outputs=args.output or [],
                               tenant=args.tenant or "")
    finally:
        await surface.close()

    print(f"status={trace.status} turns={len(trace.entries)} "
          f"llm_calls={trace.llm_calls} tiers={trace.tier_counts()}")
    print(f"evidence: {recorder.dir}")
    if trace.status != "complete":
        print(f"  {trace.summary}", file=sys.stderr)
        return 1

    if not args.distill_to:
        print("  (pass --distill-to to emit a capability)")
        return 0

    try:
        capability = distill_capability(
            trace, capability_id=args.distill_to, name=args.name or args.distill_to,
            description=args.description or args.goal, policy=policy,
            app=args.app, input_specs=specs or None)
    except DistillRefused as exc:
        print(f"refused to distil: {exc}", file=sys.stderr)
        return 1

    CAPABILITY_DIR.mkdir(exist_ok=True)
    path = CAPABILITY_DIR / f"{capability.capability_id}.json"
    path.write_text(json.dumps(capability.model_dump(mode="json"), indent=2))
    print(f"distilled -> {path} ({len(capability.steps)} steps, "
          f"approval={capability.approval.status})")
    if capability.requires_approval():
        print(f"  this capability changes state; approve it before replay:\n"
              f"    aihands approve {capability.capability_id} --by you@example.com")
    return 0


async def _replay(args) -> int:
    try:
        capability, _ = store.load(args.capability)
    except KeyError:
        print(f"no capability {args.capability!r}", file=sys.stderr)
        return 2
    capability = load_for(capability, args.tenant)

    surface = await WebSurface.launch(headless=not args.headed)
    try:
        engine = ReplayEngine(
            surface, Policy.load(args.policy), allow_risky=args.allow_risky,
            escalation_mode="block" if args.attended else "fail",
            goal=capability.name)
        result = await engine.run(capability, _params(args.param))
    finally:
        await surface.close()

    print(json.dumps(result.model_dump(mode="json"), indent=2))
    # A business outcome is a successful call that happens not to be the happy
    # path, so it exits zero. Only a genuine failure is non-zero -- otherwise a
    # caller's shell script cannot tell "no such member" from "we are broken".
    return 0 if result.ok else 1


def _approve(args) -> int:
    try:
        cap = (store.revoke(args.capability) if args.revoke
               else store.approve(args.capability, args.by))
    except KeyError:
        print(f"no capability {args.capability!r}", file=sys.stderr)
        return 2
    print(f"{cap.capability_id}: {cap.approval.status}"
          + (f" by {cap.approval.approved_by}" if cap.approval.approved_by else ""))
    return 0


def _list(args) -> int:
    rows = store.load_all()
    if not rows:
        print("no capabilities recorded")
        return 0
    for cap, path in rows.values():
        gate = ("approved" if cap.is_approved()
                else "draft" if cap.requires_approval() else "read-only")
        tiers = cap.provenance.steps_by_tier if cap.provenance else {}
        print(f"{cap.capability_id}@{cap.version}  [{gate}]  {len(cap.steps)} steps  {tiers}")
        print(f"  {cap.description}")
        print(f"  in: {list(cap.input_schema()['properties'])}  "
              f"out: {list(cap.output_schema()['properties'])}  ({path})")
    return 0


def _console(args) -> int:
    import uvicorn
    _load_env()
    print(f"console:  http://127.0.0.1:{args.port}")
    uvicorn.run("aihands.api.app:app", host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def _target_app(args) -> int:
    """Run the stand-in application.

    It deliberately is not part of the installed package: it stands in for
    third-party software we do not control, and giving our own code an import
    path into it would quietly undermine that. So it is imported from the
    working directory, the way you would run any fixture that ships beside a
    repository rather than inside it.
    """
    import uvicorn

    os.environ["TENANT"] = args.tenant
    try:
        from target_app.app import create_app
    except ModuleNotFoundError:
        # An installed console script does not put the working directory on the
        # import path, so this is the normal case rather than the exception.
        sys.path.insert(0, str(Path.cwd()))
        try:
            from target_app.app import create_app
        except ModuleNotFoundError:
            print("could not find target_app/ — run this from the repository root",
                  file=sys.stderr)
            return 2

    print(f"{args.tenant}: http://127.0.0.1:{args.port}")
    uvicorn.run(create_app(args.tenant), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aihands", description=__doc__)
    parser.add_argument("--policy", default="policy.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="drive a UI with a model and record the flow")
    d.add_argument("--goal", required=True)
    d.add_argument("--entry-url", required=True)
    d.add_argument("--param", action="append", metavar="KEY=VALUE")
    d.add_argument("--output", action="append", metavar="NAME")
    d.add_argument("--planner", choices=["ladder", "llm", "heuristic"], default="ladder")
    d.add_argument("--model", default=None)
    d.add_argument("--max-turns", type=int, default=25)
    d.add_argument("--tenant", default=None)
    d.add_argument("--app", default="mendota", help="which outcome vocabulary to attach")
    d.add_argument("--distill-to", metavar="CAPABILITY_ID")
    d.add_argument("--name"), d.add_argument("--description")
    d.add_argument("--input-specs", metavar="JSON_FILE")
    d.add_argument("--headed", action="store_true")
    d.set_defaults(run=lambda a: asyncio.run(_discover(a)))

    r = sub.add_parser("replay", help="run a saved capability with no model in the loop")
    r.add_argument("capability")
    r.add_argument("--param", action="append", metavar="KEY=VALUE")
    r.add_argument("--tenant", default=None)
    r.add_argument("--allow-risky", action="store_true")
    r.add_argument("--attended", action="store_true",
                   help="wait for a human instead of returning an intervention request")
    r.add_argument("--headed", action="store_true")
    r.set_defaults(run=lambda a: asyncio.run(_replay(a)))

    a = sub.add_parser("approve", help="sign a capability so it may replay unattended")
    a.add_argument("capability")
    a.add_argument("--by", default="operator")
    a.add_argument("--revoke", action="store_true")
    a.set_defaults(run=_approve)

    ls = sub.add_parser("list", help="show the capability catalogue")
    ls.set_defaults(run=_list)

    c = sub.add_parser("console", help="operator console and agent-facing API")
    c.add_argument("--port", type=int, default=8100)
    c.set_defaults(run=_console)

    t = sub.add_parser("app", help="run the stand-in target application")
    t.add_argument("--tenant", default="mendota", choices=["mendota", "presidio"])
    t.add_argument("--port", type=int, default=8099)
    t.set_defaults(run=_target_app)

    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
