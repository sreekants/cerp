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
from mcp.server.mcpserver import MCPServer
from pydantic import ValidationError

from cerp import __version__
from cerp.app.asgi import build_app
from cerp.app.catalogue import build_catalogue, to_json, to_markdown
from cerp.app.config import ConfigError, ConfigStore, Settings, load_settings
from cerp.app.logs import configure_audit, configure_logging
from cerp.app.server import Runtime, StartupError, build_runtime, build_server, reload_exposure
from cerp.app.watch import FileWatcher
from cerp.auth.static import StaticTokenError, is_loopback
from cerp.data.exposure import ExposureError
from cerp.data.registry import RegistryError
from cerp.data.stores import STORES, MigrationError, migrate_all

EXIT_STARTUP = 2
GRACEFUL_SECONDS = 30


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
    configure_audit(settings.logging.audit_file)
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
    configure_audit(settings.logging.audit_file)
    store = ConfigStore(settings, args.config)
    app, mcp, rt = build_app(settings, store=store)
    watcher: FileWatcher | None = None
    if args.config is not None:
        config_path: Path = args.config

        def reload() -> None:
            _reload(store, mcp, rt)

        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, lambda *_: reload())
        if settings.server.reload_poll_seconds > 0:

            def watched() -> list[Path]:
                exp = store.current.exposure.path
                return [config_path] + ([exp] if exp is not None else [])

            watcher = FileWatcher(watched, settings.server.reload_poll_seconds, reload).start()
    # MPC001-117: on SIGINT/SIGTERM uvicorn stops accepting requests and lets in-flight calls finish for up to
    # GRACEFUL_SECONDS; write transactions are synchronous `with` blocks, so none is left half-done.
    try:
        uvicorn.run(
            app,
            host=settings.http.host,
            port=settings.http.port,
            log_config=None,
            timeout_graceful_shutdown=GRACEFUL_SECONDS,
        )
    finally:
        if watcher is not None:
            watcher.stop()
        if rt.databases is not None:
            rt.databases.close_all()
        logging.getLogger("cerp.app").info("cerp stopped")
    return 0


def _reload(store: ConfigStore, mcp: MCPServer, rt: Runtime) -> None:
    """Reload reloadable settings, then the exposure (its file may change without the config changing)."""
    log = logging.getLogger("cerp.app")
    try:
        changed = store.reload()
    except ConfigError as exc:
        log.warning("configuration reload refused: %s", "; ".join(exc.errors))
        return
    log.info("configuration reloaded: %s", ", ".join(changed) or "no changes")
    try:
        reload_exposure(mcp, rt)
    except ExposureError as exc:
        log.warning("exposure reload refused, tools unchanged: %s", "; ".join(exc.errors))


def _cmd_config_check(args: argparse.Namespace) -> int:
    settings = _settings(args)
    configure_logging(settings.logging.level, None)
    _, rt = build_runtime(settings)
    print(f"configuration OK: {len(rt.tool_names)} tools")
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    settings = _settings(args)
    configure_logging(settings.logging.level, None)
    if settings.data.state_dir is None:
        print("cerp: data.state_dir is not set; nothing to migrate", file=sys.stderr)
        return EXIT_STARTUP
    try:
        results = migrate_all(STORES, settings.data.state_dir)
    except MigrationError as exc:
        print(f"cerp: {exc}", file=sys.stderr)
        return EXIT_STARTUP
    for r in results:
        print(
            f"{r.store}: version {r.from_version} -> {r.to_version}"
            + (f" (backup {r.backup})" if r.backup else "")
        )
    return 0


def _cmd_catalogue(args: argparse.Namespace) -> int:
    settings = _settings(args)
    configure_logging("WARNING", None)
    mcp, _ = build_runtime(settings)
    cat = build_catalogue(mcp)
    text = to_markdown(cat) if args.format == "md" else to_json(cat)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
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

    p_cat = with_config(sub.add_parser("catalogue", help="write the tool catalogue (MPC001-143)"))
    p_cat.add_argument("--format", choices=["json", "md"], default="json")
    p_cat.add_argument("--out", type=Path)
    p_cat.set_defaults(func=_cmd_catalogue)

    p_mig = with_config(sub.add_parser("migrate", help="migrate cerp's own stores (MPC001-166)"))
    p_mig.set_defaults(func=_cmd_migrate)

    p_reg = sub.add_parser("registry", help="model registry commands")
    rsub = p_reg.add_subparsers(dest="registry_command", required=True)
    rsub.add_parser("generate", help="regenerate the webos model registry (see: cerp registry generate -h)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:2] == ["registry", "generate"]:  # the generator owns its own arguments and --help
        from cerp.registry.generate import main as generate  # noqa: PLC0415 - the generator is a developer tool, loaded only when asked

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
