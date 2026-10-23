"""MPC001-020: BoundedValidator against the official JSON-Schema-Test-Suite (draft 2020-12).

The suite (MIT) is vendored under ``tests/fixtures/json-schema-suite`` for the keywords the tool catalogue
uses. Groups whose schema refers to an external document are skipped, because cerp refuses those schemas
on purpose (``SchemaRejected``); ``test_external_refs_are_refused`` checks that refusal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cerp.protocol.schema import BoundedValidator, SchemaRejected

SUITE = Path(__file__).resolve().parent / "fixtures" / "json-schema-suite"

# Format cases where cerp's checker is deliberately stricter or looser than the suite; each entry says why.
KNOWN: dict[str, str] = {}


def _cases() -> list[Any]:
    out = []
    for f in sorted(SUITE.rglob("*.json")):
        rel = f.relative_to(SUITE).as_posix()
        for group in json.loads(f.read_text(encoding="utf-8")):
            for case in group["tests"]:
                cid = f"{rel}::{group['description']}::{case['description']}"
                out.append(pytest.param(group["schema"], case["data"], case["valid"], id=cid))
    return out


CASES = _cases()


def test_suite_is_vendored() -> None:
    assert len(CASES) > 500


@pytest.mark.parametrize(("schema", "data", "valid"), CASES)
def test_suite_case(request: pytest.FixtureRequest, schema: Any, data: Any, valid: bool) -> None:
    try:
        v = BoundedValidator(schema if isinstance(schema, dict) else {"allOf": [schema]})
    except SchemaRejected as exc:
        if "external $ref" not in str(exc):
            raise
        pytest.skip("schema refers to an external document")
    reason = KNOWN.get(request.node.callspec.id)
    if reason:
        pytest.skip(reason)
    assert (v.problems(data) == []) is valid


def test_external_refs_are_refused() -> None:
    for ref in ("http://example.com/s.json", "other.json#/defs/a", "file:///etc/passwd"):
        with pytest.raises(SchemaRejected):
            BoundedValidator({"$ref": ref})
