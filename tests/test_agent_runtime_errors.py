import subprocess
import agent_runtime


class FakePopen:
    def __init__(self, returncode=1, stdout="", stderr="", communicate_error=None):
        self.returncode, self.stdout_text, self.stderr_text = returncode, stdout, stderr
        self.communicate_error = communicate_error

    def communicate(self, input=None, timeout=None):
        if self.communicate_error:
            raise self.communicate_error
        return self.stdout_text, self.stderr_text

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


def _disable_job(monkeypatch):
    monkeypatch.setattr(agent_runtime._WindowsKillJob, "assign", lambda self, proc: False)


def test_error_classification_and_cooldown():
    assert agent_runtime.classify_error("401 Unauthorized") == ("authentication", False)
    assert agent_runtime.classify_error("Individual quota reached") == ("quota_exhausted", False)
    assert agent_runtime.classify_error("network connection reset") == ("network", True)
    assert agent_runtime.cooldown_seconds("Resets in 1h2m3s", "quota_exhausted") == 3733


def test_network_is_retried_once(monkeypatch):
    calls = []
    def fake_popen(*args, **kwargs):
        calls.append(1)
        return FakePopen(stderr="network connection reset")
    _disable_job(monkeypatch)
    monkeypatch.setattr(agent_runtime.subprocess, "Popen", fake_popen)
    result = agent_runtime.call_codex("probe")
    assert not result.ok and result.error_code == "network" and result.retryable
    assert len(calls) == 2


def test_auth_and_missing_command_are_not_retried(monkeypatch):
    calls = []
    def auth(*args, **kwargs):
        calls.append(1)
        return FakePopen(stderr="401 Unauthorized")
    _disable_job(monkeypatch)
    monkeypatch.setattr(agent_runtime.subprocess, "Popen", auth)
    assert agent_runtime.call_codex("probe").error_code == "authentication"
    assert len(calls) == 1

    monkeypatch.setattr(agent_runtime.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("codex")))
    result = agent_runtime.call_codex("probe")
    assert result.error_code == "missing_command" and not result.retryable


def test_timeout_category(monkeypatch):
    _disable_job(monkeypatch)
    monkeypatch.setattr(agent_runtime.subprocess, "Popen", lambda *a, **k: FakePopen(
        communicate_error=subprocess.TimeoutExpired(a[0], 1)))
    result = agent_runtime.call_codex("probe")
    assert result.error_code == "timeout" and result.retryable


def test_zero_exit_sandbox_error_is_not_reported_as_success(monkeypatch):
    _disable_job(monkeypatch)
    monkeypatch.setattr(agent_runtime.subprocess, "Popen", lambda *a, **k: FakePopen(
        returncode=0,
        stdout="Encountered error in step execution: sandbox configuration error",
    ))
    result = agent_runtime.call_codex("probe")
    assert not result.ok
    assert result.error_code == "sandbox_error"


def test_long_model_output_cannot_masquerade_as_missing_command(monkeypatch):
    _disable_job(monkeypatch)
    long_review = ("报告发现缺失脚本，但这是审查结论。" * 300) + "\nno such file or directory"
    monkeypatch.setattr(agent_runtime.subprocess, "Popen", lambda *a, **k: FakePopen(
        returncode=1, stdout=long_review, stderr="",
    ))

    result = agent_runtime.call_codex("probe")

    assert not result.ok
    assert result.error_code == "process_error"
    assert "模型已产生长输出" in result.text

    monkeypatch.setattr(agent_runtime.subprocess, "Popen", lambda *a, **k: FakePopen(
        returncode=0,
        stdout="Finished",
        stderr="error executing cascade step: CORTEX_STEP_TYPE_RUN_COMMAND",
    ))
    result = agent_runtime.call_codex("probe")
    assert not result.ok
    assert result.error_code == "sandbox_error"
