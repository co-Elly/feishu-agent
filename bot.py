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
from swarm_orchestrator import swarm_orchestrator, roundtable
from roundtable_engine import RoundTableV2
from conversation_store import (
    add_exchange,
    claim_event,
    clear_history as clear_stored_history,
    get_history as get_stored_history,
    migrate_legacy_json,
)
from agent_runtime import isolated_prompt_file
from task_manager import TaskCancelled, TaskController

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
CHAT_LOG_PATH = os.path.join(os.path.dirname(__file__), "chat_log.jsonl")      # 收发消息日志（append-only）
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "chat_history.json")    # 对话历史持久化

MAX_HISTORY_TURNS = 10
MESSAGE_POOL = ThreadPoolExecutor(max_workers=int(os.environ.get("FEISHU_MAX_WORKERS", "4")))
TASK_CONTROLLER = TaskController(max_workers=int(os.environ.get("FEISHU_TASK_WORKERS", "2")))
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


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


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
    mem = load_persistent_memory()
    milestones = mem.get("project_milestones", [])
    recent_tasks = mem.get("task_history", [])[-5:]
    sched_tasks = mem.get("active_scheduled_tasks", [])

    ms_text = "\n".join(
        [
            f"- [{m.get('time')}] {m.get('project')}: {m.get('summary')}"
            for m in milestones
        ]
    )
    task_text = (
        "\n".join(
            [
                f"- [{t.get('time')}] 【{t.get('agent')}】任务: {t.get('task')}"
                for t in recent_tasks
            ]
        )
        if recent_tasks
        else "暂无最近任务"
    )
    sched_text = "\n".join(
        [
            f"- {s.get('task_name')} ({s.get('schedule')}): {s.get('action')}"
            for s in sched_tasks
        ]
    )

    return f"""
【用户与桌面环境全局记忆档案】：
- 用户姓名: 林家泽
- 操作系统: Windows 11 (WSL2 Ubuntu 24.04)
- 团队成员:「Hermes 主脑」(产品经理·主持人)、「反重力」(Antigravity 架构师)、「Codex」(核心工程师)
- Obsidian 项目总工作台: E:\\Obsidian_Vault\\多agent\\ (采用三级目录规范)
- 系统常驻定时任务：
{sched_text}
- 电脑已完成的项目里程碑：
{ms_text}
- 最近执行过的任务记录：
{task_text}
（你可以完全回忆并基于上述已完成的项目、定时任务与历史记录，与用户无缝对话或继续迭代！）
"""


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


def chat_with_hermes(user_id, user_prompt):
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

    full_prompt = (
        f"你在飞书里是老板林家泽的贴身管家 Hermes。\n"
        f"以下是最近的对话上下文（可能为空）：\n{history_text}\n"
        f"请针对老板最新的消息给出亲切、干练、简洁的中文回复：\n"
        f"老板：{user_prompt}"
    )

    try:
        # 任务书文件方式（避免 cmd.exe 单引号坑：heredoc 含引号会 unexpected EOF）
        # + -t terminal 限工具集：系统提示词 ~17万→几万 tokens，单次调用 158s → ~20s（2026-08-16 实测）
        # ⚠️ 参数顺序：-z 必须紧跟提示词，-t 放最后（argparse 对 -z 后接 -t 报 expected one argument）
        task_file = isolated_prompt_file("hermes_chat_", full_prompt)
        bash_path = task_file.replace("E:", "/mnt/e").replace("\\", "/") if task_file.startswith("E:") else task_file
        # ⚠️ 参数顺序：-z 后必须紧跟 prompt，-t 选项放最后（argparse 不允许 -z -t ... 交错）
        cmd = ["wsl.exe", "-e", "bash", "-lc", 'hermes -z "$(cat "$1")" -t terminal', "bash", bash_path]
        proc = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=150,
            cwd=r"E:\feishu-agent",
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        reply_text = stdout if stdout else (f"Hermes 报错: {stderr}" if stderr else "Hermes 执行完成，无输出。")
        add_history(session_key, user_prompt, reply_text)
        return reply_text
    except subprocess.TimeoutExpired:
        return "【管家】思考时间较长，请稍等再问我一次～（Hermes 超时 150 秒）"
    except Exception as e:
        return f"【管家】调用异常: {str(e)}"


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


def send_progress_card(client, receive_id, title, body_text):
    """发送一张 interactive 进度卡片，返回 msg_id（供后续 PATCH 更新）"""
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body_text}}
        ],
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
            reply_feishu_msg(client, msg_id, f"{payload['agent']}（{payload.get('stance','')}）：\n{payload['text']}")
        elif event_type == "round_end":
            context.progress(f"第 {payload['round']} 轮结束")
        elif event_type == "agent_error":
            context.progress(f"{payload['agent']} 暂时不可用，已移出本场后续轮次")
            reply_feishu_msg(client, msg_id, f"⚠️ {payload['agent']} 暂时不可用：{payload['error'][:300]}")

    result = RoundTableV2().run(topic, on_event=on_event, cancel_check=context.check_cancelled)
    context.progress("会议纪要已生成")
    degraded = ""
    if result.get("unavailable_agents"):
        degraded = "\n\n⚠️ **降级成员**：" + "、".join(result["unavailable_agents"])
    final_body = f"**任务**：{task['id']}\n**议题**：{topic}\n✅ **会议完成**（{result['rounds_used']} 轮）{degraded}\n\n{result['final_summary'][:800]}"
    if card_msg_id:
        update_progress_card(client, card_msg_id, "✅ 圆桌会议完成", final_body)
    else:
        reply_feishu_msg(client, msg_id, f"🏁【会议完成】\n{result['final_summary']}")
    reply_feishu_msg(client, msg_id, f"📎 任务 {task['id']} · 纪要：roundtable/{result['session_id']}/minutes.md")
    return {"session_id": result["session_id"], "rounds": result["rounds_used"], "summary": result["final_summary"], "unavailable_agents": result.get("unavailable_agents", {})}


def _run_swarm_task(client, task, context):
    goal = task["payload"]["goal"]
    msg_id = task["message_id"]
    context.progress("多智能体团队已开始协作")

    def on_agent_speak(role_tag, message_text):
        context.progress(f"{role_tag} 已完成当前阶段")
        reply_feishu_msg(client, msg_id, f"{role_tag}:\n{message_text}")

    result = swarm_orchestrator.run_collaborative_project(
        goal, on_agent_message=on_agent_speak, cancel_check=context.check_cancelled,
    )
    if not result["success"]:
        raise RuntimeError("Codex 实现或验证失败，请查看阶段输出后重试")
    reply_feishu_msg(client, msg_id, f"✅ 协作任务 {task['id']} 已完成，项目：{result['project_name']}")
    return {"project_name": result["project_name"], "success": result["success"], "final_report": result["final_report"]}


def execute_background_task(client, task, context):
    try:
        if task["task_type"] == "roundtable":
            return _run_roundtable_task(client, task, context)
        if task["task_type"] == "swarm":
            return _run_swarm_task(client, task, context)
        raise ValueError(f"未知任务类型: {task['task_type']}")
    except TaskCancelled:
        reply_feishu_msg(client, task["message_id"], f"🛑 任务 {task['id']} 已取消。")
        raise
    except Exception as exc:
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
        if lower_text in ["任务列表", "任务", "tasks", "task list"]:
            tasks = TASK_CONTROLLER.store.list(chat_id_in, limit=10)
            body = "\n".join(f"- {_task_line(task)}" for task in tasks) if tasks else "暂无任务"
            reply_feishu_msg(client, msg_id, f"📋【最近任务】\n{body}\n\n可用：任务 <ID>、取消任务 <ID>、重试任务 <ID>")
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
            reply = (
                f"📊【飞书多智能体协同工作室】\n"
                f"⏰ 时间: {now_str}\n"
                f"-------------------------\n"
                f"👔 产品经理: Hermes 主脑 (主持研讨与决策)\n"
                f"📐 首席架构师:「反重力」(系统时序/接口设计)\n"
                f"💻 核心工程师: Codex (受限工作区实现与测试)\n"
                f"🫀 服务状态: 正常 | 活跃任务: {active} | 排队: {counts.get('queued', 0)} | 执行中: {counts.get('running', 0)}\n"
                f"-------------------------\n"
                f"📂 知识中枢: Obsidian E:\\\\Obsidian_Vault\\\\多agent\\\\ (三级规范)\n"
                f"💡 触发指令: 发送「开会 <议题>」召集圆桌讨论；「协作 <项目需求>」启动协同落地！"
            )
            reply_feishu_msg(client, msg_id, reply)
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
            topic = re.sub(
                r"^(开始|现在|大家|请|帮我|我们)?\s*(开会|圆桌|讨论|头脑风暴)\s*[:：]?\s*",
                "",
                clean_text,
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
                "roundtable", chat_id_in, user_id, msg_id, {"topic": topic},
            )
            reply_feishu_msg(client, msg_id, f"🎙️ 圆桌任务已排队：{task['id']}\n发送「任务 {task['id']}」查看进度，或「取消任务 {task['id']}」。")
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
            task_goal = re.sub(r"^(协作|开工|项目|团队)\s*", "", clean_text).strip()
            if not task_goal:
                reply_feishu_msg(client, msg_id, "请在「协作」后写明具体目标，例如：协作 为项目增加健康检查。")
                return
            task = TASK_CONTROLLER.submit(
                "swarm", chat_id_in, user_id, msg_id, {"goal": task_goal},
            )
            reply_feishu_msg(client, msg_id, f"🚀 协作任务已排队：{task['id']}\n发送「任务 {task['id']}」查看进度，或「取消任务 {task['id']}」。")
            return

        # 3. 默认 Hermes 主脑管家对话（原 DeepSeek 管家已替换）
        res = chat_with_hermes(user_id, clean_text)
        reply_feishu_msg(client, msg_id, res)


def main():
    cfg = load_config()
    app_id = cfg["feishu"]["app_id"]
    app_secret = cfg["feishu"]["app_secret"]

    print("==================================================")
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
