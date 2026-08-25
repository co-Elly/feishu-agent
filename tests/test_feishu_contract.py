import json
import io
from types import SimpleNamespace

import bot
import pytest
from task_manager import TaskParked
from workflow_store import WorkflowStore


def _event(text):
    message = SimpleNamespace(message_id="m1", message_type="text",
                              content=json.dumps({"text": text}), chat_id="chat-1")
    sender_id = SimpleNamespace(user_id="user-1", open_id="open-1")
    return SimpleNamespace(event=SimpleNamespace(message=message,
                                                  sender=SimpleNamespace(sender_id=sender_id)))


def _route(monkeypatch, text, listed_tasks=()):
    replies, submitted = [], []

    class Store:
        def expire_approvals(self): return []
        def list(self, *_args, **_kwargs): return list(listed_tasks)
        def find(self, prefix, *_args, **_kwargs):
            matches = [task for task in listed_tasks if task["id"].startswith(prefix)]
            return matches[0] if len(matches) == 1 else None
        def get(self, task_id):
            return next((task for task in listed_tasks if task["id"] == task_id), None)

    class Controller:
        store = Store()
        def submit(self, task_type, chat_id, user_id, message_id, payload):
            submitted.append((task_type, payload))
            return {"id": "abc1234567"}

    monkeypatch.setattr(bot, "TASK_CONTROLLER", Controller())
    monkeypatch.setattr(bot, "reply_feishu_msg", lambda _client, _message_id, body: replies.append(body))
    monkeypatch.setattr(bot, "log_chat", lambda *args, **kwargs: None)
    bot.handle_message(None, _event(text))
    return submitted, replies


def _authorize_swarm_run(monkeypatch, task, state="waiting_approval"):
    meeting_id, workflow_id = "meeting-1", "workflow-1"
    task.setdefault("user_id", "user-1")
    task["payload"].update({
        "meeting_task_id": meeting_id,
        "workflow_id": workflow_id,
        "collaboration_confirmation": {
            "workflow_id": workflow_id,
            "meeting_task_id": meeting_id,
            "confirmed_by": "user-1",
            "confirmation_message_id": "confirm-1",
            "confirmed_at": 1,
        },
    })
    workflow = {
        "id": workflow_id, "state": state, "owner_user_id": "user-1",
        "current_task_id": task["id"],
    }

    class Workflows:
        def get(self, _workflow_id): return dict(workflow)
        def transition(self, _workflow_id, to_state, current_task_id=None):
            workflow["state"] = to_state
            if current_task_id:
                workflow["current_task_id"] = current_task_id
            return dict(workflow)

    meeting = {"id": meeting_id, "task_type": "roundtable", "status": "succeeded"}
    monkeypatch.setattr(bot, "WORKFLOW_STORE", Workflows())
    monkeypatch.setattr(bot, "TASK_CONTROLLER", SimpleNamespace(
        store=SimpleNamespace(get=lambda _task_id: meeting),
    ))


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


def test_explicit_collaboration_starts_meeting_even_when_project_contains_meeting(monkeypatch):
    submitted, _ = _route(
        monkeypatch,
        "协作 [项目:会议协作复验] 仅新增 result.md",
    )
    task_type, payload = submitted[0]
    assert task_type == "roundtable"
    assert payload["root_request"] == "仅新增 result.md"
    assert payload["project"] == "会议协作复验"
    assert payload["initiated_by"] == "collaboration_request"
    assert payload["constraint_envelope"]["allowed_paths"] == ["result.md"]
    assert payload["constraint_envelope"]["scope_restricted"] is True


def test_external_pdf_report_first_routes_to_meeting_not_preflight(monkeypatch):
    goal = (
        '协作 阅读 "C:\\Users\\26420\\Desktop\\main.pdf"，结合项目目录 '
        '"E:\\Syn3D-Process" 剖析论文内容，仅生成一份论文改进报告，不修改项目源码。'
    )
    submitted, replies = _route(monkeypatch, goal)

    task_type, payload = submitted[0]
    assert task_type == "roundtable"
    assert payload["initiated_by"] == "collaboration_request"
    assert "main.pdf" in payload["root_request"]
    assert "才会创建协作预审任务" in replies[0]


def test_report_planning_uses_local_preflight_not_generic_agent_swarm(monkeypatch):
    plans, cards = [], []
    monkeypatch.setattr(bot, "plan_read_only_report", lambda *a, **k: {
        "project_name": "Syn3D-Process", "goal": "report",
        "requirements": "唯一 DOCX", "impact_and_risks": "只读失败关闭",
        "report_request": {"page_count": 24, "project_files": 100},
    })
    monkeypatch.setattr(
        bot.swarm_orchestrator, "plan_collaborative_project",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("generic planning must not run")),
    )
    monkeypatch.setattr(bot, "send_progress_card", lambda *a, **k: cards.append((a, k)))
    monkeypatch.setattr(bot, "record_task_event", lambda *a, **k: None)
    monkeypatch.setattr(bot, "record_span", lambda *a, **k: None)

    def wait_for_approval(plan):
        plans.append(plan)
        raise TaskParked()

    context = SimpleNamespace(
        progress=lambda _text: None, check_cancelled=lambda: None,
        wait_for_approval=wait_for_approval,
    )
    task = {
        "id": "task-1", "task_type": "swarm", "phase": "planning",
        "message_id": "m1", "chat_id": "c1",
        "payload": {
            "goal": "report", "project": "Syn3D-Process",
            "operation_mode": "read_only_report",
            "constraint_envelope": {"root_request": "report"},
        },
    }
    _authorize_swarm_run(monkeypatch, task, state="planning")

    with pytest.raises(TaskParked):
        bot._run_swarm_task(None, task, context)

    assert plans[0]["report_request"]["page_count"] == 24
    assert cards


def test_read_only_report_executes_without_code_pipeline(monkeypatch, tmp_path):
    delivered, replies, progress = [], [], []
    report = tmp_path / "论文改进报告_task-1.docx"
    report.write_bytes(b"docx")
    monkeypatch.setattr(bot, "execute_read_only_report", lambda *a, **k: {
        "project_name": "Syn3D-Process", "report_path": str(report),
        "evidence": {"page_count": 24, "project_changed_files": []},
    })
    monkeypatch.setattr(bot, "send_feishu_file",
                        lambda client, chat_id, path: delivered.append((chat_id, path)))
    monkeypatch.setattr(bot, "reply_feishu_msg",
                        lambda client, message_id, text: replies.append(text))
    monkeypatch.setattr(
        bot.swarm_orchestrator, "execute_collaborative_project",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("code pipeline must not run")),
    )
    task = {
        "id": "task-1", "task_type": "swarm", "message_id": "m1", "chat_id": "c1",
        "phase": "execute", "approved_at": 1, "plan": {"project_name": "Syn3D-Process"},
        "payload": {
            "goal": '阅读 "C:\\Users\\26420\\Desktop\\main.pdf"，结合项目目录 '
                    '"E:\\Syn3D-Process"，仅生成一份报告，不修改项目源码',
            "project": "Syn3D-Process", "operation_mode": "read_only_report",
        },
    }
    _authorize_swarm_run(monkeypatch, task)
    context = SimpleNamespace(progress=progress.append, check_cancelled=lambda: None)

    result = bot._run_swarm_task(None, task, context)

    assert result["success"] is True
    assert delivered == [("c1", str(report))]
    assert "项目零修改验证通过" in replies[-1]
    assert any("发送到飞书" in item for item in progress)


def test_delivered_report_checkpoint_finishes_without_duplicate_send(monkeypatch):
    class Store:
        def checkpoints(self, _task_id):
            return [{
                "checkpoint": "report_delivered",
                "details": {
                    "report_name": "论文改进报告_task-1.docx",
                    "report_path": "durable.docx", "project_name": "Syn3D-Process",
                    "page_count": 24, "evidence": {"page_count": 24},
                },
            }]

    monkeypatch.setattr(bot, "send_feishu_file", lambda *a, **k: (
        _ for _ in ()
    ).throw(AssertionError("must not send twice")))
    monkeypatch.setattr(bot, "execute_read_only_report", lambda *a, **k: (
        _ for _ in ()
    ).throw(AssertionError("must not regenerate")))
    task = {
        "id": "task-1", "task_type": "swarm", "message_id": "m1", "chat_id": "c1",
        "phase": "execute", "approved_at": 1, "plan": {"project_name": "Syn3D-Process"},
        "payload": {"goal": "report", "project": "Syn3D-Process",
                    "operation_mode": "read_only_report"},
    }
    _authorize_swarm_run(monkeypatch, task)
    progress = []
    context = SimpleNamespace(
        store=Store(), progress=progress.append, check_cancelled=lambda: None,
    )

    result = bot._run_swarm_task(None, task, context)

    assert result["success"] is True
    assert result["report_path"] == "durable.docx"
    assert any("不重复生成或发送" in item for item in progress)


def test_send_feishu_file_uploads_stream_then_sends_file_message(monkeypatch, tmp_path):
    report = tmp_path / "report.docx"
    report.write_bytes(b"docx-bytes")
    uploads, messages, logs = [], [], []

    class Endpoint:
        def __init__(self, sink, response):
            self.sink, self.response = sink, response
        def create(self, request):
            self.sink.append(request)
            return self.response

    upload_response = SimpleNamespace(
        success=lambda: True, data=SimpleNamespace(file_key="file-key"), code=0, msg="",
    )
    message_response = SimpleNamespace(
        success=lambda: True, data=SimpleNamespace(message_id="message-id"), code=0, msg="",
    )
    client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(
        file=Endpoint(uploads, upload_response),
        message=Endpoint(messages, message_response),
    )))
    monkeypatch.setattr(bot, "log_chat", lambda *args: logs.append(args))

    message_id = bot.send_feishu_file(client, "chat-1", str(report))

    assert message_id == "message-id"
    assert len(uploads) == len(messages) == 1
    assert uploads[0].request_body.file_name == "report.docx"
    assert json.loads(messages[0].request_body.content) == {"file_key": "file-key"}
    assert messages[0].request_body.receive_id == "chat-1"
    assert logs and "已发送文件" in logs[0][2]


def test_meeting_decision_continues_roundtable_with_prior_consensus(monkeypatch, tmp_path):
    meeting = {
        "id": "meet123456", "task_type": "roundtable", "status": "succeeded",
        "payload": {
            "project": "会议协作复验", "topic": "讨论方案",
            "constraint_envelope": {
                "version": 1, "root_request": "讨论方案", "hard_constraints": [],
                "scope_restricted": False, "allowed_paths": [],
            },
        },
        "result": {"summary": "上轮共识"},
    }
    workflows = WorkflowStore(str(tmp_path / "workflow.db"))
    workflow = workflows.create("chat-1", "user-1", "会议协作复验", "讨论方案",
                                meeting["payload"]["constraint_envelope"])
    workflows.bind_task(workflow["id"], meeting["id"])
    workflows.transition(workflow["id"], "awaiting_boss_decision")
    monkeypatch.setattr(bot, "WORKFLOW_STORE", workflows)
    submitted, replies = _route(
        monkeypatch,
        "[项目:会议协作复验] 同意砍掉回滚段",
        [meeting],
    )
    assert submitted[0][0] == "roundtable"
    assert submitted[0][1]["continued_from"] == "meet123456"
    assert "上轮共识" in submitted[0][1]["topic"]
    assert "动态续会" in replies[0]


def test_boss_can_explicitly_end_discussion_before_collaboration_confirmation(monkeypatch, tmp_path):
    meeting = {
        "id": "meet123456", "task_type": "roundtable", "status": "succeeded",
        "payload": {
            "project": "项目A", "topic": "讨论方案",
            "constraint_envelope": {
                "version": 1, "root_request": "仅修改 app.py", "hard_constraints": [],
                "scope_restricted": True, "allowed_paths": ["app.py"],
            },
        },
        "result": {"summary": "上轮共识"},
    }
    workflows = WorkflowStore(str(tmp_path / "workflow.db"))
    workflow = workflows.create("chat-1", "user-1", "项目A", "仅修改 app.py",
                                meeting["payload"]["constraint_envelope"])
    workflows.bind_task(workflow["id"], meeting["id"])
    workflows.transition(workflow["id"], "awaiting_boss_decision")
    monkeypatch.setattr(bot, "WORKFLOW_STORE", workflows)

    submitted, replies = _route(
        monkeypatch, "[项目:项目A] 拍板：按当前方案定稿，无需继续讨论", [meeting],
    )

    assert submitted == []
    assert workflows.get(workflow["id"])["state"] == "awaiting_collaboration_confirmation"
    assert "是否开始协作" in replies[0]


def test_start_collaboration_hands_confirmed_workflow_to_swarm_planning(monkeypatch, tmp_path):
    meeting = {
        "id": "meet123456", "task_type": "roundtable", "status": "succeeded",
        "result": {"summary": "已拍板共识"},
        "payload": {
            "project": "会议协作复验", "topic": "讨论方案",
            "root_request": "仅新增 MEETING.md，禁止修改其他文件",
            "constraint_envelope": {
                "version": 1, "root_request": "仅新增 MEETING.md，禁止修改其他文件",
                "hard_constraints": ["仅新增 MEETING.md，禁止修改其他文件"],
                "scope_restricted": True, "allowed_paths": ["MEETING.md"],
            },
        },
    }
    workflows = WorkflowStore(str(tmp_path / "workflow.db"))
    workflow = workflows.create("chat-1", "user-1", "会议协作复验", "仅新增 MEETING.md",
                                meeting["payload"]["constraint_envelope"])
    workflows.bind_task(workflow["id"], meeting["id"])
    workflows.transition(workflow["id"], "awaiting_boss_decision")
    workflows.transition(workflow["id"], "meeting_continuation")
    workflows.transition(workflow["id"], "awaiting_collaboration_confirmation")
    monkeypatch.setattr(bot, "WORKFLOW_STORE", workflows)
    submitted, replies = _route(
        monkeypatch,
        "开始协作 [项目:会议协作复验]",
        [meeting],
    )
    assert submitted[0][0] == "swarm"
    assert submitted[0][1]["project"] == "会议协作复验"
    assert submitted[0][1]["meeting_task_id"] == "meet123456"
    assert submitted[0][1]["collaboration_confirmation"]["confirmed_by"] == "user-1"
    assert "已拍板共识" in submitted[0][1]["goal"]
    assert submitted[0][1]["constraint_envelope"]["allowed_paths"] == ["MEETING.md"]
    assert "协作任务只读规划" in replies[0]


def test_new_meeting_creates_durable_workflow_without_changing_command(monkeypatch, tmp_path):
    replies, submitted = [], []
    workflow_store = WorkflowStore(str(tmp_path / "workflow.db"))

    class Store:
        def expire_approvals(self): return []
        def list(self, *_args, **_kwargs): return []

    class Controller:
        store = Store()
        def submit(self, task_type, chat_id, user_id, message_id, payload):
            submitted.append((task_type, payload))
            return {"id": "meeting0001"}

    monkeypatch.setattr(bot, "WORKFLOW_STORE", workflow_store)
    monkeypatch.setattr(bot, "TASK_CONTROLLER", Controller())
    monkeypatch.setattr(bot, "reply_feishu_msg", lambda _c, _m, body: replies.append(body))
    monkeypatch.setattr(bot, "log_chat", lambda *args, **kwargs: None)
    bot.handle_message(None, _event("开会 [项目:升级] 讨论兼容迁移"))

    assert submitted[0][0] == "roundtable"
    workflow_id = submitted[0][1]["workflow_id"]
    workflow = workflow_store.get(workflow_id)
    assert workflow["state"] == "meeting_discussion"
    assert workflow["current_task_id"] == "meeting0001"
    assert workflow["owner_user_id"] == "user-1"


def test_stale_meeting_without_active_workflow_cannot_be_handed_off(monkeypatch):
    old_meeting = {
        "id": "old1234567", "task_type": "roundtable", "status": "succeeded",
        "payload": {"project": "旧项目", "topic": "旧会议"},
        "result": {"summary": "旧共识"},
    }
    submitted, replies = _route(
        monkeypatch, "开始协作 [项目:旧项目]", [old_meeting],
    )
    assert submitted == []
    assert "没有等待协作确认的会议流程" in replies[0]


def test_normal_chat_prompt_forbids_unverified_completion_claims(monkeypatch):
    captured = []
    monkeypatch.setattr(bot, "get_history", lambda _key: [])
    monkeypatch.setattr(bot.MEMORY_STORE, "prompt_context", lambda *args, **kwargs: "")
    monkeypatch.setattr(bot, "call_hermes", lambda prompt, **kwargs: (
        captured.append(prompt), SimpleNamespace(ok=True, text="收到", error_code=None, duration_ms=1)
    )[-1])
    monkeypatch.setattr(bot, "add_history", lambda *args: None)

    bot.chat_with_hermes("user-1", "什么进度")

    assert "不得声称“已经搞定”或“全部实测通过”" in captured[0]
    assert "不得承诺定时主动跟进" in captured[0]


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
    _authorize_swarm_run(monkeypatch, task)
    context = SimpleNamespace(progress=lambda _text: None, check_cancelled=lambda: None)

    with pytest.raises(RuntimeError, match="全量测试缺少 lark_oapi"):
        bot._run_swarm_task(None, task, context)


def test_roundtable_speech_broadcasts_full_text_as_reply_chain(monkeypatch):
    """P0-1 新契约：speech 全文通过 send_feishu_msg 播报，且首条回复原消息形成回复链。"""
    replies, updates, cards, sent, memories = [], [], [], [], []

    class Context:
        def check_cancelled(self): pass
        def progress(self, text): pass

    class Engine:
        def run(self, topic, **kwargs):
            event = kwargs["on_event"]
            event("start", {"members": ["A", "B", "C"]})
            event("speech", {"agent": "A", "stance": "同意", "text": "第一轮发言正文"})
            return {"session_id": "s1", "rounds_used": 2, "final_summary": "最终总结",
                    "unavailable_agents": {}}

    monkeypatch.setattr(bot, "RoundTableV2", Engine)
    monkeypatch.setattr(bot, "send_progress_card", lambda *args, **kwargs:
                        cards.append((args, kwargs)) or "card-1")
    monkeypatch.setattr(bot, "update_progress_card", lambda *args, **kwargs: updates.append(args) or True)
    monkeypatch.setattr(bot, "reply_feishu_msg", lambda client, message_id, text: replies.append(text))
    monkeypatch.setattr(bot, "send_feishu_msg",
                        lambda client, chat_id, text, reply_to=None: sent.append((chat_id, text, reply_to)) or "s-1")
    monkeypatch.setattr(bot, "_evolve_after_task", lambda *args: {
        "id": "evo12345", "content": "先验证现状再修改",
    })
    monkeypatch.setattr(bot, "MEMORY_STORE", SimpleNamespace(
        prompt_context=lambda *args, **kwargs: "",
        add=lambda *args, **kwargs: memories.append((args, kwargs)),
    ))
    import io as _io
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: _io.StringIO("# 会议纪要\n完整内容"))
    task = {"id": "t1", "task_type": "roundtable",
            "payload": {"topic": "欢迎会", "project": "项目记忆",
                        "constraint_envelope": {"root_request": "欢迎会"}},
            "message_id": "m1", "chat_id": "c1"}
    bot._run_roundtable_task(None, task, Context())
    # 全文已播报，且回复原消息 m1 形成回复链
    assert len(sent) == 1
    chat_id, text, reply_to = sent[0]
    assert chat_id == "c1" and reply_to == "m1"
    assert "第一轮发言正文" in text and "A" in text
    assert len(cards) == 2
    assert cards[1][1]["details_text"].startswith("# 会议纪要")
    assert updates and any("最终总结" in str(args) for args in updates)
    assert any("请老板拍板" in str(args) for args in updates)
    assert all("确认请回复 `开始协作" not in str(args) for args in updates)
    assert any("本次进化" in str(args) for args in updates)
    assert memories == [(('最终总结',), {
        "project_name": "项目记忆", "source_type": "roundtable",
        "source_id": "t1", "source_path": "roundtable/s1/minutes.md",
    })]


def test_successful_project_swarm_saves_sourced_memory(monkeypatch):
    memories, replies, execution_plans = [], [], []
    monkeypatch.setattr(bot.swarm_orchestrator, "execute_collaborative_project",
                        lambda plan, **kwargs: (execution_plans.append(plan), {
                            "success": True, "project_name": "架构验收",
                            "final_report": "验收：通过\n证据齐全",
                        })[-1])
    monkeypatch.setattr(bot, "MEMORY_STORE", SimpleNamespace(
        add=lambda *args, **kwargs: memories.append((args, kwargs)),
    ))
    monkeypatch.setattr(bot, "_evolve_after_task", lambda *args: None)
    monkeypatch.setattr(bot, "reply_feishu_msg", lambda *args: replies.append(args))
    task = {
        "id": "task-1", "retry_of": "old-task", "task_type": "swarm", "message_id": "m1",
        "phase": "execute", "approved_at": 1,
        "plan": {"project_name": "架构验收"},
        "payload": {"goal": "写文档", "project": "架构验收"},
    }
    _authorize_swarm_run(monkeypatch, task)
    context = SimpleNamespace(progress=lambda _text: None, check_cancelled=lambda: None)

    result = bot._run_swarm_task(None, task, context)

    assert result["success"] is True
    assert execution_plans[0]["execution_task_id"] == "task-1"
    assert execution_plans[0]["retry_of"] == "old-task"
    assert memories == [(('验收：通过\n证据齐全',), {
        "project_name": "架构验收", "source_type": "swarm",
        "source_id": "task-1", "source_path": "Obsidian/架构验收",
    })]
    assert replies


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
