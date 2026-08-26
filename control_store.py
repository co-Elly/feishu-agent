"""Structured operational state stored in the main SQLite database."""

import json
import sqlite3
import time

from conversation_store import DB_PATH


def _connect(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    from sqlite_compat import set_journal_mode
    set_journal_mode(conn)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""CREATE TABLE IF NOT EXISTS engine_health (
        engine TEXT PRIMARY KEY, available INTEGER NOT NULL DEFAULT 0,
        last_success_at REAL, last_error_at REAL, last_error_code TEXT,
        last_error TEXT, cooldown_until REAL, duration_ms INTEGER, updated_at REAL NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, event_type TEXT NOT NULL,
        from_status TEXT, to_status TEXT, engine TEXT, ok INTEGER, error_code TEXT,
        duration_ms INTEGER, details_json TEXT, created_at REAL NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, id)")
    return conn


def record_engine_health(engine, result):
    now = time.time()
    with _connect() as conn:
        previous = conn.execute("SELECT * FROM engine_health WHERE engine=?", (engine,)).fetchone()
        last_success = now if result.ok else (previous["last_success_at"] if previous else None)
        last_error_at = now if not result.ok else (previous["last_error_at"] if previous else None)
        last_error_code = result.error_code if not result.ok else (previous["last_error_code"] if previous else None)
        last_error = result.text[-500:] if not result.ok else (previous["last_error"] if previous else None)
        conn.execute("""INSERT INTO engine_health(
            engine, available, last_success_at, last_error_at, last_error_code,
            last_error, cooldown_until, duration_ms, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(engine) DO UPDATE SET available=excluded.available,
            last_success_at=excluded.last_success_at, last_error_at=excluded.last_error_at,
            last_error_code=excluded.last_error_code, last_error=excluded.last_error,
            cooldown_until=excluded.cooldown_until, duration_ms=excluded.duration_ms,
            updated_at=excluded.updated_at""",
            (engine, int(result.ok), last_success, last_error_at, last_error_code,
             last_error, result.cooldown_until, result.duration_ms, now))


def engine_health():
    with _connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM engine_health ORDER BY engine")]


def record_task_event(task_id, event_type, from_status=None, to_status=None, engine=None,
                      ok=None, error_code=None, duration_ms=None, details=None, db_path=None):
    safe = {key: value for key, value in (details or {}).items()
            if key.lower() not in {"prompt", "secret", "token", "api_key", "authorization"}}
    with _connect(db_path) as conn:
        conn.execute("""INSERT INTO task_events(
            task_id, event_type, from_status, to_status, engine, ok,
            error_code, duration_ms, details_json, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (task_id, event_type, from_status, to_status, engine,
         None if ok is None else int(ok), error_code, duration_ms,
         json.dumps(safe, ensure_ascii=False), time.time()))


def task_events(task_id, limit=100):
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT ?",
                            (task_id, limit)).fetchall()
    return [dict(row) for row in reversed(rows)]
