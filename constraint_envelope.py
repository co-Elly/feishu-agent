"""System-owned immutable constraints carried across every task phase."""

import re


PATH_PATTERN = re.compile(
    r"(?i)(?<![\w.-])([\w一-鿿./\\-]+\.(?:md|py|json|ya?ml|toml|txt|ps1|sql))"
)
HARD_MARKERS = ("禁止", "不得", "不允许", "仅", "只", "唯一允许", "必须")
SCOPE_PATTERN = re.compile(
    r"禁止(?:修改|改动|新增)?其他(?:业务)?文件|"
    r"唯一允许的?(?:业务)?产物|"
    r"仅(?:允许)?(?:新增|修改|改动|包含)|"
    r"只(?:允许)?(?:新增|修改|改动|包含)"
)


class ConstraintEnvelopeError(RuntimeError):
    pass


def _sentences(text):
    return [part.strip() for part in re.split(r"[\n。；;]+", text or "") if part.strip()]


def build_constraint_envelope(root_request):
    """Extract an immutable, fail-closed envelope from the user's root request."""
    root_request = str(root_request or "").strip()
    allowed_paths = sorted({
        match.replace("\\", "/").lstrip("./")
        for match in PATH_PATTERN.findall(root_request)
    })
    hard_constraints = [
        sentence for sentence in _sentences(root_request)
        if any(marker in sentence for marker in HARD_MARKERS)
    ]
    scope_restricted = bool(SCOPE_PATTERN.search(root_request))
    if scope_restricted and not allowed_paths:
        raise ConstraintEnvelopeError(
            "原始请求包含文件范围限制，但未能解析出任何允许路径；已拒绝空范围放行"
        )
    return {
        "version": 1,
        "root_request": root_request,
        "hard_constraints": hard_constraints,
        "scope_restricted": scope_restricted,
        "allowed_paths": allowed_paths,
    }


def validate_constraint_envelope(envelope):
    if not isinstance(envelope, dict) or not envelope.get("root_request"):
        raise ConstraintEnvelopeError("缺少系统约束信封或原始用户请求")
    allowed = envelope.get("allowed_paths") or []
    if envelope.get("scope_restricted") and not allowed:
        raise ConstraintEnvelopeError("严格文件范围不得为空")
    return envelope


def envelope_prompt(envelope):
    validate_constraint_envelope(envelope)
    constraints = "\n".join(f"- {item}" for item in envelope.get("hard_constraints") or []) or "- 无额外显式硬约束"
    allowed = "、".join(envelope.get("allowed_paths") or []) or "（未限定文件路径）"
    return (
        "【系统约束信封·全程不可弱化】\n"
        f"原始用户请求：{envelope['root_request']}\n"
        f"硬约束：\n{constraints}\n"
        f"允许文件：{allowed}\n"
        "会议、Agent 建议和后续规划只能在此范围内细化，不得扩大或覆盖。"
    )
