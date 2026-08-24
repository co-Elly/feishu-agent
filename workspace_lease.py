"""SQLite-backed cross-process lease for the single writable workspace."""

import contextlib
import os
import sqlite3
import time
import uuid

from conversation_store import DB_PATH


class WorkspaceLeaseBusy(RuntimeError):
    pass


class WorkspaceLease:
    def __init__(self, owner_task_id, db_path=None, resource="main-workspace", ttl_seconds=3600):
        self.owner_task_id = str(owner_task_id or "unknown")
        self.db_path = db_path or DB_PATH
        self.resource = resource
        self.ttl_seconds = int(ttl_seconds)
        self.token = uuid.uuid4().hex

    def _connect(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""CREATE TABLE IF NOT EXISTS workspace_leases (
            resource TEXT PRIMARY KEY, owner_task_id TEXT NOT NULL,
            lease_token TEXT NOT NULL, acquired_at REAL NOT NULL, expires_at REAL NOT NULL
        )""")
        return conn

    def acquire(self):
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM workspace_leases WHERE expires_at<=?", (now,))
            try:
                conn.execute("""INSERT INTO workspace_leases(
                    resource, owner_task_id, lease_token, acquired_at, expires_at
                ) VALUES(?,?,?,?,?)""",
                (self.resource, self.owner_task_id, self.token, now, now + self.ttl_seconds))
            except sqlite3.IntegrityError as exc:
                row = conn.execute(
                    "SELECT owner_task_id, expires_at FROM workspace_leases WHERE resource=?",
                    (self.resource,),
                ).fetchone()
                conn.rollback()
                owner = row[0] if row else "unknown"
                raise WorkspaceLeaseBusy(f"工作区正由任务 {owner} 独占写入") from exc
            conn.commit()
        return self

    def renew(self):
        with self._connect() as conn:
            cursor = conn.execute("""UPDATE workspace_leases SET expires_at=?
                WHERE resource=? AND lease_token=?""",
                (time.time() + self.ttl_seconds, self.resource, self.token))
        if cursor.rowcount != 1:
            raise WorkspaceLeaseBusy("工作区写入租约已丢失")

    def release(self):
        with self._connect() as conn:
            conn.execute("DELETE FROM workspace_leases WHERE resource=? AND lease_token=?",
                         (self.resource, self.token))

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_exc):
        self.release()


def release_workspace_leases(owner_task_ids, db_path=None):
    """Release leases left by tasks interrupted before their context exited."""
    owners = [str(task_id) for task_id in owner_task_ids if task_id]
    if not owners:
        return 0
    lease = WorkspaceLease("recovery", db_path=db_path)
    placeholders = ",".join("?" for _ in owners)
    with lease._connect() as conn:
        cursor = conn.execute(
            f"DELETE FROM workspace_leases WHERE owner_task_id IN ({placeholders})", owners
        )
    return cursor.rowcount


@contextlib.contextmanager
def workspace_write_lease(owner_task_id, **kwargs):
    lease = WorkspaceLease(owner_task_id, **kwargs)
    with lease:
        yield lease


def ensure_lease_schema(db_path=None):
    lease = WorkspaceLease("schema-init", db_path=db_path)
    with lease._connect():
        pass
