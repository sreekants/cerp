"""Assemble the MCP server from configuration (MPC001-013..017, -059, -080, -119, -162).

Startup order: validate the protocol revisions against the pinned SDK; set up authentication; load the
model registry and open the datasource map; check journal modes; load and validate the exposure against
the registry and the live schema; register tools; log one summary line. Any failure raises
:class:`StartupError` (or :class:`~cerp.app.config.ConfigError`) before the server accepts a request.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

from cerp import __version__
from cerp.app.config import ConfigError, ConfigStore, Settings
from cerp.auth.oauth import IntrospectionConfig, OAuthConfig, OAuthVerifier
from cerp.auth.osession import DispatchingVerifier, OSessionVerifier
from cerp.auth.static import StaticTokenSpec, StaticTokenVerifier, assert_loopback
from cerp.data.datasources import DatasourceError, DatasourceLocator
from cerp.data.domains import DomainDirectory
from cerp.data.exposure import Exposure, load_exposure
from cerp.data.registry import Registry, load_registry
from cerp.data.sqlite import Databases, DbOptions
from cerp.data.stores import STORES, MigrationError, migrate_all
from cerp.protocol.boundary import RequestMiddleware
from cerp.protocol.killswitch import KillSwitch
from cerp.protocol.naming import TOOL_CATALOGUE_VERSION, check_unique
from cerp.protocol.scopes import NO_SCOPES, ScopePolicy
from cerp.tools.context import ToolContext
from cerp.tools.describe import describe_tool_names, register_describe_tools
from cerp.tools.packs import PackError, build_context, register_packs

log = logging.getLogger("cerp.app")

SDK_PROTOCOL_VERSIONS = (*HANDSHAKE_PROTOCOL_VERSIONS, *MODERN_PROTOCOL_VERSIONS)


class StartupError(Exception):
    pass


@dataclass
class Runtime:
    """Everything the server built at startup, for tools, health checks and tests."""

    settings: Settings
    registry: Registry | None = None
    databases: Databases | None = None
    exposure: Exposure | None = None
    tool_names: list[str] = field(default_factory=list)
    store: ConfigStore | None = None
    static_tokens: bool = False
    oauth: OAuthVerifier | None = None
    killswitch: KillSwitch | None = None
    exposure_tools: list[str] = field(default_factory=list)
    scopes: ScopePolicy = NO_SCOPES
    unavailable: list[str] = field(default_factory=list)  # datasources with no file and no template
    tool_context: ToolContext | None = None  # curated tool packs (MPC001-088)
    domains: DomainDirectory | None = None  # webos domains for OSESSION callers (MPC001-286)

    def needed_databases(self) -> list[str]:
        """Datasources that exposed models read or write."""
        if self.exposure is None:
            return []
        return sorted({m.module for m in self.exposure.models})

    def readiness(self) -> dict[str, bool]:
        """Checks behind ``/readyz`` (MPC001-037). Every value must be True for the server to be ready."""
        checks: dict[str, bool] = {"configuration": True}
        if self.settings.registry.path is not None:
            checks["registry"] = self.registry is not None
        if self.databases is not None:
            # Only the databases that exposed models use: a datasource with no file and no template is
            # "unavailable" (MPC001-200) and matters only if something needs it.
            for name in self.needed_databases():
                checks[f"database:{name}"] = self.databases.can_open(name)
        if self.settings.auth.static_tokens.enabled:
            checks["key_material"] = self.static_tokens
        if self.oauth is not None:
            checks["key_material"] = self.oauth.key_material_ready()
        return checks


def _check_protocol_versions(settings: Settings) -> None:
    errors = [
        f"server.protocol_versions: {v!r} is not supported by the pinned MCP SDK "
        f"(supported: {', '.join(SDK_PROTOCOL_VERSIONS)})"
        for v in settings.server.protocol_versions
        if v not in SDK_PROTOCOL_VERSIONS
    ]
    if not settings.server.protocol_versions:
        errors.append("server.protocol_versions: at least one revision is required")
    if errors:
        raise ConfigError(errors)


def _check_features(settings: Settings) -> None:
    # MPC001-017: never declare what is not implemented. Resources and prompts are not built yet.
    errors = [
        f"features.{f}: not implemented in this release"
        for f in ("resources", "prompts", "tasks")
        if getattr(settings.features, f)
    ]
    auth_on = settings.auth.static_tokens.enabled or settings.auth.oauth.enabled
    if auth_on and not settings.server.discovery_requires_auth:
        errors.append(
            "server.discovery_requires_auth: unauthenticated discovery is not implemented yet (MPC001-015)"
        )
    if errors:
        raise ConfigError(errors)


def _auth(
    settings: Settings, environ: Mapping[str, str]
) -> tuple[StaticTokenVerifier | OAuthVerifier | None, AuthSettings | None, DomainDirectory | None]:
    """The configured verifier and auth settings, and the domain directory when OSESSION is on.

    With ``auth.osession`` (MPC001-286), the server's verifier is a :class:`DispatchingVerifier`: bearers of
    the form ``OSESSION/<session>`` are checked against the webos domains, all others by the static or OAuth
    verifier. The returned verifier is the static or OAuth one, for the runtime's own checks.
    """
    verifier, auth = _primary_auth(settings, environ)
    os_cfg = settings.auth.osession
    if not os_cfg.enabled:
        return verifier, auth, None
    assert os_cfg.domain_root is not None  # noqa: S101 - enforced by configuration validation
    domains = DomainDirectory(os_cfg.domain_root, ttl=float(os_cfg.cache_seconds))
    if auth is None:
        base = f"http://{settings.http.host}:{settings.http.port}"
        auth = AuthSettings(
            issuer_url=base,  # type: ignore[arg-type]  # pydantic coerces str to AnyHttpUrl
            resource_server_url=f"{base}{settings.http.path}",  # type: ignore[arg-type]
            validate_token_resource=False,  # a webos session has no audience
        )
    log.info("webos sessions (OSESSION bearer) accepted; domains under %s", os_cfg.domain_root)
    return verifier, auth, domains


def _primary_auth(
    settings: Settings, environ: Mapping[str, str]
) -> tuple[StaticTokenVerifier | OAuthVerifier | None, AuthSettings | None]:
    base = f"http://{settings.http.host}:{settings.http.port}"
    resource = f"{base}{settings.http.path}"
    oa = settings.auth.oauth
    if oa.enabled:
        audience = oa.audience or resource
        intro = None
        if oa.introspection is not None:
            i = oa.introspection
            intro = IntrospectionConfig(
                url=i.url,
                client_id=environ.get(i.client_id_env) if i.client_id_env else None,
                client_secret=environ.get(i.client_secret_env) if i.client_secret_env else None,
                cache_seconds=i.cache_seconds,
            )
        assert oa.issuer is not None  # noqa: S101 - enforced by configuration validation
        oauth_verifier = OAuthVerifier(
            OAuthConfig(
                issuer=oa.issuer,
                audience=audience,
                jwks_url=oa.jwks_url,
                algorithms=tuple(oa.algorithms),
                leeway_seconds=oa.leeway_seconds,
                required_scopes=tuple(oa.required_scopes),
                jwks_refresh_seconds=oa.jwks_refresh_seconds,
                introspection=intro,
            )
        )
        auth = AuthSettings(
            issuer_url=oa.issuer,  # type: ignore[arg-type]  # pydantic coerces str to AnyHttpUrl
            resource_server_url=audience,  # type: ignore[arg-type]  # pydantic coerces str to AnyHttpUrl
            required_scopes=list(oa.required_scopes) or None,
            validate_token_resource=True,
        )
        log.info("OAuth resource server: issuer %s, audience %s", oa.issuer, audience)
        return oauth_verifier, auth
    st = settings.auth.static_tokens
    if not st.enabled:
        return None, None
    assert_loopback(settings.http.host)
    verifier = StaticTokenVerifier([StaticTokenSpec(t.token_env, t.user) for t in st.tokens], environ)
    # Static tokens are minted for this server only and carry no audience, so the SDK's audience check is off.
    auth = AuthSettings(
        issuer_url=base,  # type: ignore[arg-type]  # pydantic coerces str to AnyHttpUrl
        resource_server_url=resource,  # type: ignore[arg-type]  # pydantic coerces str to AnyHttpUrl
        validate_token_resource=False,
    )
    return verifier, auth


def _db_options(settings: Settings) -> DbOptions:
    d = settings.data
    return DbOptions(d.busy_timeout_ms, d.foreign_keys, d.write_budget_ms)


def _locate(settings: Settings, environ: Mapping[str, str], rt: Runtime) -> dict[str, Path]:
    """Resolve databases through the datasource locator and bootstrap missing ones (MPC001-200)."""
    d = settings.data
    assert d.datasources_file is not None  # noqa: S101 - checked by the caller
    try:
        locator = DatasourceLocator.load(
            d.datasources_file,
            environ,
            root=d.root,
            overrides={"DBROOT": str(d.dbroot)} if d.dbroot is not None else None,
            strict=d.strict_datasources,
        )
    except DatasourceError as exc:
        raise ConfigError([f"data.datasources_file: {e}" for e in exc.errors]) from None
    report = locator.bootstrap(enabled=d.bootstrap_from_templates)
    rt.unavailable = sorted({*report.unavailable, *locator.unresolved})
    if locator.unresolved:
        log.warning(
            "datasources not resolvable here (unavailable): %s", ", ".join(sorted(locator.unresolved))
        )
    if d.strict_datasources and not d.bootstrap_from_templates and report.unavailable:
        raise ConfigError(
            [
                f"datasource {n}: {locator.path(n)} is missing and bootstrap is disabled"
                for n in report.unavailable
            ]
        )
    return {n: p for n, p in locator.paths().items() if n not in report.unavailable}


def _data(settings: Settings, rt: Runtime, environ: Mapping[str, str]) -> None:
    if settings.data.state_dir is not None:  # cerp's own stores, migrated before use (MPC001-166)
        try:
            migrate_all(STORES, settings.data.state_dir)
        except MigrationError as exc:
            raise StartupError(str(exc)) from None
    # Bootstrap runs first, before any database is opened (MPC001-200, MPC001-162).
    located = _locate(settings, environ, rt) if settings.data.datasources_file is not None else None
    if settings.registry.path is None:
        if settings.exposure.path is not None:
            raise ConfigError(["exposure.path: an exposure needs registry.path"])
        if located is not None:
            rt.databases = Databases(located, _db_options(settings))
        return
    rt.registry = load_registry(settings.registry.path)
    log.info(
        "registry %s: schema %d, generator %s, %d models, %d warnings, sources %s",
        settings.registry.path.name,
        rt.registry.schema_version,
        rt.registry.generator_version,
        len(rt.registry.models),
        len(rt.registry.warnings),
        rt.registry.fingerprint,
    )
    files: dict[str, Path] = {}
    if located is not None:
        files.update(located)
    elif settings.data.dbroot is not None:
        files.update(rt.registry.database_files(settings.data.dbroot))
    for name, p in settings.data.datasources.items():
        files[name] = p if p.is_absolute() or settings.data.dbroot is None else settings.data.dbroot / p
    files = {n: p for n, p in files.items() if "$(" not in str(p)}
    rt.databases = Databases(files, _db_options(settings))
    rt.databases.check_journal_modes()

    if settings.exposure.path is not None:
        dbs = rt.databases

        def live(ds: str, table: str) -> list[str] | None:
            try:
                return dbs.table_columns(ds, table)
            except (KeyError, FileNotFoundError):
                return None

        if rt.unavailable:  # name the datasource before the drift check reports missing tables
            exposed = load_exposure(settings.exposure.path, rt.registry).models
            needed = sorted({m.module for m in exposed} & set(rt.unavailable))
            if needed:
                raise StartupError(
                    "exposed models need unavailable datasources (no database and no template): "
                    + ", ".join(needed)
                )
        rt.exposure = load_exposure(settings.exposure.path, rt.registry, live)


def _packs(mcp: MCPServer, rt: Runtime, clock: Callable[[], dt.datetime] | None) -> list[str]:
    """Register the curated read-only tool packs (MPC001-088) after checking what they need."""
    s = rt.settings
    if rt.registry is None or rt.databases is None:
        raise ConfigError(["tools.packs: curated tools need registry.path and the webos databases"])
    assert rt.killswitch is not None  # noqa: S101 - set before tools are registered
    rt.tool_context = build_context(
        rt.databases,
        rt.registry,
        {(p.issuer, p.subject): p.user for p in s.auth.principals},
        local_user=s.auth.local_user,
        cursor_ttl=s.tools.cursor_ttl_seconds,
        clock=clock,
        domains=rt.domains,
        packs=s.tools.packs,
        db_options=_db_options(s),
    )
    try:
        names = register_packs(mcp, rt.tool_context, s.tools.packs, rt.killswitch, rt.scopes)
    except PackError as exc:
        raise StartupError(str(exc)) from None
    log.info("tool packs %s: %d tools", ",".join(s.tools.packs), len(names))
    return names


def build_runtime(
    settings: Settings | None = None,
    environ: Mapping[str, str] | None = None,
    store: ConfigStore | None = None,
    *,
    clock: Callable[[], dt.datetime] | None = None,
) -> tuple[MCPServer, Runtime]:
    """Build the server. ``store`` supplies live, reloadable settings (kill switches); default: fixed."""
    settings = settings or (store.current if store else Settings())
    store = store or ConfigStore(settings)
    killswitch = KillSwitch(lambda: store.current.kill_switches)
    env = os.environ if environ is None else environ
    _check_protocol_versions(settings)
    _check_features(settings)
    rt = Runtime(settings, store=store, static_tokens=False)
    verifier, auth, domains = _auth(settings, env)
    rt.domains = domains
    rt.static_tokens = isinstance(verifier, StaticTokenVerifier)
    rt.oauth = verifier if isinstance(verifier, OAuthVerifier) else None
    rt.scopes = ScopePolicy(enforce=rt.oauth is not None and settings.auth.oauth.enforce_scopes)
    _data(settings, rt, env)

    mcp = MCPServer(
        name=settings.server.name,
        title=settings.server.title,
        version=__version__,
        instructions=settings.server.instructions,
        token_verifier=DispatchingVerifier(verifier, OSessionVerifier(domains)) if domains else verifier,
        auth=auth,
        middleware=[RequestMiddleware(settings.server.protocol_versions, killswitch)],
    )
    rt.killswitch = killswitch
    if rt.exposure is not None:
        rt.exposure_tools = register_describe_tools(mcp, rt.exposure, killswitch, rt.scopes)
        rt.tool_names += rt.exposure_tools
    if settings.tools.packs:
        rt.tool_names += _packs(mcp, rt, clock)
    check_unique(rt.tool_names)
    log.info(
        "cerp %s ready: tool catalogue v%d, %d tools, protocol %s, auth %s",
        __version__,
        TOOL_CATALOGUE_VERSION,
        len(rt.tool_names),
        ",".join(settings.server.protocol_versions),
        {StaticTokenVerifier: "static-dev-tokens", OAuthVerifier: "oauth"}.get(type(verifier), "none")
        + ("+osession" if domains else ""),
    )
    return mcp, rt


def reload_exposure(mcp: MCPServer, rt: Runtime) -> tuple[list[str], list[str]]:
    """Re-read the exposure file named in the live settings and swap the exposure tools (MPC001-115).

    The new exposure is fully validated (registry and live schema) before anything changes; on error
    :class:`ExposureError` is raised and the running tools are untouched. Returns (added, removed) names.
    """
    path = rt.store.current.exposure.path if rt.store else rt.settings.exposure.path
    if path is None or rt.registry is None or rt.databases is None or rt.killswitch is None:
        return [], []
    dbs = rt.databases

    def live(ds: str, table: str) -> list[str] | None:
        try:
            return dbs.table_columns(ds, table)
        except (KeyError, FileNotFoundError):
            return None

    new = load_exposure(path, rt.registry, live)
    old_models = {m.key: m for m in rt.exposure.models} if rt.exposure else {}
    new_models = {m.key: m for m in new.models}
    # Tools of unchanged models stay registered, so calls to them never miss during a reload.
    stale = Exposure(tuple(m for k, m in old_models.items() if new_models.get(k) != m))
    fresh = Exposure(tuple(m for k, m in new_models.items() if old_models.get(k) != m))
    stale_names = describe_tool_names(stale)
    for name in stale_names:
        mcp.remove_tool(name)
    old_names = list(rt.exposure_tools)
    register_describe_tools(mcp, fresh, rt.killswitch, rt.scopes)
    new_names = describe_tool_names(new)
    rt.exposure = new
    rt.exposure_tools = new_names
    rt.tool_names = [n for n in rt.tool_names if n not in old_names] + new_names
    check_unique(rt.tool_names)
    added = sorted(set(new_names) - set(old_names))
    removed = sorted(set(old_names) - set(new_names))
    log.info(
        "exposure reloaded: %d tools (added: %s; removed: %s)", len(new_names), added or "-", removed or "-"
    )
    return added, removed


def build_server(settings: Settings | None = None, environ: Mapping[str, str] | None = None) -> MCPServer:
    """Return a configured cerp MCP server (default settings: no data, no tools)."""
    return build_runtime(settings, environ)[0]
