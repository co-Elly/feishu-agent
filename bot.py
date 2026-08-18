import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import requests
import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    P2ImMessageReceiveV1,
)
from swarm_orchestrator import swarm_orchestrator
from roundtable_engine import RoundTableV2
from conversation_store import (
    add_exchange,
    claim_event,
    clear_history as clear_stored_history,
    get_history as get_stored_history,
    migrate_legacy_json,
)
from agent_runtime import call_hermes, deep_health_probe, lightweight_health
from command_parser import extract_project_tag, parse_memory_command
from control_store import engine_health, record_task_event
from memory_store import MemoryStore
from settings import load_config, runtime_value, validate_startup
from task_manager import TaskCancelled, TaskController

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

CHAT_LOG_PATH = os.path.join(os.path.dirname(__file__), "chat_log.jsonl")      # 收发消息日志（append-only）
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "chat_history.json")    # 对话历史持久化

MAX_HISTORY_TURNS = 10
MESSAGE_POOL = ThreadPoolExecutor(max_workers=int(os.environ.get("FEISHU_MAX_WORKERS", "4")))
TASK_CONTROLLER = TaskController(max_workers=int(os.environ.get("FEISHU_TASK_WORKERS", runtime_value("task_workers"))))
MEMORY_STORE = MemoryStore()
_CHAT_LOCKS = {}
_CHAT_LOCKS_GUARD = threading.Lock()


def log_chat(direction, user, msg, chat_id=None):
    """消息收发日志持久化：in/out 都记，追加到 chat_log.jsonl（可审计、可回看）"""
    try:
        with open(CHAT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "dir": direction,          # in=用户发来 / out=bot发出
                        "user": user or "unknown",
                        "chat_id": chat_id or "",
                        "msg": (msg or "")[:500],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception as e:
        print(f"[ChatLog Error] {e}")


def save_histories():
    """Compatibility no-op: histories are committed transactionally to SQLite."""


def load_histories():
    """Initialize storage and import the legacy JSON once."""
    imported = migrate_legacy_json()
    print(f"[History Store] SQLite ready; imported {imported} legacy messages")


def get_proxy():
    proxies = urllib.request.getproxies()
    if not proxies:
        proxies = {
            "http": "http://127.0.0.1:9674",
            "https": "http://127.0.0.1:9674",
        }
    return proxies


def get_history(session_key):
    return get_stored_history(session_key, MAX_HISTORY_TURNS)


def add_history(session_key, user_text, model_text):
    add_exchange(session_key, user_text, model_text)


def clear_history(session_key=None):
    clear_stored_history(session_key)


def _chat_lock(chat_id):
    with _CHAT_LOCKS_GUARD:
        return _CHAT_LOCKS.setdefault(chat_id or "default", threading.Lock())


def dispatch_message(client, data):
    """Deduplicate events and serialize messages from the same chat."""
    msg = data.event.message
    event_id = msg.message_id
    if not claim_event(event_id):
        print(f"[Duplicate Event] ignored: {event_id}")
        return
    chat_id = getattr(msg, "chat_id", None) or "default"
    with _chat_lock(chat_id):
        handle_message(client, data)


MEMORY_FILE = os.path.join(os.path.dirname(__file__), "persistent_memory.json")


def load_persistent_memory():
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def get_desktop_context():
    return (
        "【桌面环境】Windows 个人工作站；团队为 Hermes、反重力、Codex。\n"
        f"Obsidian 工作台：{runtime_value('obsidian_vault')}\n"
        + MEMORY_STORE.prompt_context("")
    )


def chat_with_deepseek(user_id, user_prompt):
    """调用 DeepSeek 官方 API (DeepSeek-V3 高速对话)"""
    cfg = load_config()
    ds_cfg = cfg.get("deepseek", {})
    api_key = ds_cfg.get("api_key", "")
    model_name = ds_cfg.get("model", "deepseek-chat")

    if not api_key:
        return "【DeepSeek 管家】已收到消息，但尚未配置 DeepSeek API Key。"

    session_key = f"{user_id}:deepseek"
    history = get_history(session_key)
    desktop_context = get_desktop_context()

    system_text = (
        f"你是用户的专属全能个人数字管家兼架构师。\n{desktop_context}\n"
        "回答原则：语气亲切干练、简明扼要、结构清晰，直接提供解决方案。请根据上下文和桌面历史记忆连续对话。"
    )

    messages = [{"role": "system", "content": system_text}]
    for item in history:
        messages.append({"role": item["role"], "content": item["content"]})
    messages.append({"role": "user", "content": user_prompt})

    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": 0.7,
    }

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=30).json()
        if "choices" in res and res["choices"]:
            reply_text = res["choices"][0]["message"]["content"]
            add_history(session_key, user_prompt, reply_text)
            return reply_text
        elif "error" in res:
            return f"DeepSeek 接口提示: {res['error'].get('message', str(res['error']))}"
        return "DeepSeek 未返回有效内容。"
    except Exception as e:
        return f"DeepSeek 调用异常: {str(e)}"


def chat_with_hermes(user_id, user_prompt, project_name=None):
    """【主脑管家】调用 WSL 中的真实 Hermes Agent 处理消息（替换原 DeepSeek 管家）"""
    # 附带最近的对话历史，让 Hermes 有上下文连续感
    session_key = f"{user_id}:hermes"
    history = get_history(session_key)
    history_text = ""
    if history:
        lines = []
        for item in history[-6:]:
            role = "用户" if item["role"] == "user" else "管家"
            lines.append(f"{role}: {item['content'][:200]}")
        history_text = "\n".join(lines) + "\n"

    memory_context = MEMORY_STORE.prompt_context(user_prompt, project_name=project_name)
    full_prompt = (
        f"你在飞书里是老板林家泽的贴身管家 Hermes。\n"
        f"以下是最近的对话上下文（可能为空）：\n{history_text}\n"
        f"{memory_context}\n"
        f"请针对老板最新的消息给出亲切、干练、简洁的中文回复：\n"
        f"老板：{user_prompt}"
    )
    result = call_hermes(full_prompt, timeout=150)
    reply_text = result.text
    if result.ok:
        add_history(session_key, user_prompt, reply_text)
    return result


def reply_feishu_msg(client, message_id, text_content):
    """向飞书用户回复消息"""
    req = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(json.dumps({"text": text_content}))
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.reply(req)
    if not resp.success():
        print(f"[Feishu Reply Error] code: {resp.code}, msg: {resp.msg}")
    else:
        log_chat("out", "bot", text_content)  # 发消息持久化


def send_progress_card(client, receive_id, title, body_text, details_text=None):
    """发送一张 interactive 进度卡片，返回 msg_id（供后续 PATCH 更新）"""
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
    if details_text is None:
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body_text}}],
        }
    else:
        card = {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": title}},
            "body": {"elements": [
                {"tag": "markdown", "content": body_text},
                {
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {
                        "title": {"tag": "plain_text", "content": "查看完整会议纪要"},
                        "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined", "size": "16px 16px"},
                        "icon_position": "right",
                        "icon_expanded_angle": -180,
                    },
                    "border": {"color": "grey", "corner_radius": "5px"},
                    "padding": "8px",
                    "elements": [{"tag": "markdown", "content": details_text[:18000]}],
                },
            ]},
        }
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("interactive")
            .content(json.dumps(card))
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.create(req)
    if resp.success():
        msg = resp.data.message_id
        print(f"[Feishu Card Created] msg_id={msg}")
        return msg
    print(f"[Feishu Card Error] code: {resp.code}, msg: {resp.msg}")
    return None


def update_progress_card(client, message_id, title, body_text):
    """PATCH 更新已发送的进度卡片（同一 msg_id，不刷屏）"""
    from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body_text}}
        ],
    }
    req = (
        PatchMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            PatchMessageRequestBody.builder()
            .content(json.dumps(card))
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.patch(req)
    if not resp.success():
        print(f"[Feishu Card Update Error] code: {resp.code}, msg: {resp.msg}")
    return resp.success()


TASK_STATUS_TEXT = {
    "queued": "排队中",
    "running": "执行中",
    "waiting_approval": "等待批准",
    "succeeded": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}


def _task_line(task):
    title = task["payload"].get("topic") or task["payload"].get("goal") or task["task_type"]
    return f"{task['id']} · {TASK_STATUS_TEXT.get(task['status'], task['status'])} · {task['task_type']} · {title[:35]}"


def _evolve_after_task(task, outcome_text):
    """Best-effort, one-rule postmortem for meaningful work tasks."""
    if task["task_type"] not in {"roundtable", "swarm"}:
        return None
    try:
        result = call_hermes(
            "你是受控进化复盘员，只做文本复盘，不操作电脑。根据下面任务结果，提炼一条以后可复用的工作规则。"
            "规则必须是流程或验证方法，不得写任务专属事实，不得改变系统约束、权限或人工审批。"
            "只输出一条中文规则，最多120字。\n\n"
            f"任务类型：{task['task_type']}\n任务结果：{str(outcome_text)[:4000]}",
            timeout=120,
        )
        record_task_event(task["id"], "evolution_agent_result", engine="hermes", ok=result.ok,
                          error_code=result.error_code, duration_ms=result.duration_ms)
        if not result.ok or not result.text.strip():
            return None
        memory = MEMORY_STORE.add_evolution(
            result.text.strip()[:500], task["id"], project_name=task["payload"].get("project"),
        )
        record_task_event(task["id"], "evolution_learned",
                          details={"memory_id": memory["id"], "scope": memory["scope"]})
        return memory
    except Exception as exc:
        record_task_event(task["id"], "evolution_failed",
                          details={"error": f"{type(exc).__name__}: {exc}"[:300]})
        return None


def _run_roundtable_task(client, task, context):
    topic = task["payload"]["topic"]
    msg_id = task["message_id"]
    chat_id = task["chat_id"]
    card_msg_id = None
    stance_lines = {}

    def on_event(event_type, payload):
        nonlocal card_msg_id
        context.check_cancelled()
        if event_type == "start":
            members = "、".join(payload["members"])
            context.progress("第1轮成员并行独立发言中")
            card_msg_id = send_progress_card(
                client, chat_id, f"🎙️ 圆桌任务 {task['id']}",
                f"**议题**：{topic}\n**参会**：{members}\n\n⏳ 第1轮并行发言中…",
            )
        elif event_type == "progress":
            text = payload.get("msg", "处理中")
            context.progress(text)
            if card_msg_id:
                body = f"**任务**：{task['id']}\n**议题**：{topic}\n\n⏳ {text}"
                if stance_lines:
                    body += "\n\n**当前立场**：\n" + "\n".join(f"- {k}：{v}" for k, v in stance_lines.items())
                update_progress_card(client, card_msg_id, "🎙️ 圆桌会议进行中", body)
        elif event_type == "speech":
            stance_lines[payload["agent"]] = payload.get("stance", "中立")
        elif event_type == "round_end":
            context.progress(f"第 {payload['round']} 轮结束")
        elif event_type == "agent_error":
            context.progress(f"{payload['agent']} 暂时不可用，已移出本场后续轮次")

    project = task["payload"].get("project")
    memory_context = MEMORY_STORE.prompt_context(topic, project_name=project)
    result = RoundTableV2().run(topic, on_event=on_event, cancel_check=context.check_cancelled,
                                memory_context=memory_context)
    context.progress("会议纪要已生成")
    degraded = ""
    if result.get("unavailable_agents"):
        degraded = "\n\n⚠️ **降级成员**：" + "、".join(result["unavailable_agents"])
    final_body = f"**任务**：{task['id']}\n**议题**：{topic}\n✅ **会议完成**（{result['rounds_used']} 轮）{degraded}\n\n{result['final_summary'][:800]}"
    if card_msg_id:
        update_progress_card(client, card_msg_id, "✅ 圆桌会议完成", final_body)
    else:
        reply_feishu_msg(client, msg_id, f"🏁【会议完成】\n{result['final_summary']}")
    minutes_rel = f"roundtable/{result['session_id']}/minutes.md"
    minutes_path = os.path.join(os.path.dirname(__file__), *minutes_rel.split("/"))
    try:
        with open(minutes_path, encoding="utf-8") as handle:
            minutes_text = handle.read()
    except OSError:
        minutes_text = ""
    minutes_card = send_progress_card(
        client, chat_id, f"📎 会议纪要 · {task['id']}",
        f"会议已完成。点击下方标题展开查看。\n\n本地归档：`{minutes_rel}`",
        details_text=minutes_text,
    ) if minutes_text else None
    if not minutes_card:
        reply_feishu_msg(client, msg_id, f"📎 任务 {task['id']} · 纪要：{minutes_rel}")
    if project:
        MEMORY_STORE.add(result["final_summary"], project_name=project, source_type="roundtable",
                         source_id=task["id"], source_path=minutes_rel)
    context.progress("会议完成，正在执行一次任务后进化复盘")
    evolution = _evolve_after_task(task, result["final_summary"])
    if evolution and card_msg_id:
        update_progress_card(client, card_msg_id, "✅ 圆桌会议完成",
                             final_body + f"\n\n🧬 **本次进化**：{evolution['content']}（ID {evolution['id']}）")
    return {"session_id": result["session_id"], "rounds": result["rounds_used"],
            "summary": result["final_summary"], "unavailable_agents": result.get("unavailable_agents", {}),
            "evolution_memory_id": evolution["id"] if evolution else None}


def _run_swarm_task(client, task, context):
    goal = task["payload"]["goal"]
    msg_id = task["message_id"]
    project = task["payload"].get("project")

    def on_agent_speak(role_tag, message_text):
        context.progress(f"{role_tag} 已完成当前阶段")
        record_task_event(task["id"], "agent_stage_completed", details={"role": role_tag})
    if task["phase"] == "planning":
        context.progress("Hermes PM → 反重力架构师 → Codex 只读探索，正在生成方案")
        plan = swarm_orchestrator.plan_collaborative_project(
            goal, project_name=project,
            memory_context=MEMORY_STORE.prompt_context(goal, project_name=project),
            on_agent_message=on_agent_speak, cancel_check=context.check_cancelled,
        )
        preview = (
            f"**任务**：{task['id']}\n**项目**：{plan['project_name']}\n"
            f"**目标**：{goal}\n\n**方案摘要**：\n{plan['requirements'][:500]}\n\n"
            f"**影响与风险**：\n{plan['impact_and_risks'][:500]}\n\n"
            f"⏳ 30 分钟内发送 `批准任务 {task['id']}`，或 `拒绝任务 {task['id']}`。"
        )
        send_progress_card(client, task["chat_id"], "🛂 写入预审", preview)
        context.wait_for_approval(plan)
    plan = task.get("plan")
    if not plan or not task.get("approved_at"):
        raise RuntimeError("缺少有效写入批准")
    context.progress("已批准，反重力第一棒即将独占写入工作区")
    result = swarm_orchestrator.execute_collaborative_project(
        plan, on_agent_message=on_agent_speak, cancel_check=context.check_cancelled,
    )
    if not result["success"]:
        raise RuntimeError("Codex 实现或验证失败，请查看阶段输出后重试")
    if project:
        MEMORY_STORE.add(result["final_report"], project_name=project, source_type="swarm",
                         source_id=task["id"], source_path=f"Obsidian/{result['project_name']}")
    context.progress("协作完成，正在执行一次任务后进化复盘")
    evolution = _evolve_after_task(task, result["final_report"])
    evolution_text = f"\n\n🧬 本次进化：{evolution['content']}（ID {evolution['id']}）" if evolution else ""
    reply_feishu_msg(client, msg_id, f"✅ 协作任务 {task['id']} 已完成，项目：{result['project_name']}\n\n{result['final_report'][:1000]}{evolution_text}")
    return {"project_name": result["project_name"], "success": result["success"],
            "final_report": result["final_report"],
            "evolution_memory_id": evolution["id"] if evolution else None}


def _run_chat_task(client, task, context):
    context.progress("Hermes 正在回复")
    payload = task["payload"]
    result = chat_with_hermes(task["user_id"], payload["prompt"], payload.get("project"))
    record_task_event(task["id"], "agent_result", engine="hermes", ok=result.ok,
                      error_code=result.error_code, duration_ms=result.duration_ms)
    reply_feishu_msg(client, task["message_id"], result.text)
    if not result.ok:
        raise RuntimeError(f"Hermes {result.error_code}: {result.text[:300]}")
    return {"reply": result.text}


def _run_health_task(client, task, context):
    context.progress("三条 Agent 通道正在并行探针")
    results = deep_health_probe()
    lines = [f"- {name}: {'✅' if result.ok else '❌'} {result.text[:120]} ({result.duration_ms}ms)"
             for name, result in results.items()]
    reply_feishu_msg(client, task["message_id"], "🩺【深度健康结果】\n" + "\n".join(lines))
    return {name: {"ok": result.ok, "error_code": result.error_code,
                   "duration_ms": result.duration_ms} for name, result in results.items()}


def execute_background_task(client, task, context):
    try:
        if task["task_type"] == "roundtable":
            return _run_roundtable_task(client, task, context)
        if task["task_type"] == "swarm":
            return _run_swarm_task(client, task, context)
        if task["task_type"] == "chat":
            return _run_chat_task(client, task, context)
        if task["task_type"] == "health_probe":
            return _run_health_task(client, task, context)
        raise ValueError(f"未知任务类型: {task['task_type']}")
    except TaskCancelled:
        reply_feishu_msg(client, task["message_id"], f"🛑 任务 {task['id']} 已取消。")
        raise
    except Exception as exc:
        _evolve_after_task(task, f"任务失败：{type(exc).__name__}: {exc}")
        reply_feishu_msg(client, task["message_id"], f"❌ 任务 {task['id']} 执行失败：{type(exc).__name__}: {exc}")
        raise


def handle_message(client, data: P2ImMessageReceiveV1):
    """处理飞书收到的消息事件"""
    msg = data.event.message
    sender = data.event.sender
    user_id = "default_user"
    if sender and sender.sender_id:
        user_id = sender.sender_id.user_id or sender.sender_id.open_id or "default_user"
    msg_id = msg.message_id
    msg_type = msg.message_type

    print(f"\n[收到飞书消息] User: {user_id} | Type: {msg_type} | MsgID: {msg_id}")

    if msg_type == "text":
        content_dict = json.loads(msg.content)
        raw_text = content_dict.get("text", "").strip()
        clean_text = re.sub(r"@_user_\d+\s*", "", raw_text).strip()
        print(f"[消息内容]: {clean_text}")
        chat_id_in = getattr(msg, "chat_id", None) or ""
        log_chat("in", user_id, clean_text, chat_id_in)  # 收消息持久化（带 chat_id）

        lower_text = clean_text.lower()

        # 持久化任务控制命令
        TASK_CONTROLLER.store.expire_approvals()
        if lower_text in ["任务列表", "任务", "tasks", "task list", "任务列表 全部"]:
            include_all = lower_text == "任务列表 全部"
            tasks = TASK_CONTROLLER.store.list(chat_id_in, limit=10, include_all=include_all)
            body = "\n".join(f"- {_task_line(task)}" for task in tasks) if tasks else "暂无任务"
            reply_feishu_msg(client, msg_id, f"📋【最近任务】\n{body}\n\n可用：任务 <ID>、取消任务 <ID>、重试任务 <ID>、任务列表 全部")
            return

        match = re.fullmatch(r"任务\s+([0-9a-f]{4,10})", lower_text)
        if match:
            task = TASK_CONTROLLER.store.find(match.group(1), chat_id_in)
            if not task:
                reply_feishu_msg(client, msg_id, "未找到唯一匹配的任务，请发送「任务列表」查看完整 ID。")
                return
            detail = [
                f"🧾【任务 {task['id']}】",
                f"类型：{task['task_type']}",
                f"状态：{TASK_STATUS_TEXT.get(task['status'], task['status'])}",
                f"尝试次数：{task['attempt']}",
                f"阶段：{task.get('phase') or 'execute'}",
                f"进度：{task.get('progress') or '暂无'}",
            ]
            if task.get("error"):
                detail.append(f"错误：{task['error'][:500]}")
            reply_feishu_msg(client, msg_id, "\n".join(detail))
            return

        match = re.fullmatch(r"取消任务\s+([0-9a-f]{4,10})", lower_text)
        if match:
            task = TASK_CONTROLLER.store.find(match.group(1), chat_id_in)
            ok = bool(task and TASK_CONTROLLER.store.request_cancel(task["id"]))
            reply_feishu_msg(client, msg_id, f"{'🛑 已提交取消请求：' + task['id'] if ok else '任务不存在、已结束或 ID 不唯一。'}")
            return

        match = re.fullmatch(r"重试任务\s+([0-9a-f]{4,10})", lower_text)
        if match:
            old = TASK_CONTROLLER.store.find(match.group(1), chat_id_in)
            task = TASK_CONTROLLER.retry(old["id"], message_id=msg_id) if old else None
            reply_feishu_msg(client, msg_id, f"{'🔁 已创建重试任务：' + task['id'] if task else '仅失败、取消或已完成的唯一任务可以重试。'}")
            return

        match = re.fullmatch(r"批准任务\s+([0-9a-f]{4,10})", lower_text)
        if match:
            task = TASK_CONTROLLER.store.find(match.group(1), chat_id_in)
            outcome = TASK_CONTROLLER.approve(task["id"]) if task else "not_found"
            messages = {
                "approved": f"✅ 已批准任务 {task['id']}，写入阶段已进入本会话队尾。",
                "already_approved": f"ℹ️ 任务 {task['id']} 已批准，无需重复操作。" if task else "任务不存在。",
                "not_waiting": "任务当前不在等待批准状态。",
                "not_found": "未找到唯一匹配的任务。",
            }
            reply_feishu_msg(client, msg_id, messages[outcome])
            return

        match = re.fullmatch(r"拒绝任务\s+([0-9a-f]{4,10})", lower_text)
        if match:
            task = TASK_CONTROLLER.store.find(match.group(1), chat_id_in)
            ok = bool(task and task["status"] == "waiting_approval" and TASK_CONTROLLER.store.request_cancel(task["id"]))
            reply_feishu_msg(client, msg_id, f"{'🚫 已拒绝并取消任务 ' + task['id'] if ok else '任务不存在或不在等待批准状态。'}")
            return

        memory_command = parse_memory_command(clean_text)
        if memory_command:
            if memory_command["action"] == "remember":
                memory = MEMORY_STORE.add(memory_command["content"], project_name=memory_command.get("project"),
                                          source_type="manual", source_id=msg_id)
                scope = f"项目 {memory['project_name']}" if memory["scope"] == "project" else "全局"
                reply_feishu_msg(client, msg_id, f"🧠 已记住（{scope}）· ID {memory['id']}")
            elif memory_command["action"] == "list_memories":
                rows = MEMORY_STORE.list(memory_command.get("project"), limit=20)
                body = "\n".join(f"- {row['id']} · {row['content'][:100]}" for row in rows) or "暂无记忆"
                reply_feishu_msg(client, msg_id, f"🧠【记忆列表】\n{body}")
            else:
                memory = MEMORY_STORE.find(memory_command["id"])
                ok = bool(memory and MEMORY_STORE.delete(memory["id"]))
                reply_feishu_msg(client, msg_id, "🗑️ 已删除记忆。" if ok else "未找到唯一匹配的记忆 ID。")
            return

        # 0. 清空记忆/重置对话
        if lower_text in [
            "清空上下文",
            "重置记忆",
            "新建对话",
            "清空记忆",
            "reset",
            "clear",
        ]:
            clear_history()
            reply_feishu_msg(
                client,
                msg_id,
                "🧹【中枢记忆已清空】已为您重置所有 Agent 的对话上下文，开启全新对话！",
            )
            return

        # 1. 团队全员集合与打招呼（排除开会/圆桌类关键词：开会优先，不串台）
        if (
            ("打招呼" in lower_text
            or "出来" in lower_text
            or "集合" in lower_text
            or "自我介绍" in lower_text
            or "报到" in lower_text)
            and not any(kw in lower_text for kw in ["开会", "圆桌", "头脑风暴", "会议"])
        ):
            reply_feishu_msg(client, msg_id, "📢【多智能体团队全员集合】收到老板指令，数字员工依次出列报到！")
            time.sleep(0.5)
            reply_feishu_msg(client, msg_id, "👔 [产品经理 · Hermes 主脑]:\n“老板好！我是 Hermes 主脑兼 PM，负责为您统筹项目需求、主持圆桌会议并向您汇报决策结果！”")
            time.sleep(0.5)
            reply_feishu_msg(client, msg_id, "📐 [反重力 · 首席架构师]:\n“老板好！我是反重力架构师，负责系统总体架构、数据流与接口契约设计，把关技术选型！”")
            time.sleep(0.5)
            reply_feishu_msg(client, msg_id, "💻 [Codex · 核心工程师]:\n“老板好！我是 Codex，负责在受限工作区内实现代码、运行测试与排查问题，并汇报可验证结果。”")
            return

        # 2. 查询状态
        if lower_text in ["状态", "健康", "@status", "status", "health"]:
            now_str = time.strftime("%Y-%m-%d %H:%M:%S")
            counts = TASK_CONTROLLER.store.counts()
            active = counts.get("queued", 0) + counts.get("running", 0) + counts.get("waiting_approval", 0)
            available = lightweight_health()
            recent = {row["engine"]: row for row in engine_health()}
            engine_lines = []
            for name in ("hermes", "antigravity", "codex"):
                row = recent.get(name) or {}
                real = "尚无真实调用" if not row else (
                    f"最近 {'成功' if row.get('available') else '失败/' + str(row.get('last_error_code') or 'unknown')} · {row.get('duration_ms') or 0}ms"
                )
                engine_lines.append(f"{'✅' if available[name] else '❌'} {name}: {real}")
            reply = (
                f"📊【飞书多智能体协同工作室】\n"
                f"⏰ 时间: {now_str}\n"
                f"-------------------------\n"
                f"👔 产品经理: Hermes 主脑 (主持研讨与决策)\n"
                f"📐 首席架构师:「反重力」(系统时序/接口设计)\n"
                f"💻 核心工程师: Codex (受限工作区实现与测试)\n"
                f"🫀 服务状态: 正常 | 活跃任务: {active} | 排队: {counts.get('queued', 0)} | 执行中: {counts.get('running', 0)}\n"
                + "\n".join(engine_lines) + "\n"
                f"-------------------------\n"
                f"📂 知识中枢: Obsidian E:\\\\Obsidian_Vault\\\\多agent\\\\ (三级规范)\n"
                f"💡 触发指令: 发送「开会 <议题>」召集圆桌讨论；「协作 <项目需求>」启动协同落地！"
            )
            reply_feishu_msg(client, msg_id, reply)
            return

        if lower_text == "深度健康":
            task = TASK_CONTROLLER.submit("health_probe", chat_id_in, user_id, msg_id, {})
            reply_feishu_msg(client, msg_id, f"✅ 已收到，深度健康任务 {task['id']} 已在后台排队。")
            return

        # 2. 圆桌会议模式：多Agent独立思考 + 开放式交流意见（V2 引擎：收敛循环+黑板+进度卡片）
        # 含"开会/圆桌/头脑风暴/会议"即触发（包含匹配，兼容"开始开会"等说法）
        is_roundtable_trigger = (
            "开会" in lower_text
            or "圆桌" in lower_text
            or lower_text.startswith("讨论")
            or "头脑风暴" in lower_text
            or "会议" in lower_text
        )

        if is_roundtable_trigger:
            # 提取议题：去掉开会类动词 + 开场白尾巴（"成员先做自我介绍"等）
            tagged_text, project = extract_project_tag(clean_text)
            topic = re.sub(
                r"^(开始|现在|大家|请|帮我|我们)?\s*(开会|圆桌|讨论|头脑风暴)\s*[:：]?\s*",
                "",
                tagged_text,
            ).strip()
            topic = re.sub(r"(成员|大家|各位|先)?\s*(做?自我介绍|打招呼|报到|集合)\s*$", "", topic).strip()
            # 议题太短（<4字）时自动补全为可讨论的完整议题
            if len(topic) < 4:
                if topic in ("成员", "团队"):
                    topic = "团队成员的职责分工、自我介绍与协作边界"
                elif topic == "自我介绍":
                    topic = "团队成员自我介绍与各自能力专长"
                else:
                    topic = f"关于「{topic}」的方案设计与讨论"
            if not topic:
                topic = "（未指定议题，请各位自由讨论当前重要事项）"

            task = TASK_CONTROLLER.submit(
                "roundtable", chat_id_in, user_id, msg_id, {"topic": topic, "project": project},
            )
            reply_feishu_msg(client, msg_id, f"✅ 已收到。圆桌任务已排队：{task['id']}\n发送「任务 {task['id']}」查看进度，或「取消任务 {task['id']}」。")
            return

        # 2. 触发多智能体全员协同研讨与落地模式 (Swarm Mode)
        is_swarm_trigger = (
            lower_text.startswith("协作")
            or lower_text.startswith("开工")
            or lower_text.startswith("项目")
            or lower_text.startswith("团队")
            or "帮我做" in lower_text
            or "设计系统" in lower_text
            or "构建系统" in lower_text
            or "开发" in lower_text
        )

        if is_swarm_trigger:
            tagged_text, project = extract_project_tag(clean_text)
            task_goal = re.sub(r"^(协作|开工|项目|团队)\s*", "", tagged_text).strip()
            if not task_goal:
                reply_feishu_msg(client, msg_id, "请在「协作」后写明具体目标，例如：协作 为项目增加健康检查。")
                return
            task = TASK_CONTROLLER.submit(
                "swarm", chat_id_in, user_id, msg_id, {"goal": task_goal, "project": project},
            )
            reply_feishu_msg(client, msg_id, f"✅ 已收到。协作任务只读规划已排队：{task['id']}\n规划完成后需再次批准才会写入。")
            return

        # 3. 普通对话也持久化并后台执行，先确认接收。
        prompt, project = extract_project_tag(clean_text)
        task = TASK_CONTROLLER.submit("chat", chat_id_in, user_id, msg_id,
                                      {"prompt": prompt, "project": project})
        reply_feishu_msg(client, msg_id, f"✅ 已收到（任务 {task['id']}），Hermes 将在后台回复。")


def main():
    cfg = load_config()
    warnings = validate_startup(cfg)
    app_id = cfg["feishu"]["app_id"]
    app_secret = cfg["feishu"]["app_secret"]

    print("==================================================")
    for warning in warnings:
        print(f"[Degraded Startup] {warning}")
    print("🚀 正在启动飞书 多智能体协同工作室网关 (Swarm + Obsidian)...")
    print("==================================================")
    load_histories()  # 恢复对话历史（重启不丢）

    client = (
        lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    )
    TASK_CONTROLLER.start(lambda task, context: execute_background_task(client, task, context))

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(
            lambda data: MESSAGE_POOL.submit(dispatch_message, client, data)
        )
        .build()
    )

    ws_client = lark.ws.Client(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG,
    )

    print("✅ 飞书 WebSocket 长连接已建立！多智能体协作与 Obsidian 共用大脑已挂载！")
    ws_client.start()


if __name__ == "__main__":
    main()
