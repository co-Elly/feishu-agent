"""Persistent per-chat FIFO task state machine with approval parking."""

import json
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from control_store import record_task_event as _record_task_event
from conversation_store import DB_PATH
from settings import runtime_value


WORK_ROOT = os.path.join(runtime_value("workspace_dir"), "tasks")
TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
ACTIVE_STATES = {"queued", "running", "waiting_approval"}
WORKSPACE_WRITE_LOCK = threading.Lock()


def record_task_event(*args, **kwargs):
    """Keep task audit rows in the same database as the task (also isolates tests)."""
    return _record_task_event(*args, db_path=DB_PATH, **kwargs)


class TaskCancelled(Exception):
    pass


class TaskParked(Exception):
    pass


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
    conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY, task_type TEXT NOT NULL, status TEXT NOT NULL,
        chat_id TEXT NOT NULL, user_id TEXT, message_id TEXT,
        payload_json TEXT NOT NULL, result_json TEXT, error TEXT, progress TEXT,
        work_dir TEXT NOT NULL, retry_of TEXT, attempt INTEGER NOT NULL DEFAULT 1,
        cancel_requested INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
        started_at REAL, finished_at REAL, updated_at REAL NOT NULL,
        phase TEXT NOT NULL DEFAULT 'execute', plan_json TEXT,
        approval_expires_at REAL, approved_at REAL
    )""")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    migrations = {
        "phase": "TEXT NOT NULL DEFAULT 'execute'", "plan_json": "TEXT",
        "approval_expires_at": "REAL", "approved_at": "REAL",
    }
    for name, ddl in migrations.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_chat_created ON tasks(chat_id, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at)")
    return conn


def _row(row):
    if not row:
        return None
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json") or "{}")
    raw_result, raw_plan = item.pop("result_json"), item.pop("plan_json")
    item["result"] = json.loads(raw_result) if raw_result else None
    item["plan"] = json.loads(raw_plan) if raw_plan else None
    item["cancel_requested"] = bool(item["cancel_requested"])
    return item


def _write_snapshot(task):
    if not task:
        return
    os.makedirs(task["work_dir"], exist_ok=True)
    path, temp = os.path.join(task["work_dir"], "task.json"), os.path.join(task["work_dir"], "task.json.tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(task, handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)


class TaskStore:
    def create(self, task_type, chat_id, user_id, message_id, payload, retry_of=None, attempt=1,
               phase=None, plan=None, approved_at=None):
        task_id, now = uuid.uuid4().hex[:10], time.time()
        work_dir = os.path.join(WORK_ROOT, task_id)
        os.makedirs(work_dir, exist_ok=False)
        phase = phase or ("planning" if task_type == "swarm" else "execute")
        with _connect() as conn:
            conn.execute("""INSERT INTO tasks(
                id, task_type, status, chat_id, user_id, message_id, payload_json,
                work_dir, retry_of, attempt, created_at, updated_at, phase, plan_json, approved_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, task_type, "queued", chat_id, user_id, message_id,
             json.dumps(payload, ensure_ascii=False), work_dir, retry_of, attempt, now, now, phase,
             json.dumps(plan, ensure_ascii=False) if plan is not None else None, approved_at))
        record_task_event(task_id, "created", to_status="queued", details={"task_type": task_type, "phase": phase})
        task = self.get(task_id)
        _write_snapshot(task)
        return task

    def get(self, task_id):
        with _connect() as conn:
            return _row(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def find(self, prefix, chat_id=None):
        with _connect() as conn:
            sql = "SELECT * FROM tasks WHERE id LIKE ?"
            params = [prefix + "%"]
            if chat_id:
                sql += " AND chat_id=?"
                params.append(chat_id)
            rows = conn.execute(sql + " ORDER BY created_at DESC LIMIT 2", params).fetchall()
        return _row(rows[0]) if len(rows) == 1 else None

    def list(self, chat_id=None, limit=10, include_all=False):
        clauses, params = [], []
        if chat_id:
            clauses.append("chat_id=?")
            params.append(chat_id)
        if not include_all:
            clauses.append("NOT(task_type='chat' AND status='succeeded')")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with _connect() as conn:
            rows = conn.execute(f"SELECT * FROM tasks{where} ORDER BY created_at DESC LIMIT ?", params + [limit]).fetchall()
        return [_row(row) for row in rows]

    def counts(self):
        with _connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}

    def next_queued(self, chat_id):
        with _connect() as conn:
            row = conn.execute("""SELECT * FROM tasks
                WHERE chat_id=? AND status='queued' AND cancel_requested=0
                ORDER BY COALESCE(approved_at, created_at), created_at LIMIT 1""", (chat_id,)).fetchone()
        return _row(row)

    def queued_chats(self):
        with _connect() as conn:
            return [row[0] for row in conn.execute("SELECT DISTINCT chat_id FROM tasks WHERE status='queued'")]

    def claim(self, task_id):
        now = time.time()
        with _connect() as conn:
            old = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            cursor = conn.execute("""UPDATE tasks SET status='running', started_at=?, updated_at=?
                WHERE id=? AND status='queued' AND cancel_requested=0""", (now, now, task_id))
        if cursor.rowcount:
            record_task_event(task_id, "transition", old["status"], "running")
        return cursor.rowcount == 1

    def progress(self, task_id, text):
        with _connect() as conn:
            conn.execute("UPDATE tasks SET progress=?, updated_at=? WHERE id=?", (text[:500], time.time(), task_id))
        _write_snapshot(self.get(task_id))

    def wait_for_approval(self, task_id, plan, ttl_seconds=None):
        ttl = int(ttl_seconds or runtime_value("approval_ttl_seconds"))
        now = time.time()
        with _connect() as conn:
            cursor = conn.execute("""UPDATE tasks SET status='waiting_approval', phase='approval',
                plan_json=?, approval_expires_at=?, started_at=NULL, progress='等待写入批准', updated_at=?
                WHERE id=? AND status='running' AND phase='planning'""",
                (json.dumps(plan, ensure_ascii=False), now + ttl, now, task_id))
        if cursor.rowcount:
            record_task_event(task_id, "approval_requested", "running", "waiting_approval", details={"ttl_seconds": ttl})
            _write_snapshot(self.get(task_id))
        return cursor.rowcount == 1

    def expire_approvals(self):
        now = time.time()
        with _connect() as conn:
            rows = conn.execute("SELECT id FROM tasks WHERE status='waiting_approval' AND approval_expires_at<=?", (now,)).fetchall()
            conn.execute("""UPDATE tasks SET status='cancelled', error='approval expired', finished_at=?, updated_at=?
                WHERE status='waiting_approval' AND approval_expires_at<=?""", (now, now, now))
        for row in rows:
            record_task_event(row["id"], "approval_expired", "waiting_approval", "cancelled")
            _write_snapshot(self.get(row["id"]))
        return [row["id"] for row in rows]

    def approve(self, task_id):
        self.expire_approvals()
        now = time.time()
        with _connect() as conn:
            row = conn.execute("SELECT status, approved_at FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return "not_found"
            if row["approved_at"] is not None:
                return "already_approved"
            if row["status"] != "waiting_approval":
                return "not_waiting"
            cursor = conn.execute("""UPDATE tasks SET status='queued', phase='execute', approved_at=?,
                started_at=NULL, progress='已批准，重新排队', updated_at=?
                WHERE id=? AND status='waiting_approval' AND approved_at IS NULL""", (now, now, task_id))
        if not cursor.rowcount:
            return "already_approved"
        record_task_event(task_id, "approved", "waiting_approval", "queued")
        _write_snapshot(self.get(task_id))
        return "approved"

    def finish(self, task_id, status, result=None, error=None):
        if status not in TERMINAL_STATES:
            raise ValueError(f"invalid terminal status: {status}")
        now = time.time()
        with _connect() as conn:
            old = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            conn.execute("""UPDATE tasks SET status=?, result_json=?, error=?, finished_at=?, updated_at=? WHERE id=?""",
                (status, json.dumps(result, ensure_ascii=False) if result is not None else None, error, now, now, task_id))
        record_task_event(task_id, "transition", old["status"] if old else None, status, details={"error": (error or "")[:300]})
        _write_snapshot(self.get(task_id))

    def request_cancel(self, task_id):
        event = None
        with _connect() as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["status"] in TERMINAL_STATES:
                return False
            now = time.time()
            if row["status"] in {"queued", "waiting_approval"}:
                conn.execute("""UPDATE tasks SET status='cancelled', cancel_requested=1,
                    finished_at=?, updated_at=? WHERE id=?""", (now, now, task_id))
                event = ("cancelled", row["status"], "cancelled")
            else:
                conn.execute("UPDATE tasks SET cancel_requested=1, updated_at=? WHERE id=?", (now, task_id))
                event = ("cancel_requested", row["status"], row["status"])
        record_task_event(task_id, *event)
        _write_snapshot(self.get(task_id))
        return True

    def is_cancel_requested(self, task_id):
        with _connect() as conn:
            row = conn.execute("SELECT cancel_requested FROM tasks WHERE id=?", (task_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def retry(self, task_id, message_id=None):
        old = self.get(task_id)
        if not old or old["status"] not in TERMINAL_STATES:
            return None
        reuse_approval = bool(old["task_type"] == "swarm" and old.get("approved_at") and old.get("plan"))
        retry = self.create(
            old["task_type"], old["chat_id"], old["user_id"], message_id or old["message_id"],
            old["payload"], retry_of=old["id"], attempt=old["attempt"] + 1,
            phase="execute" if reuse_approval else None,
            plan=old["plan"] if reuse_approval else None,
            approved_at=time.time() if reuse_approval else None,
        )
        if reuse_approval:
            record_task_event(retry["id"], "retry_reused_approval", details={"source_task_id": old["id"]})
        return retry

    def recover(self):
        self.expire_approvals()
        now = time.time()
        with _connect() as conn:
            failed = conn.execute("""SELECT id FROM tasks WHERE status='running'
                AND task_type='swarm' AND phase='execute' AND cancel_requested=0""").fetchall()
            conn.execute("""UPDATE tasks SET status='failed', finished_at=?,
                error='service restarted during workspace write; manual retry required', updated_at=?
                WHERE status='running' AND task_type='swarm' AND phase='execute' AND cancel_requested=0""", (now, now))
            replay = conn.execute("""SELECT id FROM tasks WHERE status='running'
                AND NOT(task_type='swarm' AND phase='execute') AND cancel_requested=0""").fetchall()
            conn.execute("""UPDATE tasks SET status='queued', started_at=NULL, progress='服务重启后恢复排队',
                error='interrupted by service restart', updated_at=? WHERE status='running'
                AND NOT(task_type='swarm' AND phase='execute') AND cancel_requested=0""", (now,))
            conn.execute("""UPDATE tasks SET status='cancelled', finished_at=?, updated_at=?
                WHERE status IN ('running','queued') AND cancel_requested=1""", (now, now))
            queued = conn.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY created_at").fetchall()
        for row in failed:
            record_task_event(row["id"], "recovery_failed_write", "running", "failed")
        for row in replay:
            record_task_event(row["id"], "recovery_requeued", "running", "queued")
        return [row["id"] for row in queued]


class TaskContext:
    def __init__(self, store, task_id):
        self.store, self.task_id = store, task_id

    def is_cancelled(self):
        return self.store.is_cancel_requested(self.task_id)

    def check_cancelled(self):
        if self.is_cancelled():
            raise TaskCancelled("cancelled by user")

    def progress(self, text):
        self.check_cancelled()
        self.store.progress(self.task_id, text)

    def wait_for_approval(self, plan):
        self.check_cancelled()
        if not self.store.wait_for_approval(self.task_id, plan):
            raise RuntimeError("任务无法进入审批状态")
        raise TaskParked("waiting for approval")


class TaskController:
    def __init__(self, max_workers=4, store=None):
        self.store = store or TaskStore()
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="feishu-chat")
        self.runner, self._scheduled_chats, self._lock = None, set(), threading.Lock()

    def start(self, runner):
        self.runner = runner
        self.store.recover()
        for chat_id in self.store.queued_chats():
            self._schedule_chat(chat_id)

    def submit(self, task_type, chat_id, user_id, message_id, payload):
        task = self.store.create(task_type, chat_id, user_id, message_id, payload)
        self._schedule_chat(chat_id)
        return task

    def retry(self, task_id, message_id=None):
        task = self.store.retry(task_id, message_id)
        if task:
            self._schedule_chat(task["chat_id"])
        return task

    def approve(self, task_id):
        outcome = self.store.approve(task_id)
        if outcome == "approved":
            self._schedule_chat(self.store.get(task_id)["chat_id"])
        return outcome

    def shutdown(self, wait=True):
        self.executor.shutdown(wait=wait)

    def _schedule_chat(self, chat_id):
        with self._lock:
            if chat_id in self._scheduled_chats:
                return
            self._scheduled_chats.add(chat_id)
        self.executor.submit(self._run_chat, chat_id)

    def _run_chat(self, chat_id):
        while True:
            task = self.store.next_queued(chat_id)
            if not task:
                with self._lock:
                    task = self.store.next_queued(chat_id)
                    if not task:
                        self._scheduled_chats.discard(chat_id)
                        return
            self._run_task(task["id"])

    def _run_task(self, task_id):
        if not self.store.claim(task_id):
            return
        try:
            task, context = self.store.get(task_id), TaskContext(self.store, task_id)
            context.check_cancelled()
            result = self.runner(task, context)
            context.check_cancelled()
            self.store.finish(task_id, "succeeded", result=result)
        except TaskParked:
            pass
        except TaskCancelled as exc:
            self.store.finish(task_id, "cancelled", error=str(exc))
        except Exception as exc:
            self.store.finish(task_id, "failed", error=f"{type(exc).__name__}: {exc}")
