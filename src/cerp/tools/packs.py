"""Curated tool packs and their startup checks (MPC001-088; write packs: decision 0003).

A pack is enabled in configuration (``tools.packs``). Before any tool is registered, every pack's
declared tables and columns are checked against the live databases and every guard against the registry;
any problem stops startup with all problems listed, so a call can never fail on a missing column or an
unguarded model.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from typing import Final

from mcp.server.mcpserver import MCPServer

from cerp.auth.principals import PrincipalMapper
from cerp.data.registry import Registry
from cerp.data.security import SECURITY, SecurityDirectory
from cerp.data.sqlite import Databases
from cerp.protocol.cursors import CursorSigner
from cerp.protocol.killswitch import KillSwitch
from cerp.protocol.scopes import ScopePolicy
from cerp.tools import crm, personal, personal_write, pm, pm_write
from cerp.tools.context import Guard, Reads, Registrar, ToolContext, check_reads

PackRegister = Callable[[Registrar, ToolContext], None]
PACKS: Final[dict[str, tuple[PackRegister, Reads, tuple[Guard, ...]]]] = {
    "pm": (pm.register, pm.READS, pm.GUARDS),
    "crm": (crm.register, crm.READS, crm.GUARDS),
    "personal": (
        personal.register,
        personal.READS,
        (pm.TASK, pm.MILESTONE, crm.TICKET, crm.REMINDER, crm.ACTIVITY),
    ),
    # Write packs (decision 0003): off unless named, so a read-only deployment stays read-only.
    "pm_write": (pm_write.register, pm_write.READS, pm_write.GUARDS),
    "personal_write": (personal_write.register, personal_write.READS, ()),
}


class PackError(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("tool packs cannot start:\n  " + "\n  ".join(errors))


def build_context(
    databases: Databases,
    registry: Registry,
    oauth_map: dict[tuple[str, str], str],
    *,
    local_user: str | None,
    cursor_ttl: float,
    clock: Callable[[], dt.datetime] | None = None,
) -> ToolContext:
    directory = SecurityDirectory(databases)
    return ToolContext(
        databases=databases,
        registry=registry,
        mapper=PrincipalMapper(directory, oauth_map, local_user=local_user),
        directory=directory,
        cursors=CursorSigner(ttl=cursor_ttl),
        clock=clock or dt.datetime.now,
    )


def check_packs(ctx: ToolContext, names: Iterable[str]) -> list[str]:
    errors: list[str] = []
    if SECURITY not in ctx.databases.names or not ctx.databases.can_open(SECURITY):
        errors.append("datasource security: the security database is needed for authorization (MPC001-061)")
    for name in names:
        _, reads, guards = PACKS[name]
        errors += [f"{name}: {e}" for e in check_reads(ctx.databases, reads)]
        errors += [f"{name}: {e}" for e in ctx.guard_errors(guards)]
    return sorted(set(errors))


def register_packs(
    mcp: MCPServer, ctx: ToolContext, names: Iterable[str], killswitch: KillSwitch, scopes: ScopePolicy
) -> list[str]:
    wanted = list(dict.fromkeys(names))
    errors = check_packs(ctx, wanted)
    if errors:
        raise PackError(errors)
    reg = Registrar(mcp, killswitch, scopes)
    for name in wanted:
        PACKS[name][0](reg, ctx)
    return reg.names
