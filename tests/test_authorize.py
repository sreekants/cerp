"""MPC001-061 authorization port and MPC001-060 principal mapping, unit level.

The rules mirror webos ``AuthenticationAspect::CheckPrivilege`` (DenyAll, AllowAll, method right, then the
administrator) and are stricter where webos has defects (WOS-BUG-033..035). The parity test against a live
webos (MPC002-007) is separate and still to be written.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mcp.server.auth.provider import AccessToken

from cerp.auth.authorize import AccessDenied, Caller, authorize, privilege_model, require
from cerp.auth.principals import PrincipalMapper
from cerp.auth.static import STATIC_CLIENT_ID
from cerp.data.registry import Model, Registry
from cerp.data.security import SecurityDirectory, SecurityUnavailable, is_enabled
from cerp.data.sqlite import Databases
from tests.mvp_tenant import build_security


def _caller(*rights: str, admin: bool = False, enabled: bool = True) -> Caller:
    return Caller(1, "u", "U", "local:u", frozenset(rights), admin, enabled)


def test_method_right_allows() -> None:
    assert authorize(_caller("Account.View"), "Account", "View").allowed
    assert not authorize(_caller("Account.View"), "Account", "Edit").allowed


def test_allow_all_allows_every_verb() -> None:
    for verb in ("View", "Add", "Edit", "Delete"):
        assert authorize(_caller("Account.AllowAll"), "Account", verb).allowed


def test_deny_all_wins_over_allow() -> None:
    d = authorize(_caller("Account.DenyAll", "Account.AllowAll", "Account.View"), "Account", "View")
    assert not d.allowed
    assert d.reason == "DenyAll"


@pytest.mark.parametrize("held", ["Account.ViewAll", "Account.Viewer", "XAccount.View", "account.view"])
def test_rights_match_exactly_not_by_substring(held: str) -> None:
    assert not authorize(_caller(held), "Account", "View").allowed


@pytest.mark.parametrize("verb", ["List", "Export", "Describe", "", "View "])
def test_unclassified_operations_are_denied(verb: str) -> None:
    assert not authorize(_caller("Account.AllowAll"), "Account", verb).allowed


def test_disabled_callers_are_denied_even_as_administrator() -> None:
    assert not authorize(_caller("Account.View", admin=True, enabled=False), "Account", "View").allowed


def test_invalid_privilege_names_are_denied() -> None:
    assert not authorize(_caller("x.View", admin=True), "x;DROP", "View").allowed


def test_administrator_passes_where_webos_lets_them_and_is_audited(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("WARNING", logger="cerp.audit")
    d = authorize(_caller("Account.DenyAll", admin=True), "Account", "View")
    assert d.allowed
    assert d.admin_bypass
    assert [r.__dict__.get("event") for r in caplog.records] == ["admin_bypass"]


def test_require_raises() -> None:
    with pytest.raises(AccessDenied):
        require(_caller(), "Account", "View")
    require(_caller("Account.View"), "Account", "View")


@given(st.frozensets(st.text(max_size=30), max_size=8), st.text(max_size=20), st.text(max_size=10))
def test_never_allows_without_an_exact_right_unless_admin(
    rights: frozenset[str], priv: str, verb: str
) -> None:
    caller = Caller(1, "u", "U", "local:u", rights, False)
    if authorize(caller, priv, verb).allowed:
        assert f"{priv}.{verb}" in rights or f"{priv}.AllowAll" in rights
        assert f"{priv}.DenyAll" not in rights


def _model(key: str, model: str | None, privilege: str | None, enforced: bool) -> Model:
    return Model(key, "Database.Table", model, None, "x", "t", (), None, {}, (), privilege, enforced, ())


def _registry(*models: Model) -> Registry:
    return Registry(1, "test", {}, {}, {m.key: m for m in models})


def test_privilege_model_rules() -> None:
    reg = _registry(
        _model("a", "Account", "Account", True),
        _model("b", "Milestone", None, False),  # no AccessPrivilege: webos skips the check (WOS-BUG-033)
        _model("c", "Special", "Shared", True),
        _model("d", None, None, False),
    )
    assert privilege_model(reg, "a") == "Account"
    assert privilege_model(reg, "b") == "Milestone"  # cerp requires the model's own rights instead
    assert privilege_model(reg, "c") == "Shared"
    with pytest.raises(AccessDenied):
        privilege_model(reg, "d")
    with pytest.raises(AccessDenied):
        privilege_model(reg, "missing")


# ---------------------------------------------------------------- security database and mapping


@pytest.fixture
def directory(tmp_path: Path) -> SecurityDirectory:
    build_security(tmp_path)
    return SecurityDirectory(Databases({"security": tmp_path / "security.s3db"}))


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, True), (0, False), ("Y", True), ("y", True), ("1", True), ("N", False), ("0", False),
     (None, False), ("", False), ("maybe", False), (True, True), (2.5, False)],
)  # fmt: skip
def test_is_enabled_fails_closed(value: object, expected: bool) -> None:
    assert is_enabled(value) is expected


def test_directory_reads_users_rights_and_admins(directory: SecurityDirectory) -> None:
    alice = directory.user_by_name("alice")
    assert alice is not None
    assert alice.enabled
    assert alice.display_name == "Alice Andersen"
    assert "Account.View" in directory.rights(alice.id)
    assert not directory.is_administrator(alice.id)
    assert directory.is_administrator(1)
    bob = directory.user_by_name("bob")
    assert bob is not None
    assert bob.enabled
    carol = directory.user_by_name("carol")
    assert carol is not None
    assert not carol.enabled
    assert directory.user_by_name("ghost") is None


def test_unreadable_security_database_fails_closed(tmp_path: Path) -> None:
    missing = SecurityDirectory(Databases({"security": tmp_path / "nope.s3db"}))
    with pytest.raises(SecurityUnavailable):
        missing.user_by_name("alice")
    mapper = PrincipalMapper(missing, local_user="alice")
    with pytest.raises(AccessDenied, match="unavailable"):
        mapper.resolve(None)
    broken = tmp_path / "broken.s3db"
    sqlite3.connect(broken).close()  # exists but has no tables
    with pytest.raises(SecurityUnavailable):
        SecurityDirectory(Databases({"security": broken})).rights(1)


def _static(user: str) -> AccessToken:
    dummy = "t" * 30  # not a secret: the mapper never looks at the token value
    return AccessToken(token=dummy, client_id=STATIC_CLIENT_ID, scopes=[], subject=user)


def _oauth(iss: str, sub: str) -> AccessToken:
    dummy = "j"  # not a secret: the mapper never looks at the token value
    return AccessToken(token=dummy, client_id="claude", scopes=[], subject=sub, claims={"iss": iss})


def test_static_token_maps_to_its_user(directory: SecurityDirectory) -> None:
    caller = PrincipalMapper(directory).resolve(_static("alice"))
    assert (caller.user_id, caller.principal) == (2, "static:alice")


def test_oauth_maps_by_issuer_and_subject(directory: SecurityDirectory) -> None:
    mapper = PrincipalMapper(
        directory, {("https://a.example", "42"): "alice", ("https://b.example", "42"): "bob"}
    )
    assert mapper.resolve(_oauth("https://a.example", "42")).user_name == "alice"
    assert mapper.resolve(_oauth("https://b.example", "42")).user_name == "bob"
    with pytest.raises(AccessDenied):
        mapper.resolve(_oauth("https://c.example", "42"))
    with pytest.raises(AccessDenied):
        mapper.resolve(_oauth("", "42"))


def test_no_token_needs_a_local_user(directory: SecurityDirectory) -> None:
    with pytest.raises(AccessDenied):
        PrincipalMapper(directory).resolve(None)
    assert PrincipalMapper(directory, local_user="bob").resolve(None).principal == "local:bob"


@pytest.mark.parametrize("user", ["carol", "ghost"])
def test_disabled_and_unknown_users_are_rejected(directory: SecurityDirectory, user: str) -> None:
    with pytest.raises(AccessDenied):
        PrincipalMapper(directory).resolve(_static(user))


def test_disabling_a_user_takes_effect_within_the_cache_time(
    tmp_path: Path, directory: SecurityDirectory
) -> None:
    now = [0.0]
    mapper = PrincipalMapper(directory, ttl=3600, clock=lambda: now[0])  # capped at 60 s
    assert mapper.resolve(_static("alice")).user_id == 2
    con = sqlite3.connect(tmp_path / "security.s3db")
    con.execute("UPDATE users SET enabled = 0 WHERE name = 'alice'")
    con.commit()
    con.close()
    now[0] = 59.0
    assert mapper.resolve(_static("alice")).user_id == 2  # still cached
    now[0] = 60.5
    with pytest.raises(AccessDenied):
        mapper.resolve(_static("alice"))
