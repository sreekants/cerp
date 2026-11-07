"""Per-tool OAuth scope checks (MPC001-077).

The scope catalogue is :func:`cerp.data.scopes.catalogue`, generated from the registry. A scope never adds
privileges: a call needs the scope **and** the webos rights (MPC001-062).

When OAuth is on, every tool call checks the token's scopes: a read-only tool needs the module's read
scope, any other tool its write scope. Static development tokens carry no scopes and are not checked.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcp.server.auth.middleware.auth_context import get_access_token

from cerp.protocol.errors import NotPermitted


def scope_for(tool: str, *, read_only: bool) -> str:
    module = tool.split(".", 1)[0]
    return f"webos.{module}.{'read' if read_only else 'write'}"


@dataclass(frozen=True)
class ScopePolicy:
    enforce: bool = False

    def check(self, tool: str, *, read_only: bool) -> None:
        if not self.enforce:
            return
        token = get_access_token()
        if token is None:
            return  # no authentication configured for this request path (stdio)
        need = scope_for(tool, read_only=read_only)
        if need not in token.scopes:
            raise NotPermitted(f"the access token lacks the scope {need}", allowed=[need])


NO_SCOPES = ScopePolicy()
