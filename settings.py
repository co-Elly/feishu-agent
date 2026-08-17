"""Single configuration entry point with validation and secret redaction."""

import json
import os
import re
from functools import lru_cache


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

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
        return re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer ***", value)
    return value
