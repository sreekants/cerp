"""Protocol behaviour over HTTP and in-process.

MPC001-013 revisions, -014 stateless, -015 discovery, -017 capabilities, -030 endpoint, -032 routing headers,
-059 static tokens, -119 versioning.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import anyio
import mcp_types
import pytest
from mcp import Client
from starlette.testclient import TestClient

from cerp import __version__
from cerp.app.asgi import build_app
from cerp.app.config import Settings
from cerp.app.server import build_server

MODERN = "2026-07-28"
TOKEN = "t" * 32  # test-only value, not a secret
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _meta(version: str = MODERN) -> dict[str, Any]:
    return {
        mcp_types.PROTOCOL_VERSION_META_KEY: version,
        mcp_types.CLIENT_CAPABILITIES_META_KEY: {},
        mcp_types.CLIENT_INFO_META_KEY: {"name": "test", "version": "1"},
    }


def _body(r: Any) -> dict[str, Any]:
    text: str = r.text
    if text.startswith("event:") or "data:" in text:
        text = next(line[5:] for line in text.splitlines() if line.startswith("data:"))
    out: dict[str, Any] = json.loads(text)
    return out


@contextmanager
def http(settings: Settings | None = None, environ: Mapping[str, str] | None = None) -> Iterator[TestClient]:
    app, _, _ = build_app(settings, environ or {})
    with TestClient(app, base_url="http://127.0.0.1:7411") as c:
        yield c


def post(
    c: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    version: str = MODERN,
) -> Any:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {**(params or {}), "_meta": _meta(version)},
    }
    hdr = {**ACCEPT, "MCP-Protocol-Version": version, "Mcp-Method": method}
    if params and "name" in params:
        hdr["Mcp-Name"] = params["name"]
    return c.post("/mcp", json=body, headers={**hdr, **(headers or {})})


# ---------------------------------------------------------------- discovery and versioning


def test_discovery_reports_instructions_and_build_version() -> None:
    settings = Settings.model_validate({"server": {"instructions": "Use the im tools for tickets."}})
    with http(settings) as c:
        r = post(c, "server/discover")
    assert r.status_code == 200
    result = _body(r)["result"]
    assert result["instructions"] == "Use the im tools for tickets."
    assert result["_meta"][mcp_types.SERVER_INFO_META_KEY]["version"] == __version__


def test_in_process_initialize_reports_version() -> None:
    async def go() -> tuple[str | None, str | None]:
        async with Client(build_server()) as c:
            return c.server_info.version, c.instructions

    version, instructions = anyio.run(go)
    assert version == __version__
    assert instructions


def test_unsupported_revision_is_refused_with_supported_list() -> None:
    settings = Settings.model_validate({"server": {"protocol_versions": ["2025-11-25"]}})
    with http(settings) as c:
        err = _body(post(c, "tools/list"))["error"]
    assert err["code"] == mcp_types.UNSUPPORTED_PROTOCOL_VERSION
    assert err["data"]["supported"] == ["2025-11-25"]
    assert err["data"]["correlation_id"]


def test_unknown_revision_in_config_fails_startup() -> None:
    from cerp.app.config import ConfigError  # noqa: PLC0415

    with pytest.raises(ConfigError, match="not supported by the pinned MCP SDK"):
        build_server(Settings.model_validate({"server": {"protocol_versions": ["1999-01-01"]}}))


# ---------------------------------------------------------------- capabilities (MPC001-017)


def test_tools_capability_declared() -> None:
    with http() as c:
        caps = _body(post(c, "server/discover"))["result"]["capabilities"]
    assert "tools" in caps


@pytest.mark.xfail(
    reason="SDK gap: MCPServer always declares resources and prompts (coverage map, MPC001-017)", strict=True
)
def test_unimplemented_capabilities_not_declared() -> None:
    with http() as c:
        caps = _body(post(c, "server/discover"))["result"]["capabilities"]
    assert "resources" not in caps
    assert "prompts" not in caps


def test_enabling_unimplemented_features_fails_startup() -> None:
    from cerp.app.config import ConfigError  # noqa: PLC0415

    with pytest.raises(ConfigError, match=r"features\.resources"):
        build_server(Settings.model_validate({"features": {"resources": True}}))
    with pytest.raises(ConfigError, match=r"features\.tasks"):
        build_server(Settings.model_validate({"features": {"tasks": True}}))


# ---------------------------------------------------------------- endpoint (MPC001-030) and routing (-032)


def test_health_route() -> None:
    with http() as c:
        r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__}


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_unhandled_methods_get_405_with_allow(method: str) -> None:
    with http() as c:
        r = c.request(method, "/mcp", headers=ACCEPT)
    assert r.status_code == 405
    assert r.headers.get("allow")


def test_configured_path_is_used() -> None:
    with http(Settings.model_validate({"http": {"path": "/erp"}})) as c:
        assert post(c, "tools/list").status_code == 404  # /mcp is not routed
        r = c.post(
            "/erp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": _meta()}},
            headers={**ACCEPT, "MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/list"},
        )
    assert r.status_code == 200


def test_routing_header_disagreeing_with_body_is_400() -> None:
    with http() as c:
        r = post(c, "tools/list", headers={"Mcp-Method": "tools/call"})
    assert r.status_code == 400


def test_missing_mcp_name_on_tools_call_is_400() -> None:
    with http() as c:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "x", "arguments": {}, "_meta": _meta()},
        }
        r = c.post(
            "/mcp", json=body, headers={**ACCEPT, "MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/call"}
        )
    assert r.status_code == 400


# ---------------------------------------------------------------- stateless (MPC001-014)


def test_restart_between_calls_does_not_change_outcome() -> None:
    with http() as c:
        first = _body(post(c, "tools/list"))["result"]["tools"]
    with http() as c:  # a fresh server process: no session survives
        second = _body(post(c, "tools/list"))["result"]["tools"]
    assert first == second


# ---------------------------------------------------------------- static tokens (MPC001-059, -015)


def _static(host: str = "127.0.0.1") -> Settings:
    return Settings.model_validate(
        {
            "http": {"host": host},
            "auth": {
                "static_tokens": {
                    "enabled": True,
                    "tokens": [{"token_env": "CERP_TEST_TOKEN", "user": "alice"}],
                }
            },
        }
    )


def test_static_token_required_and_accepted(caplog: pytest.LogCaptureFixture) -> None:
    with http(_static(), {"CERP_TEST_TOKEN": TOKEN}) as c:
        denied = post(c, "tools/list")
        allowed = post(c, "tools/list", headers={"Authorization": f"Bearer {TOKEN}"})
        wrong = post(c, "tools/list", headers={"Authorization": "Bearer " + "x" * 32})
    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert wrong.status_code == 401
    assert "static development tokens are ENABLED" in caplog.text
    assert TOKEN not in caplog.text


def test_discovery_requires_auth_when_enabled() -> None:
    with http(_static(), {"CERP_TEST_TOKEN": TOKEN}) as c:
        assert post(c, "server/discover").status_code == 401


def test_static_tokens_need_a_strong_value() -> None:
    from cerp.auth.static import StaticTokenError  # noqa: PLC0415

    with pytest.raises(StaticTokenError, match="at least"):
        build_server(_static(), {"CERP_TEST_TOKEN": "short"})
