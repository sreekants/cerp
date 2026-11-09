"""MPC001-141/142: the configuration reference is current and the guides cover the required topics."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "docs" / "operations"


def _script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_config_reference_is_current() -> None:
    expected = _script("config_reference").render()
    assert (OPS / "config-reference.md").read_text(encoding="utf-8") == expected, (
        "run: python scripts/config_reference.py docs/operations/config-reference.md"
    )


def test_integration_guide_covers_the_required_topics() -> None:
    text = (OPS / "integration-guide.md").read_text(encoding="utf-8").lower()
    for topic in (
        "install", "configur", "registry", "expose", "curated", "option a", "option b",
        "key rotation", "kill switch", "monitoring", "backups", "troubleshooting",
    ):  # fmt: skip
        assert topic in text, topic


def test_connection_guide_covers_each_surface() -> None:
    text = (OPS / "claude-connection-guide.md").read_text(encoding="utf-8")
    for surface in ("Claude Code", "Claude Desktop", "claude.ai", "Claude API", "static", "OAuth"):
        assert surface in text, surface
