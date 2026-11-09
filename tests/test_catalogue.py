"""MPC001-143: the tool catalogue is generated from the running server and matches the golden file."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from cerp import cli
from cerp.app.catalogue import build_catalogue, to_json, to_markdown
from cerp.app.server import build_runtime
from tests.conftest import TICKET_EXPOSURE
from tests.test_exposure import _settings

GOLDEN = Path(__file__).resolve().parent / "golden" / "catalogue.json"


def test_catalogue_matches_golden(
    registry_file: Path, write_yaml: Callable[[str, Any], Path], dbroot: Path
) -> None:
    mcp, _ = build_runtime(_settings(registry_file, write_yaml("e.yaml", TICKET_EXPOSURE), dbroot), {})
    cat = build_catalogue(mcp)
    text = to_json(cat)
    if os.environ.get("CERP_UPDATE_GOLDEN"):
        GOLDEN.write_text(text, encoding="utf-8")
    assert GOLDEN.read_text(encoding="utf-8") == text, "regenerate with CERP_UPDATE_GOLDEN=1 if intended"
    (tool,) = cat["tools"]
    assert tool["name"] == "im.describe_ticket"
    assert tool["scope"] == "webos.im.read"
    assert "writes" not in tool["kill_switches"]
    assert "## `im.describe_ticket`" in to_markdown(cat)


def test_cli_writes_catalogue(
    tmp_path: Path,
    registry_file: Path,
    write_yaml: Callable[[str, Any], Path],
    dbroot: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = tmp_path / "cerp.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "registry": {"path": str(registry_file)},
                "exposure": {"path": str(write_yaml("e.yaml", TICKET_EXPOSURE))},
                "data": {"dbroot": str(dbroot)},
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "catalogue.md"
    assert cli.main(["catalogue", "--config", str(cfg), "--format", "md", "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("# Tool catalogue")
