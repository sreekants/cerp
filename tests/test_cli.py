import pytest

from cerp import __version__
from cerp.cli import build_parser, main


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_serve_refuses_non_loopback() -> None:
    assert main(["serve", "--host", "0.0.0.0"]) == 2  # noqa: S104
