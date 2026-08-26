"""Persistent per-chat FIFO task state machine with approval parking."""

import json
import hashlib
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from control_store import record_task_event as _record_task_event
from conversation_store import DB_PATH
from settings import runtime_value
from workspace_lease import release_workspace_leases


WORK_ROOT = os.path.join(runtime_value("workspace_dir"), "tasks")
TERMINAL_STATES = {"succeeded", "failed", "cancelled", "needs_review", "blocked"}
ACTIVE_STATES = {"queued", "running", "waiting_approval"}
WORKSPACE_WRITE_LOCK = threading.Lock()


def _table_exists(conn, table, column=None):
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if not cols:
        return False
    return column is None or column in cols


def collaboration_handoff_valid(payload):
    """A swarm task exists only after an owner explicitly confirms a meeting handoff."""
    payload = payload or {}
    receipt = payload.get("collaboration_confirmation") or {}
    required = (
        payload.get("workflow_id"), payload.get("meeting_task_id"),
        receipt.get("workflow_id"), receipt.get("meeting_task_id"),
        receipt.get("confirmed_by"), receipt.get("confirmation_message_id"),
        receipt.get("confirmed_at"),
    )
    return bool(
        all(required)
        and receipt.get("workflow_id") == payload.get("workflow_id")
        and receipt.get("meeting_task_id") == payload.get("meeting_task_id")
    )


def _stable_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def workspace_fingerprint(root=None):
    """Hash all Git-visible content, including pre-existing dirty and untracked files."""
    root = os.path.abspath(root or os.path.dirname(__file__))
    proc = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"],
                          cwd=root, capture_output=True, check=False)
    if proc.returncode != 0:
        return None
    digest = hashlib.sha256()
    for raw in sorted(item for item in proc.stdout.split(b"\0") if item):
        relative = raw.decode("utf-8", "surrogateescape").replace("/", os.sep)
        path = os.path.join(root, relative)
        digest.update(raw + b"\0")
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def approval_receipt_valid(task):
    """Verify that the approved plan and constraint envelope were not replaced later."""
    if not task or not task.get("approved_at"):
        return False
    if not task.get("plan_hash") and not task.get("constraint_hash"):
        return True  # compatibility for tasks approved before receipt migration
    plan = task.get("plan") or {}
    payload = task.get("payload") or {}
    constraint = plan.get("constraint_envelope") or payload.get("constraint_envelope") or {}
    hashes_valid = (task.get("plan_hash") == _stable_hash(plan)
                    and task.get("constraint_hash") == _stable_hash(constraint))
    baseline = task.get("workspace_baseline_hash")
    return hashes_valid and (not baseline or baseline == workspace_fingerprint())


def record_task_event(*args, **kwargs):
    """Keep task audit rows in the same database as the task (also isolates tests)."""
    return _record_task_event(*args, db_path=DB_PATH, **kwargs)


class TaskCancelled(Exception):
    pass


class TaskParked(Exception):
    pass


class TaskNeedsReview(Exception):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result


class TaskBlocked(Exception):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        from sqlite_compat import set_journal_mode
        set_journal_mode(conn)
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
        "approved_by": "TEXT", "approval_message_id": "TEXT",
        "plan_hash": "TEXT", "constraint_hash": "TEXT",
        "workspace_baseline_hash": "TEXT",
    }
    for name, ddl in migrations.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_chat_created ON tasks(chat_id, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS task_checkpoints (
        task_id TEXT NOT NULL, checkpoint TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL, PRIMARY KEY(task_id, checkpoint)
    )""")
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
    def checkpoint(self, task_id, checkpoint, details=None):
        now = time.time()
        with _connect() as conn:
            conn.execute("""INSERT INTO task_checkpoints(task_id, checkpoint, details_json, created_at)
                VALUES(?,?,?,?) ON CONFLICT(task_id, checkpoint) DO UPDATE SET
                details_json=excluded.details_json, created_at=excluded.created_at""",
                (task_id, checkpoint, json.dumps(details or {}, ensure_ascii=False), now))
        record_task_event(task_id, "checkpoint", details={"checkpoint": checkpoint, **(details or {})})

    def checkpoints(self, task_id):
        with _connect() as conn:
            rows = conn.execute("""SELECT checkpoint, details_json, created_at FROM task_checkpoints
                WHERE task_id=? ORDER BY created_at""", (task_id,)).fetchall()
        return [{"checkpoint": row["checkpoint"], "details": json.loads(row["details_json"] or "{}"),
                 "created_at": row["created_at"]} for row in rows]

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

    def weekly_stats(self, days=7):
        """P3c 周报统计：近 N 天任务成功率（按类型）+ 引擎耗时分布。"""
        cutoff = time.time() - days * 86400
        with _connect() as conn:
            by_type = conn.execute(
                """SELECT task_type, status, COUNT(*) AS n FROM tasks
                   WHERE created_at >= ? AND task_type != 'chat'
                   GROUP BY task_type, status""",
                (cutoff,),
            ).fetchall()
            durations = conn.execute(
                """SELECT engine, COUNT(*) AS n, AVG(duration_ms) AS avg_ms, MAX(duration_ms) AS max_ms
                   FROM trace_spans
                   WHERE created_at >= ? AND duration_ms IS NOT NULL
                   GROUP BY engine""",
                (cutoff,),
            ).fetchall() if _table_exists(conn, "trace_spans", "duration_ms") else []
            meetings = conn.execute(
                """SELECT status, COUNT(*) AS n FROM sessions
                   WHERE created_at >= datetime(?, 'unixepoch')
                   GROUP BY status""",
                (cutoff,),
            ).fetchall() if _table_exists(conn, "sessions") else []
        stats = {"by_type": {}, "engines": [], "meetings": {}}
        for row in by_type:
            stats["by_type"].setdefault(row["task_type"], {})[row["status"]] = row["n"]
        for row in durations:
            stats["engines"].append(dict(row))
        for row in meetings:
            stats["meetings"][row["status"]] = row["n"]
        return stats

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

    def approve(self, task_id, approved_by=None, approval_message_id=None):
        self.expire_approvals()
        now = time.time()
        with _connect() as conn:
            row = conn.execute(
                "SELECT status, approved_at, plan_json, payload_json FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if not row:
                return "not_found"
            if row["approved_at"] is not None:
                return "already_approved"
            if row["status"] != "waiting_approval":
                return "not_waiting"
            plan = json.loads(row["plan_json"] or "{}")
            payload = json.loads(row["payload_json"] or "{}")
            if not collaboration_handoff_valid(payload):
                return "missing_collaboration_confirmation"
            constraint = plan.get("constraint_envelope") or payload.get("constraint_envelope") or {}
            plan_hash, constraint_hash = _stable_hash(plan), _stable_hash(constraint)
            baseline_hash = workspace_fingerprint()
            cursor = conn.execute("""UPDATE tasks SET status='queued', phase='execute', approved_at=?,
                approved_by=?, approval_message_id=?, plan_hash=?, constraint_hash=?,
                workspace_baseline_hash=?,
                started_at=NULL, progress='已批准，重新排队', updated_at=?
                WHERE id=? AND status='waiting_approval' AND approved_at IS NULL""",
                (now, approved_by, approval_message_id, plan_hash, constraint_hash,
                 baseline_hash, now, task_id))
        if not cursor.rowcount:
            return "already_approved"
        record_task_event(task_id, "approved", "waiting_approval", "queued", details={
            "approved_by": approved_by, "approval_message_id": approval_message_id,
            "plan_hash": plan_hash, "constraint_hash": constraint_hash,
            "workspace_baseline_hash": baseline_hash,
        })
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
        if old["task_type"] == "swarm" and not collaboration_handoff_valid(old.get("payload")):
            return None
        reuse_approval = bool(old["task_type"] == "swarm" and old.get("approved_at")
                              and old.get("plan") and approval_receipt_valid(old))
        if reuse_approval and not message_id:
            return None
        retry = self.create(
            old["task_type"], old["chat_id"], old["user_id"], message_id or old["message_id"],
            old["payload"], retry_of=old["id"], attempt=old["attempt"] + 1,
            phase="execute" if reuse_approval else None,
            plan=old["plan"] if reuse_approval else None,
            approved_at=time.time() if reuse_approval else None,
        )
        if reuse_approval:
            with _connect() as conn:
                conn.execute("""UPDATE tasks SET approved_by=?, approval_message_id=?,
                    plan_hash=?, constraint_hash=?, workspace_baseline_hash=? WHERE id=?""", (
                    old.get("approved_by"), message_id,
                    old.get("plan_hash"), old.get("constraint_hash"),
                    old.get("workspace_baseline_hash"), retry["id"],
                ))
            record_task_event(retry["id"], "retry_reused_approval", details={"source_task_id": old["id"]})
            retry = self.get(retry["id"])
        elif old["task_type"] == "swarm" and old.get("approved_at"):
            record_task_event(retry["id"], "retry_approval_invalidated",
                              details={"source_task_id": old["id"]})
        return retry

    def recover(self):
        self.expire_approvals()
        now = time.time()
        with _connect() as conn:
            interrupted = conn.execute("""SELECT id, task_type, phase, payload_json FROM tasks WHERE status='running'
                AND cancel_requested=0""").fetchall()
            recovery_states = {}
            for row in interrupted:
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except json.JSONDecodeError:
                    payload = {}
                report_replay = (
                    row["task_type"] == "swarm" and row["phase"] == "execute"
                    and payload.get("operation_mode") == "read_only_report"
                )
                if row["task_type"] == "swarm" and row["phase"] == "execute" and not report_replay:
                    conn.execute("""UPDATE tasks SET status='blocked', finished_at=?,
                        progress='写入阶段被服务重启中断，等待人工重试',
                        error='interrupted during approved write phase; automatic replay disabled', updated_at=?
                        WHERE id=?""", (now, now, row["id"]))
                    recovery_states[row["id"]] = "blocked_write"
                else:
                    progress = ("只读报告在服务重启后恢复排队" if report_replay
                                else "服务重启后恢复排队")
                    error = ("interrupted by service restart; read-only report will resume idempotently"
                             if report_replay else
                             "interrupted by service restart; isolated execution will replay safely")
                    conn.execute("""UPDATE tasks SET status='queued', started_at=NULL, finished_at=NULL,
                        progress=?, error=?, updated_at=? WHERE id=?""",
                        (progress, error, now, row["id"]))
                    recovery_states[row["id"]] = "requeued_report" if report_replay else "requeued"
            conn.execute("""UPDATE tasks SET status='cancelled', finished_at=?, updated_at=?
                WHERE status IN ('running','queued') AND cancel_requested=1""", (now, now))
            queued = conn.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY created_at").fetchall()
        release_workspace_leases((row["id"] for row in interrupted), db_path=DB_PATH)
        for row in interrupted:
            recovery = recovery_states[row["id"]]
            if recovery == "blocked_write":
                record_task_event(row["id"], "recovery_blocked_write", "running", "blocked")
            elif recovery == "requeued_report":
                record_task_event(row["id"], "recovery_requeued_report", "running", "queued")
            else:
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

    def checkpoint(self, name, details=None):
        self.check_cancelled()
        self.store.checkpoint(self.task_id, name, details)

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
        if task_type == "swarm" and not collaboration_handoff_valid(payload):
            raise ValueError("协作任务必须来自已拍板会议的明确开始协作确认")
        task = self.store.create(task_type, chat_id, user_id, message_id, payload)
        self._schedule_chat(chat_id)
        return task

    def retry(self, task_id, message_id=None):
        task = self.store.retry(task_id, message_id)
        if task:
            self._schedule_chat(task["chat_id"])
        return task

    def approve(self, task_id, approved_by=None, approval_message_id=None):
        outcome = self.store.approve(task_id, approved_by=approved_by,
                                     approval_message_id=approval_message_id)
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
        except TaskNeedsReview as exc:
            self.store.finish(task_id, "needs_review", result=exc.result, error=str(exc))
        except TaskBlocked as exc:
            self.store.finish(task_id, "blocked", result=exc.result, error=str(exc))
        except TaskCancelled as exc:
            self.store.finish(task_id, "cancelled", error=str(exc))
        except Exception as exc:
            self.store.finish(task_id, "failed", error=f"{type(exc).__name__}: {exc}")
