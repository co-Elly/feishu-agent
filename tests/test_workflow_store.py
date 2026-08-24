import pytest

from workflow_store import WorkflowStore, WorkflowTransitionError


def test_meeting_workflow_persists_and_enforces_transitions(tmp_path):
    store = WorkflowStore(str(tmp_path / "workflow.db"))
    workflow = store.create("chat", "owner", "项目", "议题", {"version": 1})
    assert workflow["state"] == "meeting_discussion"
    store.bind_task(workflow["id"], "meeting-1")
    store.transition(workflow["id"], "awaiting_boss_decision")
    continued = store.transition(workflow["id"], "meeting_continuation", "meeting-2")
    assert continued["decision_round"] == 1
    store.transition(workflow["id"], "awaiting_collaboration_confirmation")
    updated = store.update_ledgers(workflow["id"],
        task_ledger={"open_action": "collaboration_confirmation"},
        progress_ledger={"round": 2, "making_progress": False})
    assert updated["task_ledger"]["open_action"] == "collaboration_confirmation"
    assert updated["progress_ledger"]["round"] == 2
    assert store.active("chat", "项目")["state"] == "awaiting_collaboration_confirmation"


def test_workflow_rejects_skipping_human_decision(tmp_path):
    store = WorkflowStore(str(tmp_path / "workflow.db"))
    workflow = store.create("chat", "owner", None, "议题", {"version": 1})
    with pytest.raises(WorkflowTransitionError):
        store.transition(workflow["id"], "planning")


def test_boss_decisions_are_appended_before_optional_direct_confirmation(tmp_path):
    store = WorkflowStore(str(tmp_path / "workflow.db"))
    workflow = store.create("chat", "owner", "project", "topic", {"version": 1})
    store.transition(workflow["id"], "awaiting_boss_decision")
    store.record_boss_decision(
        workflow["id"], "按方案定稿", user_id="owner", message_id="m1",
        meeting_task_id="meeting-1",
    )
    updated = store.transition(workflow["id"], "awaiting_collaboration_confirmation")
    assert updated["task_ledger"]["boss_decisions"] == [{
        "sequence": 1, "decision": "按方案定稿", "user_id": "owner",
        "message_id": "m1", "meeting_task_id": "meeting-1",
        "created_at": updated["task_ledger"]["boss_decisions"][0]["created_at"],
    }]


def test_task_failure_synchronizes_active_workflow_terminal_state(tmp_path):
    store = WorkflowStore(str(tmp_path / "workflow.db"))
    workflow = store.create("chat", "owner", "project", "topic", {"version": 1})
    store.transition(workflow["id"], "awaiting_boss_decision")
    store.transition(workflow["id"], "meeting_continuation")
    store.transition(workflow["id"], "awaiting_collaboration_confirmation")
    store.transition(workflow["id"], "planning")
    store.transition(workflow["id"], "waiting_approval")
    store.transition(workflow["id"], "executing", "task-1")

    failed = store.finish_from_task(workflow["id"], "failed", "task-1")

    assert failed["state"] == "failed"
    assert failed["current_task_id"] == "task-1"
    assert store.active("chat", "project") is None

    resumed = store.resume_for_retry(workflow["id"], "waiting_approval", "task-2", "task-1")
    assert resumed["state"] == "waiting_approval"
    assert resumed["current_task_id"] == "task-2"
    assert resumed["task_ledger"]["retries"][0]["retry_of"] == "task-1"
