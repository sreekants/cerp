"""MPC001-081: schemas generated from the registry match golden files and are valid Draft 2020-12."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from cerp.data.registry import Column, load_registry
from cerp.data.schemas import DECIMAL, UnsupportedColumn, column_schema, create_schema, record_schema
from tests.conftest import TICKET_EXPOSURE

GOLDEN = Path(__file__).resolve().parent / "golden"
TICKET = TICKET_EXPOSURE["models"]["objects/support/ticket"]


def _check_golden(name: str, schema: dict[str, object]) -> None:
    text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    path = GOLDEN / name
    if os.environ.get("CERP_UPDATE_GOLDEN"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == text, (
        f"regenerate with CERP_UPDATE_GOLDEN=1 if intended: {name}"
    )


def test_ticket_schemas_match_golden_files(registry_file: Path) -> None:
    model = load_registry(registry_file).models["objects/support/ticket"]
    fields = tuple(TICKET["fields"])
    out = record_schema(model, fields, TICKET["field_descriptions"])
    inp = create_schema(model, fields, TICKET["field_descriptions"])
    for name, schema in (("im.ticket.record.json", out), ("im.ticket.create.json", inp)):
        Draft202012Validator.check_schema(schema)
        _check_golden(name, schema)
    assert "id" not in inp["properties"]
    assert record_schema(model, fields, {}) == record_schema(model, fields, {})  # deterministic


def test_every_table_model_generates_valid_schemas(registry_file: Path) -> None:
    reg = load_registry(registry_file)
    checked = 0
    for model in reg.models.values():
        if not model.is_table or not model.columns:
            continue
        fields = tuple(c.name for c in model.columns if "BLOB" not in c.type.upper())
        Draft202012Validator.check_schema(record_schema(model, fields, {}))
        Draft202012Validator.check_schema(create_schema(model, fields, {}))
        checked += 1
    assert checked > 3


@pytest.mark.parametrize(
    ("ctype", "expected"),
    [
        ("INTEGER", {"type": "integer"}),
        ("REAL", {"type": "string", "pattern": DECIMAL}),
        ("VARCHAR(80)", {"type": "string", "maxLength": 80}),
        ("TEXT", {"type": "string", "maxLength": 65536}),
        ("DATE", {"type": "string", "format": "date"}),
        ("TIMESTAMP", {"type": "string", "format": "date-time"}),
    ],
)
def test_type_table(registry_file: Path, ctype: str, expected: dict[str, object]) -> None:
    model = load_registry(registry_file).models["objects/support/ticket"]
    assert column_schema(Column("c", ctype, True, None, False), model) == expected
    nullable = column_schema(Column("c", ctype, False, None, False), model)
    assert nullable["type"] == [expected["type"], "null"]


def test_blob_is_refused(registry_file: Path) -> None:
    model = load_registry(registry_file).models["objects/support/ticket"]
    with pytest.raises(UnsupportedColumn):
        column_schema(Column("c", "BLOB", True, None, False), model)
