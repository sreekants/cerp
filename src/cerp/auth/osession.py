"""Authenticate a webos session (OSESSION) sent as a bearer token (MPC001-286, decision 0005).

The bearer value is ``OSESSION/<session>``. ``<session>`` is the webos ``OSESSION`` cookie value, URL-encoded
or not: ``domain,user_id,salt,hash``. It is checked exactly as ``Session::FromString`` (Cortex,
``code/webos/src/odesk/portable/Session.cpp``) checks it: ``hash`` must equal the MD5 digest, in webos's
nibble-swapped hex (:func:`webos_hex`), of the domain's GUID (``nodedb.s3db``, :mod:`cerp.data.domains`) followed by ``domain,user_id,salt``. The digest is
compared in constant time and without regard to hex case.

A verified session yields an access token whose subject is the webos user id and whose ``domain`` claim names
the domain; :class:`cerp.auth.principals.PrincipalMapper` then loads that user from the domain's own security
database, and the tools use the domain's databases (:mod:`cerp.data.tenants`).

Limits, inherited from webos and recorded in decision 0005: the session carries no expiry and cannot be
revoked short of changing the domain GUID, and MD5 over a GUID that is not secret from the web server is a
weak seal. cerp never accepts a session webos would refuse, and is stricter in two ways: the domain must be
enabled and the user must exist and be enabled in the domain's security database.

:class:`DispatchingVerifier` sends ``OSESSION/`` tokens here and every other token to the configured static
or OAuth verifier, so both kinds of bearer work on one server.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import unquote

from mcp.server.auth.provider import AccessToken

from cerp.data.domains import DomainDirectory, DomainUnavailable, normalise

log = logging.getLogger("cerp.auth")

PREFIX: Final = "OSESSION/"
OSESSION_CLIENT_ID: Final = "cerp-osession"
DOMAIN_CLAIM: Final = "domain"
MAX_SESSION: Final = 1024  # Session::FromString: check_overflow(Session, MAX_STR)
MAX_PART: Final = 64  # MAX_STR_64 for user id and salt
_USER_ID: Final = re.compile(r"^[1-9][0-9]{0,18}$")
_DIGEST: Final = re.compile(r"^[0-9a-fA-F]{32}$")


class TokenVerifier(Protocol):
    async def verify_token(self, token: str) -> AccessToken | None: ...


@dataclass(frozen=True)
class OSession:
    domain: str  # as written in the session (it is hashed as written)
    user_id: str
    salt: str
    digest: str

    @property
    def signed_text(self) -> str:
        return f"{self.domain},{self.user_id},{self.salt}"


def parse(value: str) -> OSession | None:
    """The four parts of a session string, or None if it is malformed."""
    if len(value) > MAX_SESSION:
        return None
    text = unquote(value) if "%" in value else value
    parts = text.split(",")
    if len(parts) != 4:  # noqa: PLR2004 - domain, user id, salt, hash
        return None
    domain, user_id, salt, digest = (p.strip() for p in parts)
    if normalise(domain) is None or not _USER_ID.match(user_id) or not _DIGEST.match(digest):
        return None
    if not salt or len(salt) > MAX_PART or len(user_id) > MAX_PART:
        return None
    return OSession(domain, user_id, salt, digest)


def webos_hex(digest: bytes) -> str:
    """Hex as webos prints a digest: each byte's low nibble first.

    ``CMD5::GetDigest`` (empact, ``code/system/src/sec/portable/md5.cpp``) writes through the stream form of
    ``CCryptographicHash::PrintDigest``, which emits ``inttohexlwr(ch)`` and then ``inttohexlwr(ch >> 4)``, so
    every byte's two hex digits come out swapped compared with ``hexdigest()``.
    """
    return "".join(f"{b:02x}"[::-1] for b in digest)


def seal(guid: str, session: OSession) -> str:
    """The digest webos expects: MD5(guid + "domain,user_id,salt"), printed by :func:`webos_hex`."""
    return webos_hex(hashlib.md5((guid + session.signed_text).encode("utf-8"), usedforsecurity=False).digest())


class OSessionVerifier:
    def __init__(self, domains: DomainDirectory) -> None:
        self._domains = domains

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token.startswith(PREFIX):
            return None
        session = parse(token[len(PREFIX) :])
        if session is None:
            log.warning("OSESSION refused: malformed", extra={"event": "auth_failed"})
            return None
        try:
            guid = self._domains.guid(session.domain)
        except DomainUnavailable:
            log.error(  # noqa: TRY400 - no traceback or path in the log (MPC001-067)
                "OSESSION refused: the domain list is unavailable", extra={"event": "auth_failed"}
            )
            return None
        if guid is None:
            log.warning("OSESSION refused: unknown or disabled domain", extra={"event": "auth_failed"})
            return None
        if not hmac.compare_digest(seal(guid, session), session.digest.lower()):
            log.warning("OSESSION refused: seal does not match", extra={"event": "auth_failed"})
            return None
        domain = normalise(session.domain)
        return AccessToken(
            token=token,
            client_id=OSESSION_CLIENT_ID,
            scopes=[],
            subject=session.user_id,
            claims={DOMAIN_CLAIM: domain},
        )


class DispatchingVerifier:
    """``OSESSION/`` bearers go to the OSESSION verifier; everything else to the configured verifier."""

    def __init__(self, fallback: TokenVerifier | None, osession: OSessionVerifier) -> None:
        self._fallback = fallback
        self._osession = osession

    async def verify_token(self, token: str) -> AccessToken | None:
        if token.startswith(PREFIX):
            return await self._osession.verify_token(token)
        if self._fallback is None:
            return None
        return await self._fallback.verify_token(token)


def token_domain(token: AccessToken | None) -> str | None:
    """The domain of an OSESSION token, or None for any other caller."""
    if token is None or token.client_id != OSESSION_CLIENT_ID:
        return None
    value = (token.claims or {}).get(DOMAIN_CLAIM)
    return value if isinstance(value, str) else None
