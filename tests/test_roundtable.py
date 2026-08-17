# -*- coding: utf-8 -*-
"""圆桌引擎纯函数单测 —— 立场提取 / 分歧度 / 收敛判定 / 重要议题判定
运行：cd /mnt/e/feishu-agent && python3 -m pytest tests/test_roundtable.py -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import roundtable_engine as re_


# ---------------------------------------------------------------- extract_stance
def test_stance_format_agree():
    assert re_.extract_stance("立场：同意\n方案可行") == "同意"

def test_stance_format_agree_synonym():
    assert re_.extract_stance("【立场】：赞同\n继续推进") == "同意"

def test_stance_bold_format():
    assert re_.extract_stance("**立场**：反对\n这方案有风险") == "反对"

def test_stance_heuristic_disagree():
    assert re_.extract_stance("我不认同这个方案，成本太高") == "反对"

def test_stance_heuristic_refuse():
    assert re_.extract_stance("否决该方案") == "反对"

def test_stance_bugfix_not_disagree():
    """回归：'不反对' 含 '反对' 子串，必须判为同意而非反对"""
    assert re_.extract_stance("我不反对这个方案，可以先试点") == "同意"
    assert re_.extract_stance("无异议，按你说的办") == "同意"

def test_stance_supplement():
    assert re_.extract_stance("补充一点：需要加超时重试") == "补充"
    assert re_.extract_stance("建议增加监控告警") == "补充"

def test_stance_abstain():
    assert re_.extract_stance("弃权，这个领域我不熟") == "弃权"

def test_stance_neutral():
    assert re_.extract_stance("今天天气不错，讨论一下午饭") == "中立"

def test_stance_late_keyword_ignored():
    """关键词出现在 120 字之后不应触发启发式（只认开头表态）"""
    text = "先说背景。" * 40 + "最后补充一点：要加日志"  # 补充出现在 120 字后
    assert re_.extract_stance(text) == "中立"

def test_stance_agree_and_supplement():
    assert re_.extract_stance("同意并补充：再加个缓存层") == "同意"


# ---------------------------------------------------------------- divergence
def test_divergence_first_round():
    assert re_.divergence({"pm": "同意", "arch": "补充"}, None) == 2

def test_divergence_no_change():
    prev = {"pm": "同意", "arch": "同意"}
    now = {"pm": "同意", "arch": "同意"}
    assert re_.divergence(now, prev) == 0

def test_divergence_one_change():
    prev = {"pm": "同意", "arch": "补充"}
    now = {"pm": "同意", "arch": "同意"}
    assert re_.divergence(now, prev) == 1

def test_divergence_all_change():
    prev = {"pm": "同意", "arch": "同意"}
    now = {"pm": "反对", "arch": "弃权"}
    assert re_.divergence(now, prev) == 2


# ---------------------------------------------------------------- topic_is_important
def test_topic_important_paper():
    assert re_.topic_is_important("论文返修方案讨论") is True

def test_topic_important_c3():
    assert re_.topic_is_important("C3 比赛新模块选型") is True

def test_topic_important_arch():
    assert re_.topic_is_important("系统架构设计评审") is True

def test_topic_not_important():
    assert re_.topic_is_important("中午吃什么") is False


# ---------------------------------------------------------------- meeting_converged
def test_round1_never_converges():
    assert re_.meeting_converged(1, {"pm": "同意", "arch": "同意"}, None) == (False, "running")

def test_consensus_all_agree():
    prev = {"pm": "补充", "arch": "补充"}
    now = {"pm": "同意", "arch": "同意"}
    assert re_.meeting_converged(2, now, prev) == (True, "consensus")

def test_no_consensus_with_dissent():
    prev = {"pm": "同意", "arch": "补充"}
    now = {"pm": "同意", "arch": "反对"}
    assert re_.meeting_converged(2, now, prev) == (False, "running")

def test_no_consensus_neutral():
    prev = {"pm": "同意", "arch": "补充"}
    now = {"pm": "同意", "arch": "中立"}
    assert re_.meeting_converged(2, now, prev) == (False, "running")

def test_no_consensus_supplement_only():
    """补充+补充 ≠ 共识（收敛收紧规则）"""
    prev = {"pm": "补充", "arch": "同意"}
    now = {"pm": "补充", "arch": "补充"}
    assert re_.meeting_converged(2, now, prev) == (False, "running")

def test_fixed_point():
    """立场固定点：无反对但也没有人明确「同意」（补充+补充），与上轮完全一致 → 固定点收敛"""
    prev = {"pm": "补充", "arch": "补充"}
    now = {"pm": "补充", "arch": "补充"}
    assert re_.meeting_converged(3, now, prev) == (True, "fixed_point")

def test_fixed_point_consensus_precedence():
    """共识优先：同时满足共识和固定点时，判 consensus（引擎现有行为）"""
    prev = {"pm": "同意", "arch": "补充"}
    now = {"pm": "同意", "arch": "补充"}
    assert re_.meeting_converged(3, now, prev) == (True, "consensus")

def test_fixed_point_not_on_round2_first_change():
    """第 2 轮立场变化（补充→反对）不是固定点，且有反对也不构成共识"""
    prev = {"pm": "补充", "arch": "补充"}
    now = {"pm": "补充", "arch": "反对"}
    assert re_.meeting_converged(2, now, prev) == (False, "running")


if __name__ == "__main__":
    # 无 pytest 时也能直接跑
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✓ {name}")
            except AssertionError:
                failed += 1
                print(f"  ✗ {name}")
                traceback.print_exc()
    print(f"\n{'全部通过' if failed == 0 else f'{failed} 个失败'}")
    sys.exit(1 if failed else 0)
