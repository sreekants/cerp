"""MPC001-131: the load harness runs end to end against a live server (seconds here, 1 h for acceptance)."""

from __future__ import annotations

import importlib.util
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import TICKET_EXPOSURE

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "load.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("cerp_load_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cerp_load_script"] = mod  # dataclasses need their module registered
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(
    tmp_path: Path, registry_file: Path, write_yaml: Callable[[str, Any], Path], dbroot: Path
) -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    cfg = tmp_path / "cerp.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "registry": {"path": str(registry_file)},
                "exposure": {"path": str(write_yaml("e.yaml", TICKET_EXPOSURE))},
                "data": {"dbroot": str(dbroot)},
                "limits": {"rate_per_second": 10000, "burst": 10000, "concurrent_per_principal": 50},
                "logging": {"level": "WARNING"},
            }
        ),
        encoding="utf-8",
    )
    cmd = [sys.executable, "-m", "cerp", "serve", "--config", str(cfg), "--port", str(port)]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603 - fixed argv
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=1) as r:
                    if r.status == 200:
                        break
            except OSError:
                time.sleep(0.2)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=40)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="subprocess server; runs in CI on Linux")
def test_short_load_run(server: str) -> None:
    r = _load().run(server, ["im.describe_ticket"], clients=20, seconds=3, token=None)
    assert r["calls"] > 50
    assert r["error_rate"] == 0
    assert r["rate"] > 0
