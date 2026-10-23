"""MPC001-010 strict, bounded JSON at the MCP endpoint.

Uses nst/JSONTestSuite (MIT, vendored under ``tests/fixtures/jsontestsuite``): every ``n_`` case must be
refused and every ``y_`` case accepted, except the two ``y_`` cases with duplicate keys, which cerp refuses
on purpose.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cerp.app.jsonlimits import MAX_DEPTH, JsonRejected, check_body
from cerp.protocol.errors import INVALID_REQUEST, PARSE_ERROR
from tests.test_protocol import ACCEPT, http, post

SUITE = Path(__file__).resolve().parent / "fixtures" / "jsontestsuite"
DUPLICATE_KEYS = {"y_object_duplicated_key.json", "y_object_duplicated_key_and_value.json"}


def _cases(prefix: str) -> list[Path]:
    return sorted(SUITE.glob(f"{prefix}_*.json"))


def test_suite_is_vendored() -> None:
    assert len(_cases("n")) > 150
    assert len(_cases("y")) > 90


@pytest.mark.parametrize("case", _cases("n"), ids=lambda p: p.name)
def test_invalid_json_is_refused(case: Path) -> None:
    with pytest.raises(JsonRejected):
        check_body(case.read_bytes())


@pytest.mark.parametrize("case", _cases("y"), ids=lambda p: p.name)
def test_valid_json_is_accepted(case: Path) -> None:
    if case.name in DUPLICATE_KEYS:
        with pytest.raises(JsonRejected) as exc:
            check_body(case.read_bytes())
        assert exc.value.code == PARSE_ERROR
    else:
        check_body(case.read_bytes())


@pytest.mark.parametrize("text", ["NaN", "[Infinity]", '{"a": -Infinity}'])
def test_non_json_constants_are_refused(text: str) -> None:
    with pytest.raises(JsonRejected) as exc:
        check_body(text.encode())
    assert exc.value.code == PARSE_ERROR


def test_depth_limit_is_exact() -> None:
    check_body(b"[" * MAX_DEPTH + b"]" * MAX_DEPTH)
    with pytest.raises(JsonRejected) as exc:
        check_body(b"[" * (MAX_DEPTH + 1) + b"]" * (MAX_DEPTH + 1))
    assert exc.value.code == INVALID_REQUEST


def test_brackets_inside_strings_do_not_count() -> None:
    check_body(json.dumps({"s": "[" * 1000 + '\\"{' * 100}).encode())


def test_very_deep_nesting_is_refused_quickly() -> None:
    body = b"[" * 10_000 + b"]" * 10_000
    started = time.perf_counter()
    with pytest.raises(JsonRejected):
        check_body(body)
    assert time.perf_counter() - started < 0.05


def test_long_strings_are_refused() -> None:
    check_body(json.dumps({"k": "x" * 100}).encode(), max_string=100)
    for value in ({"k": "x" * 101}, {"x" * 101: 1}, [["x" * 101]]):
        with pytest.raises(JsonRejected) as exc:
            check_body(json.dumps(value).encode(), max_string=100)
        assert exc.value.code == INVALID_REQUEST


def test_huge_integer_is_refused_not_crashed() -> None:
    with pytest.raises(JsonRejected):
        check_body(b"1" * 5000)  # beyond Python's int-from-string limit


@settings(max_examples=300, deadline=None)
@given(st.binary(max_size=512))
def test_random_bytes_never_escape_as_other_errors(raw: bytes) -> None:
    with contextlib.suppress(JsonRejected):
        check_body(raw)


@settings(max_examples=200, deadline=None)
@given(
    st.recursive(
        st.none() | st.booleans() | st.integers() | st.text(),
        lambda c: st.lists(c) | st.dictionaries(st.text(), c),
        max_leaves=30,
    )
)
def test_json_written_by_python_round_trips(value: object) -> None:
    assert check_body(json.dumps(value).encode()) == value


# ---------------------------------------------------------------- over HTTP


def _error(r: object) -> dict[str, object]:
    body: dict[str, object] = r.json()["error"]  # type: ignore[attr-defined]  # httpx response
    return body


def test_oversized_body_gets_413() -> None:
    with http() as c:
        r = c.post("/mcp", content=b"[" + b"1," * 600_000 + b"1]", headers=ACCEPT)
    assert r.status_code == 413
    err = _error(r)
    assert err["code"] == INVALID_REQUEST
    assert err["data"]["correlation_id"]  # type: ignore[index]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b'{"jsonrpc": "2.0", "id": 1, "id": 2, "method": "ping"}', PARSE_ERROR),
        (b"\xff\xfe{}", PARSE_ERROR),
        (b'{"jsonrpc": "2.0", "id": 1, "method": "ping"', PARSE_ERROR),
        (b"[" * 100 + b"]" * 100, INVALID_REQUEST),
    ],
)
def test_bad_json_gets_400_without_detail(raw: bytes, code: int) -> None:
    with http() as c:
        r = c.post("/mcp", content=raw, headers=ACCEPT)
    assert r.status_code == 400
    err = _error(r)
    assert err["code"] == code
    assert set(err["data"]) == {"correlation_id"}  # type: ignore[arg-type]


def test_good_body_reaches_the_sdk() -> None:
    with http() as c:
        r = post(c, "server/discover")
    assert r.status_code == 200
