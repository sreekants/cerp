"""MPC001-115 reload: exposure changes reach the live tool list; bad files change nothing; file watching."""

from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
import yaml
from mcp import Client

from cerp.app.config import ConfigStore, load_settings
from cerp.app.server import build_runtime, reload_exposure
from cerp.app.watch import FileWatcher
from cerp.data.exposure import ExposureError
from tests.conftest import TICKET_EXPOSURE

LINK = {"verbs": ["describe"], "fields": ["id"], "description": "Links between tickets."}


def _setup(
    tmp_path: Path, registry_file: Path, dbroot: Path, write_yaml: Callable[[str, Any], Path]
) -> tuple[Any, Any, Path]:
    exposure = write_yaml("e.yaml", TICKET_EXPOSURE)
    cfg = tmp_path / "cerp.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "registry": {"path": str(registry_file)},
                "exposure": {"path": str(exposure)},
                "data": {"dbroot": str(dbroot)},
            }
        ),
        encoding="utf-8",
    )
    store = ConfigStore(load_settings(cfg, {}), cfg)
    mcp, rt = build_runtime(store.current, {}, store)
    return mcp, rt, exposure


def _names(mcp: Any) -> list[str]:
    async def go() -> list[str]:
        async with Client(mcp) as c:
            return sorted(t.name for t in (await c.list_tools()).tools)

    return anyio.run(go)


def test_exposure_change_reaches_tools_list(
    tmp_path: Path, registry_file: Path, dbroot: Path, write_yaml: Callable[[str, Any], Path]
) -> None:
    mcp, rt, exposure = _setup(tmp_path, registry_file, dbroot, write_yaml)
    assert _names(mcp) == ["im.describe_ticket"]
    spec = copy.deepcopy(TICKET_EXPOSURE)
    spec["models"]["objects/support/ticket_link"] = LINK
    exposure.write_text(yaml.safe_dump(spec), encoding="utf-8")
    added, removed = reload_exposure(mcp, rt)
    assert added == ["im.describe_ticket_link"]
    assert removed == []
    assert _names(mcp) == ["im.describe_ticket", "im.describe_ticket_link"]
    exposure.write_text(yaml.safe_dump({"models": {"objects/support/ticket_link": LINK}}), encoding="utf-8")
    _, removed = reload_exposure(mcp, rt)
    assert removed == ["im.describe_ticket"]
    assert _names(mcp) == ["im.describe_ticket_link"]
    assert rt.tool_names == ["im.describe_ticket_link"]


def test_invalid_exposure_leaves_tools_untouched(
    tmp_path: Path, registry_file: Path, dbroot: Path, write_yaml: Callable[[str, Any], Path]
) -> None:
    mcp, rt, exposure = _setup(tmp_path, registry_file, dbroot, write_yaml)
    spec = copy.deepcopy(TICKET_EXPOSURE)
    spec["models"]["objects/support/ticket"]["fields"].append("no_such_column")
    exposure.write_text(yaml.safe_dump(spec), encoding="utf-8")
    with pytest.raises(ExposureError):
        reload_exposure(mcp, rt)
    assert _names(mcp) == ["im.describe_ticket"]


def test_reload_under_load(
    tmp_path: Path, registry_file: Path, dbroot: Path, write_yaml: Callable[[str, Any], Path]
) -> None:
    """Calls to a tool that stays exposed never fail while the exposure is reloaded repeatedly."""
    mcp, rt, exposure = _setup(tmp_path, registry_file, dbroot, write_yaml)
    with_link = copy.deepcopy(TICKET_EXPOSURE)
    with_link["models"]["objects/support/ticket_link"] = LINK
    stop = threading.Event()
    reloads = 0

    def reloader() -> None:
        nonlocal reloads
        while not stop.is_set():
            spec = with_link if reloads % 2 else TICKET_EXPOSURE
            exposure.write_text(yaml.safe_dump(spec), encoding="utf-8")
            reload_exposure(mcp, rt)
            reloads += 1

    async def caller() -> list[bool]:
        out = []
        async with Client(mcp) as c:
            for _ in range(200):
                r = await c.call_tool("im.describe_ticket", {})
                out.append(bool(r.is_error))
        return out

    t = threading.Thread(target=reloader)
    t.start()
    try:
        errors = anyio.run(caller)
    finally:
        stop.set()
        t.join()
    assert reloads > 1
    assert not any(errors)


def test_file_watcher_calls_back_on_change(tmp_path: Path) -> None:
    f = tmp_path / "cerp.yaml"
    f.write_text("a: 1\n", encoding="utf-8")
    seen: list[int] = []
    w = FileWatcher(lambda: [f], 0.05, lambda: seen.append(1))
    assert not w.poll()
    f.write_text("a: 22\n", encoding="utf-8")
    assert w.poll()
    assert seen == [1]
    w.start()
    f.write_text("a: 333\n", encoding="utf-8")
    deadline = time.monotonic() + 3
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    w.stop()
    assert len(seen) == 2


def test_watcher_survives_a_failing_reload(tmp_path: Path) -> None:
    f = tmp_path / "x.yaml"
    f.write_text("1", encoding="utf-8")

    def boom() -> None:
        raise RuntimeError("bad")

    w = FileWatcher(lambda: [f], 1, boom)
    f.write_text("22", encoding="utf-8")
    assert w.poll()  # logged, not raised
