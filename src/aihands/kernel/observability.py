"""Run evidence.

Append-only JSONL, one self-contained record per line, written and flushed as
it happens. A record still sitting in a buffer when a process is killed is not
evidence, and the runs worth reviewing are exactly the ones that ended badly.

Refusals and escalations are logged as loudly as successes. A log of only the
happy path cannot answer the question a review actually asks, which is what did
it try.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redaction import Redactor

#: Overridable so a test suite does not write into the evidence directory that
#: is part of the deliverable. A run recorded by a test is not evidence.
EVIDENCE_ROOT = Path(os.environ.get("AIHANDS_EVIDENCE_DIR", "evidence/runs"))


def new_run_id(kind: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{kind}_{stamp}_{uuid.uuid4().hex[:6]}"


def _ref(path: Path) -> str:
    """How a path is written down in evidence.

    Relative to where the process was started, when the file is underneath it.
    An absolute path records the machine that happened to produce the run --
    useless to anyone reading it later, and it publishes somebody's home
    directory into a repository.
    """
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


class RunRecorder:
    def __init__(self, run_id: str, root: Path | None = None,
                 redactor: Redactor | None = None, dirname: str | None = None) -> None:
        # The run id has to be unique -- it identifies one execution forever.
        # The directory name is read by people, and for the curated evidence set
        # a timestamp and six hex characters are noise in front of the only part
        # that matters, which is what the run was demonstrating.
        self.run_id = run_id
        self.dir = (root or EVIDENCE_ROOT) / (dirname or run_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor or Redactor()
        self._log_path = self.dir / "log.jsonl"
        self._t0 = time.monotonic()

    def log(self, event: str, **fields: Any) -> None:
        record = {
            "t_ms": int((time.monotonic() - self._t0) * 1000),
            "at": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event": event,
            **{k: v for k, v in fields.items() if v is not None},
        }
        line = self.redactor.dumps(record)
        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()

    def write_json(self, name: str, payload: Any) -> str:
        path = self.dir / name
        body = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
        path.write_text(json.dumps(self.redactor.value(body), indent=2, default=str))
        return _ref(path)

    def write_text(self, name: str, text: str) -> str:
        path = self.dir / name
        path.write_text(self.redactor.text(text))
        return _ref(path)

    async def capture(self, surface: Any, label: str) -> dict[str, str]:
        """The richer signal the assignment asks for on failure.

        A screenshot answers "what did it look like"; the observation answers
        "what did the system believe was there". When those two disagree, the
        disagreement is the bug, and you cannot see it with only one of them.
        """
        out: dict[str, str] = {}
        try:
            png = self.dir / f"{label}.png"
            await surface.page.screenshot(path=str(png), full_page=True)
            out["screenshot"] = _ref(png)
        except Exception:
            pass
        try:
            obs = await surface.observe()
            out["observation"] = self.write_text(f"{label}.observation.txt", obs.render())
        except Exception:
            pass
        try:
            html = await surface.page.content()
            out["dom_snapshot"] = self.write_text(f"{label}.dom.html", html)
        except Exception:
            pass
        return out
