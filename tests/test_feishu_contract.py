import json
import io
from types import SimpleNamespace

import bot
import pytest
from task_manager import TaskParked


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


def test_waiting_approval_parks_without_failure_reply_or_evolution(monkeypatch):
    replies, evolutions = [], []
    monkeypatch.setattr(bot, "_run_swarm_task", lambda *args: (_ for _ in ()).throw(TaskParked()))
    monkeypatch.setattr(bot, "reply_feishu_msg", lambda *args: replies.append(args))
    monkeypatch.setattr(bot, "_evolve_after_task", lambda *args: evolutions.append(args))

    with pytest.raises(TaskParked):
        bot.execute_background_task(None, {
            "id": "task-1", "task_type": "swarm", "message_id": "m1",
        }, SimpleNamespace())

    assert replies == []
    assert evolutions == []


def test_failed_swarm_surfaces_final_validation_report(monkeypatch):
    monkeypatch.setattr(bot.swarm_orchestrator, "execute_collaborative_project",
                        lambda *args, **kwargs: {
                            "success": False,
                            "final_report": "验收：不通过\n全量测试缺少 lark_oapi",
                        })
    task = {
        "id": "task-1", "task_type": "swarm", "message_id": "m1",
        "phase": "execute", "approved_at": 1,
        "plan": {"project_name": "验收"},
        "payload": {"goal": "写文档", "project": "验收"},
    }
    context = SimpleNamespace(progress=lambda _text: None, check_cancelled=lambda: None)

    with pytest.raises(RuntimeError, match="全量测试缺少 lark_oapi"):
        bot._run_swarm_task(None, task, context)


def test_roundtable_speech_only_updates_one_card(monkeypatch):
    replies, updates, cards = [], [], []

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
    monkeypatch.setattr(bot, "send_progress_card", lambda *args, **kwargs:
                        cards.append((args, kwargs)) or "card-1")
    monkeypatch.setattr(bot, "update_progress_card", lambda *args, **kwargs: updates.append(args) or True)
    monkeypatch.setattr(bot, "reply_feishu_msg", lambda client, message_id, text: replies.append(text))
    monkeypatch.setattr(bot, "_evolve_after_task", lambda *args: {
        "id": "evo12345", "content": "先验证现状再修改",
    })
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: io.StringIO("# 会议纪要\n完整内容"))
    task = {"id": "t1", "task_type": "roundtable",
            "payload": {"topic": "欢迎语", "project": None},
            "message_id": "m1", "chat_id": "c1"}
    bot._run_roundtable_task(None, task, Context())
    assert all("不应单独发出的正文" not in reply for reply in replies)
    assert replies == []
    assert len(cards) == 2
    assert cards[1][1]["details_text"].startswith("# 会议纪要")
    assert updates and any("最终总结" in str(args) for args in updates)
    assert any("本次进化" in str(args) for args in updates)


def test_minutes_card_uses_clickable_collapsible_panel(monkeypatch):
    captured = {}

    class Response:
        def __init__(self, data):
            self.data, self.code, self.msg = data, 0, ""
        def success(self): return True

    class MessageApi:
        def create(self, request):
            captured["message"] = request.body
            return Response(SimpleNamespace(message_id="message-id"))

    client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(
        message=MessageApi(),
    )))
    message_id = bot.send_progress_card(client, "chat-1", "会议纪要", "点击展开", "# 完整内容")
    assert message_id == "message-id"
    card = json.loads(captured["message"].content)
    assert card["schema"] == "2.0"
    panel = card["body"]["elements"][1]
    assert panel["tag"] == "collapsible_panel" and panel["expanded"] is False
    assert panel["elements"][0]["content"] == "# 完整内容"


def test_progress_card_patch_keeps_json_2_schema():
    captured = {}

    class Response:
        code, msg = 0, ""
        def success(self): return True

    class MessageApi:
        def patch(self, request):
            captured["message"] = request.body
            return Response()

    client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=MessageApi())))
    assert bot.update_progress_card(client, "message-id", "进度", "已完成")
    card = json.loads(captured["message"].content)
    assert card["schema"] == "2.0"
    assert card["body"]["elements"] == [{"tag": "markdown", "content": "已完成"}]


def test_meaningful_task_learns_one_auditable_evolution_rule(monkeypatch):
    added, events, prompts = [], [], []

    class Store:
        def add_evolution(self, content, task_id, project_name=None):
            added.append((content, task_id, project_name))
            return {"id": "evo12345", "content": content, "scope": "project"}

    monkeypatch.setattr(bot, "MEMORY_STORE", Store())
    monkeypatch.setattr(bot, "call_hermes", lambda *args, **kwargs: (
        prompts.append(args[0]), SimpleNamespace(
            ok=True, text="先验证现状再修改", error_code=None, duration_ms=12,
        )
    )[-1])
    monkeypatch.setattr(bot, "record_task_event", lambda *args, **kwargs: events.append((args, kwargs)))
    task = {"id": "task-1", "task_type": "swarm", "payload": {"project": "A"}}

    memory = bot._evolve_after_task(task, "任务成功")

    assert memory["id"] == "evo12345"
    assert added == [("先验证现状再修改", "task-1", "A")]
    assert "不得猜测未出现的错误类别" in prompts[0]
    assert any(args[1] == "evolution_learned" for args, _ in events)
