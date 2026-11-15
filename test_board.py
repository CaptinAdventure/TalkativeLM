import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from board import Board
from mcp_server import create_server
from web import handler_for


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
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.board))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = HTTPConnection("127.0.0.1", server.server_port)
        self.addCleanup(conn.close)
        body = urlencode({"sender": "human", "subject": "<script>", "body": "Hello", "reply_to": ""})
        conn.request("POST", "/", body, {"Content-Type": "application/x-www-form-urlencoded"})
        response = conn.getresponse()
        self.assertEqual(response.status, 303)
        response.read()
        conn.request("GET", "/")
        response = conn.getresponse()
        page = response.read().decode()
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)
        self.assertIn("From: human", page)
        conn.request("POST", "/", urlencode({"sender": "a", "subject": "s", "body": "b", "reply_to": "999"}),
                     {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(conn.getresponse().status, 400)

    def test_mcp_stdio_protocol(self):
        async def run():
            params = StdioServerParameters(
                command="python",
                args=[str(Path(__file__).with_name("mcp_server.py")), "--transport", "stdio"],
                env={**os.environ, "TALKATIVE_DB": self.path},
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    result = await client.call_tool("generate_nickname", {})
                    self.assertFalse(result.isError)
                    result = await client.call_tool("create_message", {"subject": "hi", "body": "there"})
                    self.assertFalse(result.isError)
                    result = await client.call_tool("read_messages", {})
                    self.assertFalse(result.isError)
                    self.assertIn("there", str(result.content))

        asyncio.run(run())

    def test_mcp_http_sessions(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("mcp_server.py")),
             "--host", "0.0.0.0", "--port", str(port)],
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
                            posted = await a.call_tool("create_message", {"subject": "hello", "body": "world"})
                            self.assertFalse(posted.isError)
                            message_id = int(posted.content[0].text)
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
