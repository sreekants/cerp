"""The ASGI application served by uvicorn (MPC001-030, MPC001-014).

The SDK's Streamable HTTP app is mounted at the configured path (default ``/mcp``) in stateless mode, so no
client state lives in server memory between requests. In front of it, :class:`JsonLimits` refuses oversized
or malformed JSON (MPC001-010). Next to it: ``/healthz`` (liveness), ``/readyz`` (readiness, MPC001-037;
no authentication, no internal details) and, when auth is enabled, the OAuth protected-resource metadata the
SDK adds. Nothing else is exposed; the route list is logged at startup.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping

import anyio
import anyio.to_thread
from mcp.server.auth.routes import create_protected_resource_routes
from mcp.server.mcpserver import MCPServer
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cerp import __version__
from cerp.app.config import ConfigStore, LimitsSection, Settings
from cerp.app.errorbody import CorrelateErrors
from cerp.app.jsonlimits import JsonLimits
from cerp.app.limits import RequestLimits
from cerp.app.server import Runtime, build_runtime
from cerp.data.scopes import catalogue

log = logging.getLogger("cerp.app")


async def _healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "version": __version__})


def _readyz(rt: Runtime) -> Callable[[Request], Awaitable[JSONResponse]]:
    async def readyz(_: Request) -> JSONResponse:
        checks = await anyio.to_thread.run_sync(rt.readiness)
        failed = sorted(k for k, ok in checks.items() if not ok)
        if failed:
            log.warning("not ready: %s", ", ".join(failed))  # details go to the log, never to the caller
            return JSONResponse({"status": "not ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    return readyz


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
            if message["type"] == "http.response.start" and message.get("status") == 405:  # noqa: PLR2004 - HTTP status code
                headers = list(message.get("headers", []))
                if not any(k.lower() == b"allow" for k, _ in headers):
                    headers.append((b"allow", self.allow))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_allow)


def _resource_metadata(app: Starlette, rt: Runtime) -> None:
    """RFC 9728 metadata with the scope catalogue (MPC001-036, MPC001-077).

    The SDK publishes metadata only with its ``required_scopes`` (which every token would then need), so
    cerp replaces that route with one listing the whole catalogue, at the path-suffixed URL and the bare
    ``/.well-known/oauth-protected-resource``.
    """
    assert rt.oauth is not None  # noqa: S101 - checked by the caller
    oa = rt.settings.auth.oauth
    scopes = sorted({s.name for s in catalogue(rt.registry)} if rt.registry is not None else set()) + list(
        oa.required_scopes
    )
    routes = create_protected_resource_routes(
        resource_url=AnyHttpUrl(rt.oauth.config.audience),
        authorization_servers=[AnyHttpUrl(rt.oauth.config.issuer)],
        scopes_supported=sorted(set(scopes)) or None,
        resource_name=rt.settings.server.title,
    )
    paths = {getattr(r, "path", None) for r in routes}
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", None) not in paths]
    bare = "/.well-known/oauth-protected-resource"
    extra = [Route(bare, r.endpoint, methods=["GET", "OPTIONS"]) for r in routes if isinstance(r, Route)]
    app.router.routes[:0] = routes + [r for r in extra if r.path not in paths]


def build_app(
    settings: Settings | None = None,
    environ: Mapping[str, str] | None = None,
    store: ConfigStore | None = None,
) -> tuple[Starlette, MCPServer, Runtime]:
    mcp, rt = build_runtime(settings, environ, store)
    s = rt.settings
    app = mcp.streamable_http_app(
        streamable_http_path=s.http.path,
        stateless_http=s.http.stateless,
        max_request_body_size=s.http.max_request_body_bytes,
        host=s.http.host,
    )
    if rt.oauth is not None:
        _resource_metadata(app, rt)
    app.router.routes.append(Route("/healthz", _healthz, methods=["GET"]))
    app.router.routes.append(Route("/readyz", _readyz(rt), methods=["GET"]))
    # Outermost last: AllowOn405 → RequestLimits → CorrelateErrors → JsonLimits → SDK.
    app.add_middleware(JsonLimits, path=s.http.path, max_body=s.http.max_request_body_bytes)
    app.add_middleware(CorrelateErrors, path=s.http.path)
    store = rt.store

    def limits() -> LimitsSection:
        return store.current.limits if store is not None else s.limits

    auth_on = s.auth.static_tokens.enabled or s.auth.oauth.enabled
    app.add_middleware(RequestLimits, path=s.http.path, limits=limits, auth_enabled=auth_on)
    app.add_middleware(AllowOn405)
    if app.debug:  # MPC001-067: never serve tracebacks
        raise RuntimeError("Starlette debug mode must be off")
    log.info("routes: %s", "; ".join(_route_label(r) for r in app.router.routes))
    return app, mcp, rt
