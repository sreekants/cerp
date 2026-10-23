"""JSON Schema validation with bounded cost (MPC001-020).

* Draft 2020-12 with a format checker for ``date``, ``date-time``, ``email`` and ``uuid``.
* No remote resolution: any ``$ref`` that leaves the schema document fails when the schema is compiled.
* ``pattern`` and ``patternProperties`` use the ``regex`` module with a per-match timeout, and a whole
  validation is stopped once its time budget (default 50 ms) is spent. Both outcomes are ordinary
  validation errors, never hangs.
"""

from __future__ import annotations

import datetime as dt
import re
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import regex
from jsonschema import Draft202012Validator, FormatChecker, validators
from jsonschema import ValidationError as JsonSchemaError
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SchemaRejected(Exception):
    """The schema itself is unacceptable (invalid, or refers to an external document)."""


@dataclass(frozen=True)
class Problem:
    path: str
    message: str


_format_checker = FormatChecker(formats=())


@_format_checker.checks("date", raises=ValueError)
def _is_date(value: object) -> bool:
    return not isinstance(value, str) or bool(dt.date.fromisoformat(value))


@_format_checker.checks("date-time", raises=ValueError)
def _is_datetime(value: object) -> bool:
    if not isinstance(value, str):
        return True
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.tzinfo is not None or "T" in value


@_format_checker.checks("email")
def _is_email(value: object) -> bool:
    return not isinstance(value, str) or bool(_EMAIL.match(value))


@_format_checker.checks("uuid", raises=ValueError)
def _is_uuid(value: object) -> bool:
    return not isinstance(value, str) or bool(uuid.UUID(value))


def _no_remote(uri: str) -> Resource[Any]:
    raise NoSuchResource(ref=uri)  # type: ignore[call-arg]  # attrs-generated init, stubs lag


_NO_REMOTE: Registry[Any] = Registry(retrieve=_no_remote)  # type: ignore[call-arg]


class _Budget:
    def __init__(self, seconds: float) -> None:
        self.deadline = time.perf_counter() + seconds

    def remaining(self) -> float:
        return self.deadline - time.perf_counter()


class _Expired(Exception):
    pass


def _bounded_search(pattern: str, value: str, budget: _Budget) -> bool:
    left = budget.remaining()
    if left <= 0:
        raise _Expired
    try:
        return regex.search(pattern, value, timeout=left) is not None
    except TimeoutError:
        raise _Expired from None


def _make_validator_class(budget_ref: list[_Budget]) -> type[Draft202012Validator]:
    def pattern(validator: Any, patrn: str, instance: Any, schema: Any) -> Iterator[JsonSchemaError]:
        if isinstance(instance, str) and not _bounded_search(patrn, instance, budget_ref[0]):
            yield JsonSchemaError(f"{instance!r} does not match {patrn!r}")

    def pattern_properties(
        validator: Any, patterns: Mapping[str, Any], instance: Any, schema: Any
    ) -> Iterator[JsonSchemaError]:
        if not isinstance(instance, dict):
            return
        for patrn, subschema in patterns.items():
            for k, v in instance.items():
                if _bounded_search(patrn, k, budget_ref[0]):
                    yield from validator.descend(v, subschema, path=k, schema_path=patrn)

    cls: type[Draft202012Validator] = validators.extend(  # type: ignore[no-untyped-call]
        Draft202012Validator, {"pattern": pattern, "patternProperties": pattern_properties}
    )
    return cls


def _has_external_ref(node: Any) -> str | None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("$ref", "$dynamicRef") and isinstance(v, str) and not v.startswith("#"):
                return v
            found = _has_external_ref(v)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _has_external_ref(v)
            if found:
                return found
    return None


class BoundedValidator:
    """A compiled schema. Thread-safe for sequential use; create one per schema."""

    def __init__(self, schema: Mapping[str, Any], *, budget_ms: int = 50) -> None:
        external = _has_external_ref(schema)
        if external is not None:
            raise SchemaRejected(f"external $ref not allowed: {external}")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise SchemaRejected(f"invalid schema: {exc.message}") from None
        self._budget_s = budget_ms / 1000
        self._budget_ref: list[_Budget] = [_Budget(self._budget_s)]
        cls = _make_validator_class(self._budget_ref)
        self._validator = cls(dict(schema), format_checker=_format_checker, registry=_NO_REMOTE)

    def problems(self, instance: Any) -> list[Problem]:
        """All validation problems, or a single budget problem if validation ran out of time."""
        self._budget_ref[0] = _Budget(self._budget_s)
        out: list[Problem] = []
        try:
            for err in self._validator.iter_errors(instance):
                out.append(Problem(err.json_path, err.message))
                if self._budget_ref[0].remaining() <= 0:
                    raise _Expired  # noqa: TRY301
        except _Expired:
            return [Problem("$", "validation took too long; simplify the value")]
        return out
