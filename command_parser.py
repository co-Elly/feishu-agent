"""Pure parsing helpers for Feishu commands and explicit project labels."""

import re


PROJECT_TAG = re.compile(r"\[项目[:：]\s*([^\]]+?)\s*\]")
INVALID_PROJECT_PARTS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def validate_project_name(project):
    """Return a display-safe project name that cannot escape a storage root."""
    project = str(project or "").strip()
    if not project or project in {".", ".."} or len(project) > 80:
        raise ValueError("项目名不能为空、不能是路径标记，且最多 80 个字符")
    if INVALID_PROJECT_PARTS.search(project) or ".." in project:
        raise ValueError("项目名不能包含路径、盘符、控制字符或 ..")
    if project.rstrip(" .") != project:
        raise ValueError("项目名不能以空格或句点结尾")
    if project.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        raise ValueError("项目名不能使用 Windows 保留设备名")
    return project


def extract_project_tag(text):
    match = PROJECT_TAG.search(text or "")
    if not match:
        return (text or "").strip(), None
    project = validate_project_name(match.group(1))
    cleaned = (text[:match.start()] + text[match.end():]).strip()
    return re.sub(r"\s{2,}", " ", cleaned), project


def parse_memory_command(text):
    clean = (text or "").strip()
    if clean.startswith("记住 "):
        content, project = extract_project_tag(clean[3:].strip())
        return {"action": "remember", "content": content, "project": project}
    if clean.startswith("记忆列表"):
        remainder, project = extract_project_tag(clean[4:].strip())
        if remainder:
            return None
        return {"action": "list_memories", "project": project}
    match = re.fullmatch(r"忘记\s+([0-9a-fA-F]{4,8})", clean)
    if match:
        return {"action": "forget", "id": match.group(1).lower()}
    return None
