# -*- coding: utf-8 -*-
"""
roundtable_engine.py — 圆桌讨论引擎 V2（成熟架构版）

设计（2026-08-15 调研落地）：
- Orchestrator-Worker 拓扑：bot.py 调度，3 个 CLI 真身是 worker
- 黑板落盘：roundtable/<session_id>/ 目录（transcript.jsonl 消息总线 / state.json 检查点 / memories/ 个人记忆 / artifacts/ 产物）
- 收敛循环：立场提取 → 分歧度 → 立场固定点检测 → 轮数兜底（替换固定 3 轮）
- SQLite 数据层：任务队列 + 幂等缓存 + 断点续跑（WAL 模式）
- 第 1 轮并行化：线程池同时起 3 个 CLI（3-5 分钟 → ~1.5 分钟）
- 独立记忆：memories/<agent>.md 跨会议累积
"""

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from agent_runtime import call_codex, isolated_prompt_file
from task_manager import TaskCancelled

# ---------------------------------------------------------------- 路径与 DB
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RT_ROOT = os.path.join(BASE_DIR, "roundtable")
DB_PATH = os.path.join(RT_ROOT, "roundtable.db")

# 黑板上板：每个会议一个目录
def session_dir(session_id):
    return os.path.join(RT_ROOT, session_id)

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path

def _conn():
    ensure_dir(RT_ROOT)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def init_db():
    """建表：sessions(会议) / turns(发言任务) / cache(幂等缓存) / events(事件审计)"""
    conn = _conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            status TEXT DEFAULT 'running',   -- running/done/cancelled
            round INTEGER DEFAULT 0,
            state_json TEXT DEFAULT '{}',
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            request_id TEXT UNIQUE NOT NULL,  -- 幂等键
            round INTEGER NOT NULL,
            agent TEXT NOT NULL,
            status TEXT DEFAULT 'pending',    -- pending/running/done/failed/timeout
            output TEXT,
            error TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS cache (
            request_id TEXT PRIMARY KEY,      -- 幂等缓存：重试/恢复直接复用
            output TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            event_type TEXT,
            payload TEXT,
            created_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()

# ---------------------------------------------------------------- 会话管理
def create_session(topic):
    init_db()
    session_id = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    conn = _conn()
    conn.execute(
        "INSERT INTO sessions (id, topic, status, round, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (session_id, topic, "running", 0, now, now),
    )
    conn.commit()
    conn.close()
    # 建黑板目录
    sdir = ensure_dir(session_dir(session_id))
    ensure_dir(os.path.join(sdir, "memories"))
    ensure_dir(os.path.join(sdir, "artifacts"))
    return session_id

def save_session_state(session_id, state: dict):
    conn = _conn()
    conn.execute(
        "UPDATE sessions SET round=?, state_json=?, updated_at=? WHERE id=?",
        (state.get("round", 0), json.dumps(state, ensure_ascii=False), time.strftime("%Y-%m-%d %H:%M:%S"), session_id),
    )
    conn.commit()
    conn.close()
    # 同步写 state.json 黑板文件
    _write_board(session_id, "state.json", state)

def load_session_state(session_id):
    conn = _conn()
    row = conn.execute("SELECT state_json FROM sessions WHERE id=?", (session_id,)).fetchone()
    conn.close()
    if row and row["state_json"]:
        return json.loads(row["state_json"])
    return {}

def _write_board(session_id, filename, data):
    """写黑板文件（transcript.jsonl 追加 / state.json 覆盖）"""
    sdir = ensure_dir(session_dir(session_id))
    path = os.path.join(sdir, filename)
    if filename.endswith(".jsonl"):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def append_event(session_id, event_type, payload):
    conn = _conn()
    conn.execute(
        "INSERT INTO events (session_id, event_type, payload, created_at) VALUES (?,?,?,?)",
        (session_id, event_type, json.dumps(payload, ensure_ascii=False), time.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()

# ---------------------------------------------------------------- 幂等缓存
def cache_get(request_id):
    conn = _conn()
    row = conn.execute("SELECT output FROM cache WHERE request_id=?", (request_id,)).fetchone()
    conn.close()
    return row["output"] if row else None

def cache_set(request_id, output):
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO cache (request_id, output, created_at) VALUES (?,?,?)",
        (request_id, output, time.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()

# ---------------------------------------------------------------- turns 队列
def create_turn(session_id, request_id, round_no, agent):
    conn = _conn()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT OR IGNORE INTO turns (session_id, request_id, round, agent, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (session_id, request_id, round_no, agent, "pending", now, now),
    )
    conn.commit()
    conn.close()

def mark_turn(request_id, status, output=None, error=None):
    conn = _conn()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE turns SET status=?, output=?, error=?, updated_at=? WHERE request_id=?",
        (status, output, error, now, request_id),
    )
    conn.commit()
    conn.close()

def get_turn(request_id):
    conn = _conn()
    row = conn.execute("SELECT * FROM turns WHERE request_id=?", (request_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def reclaim_stale(running_timeout=120, session_timeout=600):
    """断点续跑与异常恢复：
    1. 长时间无心跳的 running 会议置为 cancelled
    2. 已结束/已取消会议中遗留的 running/pending turns 置为 failed
    3. 活跃会议中 running 但心跳超时的 turns 回收为 pending（幂等缓存保证不重复执行）
    """
    conn = _conn()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    # 1. 超过 session_timeout (默认 10 分钟) 未更新的 running 会议置为 cancelled
    conn.execute(
        "UPDATE sessions SET status='cancelled', updated_at=? "
        "WHERE status='running' AND updated_at < datetime('now', 'localtime', ?)",
        (now, f"-{session_timeout} seconds"),
    )
    # 2. 已取消或已结束会议中遗留的 running/pending turns 清理为 failed
    conn.execute(
        "UPDATE turns SET status='failed', error='session cancelled or completed', updated_at=? "
        "WHERE status IN ('running', 'pending') AND session_id IN (SELECT id FROM sessions WHERE status != 'running')",
        (now,),
    )
    # 3. 仍处于 running 状态的会议中，心跳超时的 running turns 回收为 pending
    conn.execute(
        "UPDATE turns SET status='pending', error='reclaimed: stale heartbeat', updated_at=? "
        "WHERE status='running' AND updated_at < datetime('now', 'localtime', ?)",
        (now, f"-{running_timeout} seconds"),
    )
    conn.commit()
    conn.close()

def pending_turns(session_id, round_no):
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM turns WHERE session_id=? AND round=? AND status='pending'",
        (session_id, round_no),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ---------------------------------------------------------------- 团队配置
AGENTS = {
    "pm": {
        "name": "👔 产品经理·Hermes",
        "engine": "hermes",  # 真实 Hermes（主脑真身）
        "role": "你是产品经理兼会议主持人（Hermes 主脑真身）。你负责：把控讨论方向、评估意见、推动共识、最后向老板汇报。你关注需求价值、用户视角与落地优先级。",
        "style": "言简意赅，善于总结和引导。",
    },
    "arch": {
        "name": "📐 反重力·架构师",
        "engine": "antigravity",  # 真实 agy CLI（桌面端 Google AI Pro 额度）
        "role": "你是一位首席系统架构师（代号反重力）。你负责：系统架构、数据流、模块划分、接口契约与技术选型。你关注高内聚低耦合、可扩展性与工程可行性。",
        "style": "结构化表达，给出模块/接口层面的专业意见。",
    },
    "dev": {
        "name": "💻 Codex·核心工程师",
        "engine": "codex",
        "role": "你是 Codex 核心工程师。你负责代码实现方案、可运行性、性能、安全与测试验证。",
        "style": "给出可落地的技术方案与实现要点。",
    },
}
# 圆桌参会阵容：Hermes + 反重力 + Codex
ORDER = ["pm", "arch", "dev"]
MAX_ROUNDS = 4  # 收敛兜底（简单议题可在 2 轮收敛）

# 立场关键词（发言强制首行「立场：同意/补充/反对/弃权」）
STANCE_PAT = re.compile(r"(?:【?立场】?|\*\*立场\*\*)\s*[：:]\s*(同意|赞同|补充|反对|弃权|中立)")
STANCE_KW = ["同意", "补充", "反对", "弃权"]


def extract_stance(text):
    """提取发言立场；未标注时用关键词启发式（增强版）"""
    m = STANCE_PAT.search(text)
    if m:
        st = m.group(1)
        return "同意" if st == "赞同" else st
    head = text[:120]
    # 先查反对系（优先，避免"不反对"误判）
    # ⚠️ 2026-08-16 修复："不反对"/"无异议" 含"反对"子串会被误判为反对 → 先排除
    if any(kw in head for kw in ["不反对", "无异议", "不持异议"]):
        return "同意"
    if any(kw in head for kw in ["反对", "不认同", "不同意", "拒绝", "否决", "持保留"]):
        return "反对"
    if any(kw in head for kw in ["同意", "赞同", "赞成", "支持", "认可", "力挺", "完全赞同", "赞成并", "同意并"]):
        return "同意"
    if any(kw in head for kw in ["补充", "建议", "加一条", "补充一点", "另外", "扩展"]):
        return "补充"
    if any(kw in head for kw in ["弃权", "不表态", "无意见"]):
        return "弃权"
    return "中立"


# ---------------------------------------------------------------- 议题预研
# 实体/项目词典：命中 → 去聊天记录/历史会议/工作区查证（解决"开会没人去找项目"）
TOPIC_LEXICON = [
    "Codex", "codex", "反重力", "贾维斯", "范中立", "hermes", "Hermes",
    "Syn3D", "JMS", "JIM", "论文", "投稿", "审稿", "导师", "比赛", "竞赛", "C3",
    "飞书", "网关", "看板", "文件整理", "脚本", "自动化", "团队", "成员", "分工",
    "WSL", "Obsidian", "共享大脑", "NAS", "监控", "告警", "项目", "实验", "答辩", "开题",
]
TOPIC_STOPWORDS = {
    "的", "了", "在", "我", "你", "他", "她", "它", "是", "把", "被", "让", "请", "帮",
    "我们", "你们", "之前", "关于", "什么", "怎么", "为什么", "一个", "一下",
    "这个", "那个", "开会", "圆桌", "讨论", "头脑风暴", "事情", "东西",
    "电脑端", "电脑", "设计", "环境", "进度", "系统",
    "做的事情", "历史操作", "那边",
}


def extract_keywords(topic):
    """议题关键词：词典命中优先 + 英文/数字 token + 3-8 字中文 chunk（去停用词与噪音词）"""
    kws = []
    low = (topic or "").lower()
    for kw in TOPIC_LEXICON:
        if kw.lower() in low:
            kws.append(kw)
    for m in re.finditer(r"[A-Za-z0-9]{2,}", topic or ""):
        t = m.group(0)
        if t.lower() not in [k.lower() for k in kws]:
            kws.append(t)
    clean_topic = topic or ""
    for kw in kws:
        clean_topic = re.sub(re.escape(kw), " ", clean_topic, flags=re.IGNORECASE)
    for sw in sorted(TOPIC_STOPWORDS, key=len, reverse=True):
        clean_topic = re.sub(re.escape(sw), " ", clean_topic)
    for m in re.finditer(r"[\u4e00-\u9fff]{2,}", clean_topic):
        t = m.group(0).strip()
        if t and t not in kws and t not in TOPIC_STOPWORDS and 3 <= len(t) <= 8:
            kws.append(t)
    seen, out = set(), []
    for k in kws:
        key = k.lower()
        if key not in seen:
            seen.add(key)
            out.append(k)
    return out[:5]


def research_topic(topic, max_lines=12):
    """议题预研：开会前在聊天记录/历史会议/工作区查证议题事实，生成【议题背景】注入第 1 轮"""
    kws = extract_keywords(topic)
    if not kws:
        return ""
    lines = ["🔎 议题命中关键词：" + "、".join(kws)]
    # 0) Obsidian 多agent 工作台（用户点名项目/Codex/反重力时最权威，置顶）
    try:
        hub = "/mnt/e/Obsidian_Vault/多agent"
        if os.path.isdir(hub):
            projs = sorted(
                n for n in os.listdir(hub)
                if os.path.isdir(os.path.join(hub, n)) and not n.startswith(".")
            )
            if projs:
                lines.append("📂 Obsidian 工作台项目：")
                for p in projs[:5]:
                    d = os.path.join(hub, p)
                    try:
                        fs = os.listdir(d)
                        mt = max(os.path.getmtime(os.path.join(d, f)) for f in fs)
                        lines.append(f"- {p}（最近更新 {time.strftime('%m-%d %H:%M', time.localtime(mt))}）")
                    except Exception:
                        lines.append(f"- {p}")
    except Exception:
        pass
    # 1) 聊天记录（最近 2000 行，最多 5 条且清洗换行——控体积防 PM 超时）
    log_path = os.path.join(BASE_DIR, "chat_log.jsonl")
    if os.path.exists(log_path):
        hits = []
        try:
            with open(log_path, encoding="utf-8") as f:
                tail = f.readlines()[-2000:]
            for line in tail:
                try:
                    d = json.loads(line)
                    txt = str(d.get("text") or d.get("content") or d.get("msg") or "").replace("\n", " ").replace("\r", " ").strip()
                    if any(k.lower() in txt.lower() for k in kws):
                        ts = d.get("ts") or d.get("time") or d.get("created_at") or "?"
                        dr = d.get("dir") or d.get("direction") or "?"
                        hits.append(f"[{ts} {dr}] {txt[:100]}")
                except Exception:
                    pass
        except Exception:
            pass
        if hits:
            lines.append("📜 聊天记录相关片段：")
            lines += hits[-5:]
    # 2) 历史圆桌会议
    try:
        init_db()
        conn = _conn()
        rows = conn.execute(
            "SELECT created_at, topic, status FROM sessions ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
        conn.close()
        rel = [r for r in rows if any(k.lower() in (r["topic"] or "").lower() for k in kws)]
        if rel:
            cnt = len(rel)
            done = sum(1 for r in rel if r["status"] == "done")
            lines.append(f"🗂 历史会议（同议题 {cnt} 场，收敛 {done} 场）：")
            lines += [f"- {r['created_at']} [{r['status']}] {r['topic'][:60]}" for r in rel[:3]]
    except Exception:
        pass
    # 3) workspace/ 最近文件
    try:
        ws = os.path.join(BASE_DIR, "workspace")
        if os.path.isdir(ws):
            files = sorted(
                os.listdir(ws),
                key=lambda n: os.path.getmtime(os.path.join(ws, n)),
                reverse=True,
            )[:8]
            if files:
                lines.append("📁 workspace/ 最近文件：" + "、".join(files))
    except Exception:
        pass
    if len(lines) <= 1:
        return ""
    return (
        "【议题背景·历史线索】（仅供进一步查证，不代表本场现状；"
        "不得把历史错误或运行状态当成本场事实）：\n" + "\n".join(lines)
    )


def divergence(stances_now, stances_prev):
    """分歧度：立场集合翻转的 Agent 数（0 = 无变化）"""
    if not stances_prev:
        return len(stances_now)  # 第一轮全算分歧
    return sum(1 for k in stances_now if stances_prev.get(k) != stances_now[k])


def meeting_converged(round_no, stances_now, prev_stances):
    """收敛判定（纯函数，可单测）：
    - 共识：≥2 轮 且 无反对/中立 且 至少一人明确「同意」（补充+补充≠共识）
    - 固定点：≥2 轮 且 立场与上一轮完全一致（无新分歧）
    返回 (converged: bool, how: "consensus"|"fixed_point"|"running")
    prev_stances: 上一轮立场 dict（第一轮传 None）
    """
    if round_no < 2 or not stances_now:
        return False, "running"
    no_dissent = (
        "反对" not in stances_now.values()
        and "中立" not in stances_now.values()
    )
    if no_dissent and any(s == "同意" for s in stances_now.values()):
        return True, "consensus"
    if prev_stances and divergence(stances_now, prev_stances) == 0:
        return True, "fixed_point"
    return False, "running"


# ---------------------------------------------------------------- 真身通道
def call_real_antigravity(task_text, timeout=200, max_retries=1, model="low"):
    """真实反重力：桌面端 agy CLI（Google AI Pro 免费额度）

    model: "low"=gemini-3.1-pro-low（快速档，日常） / "high"=gemini-3.1-pro-high（质量档，重要议题）
    """
    task_file = isolated_prompt_file("agy_roundtable_", task_text)
    # 转 Windows 路径
    if task_file.startswith("/mnt/"):
        drive = task_file[5].upper()
        task_file_win = f"{drive}:{task_file[6:].replace('/', chr(92))}"
    else:
        task_file_win = task_file
    import subprocess
    # 按 model 档位选 ps1：low=pro_low（快）/ high=pro（质量）
    script_name = "run_task_pro.ps1" if model == "high" else "run_task_pro_low.ps1"
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
           rf"C:\Users\26420\AppData\Local\agy\{script_name}", task_file_win]
    work_cwd = "C:\\" if os.path.exists("C:\\") else "/mnt/c"
    last_err = ""
    for attempt in range(max_retries + 1):
        try:
            proc = subprocess.run(
                cmd, shell=False, capture_output=True, encoding="utf-8",
                errors="replace", timeout=timeout, cwd=work_cwd,
            )
            stdout = (proc.stdout or "").strip()
            stderr = (proc.stderr or "").strip()
            if stdout:
                return stdout
            elif stderr:
                last_err = f"反重力报错: {stderr[:200]}"
        except subprocess.TimeoutExpired:
            last_err = "反重力思考超时"
        except Exception as e:
            last_err = f"反重力调用异常: {str(e)}"
        # 瞬时故障（认证 EOF/网络）重试一次，慢速退避
        if attempt < max_retries and any(
            kw in last_err for kw in ["Eligibility", "EOF", "network", "timeout", "proxy"]
        ):
            time.sleep(8)
            continue
        break
    return last_err if last_err else "反重力执行完成，无输出。"


def call_real_hermes(task_text, timeout=300, max_retries=1):
    """真实 Hermes：WSL 内 hermes -z（任务书文件方式，彻底避免引号转义炸弹）"""
    try:
        import subprocess
        # 写任务书（Windows Python 用 E:\\...，WSL Python 用 /mnt/e/...）
        task_file = isolated_prompt_file("hermes_roundtable_", task_text)
        # bash 里 cat 需要 WSL 路径（/mnt/e/feishu-agent/hermes_task.txt）
        if task_file.startswith("E:"):
            bash_path = task_file.replace("E:", "/mnt/e").replace("\\", "/")
        elif task_file.startswith("/mnt/"):
            bash_path = task_file
        else:
            bash_path = task_file
        # WSL 侧读取任务书文件再传给 hermes -z
        # ⚠️ cmd.exe 不识别单引号：必须用双引号包整体 + \" 转义内部引号（实测通过）
        # ⚠️ -t terminal 限工具集：系统提示词从 ~17万 tokens 缩到几万 → 单次调用 158s → 22s（2026-08-16 实测）
        # ⚠️ 参数顺序：-z 必须紧跟提示词，-t 放最后（argparse 对 -z 后接 -t 会报 expected one argument）
        cmd = ["wsl.exe", "-e", "bash", "-lc", 'hermes -z "$(cat "$1")" -t terminal', "bash", bash_path]
        work_cwd = "E:\\feishu-agent" if os.path.exists("E:\\feishu-agent") else BASE_DIR
        last_err = ""
        for attempt in range(max_retries + 1):
            try:
                proc = subprocess.run(
                    cmd, shell=False, capture_output=True, encoding="utf-8",
                    errors="replace", timeout=timeout, cwd=work_cwd,
                )
                stdout = (proc.stdout or "").strip()
                stderr = (proc.stderr or "").strip()
                if stdout:
                    return stdout
                elif stderr:
                    last_err = f"Hermes 报错: {stderr[:300]}"
            except subprocess.TimeoutExpired:
                last_err = "Hermes 思考超时（请稍后重试或缩小议题）"
            except Exception as e:
                last_err = f"Hermes 调用异常: {str(e)}"
            # 瞬时故障（思考超时/网络）重试一次
            if attempt < max_retries and any(
                kw in last_err for kw in ["思考超时", "TimeoutExpired", "network", "proxy", "EOF"]
            ):
                time.sleep(8)
                continue
            break
        return last_err if last_err else "Hermes 执行完成，无输出。"
    except subprocess.TimeoutExpired:
        return "Hermes 思考超时（请稍后重试或缩小议题）"
    except Exception as e:
        return f"Hermes 调用异常: {str(e)}"


ENGINE_CALLS = {
    "hermes": call_real_hermes,
    "antigravity": call_real_antigravity,
    "codex": call_codex,
}

ENGINE_ERROR_PREFIXES = [
    "反重力报错:", "Hermes 报错:", "Codex 调用失败:", "引擎暂时不可用:",
    "反重力思考超时", "Hermes 思考超时", "Codex 思考超时", "思考超时（请稍后重试",
    "反重力调用异常:", "Hermes 调用异常:", "Codex 调用异常:", "发言异常:",
    "反重力执行完成，无输出", "Hermes 执行完成，无输出",
]
_ENGINE_COOLDOWNS = {}
_ENGINE_COOLDOWN_LOCK = threading.Lock()


class RoundTableQuorumError(RuntimeError):
    """Raised when fewer than two healthy agents remain."""


def is_engine_error(reply):
    return any(prefix in (reply or "") for prefix in ENGINE_ERROR_PREFIXES)


def _cooldown_seconds(reply):
    match = re.search(r"Resets in\s*(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", reply or "", re.I)
    if match:
        hours, minutes, seconds = (int(value or 0) for value in match.groups())
        return max(30, hours * 3600 + minutes * 60 + seconds + 10)
    if "401 Unauthorized" in (reply or ""):
        return 300
    return 60


def _engine_cooldown_remaining(engine):
    with _ENGINE_COOLDOWN_LOCK:
        return max(0, int(_ENGINE_COOLDOWNS.get(engine, 0) - time.time()))


def _mark_engine_unavailable(engine, reply):
    with _ENGINE_COOLDOWN_LOCK:
        _ENGINE_COOLDOWNS[engine] = time.time() + _cooldown_seconds(reply)

# 重要议题关键词：命中任一 → 反重力自动切 pro-high（质量优先）
IMPORTANT_TOPIC_KW = [
    "论文", "投稿", "审稿", "期刊", "JMS", "JIM", "导师", "开题", "答辩",
    "架构", "系统设计", "方案选型", "技术选型", "招标", "报价", "预算",
    "比赛", "竞赛", "C3", "验收", "评审", "上线", "部署", "安全", "合规",
]


def topic_is_important(topic):
    """议题重要性判定：命中重要关键词 → True（反重力用 pro-high）"""
    return any(kw in (topic or "") for kw in IMPORTANT_TOPIC_KW)


# ---------------------------------------------------------------- 圆桌引擎 V2
class RoundTableV2:
    """编排器 + 黑板 + 收敛循环"""

    def __init__(self):
        init_db()
        reclaim_stale(running_timeout=600, session_timeout=3600)
        # 会议报告卡统计器（每场会议 run() 时重置）
        self._t0 = 0.0
        self._calls = 0      # 真实 LLM/引擎发言调用次数
        self._cache_hits = 0  # 幂等缓存命中次数
        self._errors = 0      # 发言失败/异常次数

    # ---- 发言执行（含幂等缓存）
    def _execute_speech(self, agent_key, session_id, round_no, prompt, antigravity_model="low"):
        agent = AGENTS[agent_key]
        request_id = f"{session_id}_r{round_no}_{agent_key}"
        # 幂等：缓存命中直接复用（断点续跑/重试不重复调用）
        cached = cache_get(request_id)
        if cached:
            self._cache_hits += 1
            return cached
        create_turn(session_id, request_id, round_no, agent_key)
        remaining = _engine_cooldown_remaining(agent["engine"])
        if remaining:
            reply = f"引擎暂时不可用: {agent['name']} 熔断中，预计 {remaining} 秒后重试"
            self._errors += 1
            mark_turn(request_id, "failed", error=reply)
            return reply
        mark_turn(request_id, "running")
        self._calls += 1  # 真实发言调用计数
        try:
            if agent["engine"] == "antigravity":
                reply = ENGINE_CALLS[agent["engine"]](prompt, model=antigravity_model)
            else:
                reply = ENGINE_CALLS[agent["engine"]](prompt)
            # ⚠️ 只缓存成功结果：错误/超时报错不缓存（否则重试永远拿到旧错误）
            # 精确匹配引擎级报错前缀，避免发言正文中提及“报错排查/错误分析”导致误伤
            # ⚠️ 2026-08-16 修复：删除泛化词 "Eligibility"/"unexpected EOF" ——
            # 发言正文提到这些词（如描述崩溃事故）会被误判 failed（R1 反重力两次踩坑）。
            # 各通道错误返回必带专属前缀（"反重力报错:"/"反重力思考超时"等），前缀匹配足够。
            if is_engine_error(reply):
                self._errors += 1
                _mark_engine_unavailable(agent["engine"], reply)
                mark_turn(request_id, "failed", error=reply)
                return reply
            mark_turn(request_id, "done", output=reply)
            cache_set(request_id, reply)
            return reply
        except Exception as e:
            self._errors += 1
            mark_turn(request_id, "failed", error=str(e))
            return f"【{agent['name']}】发言异常: {str(e)}"

    # ---- 单 Agent 发言（第 2 轮起串行：看到黑板）
    def _speak(self, agent_key, topic, session_id, round_no, board_summary, my_memory, stage, on_event=None, topic_context=""):
        agent = AGENTS[agent_key]
        memory_text = my_memory if my_memory else "（暂无历史发言）"
        ctx_block = f"{topic_context}\n\n" if topic_context else ""
        prompt = (
            f"【会议主题】{topic}\n\n"
            f"{ctx_block}"
            f"【本次讨论目标】围绕上述主题，从你的专业身份视角给出结构化专业意见。"
            f"⚠️ 这是真实工作议题，发言必须落在事实上：历史线索只能用于定位，不得直接当成本场现状；"
            f"只有本场黑板或实时查证确认的信息才能称为当前事实。禁止空谈通用流程、泛泛架构或反问澄清。\n\n"
            f"【黑板上板·其他成员发言摘要】\n{board_summary or '（第一轮独立发言，你无需参考他人）'}\n\n"
            f"【你的本场个人记忆·此前发言】\n{memory_text}\n\n"
            f"【你的身份】{agent['role']}\n"
            f"【当前轮次】第 {round_no} 轮\n{stage}\n"
            f"【发言要求】{agent['style']} 输出格式：\n"
            f"第一行写「立场：同意/补充/反对/弃权」\n"
            f"然后给出你的专业意见（≤180字）。只输出发言内容本身，不要思考过程。"
        )
        # Codex 在圆桌中仅做只读分析，不直接修改项目。
        if agent["engine"] == "codex":
            prompt += (
                "\n\n⚠️ 特别注意：你收到的【会议主题】已经是明确的讨论议题，请直接给出你的专业意见，"
                "绝对不要反问、不要要求澄清、不要列问题选项、不要要求补充信息。"
                "你的角色是参与圆桌讨论的工程师，不是需求分析师。"
            )
        # 议题重要性 → 反重力模型档位（重要议题 pro-high，日常 pro-low）
        antigravity_model = "high" if topic_is_important(topic) else "low"
        reply = self._execute_speech(agent_key, session_id, round_no, prompt, antigravity_model)
        # Anthropic 反传话游戏：长产物写 artifacts/ 文件，黑板只回传引用（省 token 防失真）
        if len(reply) > 400:
            artifact_name = f"r{round_no}_{agent_key}.md"
            artifact_path = os.path.join(session_dir(session_id), "artifacts", artifact_name)
            with open(artifact_path, "w", encoding="utf-8") as f:
                f.write(f"# {agent['name']} 第{round_no}轮完整发言\n\n{reply}\n")
            reply_brief = reply[:400] + f"\n\n📎 [完整产物已存档: artifacts/{artifact_name}]"
            # 个人记忆存完整版
            mem_path = os.path.join(session_dir(session_id), "memories", f"{agent_key}.md")
            with open(mem_path, "a", encoding="utf-8") as f:
                f.write(f"\n## 第{round_no}轮 ({time.strftime('%H:%M:%S')})\n{reply}\n")
            return reply_brief
        # 追加到个人记忆
        mem_path = os.path.join(session_dir(session_id), "memories", f"{agent_key}.md")
        with open(mem_path, "a", encoding="utf-8") as f:
            f.write(f"\n## 第{round_no}轮 ({time.strftime('%H:%M:%S')})\n{reply}\n")
        return reply

    # ---- 主流程
    def run(self, topic, on_event=None, cancel_check=None):
        """主持一场收敛式圆桌讨论；on_event(msg_type, payload) 供飞书播报"""
        # 断点续跑：回收上次崩溃遗留的 running 任务（幂等缓存保证不重复执行）
        reclaim_stale(running_timeout=600, session_timeout=3600)
        # 先预研再创建本场，避免把刚创建的 running session 误认成历史会议。
        topic_ctx = research_topic(topic)
        session_id = create_session(topic)
        # 会议报告卡：重置统计 + 开始计时
        self._t0 = time.time()
        self._calls = 0
        self._cache_hits = 0
        self._errors = 0
        if on_event and topic_ctx:
            on_event("progress", {"round": 0, "msg": "🔎 议题预研中…"})
        # 议题重要性 → 反重力模型档位（贯穿整场会议，start 事件里带出去供飞书显示）
        antigravity_model = "high" if topic_is_important(topic) else "low"
        if on_event:
            on_event("start", {
                "session_id": session_id,
                "topic": topic,
                "members": [AGENTS[k]["name"] for k in ORDER],
                "arch_model": "gemini-3.1-pro-high" if antigravity_model == "high" else "gemini-3.1-pro-low",
                "arch_level": "质量优先" if antigravity_model == "high" else "快速档",
            })

        # 黑板
        transcript = []  # {turn, agent, stance, text, ts}
        stances_history = []  # 每轮的立场 dict
        active_order = list(ORDER)
        unavailable_agents = {}
        round_no = 1

        try:
            if cancel_check:
                cancel_check()
            while round_no <= MAX_ROUNDS:
                if cancel_check:
                    cancel_check()
                if round_no == 1:
                    current_order = list(active_order)
                    stage = "这是第一轮，请基于你的专业视角**独立发表初步意见**（不要参考他人，直接给干货）。"
                    # ---- 第 1 轮并行化：3 个 CLI 同时跑
                    if on_event:
                        on_event("progress", {"round": 1, "msg": "第1轮·三真身并行独立发言中…"})
                    results = {}
                    with ThreadPoolExecutor(max_workers=len(current_order)) as ex:
                        futures = {
                            ex.submit(self._speak, k, topic, session_id, 1, "", "", stage, on_event, topic_context=topic_ctx): k
                            for k in current_order
                        }
                        for fut in futures:
                            k = futures[fut]
                            results[k] = fut.result()
                    if cancel_check:
                        cancel_check()
                else:
                    current_order = list(active_order)
                    # ---- 第 2 轮起并行：成员都基于【上一轮】黑板发言，同一轮内互不依赖
                    # （不需要看到同轮他人的发言，只有轮与轮之间有先后依赖）
                    prev_summary = "\n".join(
                        f"{AGENTS[t['agent']]['name']}（{t['stance']}）：{t['text'][:200]}" for t in transcript
                    )
                    stage = (
                        "请针对其他成员的观点**明确表态**：同意什么、补充什么、反对什么，要有交锋感。"
                        if round_no == 2
                        else "这是收敛轮，请聚焦**最终共识**：给出你确认的关键结论与行动建议，减少分歧。"
                    )
                    results = {}
                    if on_event:
                        on_event("progress", {"round": round_no, "msg": f"第{round_no}轮·成员并行发言中…"})
                    with ThreadPoolExecutor(max_workers=len(current_order)) as ex:
                        futures = {}
                        for k in current_order:
                            mem_text = self._read_memory(session_id, k)
                            futures[ex.submit(self._speak, k, topic, session_id, round_no, prev_summary, mem_text, stage, on_event, topic_context=topic_ctx)] = k
                        for fut in futures:
                            results[futures[fut]] = fut.result()
                    if cancel_check:
                        cancel_check()

                # 记录本轮发言到黑板
                stances_now = {}
                failed_this_round = []
                for k in current_order:
                    if is_engine_error(results[k]):
                        failed_this_round.append(k)
                        unavailable_agents[k] = results[k]
                        entry = {"turn": f"r{round_no}-{k}", "agent": k, "stance": "不可用", "text": results[k], "ts": time.strftime("%H:%M:%S")}
                        transcript.append(entry)
                        _write_board(session_id, "transcript.jsonl", entry)
                        if on_event:
                            on_event("agent_error", {"agent": AGENTS[k]["name"], "error": results[k]})
                        continue
                    stance = extract_stance(results[k])
                    stances_now[k] = stance
                    entry = {"turn": f"r{round_no}-{k}", "agent": k, "stance": stance, "text": results[k], "ts": time.strftime("%H:%M:%S")}
                    transcript.append(entry)
                    _write_board(session_id, "transcript.jsonl", entry)
                    if on_event:
                        on_event("speech", {"agent": AGENTS[k]["name"], "stance": stance, "text": results[k]})
                if failed_this_round:
                    active_order = [key for key in active_order if key not in failed_this_round]
                if len(active_order) < 2:
                    names = "、".join(AGENTS[key]["name"] for key in unavailable_agents)
                    raise RoundTableQuorumError(f"有效成员不足 2 人；不可用成员：{names}")
                stances_history.append(stances_now)

                # 收敛判定
                dv = divergence(stances_now, stances_history[-2] if len(stances_history) > 1 else None)
                # 收敛判定（纯函数）：共识 = ≥2轮无反对/中立且至少一人同意；固定点 = 立场与上轮完全一致
                prev_stances = stances_history[-2] if len(stances_history) > 1 else None
                converged, how = meeting_converged(round_no, stances_now, prev_stances)
                consensus = (how == "consensus")
                fixed_point = (how == "fixed_point")

                state = {
                    "session_id": session_id,
                    "round": round_no,
                    "stances": stances_now,
                    "divergence": dv,
                    "consensus": consensus,
                    "fixed_point": fixed_point,
                    "unavailable_agents": list(unavailable_agents),
                }
                save_session_state(session_id, state)
                if on_event:
                    on_event("round_end", {"round": round_no, "stances": stances_now, "consensus": consensus, "fixed_point": fixed_point})

                if consensus or fixed_point:
                    if on_event:
                        on_event("progress", {"round": round_no, "msg": "达成收敛（共识/立场固定点），提前结束讨论"})
                    break
                round_no += 1

            # ---- PM 总结陈词（judge 收口）
            if on_event:
                on_event("progress", {"round": round_no, "msg": "👔 产品经理总结陈词中…"})
            if cancel_check:
                cancel_check()
            summary = self._summarize(session_id, topic, transcript, on_event)
            # 会议报告卡：耗时/轮数/调用/缓存命中/错误，播报给老板
            dur = int(time.time() - self._t0)
            if on_event:
                on_event("report", {
                    "duration": f"{dur // 60}m{dur % 60:02d}s",
                    "rounds": round_no,
                    "calls": self._calls,
                    "cache_hits": self._cache_hits,
                    "errors": self._errors,
                })
            # 会议生命周期收口：置 done（此前永远停在 running）
            try:
                conn = _conn()
                conn.execute(
                    "UPDATE sessions SET status='done', updated_at=? WHERE id=?",
                    (time.strftime("%Y-%m-%d %H:%M:%S"), session_id),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass
            return {
                "session_id": session_id,
                "topic": topic,
                "rounds_used": round_no,
                "transcript": transcript,
                "final_summary": summary,
                "unavailable_agents": {AGENTS[key]["name"]: error for key, error in unavailable_agents.items()},
                "state": load_session_state(session_id),
            }
        except TaskCancelled:
            try:
                conn = _conn()
                conn.execute(
                    "UPDATE sessions SET status='cancelled', updated_at=? WHERE id=?",
                    (time.strftime("%Y-%m-%d %H:%M:%S"), session_id),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass
            raise
        except Exception as e:
            try:
                conn = _conn()
                conn.execute(
                    "UPDATE sessions SET status='failed', updated_at=? WHERE id=?",
                    (time.strftime("%Y-%m-%d %H:%M:%S"), session_id),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass
            raise e

    def _read_memory(self, session_id, agent_key):
        """只读取该 Agent 在本场会议中的历史发言，避免跨会议上下文污染。"""
        cur = os.path.join(session_dir(session_id), "memories", f"{agent_key}.md")
        try:
            with open(cur, "r", encoding="utf-8") as f:
                return f.read()[-2000:]
        except OSError:
            return ""

    def _summarize(self, session_id, topic, transcript, on_event=None):
        all_views = "\n".join(
            f"{AGENTS[t['agent']]['name']}（{t['stance']}）：{t['text'][:300]}" for t in transcript[-9:]
        )
        from swarm_orchestrator import call_llm
        self._calls += 1  # 总结陈词计一次 LLM 调用
        members_str = "、".join(AGENTS[k]["name"] for k in ORDER)
        summary_prompt = (
            f"你是会议主持人。刚才 {len(ORDER)} 位成员（{members_str}）就【{topic}】完成了圆桌讨论，最终发言如下：\n"
            f"{all_views}\n\n"
            f"请向老板（人类CEO）做**最终总结陈词**：\n"
            f"1. 共识点（大家一致认可什么）\n"
            f"2. 分歧点（哪里还有不同意见，各是什么立场）\n"
            f"3. 你的裁决建议（作为主持人，你推荐怎么推进）\n"
            f"4. 抛给老板 2 个关键决策问题（让老板做选择）\n"
            f"历史记录和成员推测只能标为待验证；没有本场证据的旧错误，不得写成当前已确认故障。\n"
            f"控制在 300 字以内，结构清晰。"
        )
        summary = call_llm(
            "你是高效果断的产品经理兼会议主持人，善于总结共识、暴露分歧、让老板做选择题。",
            summary_prompt,
            model="deepseek-chat",
        )
        # 会议纪要存档
        minutes = (
            f"# 圆桌会议纪要 · {topic}\n\n"
            f"> 会议时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"> 会议ID：{session_id}\n"
            f"> 参会：{', '.join(AGENTS[k]['name'] for k in ORDER)}\n\n"
            f"## 最终总结\n{summary}\n\n"
            f"## 完整记录\n"
        )
        for t in transcript:
            turn_label = f"第{t['turn'].split('-', 1)[0].removeprefix('r')}轮"
            minutes += f"\n### {AGENTS[t['agent']]['name']}（{turn_label} · 立场: {t['stance']}）\n{t['text']}\n"
        minutes_path = os.path.join(session_dir(session_id), "minutes.md")
        with open(minutes_path, "w", encoding="utf-8") as f:
            f.write(minutes)
        return summary


# 单例
roundtable_v2 = RoundTableV2()

if __name__ == "__main__":
    import sys
    test_topic = sys.argv[1] if len(sys.argv) > 1 else "测试议题：讨论飞书多Agent圆桌引擎V2.1的收敛机制与工程化落地"
    print(f"=== 启动圆桌引擎 V2.1 测试: {test_topic} ===")
    def cli_event(event_type, payload):
        print(f"[{event_type}] {payload}")
    res = roundtable_v2.run(test_topic, on_event=cli_event)
    print("\n=== 会议总结 ===")
    print(res["final_summary"])
    print(f"\n=== 完成: 轮数 {res['rounds_used']}, 会议ID {res['session_id']} ===")
