import time

import pytest

from workspace_lease import WorkspaceLease, WorkspaceLeaseBusy, release_workspace_leases


def test_cross_process_style_lease_is_exclusive_and_releasable(tmp_path):
    db = str(tmp_path / "leases.db")
    first = WorkspaceLease("task-a", db_path=db).acquire()
    with pytest.raises(WorkspaceLeaseBusy, match="task-a"):
        WorkspaceLease("task-b", db_path=db).acquire()
    first.release()
    with WorkspaceLease("task-b", db_path=db):
        pass


def test_expired_lease_can_be_reclaimed(tmp_path):
    db = str(tmp_path / "leases.db")
    WorkspaceLease("dead-task", db_path=db, ttl_seconds=-1).acquire()
    with WorkspaceLease("new-task", db_path=db):
        pass


def test_lease_token_prevents_foreign_release(tmp_path):
    db = str(tmp_path / "leases.db")
    first = WorkspaceLease("task-a", db_path=db).acquire()
    foreign = WorkspaceLease("task-a", db_path=db)
    foreign.release()
    with pytest.raises(WorkspaceLeaseBusy):
        WorkspaceLease("task-b", db_path=db).acquire()
    first.release()


def test_recovery_releases_interrupted_task_leases(tmp_path):
    db = str(tmp_path / "leases.db")
    WorkspaceLease("interrupted", db_path=db).acquire()

    assert release_workspace_leases(["interrupted"], db_path=db) == 1

    with WorkspaceLease("next-task", db_path=db):
        pass
