"""MPC001-037 readiness, MPC001-117 graceful shutdown, MPC001-112 trace ids in logs."""

from __future__ import annotations

import io
import json
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from cerp import cli
from cerp.app.asgi import build_app
from cerp.app.logs import configure_logging
from cerp.data.sqlite import Databases
from cerp.protocol import correlation
from tests.conftest import TICKET_EXPOSURE
from tests.test_exposure import _settings
from tests.test_protocol import ACCEPT, MODERN, _meta, post

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE}-00f067aa0ba902b7-01"


def _client(registry_file: Path, exposure: Path, dbroot: Path) -> tuple[TestClient, Any]:
    app, _, rt = build_app(_settings(registry_file, exposure, dbroot), {})
    return TestClient(app, base_url="http://127.0.0.1:7411"), rt


# ---------------------------------------------------------------- readiness (MPC001-037)


def test_ready_when_everything_opens(
    registry_file: Path, write_yaml: Callable[[str, Any], Path], dbroot: Path
) -> None:
    c, rt = _client(registry_file, write_yaml("e.yaml", TICKET_EXPOSURE), dbroot)
    with c:
        r = c.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ready"}
    checks = rt.readiness()
    assert checks["configuration"]
    assert checks["registry"]
    assert checks["database:im"]


def test_not_ready_hides_details_and_logs_them(
    registry_file: Path,
    write_yaml: Callable[[str, Any], Path],
    dbroot: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    c, _ = _client(registry_file, write_yaml("e.yaml", TICKET_EXPOSURE), dbroot)
    (dbroot / "im.s3db").rename(dbroot / "im.gone")
    with c:
        r = c.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"status": "not ready"}
    assert "im" not in r.text
    assert any("database:im" in rec.getMessage() for rec in caplog.records)


def test_health_and_ready_need_no_token() -> None:
    app, _, _ = build_app(None, {})
    with TestClient(app, base_url="http://127.0.0.1:7411") as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/readyz").status_code == 200


# ---------------------------------------------------------------- shutdown (MPC001-117)


def test_close_all_closes_connections_from_every_thread(dbroot: Path) -> None:
    dbs = Databases({"im": dbroot / "im.s3db"})
    opened: list[sqlite3.Connection] = []

    def worker() -> None:
        with dbs.reader("im") as con:
            opened.append(con)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    with dbs.reader("im") as con:
        opened.append(con)
    dbs.close_all()
    for con in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            con.execute("SELECT 1")


def test_close_all_rolls_back_open_write(dbroot: Path) -> None:
    dbs = Databases({"im": dbroot / "im.s3db"})
    with dbs.reader("im") as con:
        before = con.execute("SELECT count(*) FROM tickets").fetchone()[0]
    writer = dbs._conn("im", writable=True)  # simulate a write cut off mid-transaction
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO tickets (short_name) VALUES ('half-done')")
    dbs.close_all()
    con = sqlite3.connect(dbroot / "im.s3db")
    try:
        assert con.execute("SELECT count(*) FROM tickets").fetchone()[0] == before
    finally:
        con.close()


def test_serve_waits_for_in_flight_requests_and_closes_databases(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        seen.update(kwargs)

    closed: list[bool] = []
    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    monkeypatch.setattr(Databases, "close_all", lambda self: closed.append(True))
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)
    assert cli.main(["serve"]) == 0
    assert seen["timeout_graceful_shutdown"] == cli.GRACEFUL_SECONDS


# ---------------------------------------------------------------- trace ids (MPC001-112)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (TRACEPARENT, TRACE),
        (TRACEPARENT.upper(), TRACE),
        (f"00-{'0' * 32}-00f067aa0ba902b7-01", None),
        (f"00-{TRACE}-{'0' * 16}-01", None),
        ("00-abc-def-01", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_traceparent(value: str | None, expected: str | None) -> None:
    assert correlation.parse_traceparent(value) == expected


def _log_lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.startswith("{")]


@pytest.mark.parametrize("where", ["header", "meta"])
def test_client_trace_id_reaches_the_logs(where: str) -> None:
    buf = io.StringIO()
    configure_logging("INFO", None, stream=buf)
    app, _, _ = build_app(None, {})
    with TestClient(app, base_url="http://127.0.0.1:7411") as c:
        if where == "header":
            r = post(c, "tools/list", headers={"traceparent": TRACEPARENT})
        else:
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {"_meta": {**_meta(), "traceparent": TRACEPARENT}},
            }
            hdr = {**ACCEPT, "MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/list"}
            r = c.post("/mcp", json=body, headers=hdr)
    assert r.status_code == 200
    lines = [rec for rec in _log_lines(buf) if rec["component"].startswith("cerp")]
    assert any(rec["trace_id"] == TRACE for rec in lines), lines
