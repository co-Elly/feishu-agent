import agent_runtime


class FakePopen:
    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.returncode = 0

    def communicate(self, input=None, timeout=None):
        self.input = input
        self.timeout = timeout
        return "完成", ""

    def poll(self):
        return self.returncode


def _fake_process(monkeypatch, captured):
    def factory(command, **kwargs):
        proc = FakePopen(command, **kwargs)
        captured["command"] = command
        captured.update(kwargs)
        captured["process"] = proc
        return proc

    monkeypatch.setattr(agent_runtime.subprocess, "Popen", factory)
    monkeypatch.setattr(agent_runtime._WindowsKillJob, "assign", lambda self, proc: False)


def test_codex_uses_read_only_and_no_shell(monkeypatch):
    captured = {}

    _fake_process(monkeypatch, captured)
    result = agent_runtime.call_codex("检查项目")
    assert result.ok and result.text == "完成"
    assert "read-only" in captured["command"]
    assert "--ephemeral" in captured["command"]
    assert captured["shell"] is False
    assert captured["process"].input == "检查项目"


def test_codex_write_requires_explicit_flag(monkeypatch):
    captured = {}

    _fake_process(monkeypatch, captured)
    agent_runtime.call_codex("实现功能", writable=True)
    assert "workspace-write" in captured["command"]


def test_hermes_gateway_calls_disable_action_tools(monkeypatch):
    captured = {}

    _fake_process(monkeypatch, captured)
    result = agent_runtime.call_hermes("只回答，不操作电脑")

    assert result.ok
    shell_command = captured["command"][4]
    assert "--safe-mode" in shell_command and "-t clarify" in shell_command
    assert "terminal" not in shell_command
    assert captured["shell"] is False


def test_antigravity_requires_sandboxed_mode(monkeypatch, tmp_path):
    safe_script = tmp_path / "safe.ps1"
    safe_script.write_text("agy.exe --sandbox --mode plan", encoding="utf-8")
    unsafe_script = tmp_path / "unsafe.ps1"
    unsafe_script.write_text("agy.exe --dangerously-skip-permissions", encoding="utf-8")
    captured = {}
    _fake_process(monkeypatch, captured)

    monkeypatch.setattr(agent_runtime, "runtime_value", lambda name: (
        str(unsafe_script) if name == "antigravity_script_high" else str(tmp_path)
    ))
    blocked = agent_runtime.call_antigravity("实现", model="high", workspace_dir=str(tmp_path))
    assert not blocked.ok and blocked.error_code == "unsafe_configuration"
    assert "command" not in captured

    monkeypatch.setattr(agent_runtime, "runtime_value", lambda name: (
        str(safe_script) if name == "antigravity_script_high" else str(tmp_path)
    ))
    allowed = agent_runtime.call_antigravity("实现", model="high", workspace_dir=str(tmp_path))
    assert allowed.ok
    assert captured["env"]["USERPROFILE"] == str(tmp_path)
