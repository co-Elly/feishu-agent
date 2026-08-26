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


# ---- P6c: 异常告警 ----

def get_recent_failures(window_seconds=300, db_path=None):
    """获取最近 N 秒内的失败记录。"""
    cutoff = time.time() - window_seconds
    with _connect(db_path) as conn:
        rows = conn.execute("""SELECT * FROM trace_spans
            WHERE status='error' AND created_at >= ?
            ORDER BY created_at DESC LIMIT 20""", (cutoff,)).fetchall()
    return [dict(row) for row in rows]


def get_engine_error_stats(window_seconds=3600, db_path=None):
    """获取各引擎最近 N 秒内的错误统计。"""
    cutoff = time.time() - window_seconds
    with _connect(db_path) as conn:
        rows = conn.execute("""SELECT engine, failure_category,
            COUNT(*) as count, MAX(created_at) as last_occurrence
            FROM trace_spans
            WHERE status='error' AND created_at >= ?
            GROUP BY engine, failure_category
            ORDER BY count DESC""", (cutoff,)).fetchall()
    return [dict(row) for row in rows]


def check_engine_degradation(window_seconds=300, threshold=3, db_path=None):
    """检查引擎是否降级（最近 N 秒内失败次数超过阈值）。"""
    stats = get_engine_error_stats(window_seconds, db_path)
    degraded = []
    for stat in stats:
        if stat["count"] >= threshold:
            degraded.append({
                "engine": stat["engine"],
                "error_count": stat["count"],
                "last_category": stat["failure_category"],
                "last_occurrence": stat["last_occurrence"],
            })
    return degraded


def format_alert_message(degraded_engines):
    """格式化为飞书告警消息。"""
    if not degraded_engines:
        return None
    lines = ["⚠️ **引擎降级告警**\n"]
    for eng in degraded_engines:
        lines.append(
            f"  🔴 `{eng['engine']}`：{eng['error_count']} 次失败"
            f"（最近：{eng['last_category']}）"
        )
    lines.append("\n建议：检查引擎状态或切换双人局模式。")
    return "\n".join(lines)
