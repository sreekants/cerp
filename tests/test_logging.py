"""MPC001-111 structured logging and MPC001-056 redaction."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from cerp.app.logs import JsonFormatter, configure_logging, redact
from cerp.protocol import correlation


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    configure_logging("INFO", None, stream=buf)
    yield buf
    for h in list(logging.getLogger().handlers):
        if getattr(h, "_cerp", False):
            logging.getLogger().removeHandler(h)


def test_lines_are_json_with_required_fields(stream: io.StringIO) -> None:
    token = correlation.bind("abc123")
    try:
        logging.getLogger("cerp.test").info("hello %s", "world")
    finally:
        correlation.reset(token)
    rec = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert set(rec) >= {"ts", "level", "component", "correlation_id", "msg"}
    assert rec["msg"] == "hello world"
    assert rec["correlation_id"] == "abc123"
    assert rec["component"] == "cerp.test"


@pytest.mark.parametrize(
    "secret",
    [
        "Authorization: Bearer abc.def.ghi",
        "api_key=sk-12345",
        "token: hunter2hunter2",
        "sk-ant-api03-XXXXXXXXXXXX",
    ],
)
def test_secrets_are_redacted(secret: str) -> None:
    out = redact(f"before {secret} after")
    assert "[REDACTED]" in out
    assert "hunter2" not in out
    assert "XXXXXXXX" not in out
    assert "abc.def" not in out


def test_rotating_file(tmp_path: Path) -> None:
    f = tmp_path / "logs" / "cerp.log"
    configure_logging("INFO", f, stream=io.StringIO())
    try:
        logging.getLogger("cerp.test").warning("to file")
    finally:
        for h in list(logging.getLogger().handlers):
            if getattr(h, "_cerp", False):
                h.close()
                logging.getLogger().removeHandler(h)
    assert json.loads(f.read_text().strip())["msg"] == "to file"


def test_exceptions_are_formatted_and_redacted() -> None:
    try:
        raise ValueError("password=letmein")  # noqa: TRY301
    except ValueError:
        import sys  # noqa: PLC0415

        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", None, sys.exc_info())
    out = json.loads(JsonFormatter().format(rec))
    assert "letmein" not in out["exc"]
