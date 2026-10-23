from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cerp import __version__
from cerp.cli import EXIT_STARTUP, build_parser, main


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_serve_refuses_non_loopback() -> None:
    assert main(["serve", "--host", "0.0.0.0"]) == EXIT_STARTUP  # noqa: S104


def test_static_tokens_on_any_address_exit_non_zero(
    write_yaml: Callable[[str, Any], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_yaml(
        "c.yaml",
        {
            "http": {"host": "0.0.0.0"},  # noqa: S104
            "auth": {"static_tokens": {"enabled": True, "tokens": [{"token_env": "X", "user": "alice"}]}},
        },
    )
    assert main(["serve", "--config", str(cfg)]) == EXIT_STARTUP
    assert "loopback" in capsys.readouterr().err


def test_config_check_lists_errors(
    write_yaml: Callable[[str, Any], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_yaml("c.yaml", {"http": {"port": -1}, "limits": {"max_page_size": 0}})
    assert main(["config", "check", "--config", str(cfg)]) == EXIT_STARTUP
    err = capsys.readouterr().err
    assert "http.port" in err
    assert "limits.max_page_size" in err


def test_config_check_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "check"]) == 0
    assert "configuration OK" in capsys.readouterr().out
