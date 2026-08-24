import pytest

from constraint_envelope import (ConstraintEnvelopeError,
                                 build_constraint_envelope,
                                 validate_constraint_envelope)
from swarm_orchestrator import _scope_evidence


def test_root_file_scope_is_immutable_and_normalized():
    envelope = build_constraint_envelope(
        "仅新增 FULL_PIPELINE_ACCEPTANCE.md，禁止修改其他文件。"
    )
    assert envelope["scope_restricted"] is True
    assert envelope["allowed_paths"] == ["FULL_PIPELINE_ACCEPTANCE.md"]
    assert envelope["root_request"].startswith("仅新增")


def test_restricted_scope_fails_closed_when_no_path_can_be_parsed():
    with pytest.raises(ConstraintEnvelopeError, match="拒绝空范围放行"):
        build_constraint_envelope("禁止修改其他文件，只做该做的事。")


def test_meeting_plan_cannot_expand_original_allowed_files():
    envelope = build_constraint_envelope(
        "唯一允许的业务产物为 FULL_PIPELINE_ACCEPTANCE.md。"
    )
    plan = {
        "goal": "会议建议改为实现 FSM",
        "requirements": "新增 fsm_contract.py 和 tests/test_fsm_contract.py",
        "approved_scope": {"allowed_paths": ["fsm_contract.py", "tests/test_fsm_contract.py"]},
        "constraint_envelope": envelope,
    }
    evidence = _scope_evidence({}, {
        "fsm_contract.py": "new", "tests/test_fsm_contract.py": "new",
    }, plan)
    assert evidence["strict"] is True
    assert evidence["allowed_paths"] == ["FULL_PIPELINE_ACCEPTANCE.md"]
    assert evidence["violations"] == ["fsm_contract.py", "tests/test_fsm_contract.py"]
    assert evidence["passed"] is False


def test_serialized_restricted_envelope_cannot_be_emptied():
    with pytest.raises(ConstraintEnvelopeError, match="不得为空"):
        validate_constraint_envelope({
            "version": 1, "root_request": "仅新增 A.md",
            "hard_constraints": ["仅新增 A.md"],
            "scope_restricted": True, "allowed_paths": [],
        })
