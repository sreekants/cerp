"""MPC001-116 kill switches: unit rules, tools/list filtering, refusal on call, reload without restart."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import yaml
from mcp import Client

from cerp.app.config import ConfigStore, load_settings
from cerp.app.server import build_runtime
from cerp.protocol.killswitch import NO_SWITCHES, KillSwitch
from tests.conftest import TICKET_EXPOSURE


def _ks(switches: dict[str, bool]) -> KillSwitch:
    return KillSwitch(lambda: switches)


def test_rules() -> None:
    assert NO_SWITCHES.blocked("im.create_ticket", read_only=False) is None
    assert _ks({"writes": True}).blocked("im.create_ticket", read_only=False) == "writes"
    assert _ks({"writes": True}).blocked("im.describe_ticket", read_only=True) is None
    assert _ks({"module:im": True}).blocked("im.describe_ticket", read_only=True) == "module:im"
    assert _ks({"module:crm": True}).blocked("im.describe_ticket", read_only=True) is None
    assert _ks({"tool:im.x": True}).blocked("im.x", read_only=True) == "tool:im.x"
    assert _ks({"tool:im.x": False}).blocked("im.x", read_only=True) is None


def test_filter_wire_tools() -> None:
    tools: list[Any] = [
        {"name": "im.describe_ticket", "annotations": {"readOnlyHint": True}},
        {"name": "im.create_ticket", "annotations": {"readOnlyHint": False}},
        {"name": "pm.create_task"},
    ]
    assert [t["name"] for t in _ks({"writes": True}).filter_tools(tools)] == ["im.describe_ticket"]


def _config(path: Path, registry_file: Path, exposure: Path, dbroot: Path, switches: dict[str, bool]) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "registry": {"path": str(registry_file)},
                "exposure": {"path": str(exposure)},
                "data": {"dbroot": str(dbroot)},
                "kill_switches": switches,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_switched_off_tool_is_hidden_refused_and_restored_on_reload(
    tmp_path: Path, registry_file: Path, write_yaml: Callable[[str, Any], Path], dbroot: Path
) -> None:
    exposure = write_yaml("e.yaml", TICKET_EXPOSURE)
    cfg = _config(tmp_path / "cerp.yaml", registry_file, exposure, dbroot, {"module:im": True})
    store = ConfigStore(load_settings(cfg, {}), cfg)
    mcp, _ = build_runtime(store.current, {}, store)

    async def names_and_call() -> tuple[list[str], bool, Any]:
        async with Client(mcp) as c:
            names = [t.name for t in (await c.list_tools()).tools]
            r = await c.call_tool("im.describe_ticket", {})
            return names, bool(r.is_error), r.structured_content

    names, is_error, content = anyio.run(names_and_call)
    assert names == []
    assert is_error
    assert content is not None
    assert content["error"]["code"] == "disabled"

    _config(cfg, registry_file, exposure, dbroot, {})
    assert store.reload({}) == ["kill_switches"]
    names, is_error, _ = anyio.run(names_and_call)
    assert names == ["im.describe_ticket"]
    assert not is_error


def test_writes_switch_keeps_read_tools(
    tmp_path: Path, registry_file: Path, write_yaml: Callable[[str, Any], Path], dbroot: Path
) -> None:
    cfg = _config(
        tmp_path / "cerp.yaml", registry_file, write_yaml("e.yaml", TICKET_EXPOSURE), dbroot, {"writes": True}
    )
    store = ConfigStore(load_settings(cfg, {}), cfg)
    mcp, _ = build_runtime(store.current, {}, store)

    async def go() -> list[str]:
        async with Client(mcp) as c:
            return [t.name for t in (await c.list_tools()).tools]

    assert anyio.run(go) == ["im.describe_ticket"]
