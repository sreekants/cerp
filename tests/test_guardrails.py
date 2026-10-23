"""MPC001-169 / MPC001-207 guardrail lint for every tool module (im, pm, dms, personal, ...).

* no DELETE and no string-built SQL in tool code (MPC001-169);
* no tool parameter that takes raw SQL or a free-form filter, and no owner parameter: the owner always
  comes from the mapped user (MPC001-207).

Scans every module under ``src/cerp/tools`` (where ``im.*`` and other tools live). Any string literal
that contains a SQL ``DELETE`` statement, and any f-string, ``%``-format, ``.format()`` or ``+``
concatenation whose literal parts look like SQL, fails the build. SQL in tools is written as constant
strings with ``?`` placeholders; identifiers come from the registry through ``cerp.data`` helpers.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "src" / "cerp" / "tools"
_SQL = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|REPLACE|WHERE|VALUES)\b", re.I)
_DELETE = re.compile(r"\bDELETE\s+FROM\b", re.I)


def _literals(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


_FORBIDDEN_PARAMS = frozenset(
    {"sql", "raw_sql", "where", "where_clause", "query_sql", "order_by_sql", "owner", "owner_id"}
)


def violations(source: str, filename: str = "<src>") -> list[str]:
    tree = ast.parse(source, filename)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            found += [
                f"{filename}:{node.lineno}: parameter {a.arg!r} is not allowed in a tool"
                for a in args
                if a.arg.lower() in _FORBIDDEN_PARAMS
            ]
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _DELETE.search(node.value):
            found.append(f"{filename}:{node.lineno}: DELETE statement")
        built = (
            isinstance(node, ast.JoinedStr)
            or (isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)))
            or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "format"
            )
        )
        if built and any(_SQL.search(s) for s in _literals(node)):
            found.append(f"{filename}:{getattr(node, 'lineno', 0)}: SQL built from strings")
    return sorted(set(found))


def test_tool_code_has_no_delete_or_built_sql() -> None:
    problems = [
        v for f in sorted(TOOLS.rglob("*.py")) for v in violations(f.read_text(encoding="utf-8"), str(f))
    ]
    assert problems == []


@pytest.mark.parametrize(
    "bad",
    [
        'con.execute("DELETE FROM tickets WHERE id = ?", (1,))',
        'con.execute(f"SELECT * FROM tickets WHERE id = {i}")',
        'con.execute("SELECT * FROM t WHERE a = " + x)',
        'con.execute("UPDATE t SET a = %s" % x)',
        'con.execute("SELECT {} FROM t".format(col))',
        "def search_tasks(project: int, where: str): ...",
        "def create_task(title: str, *, owner_id: int): ...",
        "async def find_files(raw_sql: str): ...",
    ],
)
def test_lint_catches_bad_code(bad: str) -> None:
    assert violations(bad)


def test_lint_allows_bound_parameters() -> None:
    assert violations('con.execute("SELECT id FROM tickets WHERE status = ?", (1000,))') == []
    assert violations('msg = f"ticket {n} not found"') == []
    assert violations("def _row(owner_id: int): ...") == []  # private helpers may carry the mapped owner
    assert violations("def search_tasks(project: int, status: int, cursor: str | None = None): ...") == []
