"""OAuth 2.1 resource-server token validation (MPC001-057).

:class:`OAuthVerifier` implements the SDK's ``TokenVerifier`` protocol:

* **JWT access tokens** are validated with PyJWT: the header algorithm must be on the configured
  asymmetric allow-list (``none`` and HMAC are refused even if configured, so a public key can never be
  used as an HMAC secret); the signature is checked against the issuer's JWKS; ``iss`` must equal the
  configured issuer, ``aud`` must contain this server's resource URI, ``exp`` and ``nbf`` are checked with
  the configured leeway (default 60 s), and the required scopes must be present.
* **JWKS** are cached. An unknown ``kid`` triggers one refresh, at most once per ``jwks_refresh_seconds``,
  so a rotated key is picked up without letting forged ``kid`` values hammer the issuer.
* **Opaque tokens** (anything that is not a JWT) go to RFC 7662 introspection when configured. Active
  results are cached until the token's ``exp`` or ``cache_seconds``, whichever comes first.

Fetching is done by :func:`fetch_json` / :func:`post_form` in a worker thread; tests replace them. No token
value is logged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import anyio.to_thread
import jwt
from mcp.server.auth.provider import AccessToken

log = logging.getLogger("cerp.auth")

FORBIDDEN_ALGORITHMS = frozenset({"none", "HS256", "HS384", "HS512"})
FETCH_TIMEOUT_S = 10


def fetch_json(url: str) -> Any:
    """GET ``url`` and parse JSON (JWKS). Replaced in tests."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310 - URL is operator configuration, https enforced by deployment
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:  # noqa: S310 - same
        return json.loads(resp.read())


def post_form(url: str, form: Mapping[str, str], headers: Mapping[str, str]) -> Any:
    """POST a form and parse JSON (introspection). Replaced in tests."""
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, headers={"Accept": "application/json", **headers})  # noqa: S310 - URL is operator configuration
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:  # noqa: S310 - same
        return json.loads(resp.read())


@dataclass
class IntrospectionConfig:
    url: str
    client_id: str | None = None
    client_secret: str | None = None
    cache_seconds: int = 300


@dataclass
class OAuthConfig:
    issuer: str
    audience: str
    jwks_url: str | None = None
    algorithms: tuple[str, ...] = ("RS256", "ES256")
    leeway_seconds: int = 60
    required_scopes: tuple[str, ...] = ()
    jwks_refresh_seconds: int = 60
    introspection: IntrospectionConfig | None = None


@dataclass
class _JwksCache:
    url: str
    refresh_seconds: int
    clock: Callable[[], float]
    keys: dict[str, Any] = field(default_factory=dict)
    fetched_at: float | None = None

    async def key(self, kid: str | None) -> Any | None:
        if kid in self.keys:
            return self.keys[kid]
        now = self.clock()
        if self.fetched_at is not None and now - self.fetched_at < self.refresh_seconds:
            return None  # refreshed recently; an unknown kid does not trigger another fetch
        await self._refresh(now)
        return self.keys.get(kid) if kid is not None else None

    async def _refresh(self, now: float) -> None:
        await anyio.to_thread.run_sync(self.refresh_sync, now)

    def refresh_sync(self, now: float) -> None:
        self.fetched_at = now
        try:
            doc = fetch_json(self.url)
            keys: dict[str, Any] = {}
            for jwk in doc.get("keys", []):
                if jwk.get("use", "sig") != "sig" or not jwk.get("kid"):
                    continue
                keys[str(jwk["kid"])] = jwt.PyJWK.from_dict(jwk).key
        except Exception:  # any fetch or parse failure keeps the old keys
            log.warning("JWKS refresh from %s failed; keeping %d cached keys", self.url, len(self.keys))
            return
        self.keys = keys
        log.info("JWKS refreshed from %s: %d keys", self.url, len(keys))


def _scopes(claims: Mapping[str, Any]) -> list[str]:
    raw = claims.get("scope", claims.get("scp", []))
    if isinstance(raw, str):
        return raw.split()
    if isinstance(raw, list):
        return [str(s) for s in raw]
    return []


def _looks_like_jwt(token: str) -> bool:
    parts = token.split(".")
    if len(parts) != 3:  # noqa: PLR2004 - header.payload.signature
        return False
    try:
        header = json.loads(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
    except ValueError:
        return False
    return isinstance(header, dict) and "alg" in header


class OAuthVerifier:
    def __init__(self, config: OAuthConfig, *, clock: Callable[[], float] = time.time) -> None:
        bad = set(config.algorithms) & FORBIDDEN_ALGORITHMS
        if bad:
            raise ValueError(f"forbidden JWT algorithms: {sorted(bad)}")
        self.config = config
        self._clock = clock
        self._jwks = (
            _JwksCache(config.jwks_url, config.jwks_refresh_seconds, clock) if config.jwks_url else None
        )
        self._introspected: dict[str, tuple[float, AccessToken]] = {}

    def key_material_ready(self) -> bool:
        """Readiness (MPC001-037): signing keys loaded, fetching them once if never fetched."""
        if self._jwks is None:
            return self.config.introspection is not None
        if self._jwks.fetched_at is None:
            self._jwks.refresh_sync(self._clock())
        return bool(self._jwks.keys)

    async def verify_token(self, token: str) -> AccessToken | None:
        if _looks_like_jwt(token):
            return await self._verify_jwt(token) if self._jwks is not None else None
        if self.config.introspection is not None:
            return await self._introspect(token)
        return None

    def _accept(self, token: str, claims: Mapping[str, Any]) -> AccessToken | None:
        scopes = _scopes(claims)
        missing = [s for s in self.config.required_scopes if s not in scopes]
        if missing:
            log.info("token refused: missing scopes %s", ", ".join(missing))
            return None
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            log.info("token refused: no subject")
            return None
        exp = claims.get("exp")
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id") or claims.get("azp") or ""),
            scopes=scopes,
            expires_at=int(exp) if isinstance(exp, (int, float)) else None,
            resource=self.config.audience,
            subject=sub,
            claims={"iss": claims.get("iss")},
        )

    async def _verify_jwt(self, token: str) -> AccessToken | None:
        assert self._jwks is not None  # noqa: S101 - guarded by the caller
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return None
        alg = header.get("alg")
        if alg in FORBIDDEN_ALGORITHMS or alg not in self.config.algorithms:
            log.info("token refused: algorithm %r not allowed", alg)
            return None
        key = await self._jwks.key(header.get("kid"))
        if key is None:
            log.info("token refused: unknown signing key")
            return None
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[alg],
                audience=self.config.audience,
                issuer=self.config.issuer,
                leeway=self.config.leeway_seconds,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            log.info("token refused: %s", type(exc).__name__)
            return None
        return self._accept(token, claims)

    def _introspection_valid(self, result: Any, now: float) -> bool:
        if not isinstance(result, dict) or result.get("active") is not True:
            return False
        if result.get("iss") not in (None, self.config.issuer):
            return False
        aud = result.get("aud")
        auds = aud if isinstance(aud, list) else [aud] if aud else []
        if auds and self.config.audience not in auds:
            return False
        exp = result.get("exp")
        return not (isinstance(exp, (int, float)) and exp + self.config.leeway_seconds < now)

    async def _introspect(self, token: str) -> AccessToken | None:
        cfg = self.config.introspection
        assert cfg is not None  # noqa: S101 - guarded by the caller
        now = self._clock()
        cache_key = hashlib.sha256(token.encode()).hexdigest()  # never keep raw tokens as keys
        cached = self._introspected.get(cache_key)
        if cached is not None and cached[0] > now:
            return cached[1]
        headers: dict[str, str] = {}
        if cfg.client_id and cfg.client_secret:
            pair = f"{urllib.parse.quote(cfg.client_id)}:{urllib.parse.quote(cfg.client_secret)}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        try:
            result = await anyio.to_thread.run_sync(
                post_form, cfg.url, {"token": token, "token_type_hint": "access_token"}, headers
            )
        except Exception:  # an unreachable introspection endpoint means "not authenticated"
            log.warning("token introspection at %s failed", cfg.url)
            return None
        if not self._introspection_valid(result, now):
            return None
        exp = result.get("exp")
        accepted = self._accept(token, {"iss": self.config.issuer, **result})
        if accepted is not None:
            until = now + cfg.cache_seconds
            if isinstance(exp, (int, float)):
                until = min(until, float(exp))
            self._introspected[cache_key] = (until, accepted)
            if len(self._introspected) > 10_000:  # noqa: PLR2004 - bound the cache
                self._introspected = {k: v for k, v in self._introspected.items() if v[0] > now}
        return accepted
