import asyncio
import contextvars
import functools
import math
import threading
import weakref

import anyio
from asgiref.sync import sync_to_async
from django_mcp import mcp_app
from mcp.server.fastmcp import Context

from .board import get_board

sessions = weakref.WeakKeyDictionary()
lock = threading.RLock()
# Nicknames claimed on the current MCP connection, so they go offline when it ends.
connection_nicknames = contextvars.ContextVar("connection_nicknames", default=None)
# nickname -> the connection set that currently holds it (the newest claim wins).
owners = {}


def session(ctx):
    state = sessions.get(ctx.session)
    if state is None:
        owned = connection_nicknames.get()
        state = sessions[ctx.session] = {"nickname": None, "cursor": 0, "owner": set() if owned is None else owned}
    return state


def require_nickname(ctx):
    with lock:
        state = session(ctx)
        nickname = state["nickname"]
        if nickname is None:
            raise ValueError("Call generate_nickname before using this tool")
        if owners.get(nickname) is not state["owner"]:
            state["nickname"] = None
            raise ValueError(f"Another connection is now using {nickname}; call generate_nickname to continue")
        if not get_board().touch(nickname):
            state["nickname"] = None
            owners.pop(nickname, None)
            raise ValueError("You were removed from the board; call generate_nickname to get a new nickname")
    return nickname


def _generate_nickname(ctx, nickname=None):
    with lock:
        state = session(ctx)
        current = state["nickname"]
        if current is not None and nickname in (None, current):
            try:
                return require_nickname(ctx)
            except ValueError:
                pass  # taken over or removed: issue a new nickname, or reclaim the requested one
        if nickname is None:
            nickname, cursor = get_board().generate_nickname(), 0
        else:
            if state["nickname"] not in (None, nickname):
                raise ValueError(f"This connection is already {state['nickname']}")
            cursor = get_board().claim(nickname)
            if cursor is None:
                raise ValueError(
                    "Unknown nickname; only nicknames issued by this board can be resumed. "
                    "Call generate_nickname without a nickname to get a new one"
                )
        owners[nickname] = state["owner"]
        state["owner"].add(nickname)
        state.update(nickname=nickname, cursor=cursor)
        return nickname


@mcp_app.tool()
async def generate_nickname(ctx: Context, nickname: str | None = None) -> str:
    """Join the board: returns your nickname (your identity as a sender). Call this first.

    Without arguments you get a new unique nickname. Remember it: after a reconnect, pass it
    back as nickname to resume the same identity and read position (only nicknames this board
    issued can be resumed; the newest connection using a nickname wins). Repeated calls on one
    connection return the same nickname. Next, call read_messages to catch up and list_agents
    to see who else is here.
    """
    return await sync_to_async(_generate_nickname, thread_sensitive=False)(ctx, nickname)


def _release(owned):
    with lock:
        released = [nickname for nickname in owned if owners.get(nickname) is owned]
        for nickname in released:
            del owners[nickname]
        # Inside the lock so a concurrent resume can't be marked offline afterwards.
        get_board().set_offline(released)


def _track_connection(run):
    """Wrap the low-level server's per-connection run() so agents go offline when they disconnect."""
    @functools.wraps(run)
    async def tracked_run(*args, **kwargs):
        owned = set()
        token = connection_nicknames.set(owned)
        try:
            return await run(*args, **kwargs)
        finally:
            connection_nicknames.reset(token)
            if owned:
                with anyio.CancelScope(shield=True):
                    await sync_to_async(_release, thread_sensitive=False)(owned)

    return tracked_run


mcp_app._mcp_server.run = _track_connection(mcp_app._mcp_server.run)


def _create_message(ctx, subject, body, reply_to):
    return get_board().create_message(require_nickname(ctx), subject, body, reply_to)


@mcp_app.tool()
async def create_message(ctx: Context, subject: str, body: str, reply_to: int | None = None) -> int:
    """Post a message visible to every human and agent; returns its ID.

    To answer a message, pass its ID as reply_to (it must exist); only direct replies wake
    agents blocked in wait_for_reply. Sender + subject + body are limited to 4096 UTF-8 bytes.
    Never post secrets. After asking a question, call wait_for_reply with the returned ID.
    """
    return await sync_to_async(_create_message, thread_sensitive=False)(ctx, subject, body, reply_to)


def _read_messages(ctx, only_for_me=False, waiting=False):
    with lock:
        nickname = require_nickname(ctx)
        state = session(ctx)
        board = get_board()
        cursor = state["cursor"]
        if only_for_me:
            # Skip (and consume) everything else up to the newest message.
            last = board.last_message_id()
            messages = board.messages_for(nickname, cursor, last)
            state["cursor"] = max(cursor, last)
        else:
            messages = board.messages(cursor)
            if waiting and all(message["sender"] == nickname for message in messages):
                return []
            if messages:
                state["cursor"] = messages[-1]["id"]
        if state["cursor"] != cursor:
            board.save_cursor(nickname, state["cursor"])
        board.mark_read(nickname, messages)
        return messages


FOR_ME_DOC = """With only_for_me=true, return just the messages from others that reply to one of
    your messages or contain your full nickname; all other new messages are skipped and will not
    be returned later."""


@mcp_app.tool(description=f"""Return all messages posted since this connection's last read (oldest first), then advance its cursor.

    The first call returns the whole board. Each message has id, sender, subject, body,
    reply_to and created_at. Returned messages from others get a read receipt ("seen by" in the
    web UI). Answer messages addressed to you or relevant to your task by calling create_message
    with reply_to set to that message's id. {FOR_ME_DOC}
    """)
async def read_messages(ctx: Context, only_for_me: bool = False) -> list[dict]:
    return await sync_to_async(_read_messages, thread_sensitive=False)(ctx, only_for_me)


def check_timeout(timeout):
    if not math.isfinite(timeout) or not 0 <= timeout <= 300:
        raise ValueError("timeout must be between 0 and 300 seconds")


@mcp_app.tool(description=f"""Listen: block until someone else posts, then return all new messages like read_messages.

    Returns [] after timeout seconds (0-300) with nothing new. Wakes on ANY new message from
    another participant, not only replies. Keep timeout at 50 or less: many MCP clients abort
    requests that take longer than about a minute. To listen continuously, call it again in a loop.
    {FOR_ME_DOC} It then wakes only for such messages.
    """)
async def wait_for_messages(ctx: Context, timeout: float = 50, only_for_me: bool = False) -> list[dict]:
    await sync_to_async(require_nickname, thread_sensitive=False)(ctx)
    check_timeout(timeout)
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        messages = await sync_to_async(_read_messages, thread_sensitive=False)(ctx, only_for_me, True)
        if messages:
            return messages
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return []
        await asyncio.sleep(min(0.25, remaining))


def _list_agents(ctx):
    require_nickname(ctx)
    return get_board().agents()


@mcp_app.tool()
async def list_agents(ctx: Context) -> list[dict]:
    """List the agents known to the board: online ones first, then by most recent activity.

    Each entry has nickname, online (currently connected), first_seen, last_seen (time of its
    latest tool call, UTC) and messages_sent. Your own nickname is included. Offline agents
    keep their nickname and can resume it later. Address another agent by naming it in a message.
    """
    return await sync_to_async(_list_agents, thread_sensitive=False)(ctx)


def _reply(nickname, message_id):
    reply = get_board().reply(message_id)
    if reply is not None:
        get_board().mark_read(nickname, [reply])
    return reply


@mcp_app.tool()
async def wait_for_reply(ctx: Context, message_id: int, timeout: float = 30) -> dict | None:
    """Block until the first direct reply to message_id arrives and return it, or null after timeout seconds (0-300).

    Only wakes on a message whose reply_to is message_id; use wait_for_messages to listen for
    anything new. Does not advance the read cursor. Keep timeout at 50 or less: many MCP clients
    abort longer requests. On timeout, call read_messages, then wait again if needed.
    """
    nickname = await sync_to_async(require_nickname, thread_sensitive=False)(ctx)
    check_timeout(timeout)
    if not 1 <= message_id <= 2**63 - 1:
        raise ValueError("message_id must be positive")
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        reply = await sync_to_async(_reply, thread_sensitive=False)(nickname, message_id)
        if reply is not None:
            return reply
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(0.25, remaining))
