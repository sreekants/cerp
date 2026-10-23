"""The ASGI application served by uvicorn (MPC001-030, MPC001-014).

The SDK's Streamable HTTP app is mounted at the configured path (default ``/mcp``) in stateless mode, so no
client state lives in server memory between requests. Next to it: ``/healthz`` (liveness) and, when auth is
enabled, the OAuth protected-resource metadata the SDK adds. Nothing else is exposed; the route list is
logged at startup.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cerp import __version__
from cerp.app.config import Settings
from cerp.app.server import Runtime, build_runtime

log = logging.getLogger("cerp.app")


async def _healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "version": __version__})


def _route_label(r: BaseRoute) -> str:
    path = getattr(r, "path", "?")
    methods = getattr(r, "methods", None)
    return f"{path} [{','.join(sorted(methods))}]" if methods else str(path)


class AllowOn405:
    """Add an ``Allow`` header to 405 responses that lack one (MPC001-030); the SDK omits it on some paths."""

    def __init__(self, app: ASGIApp, allow: str = "GET, POST") -> None:
        self.app = app
        self.allow = allow.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_allow(message: Message) -> None:
            if message["type"] == "http.response.start" and message.get("status") == 405:  # noqa: PLR2004
                headers = list(message.get("headers", []))
                if not any(k.lower() == b"allow" for k, _ in headers):
                    headers.append((b"allow", self.allow))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_allow)


def build_app(
    settings: Settings | None = None, environ: Mapping[str, str] | None = None
) -> tuple[Starlette, MCPServer, Runtime]:
    mcp, rt = build_runtime(settings, environ)
    s = rt.settings
    app = mcp.streamable_http_app(
        streamable_http_path=s.http.path,
        stateless_http=s.http.stateless,
        max_request_body_size=s.http.max_request_body_bytes,
        host=s.http.host,
    )
    app.router.routes.append(Route("/healthz", _healthz, methods=["GET"]))
    app.add_middleware(AllowOn405)
    log.info("routes: %s", "; ".join(_route_label(r) for r in app.router.routes))
    return app, mcp, rt
