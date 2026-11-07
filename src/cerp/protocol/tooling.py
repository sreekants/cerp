"""One way to register a tool: exception boundary, kill switch and scope check (MPC001-006, -116, -077).

Every tool is registered through :func:`register_tool`, which wraps the function so that a switched-off tool
returns ``isError`` with code ``disabled`` and every other failure goes through :func:`guarded`.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from cerp.protocol.boundary import guarded
from cerp.protocol.errors import Disabled
from cerp.protocol.killswitch import NO_SWITCHES, KillSwitch
from cerp.protocol.naming import check_name
from cerp.protocol.scopes import NO_SCOPES, ScopePolicy


def register_tool(
    mcp: MCPServer,
    fn: Callable[..., Any],
    *,
    name: str,
    title: str,
    annotations: ToolAnnotations,
    killswitch: KillSwitch = NO_SWITCHES,
    scopes: ScopePolicy = NO_SCOPES,
) -> str:
    check_name(name)
    read_only = bool(annotations.read_only_hint)

    @functools.wraps(fn)
    def switched(*args: Any, **kwargs: Any) -> Any:
        switch = killswitch.blocked(name, read_only=read_only)
        if switch is not None:
            raise Disabled(f"{name} is switched off by the operator ({switch})")
        scopes.check(name, read_only=read_only)
        return fn(*args, **kwargs)

    mcp.tool(name=name, title=title, annotations=annotations)(guarded(switched))
    return name
