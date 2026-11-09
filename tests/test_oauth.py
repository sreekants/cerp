"""MPC001-057 OAuth resource server: JWT validation, JWKS rotation, introspection, HTTP 401s."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import anyio
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from starlette.testclient import TestClient

from cerp.app.asgi import build_app
from cerp.app.config import ConfigError, Settings, load_settings
from cerp.auth import oauth
from cerp.auth.oauth import IntrospectionConfig, OAuthConfig, OAuthVerifier
from tests.test_protocol import ACCEPT, MODERN, _meta

ISSUER = "https://idp.example.test"
AUDIENCE = "http://127.0.0.1:7411/mcp"
JWKS_URL = f"{ISSUER}/jwks"


def _rsa() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key: Any, kid: str) -> dict[str, Any]:
    pub = key.public_key()
    if isinstance(key, rsa.RSAPrivateKey):
        d: dict[str, Any] = jwt.algorithms.RSAAlgorithm.to_jwk(pub, as_dict=True)
    else:
        d = jwt.algorithms.ECAlgorithm.to_jwk(pub, as_dict=True)
    return {**d, "kid": kid, "use": "sig"}


class Idp:
    """A fake issuer: signing keys, a JWKS document and a fetch counter."""

    def __init__(self) -> None:
        self.keys: dict[str, Any] = {"k1": _rsa()}
        self.fetches = 0

    def jwks(self) -> dict[str, Any]:
        self.fetches += 1
        return {"keys": [_jwk(k, kid) for kid, k in self.keys.items()]}

    def token(self, kid: str = "k1", alg: str = "RS256", **claims: Any) -> str:
        now = int(time.time())
        body = {"iss": ISSUER, "aud": AUDIENCE, "sub": "alice", "exp": now + 300, "iat": now, **claims}
        body = {k: v for k, v in body.items() if v is not None}
        return jwt.encode(body, self.keys[kid], algorithm=alg, headers={"kid": kid})


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> Idp:
    fake = Idp()
    monkeypatch.setattr(oauth, "fetch_json", lambda url: fake.jwks())
    return fake


def _verifier(clock: Any = time.time, **kw: Any) -> OAuthVerifier:
    cfg = OAuthConfig(issuer=ISSUER, audience=AUDIENCE, jwks_url=JWKS_URL, **kw)
    return OAuthVerifier(cfg, clock=clock)


def _check(v: OAuthVerifier, token: str) -> Any:
    return anyio.run(v.verify_token, token)


def test_valid_token(idp: Idp) -> None:
    at = _check(_verifier(), idp.token(scope="mcp:read mcp:write"))
    assert at is not None
    assert at.subject == "alice"
    assert at.scopes == ["mcp:read", "mcp:write"]
    assert at.resource == AUDIENCE


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "https://other.example/mcp"},
        {"iss": "https://evil.example"},
        {"exp": int(time.time()) - 120},
        {"nbf": int(time.time()) + 120},
        {"sub": None},
        {"exp": None},
    ],
    ids=["other-audience", "other-issuer", "expired", "not-yet-valid", "no-subject", "no-exp"],
)
def test_bad_claims_are_refused(idp: Idp, claims: dict[str, Any]) -> None:
    assert _check(_verifier(), idp.token(**claims)) is None


def test_clock_skew_within_leeway_is_accepted(idp: Idp) -> None:
    assert _check(_verifier(), idp.token(exp=int(time.time()) - 30)) is not None


def test_alg_none_is_refused(idp: Idp) -> None:
    unsigned = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "exp": 9e9}, None, algorithm="none")
    assert _check(_verifier(), unsigned) is None


def test_hmac_with_public_key_is_refused(idp: Idp) -> None:
    """The classic confusion attack: sign HS256 with the RSA public key as the secret."""
    pem = (
        idp.keys["k1"]
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )

    def b64(data: bytes) -> bytes:
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "exp": 9999999999}
    signing_input = (
        b64(json.dumps({"alg": "HS256", "kid": "k1"}).encode()) + b"." + b64(json.dumps(claims).encode())
    )
    signature = b64(hmac.new(pem, signing_input, hashlib.sha256).digest())
    token = (signing_input + b"." + signature).decode()
    assert _check(_verifier(), token) is None


def test_hmac_cannot_be_configured() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        _verifier(algorithms=("RS256", "HS256"))


def test_es256_works(idp: Idp) -> None:
    idp.keys["e1"] = ec.generate_private_key(ec.SECP256R1())
    assert _check(_verifier(), idp.token(kid="e1", alg="ES256")) is not None


def test_required_scopes(idp: Idp) -> None:
    v = _verifier(required_scopes=("mcp:read",))
    assert _check(v, idp.token(scope="other")) is None
    assert _check(v, idp.token(scp=["mcp:read"])) is not None


def test_rotated_key_is_accepted_after_one_refresh(idp: Idp) -> None:
    now = [1000.0]
    v = _verifier(clock=lambda: now[0])
    assert _check(v, idp.token()) is not None
    assert idp.fetches == 1
    idp.keys = {"k2": _rsa()}  # the issuer rotates
    now[0] += 61
    assert _check(v, idp.token(kid="k2")) is not None
    assert idp.fetches == 2


def test_unknown_kids_do_not_hammer_the_issuer(idp: Idp) -> None:
    now = [1000.0]
    v = _verifier(clock=lambda: now[0])
    assert _check(v, idp.token()) is not None
    idp.keys["k9"] = _rsa()
    for _ in range(5):
        assert _check(v, idp.token(kid="k9")) is None  # within the refresh interval
    assert idp.fetches == 1
    now[0] += 61
    assert _check(v, idp.token(kid="k9")) is not None
    assert idp.fetches == 2


def test_jwks_failure_keeps_old_keys(idp: Idp, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    v = _verifier(clock=lambda: now[0])
    assert _check(v, idp.token()) is not None

    def down(url: str) -> Any:
        raise OSError("unreachable")

    monkeypatch.setattr(oauth, "fetch_json", down)
    now[0] += 61
    assert _check(v, idp.token(kid="k1")) is not None  # cached key still works
    assert v.key_material_ready()


# ---------------------------------------------------------------- introspection


def test_introspection_for_opaque_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, str]] = []
    now = 1000.0

    def post(url: str, form: dict[str, str], headers: dict[str, str]) -> Any:
        calls.append(form)
        if form["token"] == "good-opaque":  # noqa: S105 - test token, not a credential
            return {"active": True, "sub": "bob", "scope": "mcp:read", "aud": AUDIENCE, "exp": now + 100}
        return {"active": False}

    monkeypatch.setattr(oauth, "post_form", post)
    cfg = OAuthConfig(
        issuer=ISSUER, audience=AUDIENCE, introspection=IntrospectionConfig(url=f"{ISSUER}/introspect")
    )
    v = OAuthVerifier(cfg, clock=lambda: now)
    at = _check(v, "good-opaque")
    assert at is not None
    assert at.subject == "bob"
    assert _check(v, "good-opaque") is not None
    assert len(calls) == 1  # cached
    assert _check(v, "revoked-opaque") is None
    assert v.key_material_ready()


# ---------------------------------------------------------------- configuration and HTTP


def test_config_validation() -> None:
    with pytest.raises(ConfigError, match="issuer"):
        load_settings(None, {"CERP__AUTH__OAUTH__ENABLED": "true"})
    with pytest.raises(ValueError, match="asymmetric"):
        Settings.model_validate(
            {
                "auth": {
                    "oauth": {
                        "enabled": True,
                        "issuer": ISSUER,
                        "jwks_url": JWKS_URL,
                        "algorithms": ["HS256"],
                    }
                }
            }
        )
    with pytest.raises(ValueError, match="not both"):
        Settings.model_validate(
            {
                "auth": {
                    "oauth": {"enabled": True, "issuer": ISSUER, "jwks_url": JWKS_URL},
                    "static_tokens": {"enabled": True},
                }
            }
        )


def _oauth_settings() -> Settings:
    return Settings.model_validate(
        {"auth": {"oauth": {"enabled": True, "issuer": ISSUER, "jwks_url": JWKS_URL}}}
    )


def _tools_list(c: TestClient, token: str | None) -> Any:
    hdr = {**ACCEPT, "MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/list"}
    if token:
        hdr["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": _meta()}}
    return c.post("/mcp", json=body, headers=hdr)


def test_http_401_for_wrong_audience_and_alg_none(idp: Idp) -> None:
    app, _, rt = build_app(_oauth_settings(), {})
    with TestClient(app, base_url="http://127.0.0.1:7411") as c:
        assert _tools_list(c, idp.token()).status_code == 200
        assert _tools_list(c, None).status_code == 401
        assert _tools_list(c, idp.token(aud="https://other.example/mcp")).status_code == 401
        none = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "exp": 9e9}, None, algorithm="none")
        assert _tools_list(c, none).status_code == 401
        assert c.get("/readyz").status_code == 200
    assert rt.readiness()["key_material"]
