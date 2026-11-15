import argparse
import asyncio
import math
import threading
import weakref

from mcp.server.fastmcp import Context, FastMCP

from board import Board, default_db_path


def create_server(board, host="127.0.0.1", port=8001):
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
    parser = argparse.ArgumentParser(description="Run the TalkativeLM MCP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--transport", choices=("streamable-http", "stdio"), default="streamable-http")
    args = parser.parse_args()
    create_server(Board(default_db_path()), host=args.host, port=args.port).run(transport=args.transport)
