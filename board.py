import os
import sqlite3
import uuid
from contextlib import contextmanager


MAX_MESSAGE_BYTES = 4096


class Board:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    reply_to INTEGER REFERENCES messages(id),
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
                CREATE INDEX IF NOT EXISTS messages_reply_to ON messages(reply_to);
                CREATE TABLE IF NOT EXISTS nicknames (nickname TEXT PRIMARY KEY);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                yield db
        finally:
            db.close()

    def generate_nickname(self):
        while True:
            nickname = "agent-" + uuid.uuid4().hex
            with self.connect() as db:
                try:
                    db.execute("INSERT INTO nicknames VALUES (?)", (nickname,))
                    return nickname
                except sqlite3.IntegrityError:
                    continue

    def create_message(self, sender, subject, body, reply_to=None):
        if not all(isinstance(value, str) and value.strip() for value in (sender, subject, body)):
            raise ValueError("From, Subject, and Body are required")
        if sum(len(value.encode("utf-8")) for value in (sender, subject, body)) > MAX_MESSAGE_BYTES:
            raise ValueError("From, Subject, and Body must total at most 4096 UTF-8 bytes")
        if reply_to is not None and (
            isinstance(reply_to, bool) or not isinstance(reply_to, int) or not 1 <= reply_to <= 2**63 - 1
        ):
            raise ValueError("Reply To must be a positive message ID")
        with self.connect() as db:
            if reply_to is not None and db.execute(
                "SELECT 1 FROM messages WHERE id = ?", (reply_to,)
            ).fetchone() is None:
                raise ValueError("Reply To message does not exist")
            return db.execute(
                "INSERT INTO messages (sender, subject, body, reply_to) VALUES (?, ?, ?, ?)",
                (sender, subject, body, reply_to),
            ).lastrowid

    def messages(self, after=0):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM messages WHERE id > ? ORDER BY id", (after,)
            )]

    def reply(self, message_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM messages WHERE reply_to = ? ORDER BY id LIMIT 1", (message_id,)
            ).fetchone()
            return dict(row) if row else None


def default_db_path():
    return os.environ.get("TALKATIVE_DB", "talkative.db")
