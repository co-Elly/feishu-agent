# -*- coding: utf-8 -*-
"""圆桌引擎纯函数单测 —— 立场提取 / 分歧度 / 收敛判定 / 重要议题判定
运行：cd /mnt/e/feishu-agent && python3 -m pytest tests/test_roundtable.py -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import roundtable_engine as re_


def test_meeting_budget_scales_team_and_rounds_by_task_load():
    assert re_.meeting_budget("讨论欢迎语文案")["agents"] == ["pm", "dev"]
    assert re_.meeting_budget("讨论欢迎语文案")["max_rounds"] == 2
    high = re_.meeting_budget("设计数据库迁移、接口兼容和全量测试架构")
    assert high["agents"] == ["pm", "arch", "dev"]
    assert high["max_rounds"] == 4


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


# ---------------- P0-2 同议题去重 ----------------

@pytest.fixture()
def _rt_db(tmp_path, monkeypatch):
    """把 roundtable DB/黑板目录指到临时路径（只改模块全局，不重载模块，
    避免污染其他测试看到的 DB_PATH）。"""
    rt = tmp_path / "roundtable"
    db = str(rt / "roundtable.db")
    monkeypatch.setattr(re_, "RT_ROOT", str(rt))
    monkeypatch.setattr(re_, "DB_PATH", db)
    yield re_


def test_normalize_topic_strips_whitespace():
    assert re_.normalize_topic("  讨论   圆桌引擎  ") == "讨论 圆桌引擎"
    assert re_.normalize_topic("") == ""
    assert re_.normalize_topic(None) == ""


def test_normalize_topic_fullwidth_space():
    assert re_.normalize_topic("议题\u3000\u3000A") == "议题 A"


def test_find_recent_session_dedups_same_topic(_rt_db):
    r = _rt_db
    sid = r.create_session("讨论 圆桌引擎升级")
    hit = r.find_recent_session("  讨论   圆桌引擎升级  ")  # 空白差异不影响命中
    assert hit is not None and hit["id"] == sid


def test_find_recent_session_ignores_different_topic(_rt_db):
    r = _rt_db
    r.create_session("议题甲")
    assert r.find_recent_session("议题乙") is None


def test_find_recent_session_ignores_cancelled(_rt_db):
    import sqlite3 as _sq
    from roundtable_engine import DB_PATH as _db
    r = _rt_db
    sid = r.create_session("旧议题")
    conn = _sq.connect(_db)
    conn.execute("UPDATE sessions SET status='cancelled' WHERE id=?", (sid,))
    conn.commit(); conn.close()
    assert r.find_recent_session("旧议题") is None


def test_find_recent_session_window_expiry(_rt_db, monkeypatch):
    import sqlite3 as _sq
    from roundtable_engine import DB_PATH as _db
    r = _rt_db
    sid = r.create_session("过期议题")
    old = "2000-01-01 00:00:00"
    conn = _sq.connect(_db)
    conn.execute("UPDATE sessions SET created_at=? WHERE id=?", (old, sid))
    conn.commit(); conn.close()
    assert r.find_recent_session("过期议题") is None


def test_dedup_result_reads_minutes(_rt_db):
    import os as _os
    from roundtable_engine import session_dir as _sd
    r = _rt_db
    sid = r.create_session("有纪要的议题")
    _os.makedirs(_sd(sid), exist_ok=True)
    with open(_os.path.join(_sd(sid), "minutes.md"), "w", encoding="utf-8") as f:
        f.write("# 纪要\n\n## 最终总结\n共识是好的。\n\n## 完整记录\n略\n")
    row = r.find_recent_session("有纪要的议题")
    res = r.roundtable_v2._dedup_result(row) if hasattr(r.roundtable_v2, "_dedup_result") else r.RoundTableV2()._dedup_result(row)
    assert res["deduplicated"] is True
    assert "共识是好的" in res["final_summary"]


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
