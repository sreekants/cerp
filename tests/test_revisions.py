"""MPC001-013: a small scenario per configured protocol revision (the full conformance suite is MPC001-151).

For every revision in the default configuration: the server negotiates it, lists tools and calls one, over
Streamable HTTP. Handshake-era revisions use ``initialize``; 2026-07-28 uses per-request ``_meta``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from starlette.testclient import TestClient

from cerp.app.asgi import build_app
from cerp.app.config import Settings
from cerp.app.server import SDK_PROTOCOL_VERSIONS
from tests.conftest import TICKET_EXPOSURE
from tests.test_exposure import _settings
from tests.test_protocol import ACCEPT, _body, _meta

CONFIGURED = Settings().server.protocol_versions


def test_configured_revisions_are_supported_by_the_sdk() -> None:
    assert set(CONFIGURED) <= set(SDK_PROTOCOL_VERSIONS)


def _rpc(c: TestClient, version: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = dict(params or {})
    hdr = {**ACCEPT, "MCP-Protocol-Version": version}
    if version in MODERN_PROTOCOL_VERSIONS:
        params["_meta"] = _meta(version)
        hdr["Mcp-Method"] = method
        if "name" in params:
            hdr["Mcp-Name"] = params["name"]
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, headers=hdr)
    assert r.status_code == 200, r.text
    return _body(r)


@pytest.mark.parametrize("version", CONFIGURED)
def test_scenario_per_revision(
    version: str, registry_file: Path, write_yaml: Callable[[str, Any], Path], dbroot: Path
) -> None:
    app, _, _ = build_app(_settings(registry_file, write_yaml("e.yaml", TICKET_EXPOSURE), dbroot), {})
    with TestClient(app, base_url="http://127.0.0.1:7411") as c:
        if version not in MODERN_PROTOCOL_VERSIONS:
            init = _rpc(
                c,
                version,
                "initialize",
                {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
            )
            assert init["result"]["protocolVersion"] == version
        tools = _rpc(c, version, "tools/list")["result"]["tools"]
        assert [t["name"] for t in tools] == ["im.describe_ticket"]
        called = _rpc(c, version, "tools/call", {"name": "im.describe_ticket", "arguments": {}})["result"]
        assert not called.get("isError")
        assert called["structuredContent"]["handle_format"] == "im:ticket:<id>"
