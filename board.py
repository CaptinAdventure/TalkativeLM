import os
import sqlite3
import uuid
from contextlib import contextmanager


MAX_MESSAGE_BYTES = 4096
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


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
                CREATE TABLE IF NOT EXISTS reads (
                    message_id INTEGER NOT NULL REFERENCES messages(id),
                    nickname TEXT NOT NULL,
                    read_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    PRIMARY KEY (message_id, nickname)
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(nicknames)")}
            for column, definition in (
                ("created_at", "TEXT"),
                ("last_seen", "TEXT"),
                ("online", "INTEGER NOT NULL DEFAULT 0"),
                ("read_cursor", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in columns:
                    db.execute(f"ALTER TABLE nicknames ADD COLUMN {column} {definition}")

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
                    db.execute(
                        f"INSERT INTO nicknames (nickname, created_at, last_seen, online) VALUES (?, {NOW}, {NOW}, 1)",
                        (nickname,),
                    )
                    return nickname
                except sqlite3.IntegrityError:
                    continue

    def touch(self, nickname):
        with self.connect() as db:
            return db.execute(
                f"UPDATE nicknames SET last_seen = {NOW} WHERE nickname = ?", (nickname,)
            ).rowcount == 1

    def claim(self, nickname):
        """Mark an existing agent online again and return its saved read cursor, or None if unknown."""
        with self.connect() as db:
            row = db.execute(
                f"UPDATE nicknames SET online = 1, last_seen = {NOW} WHERE nickname = ? RETURNING read_cursor",
                (nickname,),
            ).fetchone()
            return None if row is None else row["read_cursor"]

    def save_cursor(self, nickname, cursor):
        with self.connect() as db:
            db.execute("UPDATE nicknames SET read_cursor = ? WHERE nickname = ?", (cursor, nickname))

    def set_offline(self, nicknames=None):
        """Mark agents offline (all when None), e.g. when their connection ends or the server starts."""
        with self.connect() as db:
            if nicknames is None:
                db.execute("UPDATE nicknames SET online = 0")
            else:
                db.executemany("UPDATE nicknames SET online = 0 WHERE nickname = ?", [(n,) for n in nicknames])

    def remove_agents(self, nicknames):
        """Forget agent nicknames; their messages and read receipts are kept."""
        with self.connect() as db:
            return sum(
                db.execute("DELETE FROM nicknames WHERE nickname = ?", (nickname,)).rowcount
                for nickname in nicknames
            )

    def agents(self):
        with self.connect() as db:
            return [dict(row, online=bool(row["online"])) for row in db.execute("""
                SELECT n.nickname, n.online, n.created_at AS first_seen, n.last_seen,
                       (SELECT COUNT(*) FROM messages m WHERE m.sender = n.nickname) AS messages_sent
                FROM nicknames n
                ORDER BY n.online DESC, n.last_seen IS NULL, n.last_seen DESC, n.nickname
            """)]

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

    def messages_for(self, nickname, after=0, until=None):
        """Messages from others that reply to one of nickname's messages or mention nickname."""
        with self.connect() as db:
            return [dict(row) for row in db.execute("""
                SELECT m.* FROM messages m LEFT JOIN messages p ON p.id = m.reply_to
                WHERE m.id > ? AND m.id <= ? AND m.sender != ?
                  AND (p.sender = ? OR instr(m.subject, ?) > 0 OR instr(m.body, ?) > 0)
                ORDER BY m.id
            """, (after, 2**63 - 1 if until is None else until, nickname, nickname, nickname, nickname))]

    def last_message_id(self):
        with self.connect() as db:
            return db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]

    def mark_read(self, nickname, messages):
        """Record read receipts for messages that nickname did not send itself."""
        with self.connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO reads (message_id, nickname) VALUES (?, ?)",
                [(message["id"], nickname) for message in messages if message["sender"] != nickname],
            )

    def readers(self):
        """Map message id to the list of {nickname, read_at} receipts, oldest first."""
        result = {}
        with self.connect() as db:
            for row in db.execute("SELECT * FROM reads ORDER BY read_at, nickname"):
                result.setdefault(row["message_id"], []).append(
                    {"nickname": row["nickname"], "read_at": row["read_at"]}
                )
        return result

    def reply(self, message_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM messages WHERE reply_to = ? ORDER BY id LIMIT 1", (message_id,)
            ).fetchone()
            return dict(row) if row else None


def default_db_path():
    return os.environ.get("TALKATIVE_DB", "talkative.db")
