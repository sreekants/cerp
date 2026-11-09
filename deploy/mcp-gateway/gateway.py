"""One process, one port: the four read-only prototype MCP servers behind a single Streamable HTTP endpoint.

    /mcp/im   /mcp/pm   /mcp/crm   /mcp/cfm      (each a full MCP endpoint)

Binds loopback only; nginx terminates TLS and checks the bearer token. Databases come from
MCP_DB_DIR (default: ./db next to this file), one <name>.s3db per server, opened read-only.

    uv run --directory <cerp> python deploy/mcp-gateway/gateway.py [--port 7410]
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import sys
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount

HERE = Path(__file__).resolve().parent
NAMES = ("im", "pm", "crm", "cfm")


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_server", HERE / f"{name}_server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def build(db_dir: Path, host: str):
    servers = {}
    for name in NAMES:
        db = db_dir / f"{name}.s3db"
        if not db.is_file():
            raise SystemExit(f"gateway: missing database {db}")
        mod = load(name)
        servers[name] = mod.build_server(mod.Database(db))
    apps = {n: s.streamable_http_app(streamable_http_path="/", host=host) for n, s in servers.items()}

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # Mounted apps don't run their own lifespan, so each server's session manager starts here.
        async with contextlib.AsyncExitStack() as stack:
            for s in servers.values():
                await stack.enter_async_context(s.session_manager.run())
            yield

    inner = Starlette(routes=[Mount(f"/mcp/{n}", app=a) for n, a in apps.items()], lifespan=lifespan)
    bare = {f"/mcp/{n}" for n in NAMES}

    async def app(scope, receive, send):
        # /mcp/im and /mcp/im/ are the same endpoint: no redirect, whose Location would name the loopback host.
        if scope["type"] == "http" and scope["path"] in bare:
            scope = dict(scope, path=scope["path"] + "/", raw_path=(scope["path"] + "/").encode())
        await inner(scope, receive, send)

    return app


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7410)
    p.add_argument("--db-dir", type=Path, default=Path(os.environ.get("MCP_DB_DIR", HERE / "db")))
    a = p.parse_args()
    uvicorn.run(build(a.db_dir, a.host), host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
