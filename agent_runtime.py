"""Local agent process adapters with isolated prompts and bounded execution."""

import os
import shlex
import subprocess
import tempfile


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _codex_command():
    configured = os.environ.get("FEISHU_CODEX_COMMAND", "").strip()
    if configured:
        return shlex.split(configured, posix=False)
    return ["codex"]


def call_codex(task_text, timeout=300, writable=False):
    """Run Codex non-interactively; writes require an explicit caller decision."""
    sandbox = "workspace-write" if writable else "read-only"
    command = _codex_command() + [
        "exec",
        "--skip-git-repo-check",
        "--color",
        "never",
        "--sandbox",
        sandbox,
        "-C",
        BASE_DIR,
        "-",
    ]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.run(
            command,
            input=task_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=BASE_DIR,
            env=env,
            shell=False,
        )
    except FileNotFoundError:
        return "Codex 调用失败: 未找到 Codex CLI；请设置 FEISHU_CODEX_COMMAND。"
    except PermissionError:
        return "Codex 调用失败: 当前服务账户无权启动 Codex CLI。"
    except subprocess.TimeoutExpired:
        return "Codex 思考超时（请稍后重试或缩小任务范围）"
    except Exception as exc:
        return f"Codex 调用异常: {exc}"

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode == 0 and stdout:
        return stdout
    detail = stderr or stdout or f"进程退出码 {proc.returncode}"
    return f"Codex 调用失败: {detail[-600:]}"


def isolated_prompt_file(prefix, content):
    """Create a unique prompt file for legacy CLI bridges that require a path."""
    task_dir = os.path.join(BASE_DIR, "workspace", "tasks")
    os.makedirs(task_dir, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=prefix, suffix=".txt",
        dir=task_dir, delete=False,
    )
    with handle:
        handle.write(content)
    return handle.name
