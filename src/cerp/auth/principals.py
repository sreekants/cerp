"""Map an authenticated principal to exactly one webos user (MPC001-060).

* **Static development tokens** (MPC001-059) name their webos user directly (``subject``).
* **OAuth tokens** are mapped by issuer **and** subject through ``auth.principals``, so two issuers with the
  same ``sub`` map to different users; an unmapped principal is refused.
* **OSESSION** (MPC001-286): the session names a domain and a webos user id; the user is read from that
  domain's own security database (``directory_for``), never from the default one.
* **Local stdio** (MPC001-168, interim): with no token, ``auth.local_user`` names the user. Configuration
  refuses it alongside HTTP auth, and the HTTP app refuses to start while it is set.

The user must exist in ``security.users`` and be enabled. A result is cached for at most ``ttl`` seconds
(default 60), so disabling a user takes effect within that time. Rights and administrator membership are
read with the user, through the same cache.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Final

from mcp.server.auth.provider import AccessToken

from cerp.auth.authorize import AccessDenied, Caller
from cerp.auth.osession import token_domain
from cerp.auth.static import STATIC_CLIENT_ID
from cerp.data.domains import DomainUnavailable
from cerp.data.security import SecurityDirectory, SecurityUnavailable, SecurityUser

CACHE_SECONDS: Final = 60.0


class PrincipalMapper:
    def __init__(
        self,
        directory: SecurityDirectory,
        oauth_map: Mapping[tuple[str, str], str] | None = None,
        *,
        local_user: str | None = None,
        ttl: float = CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        directory_for: Callable[[str], SecurityDirectory] | None = None,
    ) -> None:
        self._dir = directory
        self._directory_for = directory_for
        self._oauth = dict(oauth_map or {})
        self._local = local_user
        self._ttl = min(ttl, CACHE_SECONDS)
        self._clock = clock
        self._cache: dict[str, tuple[float, Caller]] = {}
        self._lock = threading.Lock()

    def _principal(
        self, token: AccessToken | None
    ) -> tuple[str, Callable[[SecurityDirectory], SecurityUser | None], str | None]:
        """(principal label, user lookup, domain) for a token, or raise AccessDenied."""
        if token is None:
            if self._local is None:
                raise AccessDenied("no authenticated caller")
            local = self._local
            return f"local:{local}", lambda d: d.user_by_name(local), None
        domain = token_domain(token)
        if domain is not None:
            if self._directory_for is None or not (token.subject or "").isdigit():
                raise AccessDenied("webos sessions are not accepted by this server")
            uid = int(token.subject or "0")
            return f"osession:{domain}|{uid}", lambda d: d.user_by_id(uid), domain
        if token.client_id == STATIC_CLIENT_ID and token.subject:
            name = token.subject
            return f"static:{name}", lambda d: d.user_by_name(name), None
        issuer = str((token.claims or {}).get("iss") or "")
        subject = token.subject or ""
        user = self._oauth.get((issuer, subject))
        if not issuer or not subject or user is None:
            raise AccessDenied("the token's principal is not mapped to a webos user")
        mapped = user
        return f"oauth:{issuer}|{subject}", lambda d: d.user_by_name(mapped), None

    def resolve(self, token: AccessToken | None) -> Caller:
        label, lookup, domain = self._principal(token)
        now = self._clock()
        with self._lock:
            hit = self._cache.get(label)
            if hit is not None and hit[0] > now:
                return hit[1]
        try:
            directory = self._dir
            if domain is not None and self._directory_for is not None:  # checked in _principal
                directory = self._directory_for(domain)
            user = lookup(directory)
            if user is None or not user.enabled:
                raise AccessDenied("the webos user is unknown or disabled")
            caller = Caller(
                user_id=user.id,
                user_name=user.name,
                display_name=user.display_name,
                principal=label,
                rights=directory.rights(user.id),
                administrator=directory.is_administrator(user.id),
                enabled=user.enabled,
            )
        except (SecurityUnavailable, DomainUnavailable):
            raise AccessDenied("authorization data is unavailable") from None
        with self._lock:
            self._cache[label] = (now + self._ttl, caller)
        return caller
