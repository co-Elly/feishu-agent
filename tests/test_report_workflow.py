import os

import pytest

import report_workflow


GOAL = ('阅读 "C:\\Users\\26420\\Desktop\\main.pdf"，结合项目目录 '
        '"E:\\Syn3D-Process" 剖析论文内容，仅生成一份论文改进报告，不修改项目源码。')


def test_detects_exact_read_only_report_request():
    assert report_workflow.is_read_only_report_request(GOAL)
    assert report_workflow.report_request_paths(GOAL) == (
        "C:\\Users\\26420\\Desktop\\main.pdf", "E:\\Syn3D-Process",
    )
    assert not report_workflow.is_read_only_report_request("协作 修改项目并运行测试")


def test_report_output_is_task_scoped_not_next_to_external_pdf(tmp_path, monkeypatch):
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"pdf")
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(report_workflow, "runtime_value", lambda _name: str(runtime))

    request = report_workflow.parse_report_request(
        f'阅读 "{pdf}"，结合项目目录 "{project}"，仅生成报告，不修改项目', "task1",
    )

    assert request["output_path"] == str(
        runtime / "tasks" / "task1" / "论文改进报告_task1.docx"
    )


def test_report_planning_is_local_and_does_not_call_agent(tmp_path, monkeypatch):
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"pdf")
    project = tmp_path / "project"
    project.mkdir()
    (project / "model.py").write_text("stable", encoding="utf-8")
    monkeypatch.setattr(report_workflow, "parse_report_request", lambda *_: {
        "pdf_path": str(pdf), "project_path": str(project),
        "output_path": str(tmp_path / "task" / "report.docx"),
    })
    monkeypatch.setattr(report_workflow, "extract_pdf_pages", lambda _path: ["正文", "实验"])
    monkeypatch.setattr(report_workflow, "call_codex", lambda *a, **k: (
        _ for _ in ()
    ).throw(AssertionError("preflight must not call Codex")))

    plan = report_workflow.plan_read_only_report(
        "goal", "task1", {"root_request": "goal"},
    )

    assert plan["operation_mode"] == "read_only_report"
    assert plan["report_request"]["page_count"] == 2
    assert plan["report_request"]["project_files"] == 1
    assert "预审未调用 Agent" in plan["research"]


def test_execute_report_keeps_project_unchanged_and_writes_one_docx(tmp_path, monkeypatch):
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"pdf")
    project = tmp_path / "project"
    project.mkdir()
    (project / "model.py").write_text("stable", encoding="utf-8")
    output = tmp_path / "report.docx"

    monkeypatch.setattr(report_workflow, "parse_report_request", lambda *_: {
        "pdf_path": str(pdf), "project_path": str(project), "output_path": str(output),
    })
    monkeypatch.setattr(report_workflow, "runtime_value", lambda _name: str(tmp_path / "runtime"))
    monkeypatch.setattr(report_workflow, "extract_pdf_pages", lambda _path: ["论文正文" * 300])
    monkeypatch.setattr(report_workflow, "analyze_pdf_and_project",
                        lambda *_args, **_kwargs: "# 论文改进报告\n\n## 高优先级\n- 补充消融实验" * 20)

    result = report_workflow.execute_read_only_report("goal", "task1")

    assert result["success"] is True
    assert output.is_file()
    assert (project / "model.py").read_text(encoding="utf-8") == "stable"
    assert [path.name for path in tmp_path.glob("*.docx")] == ["report.docx"]


def test_project_change_aborts_before_report_write(tmp_path, monkeypatch):
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"pdf")
    project = tmp_path / "project"
    project.mkdir()
    source = project / "model.py"
    source.write_text("before", encoding="utf-8")
    output = tmp_path / "report.docx"

    monkeypatch.setattr(report_workflow, "parse_report_request", lambda *_: {
        "pdf_path": str(pdf), "project_path": str(project), "output_path": str(output),
    })
    monkeypatch.setattr(report_workflow, "runtime_value", lambda _name: str(tmp_path / "runtime"))
    monkeypatch.setattr(report_workflow, "extract_pdf_pages", lambda _path: ["论文正文" * 300])

    def mutate(*_args, **_kwargs):
        source.write_text("after", encoding="utf-8")
        return "# 报告\n" + "内容" * 300

    monkeypatch.setattr(report_workflow, "analyze_pdf_and_project", mutate)

    with pytest.raises(report_workflow.ReportWorkflowError, match="发生变化"):
        report_workflow.execute_read_only_report("goal", "task1")
    assert not output.exists()


def test_snapshot_includes_cache_directories_and_report_never_overwrites(tmp_path):
    project = tmp_path / "project"
    cache = project / "__pycache__"
    cache.mkdir(parents=True)
    cached = cache / "model.pyc"
    cached.write_bytes(b"before")

    before = report_workflow.snapshot_tree(str(project))
    cached.write_bytes(b"after")
    after = report_workflow.snapshot_tree(str(project))

    assert "__pycache__/model.pyc" in before
    assert before != after
    output = tmp_path / "report.docx"
    output.write_bytes(b"existing")
    with pytest.raises(report_workflow.ReportWorkflowError, match="拒绝覆盖"):
        report_workflow.write_docx("# 报告\n内容", str(output))
    assert output.read_bytes() == b"existing"


def test_existing_task_report_is_validated_and_reused(tmp_path, monkeypatch):
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"pdf")
    project = tmp_path / "project"
    project.mkdir()
    output = tmp_path / "report.docx"
    report_workflow.write_docx("# 论文改进报告\n" + "可验证内容" * 100, str(output))
    monkeypatch.setattr(report_workflow, "parse_report_request", lambda *_: {
        "pdf_path": str(pdf), "project_path": str(project), "output_path": str(output),
    })
    monkeypatch.setattr(report_workflow, "extract_pdf_pages", lambda _path: ["第一页", "第二页"])

    result = report_workflow.recover_existing_report("goal", "task1")

    assert result["report_path"] == str(output)
    assert result["evidence"]["page_count"] == 2
    assert result["evidence"]["recovered_existing_report"] is True


def test_project_evidence_is_bounded_prioritized_and_excludes_secrets(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("project overview", encoding="utf-8")
    submission = project / "submission_jms"
    submission.mkdir()
    (submission / "main.tex").write_text("paper method", encoding="utf-8")
    results = project / "results"
    results.mkdir()
    (results / "metrics.json").write_text('{"score": 0.9}', encoding="utf-8")
    (project / ".env").write_text("API_KEY=do-not-read", encoding="utf-8")
    (project / "access_token.txt").write_text("secret", encoding="utf-8")

    evidence, selected, stats = report_workflow.build_project_evidence(str(project), max_chars=5000)

    assert "README.md" in selected
    assert "submission_jms/main.tex" in selected
    assert "results/metrics.json" in selected
    assert ".env" not in evidence and "access_token.txt" not in evidence
    assert stats["selected_text_files"] == 3
    assert stats["evidence_chars"] <= 5000
