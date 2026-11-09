"""MPC001-120: the service definitions are well formed and least-privilege."""

from __future__ import annotations

import configparser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _unit() -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # type: ignore[assignment,method-assign]  # keep systemd's case-sensitive keys
    cp.read_string((ROOT / "packaging" / "linux" / "cerp.service").read_text(encoding="utf-8"))
    return cp


def test_systemd_unit_runs_cerp_serve_as_its_own_user() -> None:
    svc = _unit()["Service"]
    assert svc["ExecStart"].split()[1:3] == ["serve", "--config"]
    assert svc["User"] == "cerp"
    assert svc["NoNewPrivileges"] == "yes"
    assert svc["ProtectSystem"] == "strict"
    assert svc["CapabilityBoundingSet"] == ""
    assert int(svc["TimeoutStopSec"]) > 30  # longer than the graceful drain (MPC001-117)
    assert _unit()["Install"]["WantedBy"] == "multi-user.target"


def test_windows_script_uses_a_service_account_and_scoped_grants() -> None:
    text = (ROOT / "packaging" / "windows" / "install-service.ps1").read_text(encoding="utf-8")
    assert "NT SERVICE\\$name" in text
    assert "LocalSystem" not in text
    assert text.count("icacls") == 5
    assert "serve --config" in text
