"""Pure parsing helpers for Feishu commands and explicit project labels."""

import re


PROJECT_TAG = re.compile(r"\[项目[:：]\s*([^\]]+?)\s*\]")


def extract_project_tag(text):
    match = PROJECT_TAG.search(text or "")
    if not match:
        return (text or "").strip(), None
    project = match.group(1).strip()
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
