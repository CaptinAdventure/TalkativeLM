import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient

from board import Board
from mcp_server import create_server


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
        server = create_server(self.board)
        tools = {name: tool.fn for name, tool in server._tool_manager._tools.items()}

        class Session:
            pass

        a = SimpleNamespace(session=Session())
        b = SimpleNamespace(session=Session())
        with self.assertRaises(ValueError):
            tools["read_messages"](a)
        nickname_a = tools["generate_nickname"](a)
        self.assertEqual(tools["generate_nickname"](a), nickname_a)
        self.assertNotEqual(tools["generate_nickname"](b), nickname_a)
        first = tools["create_message"](a, "hello", "world")
        self.assertEqual(tools["read_messages"](a)[0]["id"], first)
        self.assertEqual(tools["read_messages"](a), [])
        self.assertEqual(tools["read_messages"](b)[0]["id"], first)
        second = tools["create_message"](b, "re", "answer", first)
        reply = asyncio.run(tools["wait_for_reply"](a, first, 0))
        self.assertEqual(reply["id"], second)
        self.assertEqual(tools["read_messages"](a)[0]["id"], second)
        self.assertIsNone(asyncio.run(tools["wait_for_reply"](a, second, 0)))

    def test_web_form_and_escaping(self):
        with TestClient(create_server(self.board).streamable_http_app()) as client:
            response = client.post("/", data={"sender": "human", "subject": "<script>", "body": "Hello",
                                               "reply_to": ""}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            response = client.get("/")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertIn("&lt;script&gt;", response.text)
            self.assertNotIn("<script>", response.text)
            self.assertIn("From: human", response.text)
            response = client.post("/", data={"sender": "a", "subject": "s", "body": "b", "reply_to": "999"})
            self.assertEqual(response.status_code, 400)
            response = client.post("/", content=urlencode([("sender", "a"), ("sender", "b")]),
                                   headers={"content-type": "application/x-www-form-urlencoded"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(client.post("/", content="x" * 16385,
                                         headers={"content-type": "application/x-www-form-urlencoded"}).status_code, 400)
            self.assertEqual(client.get("/unknown").status_code, 404)
            self.assertEqual(client.post("/mcp", content="{}", headers={
                "host": "evil.example", "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            }).status_code, 421)

    def test_mcp_http_sessions(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("mcp_server.py")),
             "--port", str(port)],
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
            async with streamable_http_client(url) as (read_a, write_a, _):
                async with ClientSession(read_a, write_a) as a:
                    await a.initialize()
                    async with streamable_http_client(url) as (read_b, write_b, _):
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
                                response = await web.post("/", data={"sender": "human", "subject": "web",
                                                                     "body": "from browser"})
                                self.assertEqual(response.status_code, 303)
                                self.assertIn("from browser", str((await a.call_tool("read_messages", {})).content))
                            posted = await a.call_tool("create_message", {"subject": "hello", "body": "world"})
                            self.assertFalse(posted.isError)
                            message_id = int(posted.content[0].text)
                            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as web:
                                self.assertIn("world", (await web.get("/")).text)
                            self.assertIn("world", str((await a.call_tool("read_messages", {})).content))
                            self.assertNotIn("world", str((await a.call_tool("read_messages", {})).content))
                            self.assertIn("world", str((await b.call_tool("read_messages", {})).content))
                            reply = await b.call_tool("create_message",
                                                      {"subject": "re", "body": "answer", "reply_to": message_id})
                            self.assertFalse(reply.isError)
                            waited = await a.call_tool("wait_for_reply", {"message_id": message_id, "timeout": 0})
                            self.assertIn("answer", str(waited.content))
                            self.assertIn("answer", str((await a.call_tool("read_messages", {})).content))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
