"""Local trace spans and stable multi-agent failure taxonomy."""

import json
import sqlite3
import time

from conversation_store import DB_PATH


FAILURE_CATEGORIES = {
    "SPEC_VIOLATION", "ROLE_VIOLATION", "STEP_REPETITION", "CONTEXT_LOSS",
    "HANDOFF_MISALIGNMENT", "NO_PROGRESS", "PREMATURE_TERMINATION",
    "INCOMPLETE_VERIFICATION", "SCOPE_VIOLATION", "APPROVAL_MISMATCH",
    "ENGINE_TIMEOUT", "WORKSPACE_CONFLICT", "ENGINE_FAILURE", "UNKNOWN",
}


def classify_failure(text, error_code=None):
    lower = str(text or "").lower()
    if error_code == "timeout" or "timeout" in lower or "超时" in lower:
        return "ENGINE_TIMEOUT"
    if "批准" in lower and any(token in lower for token in ("变化", "缺少", "无效")):
        return "APPROVAL_MISMATCH"
    if any(token in lower for token in ("scope", "越界", "隔离违规", "禁止修改")):
        return "SCOPE_VIOLATION"
    if any(token in lower for token in ("lease", "租约", "独占写入", "workspace conflict")):
        return "WORKSPACE_CONFLICT"
    if any(token in lower for token in ("没有进展", "no progress", "固定点")):
        return "NO_PROGRESS"
    if any(token in lower for token in ("验收不完整", "未验证", "incomplete verification")):
        return "INCOMPLETE_VERIFICATION"
    if error_code:
        return "ENGINE_FAILURE"
    return "UNKNOWN"


def _connect(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""CREATE TABLE IF NOT EXISTS trace_spans (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL,
        span_name TEXT NOT NULL, engine TEXT, status TEXT NOT NULL,
        duration_ms INTEGER, failure_category TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trace_spans_trace ON trace_spans(trace_id, id)")
    return conn


def record_span(trace_id, span_name, status, engine=None, duration_ms=None,
                failure_category=None, metadata=None, db_path=None):
    if failure_category and failure_category not in FAILURE_CATEGORIES:
        raise ValueError(f"unknown failure category: {failure_category}")
    safe = {key: value for key, value in (metadata or {}).items()
            if key.lower() not in {"prompt", "secret", "token", "api_key", "authorization"}}
    with _connect(db_path) as conn:
        conn.execute("""INSERT INTO trace_spans(trace_id, span_name, engine, status,
            duration_ms, failure_category, metadata_json, created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (trace_id, span_name, engine, status, duration_ms, failure_category,
             json.dumps(safe, ensure_ascii=False), time.time()))


def trace_spans(trace_id, db_path=None):
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM trace_spans WHERE trace_id=? ORDER BY id",
                            (trace_id,)).fetchall()
    return [dict(row) for row in rows]


def ensure_observability_schema(db_path=None):
    with _connect(db_path):
        pass
