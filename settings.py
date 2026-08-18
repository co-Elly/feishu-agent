"""Single configuration entry point with validation and secret redaction."""

import hashlib
import json
import logging
import os
import re
import threading
from functools import lru_cache
import yaml


logger = logging.getLogger("prompts")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
PROMPTS_PATH = os.path.join(BASE_DIR, "prompts.yaml")

# 总结模板占位符白名单
ALLOWED_SUMMARY_VARIABLES = {"topic", "agent_count", "members", "all_views"}

RUNTIME_DEFAULTS = {
    "workspace_dir": os.path.join(BASE_DIR, "workspace"),
    "obsidian_vault": os.environ.get("FEISHU_OBSIDIAN_VAULT", os.path.join(BASE_DIR, "workspace", "obsidian")),
    "antigravity_script_high": os.environ.get("FEISHU_ANTIGRAVITY_HIGH", ""),
    "antigravity_script_low": os.environ.get("FEISHU_ANTIGRAVITY_LOW", ""),
    "codex_command": "codex",
    "hermes_command": "hermes",
    "approval_ttl_seconds": 1800,
    "task_workers": 4,
}

# ---------------------------------------------------------------- 提示词与风格预设
STYLE_PRESETS = {
    # 常用英文预设
    "concise": "言简意赅，直击要害，只讲关键结论（≤100字）。",
    "detailed": "详尽严密，提供充分的技术细节、推导过程与架构权衡。",
    "critical": "尖锐犀利，重点挑剔方案的潜在漏洞、边界缺陷与安全隐患。",
    "constructive": "兼顾批判与建议，指出问题的同时必给出至少一种替代方案。",
    "structured": "结构化表达，给出模块/接口层面的专业意见。",
    "actionable": "给出可落地的技术方案与实现要点。",
    "summary_and_guide": "言简意赅，善于总结和引导。",
    # 中文别名
    "精炼": "言简意赅，直击要害，只讲关键结论（≤100字）。",
    "详尽": "详尽严密，提供充分的技术细节、推导过程与架构权衡。",
    "严苛": "尖锐犀利，重点挑剔方案的潜在漏洞、边界缺陷与安全隐患。",
    "挑刺": "尖锐犀利，重点挑剔方案的潜在漏洞、边界缺陷与安全隐患。",
    "建设性": "兼顾批判与建议，指出问题的同时必给出至少一种替代方案。",
    "结构化": "结构化表达，给出模块/接口层面的专业意见。",
    "落地": "给出可落地的技术方案与实现要点。",
    "务实": "给出可落地的技术方案与实现要点。",
    "引导": "言简意赅，善于总结和引导。",
}

DEFAULT_AGENTS = {
    "pm": {
        "name": "👔 产品经理·Hermes",
        "engine": "hermes",
        "role": "你是产品经理兼会议主持人（Hermes 主脑真身）。你负责：把控讨论方向、评估意见、推动共识、最后向老板汇报。你关注需求价值、用户视角与落地优先级。",
        "style": "言简意赅，善于总结和引导。",
    },
    "arch": {
        "name": "📐 反重力·架构师",
        "engine": "antigravity",
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

DEFAULT_SUMMARY_CONFIG = {
    "system": "你是高效果断的产品经理兼会议主持人，善于总结共识、暴露分歧、让老板做选择题。",
    "template": (
        "你是会议主持人。刚才 {agent_count} 位成员（{members}）就【{topic}】完成了圆桌讨论，最终发言如下：\n"
        "{all_views}\n\n"
        "请向老板（人类CEO）做**最终总结陈词**：\n"
        "1. 共识点（大家一致认可什么）\n"
        "2. 分歧点（哪里还有不同意见，各是什么立场）\n"
        "3. 你的裁决建议（作为主持人，你推荐怎么推进）\n"
        "4. 抛给老板 2 个关键决策问题（让老板做选择）\n"
        "历史记录和成员推测只能标为待验证；没有本场证据的旧错误，不得写成当前已确认故障。\n"
        "控制在 300 字以内，结构清晰。"
    ),
}

# ---------------------------------------------------------------- 提示词热重载与校验状态
_PROMPTS_LOCK = threading.Lock()
_ACTIVE_PROMPTS_PATH = PROMPTS_PATH
_PROMPTS_CACHE = None
_PROMPTS_MTIME = 0.0
_PROMPTS_HASH = ""
_PROMPTS_VERSION = ""


def validate_prompts_data(data):
    """校验提示词配置数据合法性：
    1. YAML 数据必须是 dict
    2. 必需包含有效非空的 version 字段
    3. agents / pm_summary 结构合法
    4. 变量白名单校验（pm_summary.template 仅允许 ALLOWED_SUMMARY_VARIABLES）
    """
    if not isinstance(data, dict):
        return False, "YAML 提示词根结构必须为字典对象"

    version = data.get("version")
    if version is None or not str(version).strip():
        return False, "缺少必需的 version 字段或版本号为空"

    agents_sec = data.get("agents")
    if agents_sec is not None:
        if not isinstance(agents_sec, dict):
            return False, "agents 字段必须为字典"
        for key, agent in agents_sec.items():
            if not isinstance(agent, dict):
                return False, f"agents.{key} 必须为字典"

    summary_sec = data.get("pm_summary")
    if summary_sec is not None:
        if not isinstance(summary_sec, dict):
            return False, "pm_summary 字段必须为字典"
        template = summary_sec.get("template") or summary_sec.get("prompt")
        if template is not None:
            if not isinstance(template, str):
                return False, "pm_summary.template 必须为字符串"
            # 提取模板占位符
            placeholders = set(re.findall(r"(?<!\{)\{([a-zA-Z0-9_]+)\}(?!\})", template))
            invalid_vars = placeholders - ALLOWED_SUMMARY_VARIABLES
            if invalid_vars:
                return False, f"pm_summary.template 包含非白名单变量: {', '.join(sorted(invalid_vars))}"

    return True, None


def load_prompts_yaml(path=None, force=False):
    """加载并校验 prompts.yaml，支持基于 mtime 的原子热更新与异常回退"""
    global _PROMPTS_CACHE, _PROMPTS_MTIME, _PROMPTS_HASH, _PROMPTS_VERSION, _ACTIVE_PROMPTS_PATH
    with _PROMPTS_LOCK:
        target_path = os.path.abspath(path) if path else _ACTIVE_PROMPTS_PATH
        if path:
            if _ACTIVE_PROMPTS_PATH != target_path:
                _ACTIVE_PROMPTS_PATH = target_path
                force = True

        if not os.path.exists(target_path):
            return _PROMPTS_CACHE

        try:
            mtime = os.path.getmtime(target_path)
        except OSError as exc:
            logger.error(f"获取提示词文件 mtime 失败: {exc}，保持上一有效版本 (version={_PROMPTS_VERSION}, hash={_PROMPTS_HASH[:16]})")
            return _PROMPTS_CACHE

        if not force and mtime == _PROMPTS_MTIME and _PROMPTS_CACHE is not None:
            return _PROMPTS_CACHE

        try:
            with open(target_path, "r", encoding="utf-8") as handle:
                raw_content = handle.read()
        except OSError as exc:
            logger.error(f"读取提示词文件失败: {exc}，保持上一有效版本 (version={_PROMPTS_VERSION}, hash={_PROMPTS_HASH[:16]})")
            return _PROMPTS_CACHE

        content_hash = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()

        try:
            data = yaml.safe_load(raw_content)
        except Exception as exc:
            logger.error(f"YAML 语法解析失败: {exc}，回退上一有效版本 (version={_PROMPTS_VERSION}, hash={_PROMPTS_HASH[:16]})")
            return _PROMPTS_CACHE

        valid, err_msg = validate_prompts_data(data)
        if not valid:
            logger.error(f"提示词数据校验失败: {err_msg}，回退上一有效版本 (version={_PROMPTS_VERSION}, hash={_PROMPTS_HASH[:16]})")
            return _PROMPTS_CACHE

        # 校验通过，执行原子切换
        _PROMPTS_CACHE = data
        _PROMPTS_MTIME = mtime
        _PROMPTS_HASH = content_hash
        _PROMPTS_VERSION = str(data.get("version", ""))
        logger.info(f"提示词配置已成功加载/热更新: version={_PROMPTS_VERSION}, hash={_PROMPTS_HASH[:16]}")
        return _PROMPTS_CACHE


def set_prompts_path(path):
    """设置当前提示词文件路径并强制重新加载"""
    global _ACTIVE_PROMPTS_PATH
    _ACTIVE_PROMPTS_PATH = os.path.abspath(path)
    return load_prompts_yaml(path=_ACTIVE_PROMPTS_PATH, force=True)


def reset_prompts_path():
    """重置回默认的 prompts.yaml 路径"""
    global _ACTIVE_PROMPTS_PATH
    _ACTIVE_PROMPTS_PATH = PROMPTS_PATH
    return load_prompts_yaml(path=PROMPTS_PATH, force=True)


def get_active_prompts_meta(path=None):
    """获取当前生效的提示词版本与哈希元数据"""
    load_prompts_yaml(path=path)
    return {
        "version": _PROMPTS_VERSION,
        "hash": _PROMPTS_HASH,
        "mtime": _PROMPTS_MTIME,
    }


def resolve_style(style_val, default=""):
    """将风格配置值解析为具体的 prompt 描述（支持预定义 preset key 或自定义文本）"""
    if not style_val:
        return default
    cleaned = str(style_val).strip()
    return STYLE_PRESETS.get(cleaned.lower(), STYLE_PRESETS.get(cleaned, cleaned))


def get_agents_config(config=None, prompts_path=None):
    """获取并合并 Agent 角色/风格配置。
    加载优先级：prompts.yaml -> config.json -> 内置 DEFAULT_AGENTS。
    缺失字段自动回退至默认值。
    """
    custom_agents = {}
    if config is not None:
        prompts_sec = config.get("prompts") or {}
        custom_agents = prompts_sec.get("agents") or config.get("agents") or {}
    else:
        yaml_data = load_prompts_yaml(path=prompts_path)
        if yaml_data and isinstance(yaml_data.get("agents"), dict):
            custom_agents = yaml_data.get("agents")
        else:
            try:
                cfg = load_config()
                prompts_sec = cfg.get("prompts") or {}
                custom_agents = prompts_sec.get("agents") or cfg.get("agents") or {}
            except Exception:
                custom_agents = {}

    result = {}
    for key, def_agent in DEFAULT_AGENTS.items():
        agent_copy = dict(def_agent)
        if isinstance(custom_agents, dict) and key in custom_agents and isinstance(custom_agents[key], dict):
            c_agent = custom_agents[key]
            if "name" in c_agent and str(c_agent["name"]).strip():
                agent_copy["name"] = str(c_agent["name"]).strip()
            if "role" in c_agent and str(c_agent["role"]).strip():
                agent_copy["role"] = str(c_agent["role"]).strip()
            if "style" in c_agent and str(c_agent["style"]).strip():
                agent_copy["style"] = resolve_style(c_agent["style"], def_agent.get("style", ""))
        result[key] = agent_copy
    return result


def get_summary_config(config=None, prompts_path=None):
    """获取 PM 总结词及 Prompt 模板配置。
    加载优先级：prompts.yaml -> config.json -> 内置 DEFAULT_SUMMARY_CONFIG。
    """
    summary_sec = {}
    if config is not None:
        prompts_sec = config.get("prompts") or {}
        summary_sec = prompts_sec.get("pm_summary") or config.get("pm_summary") or {}
    else:
        yaml_data = load_prompts_yaml(path=prompts_path)
        if yaml_data and isinstance(yaml_data.get("pm_summary"), dict):
            summary_sec = yaml_data.get("pm_summary")
        else:
            try:
                cfg = load_config()
                prompts_sec = cfg.get("prompts") or {}
                summary_sec = prompts_sec.get("pm_summary") or cfg.get("pm_summary") or {}
            except Exception:
                summary_sec = {}

    system_prompt = summary_sec.get("system")
    if not system_prompt or not str(system_prompt).strip():
        system_prompt = DEFAULT_SUMMARY_CONFIG["system"]

    template = summary_sec.get("template") or summary_sec.get("prompt")
    if not template or not str(template).strip():
        template = DEFAULT_SUMMARY_CONFIG["template"]

    return {
        "system": str(system_prompt).strip(),
        "template": str(template).strip(),
    }


class SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def format_summary_prompt(template, **kwargs):
    """安全格式化总结模板，防止因缺失参数或多余花括号导致异常"""
    try:
        return template.format_map(SafeFormatDict(**kwargs))
    except Exception:
        res = template
        for k, v in kwargs.items():
            res = res.replace(f"{{{k}}}", str(v))
        return res


def render_prompt(role, ctx=None, override=None):
    """
    渲染 Agent 发言 Prompt，支持运行期风格覆盖 (Runtime Style Override)。

    参数:
    - role: agent key (如 'pm', 'arch', 'dev') 或包含 role/style/engine 的字典，或纯角色人设字符串
    - ctx: 会议上下文 (dict 或 str)。支持字段: topic, round_no, stage, board_summary, my_memory, topic_context
    - override: 运行期风格覆盖 (仅接受 STYLE_PRESETS 预定义的 key/别名)，越界值自动忽略；系统约束与人设不可覆盖。
    """
    agents_cfg = get_agents_config()
    if isinstance(role, str) and role in agents_cfg:
        agent_data = agents_cfg[role]
        role_desc = agent_data["role"]
        default_style = agent_data.get("style", "")
        engine_type = agent_data.get("engine", "")
    elif isinstance(role, dict):
        role_desc = role.get("role", "")
        default_style = role.get("style", "")
        engine_type = role.get("engine", "")
    else:
        role_desc = str(role or "")
        default_style = ""
        engine_type = ""

    if isinstance(ctx, dict):
        topic = ctx.get("topic", "")
        topic_context = ctx.get("topic_context", "")
        round_no = ctx.get("round_no", 1)
        stage = ctx.get("stage", "")
        board_summary = ctx.get("board_summary", "")
        my_memory = ctx.get("my_memory", "")
        if override is None:
            override = ctx.get("override") or ctx.get("style_override") or ctx.get("style")
    elif isinstance(ctx, str):
        topic = ctx
        topic_context = ""
        round_no = 1
        stage = ""
        board_summary = ""
        my_memory = ""
    else:
        topic = ""
        topic_context = ""
        round_no = 1
        stage = ""
        board_summary = ""
        my_memory = ""

    # 运行期风格覆盖解析 (越界值忽略，系统约束/人设不可覆盖)
    effective_style = default_style
    if override is not None and str(override).strip():
        override_key = str(override).strip()
        if override_key.lower() in STYLE_PRESETS:
            effective_style = STYLE_PRESETS[override_key.lower()]
        elif override_key in STYLE_PRESETS:
            effective_style = STYLE_PRESETS[override_key]
        else:
            # 越界值自动忽略，保持默认
            pass

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
        f"【你的身份】{role_desc}\n"
        f"【当前轮次】第 {round_no} 轮\n{stage}\n"
        f"【发言要求】{effective_style} 输出格式：\n"
        f"第一行写「立场：同意/补充/反对/弃权」\n"
        f"然后给出你的专业意见（≤180字）。只输出发言内容本身，不要思考过程。"
    )
    if engine_type == "codex":
        prompt += (
            "\n\n⚠️ 特别注意：你收到的【会议主题】已经是明确的讨论议题，请直接给出你的专业意见，"
            "绝对不要反问、不要要求澄清、不要列问题选项、不要要求补充信息。"
            "你的角色是参与圆桌讨论的工程师，不是需求分析师。"
        )
    return prompt


class ConfigError(RuntimeError):
    pass


def _expand(value):
    if not isinstance(value, str):
        return value
    match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value.strip())
    return os.environ.get(match.group(1), "") if match else os.path.expandvars(value)


@lru_cache(maxsize=1)
def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            config = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"缺少配置文件: {CONFIG_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config.json 格式错误: {exc}") from exc
    for section in config.values():
        if isinstance(section, dict):
            for key, value in list(section.items()):
                section[key] = _expand(value)
    runtime = dict(RUNTIME_DEFAULTS)
    runtime.update(config.get("runtime") or {})
    runtime["workspace_dir"] = os.path.abspath(runtime["workspace_dir"])
    config["runtime"] = runtime
    return config


def runtime_value(name):
    return load_config()["runtime"][name]


def validate_startup(config=None):
    """Feishu credentials are fatal; optional agents only produce warnings."""
    config = config or load_config()
    feishu = config.get("feishu") or {}
    missing = [key for key in ("app_id", "app_secret") if not str(feishu.get(key, "")).strip()]
    if missing:
        raise ConfigError("飞书凭据缺失: " + ", ".join(missing))
    warnings = []
    runtime = config["runtime"]
    for label, key in (("反重力高质量脚本", "antigravity_script_high"), ("反重力快速脚本", "antigravity_script_low")):
        if not os.path.isfile(runtime[key]):
            warnings.append(f"{label}不存在，相关 Agent 将降级: {runtime[key]}")
    if not str(runtime.get("codex_command", "")).strip():
        warnings.append("未配置 Codex 命令，Codex Agent 将降级")
    if not str(runtime.get("hermes_command", "")).strip():
        warnings.append("未配置 Hermes 命令，Hermes Agent 将降级")
    return warnings


def redact(value):
    """Return a log-safe copy without credentials or bearer tokens."""
    secret_keys = {"app_secret", "api_key", "token", "authorization", "password"}
    if isinstance(value, dict):
        return {key: ("***" if key.lower() in secret_keys else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer ***", value)
        text = re.sub(r"(?i)\b(?:sk|rk|pk)-[A-Za-z0-9._~+/=-]{12,}", "***", text)
        return re.sub(
            r'''(?ix)(["']?(?:api[_-]?key|app[_-]?secret|access[_-]?token|authorization|password)["']?\s*[:=]\s*["']?)[^\s,"'}]+''',
            r"\1***",
            text,
        )
    return value
