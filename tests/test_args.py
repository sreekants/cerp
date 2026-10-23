"""MPC001-065 bounded arguments and path confinement; MPC001-008 field names come only from exposure."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from pydantic import TypeAdapter, ValidationError

from cerp.app.server import build_runtime
from cerp.data.exposure import UnknownField, load_exposure
from cerp.data.paths import PathRejected, confine
from cerp.data.registry import load_registry
from cerp.protocol.args import FREE_TEXT_MAX, SHORT_MAX, FreeText, HandleText, ShortText
from tests.conftest import TICKET_EXPOSURE
from tests.test_exposure import _settings


def _strings_without_bound(schema: Any, path: str = "$") -> Iterator[str]:
    if isinstance(schema, dict):
        if schema.get("type") == "string" and "maxLength" not in schema and "enum" not in schema:
            yield path
        for k, v in schema.items():
            yield from _strings_without_bound(v, f"{path}.{k}")
    elif isinstance(schema, list):
        for i, v in enumerate(schema):
            yield from _strings_without_bound(v, f"{path}[{i}]")


def test_every_registered_tool_bounds_its_strings(
    registry_file: Path, write_yaml: Callable[[str, Any], Path], dbroot: Path
) -> None:
    mcp, _ = build_runtime(_settings(registry_file, write_yaml("e.yaml", TICKET_EXPOSURE), dbroot), {})

    async def go() -> dict[str, list[str]]:
        async with Client(mcp) as c:
            return {
                t.name: list(_strings_without_bound(t.input_schema)) for t in (await c.list_tools()).tools
            }

    found = anyio.run(go)
    assert found  # at least one tool was checked
    assert {k: v for k, v in found.items() if v} == {}


def test_the_check_finds_an_unbounded_string() -> None:
    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}, "ok": {"type": "string", "maxLength": 9}},
    }
    assert list(_strings_without_bound(schema)) == ["$.properties.q"]


@pytest.mark.parametrize(("kind", "limit"), [(ShortText, SHORT_MAX), (FreeText, FREE_TEXT_MAX)])
def test_text_types_have_limits(kind: Any, limit: int) -> None:
    ta: TypeAdapter[str] = TypeAdapter(kind)
    assert ta.validate_python("x" * limit)
    with pytest.raises(ValidationError):
        ta.validate_python("x" * (limit + 1))
    assert ta.json_schema()["maxLength"] == limit


@pytest.mark.parametrize("good", ["im:ticket:1", "crm.x:account_2:42"])
def test_handle_text_accepts(good: str) -> None:
    TypeAdapter(HandleText).validate_python(good)


@pytest.mark.parametrize(
    "bad", ["im:ticket:0", "im:ticket:-1", "IM:ticket:1", "im:ticket", "im:ticket:1;drop", "im:ticket:1\n"]
)
def test_handle_text_refuses(bad: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(HandleText).validate_python(bad)


# ---------------------------------------------------------------- paths

INJECTION = [
    "",
    "../secret.txt",
    "a/../../b",
    "/etc/passwd",
    "\\\\server\\share\\x",
    "C:\\Windows\\win.ini",
    "c:/x",
    "a/./b",
    "a//b",
    "a/",
    "file.txt:stream",
    "CON",
    "nul.txt",
    "com1",
    "LPT9.log",
    "dir /x",
    "trailing.",
    "trailing ",
    "a\x00b",
    "a\nb",
    "a|b",
    "x" * 1025,
    "..\\..\\x",
    "\u2025/x",  # normalises to ".." under NFKC, not NFC: must still be safe either way
]


@pytest.mark.parametrize("bad", INJECTION)
def test_confine_refuses_escapes(tmp_path: Path, bad: str) -> None:
    try:
        target = confine(tmp_path, bad)
    except PathRejected:
        return
    assert tmp_path.resolve() in target.parents  # anything accepted stays inside


@pytest.mark.parametrize("bad", [b for b in INJECTION if b not in ("dir /x", "\u2025/x")])
def test_confine_rejects(tmp_path: Path, bad: str) -> None:
    with pytest.raises(PathRejected):
        confine(tmp_path, bad)


def test_confine_accepts_nested(tmp_path: Path) -> None:
    assert confine(tmp_path, "docs/2026/plan.md") == tmp_path.resolve() / "docs" / "2026" / "plan.md"
    assert confine(tmp_path, "docs\\a.txt") == tmp_path.resolve() / "docs" / "a.txt"


def test_confine_refuses_link_out(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not permitted here")
    with pytest.raises(PathRejected):
        confine(root, "link/x.txt")


# ---------------------------------------------------------------- field names (MPC001-008)


def test_field_names_must_be_exposed(registry_file: Path, write_yaml: Callable[[str, Any], Path]) -> None:
    exp = load_exposure(write_yaml("e.yaml", TICKET_EXPOSURE), load_registry(registry_file))
    assert exp.check_fields("im", "ticket", ["status", "priority"]) == ["status", "priority"]
    for bad in (["status; DROP TABLE tickets"], ["map_url"], ["status", '"id"']):
        with pytest.raises(UnknownField) as exc:
            exp.check_fields("im", "ticket", bad)
        assert "status" in exc.value.allowed
    with pytest.raises(UnknownField) as exc:
        exp.check_fields("im", "no_such", ["id"])
    assert exc.value.allowed == []
