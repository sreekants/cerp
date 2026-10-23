"""MPC001-115 configuration; MPC001-059 static-token bind rule."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cerp.app.config import ConfigError, ConfigStore, Settings, load_settings


def test_defaults_are_valid() -> None:
    s = load_settings(None, environ={})
    assert s.http.host == "127.0.0.1"
    assert s.http.path == "/mcp"
    assert s.http.stateless is True


def test_invalid_config_lists_every_error(write_yaml: Callable[[str, Any], Path]) -> None:
    path = write_yaml(
        "c.yaml",
        {"http": {"port": 0, "path": "mcp"}, "limits": {"max_page_size": 0}, "bogus": 1},
    )
    with pytest.raises(ConfigError) as exc:
        load_settings(path, environ={})
    text = "\n".join(exc.value.errors)
    for where in ("http.port", "http.path", "limits.max_page_size", "bogus"):
        assert where in text
    assert len(exc.value.errors) == 4


def test_environment_overrides_file(write_yaml: Callable[[str, Any], Path]) -> None:
    path = write_yaml("c.yaml", {"http": {"port": 8000}})
    s = load_settings(path, environ={"CERP__HTTP__PORT": "9001", "CERP__LIMITS__MAX_PAGE_SIZE": "20"})
    assert s.http.port == 9001
    assert s.limits.max_page_size == 20


def test_relative_paths_resolve_against_the_file(
    write_yaml: Callable[[str, Any], Path], tmp_path: Path
) -> None:
    path = write_yaml("c.yaml", {"registry": {"path": "reg/webos.yaml"}})
    assert load_settings(path, environ={}).registry.path == tmp_path / "reg" / "webos.yaml"


def test_static_tokens_refused_off_loopback() -> None:
    with pytest.raises(ValueError, match="loopback"):
        Settings.model_validate({"http": {"host": "0.0.0.0"}, "auth": {"static_tokens": {"enabled": True}}})  # noqa: S104


def test_reload_applies_reloadable_sections(write_yaml: Callable[[str, Any], Path]) -> None:
    path = write_yaml("c.yaml", {"limits": {"max_page_size": 50}})
    store = ConfigStore(load_settings(path, environ={}), path)
    write_yaml("c.yaml", {"limits": {"max_page_size": 10}, "kill_switches": {"im.create_ticket": True}})
    assert store.reload(environ={}) == ["kill_switches", "limits"]
    assert store.current.limits.max_page_size == 10


def test_reload_refuses_restart_only_sections(write_yaml: Callable[[str, Any], Path]) -> None:
    path = write_yaml("c.yaml", {"http": {"port": 7411}})
    store = ConfigStore(load_settings(path, environ={}), path)
    write_yaml("c.yaml", {"http": {"port": 7500}, "limits": {"max_page_size": 10}})
    with pytest.raises(ConfigError, match="http: changes need a restart"):
        store.reload(environ={})
    assert store.current.http.port == 7411
    assert store.current.limits.max_page_size == 100
