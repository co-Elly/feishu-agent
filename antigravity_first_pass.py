"""Controller-mediated first-pass code authoring for Antigravity.

Antigravity stays in plan mode because its native file sandbox is not a reliable
write boundary.  It authors a unified diff; this module validates that diff and
applies it only to the task-scoped staging workspace.
"""

import os
import re
import shlex
import subprocess


MAX_CONTEXT_CHARS = 160_000
MAX_FILE_CHARS = 30_000
MAX_PATCH_CHARS = 240_000
TEXT_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".css", ".go", ".h", ".hpp", ".html", ".ini",
    ".java", ".js", ".json", ".jsx", ".md", ".php", ".ps1", ".py",
    ".rb", ".rs", ".rst", ".sh", ".sql", ".toml", ".ts", ".tsx",
    ".txt", ".xml", ".yaml", ".yml",
}
CONTROL_PATHS = {
    ".git", "config.json", "chat_log.jsonl", "chat_history.json",
    "persistent_memory.json", "_guardian.ps1", "scripts/restart_bot.ps1",
}
PATH_PATTERN = re.compile(
    r"(?i)(?<![\w./\\-])([\w .()@+\-/\\]+\.(?:c|cc|cpp|css|go|h|hpp|html|ini|java|js|json|jsx|md|php|ps1|py|rb|rs|rst|sh|sql|toml|ts|tsx|txt|xml|ya?ml))"
)


class AntigravityFirstPassError(RuntimeError):
    pass


def normalize_relative(path):
    raw = str(path or "").strip().strip('"\'').replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    if (not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw)
            or raw == ".." or raw.startswith("../") or "/../" in f"/{raw}/"):
        raise AntigravityFirstPassError(f"首版补丁包含不安全路径：{path}")
    normalized = os.path.normpath(raw).replace("\\", "/")
    if normalized in {".", ".."} or normalized.startswith("../"):
        raise AntigravityFirstPassError(f"首版补丁包含不安全路径：{path}")
    return normalized


def path_allowed(path, allowed_paths):
    path = normalize_relative(path)
    for raw in allowed_paths or ():
        allowed = normalize_relative(raw).rstrip("/")
        if path == allowed or path.startswith(allowed + "/"):
            return True
    return False


def _referenced_paths(plan):
    values = [
        plan.get("goal"), plan.get("requirements"), plan.get("architecture"),
        plan.get("research"), plan.get("impact_and_risks"),
    ]
    paths = []
    for value in values:
        for match in PATH_PATTERN.findall(str(value or "")):
            try:
                path = normalize_relative(match.strip(" `，。；：:;()[]{}"))
            except AntigravityFirstPassError:
                continue
            if path not in paths:
                paths.append(path)
    return paths


def build_source_context(stage_root, plan, allowed_paths, max_chars=MAX_CONTEXT_CHARS):
    """Return bounded source text for files frozen in or referenced by the plan."""
    stage_root = os.path.abspath(stage_root)
    candidates = []
    for raw in list(allowed_paths or ()) + _referenced_paths(plan):
        try:
            path = normalize_relative(raw)
        except AntigravityFirstPassError:
            continue
        if path not in candidates and (path_allowed(path, allowed_paths) or path in allowed_paths):
            candidates.append(path)
    if not candidates:
        raise AntigravityFirstPassError("预审方案没有冻结任何可写文件，拒绝生成首版代码")

    sections, used = [], 0
    for relative in candidates:
        target = os.path.realpath(os.path.join(stage_root, relative.replace("/", os.sep)))
        if os.path.commonpath([stage_root, target]) != stage_root:
            continue
        extension = os.path.splitext(relative)[1].lower()
        if os.path.isfile(target) and extension in TEXT_EXTENSIONS:
            try:
                with open(target, encoding="utf-8", errors="replace") as handle:
                    content = handle.read(MAX_FILE_CHARS + 1)
            except OSError:
                continue
            if len(content) > MAX_FILE_CHARS:
                content = content[:MAX_FILE_CHARS] + "\n[文件已截断，未提供部分不得猜测修改]\n"
            section = f"\n===== FILE: {relative} =====\n{content}\n===== END FILE =====\n"
        elif not os.path.exists(target):
            section = f"\n===== NEW FILE ALLOWED: {relative} =====\n"
        else:
            continue
        if used + len(section) > max_chars:
            break
        sections.append(section)
        used += len(section)
    if not sections:
        raise AntigravityFirstPassError("冻结范围内没有可安全提供给反重力的文本文件上下文")
    return "".join(sections), candidates


def extract_unified_diff(text):
    text = str(text or "").replace("\r\n", "\n")
    if len(text) > MAX_PATCH_CHARS:
        raise AntigravityFirstPassError("反重力首版补丁超过大小上限")
    start = text.find("diff --git ")
    if start < 0:
        raise AntigravityFirstPassError("反重力没有返回可应用的 unified diff")
    patch = text[start:]
    for marker in ("\nEND_PATCH", "\n```"):
        position = patch.find(marker)
        if position >= 0:
            patch = patch[:position]
    patch = patch.rstrip() + "\n"
    if "GIT binary patch" in patch or "Binary files " in patch or "120000" in patch:
        raise AntigravityFirstPassError("首版补丁禁止二进制文件或符号链接")
    return patch


def validate_patch_paths(patch, allowed_paths):
    if not allowed_paths:
        raise AntigravityFirstPassError("预审方案缺少冻结的可写范围")
    changed = []
    for line in patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        try:
            parts = shlex.split(line, posix=True)
        except ValueError as exc:
            raise AntigravityFirstPassError(f"无法解析首版补丁路径：{exc}") from exc
        if len(parts) != 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/"):
            raise AntigravityFirstPassError("首版补丁必须使用标准 diff --git a/... b/... 格式")
        before, after = normalize_relative(parts[2][2:]), normalize_relative(parts[3][2:])
        if before != after:
            raise AntigravityFirstPassError("首版补丁暂不允许重命名文件")
        if before in CONTROL_PATHS or before.startswith(".git/"):
            raise AntigravityFirstPassError(f"首版补丁试图修改受保护控制文件：{before}")
        if not path_allowed(before, allowed_paths):
            raise AntigravityFirstPassError(f"首版补丁超出批准范围：{before}")
        if before not in changed:
            changed.append(before)
    if not changed:
        raise AntigravityFirstPassError("首版补丁没有声明任何文件变更")
    return changed


def apply_validated_patch(stage_root, patch, allowed_paths):
    stage_root = os.path.abspath(stage_root)
    changed = validate_patch_paths(patch, allowed_paths)
    command = ["git", "apply", "--no-index", "--whitespace=nowarn", "-"]
    git_env = dict(os.environ)
    # Pytest staging may live under the source repository.  Never let git discover
    # and target that parent repository; production staging is external as well.
    git_env["GIT_CEILING_DIRECTORIES"] = os.path.dirname(stage_root)
    check = subprocess.run(
        command[:2] + ["--check"] + command[2:], cwd=stage_root, input=patch,
        capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False,
        env=git_env,
    )
    if check.returncode != 0:
        detail = (check.stderr or check.stdout or "git apply --check failed")[-1000:]
        raise AntigravityFirstPassError(f"反重力首版补丁无法安全应用：{detail}")
    applied = subprocess.run(
        command, cwd=stage_root, input=patch, capture_output=True, text=True,
        encoding="utf-8", errors="replace", shell=False, env=git_env,
    )
    if applied.returncode != 0:
        detail = (applied.stderr or applied.stdout or "git apply failed")[-1000:]
        raise AntigravityFirstPassError(f"反重力首版补丁应用失败：{detail}")
    return changed
