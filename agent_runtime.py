"""Unified local Agent runtime with bounded retries, health and error taxonomy."""

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from control_store import record_engine_health
from settings import BASE_DIR, runtime_value


@dataclass(frozen=True)
class AgentResult:
    ok: bool
    text: str
    error_code: str | None = None
    retryable: bool = False
    cooldown_until: float | None = None
    duration_ms: int = 0


ERROR_LABELS = {
    "missing_command": "缺少命令", "authentication": "认证失败",
    "quota_exhausted": "额度耗尽", "timeout": "调用超时",
    "network": "网络故障", "process_error": "进程异常",
}


def cooldown_seconds(text, code):
    match = re.search(r"Resets in\s*(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text or "", re.I)
    if match:
        hours, minutes, seconds = (int(value or 0) for value in match.groups())
        return max(30, hours * 3600 + minutes * 60 + seconds + 10)
    return {"authentication": 300, "quota_exhausted": 300, "timeout": 60, "network": 30}.get(code, 0)


def classify_error(text, exception=None):
    if isinstance(exception, (FileNotFoundError, PermissionError)):
        return "missing_command", False
    if isinstance(exception, subprocess.TimeoutExpired):
        return "timeout", True
    lower = (text or "").lower()
    if any(token in lower for token in ("command not found", "is not recognized", "no such file or directory")):
        return "missing_command", False
    if any(token in lower for token in ("401", "unauthorized", "authentication failed", "not logged in", "invalid api key")):
        return "authentication", False
    if any(token in lower for token in ("quota", "rate limit", "resource_exhausted", "eligibility", "resets in")):
        return "quota_exhausted", False
    if any(token in lower for token in ("network", "connection", "dns", "proxy", "eof", "timed out")):
        return "network", True
    return "process_error", False


def _result(engine, started, ok, text, error_code=None, retryable=False):
    duration = int((time.monotonic() - started) * 1000)
    cooldown = time.time() + cooldown_seconds(text, error_code) if error_code else None
    result = AgentResult(ok, (text or "").strip(), error_code, retryable, cooldown, duration)
    try:
        record_engine_health(engine, result)
    except Exception as exc:
        print(f"[Health Store Warning] {type(exc).__name__}: {exc}")
    return result


def _run(engine, command, timeout, cwd, input_text=None):
    """Run once; only network-class failures receive one retry."""
    started = time.monotonic()
    for attempt in range(2):
        try:
            proc = subprocess.run(
                command, input=input_text, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd,
                env={**os.environ, "PYTHONUTF8": "1"}, shell=False,
            )
        except Exception as exc:
            code, retryable = classify_error(str(exc), exc)
            if code == "network" and attempt == 0:
                continue
            return _result(engine, started, False, f"{ERROR_LABELS[code]}: {exc}", code, retryable)
        stdout, stderr = (proc.stdout or "").strip(), (proc.stderr or "").strip()
        if proc.returncode == 0 and stdout:
            lower_stdout = stdout.lower()
            if any(token in lower_stdout for token in (
                "401 unauthorized", "resource_exhausted", "individual quota reached",
                "authentication failed", "eligibility error",
            )):
                code, retryable = classify_error(stdout)
                return _result(engine, started, False, f"{ERROR_LABELS[code]}: {stdout[-600:]}", code, retryable)
            return _result(engine, started, True, stdout)
        detail = stderr or stdout or f"进程退出码 {proc.returncode}"
        code, retryable = classify_error(detail)
        if code == "network" and attempt == 0:
            continue
        return _result(engine, started, False, f"{ERROR_LABELS[code]}: {detail[-600:]}", code, retryable)
    return _result(engine, started, False, "网络故障", "network", True)


def isolated_prompt_file(prefix, content):
    task_dir = os.path.join(runtime_value("workspace_dir"), "tasks")
    os.makedirs(task_dir, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=prefix,
                                         suffix=".txt", dir=task_dir, delete=False)
    with handle:
        handle.write(content)
    return handle.name


def call_codex(task_text, timeout=300, writable=False):
    configured = os.environ.get("FEISHU_CODEX_COMMAND") or runtime_value("codex_command")
    sandbox = "workspace-write" if writable else "read-only"
    command = shlex.split(configured, posix=False) + [
        "exec", "--skip-git-repo-check", "--color", "never", "--sandbox", sandbox,
        "-C", BASE_DIR, "-",
    ]
    return _run("codex", command, timeout, BASE_DIR, input_text=task_text)


def call_hermes(task_text, timeout=300):
    task_file = isolated_prompt_file("hermes_", task_text)
    bash_path = task_file.replace("E:", "/mnt/e").replace("\\", "/") if task_file.startswith("E:") else task_file
    hermes = shlex.quote(str(runtime_value("hermes_command")))
    command = ["wsl.exe", "-e", "bash", "-lc", f'{hermes} -z "$(cat "$1")" -t terminal', "bash", bash_path]
    return _run("hermes", command, timeout, BASE_DIR)


def call_antigravity(task_text, timeout=200, model="low"):
    task_file = isolated_prompt_file("agy_", task_text)
    if task_file.startswith("/mnt/"):
        drive = task_file[5].upper()
        task_file = f"{drive}:{task_file[6:].replace('/', chr(92))}"
    script = runtime_value("antigravity_script_high" if model == "high" else "antigravity_script_low")
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, task_file]
    return _run("antigravity", command, timeout, "C:\\")


AGENT_CALLS = {"hermes": call_hermes, "antigravity": call_antigravity, "codex": call_codex}


def lightweight_health():
    codex_parts = shlex.split(str(runtime_value("codex_command")), posix=False)
    return {
        "hermes": bool(runtime_value("hermes_command")) and shutil.which("wsl.exe") is not None,
        "antigravity": shutil.which("powershell.exe") is not None and any(
            os.path.isfile(runtime_value(key)) for key in ("antigravity_script_low", "antigravity_script_high")
        ),
        "codex": bool(codex_parts) and shutil.which(codex_parts[0]) is not None,
    }


def deep_health_probe():
    prompts = {name: "只回复 OK，用于健康检查，不读取或修改文件。" for name in AGENT_CALLS}
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="agent-health") as pool:
        futures = {name: pool.submit(AGENT_CALLS[name], prompt, 60) for name, prompt in prompts.items()}
        return {name: future.result() for name, future in futures.items()}
