import json
from pathlib import Path

import bot
from agent_runtime import AgentResult
from settings import redact


def test_redact_masks_common_inline_secrets():
    text = "Bearer abc.def sk-example123456789 api_key=very-secret-value password:guessme access_key=temp-key&ticket=temp-ticket"
    safe = redact(text)
    assert "abc.def" not in safe
    assert "sk-example123456789" not in safe
    assert "very-secret-value" not in safe
    assert "guessme" not in safe
    assert "temp-key" not in safe
    assert "temp-ticket" not in safe


def test_guardian_redacts_sdk_connection_credentials():
    source = (Path(__file__).parents[1] / "_guardian.ps1").read_text(encoding="utf-8")
    assert "Get-RedactedLogLine" in source
    assert "access_key|ticket" in source


def test_chat_log_redacts_before_persisting(tmp_path, monkeypatch):
    log_path = tmp_path / "chat.jsonl"
    monkeypatch.setattr(bot, "CHAT_LOG_PATH", str(log_path))
    bot.log_chat("in", "user", "请配置 sk-example123456789")
    row = json.loads(log_path.read_text(encoding="utf-8"))
    assert row["msg"] == "请配置 ***"


def test_ordinary_chat_prompt_forbids_action_claims(monkeypatch):
    captured = {}

    def fake_call(prompt, timeout):
        captured["prompt"] = prompt
        return AgentResult(True, "请发送协作指令")

    monkeypatch.setattr(bot, "call_hermes", fake_call)
    monkeypatch.setattr(bot, "get_history", lambda _key: [])
    monkeypatch.setattr(bot.MEMORY_STORE, "prompt_context", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(bot, "add_history", lambda *_args, **_kwargs: None)

    result = bot.chat_with_hermes("user", "帮我修改配置")
    assert result.ok
    assert "没有操作电脑、文件、飞书或其他 Agent 的权限" in captured["prompt"]
    assert "不得声称已经执行" in captured["prompt"]
    assert "协作 <目标>" in captured["prompt"]
