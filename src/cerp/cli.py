"""Command line entry point: ``cerp``."""

import argparse
import ipaddress
import sys
from collections.abc import Sequence

import uvicorn

from cerp import __version__
from cerp.app.server import build_server

DEFAULT_PORT = 7411


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _cmd_stdio(_: argparse.Namespace) -> int:
    build_server().run("stdio")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    # MPC001-033/034: loopback by default; anything else needs TLS, which is not implemented yet.
    if not _is_loopback(args.host):
        print(
            f"cerp: refusing to bind {args.host}: only loopback is supported until TLS lands (MPC001-034)",
            file=sys.stderr,
        )
        return 2
    app = build_server().streamable_http_app(stateless_http=True, host=args.host)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cerp", description="cerp (Claude ERP) MCP server")
    parser.add_argument("--version", action="version", version=f"cerp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_stdio = sub.add_parser("stdio", help="serve MCP over stdio for a local client (MPC001-168)")
    p_stdio.set_defaults(func=_cmd_stdio)

    p_serve = sub.add_parser("serve", help="serve MCP over Streamable HTTP (MPC001-030)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_serve.set_defaults(func=_cmd_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result
