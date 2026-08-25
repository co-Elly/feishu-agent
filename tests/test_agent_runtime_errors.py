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


# ---------------- P0-3 错误分类修复 ----------------

def test_codex_long_answer_with_error_words_is_process_error(monkeypatch):
    """退出码非零 + 模型长回答里含 "command not found" 等词 → process_error，不再误判 missing_command。"""
    _disable_job(monkeypatch)
    answer = ("审查结论：脚本依赖 CLI 工具，若环境缺失会报 command not found。" * 100)
    monkeypatch.setattr(agent_runtime.subprocess, "Popen", lambda *a, **k: FakePopen(
        returncode=1, stdout=answer, stderr="",
    ))
    result = agent_runtime.call_codex("probe")
    assert not result.ok
    assert result.error_code == "process_error"


def test_codex_stderr_command_not_found_still_missing_command(monkeypatch):
    """stderr 明确报 command not found → 仍判 missing_command（真证据）。"""
    _disable_job(monkeypatch)
    monkeypatch.setattr(agent_runtime.subprocess, "Popen", lambda *a, **k: FakePopen(
        returncode=1, stdout="", stderr="codex: command not found",
    ))
    result = agent_runtime.call_codex("probe")
    assert not result.ok
    assert result.error_code == "missing_command"


def test_detect_write_intent_positive():
    assert agent_runtime.detect_write_intent("把结果写入 report.md")
    assert agent_runtime.detect_write_intent("新建 config.yaml 并填入默认值")
    assert agent_runtime.detect_write_intent("请修改 bot.py 第10行的超时参数")
    assert agent_runtime.detect_write_intent("save the summary to notes.txt")


def test_detect_write_intent_negative():
    assert not agent_runtime.detect_write_intent("调研多 Agent 编排的最新进展并总结")
    assert not agent_runtime.detect_write_intent("分析这份 PDF 的论证结构")
    assert not agent_runtime.detect_write_intent("")
    assert not agent_runtime.detect_write_intent(None)


def test_call_antigravity_blocks_write_intent(monkeypatch):
    """含写意图的任务书 → 直接拦截，返回 write_intent_requires_staging，不启动进程。"""
    def _no_popen(*a, **k):
        raise AssertionError("不应启动进程")
    monkeypatch.setattr(agent_runtime.subprocess, "Popen", _no_popen)
    result = agent_runtime.call_antigravity("把结果写入 report.md")
    assert not result.ok
    assert result.error_code == "write_intent_requires_staging"


# ---------------- P0-3 codex missing_command 误判修复 ----------------

class _FakeProc:
    def __init__(self, returncode=1):
        self.returncode = returncode
        self.pid = 12345
        self._out, self._err = "", ""
    def poll(self):
        return self.returncode
    def kill(self):
        pass
    def communicate(self, input=None, timeout=None):
        return self._out, self._err


def _run_with_output(monkeypatch, stdout, stderr, returncode=1):
    """驱动 _run 的非零退出诊断路径。"""
    class _FakeKJ:
        def assign(self, proc): pass
        def close(self): pass

    def fake_popen(*a, **k):
        proc = _FakeProc(returncode)
        proc._out, proc._err = stdout, stderr
        return proc
    monkeypatch.setattr(agent_runtime.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(agent_runtime, "_WindowsKillJob", _FakeKJ)


def test_codex_long_answer_not_missing_command(monkeypatch):
    """模型回答正文含 '缺失脚本'/'command not found' 讨论词 ≠ 执行器缺命令。

    退出码非零但 stderr 干净时必须归为 process_error，不得误判 missing_command。
    """
    answer = "分析结论：需要先修复缺失脚本 no such file or directory 问题……" * 20
    _run_with_output(monkeypatch, stdout=answer, stderr="", returncode=1)
    result = agent_runtime.call_codex("分析任务", timeout=10)
    assert not result.ok
    assert result.error_code != "missing_command"


def test_stderr_command_not_found_still_missing_command(monkeypatch):
    """stderr 明确报 command not found → 仍判 missing_command（真实故障）。"""
    _run_with_output(monkeypatch, stdout="", stderr="codex: command not found", returncode=127)
    result = agent_runtime.call_codex("任务", timeout=10)
    assert not result.ok
    assert result.error_code == "missing_command"


def test_zero_exit_with_content_is_success(monkeypatch):
    """退出码 0 且有实质输出（即使正文提到错误词）→ 成功。"""
    answer = "报告：检查发现 no such file or directory 风险点若干。" * 5
    _run_with_output(monkeypatch, stdout=answer, stderr="", returncode=0)
    result = agent_runtime.call_codex("写报告", timeout=10)
    assert result.ok
