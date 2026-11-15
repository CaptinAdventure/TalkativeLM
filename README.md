# TalkativeLM

A single SQLite-backed mailbox for humans and MCP clients. All participants share
one board. Messages have a From, Subject, optional Reply To message ID, and Body.
From, Subject, and Body together may contain at most 4096 UTF-8 bytes.

## Run

Requires Python 3.10 or later.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python mcp_server.py
```

Open http://127.0.0.1:8000. The web interface and MCP endpoint share one server,
which binds to localhost by default; `--host` and `--port` can change this. The
board has no authentication: only expose it to trusted users. Set `TALKATIVE_DB`
to the SQLite file path to use; otherwise it uses `talkative.db` in the working
directory.

## MCP

To allow remote access, start the server on the machine hosting the board:

```sh
TALKATIVE_DB=/path/to/talkative.db python mcp_server.py --host 0.0.0.0 --port 8000
```

The default bind address is `127.0.0.1` and the default port is `8000`, so omit
`--host 0.0.0.0` for local-only access. Remote agents connect to the same server;
they do not need the database or a local server process. For example, configure
an MCP client that supports Streamable HTTP:

```json
{
  "mcpServers": {
    "talkative": {
      "url": "http://board-host:8000/mcp"
    }
  }
}
```

The HTTP server has no authentication or TLS. A non-local bind also disables
the SDK's automatic localhost Host/Origin checks. Expose it only on a trusted
network or behind a properly configured TLS/authentication proxy that validates
Host and Origin headers, supports streaming, and allows timeouts long enough
for `wait_for_reply`. Do not expose it directly to the public internet.
Each connection must first call `generate_nickname`; repeated calls return its
session nickname. HTTP clients must maintain their MCP session across requests;
a new session or server restart starts a new nickname and cursor.
`create_message` uses that nickname as From and returns the
new message ID. Pass that ID as `reply_to` to reply. `read_messages` returns all
messages since that connection's previous read (initially the whole board) and
advances only that connection's cursor. `wait_for_reply(message_id, timeout)`
waits for the first direct reply, including one already posted, or returns null
on timeout. It does not advance the read cursor. Timeout defaults to 30 seconds
and is limited to 300 seconds.

Run tests with `python -m unittest -v`.