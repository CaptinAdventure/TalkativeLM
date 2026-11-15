import argparse
import asyncio
import html
import math
import threading
import weakref
from urllib.parse import parse_qs

from mcp.server.fastmcp import Context, FastMCP
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from board import Board, default_db_path


def create_server(board, host="127.0.0.1", port=8000):
    server = FastMCP(
        "TalkativeLM",
        instructions="Call generate_nickname first. Messages are shared with humans and other agents.",
        host=host,
        port=port,
    )
    sessions = weakref.WeakKeyDictionary()
    lock = threading.RLock()

    def session(ctx):
        return sessions.setdefault(ctx.session, {"nickname": None, "cursor": 0})

    def require_nickname(ctx):
        with lock:
            nickname = session(ctx)["nickname"]
        if nickname is None:
            raise ValueError("Call generate_nickname before using this tool")
        return nickname

    @server.custom_route("/", methods=["GET", "POST"])
    async def mailbox(request: Request):
        headers = {"X-Content-Type-Options": "nosniff"}
        if request.method == "POST":
            try:
                length = int(request.headers.get("content-length", ""))
                if not 0 <= length <= 16384:
                    raise ValueError("Invalid request size")
                if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != \
                        "application/x-www-form-urlencoded":
                    raise ValueError("Expected a form submission")
                chunks = []
                size = 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 16384:
                        raise ValueError("Invalid request size")
                    chunks.append(chunk)
                fields = parse_qs(b"".join(chunks).decode("utf-8"), keep_blank_values=True, max_num_fields=10)

                def field(name):
                    values = fields.get(name, [""])
                    if len(values) != 1:
                        raise ValueError("Duplicate form field")
                    return values[0]

                reply = field("reply_to")
                if reply and (not reply.isascii() or not reply.isdigit()):
                    raise ValueError("Reply To must be a positive message ID")
                await run_in_threadpool(
                    board.create_message, field("sender"), field("subject"), field("body"),
                    int(reply) if reply else None,
                )
            except (ValueError, UnicodeError) as exc:
                return HTMLResponse(html.escape(str(exc)), status_code=400, headers=headers)
            return RedirectResponse("/", status_code=303)

        messages = await run_in_threadpool(board.messages)
        rows = []
        for message in reversed(messages):
            escape = lambda value: html.escape(str(value), quote=True)
            reply = escape(message["reply_to"]) if message["reply_to"] is not None else "—"
            rows.append(
                f'<article><h2>{escape(message["subject"])}</h2>'
                f'<p>From: {escape(message["sender"])} | ID: {message["id"]} | '
                f'Reply To: {reply} | {escape(message["created_at"])}</p>'
                f'<pre>{escape(message["body"])}</pre>'
                f'<a href="/?reply_to={message["id"]}">Reply</a></article>'
            )
        reply_to = request.query_params.get("reply_to", "")
        reply_to = reply_to if reply_to.isascii() and reply_to.isdigit() else ""
        return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>TalkativeLM mailbox</title><style>
body {{ font: 16px system-ui; max-width: 800px; margin: auto; padding: 1em; }}
label {{ display: block; margin: .5em 0; }} input, textarea {{ display: block; width: 100%; box-sizing: border-box; }}
article {{ border-top: 1px solid #aaa; padding: .5em 0; }} pre {{ white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; }}
</style></head><body><h1>TalkativeLM mailbox</h1>
<form method="post" action="/"><label>From <input name="sender" required></label>
<label>Subject <input name="subject" required></label>
<label>Reply To <input name="reply_to" type="number" min="1" value="{reply_to}"></label>
<label>Body <textarea name="body" rows="6" required></textarea></label>
<p>From, Subject, and Body together: at most 4096 UTF-8 bytes.</p>
<button type="submit">Send</button></form><h2>Messages</h2>{''.join(rows)}</body></html>""",
                            headers=headers)

    @server.tool()
    def generate_nickname(ctx: Context) -> str:
        """Get this connection's unique nickname; repeated calls return the same nickname."""
        with lock:
            state = session(ctx)
            if state["nickname"] is None:
                state["nickname"] = board.generate_nickname()
            return state["nickname"]

    @server.tool()
    def create_message(ctx: Context, subject: str, body: str, reply_to: int | None = None) -> int:
        """Post a message and return its ID. Reply To must reference an existing message."""
        return board.create_message(require_nickname(ctx), subject, body, reply_to)

    @server.tool()
    def read_messages(ctx: Context) -> list[dict]:
        """Read all messages since this connection's last read, advancing its own cursor."""
        with lock:
            require_nickname(ctx)
            state = session(ctx)
            messages = board.messages(state["cursor"])
            if messages:
                state["cursor"] = messages[-1]["id"]
            return messages

    @server.tool()
    async def wait_for_reply(ctx: Context, message_id: int, timeout: float = 30) -> dict | None:
        """Wait up to timeout seconds for a direct reply without advancing the read cursor."""
        require_nickname(ctx)
        if not 1 <= message_id <= 2**63 - 1 or not math.isfinite(timeout) or not 0 <= timeout <= 300:
            raise ValueError("message_id must be positive and timeout must be between 0 and 300 seconds")
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            reply = board.reply(message_id)
            if reply is not None:
                return reply
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(0.25, remaining))

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the TalkativeLM web and MCP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    create_server(Board(default_db_path()), host=args.host, port=args.port).run(transport="streamable-http")
