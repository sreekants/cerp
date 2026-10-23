"""Static bearer tokens for development on loopback only (MPC001-059).

Each token maps to one webos user. Token values come from environment variables named in configuration,
so no secret is ever written to a config file, log or fixture (MPC001-056). The loopback-only rule is
enforced twice: by configuration validation and by :func:`assert_loopback` at server construction.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from mcp.server.auth.provider import AccessToken

log = logging.getLogger("cerp.auth")

STATIC_CLIENT_ID = "cerp-static-dev"
MIN_TOKEN_LENGTH = 24


class StaticTokenError(Exception):
    pass


@dataclass(frozen=True)
class StaticTokenSpec:
    token_env: str
    user: str


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def assert_loopback(host: str) -> None:
    if not is_loopback(host):
        raise StaticTokenError(f"static tokens are only allowed on a loopback bind, not {host!r}")


class StaticTokenVerifier:
    """Implements the SDK's ``TokenVerifier`` protocol over a fixed token → user table."""

    def __init__(self, specs: list[StaticTokenSpec], environ: Mapping[str, str]) -> None:
        if not specs:
            raise StaticTokenError("static tokens are enabled but none are configured")
        table: list[tuple[bytes, str]] = []
        for spec in specs:
            value = environ.get(spec.token_env, "")
            if len(value) < MIN_TOKEN_LENGTH:
                raise StaticTokenError(
                    f"environment variable {spec.token_env} must hold a token of at least "
                    f"{MIN_TOKEN_LENGTH} characters"
                )
            table.append((value.encode(), spec.user))
        self._table = table
        log.warning(
            "static development tokens are ENABLED for %d user(s); never use this outside development",
            len(table),
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        presented = token.encode()
        user: str | None = None
        for value, mapped in self._table:
            # compare against every entry so timing does not reveal which one matched
            if hmac.compare_digest(presented, value):
                user = mapped
        if user is None:
            return None
        return AccessToken(token=token, client_id=STATIC_CLIENT_ID, scopes=[], subject=user)
