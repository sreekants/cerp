"""MPC001-080 exposure, MPC001-082 naming, describe tools end to end."""

from __future__ import annotations

import copy
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from cerp.app.config import Settings
from cerp.app.server import build_runtime
from cerp.data.exposure import ExposureError, load_exposure
from cerp.data.registry import load_registry
from cerp.data.sqlite import Databases
from cerp.protocol.naming import ToolNameError, check_unique, curated_name, generic_name
from tests.conftest import TICKET_EXPOSURE


def _settings(registry_file: Path, exposure: Path, dbroot: Path) -> Settings:
    return Settings.model_validate(
        {
            "registry": {"path": str(registry_file)},
            "exposure": {"path": str(exposure)},
            "data": {"dbroot": str(dbroot)},
        }
    )


def test_valid_exposure_loads(
    registry_file: Path, write_yaml: Callable[[str, Any], Path], dbroot: Path
) -> None:
    reg = load_registry(registry_file)
    dbs = Databases(reg.database_files(dbroot))
    exp = load_exposure(write_yaml("e.yaml", TICKET_EXPOSURE), reg, dbs.table_columns)
    (m,) = exp.models
    assert (m.module, m.name) == ("im", "ticket")
    assert exp.get("im", "ticket") is m


def test_unknown_field_fails_with_field_name(
    registry_file: Path, write_yaml: Callable[[str, Any], Path]
) -> None:
    bad = copy.deepcopy(TICKET_EXPOSURE)
    bad["models"]["objects/support/ticket"]["fields"].append("shoe_size")
    bad["models"]["objects/support/ticket"]["verbs"].append("obliterate")
    bad["models"]["objects/no/such"] = {"verbs": ["get"], "fields": ["id"]}
    with pytest.raises(ExposureError) as exc:
        load_exposure(write_yaml("e.yaml", bad), load_registry(registry_file))
    text = "\n".join(exc.value.errors)
    assert "shoe_size" in text
    assert "obliterate" in text
    assert "objects/no/such" in text


def test_dropped_column_is_detected_as_drift(
    registry_file: Path, write_yaml: Callable[[str, Any], Path], dbroot: Path
) -> None:
    con = sqlite3.connect(dbroot / "im.s3db")
    con.execute("ALTER TABLE tickets DROP COLUMN map_url")
    con.commit()
    con.close()
    reg = load_registry(registry_file)
    dbs = Databases(reg.database_files(dbroot))
    with pytest.raises(ExposureError, match=r"im\.tickets has drifted.*missing: map_url"):
        load_exposure(write_yaml("e.yaml", TICKET_EXPOSURE), reg, dbs.table_columns)


def test_server_registers_describe_tool(
    registry_file: Path, write_yaml: Callable[[str, Any], Path], dbroot: Path
) -> None:
    mcp, rt = build_runtime(_settings(registry_file, write_yaml("e.yaml", TICKET_EXPOSURE), dbroot), {})
    assert rt.tool_names == ["im.describe_ticket"]

    async def go() -> dict[str, Any]:
        async with Client(mcp) as c:
            tools = await c.list_tools()
            assert [t.name for t in tools.tools] == ["im.describe_ticket"]
            assert tools.tools[0].annotations is not None
            assert tools.tools[0].annotations.read_only_hint is True
            r = await c.call_tool("im.describe_ticket", {})
            assert r.structured_content is not None
            return r.structured_content

    out = anyio.run(go)
    assert out["handle_format"] == "im:ticket:<id>"
    fields = {f["name"]: f for f in out["fields"]}
    assert fields["status"]["description"].startswith("1000 Open")
    assert fields["short_name"]["type"]
    assert out["sortable"] == ["creation_time"]


def test_drift_fails_server_startup(
    registry_file: Path, write_yaml: Callable[[str, Any], Path], dbroot: Path
) -> None:
    con = sqlite3.connect(dbroot / "im.s3db")
    con.execute("ALTER TABLE tickets ADD COLUMN extra TEXT")
    con.commit()
    con.close()
    with pytest.raises(ExposureError, match="new: extra"):
        build_runtime(_settings(registry_file, write_yaml("e.yaml", TICKET_EXPOSURE), dbroot), {})


def test_tool_names() -> None:
    assert generic_name("crm", "get", "account") == "crm.get_account"
    assert curated_name("im", "assign_incident") == "im.assign_incident"
    with pytest.raises(ToolNameError):
        generic_name("crm", "get", "Account")
    with pytest.raises(ToolNameError):
        curated_name("im", "x" * 70)
    with pytest.raises(ToolNameError, match="duplicate"):
        check_unique(["im.a", "im.b", "im.a"])
    with pytest.raises(ToolNameError):
        check_unique(["im a"])
