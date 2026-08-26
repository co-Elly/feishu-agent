"""Thread-safe SQLite conversation history with one-time JSON migration."""

import json
import os
import sqlite3
import threading
import time


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.abspath(os.environ.get(
    "FEISHU_DB_PATH", os.path.join(BASE_DIR, "workspace", "conversations.db"),
))
LEGACY_PATH = os.path.join(BASE_DIR, "chat_history.json")
_LOCK = threading.RLock()


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    from sqlite_compat import set_journal_mode
    set_journal_mode(conn)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_key TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at REAL NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_key, id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS processed_events (
        event_id TEXT PRIMARY KEY,
        received_at REAL NOT NULL
    )""")
    return conn


def migrate_legacy_json():
    if not os.path.exists(LEGACY_PATH):
        return 0
    with _LOCK, _connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if count:
            return 0
        try:
            with open(LEGACY_PATH, encoding="utf-8") as handle:
                histories = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return 0
        rows = []
        now = time.time()
        for session_key, items in histories.items():
            for offset, item in enumerate(items):
                if item.get("role") in {"user", "assistant"}:
                    rows.append((session_key, item["role"], str(item.get("content", "")), now + offset / 1000))
        conn.executemany("INSERT INTO messages(session_key, role, content, created_at) VALUES(?,?,?,?)", rows)
        return len(rows)


def get_history(session_key, max_turns=10):
    limit = max_turns * 2
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_key=? ORDER BY id DESC LIMIT ?",
            (session_key, limit),
        ).fetchall()
    return [{"role": role, "content": content} for role, content in reversed(rows)]


def add_exchange(session_key, user_text, model_text):
    with _LOCK, _connect() as conn:
        now = time.time()
        conn.executemany(
            "INSERT INTO messages(session_key, role, content, created_at) VALUES(?,?,?,?)",
            [(session_key, "user", user_text, now), (session_key, "assistant", model_text, now + 0.001)],
        )


def clear_history(session_key=None):
    with _LOCK, _connect() as conn:
        if session_key:
            conn.execute("DELETE FROM messages WHERE session_key=?", (session_key,))
        else:
            conn.execute("DELETE FROM messages")


def claim_event(event_id):
    """Atomically claim an incoming Feishu event; False means duplicate."""
    with _LOCK, _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO processed_events(event_id, received_at) VALUES(?,?)",
                (event_id, time.time()),
            )
            conn.execute("DELETE FROM processed_events WHERE received_at < ?", (time.time() - 604800,))
            return True
        except sqlite3.IntegrityError:
            return False
