import os
import threading
import time

import task_manager
import pytest
from workspace_lease import WorkspaceLease


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(task_manager, "WORK_ROOT", str(tmp_path / "work"))
    return task_manager.TaskStore()


def _swarm_payload(goal="测试", **extra):
    payload = {
        "goal": goal, "workflow_id": "workflow-1", "meeting_task_id": "meeting-1",
        "collaboration_confirmation": {
            "workflow_id": "workflow-1", "meeting_task_id": "meeting-1",
            "confirmed_by": "user", "confirmation_message_id": "confirm-1",
            "confirmed_at": 1,
        },
    }
    payload.update(extra)
    return payload


def test_task_lifecycle_and_snapshot(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    task = store.create("roundtable", "chat", "user", "msg", {"topic": "测试"})
    assert task["status"] == "queued"
    assert os.path.exists(os.path.join(task["work_dir"], "task.json"))
    assert store.claim(task["id"])
    store.progress(task["id"], "第一轮")
    store.finish(task["id"], "succeeded", {"ok": True})
    finished = store.get(task["id"])
    assert finished["status"] == "succeeded"
    assert finished["result"] == {"ok": True}


def test_queued_cancel_is_terminal(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    task = store.create("swarm", "chat", "user", "msg", _swarm_payload())
    assert store.request_cancel(task["id"])
    assert store.get(task["id"])["status"] == "cancelled"
    assert not store.claim(task["id"])


def test_running_cancel_is_cooperative(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    task = store.create("swarm", "chat", "user", "msg", _swarm_payload())
    assert store.claim(task["id"])
    assert store.request_cancel(task["id"])
    context = task_manager.TaskContext(store, task["id"])
    try:
        context.check_cancelled()
        assert False, "expected TaskCancelled"
    except task_manager.TaskCancelled:
        pass


def test_restart_requeues_running_task(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    task = store.create("roundtable", "chat", "user", "msg", {"topic": "恢复"})
    assert store.claim(task["id"])
    assert store.recover() == [task["id"]]
    recovered = store.get(task["id"])
    assert recovered["status"] == "queued"
    assert recovered["progress"] == "服务重启后恢复排队"


def test_restart_replays_swarm_planning_task(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    task = store.create("swarm", "chat", "user", "msg", _swarm_payload("有副作用"))
    assert store.claim(task["id"])
    assert store.recover() == [task["id"]]
    recovered = store.get(task["id"])
    assert recovered["status"] == "queued"


def test_restart_blocks_approved_write_and_releases_its_lease(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    task = store.create("swarm", "chat", "user", "msg", _swarm_payload("有副作用"))
    assert store.claim(task["id"])
    assert store.wait_for_approval(task["id"], {"project_name": "p"})
    assert store.approve(task["id"]) == "approved"
    assert store.claim(task["id"])
    WorkspaceLease(task["id"], db_path=task_manager.DB_PATH).acquire()
    assert store.recover() == []
    recovered = store.get(task["id"])
    assert recovered["status"] == "blocked"
    assert "automatic replay disabled" in recovered["error"]
    with WorkspaceLease("next-task", db_path=task_manager.DB_PATH):
        pass


def test_restart_idempotently_requeues_approved_read_only_report(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    task = store.create("swarm", "chat", "user", "msg", _swarm_payload(
        "只读报告", operation_mode="read_only_report",
    ))
    assert store.claim(task["id"])
    assert store.wait_for_approval(task["id"], {"project_name": "report"})
    assert store.approve(task["id"]) == "approved"
    assert store.claim(task["id"])
    WorkspaceLease(task["id"], db_path=task_manager.DB_PATH).acquire()

    assert store.recover() == [task["id"]]
    recovered = store.get(task["id"])
    assert recovered["status"] == "queued"
    assert "read-only report will resume idempotently" in recovered["error"]
    with WorkspaceLease("next-task", db_path=task_manager.DB_PATH):
        pass


def test_task_checkpoints_are_idempotent_and_auditable(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    task = store.create("swarm", "chat", "user", "m", _swarm_payload("g"))
    store.checkpoint(task["id"], "planning_started")
    store.checkpoint(task["id"], "planning_started", {"retry": True})
    checkpoints = store.checkpoints(task["id"])
    assert checkpoints == [{"checkpoint": "planning_started", "details": {"retry": True},
                            "created_at": checkpoints[0]["created_at"]}]


def test_retry_clones_terminal_task(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    task = store.create("roundtable", "chat", "user", "old-msg", {"topic": "重试"})
    assert store.claim(task["id"])
    store.finish(task["id"], "failed", error="boom")
    retry = store.retry(task["id"], message_id="new-msg")
    assert retry["retry_of"] == task["id"]
    assert retry["attempt"] == 2
    assert retry["message_id"] == "new-msg"


def test_retry_of_approved_swarm_reuses_plan_and_enters_execute(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    task = store.create("swarm", "chat", "user", "old-msg", _swarm_payload("重试写入"))
    assert store.claim(task["id"])
    plan = {"project_name": "p", "architecture": "approved"}
    assert store.wait_for_approval(task["id"], plan)
    assert store.approve(task["id"]) == "approved"
    assert store.claim(task["id"])
    store.finish(task["id"], "failed", error="agent failed")

    retry = store.retry(task["id"], message_id="retry-msg")

    assert retry["phase"] == "execute"
    assert retry["plan"] == plan
    assert retry["approved_at"] is not None
    assert retry["status"] == "queued"


def test_approved_swarm_cannot_retry_without_new_user_message(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    task = store.create("swarm", "chat", "user", "old-msg", _swarm_payload("重试写入"))
    assert store.claim(task["id"])
    assert store.wait_for_approval(task["id"], {"project_name": "p"})
    assert store.approve(task["id"]) == "approved"
    assert store.claim(task["id"])
    store.finish(task["id"], "failed", error="agent failed")

    assert store.retry(task["id"], message_id=None) is None


def test_default_list_hides_only_successful_chat_tasks(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    chat = store.create("chat", "chat", "user", "m1", {"prompt": "你好"})
    swarm = store.create("swarm", "chat", "user", "m2", _swarm_payload("改造"))
    assert store.claim(chat["id"])
    store.finish(chat["id"], "succeeded", result={})
    assert store.claim(swarm["id"])
    store.finish(swarm["id"], "succeeded", result={})

    assert [task["id"] for task in store.list("chat")] == [swarm["id"]]
    assert {task["id"] for task in store.list("chat", include_all=True)} == {chat["id"], swarm["id"]}


def test_controller_executes_and_finishes(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    done = threading.Event()

    def runner(task, context):
        context.progress("working")
        done.set()
        return {"value": 1}

    controller = task_manager.TaskController(max_workers=1, store=store)
    controller.start(runner)
    task = controller.submit("roundtable", "chat", "user", "msg", {"topic": "执行"})
    assert done.wait(2)
    for _ in range(20):
        current = store.get(task["id"])
        if current["status"] == "succeeded":
            break
        time.sleep(0.02)
    assert current["result"] == {"value": 1}
    controller.shutdown()


def test_controller_refuses_swarm_without_meeting_confirmation(tmp_path, monkeypatch):
    controller = task_manager.TaskController(max_workers=1, store=_store(tmp_path, monkeypatch))
    with pytest.raises(ValueError, match="明确开始协作确认"):
        controller.submit("swarm", "chat", "user", "msg", {"goal": "绕过会议"})
    controller.shutdown()


def test_controller_records_needs_review_and_blocked(tmp_path, monkeypatch):
    for exception, expected in ((task_manager.TaskNeedsReview("semantic"), "needs_review"),
                                (task_manager.TaskBlocked("environment"), "blocked")):
        store = _store(tmp_path / expected, monkeypatch)
        done = threading.Event()
        def runner(_task, _context, exc=exception):
            done.set()
            raise exc
        controller = task_manager.TaskController(max_workers=1, store=store)
        controller.start(runner)
        task = controller.submit("swarm", expected, "user", "msg", _swarm_payload("test"))
        assert done.wait(2)
        for _ in range(50):
            current = store.get(task["id"])
            if current["status"] == expected:
                break
            time.sleep(0.02)
        assert current["status"] == expected
        controller.shutdown()
