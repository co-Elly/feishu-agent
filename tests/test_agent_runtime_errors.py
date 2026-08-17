import subprocess
from types import SimpleNamespace

import agent_runtime


def test_error_classification_and_cooldown():
    assert agent_runtime.classify_error("401 Unauthorized") == ("authentication", False)
    assert agent_runtime.classify_error("Individual quota reached") == ("quota_exhausted", False)
    assert agent_runtime.classify_error("network connection reset") == ("network", True)
    assert agent_runtime.cooldown_seconds("Resets in 1h2m3s", "quota_exhausted") == 3733


def test_network_is_retried_once(monkeypatch):
    calls = []
    def fake_run(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(returncode=1, stdout="", stderr="network connection reset")
    monkeypatch.setattr(agent_runtime.subprocess, "run", fake_run)
    result = agent_runtime.call_codex("probe")
    assert not result.ok and result.error_code == "network" and result.retryable
    assert len(calls) == 2


def test_auth_and_missing_command_are_not_retried(monkeypatch):
    calls = []
    def auth(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(returncode=1, stdout="", stderr="401 Unauthorized")
    monkeypatch.setattr(agent_runtime.subprocess, "run", auth)
    assert agent_runtime.call_codex("probe").error_code == "authentication"
    assert len(calls) == 1

    monkeypatch.setattr(agent_runtime.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("codex")))
    result = agent_runtime.call_codex("probe")
    assert result.error_code == "missing_command" and not result.retryable


def test_timeout_category(monkeypatch):
    monkeypatch.setattr(agent_runtime.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        subprocess.TimeoutExpired(a[0], 1)))
    result = agent_runtime.call_codex("probe")
    assert result.error_code == "timeout" and result.retryable
