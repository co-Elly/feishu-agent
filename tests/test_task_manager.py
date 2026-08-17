import os
import threading
import time

import task_manager


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(task_manager, "WORK_ROOT", str(tmp_path / "work"))
    return task_manager.TaskStore()


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
    task = store.create("swarm", "chat", "user", "msg", {"goal": "测试"})
    assert store.request_cancel(task["id"])
    assert store.get(task["id"])["status"] == "cancelled"
    assert not store.claim(task["id"])


def test_running_cancel_is_cooperative(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    task = store.create("swarm", "chat", "user", "msg", {"goal": "测试"})
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


def test_restart_fails_non_idempotent_swarm_task(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    task = store.create("swarm", "chat", "user", "msg", {"goal": "有副作用"})
    assert store.claim(task["id"])
    assert store.recover() == []
    recovered = store.get(task["id"])
    assert recovered["status"] == "failed"
    assert "use retry" in recovered["error"]


def test_retry_clones_terminal_task(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    task = store.create("roundtable", "chat", "user", "old-msg", {"topic": "重试"})
    assert store.claim(task["id"])
    store.finish(task["id"], "failed", error="boom")
    retry = store.retry(task["id"], message_id="new-msg")
    assert retry["retry_of"] == task["id"]
    assert retry["attempt"] == 2
    assert retry["message_id"] == "new-msg"


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
