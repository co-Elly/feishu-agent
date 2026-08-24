from observability import classify_failure, record_span, trace_spans


def test_failure_taxonomy_is_stable():
    assert classify_failure("调用超时", "timeout") == "ENGINE_TIMEOUT"
    assert classify_failure("批准后的方案发生变化") == "APPROVAL_MISMATCH"
    assert classify_failure("隔离违规：修改越界") == "SCOPE_VIOLATION"
    assert classify_failure("工作区租约已丢失") == "WORKSPACE_CONFLICT"


def test_trace_span_redacts_sensitive_metadata(tmp_path):
    db = str(tmp_path / "trace.db")
    record_span("task-1", "codex", "error", engine="codex", duration_ms=12,
                failure_category="ENGINE_FAILURE",
                metadata={"error_code": "network", "token": "secret"}, db_path=db)
    rows = trace_spans("task-1", db_path=db)
    assert rows[0]["span_name"] == "codex"
    assert "secret" not in rows[0]["metadata_json"]
