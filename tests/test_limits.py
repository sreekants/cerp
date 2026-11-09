"""MPC001-035 request limits, MPC001-012 correlation ids on every error, MPC001-067 error hygiene."""

from __future__ import annotations

import io
import json
import re
from typing import Any

import anyio
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send

from cerp.app.asgi import build_app
from cerp.app.config import LimitsSection, Settings
from cerp.app.limits import RequestLimits, _Buckets
from cerp.app.logs import configure_logging
from cerp.protocol.tooling import register_tool
from cerp.tools.describe import READ_ONLY
from tests.test_protocol import ACCEPT, MODERN, TOKEN, post


def _app(**limits: Any) -> TestClient:
    app, _, _ = build_app(Settings.model_validate({"limits": limits}), {})
    return TestClient(app, base_url="http://127.0.0.1:7411")


def test_large_headers_get_431() -> None:
    with _app() as c:
        r = post(c, "tools/list", headers={"X-Big": "x" * 20_000})
    assert r.status_code == 431
    assert r.json()["error"]["data"]["correlation_id"]


def test_rate_limit_gets_429_with_retry_after() -> None:
    with _app(rate_per_second=0.5, burst=2) as c:
        codes = [post(c, "tools/list").status_code for _ in range(3)]
        r = post(c, "tools/list")
    assert codes == [200, 200, 429]
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1
    assert r.json()["error"]["data"]["correlation_id"]


def test_anonymous_callers_share_a_stricter_bucket() -> None:
    s = Settings.model_validate(
        {
            "auth": {"static_tokens": {"enabled": True, "tokens": [{"token_env": "T", "user": "u"}]}},
            "limits": {"anonymous_burst": 1, "anonymous_rate_per_second": 0.1, "burst": 50},
        }
    )
    app, _, _ = build_app(s, {"T": TOKEN})
    with TestClient(app, base_url="http://127.0.0.1:7411") as c:
        assert post(c, "tools/list").status_code == 401  # admitted, then refused by auth
        assert post(c, "tools/list").status_code == 429  # second anonymous request: bucket empty
        authed = {"Authorization": f"Bearer {TOKEN}"}
        assert all(post(c, "tools/list", headers=authed).status_code == 200 for _ in range(5))


def test_concurrency_and_refill() -> None:
    now = [0.0]
    b = _Buckets(lambda: now[0])
    assert b.acquire("k", rate=10, burst=20, concurrent=2) is None
    assert b.acquire("k", rate=10, burst=20, concurrent=2) is None
    assert b.acquire("k", rate=10, burst=20, concurrent=2) == 1.0  # two in flight
    b.release("k")
    assert b.acquire("k", rate=10, burst=20, concurrent=2) is None
    b2 = _Buckets(lambda: now[0])
    assert b2.acquire("k", rate=1, burst=1, concurrent=9) is None
    wait = b2.acquire("k", rate=1, burst=1, concurrent=9)
    assert wait is not None
    assert 0 < wait <= 1
    now[0] += 1.0
    assert b2.acquire("k", rate=1, burst=1, concurrent=9) is None


def test_slow_request_gets_504() -> None:
    async def slow(scope: Scope, receive: Receive, send: Send) -> None:
        await anyio.sleep(1)

    lim = LimitsSection(request_timeout_s=0.05)
    c = TestClient(RequestLimits(slow, path="/mcp", limits=lambda: lim))  # no lifespan for a bare app
    r = c.post("/mcp", json={})
    assert r.status_code == 504
    assert r.json()["error"]["data"]["correlation_id"]


# ---------------------------------------------------------------- MPC001-012 and MPC001-067

SECRET_PATH = "/srv/webos/data/security.s3db"  # noqa: S105 - a fake path, not a credential
LEAKS = re.compile(r"Traceback|\.py\b|\.s3db|/srv/|SELECT|sqlite|security", re.I)


def _faults(c: TestClient) -> list[Any]:
    hdr = {**ACCEPT, "MCP-Protocol-Version": MODERN}
    call = {"name": "boom", "arguments": {}}
    return [
        post(c, "tools/list", headers={"Mcp-Method": "tools/call"}),  # SDK transport error (-32020)
        post(c, "tools/list", version="1999-01-01"),  # unsupported revision
        c.post("/mcp", content=b'{"jsonrpc": "2.0", "id": 1, "id": 2}', headers=hdr),  # duplicate key
        c.post("/mcp", content=b"[" * 200 + b"]" * 200, headers=hdr),  # too deep
        post(c, "tools/call", {"name": "no.such", "arguments": {}}),  # unknown tool
        post(c, "tools/call", call),  # internal failure with a revealing message
        post(c, "nope/method"),  # method not found
    ]


def _cids(r: Any) -> list[str]:
    text = r.text
    if "data:" in text:
        text = next(line[5:] for line in text.splitlines() if line.startswith("data:"))
    doc = json.loads(text)
    found = []
    err = doc.get("error")
    if isinstance(err, dict):
        found.append(err.get("data", {}).get("correlation_id"))
    res = doc.get("result")
    if isinstance(res, dict) and res.get("isError"):
        found.append((res.get("structuredContent") or {}).get("error", {}).get("correlation_id"))
    return found


def test_every_error_has_a_logged_correlation_id_and_no_internals() -> None:
    buf = io.StringIO()
    configure_logging("INFO", None, stream=buf)
    app, mcp, _ = build_app(None, {})

    def boom() -> None:
        raise RuntimeError(f"SELECT password FROM users failed in {SECRET_PATH}")  # noqa: S608 - bait text

    register_tool(mcp, boom, name="boom", title="Boom", annotations=READ_ONLY)
    with TestClient(app, base_url="http://127.0.0.1:7411") as c:
        responses = _faults(c)
    log_text = buf.getvalue()  # JSON lines: the correlation id is a field of every line
    for r in responses:
        cids = _cids(r)
        assert cids, r.text
        assert all(cids), r.text
        for cid in cids:
            assert cid in log_text, (cid, r.text)
        assert not LEAKS.search(r.text), r.text
    assert SECRET_PATH in log_text  # the detail went to the log instead


def test_debug_mode_is_off() -> None:
    app, _, _ = build_app(None, {})
    assert app.debug is False
