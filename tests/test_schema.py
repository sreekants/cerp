"""MPC001-020 bounded JSON Schema validation."""

from __future__ import annotations

import time

import pytest

from cerp.protocol.schema import BoundedValidator, SchemaRejected


def test_formats_are_checked() -> None:
    v = BoundedValidator(
        {
            "type": "object",
            "properties": {
                "d": {"type": "string", "format": "date"},
                "t": {"type": "string", "format": "date-time"},
                "e": {"type": "string", "format": "email"},
                "u": {"type": "string", "format": "uuid"},
            },
        }
    )
    good = {
        "d": "2026-10-22",
        "t": "2026-10-22T10:00:00Z",
        "e": "a@b.co",
        "u": "123e4567-e89b-12d3-a456-426614174000",
    }
    assert v.problems(good) == []
    bad = {"d": "22/10/2026", "t": "yesterday", "e": "nobody", "u": "nope"}
    assert {p.path for p in v.problems(bad)} == {"$.d", "$.t", "$.e", "$.u"}


def test_external_ref_is_refused() -> None:
    with pytest.raises(SchemaRejected, match="external"):
        BoundedValidator({"$ref": "https://example.com/schema.json"})


def test_local_ref_is_allowed() -> None:
    v = BoundedValidator({"$defs": {"n": {"type": "integer"}}, "$ref": "#/$defs/n"})
    assert v.problems(3) == []
    assert v.problems("x")


def test_invalid_schema_is_refused() -> None:
    with pytest.raises(SchemaRejected):
        BoundedValidator({"type": 12})


def test_catastrophic_backtracking_is_bounded() -> None:
    v = BoundedValidator({"type": "string", "pattern": "^(a+)+$"}, budget_ms=50)
    started = time.perf_counter()
    problems = v.problems("a" * 40 + "!")
    took = time.perf_counter() - started
    assert problems
    assert took < 1.0


def test_pattern_properties_use_bounded_engine() -> None:
    v = BoundedValidator({"type": "object", "patternProperties": {"^x_": {"type": "integer"}}})
    assert v.problems({"x_a": 1, "y": "s"}) == []
    assert v.problems({"x_a": "s"})
