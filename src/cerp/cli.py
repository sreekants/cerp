"""Command line entry point: ``cerp``.

    cerp serve  [--config FILE] [--host H] [--port P]   Streamable HTTP (MPC001-030)
    cerp stdio  [--config FILE]                         stdio for a local client (MPC001-168)
    cerp config check --config FILE                     validate configuration and exit
    cerp registry generate --portal … --out …           regenerate the model registry (MPC002-006)

Startup problems (bad configuration, registry or exposure) print every error and exit with status 2.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from cerp import __version__
from cerp.app.asgi import build_app
from cerp.app.config import ConfigError, ConfigStore, Settings, load_settings
from cerp.app.logs import configure_logging
from cerp.app.server import StartupError, build_runtime, build_server
from cerp.auth.static import StaticTokenError, is_loopback
from cerp.data.exposure import ExposureError
from cerp.data.registry import RegistryError

EXIT_STARTUP = 2


def _settings(args: argparse.Namespace) -> Settings:
    settings = load_settings(args.config)
    overrides = {
        k: v
        for k, v in (("host", getattr(args, "host", None)), ("port", getattr(args, "port", None)))
        if v is not None
    }
    if overrides:
        try:
            settings = Settings.model_validate(
                settings.model_dump() | {"http": settings.http.model_dump() | overrides}
            )
        except ValidationError as exc:
            raise ConfigError(
                [f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}" for e in exc.errors()]
            ) from None
    return settings


def _cmd_stdio(args: argparse.Namespace) -> int:
    settings = _settings(args)
    configure_logging(settings.logging.level, settings.logging.file)  # stderr: stdout carries the protocol
    build_server(settings).run("stdio")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    settings = _settings(args)
    # MPC001-033/034: loopback only until TLS and OAuth land.
    if not is_loopback(settings.http.host):
        print(
            f"cerp: refusing to bind {settings.http.host}: only loopback is supported until TLS lands "
            "(MPC001-034)",
            file=sys.stderr,
        )
        return EXIT_STARTUP
    configure_logging(
        settings.logging.level,
        settings.logging.file,
        max_bytes=settings.logging.max_bytes,
        backups=settings.logging.backups,
    )
    store = ConfigStore(settings, args.config)
    if args.config is not None and hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: _reload(store))
    app, _, _ = build_app(settings)
    uvicorn.run(app, host=settings.http.host, port=settings.http.port, log_config=None)
    return 0


def _reload(store: ConfigStore) -> None:
    log = logging.getLogger("cerp.app")
    try:
        changed = store.reload()
    except ConfigError as exc:
        log.warning("configuration reload refused: %s", "; ".join(exc.errors))
    else:
        log.info("configuration reloaded: %s", ", ".join(changed) or "no changes")


def _cmd_config_check(args: argparse.Namespace) -> int:
    settings = _settings(args)
    configure_logging(settings.logging.level, None)
    _, rt = build_runtime(settings)
    print(f"configuration OK: {len(rt.tool_names)} tools")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cerp", description="cerp (Claude ERP) MCP server")
    parser.add_argument("--version", action="version", version=f"cerp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_config(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--config", type=Path, help="YAML configuration file (MPC001-115)")
        return p

    p_stdio = with_config(sub.add_parser("stdio", help="serve MCP over stdio for a local client"))
    p_stdio.set_defaults(func=_cmd_stdio)

    p_serve = with_config(sub.add_parser("serve", help="serve MCP over Streamable HTTP"))
    p_serve.add_argument("--host", help="bind address (default from config, else 127.0.0.1)")
    p_serve.add_argument("--port", type=int, help="port (default from config, else 7411)")
    p_serve.set_defaults(func=_cmd_serve)

    p_config = sub.add_parser("config", help="configuration commands")
    csub = p_config.add_subparsers(dest="config_command", required=True)
    p_check = with_config(csub.add_parser("check", help="validate configuration, registry and exposure"))
    p_check.set_defaults(func=_cmd_config_check)

    p_reg = sub.add_parser("registry", help="model registry commands")
    rsub = p_reg.add_subparsers(dest="registry_command", required=True)
    rsub.add_parser("generate", help="regenerate the webos model registry (see: cerp registry generate -h)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:2] == ["registry", "generate"]:  # the generator owns its own arguments and --help
        from cerp.registry.generate import main as generate  # noqa: PLC0415

        return generate(argv[2:])
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
    except ConfigError as exc:
        print(f"cerp: {exc}", file=sys.stderr)
        return EXIT_STARTUP
    except (StartupError, StaticTokenError, RegistryError, ExposureError) as exc:
        print(f"cerp: {exc}", file=sys.stderr)
        return EXIT_STARTUP
    return result
