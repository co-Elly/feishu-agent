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


class _WindowsKillJob:
    """Own a Windows Job Object that kills an invocation's full child tree on close."""
    KILL_ON_JOB_CLOSE = 0x00002000

    def __init__(self):
        self.handle = None

    def assign(self, proc):
        if os.name != "nt":
            return False
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                      wintypes.LPVOID, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return False
        info = EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = self.KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(handle)
            return False
        if not kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(proc._handle)):
            kernel32.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def close(self):
        if self.handle and os.name == "nt":
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self.handle)
            self.handle = None


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
    "unsafe_configuration": "不安全配置",
    "sandbox_error": "沙箱执行失败",
}


ZERO_EXIT_ERROR_MARKERS = (
    "encountered error in step execution",
    "sandbox configuration error",
    "error executing cascade step",
    "cortex_step_type_run_command",
)


def antigravity_script_safety(script, expected_mode):
    try:
        text = open(script, encoding="utf-8").read().lower()
    except OSError as exc:
        return False, f"无法读取反重力启动脚本: {exc}"
    if "--dangerously-skip-permissions" in text:
        return False, "启动脚本启用了 --dangerously-skip-permissions"
    if "--sandbox" not in text:
        return False, "启动脚本未启用 --sandbox"
    if not re.search(rf"--mode\s+{re.escape(expected_mode)}(?:\s|$)", text):
        return False, f"启动脚本未使用 --mode {expected_mode}"
    return True, ""


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


def _run(engine, command, timeout, cwd, input_text=None, extra_env=None):
    """Run once; only network-class failures receive one retry."""
    started = time.monotonic()
    for attempt in range(2):
        proc, kill_job = None, _WindowsKillJob()
        try:
            proc = subprocess.Popen(
                command, stdin=subprocess.PIPE if input_text is not None else None,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", cwd=cwd,
                env={**os.environ, **(extra_env or {}), "PYTHONUTF8": "1"}, shell=False,
            )
            kill_job.assign(proc)
            stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
        except Exception as exc:
            kill_job.close()
            if proc and proc.poll() is None:
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
            code, retryable = classify_error(str(exc), exc)
            if code == "network" and attempt == 0:
                continue
            return _result(engine, started, False, f"{ERROR_LABELS[code]}: {exc}", code, retryable)
        finally:
            kill_job.close()
        stdout, stderr = (stdout or "").strip(), (stderr or "").strip()
        if proc.returncode == 0:
            combined = "\n".join(part for part in (stdout, stderr) if part)
            lower_combined = combined.lower()
            if any(token in lower_combined for token in ZERO_EXIT_ERROR_MARKERS):
                return _result(
                    engine, started, False,
                    f"{ERROR_LABELS['sandbox_error']}: {combined[-600:]}",
                    "sandbox_error", False,
                )
            if any(token in lower_combined for token in (
                "401 unauthorized", "resource_exhausted", "individual quota reached",
                "authentication failed", "eligibility error",
            )):
                code, retryable = classify_error(combined)
                return _result(engine, started, False, f"{ERROR_LABELS[code]}: {combined[-600:]}", code, retryable)
            if stdout:
                return _result(engine, started, True, stdout)
        diagnostic = stderr or (
            stdout if len(stdout) < 2000 else f"进程退出码 {proc.returncode}；模型已产生长输出但执行器未成功退出"
        )
        detail = diagnostic
        if stdout and stdout != diagnostic:
            detail += f"\n模型输出尾部：{stdout[-400:]}"
        # 退出码非零但模型已产出实质回答时，禁止用回答正文做错误分类——
        # 正文里出现 "command not found" 等词是模型在讨论问题，不是执行器缺命令。
        # 只有 stderr（真正的诊断通道）才允许参与 classify_error。
        if stdout and not stderr:
            code, retryable = "process_error", False
        else:
            code, retryable = classify_error(diagnostic)
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


def call_codex(task_text, timeout=300, writable=False, workspace_dir=None):
    configured = os.environ.get("FEISHU_CODEX_COMMAND") or runtime_value("codex_command")
    sandbox = "workspace-write" if writable else "read-only"
    target_dir = os.path.abspath(workspace_dir or BASE_DIR)
    command = shlex.split(configured, posix=False) + [
        "exec", "--skip-git-repo-check", "--color", "never", "--sandbox", sandbox,
        "--ephemeral", "-C", target_dir, "-",
    ]
    return _run("codex", command, timeout, target_dir, input_text=task_text)


def call_hermes(task_text, timeout=300):
    task_file = isolated_prompt_file("hermes_", task_text)
    bash_path = task_file.replace("E:", "/mnt/e").replace("\\", "/") if task_file.startswith("E:") else task_file
    hermes = shlex.quote(str(runtime_value("hermes_command")))
    command = [
        "wsl.exe", "-e", "bash", "-lc",
        f'{hermes} -z "$(cat "$1")" -t clarify --safe-mode', "bash", bash_path,
    ]
    return _run("hermes", command, timeout, BASE_DIR)


WRITE_INTENT_PAT = re.compile(
    r"(写入|写入到|保存到|保存为|新建|创建|创建文件|修改|编辑|删除|覆盖|更新)"
    r"[^。\n]{0,60}?\.(py|js|ts|json|yaml|yml|md|txt|toml|cfg|ini|bat|ps1|sh|html|css|csv)\b"
    r"|(?:write|create|modify|edit|update|save)[\w\s]{0,30}\b(?:to\b)?[\w\-./\\]*\.\w{1,5}",
    re.I,
)


def detect_write_intent(task_text):
    """检测任务书是否包含明确的“写文件”意图（动词 + 文件名）。

    agy headless/plan 模式无法授予写权限，含写意图的任务必须走协作 staging 流程，
    提前拦截避免整单执行到一半才报 write_file permission 错误。
    """
    if not task_text:
        return False
    return bool(WRITE_INTENT_PAT.search(task_text))


def call_antigravity(task_text, timeout=200, model="low", workspace_dir=None):
    if detect_write_intent(task_text):
        return _result(
            "antigravity", time.monotonic(), False,
            "任务包含写文件意图：agy 以 plan 模式运行且 headless 下无法提示授权。"
            "请通过「协作 <目标>」流程在 staging 工作区执行，或改派 Codex（workspace-write）。",
            "write_intent_requires_staging", False,
        )
    task_file = isolated_prompt_file("agy_", task_text)
    if task_file.startswith("/mnt/"):
        drive = task_file[5].upper()
        task_file = f"{drive}:{task_file[6:].replace('/', chr(92))}"
    script = runtime_value("antigravity_script_high" if model == "high" else "antigravity_script_low")
    expected_mode = "plan"
    safe, reason = antigravity_script_safety(script, expected_mode)
    if not safe:
        return _result("antigravity", time.monotonic(), False, reason,
                       "unsafe_configuration", False)
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, task_file]
    service_profile = os.path.abspath(runtime_value("antigravity_service_profile"))
    return _run(
        "antigravity", command, timeout, os.path.abspath(workspace_dir or "C:\\"),
        extra_env={"USERPROFILE": service_profile},
    )


AGENT_CALLS = {"hermes": call_hermes, "antigravity": call_antigravity, "codex": call_codex}


def lightweight_health():
    codex_parts = shlex.split(str(runtime_value("codex_command")), posix=False)
    antigravity_safe = all(
        antigravity_script_safety(runtime_value(key), mode)[0]
        for key, mode in (("antigravity_script_low", "plan"),
                          ("antigravity_script_high", "plan"))
    )
    return {
        "hermes": bool(runtime_value("hermes_command")) and shutil.which("wsl.exe") is not None,
        "antigravity": shutil.which("powershell.exe") is not None and antigravity_safe,
        "codex": bool(codex_parts) and shutil.which(codex_parts[0]) is not None,
    }


def deep_health_probe():
    prompts = {name: "只回复 OK，用于健康检查，不读取或修改文件。" for name in AGENT_CALLS}
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="agent-health") as pool:
        futures = {name: pool.submit(AGENT_CALLS[name], prompt, 60) for name, prompt in prompts.items()}
        return {name: future.result() for name, future in futures.items()}
