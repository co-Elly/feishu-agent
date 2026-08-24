import os
import sys

from isolated_workspace import IsolatedWorkspace, SourceTreeGuard, recover_abandoned_merges


def _workspace(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "scripts").mkdir()
    (source / "keep.txt").write_text("old", encoding="utf-8")
    (source / "delete.txt").write_text("delete", encoding="utf-8")
    (source / "config.example.json").write_text("{}", encoding="utf-8")
    (source / "config.json").write_text('{"secret":"real"}', encoding="utf-8")
    (source / "_guardian.ps1").write_text("guard", encoding="utf-8")
    (source / "scripts" / "restart_bot.ps1").write_text("restart", encoding="utf-8")
    return IsolatedWorkspace(str(source), str(tmp_path / "executions"), "task", sys.executable)


def test_staging_does_not_expose_real_config_or_touch_source(tmp_path):
    isolated = _workspace(tmp_path).prepare()
    assert (tmp_path / "source" / "keep.txt").read_text(encoding="utf-8") == "old"
    assert open(os.path.join(isolated.root, "config.json"), encoding="utf-8").read() == "{}"
    open(os.path.join(isolated.root, "keep.txt"), "w", encoding="utf-8").write("new")
    assert (tmp_path / "source" / "keep.txt").read_text(encoding="utf-8") == "old"
    assert not os.path.exists(os.path.join(isolated.root, "_guardian.ps1"))
    assert not os.path.exists(os.path.join(isolated.root, "scripts", "restart_bot.ps1"))


def test_staging_must_be_outside_source_tree(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    try:
        IsolatedWorkspace(str(source), str(source / "workspace"), "task", sys.executable)
        assert False, "expected nested execution root rejection"
    except ValueError as exc:
        assert "主项目目录内部" in str(exc)


def test_validated_merge_can_rollback_updates_creates_and_deletes(tmp_path):
    isolated = _workspace(tmp_path).prepare()
    before = isolated.snapshot()
    open(os.path.join(isolated.root, "keep.txt"), "w", encoding="utf-8").write("new")
    open(os.path.join(isolated.root, "created.txt"), "w", encoding="utf-8").write("created")
    os.remove(os.path.join(isolated.root, "delete.txt"))
    changed = isolated.changed(before, isolated.snapshot())
    assert changed == ["created.txt", "delete.txt", "keep.txt"]
    isolated.merge(changed)
    assert (tmp_path / "source" / "keep.txt").read_text(encoding="utf-8") == "new"
    assert (tmp_path / "source" / "created.txt").exists()
    assert not (tmp_path / "source" / "delete.txt").exists()
    isolated.rollback()
    assert (tmp_path / "source" / "keep.txt").read_text(encoding="utf-8") == "old"
    assert not (tmp_path / "source" / "created.txt").exists()
    assert (tmp_path / "source" / "delete.txt").read_text(encoding="utf-8") == "delete"


def test_source_guard_detects_and_restores_escape(tmp_path):
    isolated = _workspace(tmp_path)
    source = tmp_path / "source"
    guard = SourceTreeGuard(str(source)).capture()
    (source / "keep.txt").write_text("escaped", encoding="utf-8")
    (source / "new.py").write_text("bad", encoding="utf-8")
    changed = guard.restore_if_changed()
    assert changed == ["keep.txt", "new.py"]
    assert (source / "keep.txt").read_text(encoding="utf-8") == "old"
    assert not (source / "new.py").exists()


def test_source_guard_ignores_runtime_files(tmp_path):
    _workspace(tmp_path)
    source = tmp_path / "source"
    chat_log = source / "chat_log.jsonl"
    chat_log.write_text("before", encoding="utf-8")
    guard = SourceTreeGuard(str(source)).capture()

    chat_log.write_text("after", encoding="utf-8")
    (source / "config.json").write_text("changed", encoding="utf-8")

    assert guard.restore_if_changed() == ["config.json"]
    assert chat_log.read_text(encoding="utf-8") == "after"
    assert (source / "config.json").read_text(encoding="utf-8") == '{"secret":"real"}'


def test_abandoned_merge_journal_rolls_back_after_simulated_crash(tmp_path):
    isolated = _workspace(tmp_path).prepare()
    source = tmp_path / "source"
    open(os.path.join(isolated.root, "keep.txt"), "w", encoding="utf-8").write("new")
    open(os.path.join(isolated.root, "created.txt"), "w", encoding="utf-8").write("created")
    isolated.merge(["keep.txt", "created.txt"])
    assert (source / "keep.txt").read_text(encoding="utf-8") == "new"
    recovered = recover_abandoned_merges(str(source), str(tmp_path / "executions"))
    assert recovered == ["task"]
    assert (source / "keep.txt").read_text(encoding="utf-8") == "old"
    assert not (source / "created.txt").exists()


def test_cleanup_retries_windows_directory_in_use_without_masking_result(tmp_path, monkeypatch):
    isolated = _workspace(tmp_path).prepare()
    real_rmtree = __import__("shutil").rmtree
    calls = []

    def temporarily_busy(path, onerror=None):
        calls.append(path)
        if len(calls) == 1:
            raise PermissionError(32, "directory in use", path)
        return real_rmtree(path, onerror=onerror)

    monkeypatch.setattr("isolated_workspace.shutil.rmtree", temporarily_busy)
    assert isolated.cleanup(attempts=2, initial_delay=0) is True
    assert len(calls) == 2
    assert not os.path.exists(isolated.execution_dir)


def test_cleanup_exhaustion_is_deferred_instead_of_raising(tmp_path, monkeypatch):
    isolated = _workspace(tmp_path).prepare()

    def always_busy(path, onerror=None):
        raise PermissionError(32, "directory in use", path)

    monkeypatch.setattr("isolated_workspace.shutil.rmtree", always_busy)
    assert isolated.cleanup(attempts=2, initial_delay=0) is False
    assert os.path.isdir(isolated.execution_dir)
