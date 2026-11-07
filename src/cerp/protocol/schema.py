"""JSON Schema validation with bounded cost (MPC001-020).

* Draft 2020-12 with a format checker for ``date``, ``date-time``, ``email`` and ``uuid``.
* No remote resolution: any ``$ref`` that leaves the schema document fails when the schema is compiled.
* ``pattern`` and ``patternProperties`` use the ``regex`` module with a per-match timeout, and a whole
  validation is stopped once its time budget (default 50 ms) is spent. Both outcomes are ordinary
  validation errors, never hangs.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import regex
from jsonschema import Draft202012Validator, FormatChecker, validators
from jsonschema import ValidationError as JsonSchemaError
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource


class SchemaRejected(Exception):
    """The schema itself is unacceptable (invalid, or refers to an external document)."""


@dataclass(frozen=True)
class Problem:
    path: str
    message: str


_format_checker = FormatChecker(formats=())
_schema_checker = FormatChecker(formats=())  # used on schemas themselves: ``pattern`` is a ``regex``

# RFC 3339 (section 5.6), ASCII only. Python's fromisoformat accepts ISO 8601 forms RFC 3339 does not.
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})\Z", re.ASCII)
_DATETIME = re.compile(
    r"(\d{4}-\d{2}-\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:([Zz])|([+-])(\d{2}):(\d{2}))\Z", re.ASCII
)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.ASCII | re.I)

# RFC 5321 Mailbox: Dot-string or Quoted-string "@" Domain or address literal. ASCII only (no RFC 6531).
_ATEXT = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LOCAL = rf'(?:{_ATEXT}(?:\.{_ATEXT})*|"(?:[\x20\x21\x23-\x5b\x5d-\x7e]|\\[\x20-\x7e])*")'
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_EMAIL = re.compile(rf"{_LOCAL}@(?:{_LABEL}(?:\.{_LABEL})*|\[([^\[\]]*)\])\Z", re.ASCII)
_IPV4 = re.compile(
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}\Z", re.ASCII
)
_LEAP_MINUTE = 23 * 60 + 59
_MINUTES_PER_DAY = 24 * 60
_MAX_HOUR, _MAX_MINUTE, _LEAP_SECOND = 23, 59, 60


def _valid_date(text: str) -> bool:
    m = _DATE.match(text)
    if not m:
        return False
    try:
        dt.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return False
    return True


@_format_checker.checks("date")
def _is_date(value: object) -> bool:
    return not isinstance(value, str) or _valid_date(value)


@_format_checker.checks("date-time")
def _is_datetime(value: object) -> bool:
    if not isinstance(value, str):
        return True
    m = _DATETIME.match(value)
    if not m or not _valid_date(m[1]):
        return False
    hour, minute, second = int(m[2]), int(m[3]), int(m[4])
    off = 0
    if not m[5]:
        oh, om = int(m[7]), int(m[8])
        if oh > _MAX_HOUR or om > _MAX_MINUTE:
            return False
        off = (oh * 60 + om) * (1 if m[6] == "+" else -1)
    if hour > _MAX_HOUR or minute > _MAX_MINUTE or second > _LEAP_SECOND:
        return False
    # A leap second is only valid at 23:59:60 UTC.
    return second < _LEAP_SECOND or (hour * 60 + minute - off) % _MINUTES_PER_DAY == _LEAP_MINUTE


def _valid_ipv6(text: str) -> bool:
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return False
    return True


@_format_checker.checks("email")
def _is_email(value: object) -> bool:
    if not isinstance(value, str):
        return True
    m = _EMAIL.match(value)
    if not m:
        return False
    literal = m[1]
    if literal is None:
        return True
    if literal[:5].lower() == "ipv6:":
        return _valid_ipv6(literal[5:])
    return bool(_IPV4.match(literal))


@_format_checker.checks("uuid")
def _is_uuid(value: object) -> bool:
    return not isinstance(value, str) or bool(_UUID.match(value))


@_schema_checker.checks("regex", raises=regex.error)
def _is_regex(value: object) -> bool:
    return not isinstance(value, str) or bool(regex.compile(value))


def _no_remote(uri: str) -> Resource[Any]:
    raise NoSuchResource(ref=uri)  # type: ignore[call-arg]  # attrs-generated init, stubs lag


_NO_REMOTE: Registry[Any] = Registry(retrieve=_no_remote)  # type: ignore[call-arg]  # attrs-generated init, stubs lag


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

    cls: type[Draft202012Validator] = validators.extend(  # type: ignore[no-untyped-call]  # jsonschema stubs leave extend untyped
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
            Draft202012Validator.check_schema(schema, format_checker=_schema_checker)
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
                    raise _Expired  # noqa: TRY301 - one handler for both budget exits
        except _Expired:
            return [Problem("$", "validation took too long; simplify the value")]
        return out
