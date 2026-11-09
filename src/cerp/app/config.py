"""Server configuration: one YAML file, validated by pydantic, overridable by environment (MPC001-115).

Every setting lives in one file whose path is given on the command line (``--config``). Any value can be
overridden by an environment variable named ``CERP__<SECTION>__<KEY>`` (double underscores between levels,
case-insensitive), whose value is parsed as YAML so ``CERP__HTTP__PORT=8000`` is an integer.

Invalid configuration stops startup with *every* error listed (:class:`ConfigError`). Sections marked
reloadable (``limits``, ``exposure``, ``kill_switches``) can be replaced at runtime by
:meth:`ConfigStore.reload`; a reload that changes anything else is refused, because bind addresses, auth
and data paths only take effect on restart.

Secrets never appear in this file (MPC001-056): a static development token is named by the environment
variable that holds it (``token_env``), not written here.
"""

from __future__ import annotations

import ipaddress
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

ENV_PREFIX = "CERP__"
RELOADABLE_SECTIONS = frozenset({"limits", "exposure", "kill_switches"})

DEFAULT_INSTRUCTIONS = (
    "cerp gives access to a small business's webos ERP data (sales, support tickets, projects, inventory, "
    "cash flow). Records are identified by handles of the form <module>:<model>:<id>. Use the describe tools "
    "to learn a model's fields before querying it."
)


class ConfigError(Exception):
    """Configuration is invalid. ``errors`` lists every problem as ``location: message``."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("invalid configuration:\n  " + "\n  ".join(errors))


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerSection(_Section):
    name: str = "cerp"
    title: str = "cerp (Claude ERP)"
    instructions: str = DEFAULT_INSTRUCTIONS
    # MPC001-013: the protocol revisions this deployment accepts; must be a subset of what the SDK supports.
    protocol_versions: list[str] = Field(default_factory=lambda: ["2025-06-18", "2025-11-25", "2026-07-28"])
    # MPC001-015: whether MCP discovery needs credentials when auth is enabled.
    discovery_requires_auth: bool = True
    # MPC001-115: poll the config and exposure files and reload on change (0 = off; SIGHUP still works).
    reload_poll_seconds: float = Field(default=5.0, ge=0)


class HttpSection(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=7411, ge=1, le=65535)
    path: str = "/mcp"
    stateless: bool = True  # MPC001-014
    max_request_body_bytes: int = Field(default=1024 * 1024, ge=1024)  # MPC001-010

    @field_validator("path")
    @classmethod
    def _path_shape(cls, v: str) -> str:
        if not v.startswith("/") or v.endswith("/") or len(v) < 2:  # noqa: PLR2004 - "/x" is the shortest valid path
            raise ValueError("must start with '/', not end with '/', and not be '/' alone")
        return v

    @property
    def is_loopback(self) -> bool:
        if self.host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False


class StaticToken(_Section):
    token_env: str = Field(description="Environment variable holding the token value")
    user: str = Field(min_length=1, description="webos user the token acts as")


class StaticTokens(_Section):
    enabled: bool = False
    tokens: list[StaticToken] = Field(default_factory=list)


ALLOWED_JWT_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "ES256", "ES384")


class Introspection(_Section):
    """RFC 7662 introspection for opaque tokens (MPC001-057). Credentials come from environment variables."""

    url: str
    client_id_env: str | None = None
    client_secret_env: str | None = None
    cache_seconds: int = Field(default=300, ge=0, le=3600)


class OAuth(_Section):
    """This server as an OAuth 2.1 resource server (MPC001-057)."""

    enabled: bool = False
    issuer: str | None = Field(default=None, description="Expected `iss`; also advertised in metadata")
    audience: str | None = Field(
        default=None, description="This server's canonical resource URI; default http://host:port/path"
    )
    jwks_url: str | None = None
    algorithms: list[str] = Field(default_factory=lambda: ["RS256", "ES256"])
    leeway_seconds: int = Field(default=60, ge=0, le=300)
    required_scopes: list[str] = Field(default_factory=list)
    jwks_refresh_seconds: int = Field(default=60, ge=10)
    introspection: Introspection | None = None
    enforce_scopes: bool = Field(
        default=True, description="Require webos.<module>.read/write scopes (MPC001-077)"
    )

    @field_validator("algorithms")
    @classmethod
    def _asymmetric_only(cls, v: list[str]) -> list[str]:
        bad = [a for a in v if a not in ALLOWED_JWT_ALGORITHMS]
        if bad or not v:
            raise ValueError(
                f"only asymmetric algorithms are allowed ({', '.join(ALLOWED_JWT_ALGORITHMS)}); "
                f"got {bad or v}"
            )
        return v

    @model_validator(mode="after")
    def _complete(self) -> OAuth:
        if self.enabled:
            if not self.issuer:
                raise ValueError("auth.oauth.issuer is required when OAuth is enabled")
            if not self.jwks_url and self.introspection is None:
                raise ValueError("auth.oauth needs jwks_url, introspection, or both")
        return self


class PrincipalMap(_Section):
    """One OAuth principal (issuer + subject) mapped to one webos user (MPC001-060)."""

    issuer: str = Field(min_length=1, description="Token issuer (the `iss` claim)")
    subject: str = Field(min_length=1, description="Token subject (the `sub` claim)")
    user: str = Field(min_length=1, description="webos user name in security.users")


class AuthSection(_Section):
    static_tokens: StaticTokens = StaticTokens()
    oauth: OAuth = OAuth()
    # MPC001-060: OAuth principals -> webos users. Static tokens name their user directly.
    principals: list[PrincipalMap] = Field(default_factory=list)
    # MPC001-168 (interim): the webos user a local stdio client acts as. Refused with HTTP authentication,
    # and the HTTP app refuses to start while it is set, so it can never apply to a network caller.
    local_user: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _one_method(self) -> AuthSection:
        if self.static_tokens.enabled and self.oauth.enabled:
            raise ValueError("auth: enable static_tokens or oauth, not both")
        if self.local_user is not None and (self.static_tokens.enabled or self.oauth.enabled):
            raise ValueError("auth.local_user is for stdio only and cannot be combined with HTTP auth")
        seen: set[tuple[str, str]] = set()
        for p in self.principals:
            if (p.issuer, p.subject) in seen:
                raise ValueError(f"auth.principals: {p.issuer} {p.subject} is mapped twice")
            seen.add((p.issuer, p.subject))
        return self


class FeatureSection(_Section):
    resources: bool = False
    prompts: bool = False
    tasks: bool = False  # MPC001-026 long-running tasks; refused until the task store exists


class DataSection(_Section):
    # MPC001-200: the webos datasource file; with it, every database path comes from the datasource locator.
    datasources_file: Path | None = None
    root: Path | None = None  # application root for relative paths in the datasource file; default: cwd
    bootstrap_from_templates: bool = True
    # False: datasources that cannot be resolved, or are missing with bootstrap off, are unavailable
    # instead of fatal; startup still fails if an exposed model or a tool pack needs one (MPC001-200).
    strict_datasources: bool = True
    dbroot: Path | None = None  # overrides $(DBROOT), e.g. for a test tenant
    state_dir: Path | None = Field(default=None, description="cerp's own SQLite stores (MPC001-166)")
    # datasource name -> database file; relative paths are under dbroot. Overrides the registry's map.
    datasources: dict[str, Path] = Field(default_factory=dict)
    busy_timeout_ms: int = Field(default=5000, ge=0)
    foreign_keys: bool = False
    write_budget_ms: int = Field(default=200, ge=1)


class RegistrySection(_Section):
    path: Path | None = None


class ExposureSection(_Section):
    path: Path | None = None


class LimitsSection(_Section):
    max_page_size: int = Field(default=100, ge=1, le=1000)
    validation_budget_ms: int = Field(default=50, ge=1)
    # MPC001-035 request limits (reloadable). Per principal = per bearer token; anonymous = per client IP.
    max_header_bytes: int = Field(default=16 * 1024, ge=1024)
    request_timeout_s: float = Field(default=30.0, gt=0)
    concurrent_per_principal: int = Field(default=5, ge=1)
    rate_per_second: float = Field(default=10.0, gt=0)
    burst: int = Field(default=20, ge=1)
    anonymous_concurrent: int = Field(default=2, ge=1)
    anonymous_rate_per_second: float = Field(default=2.0, gt=0)
    anonymous_burst: int = Field(default=5, ge=1)


ToolPack = Literal["pm", "crm", "personal", "pm_write", "personal_write"]


class ToolsSection(_Section):
    # MPC001-088: curated tool packs to register; the *_write packs add write tools (decision 0003).
    # They need a registry and the security database.
    packs: list[ToolPack] = Field(default_factory=list)
    cursor_ttl_seconds: int = Field(default=900, ge=60, le=86400)  # MPC001-023


class LoggingSection(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    file: Path | None = None
    max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    backups: int = Field(default=5, ge=0)
    audit_file: Path | None = Field(default=None, description="JSON-lines audit trail (MPC001-110)")


class Settings(_Section):
    server: ServerSection = ServerSection()
    http: HttpSection = HttpSection()
    auth: AuthSection = AuthSection()
    features: FeatureSection = FeatureSection()
    data: DataSection = DataSection()
    registry: RegistrySection = RegistrySection()
    exposure: ExposureSection = ExposureSection()
    limits: LimitsSection = LimitsSection()
    kill_switches: dict[str, bool] = Field(default_factory=dict)
    tools: ToolsSection = ToolsSection()
    logging: LoggingSection = LoggingSection()

    @model_validator(mode="after")
    def _static_tokens_on_loopback_only(self) -> Settings:
        # MPC001-059: static tokens are a development aid and never leave the machine.
        if self.auth.static_tokens.enabled and not self.http.is_loopback:
            raise ValueError(
                f"auth.static_tokens.enabled requires a loopback bind; http.host is {self.http.host!r}"
            )
        return self


def _format_errors(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        out.append(f"{loc}: {err['msg']}")
    return out


def _env_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for key, raw in environ.items():
        if not key.upper().startswith(ENV_PREFIX):
            continue
        parts = [p.lower() for p in key[len(ENV_PREFIX) :].split("__") if p]
        if not parts:
            continue
        node = tree
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                child = node[part] = {}
            node = child
        node[parts[-1]] = yaml.safe_load(raw) if raw != "" else ""
    return tree


def _merge(base: dict[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(path: Path | None, environ: Mapping[str, str] | None = None) -> Settings:
    """Read, merge and validate configuration. Raises :class:`ConfigError` listing every problem."""
    raw: Any = {}
    if path is not None:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise ConfigError([f"{path}: cannot read ({exc.strerror})"]) from None
        except yaml.YAMLError as exc:
            raise ConfigError([f"{path}: not valid YAML ({exc})"]) from None
        if not isinstance(raw, dict):
            raise ConfigError([f"{path}: top level must be a mapping"])
    merged = _merge(raw, _env_overrides(os.environ if environ is None else environ))
    try:
        settings = Settings.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc)) from None
    if path is not None:
        settings = _resolve_paths(settings, path.parent)
    return settings


def _resolve_paths(s: Settings, base: Path) -> Settings:
    """Make relative paths in the file relative to the file's own directory."""

    def fix(p: Path | None) -> Path | None:
        return None if p is None or p.is_absolute() else base / p

    return s.model_copy(
        update={
            "data": s.data.model_copy(
                update={
                    "dbroot": fix(s.data.dbroot) or s.data.dbroot,
                    "datasources_file": fix(s.data.datasources_file) or s.data.datasources_file,
                    "root": fix(s.data.root) or s.data.root,
                    "state_dir": fix(s.data.state_dir) or s.data.state_dir,
                }
            ),
            "registry": s.registry.model_copy(update={"path": fix(s.registry.path) or s.registry.path}),
            "exposure": s.exposure.model_copy(update={"path": fix(s.exposure.path) or s.exposure.path}),
            "logging": s.logging.model_copy(
                update={
                    "file": fix(s.logging.file) or s.logging.file,
                    "audit_file": fix(s.logging.audit_file) or s.logging.audit_file,
                }
            ),
        }
    )


class ConfigStore:
    """Holds the live settings. Readers take :attr:`current`; :meth:`reload` swaps reloadable sections."""

    def __init__(self, settings: Settings, path: Path | None = None) -> None:
        self._settings = settings
        self._path = path
        self._lock = threading.Lock()

    @property
    def current(self) -> Settings:
        return self._settings

    def reload(self, environ: Mapping[str, str] | None = None) -> list[str]:
        """Re-read the file and apply reloadable sections. Returns the sections that changed.

        Raises :class:`ConfigError` if the new file is invalid or changes a section that needs a restart;
        in both cases the running settings are left untouched.
        """
        new = load_settings(self._path, environ)
        with self._lock:
            old = self._settings
            changed = [name for name in Settings.model_fields if getattr(old, name) != getattr(new, name)]
            fixed = sorted(set(changed) - RELOADABLE_SECTIONS)
            if fixed:
                raise ConfigError([f"{name}: changes need a restart" for name in fixed])
            self._settings = new
        return sorted(changed)
