"""Test fixtures.

The live-surface tests drive the real target application rather than a mock.
A mocked page cannot tell you that a frameset makes page-level waiting a silent
race, and that class of bug is the entire difficulty of this problem.
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.request

import pytest

MENDOTA, PRESIDIO = 8099, 8098


@pytest.fixture(scope="session", autouse=True)
def isolated_evidence(tmp_path_factory):
    """Tests write their run records somewhere disposable.

    evidence/ is part of the deliverable -- it is the proof that particular
    runs really happened. A directory full of test artifacts would drown that.
    """
    import os
    os.environ["AIHANDS_EVIDENCE_DIR"] = str(tmp_path_factory.mktemp("evidence"))
    import aihands.kernel.observability as obs
    from pathlib import Path as _P
    obs.EVIDENCE_ROOT = _P(os.environ["AIHANDS_EVIDENCE_DIR"])
    yield


def _up(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.25)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _serve(tenant: str, port: int) -> None:
    import uvicorn
    from target_app.app import create_app
    uvicorn.run(create_app(tenant), host="127.0.0.1", port=port, log_level="critical")


@pytest.fixture(scope="session", autouse=True)
def target_apps():
    """Reuse the developer's already-running instances when present, otherwise
    start our own. Tests should not fight with a dev environment, and they
    should not need one either."""
    started = []
    for tenant, port in (("mendota", MENDOTA), ("presidio", PRESIDIO)):
        if _up(port):
            continue
        thread = threading.Thread(target=_serve, args=(tenant, port), daemon=True)
        thread.start()
        started.append(port)
    deadline = time.time() + 15
    for port in started:
        while time.time() < deadline and not _up(port):
            time.sleep(0.1)
    yield


@pytest.fixture
def reset():
    def _reset(port: int = MENDOTA) -> None:
        urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{port}/admin/reset", method="POST")).read()
    return _reset


@pytest.fixture
def capability():
    import json
    from pathlib import Path
    from aihands.schema.capability import Capability
    path = Path("capabilities/mendota.member.open_savings_subaccount.json")
    return Capability.model_validate(json.loads(path.read_text()))


@pytest.fixture
def approved(capability):
    from datetime import datetime, timezone
    from aihands.schema.capability import Approval
    return capability.model_copy(update={"approval": Approval(
        status="approved", approved_by="test@mendota",
        approved_at=datetime.now(timezone.utc).isoformat(),
        approved_steps_hash=capability.steps_hash())})


@pytest.fixture
async def surface():
    from aihands.surface.web_surface import WebSurface
    s = await WebSurface.launch(headless=True)
    try:
        yield s
    finally:
        await s.close()
