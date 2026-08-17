from types import SimpleNamespace

import swarm_orchestrator


def _result(text):
    return SimpleNamespace(ok=True, text=text)


def test_approved_pipeline_runs_antigravity_then_codex_then_hermes(monkeypatch):
    order = []

    monkeypatch.setattr(swarm_orchestrator, "call_antigravity",
                        lambda *args, **kwargs: order.append("antigravity") or _result("第一版完成"))
    monkeypatch.setattr(swarm_orchestrator, "call_codex",
                        lambda *args, **kwargs: order.append("codex") or _result("收尾和测试完成"))
    monkeypatch.setattr(swarm_orchestrator, "call_hermes",
                        lambda *args, **kwargs: order.append("hermes") or _result("验收：通过\n证据齐全"))

    class Bridge:
        def init_project(self, *args): pass
        def write_architecture(self, *args): pass
        def write_code_test(self, *args): pass
        def append_decision_log(self, *args): pass

    orchestrator = swarm_orchestrator.MultiAgentSwarm(bridge=Bridge())
    result = orchestrator.execute_collaborative_project({
        "project_name": "测试项目", "goal": "完成改造", "requirements": "验收标准",
        "architecture": "批准架构", "research": "仓库调查",
    })

    assert result["success"] is True
    assert order == ["antigravity", "codex", "hermes"]


def test_read_only_plan_uses_three_real_agent_channels(monkeypatch):
    order = []

    monkeypatch.setattr(swarm_orchestrator, "call_hermes",
                        lambda *args, **kwargs: order.append("hermes") or _result("验收标准"))
    monkeypatch.setattr(swarm_orchestrator, "call_antigravity",
                        lambda *args, **kwargs: order.append("antigravity") or _result("架构方案"))
    monkeypatch.setattr(swarm_orchestrator, "call_codex",
                        lambda *args, **kwargs: order.append("codex") or _result("仓库调查"))

    plan = swarm_orchestrator.MultiAgentSwarm().plan_collaborative_project(
        "完成改造", project_name="测试项目",
    )

    assert order == ["hermes", "antigravity", "codex"]
    assert plan["requirements"] == "验收标准"
    assert plan["architecture"] == "架构方案"
    assert plan["research"] == "仓库调查"
