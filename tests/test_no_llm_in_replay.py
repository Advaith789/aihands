"""Replay must not be able to reach a language model.

Asserted structurally rather than promised in a comment. The claim "no model in
the decision loop" is the product, so it should be something a test can fail on
rather than something a reader has to take on trust.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys

FORBIDDEN = {"openai", "anthropic", "cohere", "google.generativeai", "litellm"}


def _reachable(module_name: str) -> set[str]:
    """Every module reachable from this one, following real imports."""
    before = set(sys.modules)
    importlib.import_module(module_name)
    seen: set[str] = set()
    frontier = [module_name]
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        module = sys.modules.get(name)
        if module is None:
            continue
        for attr in vars(module).values():
            child = getattr(attr, "__module__", None) or getattr(attr, "__name__", None)
            if isinstance(child, str) and child in sys.modules:
                frontier.append(child)
    del before
    return seen


def test_replay_package_cannot_reach_a_model_client():
    reachable = _reachable("aihands.replay.engine")
    leaked = {m for m in reachable if m.split(".")[0] in FORBIDDEN}
    assert not leaked, f"replay reached a model client: {sorted(leaked)}"


def test_replay_source_mentions_no_provider():
    import inspect
    import aihands.replay.engine as engine
    source = inspect.getsource(engine).lower()
    for provider in FORBIDDEN:
        assert provider not in source


def test_only_the_discovery_package_imports_a_model_client():
    """The model belongs in exactly one place. If a second module starts
    importing one, that is an architectural change and should be noticed."""
    import aihands
    importers = []
    for info in pkgutil.walk_packages(aihands.__path__, "aihands."):
        try:
            module = importlib.import_module(info.name)
        except Exception:
            continue
        source_names = {getattr(v, "__module__", "") or "" for v in vars(module).values()}
        if any(n.split(".")[0] in FORBIDDEN for n in source_names if n):
            importers.append(info.name)
    assert all(name.startswith("aihands.discovery") for name in importers), importers


def test_results_carry_the_claim_so_a_caller_can_check_it():
    from aihands.schema.results import ReplayResult
    assert "llm_calls" in ReplayResult.model_fields
