"""Structured JSON logging (MPC001-111, MPC001-112).

Every line is one JSON object with ``ts``, ``level``, ``component`` (the logger name), ``correlation_id``,
``trace_id`` (from the client's ``traceparent``) and ``msg``, written to stderr and, when configured, to a
rotating file. A redaction filter removes bearer tokens and anything that looks like an API key from the
message before it is written (MPC001-056); code must still never pass secrets, full tool arguments or
customer message bodies to the logger.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any, TextIO

from cerp.protocol import correlation

_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(sk-ant-[a-z0-9]{2,}-)[A-Za-z0-9_-]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)\S+"),
)
REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: m.group(1) + REDACTED, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        cid = getattr(record, "correlation_id", None) or correlation.current()
        out: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "component": record.name,
            "correlation_id": cid,
            "trace_id": correlation.current_trace(),
            "msg": redact(record.getMessage()),
        }
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, ensure_ascii=False)


def configure_logging(
    level: str = "INFO",
    file: Path | None = None,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    backups: int = 5,
    stream: TextIO | None = None,
) -> None:
    """Route the root logger (and the SDK's) through the JSON formatter. Safe to call more than once."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_cerp", False):
            root.removeHandler(h)
            h.close()
    fmt = JsonFormatter()
    handlers: list[logging.Handler] = [logging.StreamHandler(stream or sys.stderr)]
    if file is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
            )
        )
    for h in handlers:
        h.setFormatter(fmt)
        h._cerp = True  # type: ignore[attr-defined]  # marker so reconfiguration removes only our handlers
        root.addHandler(h)
    root.setLevel(level)


def configure_audit(file: Path | None, *, max_bytes: int = 50 * 1024 * 1024, backups: int = 10) -> None:
    """Route ``cerp.audit`` records (MPC001-110). With a file, records go only there, one JSON object per
    line; without one they go to the normal log. Safe to call more than once."""
    audit = logging.getLogger("cerp.audit")
    for h in list(audit.handlers):
        if getattr(h, "_cerp", False):
            audit.removeHandler(h)
            h.close()
    audit.setLevel(logging.INFO)
    if file is None:
        audit.propagate = True
        return
    file.parent.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(message)s"))
    h._cerp = True  # type: ignore[attr-defined]  # marker so reconfiguration removes only our handlers
    audit.addHandler(h)
    audit.propagate = False
