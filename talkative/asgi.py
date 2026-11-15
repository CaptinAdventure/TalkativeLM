import contextlib
import logging
import os
from urllib.parse import urlsplit

import anyio
from asgiref.sync import sync_to_async
from django.core.asgi import get_asgi_application
from django.http.request import split_domain_port, validate_host

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "talkative.settings")
django_app = get_asgi_application()

from django.conf import settings
from django_mcp import mcp_app, mount_mcp_server
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from . import mcp
from .board import get_board

django_mcp_app = mount_mcp_server(django_app, mcp_base_path="/mcp")
mcp_app.streamable_http_app()
logger = logging.getLogger(__name__)


def _is_closed_stream(exc):
    if isinstance(exc, (anyio.ClosedResourceError, anyio.BrokenResourceError)):
        return True
    children = getattr(exc, "exceptions", None)
    return bool(children) and all(_is_closed_stream(child) for child in children)


def _drop_closed_router_errors(record):
    # The SDK logs its own handled ClosedResourceError whenever a client ends a session.
    exc = record.exc_info[1] if record.exc_info else None
    return not (exc is not None and _is_closed_stream(exc) and record.getMessage().startswith("Error in message router"))


logging.getLogger("mcp.server.streamable_http").addFilter(_drop_closed_router_errors)


class _IsolatedTaskGroup:
    """Keeps one failing MCP session from cancelling the SDK's shared task group.

    mcp 1.9.x (pinned by django-mcp) runs every Streamable HTTP session in one task
    group, so a session error such as a client closing its session would otherwise
    shut down /mcp for every client until restart.
    """

    def __init__(self, task_group):
        self._task_group = task_group

    def __getattr__(self, name):
        return getattr(self._task_group, name)

    async def start(self, func, *args, name=None):
        async def guarded(*, task_status=anyio.TASK_STATUS_IGNORED):
            try:
                await func(*args, task_status=task_status)
            except Exception as exc:
                if _is_closed_stream(exc):
                    logger.debug("MCP session closed by client")
                else:
                    logger.exception("MCP session failed")

        return await self._task_group.start(guarded, name=name)


@contextlib.asynccontextmanager
async def lifespan(app):
    # MCP sessions live in memory, so no agent is connected right after a (re)start.
    await sync_to_async(get_board().set_offline, thread_sensitive=False)()
    manager = mcp_app.session_manager
    async with manager.run():
        manager._task_group = _IsolatedTaskGroup(manager._task_group)
        yield


class StreamableEndpoint:
    async def __call__(self, scope, receive, send):
        manager = mcp_app.session_manager
        session_id = Headers(scope=scope).get("mcp-session-id")
        if session_id is not None and session_id not in manager._server_instances:
            # The spec requires 404 for unknown sessions (e.g. after a server restart) so
            # clients re-initialize; mcp 1.9.x answers 400, which clients never recover from.
            response = JSONResponse(
                {"jsonrpc": "2.0", "id": "server-error", "error": {"code": -32600, "message": "Session not found"}},
                status_code=404,
            )
            await response(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)


app = Starlette(
    routes=[Route("/mcp", endpoint=StreamableEndpoint()), Mount("/", app=django_mcp_app)],
    lifespan=lifespan,
)


async def application(scope, receive, send):
    if scope["type"] == "http":
        hosts = [v.decode("latin-1") for k, v in scope["headers"] if k.lower() == b"host"]
        origins = [v.decode("latin-1") for k, v in scope["headers"] if k.lower() == b"origin"]
        domain, _ = split_domain_port(hosts[0]) if len(hosts) == 1 else ("", "")
        if not domain or not validate_host(domain, settings.ALLOWED_HOSTS):
            await PlainTextResponse("Invalid Host header", status_code=421)(scope, receive, send)
            return
        if origins:
            try:
                origin = urlsplit(origins[0])
                valid_origin = (
                    len(origins) == 1 and origin.scheme == scope["scheme"]
                    and origin.netloc.lower() == hosts[0].lower()
                    and not origin.path and not origin.query and not origin.fragment
                )
            except ValueError:
                valid_origin = False
            if not valid_origin:
                await PlainTextResponse("Invalid Origin header", status_code=403)(scope, receive, send)
                return
    await app(scope, receive, send)
