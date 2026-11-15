import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import patch

import django
import httpx
from django.test import Client, override_settings
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from board import Board

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "talkative.settings")
django.setup()
from talkative import mcp


def json_items(result):
    return [json.loads(item.text) for item in result.content]


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "board.db")
        self.board = Board(self.path)

    def test_persistence_validation_and_replies(self):
        first = self.board.create_message("a", "s", "b" * 4094)
        self.assertEqual(Board(self.path).messages()[0]["id"], first)
        for args in [("a", "s", "b" * 4095), ("a", "s", "é" * 2048), ("", "s", "b")]:
            with self.assertRaises(ValueError):
                self.board.create_message(*args)
        with self.assertRaises(ValueError):
            self.board.create_message("a", "s", "b", 999)
        second = self.board.create_message("b", "re", "answer", first)
        self.assertEqual(self.board.reply(first)["id"], second)
        self.assertEqual([m["id"] for m in self.board.messages(first)], [second])

    def test_session_nicknames_and_independent_cursors(self):
        class Session:
            pass

        a = SimpleNamespace(session=Session())
        b = SimpleNamespace(session=Session())
        with patch("talkative.mcp.get_board", return_value=self.board):
            with self.assertRaises(ValueError):
                asyncio.run(mcp.read_messages(a))
            nickname_a = asyncio.run(mcp.generate_nickname(a))
            self.assertEqual(asyncio.run(mcp.generate_nickname(a)), nickname_a)
            self.assertNotEqual(asyncio.run(mcp.generate_nickname(b)), nickname_a)
            first = asyncio.run(mcp.create_message(a, "hello", "world"))
            self.assertEqual(asyncio.run(mcp.read_messages(a))[0]["id"], first)
            self.assertEqual(asyncio.run(mcp.read_messages(a)), [])
            self.assertEqual(asyncio.run(mcp.read_messages(b))[0]["id"], first)
            second = asyncio.run(mcp.create_message(b, "re", "answer", first))
            reply = asyncio.run(mcp.wait_for_reply(a, first, 0))
            self.assertEqual(reply["id"], second)
            self.assertEqual(asyncio.run(mcp.read_messages(a))[0]["id"], second)
            self.assertIsNone(asyncio.run(mcp.wait_for_reply(a, second, 0)))
            asyncio.run(mcp.create_message(a, "own", "post"))
            self.assertEqual(asyncio.run(mcp.wait_for_messages(a, 0)), [])

            async def listen():
                waiting = asyncio.create_task(mcp.wait_for_messages(a, 5))
                await asyncio.sleep(0.3)
                self.assertFalse(waiting.done())
                await asyncio.to_thread(self.board.create_message, "human", "new thread", "not a reply")
                return await asyncio.wait_for(waiting, 2)

            self.assertEqual([m["subject"] for m in asyncio.run(listen())], ["own", "new thread"])
            self.assertEqual(asyncio.run(mcp.read_messages(a)), [])
            with self.assertRaises(ValueError):
                asyncio.run(mcp.wait_for_messages(a, 301))
            agents = {agent["nickname"]: agent for agent in asyncio.run(mcp.list_agents(b))}
            self.assertEqual(agents.keys(), {nickname_a, asyncio.run(mcp.generate_nickname(b))})
            self.assertEqual(agents[nickname_a]["messages_sent"], 2)
            self.assertIsNotNone(agents[nickname_a]["last_seen"])
        with self.assertRaises(ValueError):
            asyncio.run(mcp.list_agents(SimpleNamespace(session=Session())))

    def test_only_for_me_and_read_receipts(self):
        class Session:
            pass

        a, b, c = (SimpleNamespace(session=Session()) for _ in range(3))
        with patch("talkative.mcp.get_board", return_value=self.board):
            nick_a, nick_b, nick_c = (asyncio.run(mcp.generate_nickname(ctx)) for ctx in (a, b, c))
            question = asyncio.run(mcp.create_message(a, "question", "anyone?"))
            asyncio.run(mcp.create_message(b, "unrelated", "noise"))
            reply = asyncio.run(mcp.create_message(b, "re", "answer", question))
            mention = asyncio.run(mcp.create_message(c, "ping", f"hey {nick_a}"))
            asyncio.run(mcp.create_message(a, f"self {nick_a}", "own message is never for me"))
            self.assertEqual([m["id"] for m in asyncio.run(mcp.read_messages(a, True))], [reply, mention])
            self.assertEqual(asyncio.run(mcp.read_messages(a)), [])  # skipped messages are consumed

            async def listen():
                waiting = asyncio.create_task(mcp.wait_for_messages(a, 5, True))
                await asyncio.to_thread(self.board.create_message, nick_b, "more noise", "not for a")
                await asyncio.sleep(0.4)
                self.assertFalse(waiting.done())
                await asyncio.to_thread(self.board.create_message, nick_c, "re", "for a", question)
                return await asyncio.wait_for(waiting, 2)

            self.assertEqual([m["body"] for m in asyncio.run(listen())], ["for a"])

            # Receipts: only messages actually returned to someone else, never one's own.
            asyncio.run(mcp.read_messages(c))
            self.assertEqual(asyncio.run(mcp.wait_for_reply(b, question, 0))["id"], reply)
            readers = self.board.readers()
            self.assertEqual([r["nickname"] for r in readers[reply]], [nick_a, nick_c])
            self.assertEqual({r["nickname"] for r in readers[question]}, {nick_c})
            self.assertNotIn(nick_a, [r["nickname"] for r in readers.get(question, [])])
            self.assertNotIn(question + 1, {m for m, rs in readers.items() if nick_a in [r["nickname"] for r in rs]})

    def test_resume_nickname_and_takeover(self):
        class Session:
            pass

        def connect():
            return SimpleNamespace(session=Session())

        first = connect()
        with patch("talkative.mcp.get_board", return_value=self.board):
            nickname = asyncio.run(mcp.generate_nickname(first))
            other = asyncio.run(mcp.generate_nickname(connect()))
            seen = asyncio.run(mcp.create_message(first, "before", "disconnect"))
            self.assertEqual([m["id"] for m in asyncio.run(mcp.read_messages(first))], [seen])
            mcp._release(mcp.session(first)["owner"])
            self.assertFalse({a["nickname"]: a for a in self.board.agents()}[nickname]["online"])
            later = self.board.create_message(other, "while", "offline")

            # Resuming restores the identity and the persisted read position.
            second = connect()
            self.assertEqual(asyncio.run(mcp.generate_nickname(second, nickname)), nickname)
            self.assertTrue({a["nickname"]: a for a in self.board.agents()}[nickname]["online"])
            self.assertEqual([m["id"] for m in asyncio.run(mcp.read_messages(second))], [later])
            with self.assertRaisesRegex(ValueError, "already"):
                asyncio.run(mcp.generate_nickname(second, other))
            with self.assertRaisesRegex(ValueError, "Unknown nickname"):
                asyncio.run(mcp.generate_nickname(connect(), "agent-made-up"))

            # The newest connection wins; the old one is told and can start over with a new nickname.
            third = connect()
            self.assertEqual(asyncio.run(mcp.generate_nickname(third, nickname)), nickname)
            with self.assertRaisesRegex(ValueError, "Another connection"):
                asyncio.run(mcp.read_messages(second))
            self.assertNotIn(asyncio.run(mcp.generate_nickname(second)), (nickname, other))
            mcp._release(mcp.session(second)["owner"])
            self.assertTrue({a["nickname"]: a for a in self.board.agents()}[nickname]["online"])

            # A nickname removed in the web UI can no longer be resumed.
            self.board.remove_agents([nickname])
            with self.assertRaisesRegex(ValueError, "Unknown nickname"):
                asyncio.run(mcp.generate_nickname(connect(), nickname))

    def test_upgrades_legacy_nickname_table(self):
        path = str(Path(self.temp.name) / "legacy.db")
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE nicknames (nickname TEXT PRIMARY KEY)")
            db.execute("INSERT INTO nicknames VALUES ('agent-old')")
        board = Board(path)
        board.touch("agent-old")
        [agent] = board.agents()
        self.assertEqual(agent["nickname"], "agent-old")
        self.assertIsNotNone(agent["last_seen"])

    @override_settings(ALLOWED_HOSTS=["testserver"])
    def test_remove_agent_from_web_keeps_messages(self):
        class Session:
            pass

        agent_ctx = SimpleNamespace(session=Session())
        with patch("talkative.views.get_board", return_value=self.board), \
                patch("talkative.mcp.get_board", return_value=self.board):
            agent = asyncio.run(mcp.generate_nickname(agent_ctx))
            asyncio.run(mcp.create_message(agent_ctx, "kept", "after removal"))
            self.board.mark_read("agent-reader", self.board.messages())
            client = Client(enforce_csrf_checks=True)
            page = client.get("/")
            self.assertIn("Seen by 1", page.content.decode())
            self.assertIn('<span title="Read at ', page.content.decode())
            self.assertIn("agent-reader</span>", page.content.decode())
            self.assertIn(f'action="/agents/{agent}/remove"', page.content.decode())
            token = page.cookies["csrftoken"].value
            self.assertEqual(client.post(f"/agents/{agent}/remove").status_code, 403)
            self.assertEqual(client.get(f"/agents/{agent}/remove").status_code, 405)
            removed = client.post(f"/agents/{agent}/remove", HTTP_X_CSRFTOKEN=token)
            self.assertEqual((removed.status_code, removed.headers["Location"]), (303, "/"))
            self.assertEqual(self.board.agents(), [])
            self.assertIn("after removal", client.get("/").content.decode())
            self.assertEqual(client.post(f"/agents/{agent}/remove", HTTP_X_CSRFTOKEN=token).status_code, 404)
            # A removed agent that is still connected is told to rejoin and gets a new nickname.
            with self.assertRaisesRegex(ValueError, "removed"):
                asyncio.run(mcp.read_messages(agent_ctx))
            self.assertNotEqual(asyncio.run(mcp.generate_nickname(agent_ctx)), agent)

    @override_settings(ALLOWED_HOSTS=["testserver"])
    def test_web_form_and_escaping(self):
        with patch("talkative.views.get_board", return_value=self.board):
            client = Client(enforce_csrf_checks=True)
            self.assertEqual(client.post("/", {"sender": "human"}).status_code, 403)
            empty = client.get("/")
            self.assertIn(b"Your inbox is empty", empty.content)
            self.assertIn(b"No agents have joined yet.", empty.content)
            agent = self.board.generate_nickname()
            self.assertIn(agent, client.get("/").content.decode())
            token = empty.cookies["csrftoken"].value
            response = client.post("/", urlencode({"sender": "human", "subject": "<script>", "body": "Hello",
                                                    "reply_to": "", "csrfmiddlewaretoken": token}),
                                   content_type="application/x-www-form-urlencoded")
            self.assertEqual(response.status_code, 303)
            response = client.get("/")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            page = response.content.decode()
            self.assertIn("&lt;script&gt;", page)
            self.assertNotIn("<script>", page)
            self.assertIn("From: human", page)
            self.assertIn('href="/?reply_to=1#compose"', page)
            reply_page = client.get("/?reply_to=1").content.decode()
            self.assertIn("Reply to #1", reply_page)
            self.assertIn('name="reply_to" type="number" min="1" value="1"', reply_page)
            invalid_reply = client.get("/?reply_to=not-a-number").content.decode()
            self.assertIn('name="reply_to" type="number" min="1" value=""', invalid_reply)
            response = client.post("/", urlencode({"sender": "human", "subject": "Re", "body": "Reply body",
                                                    "reply_to": "1", "csrfmiddlewaretoken": token}),
                                   content_type="application/x-www-form-urlencoded")
            self.assertEqual(response.status_code, 303)
            self.assertEqual(self.board.reply(1)["body"], "Reply body")
            page = client.get("/").content.decode()
            self.assertIn("2 messages", page)
            self.assertLess(page.index("Reply body"), page.index("Hello"))
            response = client.post("/", urlencode({"sender": "a", "subject": "s", "body": "b", "reply_to": "999",
                                                    "csrfmiddlewaretoken": token}),
                                   content_type="application/x-www-form-urlencoded")
            self.assertEqual(response.status_code, 400)
            response = client.post("/", data=urlencode([("sender", "a"), ("sender", "b"),
                                                         ("csrfmiddlewaretoken", token)]),
                                   content_type="application/x-www-form-urlencoded")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(client.post("/", data="x" * 16385, content_type="application/x-www-form-urlencoded",
                                         HTTP_X_CSRFTOKEN=token).status_code, 400)
            self.assertEqual(client.get("/unknown").status_code, 404)

    def test_mcp_http_sessions(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        stale = self.board.generate_nickname()
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "talkative.asgi:application", "--port", str(port)],
            env={**os.environ, "TALKATIVE_DB": self.path},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        def stop():
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self.addCleanup(stop)
        deadline = time.monotonic() + 10
        while True:
            if process.poll() is not None:
                self.fail("MCP server exited before starting")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    self.fail("MCP server did not start")
                time.sleep(0.05)

        async def run():
            url = f"http://127.0.0.1:{port}/mcp"
            async with streamablehttp_client(url) as (read_a, write_a, _):
                async with ClientSession(read_a, write_a) as a:
                    await a.initialize()
                    async with sse_client(url + "/sse") as (read_b, write_b):
                        async with ClientSession(read_b, write_b) as b:
                            await b.initialize()
                            self.assertTrue((await a.call_tool("read_messages", {})).isError)
                            nick_a = await a.call_tool("generate_nickname", {})
                            nick_b = await b.call_tool("generate_nickname", {})
                            self.assertFalse(nick_a.isError)
                            self.assertFalse(nick_b.isError)
                            self.assertNotEqual(nick_a.content[0].text, nick_b.content[0].text)
                            self.assertEqual((await a.call_tool("generate_nickname", {})).content[0].text,
                                             nick_a.content[0].text)
                            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as web:
                                page = await web.get("/")
                                self.assertEqual(page.status_code, 200)
                                response = await web.post("/", data={"sender": "human", "subject": "web",
                                                                     "body": "from browser"},
                                                          headers={"X-CSRFToken": web.cookies["csrftoken"]})
                                self.assertEqual(response.status_code, 303)
                                self.assertIn("from browser", str((await a.call_tool("read_messages", {})).content))
                                self.assertEqual((await web.post("/mcp", content="{}", headers={
                                    "host": "evil.example",
                                })).status_code, 421)
                                self.assertEqual((await web.post("/mcp", content="{}", headers={
                                    "origin": "http://evil.example",
                                })).status_code, 403)
                                self.assertEqual((await web.post("/mcp", content="{}", headers={
                                    "mcp-session-id": "stale-after-restart",
                                })).status_code, 404)
                            posted = await a.call_tool("create_message", {"subject": "hello", "body": "world"})
                            self.assertFalse(posted.isError)
                            message_id = int(posted.content[0].text)
                            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as web:
                                self.assertIn("world", (await web.get("/")).text)
                            self.assertIn("world", str((await a.call_tool("read_messages", {})).content))
                            self.assertNotIn("world", str((await a.call_tool("read_messages", {})).content))
                            self.assertIn("world", str((await b.call_tool("read_messages", {})).content))
                            waiting = asyncio.create_task(a.call_tool(
                                "wait_for_reply", {"message_id": message_id, "timeout": 2}))
                            await asyncio.sleep(0.1)
                            reply = await b.call_tool("create_message",
                                                      {"subject": "re", "body": "answer", "reply_to": message_id})
                            self.assertFalse(reply.isError)
                            waited = await waiting
                            self.assertIn("answer", str(waited.content))
                            self.assertIn("answer", str((await a.call_tool("read_messages", {})).content))

        async def reconnect(resume=None):
            async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    args = {} if resume is None else {"nickname": resume}
                    nickname = (await client.call_tool("generate_nickname", args)).content[0].text
                    online = [a["nickname"] for a in json_items(await client.call_tool("list_agents", {})) if a["online"]]
                    self.assertEqual(online, [nickname])
                    return nickname, json_items(await client.call_tool("read_messages", {}))

        def wait_until_all_offline():
            deadline = time.monotonic() + 5
            while any(a["online"] for a in self.board.agents()) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(any(a["online"] for a in self.board.agents()))

        # Agents from a previous run are kept but marked offline at startup.
        self.assertEqual([(a["nickname"], a["online"]) for a in self.board.agents()], [(stale, False)])
        asyncio.run(run())
        # Agents go offline when their Streamable HTTP or SSE connection ends; they and their messages stay.
        wait_until_all_offline()
        self.assertEqual(len(self.board.agents()), 3)
        # Closed sessions (client DELETE) must not take down the shared MCP endpoint.
        nickname, backlog = asyncio.run(reconnect())
        self.assertTrue(backlog)
        wait_until_all_offline()
        # Resuming keeps the identity and read position: nothing new since the last read.
        self.assertEqual(asyncio.run(reconnect(nickname)), (nickname, []))
        wait_until_all_offline()

if __name__ == "__main__":
    unittest.main()
