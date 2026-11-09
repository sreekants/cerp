"""MPC001-132: the soak harness runs end to end (a few seconds here; 24 h on the reference machine)."""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "soak.py"


def _soak() -> Any:
    spec = importlib.util.spec_from_file_location("soak", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_slope() -> None:
    soak = _soak()
    assert soak.slope_mb_per_run([(0, 100_000_000), (10, 100_000_000)]) == 0
    assert round(soak.slope_mb_per_run([(0, 0), (10, 10_000_000)]), 3) == 10.0


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RSS via /proc; Windows soaks need psutil")
def test_short_soak_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CERP__LIMITS__RATE_PER_SECOND", "100")  # above the soak rate (MPC001-035)
    monkeypatch.setenv("CERP__LIMITS__BURST", "100")
    out = tmp_path / "soak.csv"
    code = _soak().main(
        [
            "--port", str(_free_port()), "--tool", "no.such_tool", "--hours", str(3 / 3600),
            "--rate", "50", "--sample", "0.5", "--out", str(out),
        ]
    )  # fmt: skip
    rows = out.read_text(encoding="utf-8").splitlines()
    assert rows[0] == "elapsed_s,rss_bytes,calls,failures"
    assert len(rows) >= 4
    assert code == 0
