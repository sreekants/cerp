"""Shared runtime for the curated read-only tools (MPC001-088).

A :class:`ToolContext` gives a tool everything it may use, and nothing else:

* :meth:`caller` maps the request's principal to an enabled webos user (MPC001-060);
* :meth:`allowed` / :meth:`require` check a :class:`Guard` through the one authorization function
  (MPC001-061); a guard names a registry model, whose privilege comes from the registry, or (for tables
  with no model of their own) an explicit privilege name;
* :meth:`rows` runs a **constant** SQL statement with bound values on a read-only connection
  (MPC001-162, MPC001-065) and cleans webos's literal ``'NULL'`` strings (MPC001-163);
* :meth:`offset` / :meth:`next_cursor` implement signed cursor paging (MPC001-023).

Each tool pack declares the tables and columns it reads (:data:`Reads`); :func:`check_reads` verifies them
against the live databases at startup, so a renamed column stops the server instead of failing a call.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Final

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import StringConstraints

from cerp.auth.authorize import AccessDenied, Caller, authorize, privilege_model
from cerp.auth.principals import PrincipalMapper
from cerp.data.handles import Handle, HandleError
from cerp.data.records import clean_row
from cerp.data.registry import Registry
from cerp.data.security import SecurityDirectory, SecurityUnavailable
from cerp.data.sqlite import Databases
from cerp.protocol.audit import note_webos_user
from cerp.protocol.cursors import CursorSigner, query_digest
from cerp.protocol.errors import NotFound, NotPermitted, ValidationError
from cerp.protocol.killswitch import KillSwitch
from cerp.protocol.scopes import ScopePolicy
from cerp.protocol.tooling import register_tool

DEFAULT_PAGE: Final = 25
MAX_PAGE: Final = 100
READ_ONLY: Final = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

DateText = Annotated[str, StringConstraints(max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$")]
"""A calendar date, ``YYYY-MM-DD``."""
CursorText = Annotated[str, StringConstraints(max_length=512)]
"""An opaque cursor returned by a previous call (MPC001-023)."""

Reads = Mapping[tuple[str, str], tuple[str, ...]]
"""(datasource, table) → columns a tool pack reads."""


@dataclass(frozen=True)
class Guard:
    """What protects a set of records: a registry model, or a privilege name for unmodelled tables."""

    label: str
    model_key: str | None = None
    privilege: str | None = None


def stamp(value: dt.datetime) -> str:
    """webos timestamp text: ``YYYY-MM-DD HH:MM:SS``, local time (pm.md §4, crm.md §4)."""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def like(text: str) -> str:
    """A LIKE pattern matching ``text`` literally as a substring (use with ``ESCAPE '\\'``)."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def handle(module: str, model: str, record_id: Any) -> str | None:
    """A record handle (MPC001-087), or None for an unset reference (webos uses 0 or NULL)."""
    try:
        n = int(record_id)
    except (TypeError, ValueError):
        return None
    return f"{module}:{model}:{n}" if n > 0 else None


def record_id(text: str, module: str, model: str, field: str) -> int:
    """The id in a handle argument, which must name a ``module:model`` record."""
    try:
        return Handle.parse(text).expect(module, model).id
    except HandleError as exc:
        raise ValidationError(str(exc), field=field) from None


@dataclass(frozen=True)
class Registrar:
    """Registers read-only tools with the kill switch and scope policy in force (MPC001-116, -077)."""

    mcp: MCPServer
    killswitch: KillSwitch
    scopes: ScopePolicy
    names: list[str] = field(default_factory=list)

    def __call__(self, fn: Callable[..., Any], name: str, title: str) -> None:
        register_tool(
            self.mcp,
            fn,
            name=name,
            title=title,
            annotations=READ_ONLY,
            killswitch=self.killswitch,
            scopes=self.scopes,
        )
        self.names.append(name)


def parse_day(text: str | None) -> dt.date | None:
    """The date part of a webos timestamp or date text, or None if absent or unreadable."""
    if not text:
        return None
    try:
        return dt.date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def check_reads(databases: Databases, reads: Reads) -> list[str]:
    """Problems with the tables and columns a pack reads, against the live databases."""
    errors: list[str] = []
    for (ds, table), columns in sorted(reads.items()):
        try:
            live = databases.table_columns(ds, table)
        except (KeyError, FileNotFoundError, sqlite3.Error) as exc:
            errors.append(f"{ds}.{table}: database unavailable ({type(exc).__name__})")
            continue
        if live is None:
            errors.append(f"{ds}.{table}: table does not exist")
            continue
        missing = [c for c in columns if c not in live]
        if missing:
            errors.append(f"{ds}.{table}: missing columns {', '.join(missing)}")
    return errors


@dataclass
class ToolContext:
    databases: Databases
    registry: Registry
    mapper: PrincipalMapper
    directory: SecurityDirectory
    cursors: CursorSigner
    clock: Callable[[], dt.datetime] = dt.datetime.now
    _privileges: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------ callers and rights

    def caller(self) -> Caller:
        try:
            caller = self.mapper.resolve(get_access_token())
        except AccessDenied as exc:
            raise NotPermitted(str(exc)) from None
        note_webos_user(caller.user_name)  # the audit record names the mapped user (MPC001-110)
        return caller

    def privilege(self, guard: Guard) -> str:
        if guard.privilege is not None:
            return guard.privilege
        assert guard.model_key is not None  # noqa: S101 - a guard names one or the other
        name = self._privileges.get(guard.model_key)
        if name is None:
            name = self._privileges[guard.model_key] = privilege_model(self.registry, guard.model_key)
        return name

    def guard_errors(self, guards: Iterable[Guard]) -> list[str]:
        """Guards that cannot be resolved against the registry (reported at startup)."""
        errors = []
        for g in guards:
            try:
                self.privilege(g)
            except AccessDenied as exc:
                errors.append(f"guard {g.label}: {exc}")
        return errors

    def allowed(self, caller: Caller, guard: Guard, verb: str = "View") -> bool:
        try:
            return authorize(caller, self.privilege(guard), verb).allowed
        except AccessDenied:
            return False

    def require(self, caller: Caller, guard: Guard, verb: str = "View") -> None:
        if not self.allowed(caller, guard, verb):
            raise NotPermitted(f"you do not have the right to read {guard.label}")

    def require_record(self, caller: Caller, guard: Guard, what: str) -> None:
        """For a single record, a missing right looks exactly like a missing record (MPC001-087)."""
        if not self.allowed(caller, guard):
            raise NotFound(f"{what} not found")

    # ------------------------------------------------------------ data

    def rows(
        self, datasource: str, statement: str, params: Mapping[str, Any] | tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        with self.databases.reader(datasource) as con:
            return [clean_row(dict(r)) for r in con.execute(statement, params).fetchall()]

    def one(
        self, datasource: str, statement: str, params: Mapping[str, Any] | tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        found = self.rows(datasource, statement, params)
        return found[0] if found else None

    def user_names(self, ids: Iterable[Any]) -> dict[int, str]:
        """webos user id → display name, for ids that exist; unknown ids are left out."""
        out: dict[int, str] = {}
        for raw in set(ids):
            try:
                uid = int(raw)
            except (TypeError, ValueError):
                continue
            if uid <= 0:
                continue
            try:
                user = self.directory.user_by_id(uid)
            except SecurityUnavailable:
                return out
            if user is not None:
                out[uid] = user.display_name
        return out

    # ------------------------------------------------------------ paging and time

    def now(self) -> dt.datetime:
        return self.clock()

    @staticmethod
    def page_size(limit: int | None) -> int:
        return max(1, min(limit or DEFAULT_PAGE, MAX_PAGE))

    def offset(self, tool: str, args: Mapping[str, Any], caller: Caller, cursor: str | None) -> int:
        if not cursor:
            return 0
        return self.cursors.decode(cursor, principal=caller.principal, query=query_digest(tool, args))

    def next_cursor(
        self, tool: str, args: Mapping[str, Any], caller: Caller, *, offset: int, limit: int, fetched: int
    ) -> str | None:
        """A cursor for the next page if more rows exist (the query fetched ``limit + 1``)."""
        if fetched <= limit:
            return None
        return self.cursors.encode(
            principal=caller.principal, query=query_digest(tool, args), offset=offset + limit
        )
