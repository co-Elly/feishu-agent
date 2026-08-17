import pytest

import roundtable_engine as rt


def _engine(tmp_path, monkeypatch):
    root = tmp_path / "roundtable"
    monkeypatch.setattr(rt, "RT_ROOT", str(root))
    monkeypatch.setattr(rt, "DB_PATH", str(root / "roundtable.db"))
    monkeypatch.setattr(rt, "research_topic", lambda topic: "")
    rt._ENGINE_COOLDOWNS.clear()
    return rt.RoundTableV2()


def test_quota_reset_duration_is_parsed():
    assert rt._cooldown_seconds("Resets in 1h40m58s") == 6068
    assert rt.is_engine_error("反重力报错: quota")
    assert rt.is_engine_error("Codex 调用失败: 401 Unauthorized")


def test_failed_agent_is_removed_after_first_round(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    calls = []

    def speak(agent_key, *args, **kwargs):
        calls.append(agent_key)
        if agent_key == "arch":
            return "反重力报错: Individual quota reached. Resets in 1h"
        return "立场：同意\n方案可行"

    monkeypatch.setattr(engine, "_speak", speak)
    monkeypatch.setattr(engine, "_summarize", lambda *args, **kwargs: "总结")
    result = engine.run("普通测试")
    assert result["rounds_used"] == 2
    assert calls.count("arch") == 1
    assert calls.count("pm") == 2
    assert calls.count("dev") == 2
    assert "📐 反重力·架构师" in result["unavailable_agents"]


def test_insufficient_quorum_fails_after_first_round(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    calls = []

    def speak(agent_key, *args, **kwargs):
        calls.append(agent_key)
        if agent_key == "pm":
            return "立场：同意\n继续"
        return "Codex 调用失败: 401" if agent_key == "dev" else "反重力报错: quota"

    monkeypatch.setattr(engine, "_speak", speak)
    with pytest.raises(rt.RoundTableQuorumError):
        engine.run("法定人数测试")
    assert sorted(calls) == ["arch", "dev", "pm"]


def test_research_runs_before_current_session_is_created(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    order = []
    original_create = rt.create_session

    monkeypatch.setattr(rt, "research_topic", lambda topic: order.append("research") or "")

    def create(topic):
        order.append("create")
        return original_create(topic)

    monkeypatch.setattr(rt, "create_session", create)
    monkeypatch.setattr(engine, "_speak", lambda *args, **kwargs: "立场：同意\n方案可行")
    monkeypatch.setattr(engine, "_summarize", lambda *args, **kwargs: "总结")
    engine.run("预研顺序测试")
    assert order[:2] == ["research", "create"]


def test_memory_is_scoped_to_current_session(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    current = tmp_path / "roundtable" / "current" / "memories"
    old = tmp_path / "roundtable" / "old" / "memories"
    current.mkdir(parents=True)
    old.mkdir(parents=True)
    (current / "arch.md").write_text("本场发言", encoding="utf-8")
    (old / "arch.md").write_text("无关旧会议", encoding="utf-8")
    assert engine._read_memory("current", "arch") == "本场发言"


def test_minutes_turn_label_does_not_corrupt_agent_key(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    session_id = "minutes-test"
    rt.ensure_dir(rt.session_dir(session_id))
    import swarm_orchestrator
    monkeypatch.setattr(swarm_orchestrator, "call_llm", lambda *args, **kwargs: "总结")

    engine._summarize(session_id, "标签测试", [{
        "turn": "r1-arch", "agent": "arch", "stance": "同意", "text": "可行",
    }])
    minutes = (tmp_path / "roundtable" / session_id / "minutes.md").read_text(encoding="utf-8")
    assert "第1轮 · 立场" in minutes
    assert "a第ch" not in minutes
