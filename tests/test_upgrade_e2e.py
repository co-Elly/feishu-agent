"""Cross-module acceptance for the painless-upgrade control plane."""

import os
import sys

import task_manager
from constraint_envelope import build_constraint_envelope
from isolated_workspace import IsolatedWorkspace
from workflow_store import WorkflowStore
from workspace_lease import WorkspaceLease


def test_meeting_to_approved_isolated_merge_end_to_end(tmp_path, monkeypatch):
    database = str(tmp_path / "control.db")
    monkeypatch.setattr(task_manager, "DB_PATH", database)
    monkeypatch.setattr(task_manager, "WORK_ROOT", str(tmp_path / "tasks"))
    tasks = task_manager.TaskStore()
    workflows = WorkflowStore(database)
    envelope = build_constraint_envelope("仅新增 RESULT.md，禁止修改其他文件")

    workflow = workflows.create("chat", "boss", "终验", envelope["root_request"], envelope)
    meeting = tasks.create("roundtable", "chat", "boss", "m1", {
        "topic": envelope["root_request"], "workflow_id": workflow["id"],
        "constraint_envelope": envelope,
    })
    workflows.bind_task(workflow["id"], meeting["id"])
    workflows.transition(workflow["id"], "awaiting_boss_decision")
    workflows.transition(workflow["id"], "meeting_continuation")
    workflows.transition(workflow["id"], "awaiting_collaboration_confirmation")

    swarm = tasks.create("swarm", "chat", "boss", "m2", {
        "goal": envelope["root_request"], "workflow_id": workflow["id"],
        "meeting_task_id": meeting["id"],
        "collaboration_confirmation": {
            "workflow_id": workflow["id"], "meeting_task_id": meeting["id"],
            "confirmed_by": "boss", "confirmation_message_id": "m2",
            "confirmed_at": 1,
        },
        "constraint_envelope": envelope,
    })
    workflows.transition(workflow["id"], "planning", swarm["id"])
    assert tasks.claim(swarm["id"])
    plan = {"project_name": "终验", "constraint_envelope": envelope,
            "approved_scope": {"allowed_paths": ["RESULT.md"]}}
    assert tasks.wait_for_approval(swarm["id"], plan)
    workflows.transition(workflow["id"], "waiting_approval")
    assert tasks.approve(swarm["id"], approved_by="boss", approval_message_id="approve-1") == "approved"
    assert task_manager.approval_receipt_valid(tasks.get(swarm["id"]))

    source = tmp_path / "source"
    source.mkdir()
    (source / "config.example.json").write_text("{}", encoding="utf-8")
    isolated = IsolatedWorkspace(str(source), str(tmp_path / "executions"), swarm["id"], sys.executable).prepare()
    before = isolated.snapshot()
    open(os.path.join(isolated.root, "RESULT.md"), "w", encoding="utf-8").write("accepted")
    changed = isolated.changed(before, isolated.snapshot())
    assert changed == ["RESULT.md"]
    with WorkspaceLease(swarm["id"], db_path=database):
        isolated.merge(changed)
        isolated.commit()
    assert (source / "RESULT.md").read_text(encoding="utf-8") == "accepted"
    workflows.transition(workflow["id"], "executing")
    workflows.transition(workflow["id"], "verifying")
    assert workflows.transition(workflow["id"], "completed")["state"] == "completed"
