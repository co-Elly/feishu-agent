import threading
import time

import task_manager


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(task_manager, "WORK_ROOT", str(tmp_path / "work"))
    return task_manager.TaskStore()


def _wait(store, task_ids, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        states = [store.get(task_id)["status"] for task_id in task_ids]
        if all(state in task_manager.TERMINAL_STATES for state in states):
            return states
        time.sleep(0.01)
    return states


def test_approval_is_atomic_idempotent_and_requeues_at_tail(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    first = store.create("swarm", "chat", "u", "m", {"goal": "g"})
    assert store.claim(first["id"])
    assert store.wait_for_approval(first["id"], {"project_name": "P"}, ttl_seconds=60)
    second = store.create("chat", "chat", "u", "m2", {"prompt": "next"})
    assert store.approve(first["id"]) == "approved"
    assert store.approve(first["id"]) == "already_approved"
    assert store.next_queued("chat")["id"] == second["id"]


def test_approval_expiry_cancels_task(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    task = store.create("swarm", "chat", "u", "m", {"goal": "g"})
    assert store.claim(task["id"])
    assert store.wait_for_approval(task["id"], {}, ttl_seconds=-1)
    assert store.approve(task["id"]) == "not_waiting"
    assert store.get(task["id"])["status"] == "cancelled"


def test_waiting_approval_survives_restart(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    task = store.create("swarm", "chat", "u", "m", {"goal": "g"})
    assert store.claim(task["id"])
    assert store.wait_for_approval(task["id"], {}, ttl_seconds=60)
    store.recover()
    assert store.get(task["id"])["status"] == "waiting_approval"


def test_same_chat_fifo(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    entered, release, order = threading.Event(), threading.Event(), []

    def runner(task, context):
        order.append(task["payload"]["n"])
        if task["payload"]["n"] == 1:
            entered.set()
            release.wait(2)
        return {}

    controller = task_manager.TaskController(max_workers=2, store=store)
    controller.start(runner)
    first = controller.submit("chat", "same", "u", "m1", {"n": 1})
    assert entered.wait(1)
    second = controller.submit("chat", "same", "u", "m2", {"n": 2})
    time.sleep(0.1)
    assert order == [1]
    release.set()
    assert _wait(store, [first["id"], second["id"]]) == ["succeeded", "succeeded"]
    assert order == [1, 2]


def test_different_chats_run_in_parallel(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    both, release, active, guard = threading.Event(), threading.Event(), set(), threading.Lock()

    def runner(task, context):
        with guard:
            active.add(task["chat_id"])
            if len(active) == 2:
                both.set()
        release.wait(2)
        return {}

    controller = task_manager.TaskController(max_workers=2, store=store)
    controller.start(runner)
    a = controller.submit("chat", "a", "u", "m1", {})
    b = controller.submit("chat", "b", "u", "m2", {})
    assert both.wait(1)
    release.set()
    assert _wait(store, [a["id"], b["id"]]) == ["succeeded", "succeeded"]


def test_global_workspace_write_lock_serializes_writers():
    order, first_inside, release = [], threading.Event(), threading.Event()

    def writer(name):
        with task_manager.WORKSPACE_WRITE_LOCK:
            order.append(name)
            if name == "a":
                first_inside.set()
                release.wait(2)

    a = threading.Thread(target=writer, args=("a",))
    b = threading.Thread(target=writer, args=("b",))
    a.start(); assert first_inside.wait(1); b.start()
    time.sleep(0.1); assert order == ["a"]
    release.set(); a.join(); b.join()
    assert order == ["a", "b"]
