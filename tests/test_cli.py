"""The commands the README tells a reviewer to type.

These exist because of a bug this suite did not catch: `aihands app` worked
every time I ran it during development, because I ran it as
`python -m target_app.app`, which puts the working directory on the import
path. The installed console script does not, so on a fresh clone the very first
command in the README failed. Testing the module is not the same as testing the
command someone is told to run.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / ".venv" / "bin" / "aihands"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not BIN.exists(), reason="console script not installed")
@pytest.mark.parametrize("command", ["discover", "replay", "approve", "list",
                                     "console", "app"])
def test_every_documented_command_exists(command):
    done = subprocess.run([str(BIN), command, "--help"], capture_output=True,
                          text=True, cwd=ROOT, timeout=60)
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(not BIN.exists(), reason="console script not installed")
def test_the_target_app_starts_from_the_installed_command():
    """The first thing the README asks anyone to run."""
    port = _free_port()
    proc = subprocess.Popen([str(BIN), "app", "--tenant", "mendota", "--port", str(port)],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        deadline = time.time() + 25
        body = None
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"the app exited immediately:\n{proc.stdout.read()}")
            try:
                body = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/admin/health", timeout=1).read().decode()
                break
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.25)
        assert body and '"ok":true' in body.replace(" ", ""), body
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.skipif(not BIN.exists(), reason="console script not installed")
def test_listing_the_catalogue_works_from_the_command_line():
    done = subprocess.run([str(BIN), "list"], capture_output=True, text=True,
                          cwd=ROOT, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "open_savings_subaccount" in done.stdout
