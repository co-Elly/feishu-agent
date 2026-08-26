"""Durable business workflow state layered above individual background tasks."""

import json
import os
import sqlite3
import time
import uuid

from conversation_store import DB_PATH


STATES = {
    "meeting_discussion", "awaiting_boss_decision", "meeting_continuation",
    "awaiting_collaboration_confirmation", "planning", "waiting_approval",
    "executing", "verifying", "completed", "cancelled", "failed",
}

ALLOWED_TRANSITIONS = {
    "meeting_discussion": {"awaiting_boss_decision", "failed", "cancelled"},
    "awaiting_boss_decision": {"meeting_continuation", "awaiting_collaboration_confirmation", "cancelled"},
    "meeting_continuation": {"awaiting_boss_decision", "awaiting_collaboration_confirmation", "failed", "cancelled"},
    "awaiting_collaboration_confirmation": {"meeting_continuation", "planning", "cancelled"},
    "planning": {"waiting_approval", "failed", "cancelled"},
    "waiting_approval": {"executing", "cancelled"},
    "executing": {"verifying", "failed", "cancelled"},
    "verifying": {"completed", "failed"},
    "completed": set(), "cancelled": set(), "failed": set(),
}

TERMINAL_STATES = {"completed", "cancelled", "failed"}


class WorkflowTransitionError(RuntimeError):
    pass


class WorkflowStore:
    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH

    def _connect(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""CREATE TABLE IF NOT EXISTS workflow_instances (
            id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, owner_user_id TEXT,
            project_name TEXT, state TEXT NOT NULL, root_request TEXT NOT NULL,
            constraint_json TEXT NOT NULL, current_task_id TEXT,
            decision_round INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        )""")
        existing = {row[1] for row in conn.execute("PRAGMA table_info(workflow_instances)")}
        for name in ("task_ledger_json", "progress_ledger_json"):
            if name not in existing:
                conn.execute(f"ALTER TABLE workflow_instances ADD COLUMN {name} TEXT NOT NULL DEFAULT '{{}}'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_chat_updated ON workflow_instances(chat_id, updated_at DESC)")
        return conn

    def ensure_schema(self):
        with self._connect():
            pass

    @staticmethod
    def _row(row):
        if not row:
            return None
        item = dict(row)
        item["constraint_envelope"] = json.loads(item.pop("constraint_json") or "{}")
        item["task_ledger"] = json.loads(item.pop("task_ledger_json") or "{}")
        item["progress_ledger"] = json.loads(item.pop("progress_ledger_json") or "{}")
        return item

    def create(self, chat_id, owner_user_id, project_name, root_request, constraint_envelope):
        workflow_id, now = "wf_" + uuid.uuid4().hex[:12], time.time()
        with self._connect() as conn:
            conn.execute("""INSERT INTO workflow_instances(
                id, chat_id, owner_user_id, project_name, state, root_request,
                constraint_json, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""", (
                workflow_id, chat_id, owner_user_id, project_name, "meeting_discussion",
                root_request, json.dumps(constraint_envelope, ensure_ascii=False), now, now,
            ))
        return self.get(workflow_id)

    def get(self, workflow_id):
        with self._connect() as conn:
            return self._row(conn.execute(
                "SELECT * FROM workflow_instances WHERE id=?", (workflow_id,),
            ).fetchone())

    def active(self, chat_id, project_name=None):
        terminal = ("completed", "cancelled", "failed")
        sql = "SELECT * FROM workflow_instances WHERE chat_id=? AND state NOT IN (?,?,?)"
        params = [chat_id, *terminal]
        if project_name is not None:
            sql += " AND project_name=?"
            params.append(project_name)
        sql += " ORDER BY updated_at DESC LIMIT 1"
        with self._connect() as conn:
            return self._row(conn.execute(sql, params).fetchone())

    def transition(self, workflow_id, to_state, current_task_id=None):
        if to_state not in STATES:
            raise WorkflowTransitionError(f"未知工作流状态: {to_state}")
        now = time.time()
        with self._connect() as conn:
            row = conn.execute("SELECT state FROM workflow_instances WHERE id=?", (workflow_id,)).fetchone()
            if not row:
                raise WorkflowTransitionError("工作流不存在")
            old = row["state"]
            if to_state != old and to_state not in ALLOWED_TRANSITIONS[old]:
                raise WorkflowTransitionError(f"不允许从 {old} 转换到 {to_state}")
            increment = 1 if old == "awaiting_boss_decision" and to_state == "meeting_continuation" else 0
            conn.execute("""UPDATE workflow_instances SET state=?,
                current_task_id=COALESCE(?, current_task_id),
                decision_round=decision_round+?, updated_at=? WHERE id=?""",
                (to_state, current_task_id, increment, now, workflow_id))
        return self.get(workflow_id)

    def finish_from_task(self, workflow_id, task_status, current_task_id=None):
        """Synchronize an unexpected task terminal state without skipping the FSM."""
        target = "cancelled" if task_status == "cancelled" else "failed"
        current = self.get(workflow_id)
        if not current or current["state"] in TERMINAL_STATES:
            return current
        return self.transition(workflow_id, target, current_task_id)

    def resume_for_retry(self, workflow_id, to_state, task_id, retry_of):
        """Explicitly reopen a terminal workflow for a user-triggered task retry."""
        if to_state not in {"planning", "waiting_approval"}:
            raise WorkflowTransitionError("重试只能恢复到规划或等待批准阶段")
        current = self.get(workflow_id)
        if not current:
            raise WorkflowTransitionError("工作流不存在")
        if current["state"] not in TERMINAL_STATES:
            return current
        task_data = dict(current.get("task_ledger") or {})
        retries = list(task_data.get("retries") or [])
        retries.append({
            "task_id": task_id, "retry_of": retry_of, "resumed_to": to_state,
            "created_at": time.time(),
        })
        task_data["retries"] = retries
        with self._connect() as conn:
            conn.execute("""UPDATE workflow_instances SET state=?, current_task_id=?,
                task_ledger_json=?, updated_at=? WHERE id=?""", (
                to_state, task_id, json.dumps(task_data, ensure_ascii=False),
                time.time(), workflow_id,
            ))
        return self.get(workflow_id)

    def bind_task(self, workflow_id, task_id):
        with self._connect() as conn:
            conn.execute("UPDATE workflow_instances SET current_task_id=?, updated_at=? WHERE id=?",
                         (task_id, time.time(), workflow_id))
        return self.get(workflow_id)

    def update_ledgers(self, workflow_id, task_ledger=None, progress_ledger=None):
        current = self.get(workflow_id)
        if not current:
            raise WorkflowTransitionError("工作流不存在")
        task_data = dict(current.get("task_ledger") or {})
        progress_data = dict(current.get("progress_ledger") or {})
        task_data.update(task_ledger or {})
        progress_data.update(progress_ledger or {})
        with self._connect() as conn:
            conn.execute("""UPDATE workflow_instances SET task_ledger_json=?,
                progress_ledger_json=?, updated_at=? WHERE id=?""", (
                json.dumps(task_data, ensure_ascii=False),
                json.dumps(progress_data, ensure_ascii=False), time.time(), workflow_id,
            ))
        return self.get(workflow_id)

    def record_boss_decision(self, workflow_id, decision, user_id=None, message_id=None,
                             meeting_task_id=None):
        """Append an immutable human-decision record to the workflow ledger."""
        current = self.get(workflow_id)
        if not current:
            raise WorkflowTransitionError("工作流不存在")
        task_data = dict(current.get("task_ledger") or {})
        decisions = list(task_data.get("boss_decisions") or [])
        decisions.append({
            "sequence": len(decisions) + 1,
            "decision": str(decision or "").strip(),
            "user_id": user_id,
            "message_id": message_id,
            "meeting_task_id": meeting_task_id,
            "created_at": time.time(),
        })
        task_data["boss_decisions"] = decisions
        with self._connect() as conn:
            conn.execute(
                "UPDATE workflow_instances SET task_ledger_json=?, updated_at=? WHERE id=?",
                (json.dumps(task_data, ensure_ascii=False), time.time(), workflow_id),
            )
        return self.get(workflow_id)

    # ---- P5c: 可恢复协作快照 ----

    def export_audit_log(self, workflow_id) -> dict:
        """导出完整决策链为可审计的 JSON 文件。

        包含：工作流状态、任务清单、决策链、时间线。
        系统重启后可通过 import_audit_log 恢复状态。
        """
        wf = self.get(workflow_id)
        if not wf:
            raise WorkflowTransitionError("工作流不存在")
        task_data = wf.get("task_ledger") or {}
        progress_data = wf.get("progress_ledger") or {}
        timeline = self._build_timeline(wf, task_data, progress_data)
        return {
            "version": "1.0",
            "workflow": wf,
            "task_ledger": task_data,
            "progress_ledger": progress_data,
            "timeline": timeline,
            "decision_chain": self._build_decision_chain(task_data),
            "exported_at": time.time(),
        }

    def _build_timeline(self, wf, task_data, progress_data) -> list:
        """构建时间线：按时间排序的所有状态变更和决策。"""
        events = []
        for decision in task_data.get("boss_decisions") or []:
            events.append({
                "type": "boss_decision",
                "timestamp": decision.get("created_at", 0),
                "data": decision,
            })
        for progress in progress_data.get("progress_events") or []:
            events.append({
                "type": "progress",
                "timestamp": progress.get("created_at", 0),
                "data": progress,
            })
        events.sort(key=lambda e: e.get("timestamp", 0))
        return events

    def _build_decision_chain(self, task_data) -> list:
        """构建决策链：从会议到拍板到批准的完整链路。"""
        chain = []
        for decision in task_data.get("boss_decisions") or []:
            chain.append({
                "sequence": decision.get("sequence"),
                "decision": decision.get("decision"),
                "user_id": decision.get("user_id"),
                "meeting_task_id": decision.get("meeting_task_id"),
                "timestamp": decision.get("created_at"),
            })
        return chain

    def import_audit_log(self, data: dict) -> str:
        """从快照恢复工作流。

        验证完整性后恢复状态机，返回新 workflow_id。
        """
        wf_data = data.get("workflow") or {}
        if not wf_data.get("id"):
            raise WorkflowTransitionError("快照缺少工作流 ID")
        # 生成新 ID 避免冲突
        new_id = "wf_" + uuid.uuid4().hex[:12]
        now = time.time()
        with self._connect() as conn:
            conn.execute("""INSERT INTO workflow_instances(
                id, chat_id, owner_user_id, project_name, state, root_request,
                constraint_json, task_ledger_json, progress_ledger_json, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                new_id,
                wf_data.get("chat_id", ""),
                wf_data.get("owner_user_id"),
                wf_data.get("project_name"),
                wf_data.get("state", "meeting_discussion"),
                wf_data.get("root_request", ""),
                json.dumps(wf_data.get("constraint_envelope", {}), ensure_ascii=False),
                json.dumps(data.get("task_ledger", {}), ensure_ascii=False),
                json.dumps(data.get("progress_ledger", {}), ensure_ascii=False),
                now, now,
            ))
        return new_id

    def save_audit_snapshot(self, workflow_id, output_dir=None) -> str:
        """将审计日志保存到文件。返回文件路径。"""
        data = self.export_audit_log(workflow_id)
        if not output_dir:
            output_dir = os.path.join(os.path.dirname(self.db_path), "audit")
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, f"{workflow_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path


WORKFLOW_STORE = WorkflowStore()
