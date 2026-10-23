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


class HttpSection(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=7411, ge=1, le=65535)
    path: str = "/mcp"
    stateless: bool = True  # MPC001-014
    max_request_body_bytes: int = Field(default=4 * 1024 * 1024, ge=1024)

    @field_validator("path")
    @classmethod
    def _path_shape(cls, v: str) -> str:
        if not v.startswith("/") or v.endswith("/") or len(v) < 2:  # noqa: PLR2004
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


class AuthSection(_Section):
    static_tokens: StaticTokens = StaticTokens()


class FeatureSection(_Section):
    resources: bool = False
    prompts: bool = False


class DataSection(_Section):
    dbroot: Path | None = None
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


class LoggingSection(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    file: Path | None = None
    max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    backups: int = Field(default=5, ge=0)


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
            "data": s.data.model_copy(update={"dbroot": fix(s.data.dbroot) or s.data.dbroot}),
            "registry": s.registry.model_copy(update={"path": fix(s.registry.path) or s.registry.path}),
            "exposure": s.exposure.model_copy(update={"path": fix(s.exposure.path) or s.exposure.path}),
            "logging": s.logging.model_copy(update={"file": fix(s.logging.file) or s.logging.file}),
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
