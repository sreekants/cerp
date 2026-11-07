"""JSON Schemas for generic tools, generated from the registry (MPC001-081).

One documented type table maps SQLite column types to JSON Schema (Draft 2020-12):

=====================================  ==========================================================
Column type                            Schema
=====================================  ==========================================================
``INTEGER``, ``INT``, ``BIGINT``, …    ``{"type": "integer"}``
``REAL``, ``FLOAT``, ``DOUBLE``,       ``{"type": "string", "pattern": DECIMAL}`` — exact decimals
``NUMERIC``, ``DECIMAL``, ``MONEY``    as strings, never floats (MPC001-010 requirement 3)
``VARCHAR(n)``, ``CHAR(n)``            ``{"type": "string", "maxLength": n}``
``TEXT``, ``CLOB``, untyped            ``{"type": "string", "maxLength": 65536}``
``DATE``                               ``{"type": "string", "format": "date"}``
``TIMESTAMP``, ``DATETIME``, ``TIME``  ``{"type": "string", "format": "date-time"}``; also any
                                       column named in the model's ``ConvertTimeFields``
``BLOB``                               not exposed (refused)
=====================================  ==========================================================

Nullable columns also accept ``null``. Only allow-listed fields appear. Required input fields are NOT NULL
columns without a database default and without a ``ModelDefaults`` value; the primary key is never input.
Output is deterministic (fields in allow-list order) so schemas diff cleanly.

This module imports nothing else from cerp except the data layer (MPC001-005).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from cerp.data.registry import Column, Model

DECIMAL = r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$"
TEXT_MAX = 65536
_SIZED = re.compile(r"^\s*(?:N?VAR)?CHAR(?:ACTER)?\s*\(\s*(\d+)\s*\)", re.I)


class UnsupportedColumn(ValueError):
    pass


def column_schema(col: Column, model: Model) -> dict[str, Any]:
    t = col.type.strip().upper()
    base = t.split("(", 1)[0].strip()
    out: dict[str, Any]
    if col.name in model.convert_time_fields or base in ("TIMESTAMP", "DATETIME", "TIME"):
        out = {"type": "string", "format": "date-time"}
    elif base == "DATE":
        out = {"type": "string", "format": "date"}
    elif "INT" in base:
        out = {"type": "integer"}
    elif base in ("REAL", "FLOAT", "DOUBLE", "DOUBLE PRECISION", "NUMERIC", "DECIMAL", "MONEY"):
        out = {"type": "string", "pattern": DECIMAL}
    elif m := _SIZED.match(t):
        out = {"type": "string", "maxLength": int(m.group(1))}
    elif base in ("TEXT", "CLOB", "") or "CHAR" in base:
        out = {"type": "string", "maxLength": TEXT_MAX}
    else:
        raise UnsupportedColumn(f"{model.table}.{col.name}: column type {col.type!r} cannot be exposed")
    if not col.not_null and not col.primary_key:
        out["type"] = [out["type"], "null"]
    return out


def _columns(model: Model, fields: tuple[str, ...]) -> list[Column]:
    by_name = {c.name: c for c in model.columns or ()}
    return [by_name[f] for f in fields if f in by_name]


def record_schema(model: Model, fields: tuple[str, ...], descriptions: Mapping[str, str]) -> dict[str, Any]:
    """Output schema of one record: the allow-listed fields, nothing else."""
    props: dict[str, Any] = {}
    for col in _columns(model, fields):
        s = column_schema(col, model)
        if col.name in descriptions:
            s["description"] = descriptions[col.name]
        props[col.name] = s
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": props,
        "additionalProperties": False,
    }


def create_schema(model: Model, fields: tuple[str, ...], descriptions: Mapping[str, str]) -> dict[str, Any]:
    """Input schema for creating a record: allow-listed fields except the primary key."""
    schema = record_schema(model, fields, descriptions)
    pk = {c.name for c in model.columns or () if c.primary_key}
    schema["properties"] = {k: v for k, v in schema["properties"].items() if k not in pk}
    required = [
        c.name
        for c in _columns(model, fields)
        if c.not_null and c.default is None and c.name not in model.model_defaults and c.name not in pk
    ]
    if required:
        schema["required"] = required
    return schema
