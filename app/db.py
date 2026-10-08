"""Single-host storage. Only the backend owns this database.

The WhatsApp bridge will own a separate credential store. Never mount this file
on a network filesystem or run multiple backend instances against it.
"""

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.domain import Completion, Message, QuotaExceeded, User


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4):
                raise RuntimeError("Unsupported database schema version")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    channel TEXT NOT NULL DEFAULT 'web' CHECK(channel = 'web'),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    user_text TEXT NOT NULL, assistant_text TEXT NOT NULL,
                    model TEXT NOT NULL, created_at TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS turns_conversation ON turns(conversation_id, id);
                CREATE TABLE IF NOT EXISTS usage (
                    day TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(id),
                    requests INTEGER NOT NULL, PRIMARY KEY(day, user_id)
                );
                CREATE TABLE IF NOT EXISTS whatsapp_members (
                    phone TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL UNIQUE REFERENCES users(id),
                    role TEXT NOT NULL CHECK(role IN ('owner', 'member'))
                );
                CREATE TABLE IF NOT EXISTS whatsapp_scopes (
                    scope TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL UNIQUE REFERENCES conversations(id)
                );
                CREATE TABLE IF NOT EXISTS whatsapp_events (
                    event_id TEXT PRIMARY KEY,
                    text TEXT NOT NULL, model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    root_id TEXT NOT NULL, sha256 TEXT NOT NULL,
                    drive_file_id TEXT NOT NULL, filename TEXT NOT NULL,
                    destination_id TEXT NOT NULL, uploaded_by TEXT NOT NULL,
                    status TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(root_id, sha256)
                );
                CREATE TABLE IF NOT EXISTS knowledge_stores (
                    root_id TEXT PRIMARY KEY, store_name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    root_id TEXT NOT NULL, drive_id TEXT NOT NULL,
                    source_key TEXT NOT NULL, filename TEXT NOT NULL,
                    modified_at TEXT NOT NULL, sha256 TEXT NOT NULL,
                    document_name TEXT NOT NULL DEFAULT '', operation_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL, page_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                    PRIMARY KEY(root_id, drive_id)
                );
                PRAGMA user_version = 4;
            """)
        from app.workspace import Workspace

        Workspace(self).initialize()

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def enroll(self, name: str, token: str) -> User:
        user = User(str(uuid4()), name)
        with self.connect() as db:
            db.execute(
                "INSERT INTO users(id, name, token_hash) VALUES (?, ?, ?)",
                (user.id, user.name, self.token_hash(token)),
            )
        return user

    def revoke(self, user_id: str) -> bool:
        with self.connect() as db:
            if db.execute(
                "SELECT 1 FROM whatsapp_members WHERE user_id = ? AND role = 'owner'", (user_id,)
            ).fetchone():
                raise PermissionError("The permanent owner cannot be revoked")
            return db.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,)).rowcount > 0

    def ensure_owner(self, phone: str, name: str = "Owner") -> User:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT u.id, u.name, m.phone FROM users u JOIN whatsapp_members m "
                "ON u.id = m.user_id WHERE m.role = 'owner'"
            ).fetchone()
            if row:
                if row["phone"] != phone:
                    raise RuntimeError("Configured owner differs from registered permanent owner")
                db.execute("UPDATE users SET active = 1 WHERE id = ?", (row["id"],))
                return User(row["id"], row["name"])
            user = User(str(uuid4()), name)
            db.execute(
                "INSERT INTO users(id, name, token_hash) VALUES (?, ?, ?)",
                (user.id, user.name, self.token_hash(secrets.token_urlsafe(32))),
            )
            db.execute(
                "INSERT INTO whatsapp_members(phone, user_id, role) VALUES (?, ?, 'owner')",
                (phone, user.id),
            )
            return user

    def whatsapp_user(self, phone: str) -> User | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT u.id, u.name FROM users u JOIN whatsapp_members m "
                "ON u.id = m.user_id WHERE m.phone = ? AND u.active = 1",
                (phone,),
            ).fetchone()
        return User(row["id"], row["name"]) if row else None

    def approve_whatsapp_member(self, phone: str, name: str) -> User:
        import re

        if not re.fullmatch(r"[1-9][0-9]{7,14}", phone) or not 1 <= len(name.strip()) <= 100:
            raise ValueError("Use a full international phone number and a name of 1–100 characters")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR REPLACE INTO access_members(phone,allowed,blocked,admin) "
                "VALUES (?,1,0,COALESCE((SELECT admin FROM access_members WHERE phone=?),0))",
                (phone, phone),
            )
            row = db.execute(
                "SELECT u.id,u.name,m.role FROM users u JOIN whatsapp_members m "
                "ON u.id=m.user_id WHERE m.phone=?",
                (phone,),
            ).fetchone()
            if row:
                # Approval never changes an existing role, including the permanent owner.
                db.execute("UPDATE users SET active=1 WHERE id=?", (row["id"],))
                return User(row["id"], row["name"])
            user = User(str(uuid4()), name.strip())
            db.execute(
                "INSERT INTO users(id,name,token_hash) VALUES (?,?,?)",
                (user.id, user.name, self.token_hash(secrets.token_urlsafe(32))),
            )
            db.execute(
                "INSERT INTO whatsapp_members(phone,user_id,role) VALUES (?,?,'member')",
                (phone, user.id),
            )
            return user

    def whatsapp_members(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT m.phone,m.role,u.id,u.name FROM whatsapp_members m "
                    "JOIN users u ON u.id=m.user_id WHERE u.active=1 ORDER BY m.phone"
                ).fetchall()
            ]

    def whatsapp_conversation(self, user: User, scope: str) -> str:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT conversation_id FROM whatsapp_scopes WHERE scope = ?", (scope,)
            ).fetchone()
            if row:
                return row[0]
            cid = str(uuid4())
            db.execute(
                "INSERT INTO conversations(id, user_id, created_at) VALUES (?, ?, ?)",
                (cid, user.id, datetime.now(UTC).isoformat()),
            )
            db.execute("INSERT INTO whatsapp_scopes VALUES (?, ?)", (scope, cid))
            return cid

    def whatsapp_result(self, event_id: str) -> Completion | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM whatsapp_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return (
            Completion(row["text"], row["model"], row["input_tokens"], row["output_tokens"])
            if row
            else None
        )

    def authenticate(self, token: str) -> User | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id, name FROM users WHERE token_hash = ? AND active = 1",
                (self.token_hash(token),),
            ).fetchone()
        return User(row["id"], row["name"]) if row else None

    def is_active(self, user_id: str) -> bool:
        with self.connect() as db:
            return (
                db.execute("SELECT 1 FROM users WHERE id = ? AND active = 1", (user_id,)).fetchone()
                is not None
            )

    def create_conversation(self, user: User) -> str:
        conversation_id = str(uuid4())
        with self.connect() as db:
            # Bound empty sessions too; there is no need for thousands per member.
            count = db.execute(
                "SELECT COUNT(*) FROM conversations WHERE user_id = ?", (user.id,)
            ).fetchone()[0]
            if count >= 100:
                raise QuotaExceeded()
            db.execute(
                "INSERT INTO conversations(id, user_id, created_at) VALUES (?, ?, ?)",
                (conversation_id, user.id, datetime.now(UTC).isoformat()),
            )
        return conversation_id

    def owns_conversation(self, user_id: str, conversation_id: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM conversations WHERE id = ? AND user_id = ? "
                    "AND id NOT IN (SELECT conversation_id FROM whatsapp_scopes)",
                    (conversation_id, user_id),
                ).fetchone()
                is not None
            )

    def delete_conversation(self, user_id: str, conversation_id: str) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM conversations WHERE id = ? AND user_id = ? "
                "AND id NOT IN (SELECT conversation_id FROM whatsapp_scopes)",
                (conversation_id, user_id),
            ).fetchone():
                return False
            db.execute("DELETE FROM turns WHERE conversation_id = ?", (conversation_id,))
            db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            return True

    def history(self, conversation_id: str, max_turns: int, max_characters: int) -> list[Message]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT user_text, assistant_text FROM turns "
                "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                (conversation_id, max_turns),
            ).fetchall()
        pairs = []
        remaining = max_characters
        for row in rows:
            size = len(row["user_text"]) + len(row["assistant_text"])
            if size > remaining:
                break
            pairs.append(
                [Message("user", row["user_text"]), Message("assistant", row["assistant_text"])]
            )
            remaining -= size
        return [message for pair in reversed(pairs) for message in pair]

    def reserve_request(self, user_id: str, team_limit: int, user_limit: int):
        day = datetime.now(UTC).date().isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            total = db.execute(
                "SELECT COALESCE(SUM(requests), 0) FROM usage WHERE day = ?", (day,)
            ).fetchone()[0]
            row = db.execute(
                "SELECT requests FROM usage WHERE day = ? AND user_id = ?", (day, user_id)
            ).fetchone()
            if total >= team_limit or (row and row[0] >= user_limit):
                raise QuotaExceeded()
            # Failed attempts count: upstream timeouts may still have been billed.
            db.execute(
                "INSERT INTO usage(day, user_id, requests) VALUES (?, ?, 1) "
                "ON CONFLICT(day, user_id) DO UPDATE SET requests = requests + 1",
                (day, user_id),
            )

    def save_turn(
        self,
        conversation_id: str,
        text: str,
        result: Completion,
        max_turns: int,
        event_id: str | None = None,
    ):
        with self.connect() as db:
            if event_id:
                db.execute(
                    "INSERT INTO whatsapp_events VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        result.text,
                        result.model,
                        result.input_tokens,
                        result.output_tokens,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            db.execute(
                "INSERT INTO turns(conversation_id, user_text, assistant_text, model, "
                "created_at, input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    text,
                    result.text,
                    result.model,
                    datetime.now(UTC).isoformat(),
                    result.input_tokens,
                    result.output_tokens,
                ),
            )
            db.execute(
                "DELETE FROM turns WHERE conversation_id = ? AND id NOT IN "
                "(SELECT id FROM turns WHERE conversation_id = ? ORDER BY id DESC LIMIT ?)",
                (conversation_id, conversation_id, max_turns),
            )

    def cleanup(self, retention_days: int):
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        with self.connect() as db:
            db.execute("DELETE FROM turns WHERE created_at < ?", (cutoff,))
            db.execute(
                "UPDATE whatsapp_events SET text = '[Earlier response expired; please ask again.]' "
                "WHERE created_at < ?",
                (cutoff,),
            )
