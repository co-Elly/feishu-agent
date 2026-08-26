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
from control_store import engine_health, record_task_event, task_events
from constraint_envelope import (ConstraintEnvelopeError, build_constraint_envelope,
                                 envelope_prompt, validate_constraint_envelope)
from gateway_guard import acquire_ingress_lease
from memory_store import MemoryStore
from service_health import guardian_status
from settings import load_config, redact, runtime_value, validate_startup
from task_manager import (TaskBlocked, TaskCancelled, TaskController,
                          TaskNeedsReview, TaskParked, approval_receipt_valid,
                          collaboration_handoff_valid)
from workflow_store import WORKFLOW_STORE
from observability import classify_failure, ensure_observability_schema, record_span
from isolated_workspace import recover_abandoned_merges
from ingress_bridge import IngressBridge
from workspace_lease import ensure_lease_schema
from report_workflow import (ReportWorkflowError, execute_read_only_report,
                             is_read_only_report_request, parse_report_request,
                             plan_read_only_report, recover_existing_report,
                             report_request_paths)

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
_CHAT_LOG_LOCK = threading.Lock()


def log_chat(direction, user, msg, chat_id=None):
    """消息收发日志持久化：in/out 都记，追加到 chat_log.jsonl（可审计、可回看）"""
    try:
        with _CHAT_LOG_LOCK:
            if os.path.isfile(CHAT_LOG_PATH) and os.path.getsize(CHAT_LOG_PATH) >= 5 * 1024 * 1024:
                archive_dir = os.path.join(runtime_value("workspace_dir"), "log-archive")
                os.makedirs(archive_dir, exist_ok=True)
                archive = os.path.join(archive_dir, f"chat-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
                os.replace(CHAT_LOG_PATH, archive)
                archives = sorted(
                    (os.path.join(archive_dir, name) for name in os.listdir(archive_dir)
                     if name.startswith("chat-") and name.endswith(".jsonl")),
                    key=os.path.getmtime, reverse=True,
                )
                for old in archives[10:]:
                    os.remove(old)
            with open(CHAT_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "dir": direction,          # in=用户发来 / out=bot发出
                        "user": user or "unknown",
                        "chat_id": chat_id or "",
                        "msg": redact(str(msg or ""))[:500],
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


def _latest_roundtable(chat_id, project=None):
    """Return the completed meeting that owns the next decision in this chat."""
    list_tasks = getattr(TASK_CONTROLLER.store, "list", None)
    if not list_tasks:
        return None
    tasks = list_tasks(chat_id, limit=30, include_all=True)
    if project:
        for task in tasks:
            payload = task.get("payload") or {}
            if payload.get("project") != project:
                continue
            if task["task_type"] == "swarm" and payload.get("meeting_task_id"):
                return None
            if task["task_type"] == "roundtable" and task["status"] == "succeeded":
                return task
        return None
    latest = tasks[0] if tasks else None
    if latest and latest["task_type"] == "roundtable" and latest["status"] == "succeeded":
        return latest
    return None


def _meeting_handoff_goal(meeting, workflow=None):
    payload = meeting.get("payload") or {}
    result = meeting.get("result") or {}
    summary = str(result.get("summary") or "").strip()
    ledger = (workflow or {}).get("task_ledger") or {}
    decisions = ledger.get("boss_decisions") or []
    decision_text = "\n".join(
        f"- 第 {item.get('sequence', index)} 次拍板：{item.get('decision', '')}"
        for index, item in enumerate(decisions, 1)
    ) or "- （拍板内容已包含在最终会议共识中）"
    root_request = (workflow or {}).get("root_request") or payload.get("root_request") or payload.get("topic")
    return (
        f"根据圆桌任务 {meeting['id']} 已拍板的会议共识开始协作落地。\n"
        f"用户原始请求：{root_request or '（未记录）'}\n"
        f"老板拍板记录：\n{decision_text}\n"
        f"最终会议共识：{summary or '（请从项目记忆中读取）'}"
    )


def _workflow_current_meeting(workflow, chat_id):
    if not workflow or not workflow.get("current_task_id"):
        return None
    meeting = TASK_CONTROLLER.store.find(workflow["current_task_id"], chat_id)
    if not meeting or meeting.get("task_type") != "roundtable" or meeting.get("status") != "succeeded":
        return None
    return meeting


def _decision_ends_discussion(text):
    compact = re.sub(r"\s+", "", str(text or ""))
    return bool(re.search(
        r"(?:无需|不用|不必)(?:再|继续)?(?:讨论|续会)|直接(?:结束会议|定稿|进入协作确认)",
        compact,
    ))


def _task_constraints(task):
    payload = task.get("payload") or {}
    envelope = payload.get("constraint_envelope")
    if not envelope:
        raise ConstraintEnvelopeError(
            "该历史任务创建于全局约束信封上线之前，不得直接续会或交接"
        )
    return validate_constraint_envelope(envelope)


def _can_control_task(task, actor_user_id):
    """Task mutations are owner-only; legacy tasks without an owner remain controllable."""
    if not task:
        return False
    owner = str(task.get("user_id") or "").strip()
    return not owner or owner == str(actor_user_id or "").strip()


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
        except Exception as exc:
            print(f"[Legacy Memory Warning] {type(exc).__name__}: {exc}")
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
        f"这是普通聊天通道：你没有操作电脑、文件、飞书或其他 Agent 的权限，不得声称已经执行、修改、配置或测试。\n"
        f"如果老板要求实际操作，请明确说明需要发送“协作 <目标>”创建可审计任务；不要在聊天里模拟完成。\n"
        f"必须区分已验证事实、推测和未来动作；没有任务状态、文件或命令证据时，不得声称“已经搞定”或“全部实测通过”。\n"
        f"不得承诺定时主动跟进或稍后主动汇报。涉及已有后台任务时，只报告任务 ID、当前可见状态和一个下一步动作。\n"
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


# ---------------- 飞书 API 限流退避（P1） ----------------
FEISHU_RATE_LIMIT_CODE = 99991663     # 触发限流 → 指数退避 1/2/4s 重试
FEISHU_FREQUENCY_CODE = 99991668      # 发送频率超限 → 固定退避 30s 重试一次
_FEISHU_RETRY_DELAYS = {FEISHU_RATE_LIMIT_CODE: [1, 2, 4], FEISHU_FREQUENCY_CODE: [30]}


def _feishu_call_with_retry(send_fn):
    """执行 send_fn() 并对飞书限流/频控错误码自动退避重试。

    send_fn 返回 resp；resp.success() 为 False 且 code 命中退避表时按表内
    延迟序列 sleep 后重发，耗尽次数仍失败则返回最后一次 resp。
    """
    resp = send_fn()
    if resp is not None and resp.success():
        return resp
    code = getattr(resp, "code", None)
    delays = _FEISHU_RETRY_DELAYS.get(code)
    for delay in delays or []:
        print(f"[Feishu Retry] code={code}, backing off {delay}s")
        time.sleep(delay)
        resp = send_fn()
        if resp is not None and resp.success():
            return resp
    return resp


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
    resp = _feishu_call_with_retry(lambda: client.im.v1.message.reply(req))
    if not resp.success():
        print(f"[Feishu Reply Error] code: {resp.code}, msg: {resp.msg}")
    else:
        log_chat("out", "bot", text_content)  # 发消息持久化


FEISHU_MSG_MAX_LEN = 1800


def send_feishu_msg(client, chat_id, text_content, reply_to=None):
    """发送文本消息（可指定 chat_id 或回复某条消息），返回 msg_id 或 None。

    超过 FEISHU_MSG_MAX_LEN 自动分片，后续片回复前一片形成连贯阅读链。
    """
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
    chunks = [text_content[i:i + FEISHU_MSG_MAX_LEN]
              for i in range(0, len(text_content), FEISHU_MSG_MAX_LEN)] or [""]
    last_msg_id = None
    parent_id = reply_to
    for idx, chunk in enumerate(chunks):
        if len(chunks) > 1:
            chunk = f"[{idx + 1}/{len(chunks)}] {chunk}"
        if parent_id:
            msg_id = _reply_and_get_msg_id(client, parent_id, chunk)
        else:
            req = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(json.dumps({"text": chunk}))
                    .build()
                )
                .build()
            )
            resp = _feishu_call_with_retry(lambda: client.im.v1.message.create(req))
            msg_id = resp.data.message_id if resp.success() else None
            if not resp.success():
                print(f"[Feishu Send Error] code: {resp.code}, msg: {resp.msg}")
        if msg_id:
            log_chat("out", "bot", chunk, chat_id)
            parent_id = msg_id  # 后续片回复前一片
            last_msg_id = msg_id
    return last_msg_id


def _reply_and_get_msg_id(client, message_id, text_content):
    """回复消息并返回新消息的 msg_id（失败返回 None）。"""
    from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
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
    resp = _feishu_call_with_retry(lambda: client.im.v1.message.reply(req))
    if resp.success():
        return resp.data.message_id
    print(f"[Feishu Reply Error] code: {resp.code}, msg: {resp.msg}")
    return None


def send_feishu_file(client, chat_id, file_path):
    from lark_oapi.api.im.v1 import (CreateFileRequest, CreateFileRequestBody,
                                     CreateMessageRequest, CreateMessageRequestBody)
    with open(file_path, "rb") as handle:
        upload = client.im.v1.file.create(
            CreateFileRequest.builder().request_body(
                CreateFileRequestBody.builder()
                .file_type("stream")
                .file_name(os.path.basename(file_path))
                .file(handle)
                .build()
            ).build()
        )
    if not upload.success():
        raise RuntimeError(f"飞书文件上传失败：{upload.code} {upload.msg}")
    response = client.im.v1.message.create(
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("file")
            .content(json.dumps({"file_key": upload.data.file_key}))
            .build()
        ).build()
    )
    if not response.success():
        raise RuntimeError(f"飞书文件发送失败：{response.code} {response.msg}")
    log_chat("out", "bot", f"📎 已发送文件：{os.path.basename(file_path)}", chat_id)
    return response.data.message_id


def _progress_card(title, body_text, details_text=None):
    elements = [{"tag": "markdown", "content": body_text}]
    if details_text is not None:
        elements.append({
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
        })
    return {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": title}},
        "body": {"elements": elements},
    }


def send_progress_card(client, receive_id, title, body_text, details_text=None):
    """发送 JSON 2.0 interactive 进度卡片，返回可供 PATCH 更新的 msg_id。"""
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
    card = _progress_card(title, body_text, details_text)
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
    resp = _feishu_call_with_retry(lambda: client.im.v1.message.create(req))
    if resp.success():
        msg = resp.data.message_id
        print(f"[Feishu Card Created] msg_id={msg}")
        return msg
    print(f"[Feishu Card Error] code: {resp.code}, msg: {resp.msg}")
    return None


def update_progress_card(client, message_id, title, body_text):
    """PATCH 更新已发送的 JSON 2.0 进度卡片（同一 msg_id，不刷屏）。"""
    from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
    card = _progress_card(title, body_text)
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
    resp = _feishu_call_with_retry(lambda: client.im.v1.message.patch(req))
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
    "needs_review": "等待人工复核",
    "blocked": "环境阻塞",
}


def _task_line(task):
    title = task["payload"].get("topic") or task["payload"].get("goal") or task["task_type"]
    return f"{task['id']} · {TASK_STATUS_TEXT.get(task['status'], task['status'])} · {task['task_type']} · {title[:35]}"


def _task_agent_progress(task):
    roles = {}
    for event in task_events(task["id"], limit=100):
        if event["event_type"] != "agent_result":
            continue
        try:
            details = json.loads(event.get("details_json") or "{}")
        except json.JSONDecodeError:
            details = {}
        role = details.get("role") or event.get("engine") or "Agent"
        status = "✅ 已完成" if event.get("ok") else "❌ 失败"
        duration = event.get("duration_ms")
        roles[role] = f"{status}" + (f" · {duration}ms" if duration is not None else "")
    return [f"{role}：{status}" for role, status in roles.items()]


def _evolve_after_task(task, outcome_text):
    """Best-effort, one-rule postmortem for meaningful work tasks."""
    if task["task_type"] not in {"roundtable", "swarm"}:
        return None
    try:
        result = call_hermes(
            "你是受控进化复盘员，只做文本复盘，不操作电脑。根据下面任务结果，提炼一条以后可复用的工作规则。"
            "规则必须是流程或验证方法，不得写任务专属事实，不得改变系统约束、权限或人工审批。"
            "只能使用下面明确提供的证据，不得猜测未出现的错误类别、供应商或原因。"
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
    last_speech_msg_id = None
    workflow_id = task["payload"].get("workflow_id")

    def on_event(event_type, payload):
        nonlocal card_msg_id
        nonlocal last_speech_msg_id
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
            # 全文播报：每条发言回复上一条，形成飞书里的连贯讨论链
            nonlocal last_speech_msg_id
            speech_text = payload.get("text") or ""
            if speech_text.strip():
                header = f"🎙️ {payload['agent']}（第{payload.get('round', '?')}轮 · {payload.get('stance', '中立')}）"
                sent = send_feishu_msg(
                    client, chat_id,
                    f"{header}\n\n{speech_text}",
                    reply_to=last_speech_msg_id or msg_id,
                )
                if sent:
                    last_speech_msg_id = sent
        elif event_type == "round_end":
            score = payload.get("consensus_score")
            score_txt = f" · 共识度 {score:.2f}" if isinstance(score, (int, float)) else ""
            context.progress(f"第 {payload['round']} 轮结束{score_txt}")
            if workflow_id:
                WORKFLOW_STORE.update_ledgers(workflow_id, progress_ledger={
                    "round": payload["round"], "stances": payload.get("stances") or {},
                    "consensus": bool(payload.get("consensus")),
                    "fixed_point": bool(payload.get("fixed_point")),
                    "consensus_score": score,
                    "making_progress": not bool(payload.get("fixed_point")),
                })
        elif event_type == "agent_error":
            context.progress(f"{payload['agent']} 暂时不可用，已移出本场后续轮次")

    project = task["payload"].get("project")
    constraints = _task_constraints(task)
    memory_context = (
        envelope_prompt(constraints) + "\n\n" +
        MEMORY_STORE.prompt_context(topic, project_name=project)
    )
    result = RoundTableV2().run(topic, on_event=on_event, cancel_check=context.check_cancelled,
                                memory_context=memory_context)
    if workflow_id:
        WORKFLOW_STORE.update_ledgers(workflow_id, task_ledger={
            "root_request": constraints["root_request"],
            "latest_summary": result["final_summary"],
            "open_action": "boss_decision" if not task["payload"].get("continued_from") else "collaboration_confirmation",
        }, progress_ledger={
            "rounds_used": result["rounds_used"],
            "unavailable_agents": list((result.get("unavailable_agents") or {}).keys()),
        })
        next_state = ("awaiting_collaboration_confirmation"
                      if task["payload"].get("continued_from") else "awaiting_boss_decision")
        WORKFLOW_STORE.transition(workflow_id, next_state, task["id"])
    context.progress("会议纪要已生成")
    degraded = ""
    if result.get("unavailable_agents"):
        degraded = "\n\n⚠️ **降级成员**：" + "、".join(result["unavailable_agents"])
    project_hint = f" [项目:{project}]" if project else ""
    if task["payload"].get("continued_from"):
        next_step = (
            "\n\n**本轮已吸收老板拍板**：\n"
            "- 如仍需调整，可继续回复补充意见，系统会按任务量动态续会。\n"
            f"- 是否开始协作？确认请回复 `开始协作{project_hint}`。"
        )
    else:
        next_step = (
            "\n\n**请老板拍板**：直接回复决策或调整意见。"
            "系统会带着本次共识继续讨论并动态收敛，完成后再询问是否开始协作。"
        )
    final_body = f"**任务**：{task['id']}\n**议题**：{topic}\n✅ **会议完成**（{result['rounds_used']} 轮）{degraded}\n\n{result['final_summary'][:800]}{next_step}"
    if card_msg_id:
        update_progress_card(client, card_msg_id, "✅ 圆桌会议完成", final_body)
    else:
        reply_feishu_msg(client, msg_id, f"🏁【会议完成】\n{result['final_summary']}{next_step}")
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
    TASK_CONTROLLER.store.finish(task["id"], "succeeded", result={
        "session_id": result["session_id"],
        "rounds_used": result["rounds_used"],
        "summary": result["final_summary"],
    })
    return {"session_id": result["session_id"], "rounds": result["rounds_used"],
            "summary": result["final_summary"], "unavailable_agents": result.get("unavailable_agents", {}),
            "evolution_memory_id": evolution["id"] if evolution else None}


def _run_swarm_task(client, task, context):
    payload = task["payload"]
    if not collaboration_handoff_valid(payload):
        raise TaskBlocked("协作任务缺少会议拍板后的明确开始协作确认，拒绝进入预审或执行")
    workflow_id = payload.get("workflow_id")
    workflow = WORKFLOW_STORE.get(workflow_id)
    meeting = TASK_CONTROLLER.store.get(payload.get("meeting_task_id"))
    confirmation = payload.get("collaboration_confirmation") or {}
    if (not workflow or not meeting or meeting.get("status") != "succeeded"
            or meeting.get("task_type") != "roundtable"
            or workflow.get("owner_user_id") != confirmation.get("confirmed_by")):
        raise TaskBlocked("协作确认回执与会议工作流不一致，拒绝继续")
    if workflow["state"] in {"completed", "cancelled", "failed"}:
        if not task.get("retry_of"):
            raise TaskBlocked("协作工作流已经结束，非重试任务不得重新打开")
        retry_state = "planning" if task["phase"] == "planning" else "waiting_approval"
        workflow = WORKFLOW_STORE.resume_for_retry(
            workflow_id, retry_state, task["id"], task["retry_of"],
        )
    if task["phase"] == "planning" and workflow["state"] == "awaiting_collaboration_confirmation":
        workflow = WORKFLOW_STORE.transition(workflow_id, "planning", task["id"])
    goal = payload["goal"]
    msg_id = task["message_id"]
    project = payload.get("project")
    operation_mode = payload.get("operation_mode") or (
        "read_only_report" if is_read_only_report_request(goal) else "code_change"
    )
    report_mode = operation_mode == "read_only_report"

    def durable_checkpoint(name, details=None):
        callback = getattr(context, "checkpoint", None)
        if callback:
            callback(name, details)

    def on_agent_speak(role_tag, message_text):
        context.progress(f"{role_tag} 已完成当前阶段")
        record_task_event(task["id"], "agent_stage_completed", details={"role": role_tag})

    def on_agent_result(role_tag, engine, result):
        record_task_event(
            task["id"], "agent_result", engine=engine, ok=result.ok,
            error_code=result.error_code, duration_ms=result.duration_ms,
            details={"role": role_tag},
        )
        record_span(task["id"], role_tag, "ok" if result.ok else "error",
                    engine=engine, duration_ms=result.duration_ms,
                    failure_category=None if result.ok else classify_failure(result.text, result.error_code),
                    metadata={"error_code": result.error_code})
    if task["phase"] == "planning":
        durable_checkpoint("planning_started")
        if report_mode:
            context.progress("正在执行本地确定性报告预检；批准前不调用 Agent")
            started = time.monotonic()
            try:
                plan = plan_read_only_report(
                    goal, task["id"], payload.get("constraint_envelope"),
                    progress=context.progress, cancel_check=context.check_cancelled,
                )
            except ReportWorkflowError as exc:
                raise TaskBlocked(str(exc)) from exc
            duration_ms = int((time.monotonic() - started) * 1000)
            record_task_event(
                task["id"], "agent_result", engine="local", ok=True,
                duration_ms=duration_ms, details={"role": "🛡️ 报告只读预检"},
            )
            record_span(task["id"], "🛡️ 报告只读预检", "ok", engine="local",
                        duration_ms=duration_ms, metadata=plan["report_request"])
        else:
            context.progress("Hermes PM → 反重力架构师 → Codex 只读探索，正在生成方案")
            plan = swarm_orchestrator.plan_collaborative_project(
                goal, project_name=project,
                constraint_envelope=payload.get("constraint_envelope"),
                memory_context=MEMORY_STORE.prompt_context(goal, project_name=project),
                on_agent_message=on_agent_speak, on_agent_result=on_agent_result,
                cancel_check=context.check_cancelled,
            )
        frozen_scope = (plan.get("approved_scope") or {}).get("allowed_paths") or []
        scope_preview = (
            "**冻结写入范围**：" + "、".join(f"`{path}`" for path in frozen_scope) + "\n\n"
            if not report_mode else ""
        )
        preview = (
            f"**任务**：{task['id']}\n**项目**：{plan['project_name']}\n"
            f"**目标**：{goal}\n\n**方案摘要**：\n{plan['requirements'][:500]}\n\n"
            f"**影响与风险**：\n{plan['impact_and_risks'][:500]}\n\n"
            + scope_preview
            + ("**执行模式**：外部 PDF + 项目只读分析；仅生成并发送一份 DOCX 报告，"
               "不进入源码写入、仓库测试或合并流程。\n\n" if report_mode else
               "**系统产物**：任务快照、SQLite 审计、项目记忆与 Obsidian 纪要；不计入业务文件变更。\n\n")
            + f"⏳ 30 分钟内发送 `批准任务 {task['id']}`，或 `拒绝任务 {task['id']}`。"
        )
        send_progress_card(client, task["chat_id"],
                           "🛂 报告生成预审" if report_mode else "🛂 写入预审", preview)
        durable_checkpoint("planning_completed", {"project_name": plan["project_name"]})
        if workflow_id:
            WORKFLOW_STORE.transition(workflow_id, "waiting_approval", task["id"])
        context.wait_for_approval(plan)
    plan = task.get("plan")
    if not plan or not task.get("approved_at"):
        raise RuntimeError("缺少有效写入批准")
    if not approval_receipt_valid(task):
        raise RuntimeError("批准后的方案或约束已发生变化，拒绝执行并要求重新预审")
    if operation_mode == "read_only_report":
        context.progress("已批准，正在启动外部输入只读报告流程")
        durable_checkpoint("report_execution_started")
        if workflow_id:
            current = WORKFLOW_STORE.get(workflow_id)
            if current and current["state"] == "waiting_approval":
                WORKFLOW_STORE.transition(workflow_id, "executing", task["id"])
        checkpoint_rows = (
            context.store.checkpoints(task["id"])
            if hasattr(context, "store") and hasattr(context.store, "checkpoints") else []
        )
        checkpoint_map = {item["checkpoint"]: item.get("details") or {} for item in checkpoint_rows}
        delivered = checkpoint_map.get("report_delivered")
        if delivered:
            summary = (
                f"✅ 只读报告任务 {task['id']} 已完成。论文 {delivered.get('page_count', '已核验')} 页，"
                f"项目零修改验证通过，文件已发送：{delivered.get('report_name', '论文改进报告')}"
            )
            context.progress("检测到已发送检查点，幂等完成任务，不重复生成或发送")
            return {
                "project_name": delivered.get("project_name") or project or "只读报告",
                "success": True, "final_report": summary,
                "report_path": delivered.get("report_path"),
                "evidence": delivered.get("evidence") or {},
            }
        try:
            result = recover_existing_report(goal, task["id"]) if hasattr(context, "store") else None
            if result:
                context.progress("检测到同任务已生成报告，正在恢复文件发送")
            else:
                result = execute_read_only_report(
                    goal, task["id"], progress=context.progress,
                    cancel_check=context.check_cancelled,
                    on_agent_result=on_agent_result,
                )
        except ReportWorkflowError as exc:
            reply_feishu_msg(client, msg_id, f"⛔ 只读报告任务 {task['id']} 被阻塞：{exc}")
            raise TaskBlocked(str(exc)) from exc
        durable_checkpoint("report_generated", {
            "report_name": os.path.basename(result["report_path"]),
            "report_path": result["report_path"],
            "project_name": result["project_name"],
            "evidence": result["evidence"],
        })
        context.progress("📎 报告已生成，正在发送到飞书")
        send_feishu_file(client, task["chat_id"], result["report_path"])
        if workflow_id:
            WORKFLOW_STORE.transition(workflow_id, "verifying", task["id"])
            WORKFLOW_STORE.transition(workflow_id, "completed", task["id"])
        durable_checkpoint("report_delivered", {
            "report_name": os.path.basename(result["report_path"]),
            "report_path": result["report_path"],
            "project_name": result["project_name"],
            "page_count": result["evidence"]["page_count"],
            "evidence": result["evidence"],
        })
        context.progress("只读报告已生成并发送，项目零修改验证通过")
        summary = (
            f"✅ 只读报告任务 {task['id']} 已完成。论文 {result['evidence']['page_count']} 页，"
            f"项目零修改验证通过，已发送文件：{os.path.basename(result['report_path'])}"
        )
        reply_feishu_msg(client, msg_id, summary)
        return {
            "project_name": result["project_name"], "success": True,
            "final_report": summary, "report_path": result["report_path"],
            "evidence": result["evidence"],
        }
    plan = dict(plan, execution_task_id=task["id"], retry_of=task.get("retry_of"))
    context.progress("已批准，反重力正在编写首版代码；调度器校验落入隔离区后由 Codex 完善与验证")
    durable_checkpoint("execution_started")
    if workflow_id:
        current = WORKFLOW_STORE.get(workflow_id)
        if current and current["state"] == "waiting_approval":
            WORKFLOW_STORE.transition(workflow_id, "executing", task["id"])
    result = swarm_orchestrator.execute_collaborative_project(
        plan, on_agent_message=on_agent_speak, on_agent_result=on_agent_result,
        cancel_check=context.check_cancelled,
    )
    if workflow_id and result.get("success"):
        WORKFLOW_STORE.transition(workflow_id, "verifying", task["id"])
    durable_checkpoint("mechanical_verification_completed", {
        "success": bool(result.get("success")), "status": result.get("status"),
    })
    if result.get("status") == "blocked":
        reply_feishu_msg(client, msg_id, (
            f"⛔ 协作任务 {task['id']} 因环境问题阻塞，不判定为代码失败。\n\n"
            f"{result['final_report'][:1000]}"
        ))
        raise TaskBlocked(result["final_report"][:1200], result=result)
    if result.get("status") == "needs_review":
        reply_feishu_msg(client, msg_id, (
            f"⚠️ 协作任务 {task['id']} 机械验收已通过，Hermes 建议人工复核。\n\n"
            f"{result['final_report'][:1000]}"
        ))
        raise TaskNeedsReview(result["final_report"][:1200], result=result)
    if not result["success"]:
        raise RuntimeError(f"协作验收失败：{result['final_report'][:1200]}")
    if project:
        MEMORY_STORE.add(result["final_report"], project_name=project, source_type="swarm",
                         source_id=task["id"], source_path=f"Obsidian/{result['project_name']}")
    context.progress("协作完成，正在执行一次任务后进化复盘")
    evolution = _evolve_after_task(task, result["final_report"])
    if workflow_id:
        WORKFLOW_STORE.transition(workflow_id, "completed", task["id"])
    durable_checkpoint("completed")
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
    workflow_id = (task.get("payload") or {}).get("workflow_id")

    def finish_workflow(status):
        if not workflow_id:
            return
        try:
            WORKFLOW_STORE.finish_from_task(workflow_id, status, task["id"])
        except Exception as transition_exc:
            print(f"[Workflow Terminal Sync Warning] {type(transition_exc).__name__}: {transition_exc}")

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
    except TaskParked:
        raise
    except (TaskNeedsReview, TaskBlocked):
        finish_workflow("failed")
        raise
    except TaskCancelled:
        finish_workflow("cancelled")
        reply_feishu_msg(client, task["message_id"], f"🛑 任务 {task['id']} 已取消。")
        raise
    except Exception as exc:
        finish_workflow("failed")
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
        print(f"[消息内容]: {redact(clean_text)}")
        chat_id_in = getattr(msg, "chat_id", None) or ""
        log_chat("in", user_id, clean_text, chat_id_in)  # 收消息持久化（带 chat_id）

        # 项目标签在任何命令路由前统一校验，避免存储桥接收到路径型项目名。
        try:
            extract_project_tag(clean_text)
        except ValueError as exc:
            reply_feishu_msg(client, msg_id, f"⛔ 项目标签无效：{exc}")
            return

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
            if task["task_type"] == "swarm":
                mode = (task.get("payload") or {}).get("operation_mode")
                detail.append(f"执行模式：{'只读报告' if mode == 'read_only_report' else '代码协作'}")
                detail.extend(_task_agent_progress(task))
            if task.get("error"):
                detail.append(f"错误：{task['error'][:500]}")
            reply_feishu_msg(client, msg_id, "\n".join(detail))
            return

        match = re.fullmatch(r"取消任务\s+([0-9a-f]{4,10})", lower_text)
        if match:
            task = TASK_CONTROLLER.store.find(match.group(1), chat_id_in)
            authorized = _can_control_task(task, user_id)
            ok = bool(authorized and TASK_CONTROLLER.store.request_cancel(task["id"]))
            denied = task and not authorized
            reply_feishu_msg(client, msg_id, "⛔ 仅任务发起人可以取消该任务。" if denied else
                             f"{'🛑 已提交取消请求：' + task['id'] if ok else '任务不存在、已结束或 ID 不唯一。'}")
            return

        match = re.fullmatch(r"重试任务\s+([0-9a-f]{4,10})", lower_text)
        if match:
            old = TASK_CONTROLLER.store.find(match.group(1), chat_id_in)
            authorized = _can_control_task(old, user_id)
            invalid_handoff = bool(
                old and old.get("task_type") == "swarm"
                and not collaboration_handoff_valid(old.get("payload"))
            )
            task = TASK_CONTROLLER.retry(old["id"], message_id=msg_id) if authorized and not invalid_handoff else None
            if old and not authorized:
                reply = "⛔ 仅任务发起人可以重试该任务。"
            elif invalid_handoff:
                reply = "⛔ 该旧任务没有会议拍板和开始协作确认回执，不能重试；请重新发送「协作 <目标>」进入完整会议流程。"
            else:
                reply = f"{'🔁 已创建重试任务：' + task['id'] if task else '仅失败、取消或已完成的唯一任务可以重试。'}"
            reply_feishu_msg(client, msg_id, reply)
            return

        match = re.fullmatch(r"批准任务\s+([0-9a-f]{4,10})", lower_text)
        if match:
            task = TASK_CONTROLLER.store.find(match.group(1), chat_id_in)
            authorized = _can_control_task(task, user_id)
            outcome = TASK_CONTROLLER.approve(
                task["id"], approved_by=user_id, approval_message_id=msg_id,
            ) if authorized else ("forbidden" if task else "not_found")
            messages = {
                "approved": f"✅ 已批准任务 {task['id']}，写入阶段已进入本会话队尾。",
                "already_approved": f"ℹ️ 任务 {task['id']} 已批准，无需重复操作。" if task else "任务不存在。",
                "not_waiting": "任务当前不在等待批准状态。",
                "not_found": "未找到唯一匹配的任务。",
                "forbidden": "⛔ 仅任务发起人可以批准该任务。",
                "missing_collaboration_confirmation": (
                    "⛔ 该任务未经过完整的会议拍板和开始协作确认，批准被拒绝；"
                    "请重新发送「协作 <目标>」进入会议流程。"
                ),
            }
            reply_feishu_msg(client, msg_id, messages[outcome])
            return

        match = re.fullmatch(r"拒绝任务\s+([0-9a-f]{4,10})", lower_text)
        if match:
            task = TASK_CONTROLLER.store.find(match.group(1), chat_id_in)
            authorized = _can_control_task(task, user_id)
            ok = bool(authorized and task["status"] == "waiting_approval" and TASK_CONTROLLER.store.request_cancel(task["id"]))
            reply_feishu_msg(client, msg_id, "⛔ 仅任务发起人可以拒绝该任务。" if task and not authorized else
                             f"{'🚫 已拒绝并取消任务 ' + task['id'] if ok else '任务不存在或不在等待批准状态。'}")
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
            clear_history(f"{user_id}:hermes")
            clear_history(f"{user_id}:deepseek")
            reply_feishu_msg(
                client,
                msg_id,
                "🧹【个人上下文已清空】已重置您与各管家 Agent 的对话上下文，不影响其他用户。",
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
            guardian = guardian_status()
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
                f"🫀 服务状态: {'需人工恢复' if guardian.get('state') == 'halted' else '正常'} | 活跃任务: {active} | 排队: {counts.get('queued', 0)} | 执行中: {counts.get('running', 0)}\n"
                f"🔁 最近退出: {guardian.get('exit_reason', '无记录')}"
                f"{(' (code ' + str(guardian.get('exit_code')) + ')') if guardian.get('exit_code') is not None else ''}"
                f" | 近1小时重启: {guardian.get('restarts_last_hour', 0)}\n"
                + "\n".join(engine_lines) + "\n"
                f"-------------------------\n"
                f"📂 知识中枢: Obsidian E:\\\\Obsidian_Vault\\\\多agent\\\\ (三级规范)\n"
                f"💡 触发指令: 「协作 <目标>」从圆桌开始；拍板收敛并确认「开始协作」后才创建预审。"
            )
            reply_feishu_msg(client, msg_id, reply)
            return

        if lower_text == "深度健康":
            task = TASK_CONTROLLER.submit("health_probe", chat_id_in, user_id, msg_id, {})
            reply_feishu_msg(client, msg_id, f"✅ 已收到，深度健康任务 {task['id']} 已在后台排队。")
            return

        # P3c 周报：近 7 天任务成功率 + 引擎耗时 + 会议统计
        if lower_text in ["周报", "weekly", "weekly report"]:
            stats = TASK_CONTROLLER.store.weekly_stats(days=7)
            lines = ["📈【周报 · 近7天】", "-------------------------"]
            for ttype, status_counts in stats["by_type"].items():
                total = sum(status_counts.values())
                ok = status_counts.get("succeeded", 0)
                rate = f"{ok / total * 100:.0f}%" if total else "-"
                parts = " ".join(f"{k}:{v}" for k, v in sorted(status_counts.items()))
                lines.append(f"• {ttype}: {ok}/{total} 成功（{rate}） [{parts}]")
            if not stats["by_type"]:
                lines.append("• 本周无任务记录")
            if stats["engines"]:
                lines.append("-------------------------")
                for e in stats["engines"]:
                    avg_s = (e["avg_ms"] or 0) / 1000
                    max_s = (e["max_ms"] or 0) / 1000
                    lines.append(f"⚙️ {e['engine']}: {e['n']} 次 · 平均 {avg_s:.1f}s · 最长 {max_s:.1f}s")
            meeting_line = " · ".join(f"{k}:{v}" for k, v in sorted(stats["meetings"].items()))
            if meeting_line:
                lines.append(f"🎙️ 圆桌会议: {meeting_line}")
            reply_feishu_msg(client, msg_id, "\n".join(lines))
            return

        # 项目标签不参与命令路由，避免项目名中的“会议”误触发圆桌。
        tagged_text, project = extract_project_tag(clean_text)
        command_text = tagged_text.strip()
        lower_command = command_text.lower()

        # 明确的“开始协作”优先级最高，把最近一轮已拍板会议交接给 swarm。
        start_handoff = any(lower_command.startswith(prefix) for prefix in (
            "开始协作", "进入协作", "转入协作",
        ))
        if start_handoff:
            workflow = WORKFLOW_STORE.active(chat_id_in, project_name=project) if project else WORKFLOW_STORE.active(chat_id_in)
            if not workflow:
                reply_feishu_msg(client, msg_id, "当前没有等待协作确认的会议流程，请先发送「协作 <目标>」进入会议讨论。")
                return
            if workflow["state"] != "awaiting_collaboration_confirmation":
                reply_feishu_msg(client, msg_id, "当前会议尚未完成老板拍板轮次，请先回复决策意见完成收敛。")
                return
            if workflow.get("owner_user_id") and workflow["owner_user_id"] != user_id:
                reply_feishu_msg(client, msg_id, "⛔ 仅本议题发起人可以确认开始协作。")
                return
            meeting = _workflow_current_meeting(workflow, chat_id_in)
            if not meeting:
                reply_feishu_msg(client, msg_id, "会议终态与工作流记录不一致，暂不能创建协作预审任务。")
                return
            project = project or meeting.get("payload", {}).get("project")
            try:
                envelope = _task_constraints(meeting)
            except ConstraintEnvelopeError as exc:
                reply_feishu_msg(client, msg_id, f"⛔ 无法交接：{exc}。请用「开会 <新议题>」创建新链路。")
                return
            handoff_goal = _meeting_handoff_goal(meeting, workflow)
            operation_mode = ("read_only_report" if is_read_only_report_request(handoff_goal)
                              else "code_change")
            if operation_mode == "read_only_report" and not project:
                _, report_project = report_request_paths(handoff_goal)
                project = os.path.basename(report_project.rstrip("\\/")) if report_project else None
            task = TASK_CONTROLLER.submit(
                "swarm", chat_id_in, user_id, msg_id,
                {"goal": handoff_goal, "project": project, "operation_mode": operation_mode,
                 "meeting_task_id": meeting["id"],
                  "workflow_id": workflow["id"],
                  "collaboration_confirmation": {
                      "workflow_id": workflow["id"],
                      "meeting_task_id": meeting["id"],
                      "confirmed_by": user_id,
                      "confirmation_message_id": msg_id,
                      "confirmed_at": time.time(),
                  },
                  "root_request": envelope["root_request"],
                  "constraint_envelope": envelope},
            )
            WORKFLOW_STORE.transition(workflow["id"], "planning", task["id"])
            next_action = "生成报告" if operation_mode == "read_only_report" else "写入"
            reply_feishu_msg(client, msg_id, f"✅ 会议共识已交接。协作任务只读规划已排队：{task['id']}\n规划完成后需再次批准才会{next_action}。")
            return

        # 显式协作命令只创建会议链路；不得绕过拍板与开始协作确认直接建预审任务。
        is_swarm_trigger = (
            lower_command.startswith("协作") or lower_command.startswith("开工")
            or lower_command.startswith("项目") or lower_command.startswith("团队")
            or "帮我做" in lower_command or "设计系统" in lower_command
            or "构建系统" in lower_command or "开发" in lower_command
        )
        if is_swarm_trigger:
            task_goal = re.sub(r"^(协作|开工|项目|团队)\s*", "", command_text).strip()
            if not task_goal:
                reply_feishu_msg(client, msg_id, "请在「协作」后写明具体目标，例如：协作 为项目增加健康检查。")
                return
            existing_workflow = WORKFLOW_STORE.active(chat_id_in, project_name=project) if project else WORKFLOW_STORE.active(chat_id_in)
            if existing_workflow and existing_workflow["state"] in {
                "planning", "waiting_approval", "executing", "verifying",
            }:
                reply_feishu_msg(
                    client, msg_id,
                    f"当前已有协作链路 {existing_workflow['id']} 处于 {existing_workflow['state']}，"
                    "请先完成或取消后再发起新协作。",
                )
                return
            if existing_workflow:
                WORKFLOW_STORE.transition(existing_workflow["id"], "cancelled")
            envelope = build_constraint_envelope(task_goal)
            workflow = WORKFLOW_STORE.create(chat_id_in, user_id, project, task_goal, envelope)
            meeting_topic = (
                "围绕以下协作目标进行动态多轮讨论；明确需求边界、影响文件、验收标准、风险与回退。"
                "本阶段只讨论，不创建预审、不实施；收敛后请老板拍板。\n"
                f"协作目标：{task_goal}"
            )
            task = TASK_CONTROLLER.submit(
                "roundtable", chat_id_in, user_id, msg_id,
                {"topic": meeting_topic, "project": project, "root_request": task_goal,
                 "workflow_id": workflow["id"], "constraint_envelope": envelope,
                 "initiated_by": "collaboration_request"},
            )
            WORKFLOW_STORE.bind_task(workflow["id"], task["id"])
            reply_feishu_msg(
                client, msg_id,
                f"✅ 已收到。先进入会议讨论，圆桌任务：{task['id']}\n"
                "会议将动态多轮收敛并请你拍板；你确认「开始协作」后才会创建协作预审任务。",
            )
            return

        # 圆桌只根据去掉项目标签后的显式命令词触发。
        is_roundtable_trigger = bool(re.match(
            r"^(开始\s*)?(开会|圆桌|讨论|头脑风暴|会议)(?:\s|[:：]|$)", command_text,
        ))
        if is_roundtable_trigger:
            topic = re.sub(
                r"^(开始|现在|大家|请|帮我|我们)?\s*(开会|圆桌|讨论|头脑风暴|会议)\s*[:：]?\s*", "", command_text,
            ).strip()
            topic = re.sub(r"(成员|大家|各位|先)?\s*(做?自我介绍|打招呼|报到|集合)\s*$", "", topic).strip()
            if len(topic) < 4:
                topic = f"关于「{topic}」的方案设计与讨论" if topic else "（未指定议题，请各位自由讨论当前重要事项）"
            envelope = build_constraint_envelope(topic)
            existing_workflow = WORKFLOW_STORE.active(chat_id_in, project_name=project) if project else WORKFLOW_STORE.active(chat_id_in)
            if existing_workflow and existing_workflow["state"] in {
                "meeting_discussion", "awaiting_boss_decision", "meeting_continuation",
                "awaiting_collaboration_confirmation",
            }:
                WORKFLOW_STORE.transition(existing_workflow["id"], "cancelled")
            workflow = WORKFLOW_STORE.create(chat_id_in, user_id, project, topic, envelope)
            task = TASK_CONTROLLER.submit(
                "roundtable", chat_id_in, user_id, msg_id,
                {"topic": topic, "project": project, "root_request": topic,
                 "workflow_id": workflow["id"], "constraint_envelope": envelope},
            )
            WORKFLOW_STORE.bind_task(workflow["id"], task["id"])
            reply_feishu_msg(client, msg_id, f"✅ 已收到。圆桌任务已排队：{task['id']}\n发送「任务 {task['id']}」查看进度，或「取消任务 {task['id']}」。")
            return

        # 只有工作流明确等待老板决策/补充时，普通回复才会被认作拍板并触发按需续会。
        workflow = WORKFLOW_STORE.active(chat_id_in, project_name=project) if project else WORKFLOW_STORE.active(chat_id_in)
        if workflow and workflow["state"] in {
            "awaiting_boss_decision", "awaiting_collaboration_confirmation",
        }:
            meeting = _workflow_current_meeting(workflow, chat_id_in)
            if not meeting:
                reply_feishu_msg(client, msg_id, "会议终态与工作流记录不一致，无法记录拍板；请查询当前任务状态。")
                return
            previous = (meeting.get("result") or {}).get("summary") or ""
            continuation_topic = (
                f"延续圆桌任务 {meeting['id']}。\n上轮共识：{previous}\n"
                f"老板拍板/补充：{command_text}\n"
                "请根据新决策动态收敛剩余分歧，不重复已确定事项。"
            )
            project = project or meeting.get("payload", {}).get("project")
            try:
                envelope = _task_constraints(meeting)
            except ConstraintEnvelopeError as exc:
                reply_feishu_msg(client, msg_id, f"⛔ 无法续会：{exc}。请用「开会 <新议题>」创建新链路。")
                return
            if workflow.get("owner_user_id") and workflow["owner_user_id"] != user_id:
                reply_feishu_msg(client, msg_id, "⛔ 仅本议题发起人可以提交拍板意见。")
                return
            workflow = WORKFLOW_STORE.record_boss_decision(
                workflow["id"], command_text, user_id=user_id, message_id=msg_id,
                meeting_task_id=meeting["id"],
            )
            if workflow["state"] == "awaiting_boss_decision" and _decision_ends_discussion(command_text):
                WORKFLOW_STORE.transition(workflow["id"], "awaiting_collaboration_confirmation", meeting["id"])
                project_hint = f" [项目:{project}]" if project else ""
                reply_feishu_msg(
                    client, msg_id,
                    "✅ 已记录拍板；你明确要求不再续会。是否开始协作？\n"
                    f"确认请回复 `开始协作{project_hint}`，确认后才会创建协作预审任务。",
                )
                return
            WORKFLOW_STORE.transition(workflow["id"], "meeting_continuation")
            task = TASK_CONTROLLER.submit(
                "roundtable", chat_id_in, user_id, msg_id,
                {"topic": continuation_topic, "project": project, "continued_from": meeting["id"],
                  "workflow_id": workflow["id"], "boss_decision": command_text,
                  "root_request": envelope["root_request"], "constraint_envelope": envelope},
            )
            WORKFLOW_STORE.bind_task(workflow["id"], task["id"])
            reply_feishu_msg(client, msg_id, f"✅ 已记录拍板意见，动态续会任务已排队：{task['id']}")
            return

        # 3. 普通对话也持久化并后台执行，先确认接收。
        prompt, project = extract_project_tag(clean_text)
        task = TASK_CONTROLLER.submit("chat", chat_id_in, user_id, msg_id,
                                      {"prompt": prompt, "project": project})
        reply_feishu_msg(client, msg_id, f"✅ 已收到（任务 {task['id']}），Hermes 将在后台回复。")


def main():
    global _FEISHU_INGRESS_LEASE
    cfg = load_config()
    warnings = validate_startup(cfg)
    app_id = cfg["feishu"]["app_id"]
    app_secret = cfg["feishu"]["app_secret"]
    ingress_mode = os.environ.get("FEISHU_INGRESS_MODE", "hermes_delegate").strip().lower()
    if ingress_mode == "direct":
        _FEISHU_INGRESS_LEASE = acquire_ingress_lease(runtime_value("workspace_dir"), app_id)
    execution_roots = [runtime_value("execution_dir")]
    legacy_execution_root = os.path.join(runtime_value("workspace_dir"), "executions")
    if legacy_execution_root not in execution_roots:
        execution_roots.append(legacy_execution_root)
    recovered_merges = []
    for execution_root in execution_roots:
        recovered_merges.extend(recover_abandoned_merges(os.path.dirname(__file__), execution_root))
    WORKFLOW_STORE.ensure_schema()
    ensure_observability_schema()
    ensure_lease_schema()

    print("==================================================")
    for warning in warnings:
        print(f"[Degraded Startup] {warning}")
    if recovered_merges:
        print(f"[Merge Recovery] rolled back abandoned tasks: {', '.join(recovered_merges)}")
    print("🚀 正在启动飞书 多智能体协同工作室网关 (Swarm + Obsidian)...")
    print("==================================================")
    load_histories()  # 恢复对话历史（重启不丢）

    client = (
        lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    )
    TASK_CONTROLLER.start(lambda task, context: execute_background_task(client, task, context))

    if ingress_mode == "hermes_delegate":
        token_path = os.path.join(runtime_value("workspace_dir"), "ingress-bridge.token")
        bridge = IngressBridge(
            lambda data: MESSAGE_POOL.submit(dispatch_message, client, data),
            token_path,
            port=int(os.environ.get("FEISHU_ORCHESTRATOR_BRIDGE_PORT", "8765")),
        ).start()
        print("✅ Hermes 为唯一飞书入口；本地调度委托桥已建立！")
        try:
            while True:
                time.sleep(3600)
        finally:
            bridge.close()
        return

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
        log_level=lark.LogLevel.INFO,
    )

    print("✅ 飞书 WebSocket 长连接已建立！多智能体协作与 Obsidian 共用大脑已挂载！")
    ws_client.start()


if __name__ == "__main__":
    main()
