import argparse
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from board import Board, default_db_path


def handler_for(board):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, body, content_type="text/html; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if urlsplit(self.path).path != "/":
                self.respond(404, "Not found", "text/plain; charset=utf-8")
                return
            messages = board.messages()
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
            reply_to = parse_qs(urlsplit(self.path).query).get("reply_to", [""])[0]
            reply_to = reply_to if reply_to.isascii() and reply_to.isdigit() else ""
            self.respond(200, f"""<!doctype html>
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
<button type="submit">Send</button></form><h2>Messages</h2>{''.join(rows)}</body></html>""")

        def do_POST(self):
            if urlsplit(self.path).path != "/":
                self.respond(404, "Not found", "text/plain; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 0 <= length <= 16384:
                    raise ValueError("Invalid request size")
                if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                    raise ValueError("Expected a form submission")
                fields = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True, max_num_fields=10)
                def field(name):
                    values = fields.get(name, [""])
                    if len(values) != 1:
                        raise ValueError("Duplicate form field")
                    return values[0]
                reply = field("reply_to")
                if reply and (not reply.isascii() or not reply.isdigit()):
                    raise ValueError("Reply To must be a positive message ID")
                board.create_message(field("sender"), field("subject"), field("body"), int(reply) if reply else None)
            except (ValueError, UnicodeError) as exc:
                self.respond(400, html.escape(str(exc)))
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the TalkativeLM mailbox")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), handler_for(Board(default_db_path()))).serve_forever()
