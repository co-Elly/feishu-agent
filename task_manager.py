"""Persistent background task state machine for Feishu long-running jobs."""

import json
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from conversation_store import DB_PATH


WORK_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace", "tasks")
TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
ACTIVE_STATES = {"queued", "running", "waiting_approval"}


class TaskCancelled(Exception):
    pass


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        task_type TEXT NOT NULL,
        status TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        user_id TEXT,
        message_id TEXT,
        payload_json TEXT NOT NULL,
        result_json TEXT,
        error TEXT,
        progress TEXT,
        work_dir TEXT NOT NULL,
        retry_of TEXT,
        attempt INTEGER NOT NULL DEFAULT 1,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        started_at REAL,
        finished_at REAL,
        updated_at REAL NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_chat_created ON tasks(chat_id, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at)")
    return conn


def _row(row):
    if not row:
        return None
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json") or "{}")
    raw_result = item.pop("result_json")
    item["result"] = json.loads(raw_result) if raw_result else None
    item["cancel_requested"] = bool(item["cancel_requested"])
    return item


def _write_snapshot(task):
    if not task:
        return
    path = os.path.join(task["work_dir"], "task.json")
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(task, handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)


class TaskStore:
    def create(self, task_type, chat_id, user_id, message_id, payload, retry_of=None, attempt=1):
        task_id = uuid.uuid4().hex[:10]
        work_dir = os.path.join(WORK_ROOT, task_id)
        os.makedirs(work_dir, exist_ok=False)
        now = time.time()
        with _connect() as conn:
            conn.execute(
                """INSERT INTO tasks(
                    id, task_type, status, chat_id, user_id, message_id, payload_json,
                    work_dir, retry_of, attempt, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, task_type, "queued", chat_id, user_id, message_id,
                 json.dumps(payload, ensure_ascii=False), work_dir, retry_of, attempt, now, now),
            )
        task = self.get(task_id)
        _write_snapshot(task)
        return task

    def get(self, task_id):
        with _connect() as conn:
            return _row(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def find(self, task_id_or_prefix, chat_id=None):
        with _connect() as conn:
            if chat_id:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE id LIKE ? AND chat_id=? ORDER BY created_at DESC LIMIT 2",
                    (task_id_or_prefix + "%", chat_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE id LIKE ? ORDER BY created_at DESC LIMIT 2",
                    (task_id_or_prefix + "%",),
                ).fetchall()
        return _row(rows[0]) if len(rows) == 1 else None

    def list(self, chat_id=None, limit=10):
        with _connect() as conn:
            if chat_id:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE chat_id=? ORDER BY created_at DESC LIMIT ?",
                    (chat_id, limit),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row(row) for row in rows]

    def counts(self):
        with _connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}

    def claim(self, task_id):
        now = time.time()
        with _connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET status='running', started_at=?, updated_at=? "
                "WHERE id=? AND status='queued' AND cancel_requested=0",
                (now, now, task_id),
            )
            return cursor.rowcount == 1

    def progress(self, task_id, text):
        with _connect() as conn:
            conn.execute("UPDATE tasks SET progress=?, updated_at=? WHERE id=?", (text[:500], time.time(), task_id))
        _write_snapshot(self.get(task_id))

    def finish(self, task_id, status, result=None, error=None):
        if status not in TERMINAL_STATES:
            raise ValueError(f"invalid terminal status: {status}")
        with _connect() as conn:
            conn.execute(
                "UPDATE tasks SET status=?, result_json=?, error=?, finished_at=?, updated_at=? WHERE id=?",
                (status, json.dumps(result, ensure_ascii=False) if result is not None else None,
                 error, time.time(), time.time(), task_id),
            )
        _write_snapshot(self.get(task_id))

    def request_cancel(self, task_id):
        with _connect() as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["status"] in TERMINAL_STATES:
                return False
            now = time.time()
            if row["status"] == "queued":
                conn.execute(
                    "UPDATE tasks SET status='cancelled', cancel_requested=1, finished_at=?, updated_at=? WHERE id=?",
                    (now, now, task_id),
                )
            else:
                conn.execute("UPDATE tasks SET cancel_requested=1, updated_at=? WHERE id=?", (now, task_id))
            return True

    def is_cancel_requested(self, task_id):
        with _connect() as conn:
            row = conn.execute("SELECT cancel_requested FROM tasks WHERE id=?", (task_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def retry(self, task_id, message_id=None):
        old = self.get(task_id)
        if not old or old["status"] not in TERMINAL_STATES:
            return None
        return self.create(
            old["task_type"], old["chat_id"], old["user_id"], message_id or old["message_id"],
            old["payload"], retry_of=old["id"], attempt=old["attempt"] + 1,
        )

    def recover(self):
        """Recover safely: queued work resumes; non-idempotent Swarm work fails for explicit retry."""
        now = time.time()
        with _connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='failed', finished_at=?, "
                "error='service restarted during non-idempotent execution; use retry', updated_at=? "
                "WHERE status IN ('running', 'waiting_approval') AND task_type='swarm' AND cancel_requested=0",
                (now, now),
            )
            conn.execute(
                "UPDATE tasks SET status='queued', started_at=NULL, progress='服务重启后恢复排队', "
                "error='interrupted by service restart', updated_at=? "
                "WHERE status IN ('running', 'waiting_approval') AND task_type!='swarm' AND cancel_requested=0",
                (now,),
            )
            conn.execute(
                "UPDATE tasks SET status='cancelled', finished_at=?, updated_at=? "
                "WHERE status IN ('running', 'waiting_approval', 'queued') AND cancel_requested=1",
                (now, now),
            )
            rows = conn.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY created_at").fetchall()
        return [row["id"] for row in rows]


class TaskContext:
    def __init__(self, store, task_id):
        self.store = store
        self.task_id = task_id

    def is_cancelled(self):
        return self.store.is_cancel_requested(self.task_id)

    def check_cancelled(self):
        if self.is_cancelled():
            raise TaskCancelled("cancelled by user")

    def progress(self, text):
        self.check_cancelled()
        self.store.progress(self.task_id, text)


class TaskController:
    def __init__(self, max_workers=2, store=None):
        self.store = store or TaskStore()
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="feishu-task")
        self.runner = None
        self._scheduled = set()
        self._lock = threading.Lock()

    def start(self, runner):
        self.runner = runner
        for task_id in self.store.recover():
            self._schedule(task_id)

    def submit(self, task_type, chat_id, user_id, message_id, payload):
        task = self.store.create(task_type, chat_id, user_id, message_id, payload)
        self._schedule(task["id"])
        return task

    def retry(self, task_id, message_id=None):
        task = self.store.retry(task_id, message_id=message_id)
        if task:
            self._schedule(task["id"])
        return task

    def _schedule(self, task_id):
        with self._lock:
            if task_id in self._scheduled:
                return
            self._scheduled.add(task_id)
        self.executor.submit(self._run, task_id)

    def _run(self, task_id):
        try:
            if not self.store.claim(task_id):
                return
            task = self.store.get(task_id)
            context = TaskContext(self.store, task_id)
            context.check_cancelled()
            result = self.runner(task, context)
            context.check_cancelled()
            self.store.finish(task_id, "succeeded", result=result)
        except TaskCancelled as exc:
            self.store.finish(task_id, "cancelled", error=str(exc))
        except Exception as exc:
            self.store.finish(task_id, "failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._scheduled.discard(task_id)
