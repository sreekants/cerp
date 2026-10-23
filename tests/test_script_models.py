"""MPC001-164: Script models are listed for porting and cannot be exposed until ported."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from cerp.data.exposure import ExposureError, load_exposure
from cerp.data.registry import load_registry

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "script_models.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("script_models", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_model_cannot_be_exposed(registry_file: Path, write_yaml: Callable[[str, Any], Path]) -> None:
    exposure = {"models": {"objects/assets/asset": {"verbs": ["describe"], "fields": ["id"]}}}
    with pytest.raises(ExposureError, match="MPC001-164"):
        load_exposure(write_yaml("e.yaml", exposure), load_registry(registry_file))


def test_port_list_has_every_script_model_and_nothing_else(registry_file: Path) -> None:
    raw = yaml.safe_load(registry_file.read_text(encoding="utf-8"))
    scripts = {k for k, m in raw["models"].items() if (m.get("identity") or {}).get("type") == "Script"}
    assert scripts  # the fixture portal has at least one
    text = _module().render(raw)
    listed = {line.split("`")[1] for line in text.splitlines() if line.startswith("| `")}
    assert listed == scripts
    assert f"**Status:** {len(scripts)} Script models" in text
