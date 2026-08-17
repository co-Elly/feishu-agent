from types import SimpleNamespace

import agent_runtime


def test_codex_uses_read_only_and_no_shell(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="完成", stderr="")

    monkeypatch.setattr(agent_runtime.subprocess, "run", fake_run)
    assert agent_runtime.call_codex("检查项目") == "完成"
    assert "read-only" in captured["command"]
    assert captured["shell"] is False
    assert captured["input"] == "检查项目"


def test_codex_write_requires_explicit_flag(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="完成", stderr="")

    monkeypatch.setattr(agent_runtime.subprocess, "run", fake_run)
    agent_runtime.call_codex("实现功能", writable=True)
    assert "workspace-write" in captured["command"]
