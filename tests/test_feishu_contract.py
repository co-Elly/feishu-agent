import json
from types import SimpleNamespace

import bot


def _event(text):
    message = SimpleNamespace(message_id="m1", message_type="text",
                              content=json.dumps({"text": text}), chat_id="chat-1")
    sender_id = SimpleNamespace(user_id="user-1", open_id="open-1")
    return SimpleNamespace(event=SimpleNamespace(message=message,
                                                  sender=SimpleNamespace(sender_id=sender_id)))


def test_normal_chat_is_acknowledged_without_running_agent_inline(monkeypatch):
    replies, submitted = [], []

    class Store:
        def expire_approvals(self): return []

    class Controller:
        store = Store()
        def submit(self, task_type, chat_id, user_id, message_id, payload):
            submitted.append((task_type, payload))
            return {"id": "abc1234567"}

    monkeypatch.setattr(bot, "TASK_CONTROLLER", Controller())
    monkeypatch.setattr(bot, "reply_feishu_msg", lambda client, message_id, text: replies.append(text))
    monkeypatch.setattr(bot, "log_chat", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot, "chat_with_hermes", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("agent must not run in receive handler")))

    bot.handle_message(None, _event("你好"))
    assert submitted == [("chat", {"prompt": "你好", "project": None})]
    assert replies and "已收到" in replies[0]


def test_roundtable_speech_only_updates_one_card(monkeypatch):
    replies, updates = [], []

    class Context:
        def check_cancelled(self): pass
        def progress(self, text): pass

    class Engine:
        def run(self, topic, **kwargs):
            event = kwargs["on_event"]
            event("start", {"members": ["A", "B", "C"]})
            event("speech", {"agent": "A", "stance": "同意", "text": "不应单独发出的正文"})
            event("progress", {"msg": "第二轮"})
            return {"session_id": "s1", "rounds_used": 2, "final_summary": "最终总结",
                    "unavailable_agents": {}}

    monkeypatch.setattr(bot, "RoundTableV2", Engine)
    monkeypatch.setattr(bot, "send_progress_card", lambda *args, **kwargs: "card-1")
    monkeypatch.setattr(bot, "update_progress_card", lambda *args, **kwargs: updates.append(args) or True)
    monkeypatch.setattr(bot, "reply_feishu_msg", lambda client, message_id, text: replies.append(text))
    task = {"id": "t1", "payload": {"topic": "欢迎语", "project": None},
            "message_id": "m1", "chat_id": "c1"}
    bot._run_roundtable_task(None, task, Context())
    assert all("不应单独发出的正文" not in reply for reply in replies)
    assert len(replies) == 1 and "minutes.md" in replies[0]
    assert updates and any("最终总结" in str(args) for args in updates)
