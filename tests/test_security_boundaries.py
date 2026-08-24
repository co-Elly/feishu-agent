import json
from types import SimpleNamespace

import pytest

import bot
from command_parser import extract_project_tag, validate_project_name
from obsidian_bridge import ObsidianBridge


def _event(text, user_id="user-2"):
    message = SimpleNamespace(message_id="m-control", message_type="text",
                              content=json.dumps({"text": text}), chat_id="chat-1")
    sender_id = SimpleNamespace(user_id=user_id, open_id=user_id)
    return SimpleNamespace(event=SimpleNamespace(message=message,
        sender=SimpleNamespace(sender_id=sender_id)))


@pytest.mark.parametrize("name", ["../escape", "E:\\escape", "CON", "bad/name", "bad..name"])
def test_project_name_rejects_path_escape(name):
    with pytest.raises(ValueError):
        validate_project_name(name)


def test_obsidian_project_stays_under_vault(tmp_path):
    bridge = ObsidianBridge(str(tmp_path))
    path = bridge.get_project_dir("安全项目")
    assert path.startswith(str(tmp_path))
    with pytest.raises(ValueError):
        bridge.get_project_dir("../escape")


def test_project_tag_is_validated():
    with pytest.raises(ValueError):
        extract_project_tag("协作 [项目:../escape] 做事")


def test_non_owner_cannot_approve(monkeypatch):
    replies, approvals = [], []
    task = {"id": "abc1234567", "user_id": "owner", "status": "waiting_approval"}

    class Store:
        def expire_approvals(self): return []
        def find(self, *_args): return task

    class Controller:
        store = Store()
        def approve(self, task_id): approvals.append(task_id); return "approved"

    monkeypatch.setattr(bot, "TASK_CONTROLLER", Controller())
    monkeypatch.setattr(bot, "reply_feishu_msg", lambda _c, _m, body: replies.append(body))
    monkeypatch.setattr(bot, "log_chat", lambda *args: None)
    bot.handle_message(None, _event("批准任务 abc1234567"))
    assert approvals == []
    assert "仅任务发起人" in replies[0]


def test_reset_only_clears_requesting_users_sessions(monkeypatch):
    cleared, replies = [], []

    class Store:
        def expire_approvals(self): return []

    monkeypatch.setattr(bot.TASK_CONTROLLER, "store", Store())
    monkeypatch.setattr(bot, "clear_history", lambda key=None: cleared.append(key))
    monkeypatch.setattr(bot, "reply_feishu_msg", lambda _c, _m, body: replies.append(body))
    monkeypatch.setattr(bot, "log_chat", lambda *args: None)
    bot.handle_message(None, _event("清空上下文", user_id="owner"))
    assert cleared == ["owner:hermes", "owner:deepseek"]
    assert "不影响其他用户" in replies[0]
