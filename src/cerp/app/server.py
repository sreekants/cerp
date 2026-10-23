"""Assemble the MCP server from configuration (MPC001-013..017, -059, -080, -119, -162).

Startup order: validate the protocol revisions against the pinned SDK; set up authentication; load the
model registry and open the datasource map; check journal modes; load and validate the exposure against
the registry and the live schema; register tools; log one summary line. Any failure raises
:class:`StartupError` (or :class:`~cerp.app.config.ConfigError`) before the server accepts a request.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

from cerp import __version__
from cerp.app.config import ConfigError, Settings
from cerp.auth.static import StaticTokenSpec, StaticTokenVerifier, assert_loopback
from cerp.data.exposure import Exposure, load_exposure
from cerp.data.registry import Registry, load_registry
from cerp.data.sqlite import Databases, DbOptions
from cerp.protocol.boundary import RequestMiddleware
from cerp.protocol.naming import TOOL_CATALOGUE_VERSION, check_unique
from cerp.tools.describe import register_describe_tools

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
        for f in ("resources", "prompts")
        if getattr(settings.features, f)
    ]
    if settings.auth.static_tokens.enabled and not settings.server.discovery_requires_auth:
        errors.append(
            "server.discovery_requires_auth: unauthenticated discovery is not implemented yet (MPC001-015)"
        )
    if errors:
        raise ConfigError(errors)


def _auth(
    settings: Settings, environ: Mapping[str, str]
) -> tuple[StaticTokenVerifier | None, AuthSettings | None]:
    st = settings.auth.static_tokens
    if not st.enabled:
        return None, None
    assert_loopback(settings.http.host)
    verifier = StaticTokenVerifier([StaticTokenSpec(t.token_env, t.user) for t in st.tokens], environ)
    base = f"http://{settings.http.host}:{settings.http.port}"
    # Static tokens are minted for this server only and carry no audience, so the SDK's audience check is off.
    auth = AuthSettings(
        issuer_url=base,  # type: ignore[arg-type]
        resource_server_url=f"{base}{settings.http.path}",  # type: ignore[arg-type]
        validate_token_resource=False,
    )
    return verifier, auth


def _data(settings: Settings, rt: Runtime) -> None:
    if settings.registry.path is None:
        if settings.exposure.path is not None:
            raise ConfigError(["exposure.path: an exposure needs registry.path"])
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
    if settings.data.dbroot is not None:
        files.update(rt.registry.database_files(settings.data.dbroot))
    for name, p in settings.data.datasources.items():
        files[name] = p if p.is_absolute() or settings.data.dbroot is None else settings.data.dbroot / p
    files = {n: p for n, p in files.items() if "$(" not in str(p)}
    rt.databases = Databases(
        files,
        DbOptions(settings.data.busy_timeout_ms, settings.data.foreign_keys, settings.data.write_budget_ms),
    )
    rt.databases.check_journal_modes()

    if settings.exposure.path is not None:
        dbs = rt.databases

        def live(ds: str, table: str) -> list[str] | None:
            try:
                return dbs.table_columns(ds, table)
            except (KeyError, FileNotFoundError):
                return None

        rt.exposure = load_exposure(settings.exposure.path, rt.registry, live)


def build_runtime(
    settings: Settings | None = None, environ: Mapping[str, str] | None = None
) -> tuple[MCPServer, Runtime]:
    settings = settings or Settings()
    env = os.environ if environ is None else environ
    _check_protocol_versions(settings)
    _check_features(settings)
    rt = Runtime(settings)
    verifier, auth = _auth(settings, env)
    _data(settings, rt)

    mcp = MCPServer(
        name=settings.server.name,
        title=settings.server.title,
        version=__version__,
        instructions=settings.server.instructions,
        token_verifier=verifier,
        auth=auth,
        middleware=[RequestMiddleware(settings.server.protocol_versions)],
    )
    if rt.exposure is not None:
        rt.tool_names += register_describe_tools(mcp, rt.exposure)
    check_unique(rt.tool_names)
    log.info(
        "cerp %s ready: tool catalogue v%d, %d tools, protocol %s, auth %s",
        __version__,
        TOOL_CATALOGUE_VERSION,
        len(rt.tool_names),
        ",".join(settings.server.protocol_versions),
        "static-dev-tokens" if verifier else "none",
    )
    return mcp, rt


def build_server(settings: Settings | None = None, environ: Mapping[str, str] | None = None) -> MCPServer:
    """Return a configured cerp MCP server (default settings: no data, no tools)."""
    return build_runtime(settings, environ)[0]
