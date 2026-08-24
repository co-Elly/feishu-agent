import json
from pathlib import Path

import service_health


def test_guardian_status_reads_structured_restart_evidence(monkeypatch, tmp_path):
    status = {
        "state": "restarting", "exit_code": 1,
        "exit_reason": "crash_or_external_stop", "restarts_last_hour": 2,
    }
    (tmp_path / "guardian_status.json").write_text(
        json.dumps(status), encoding="utf-8",
    )
    monkeypatch.setattr(service_health, "runtime_value", lambda _name: str(tmp_path))
    assert service_health.guardian_status() == status


def test_guardian_status_tolerates_missing_or_invalid_file(monkeypatch, tmp_path):
    monkeypatch.setattr(service_health, "runtime_value", lambda _name: str(tmp_path))
    assert service_health.guardian_status() == {}
    Path(tmp_path, "guardian_status.json").write_text("not-json", encoding="utf-8")
    assert service_health.guardian_status() == {}
