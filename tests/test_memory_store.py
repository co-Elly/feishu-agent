from memory_store import MemoryStore


def test_scope_isolation_search_and_delete(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.db"))
    global_memory = store.add("欢迎语默认正式", source_type="manual", source_id="m1")
    project_a = store.add("欢迎语使用轻松风格", project_name="A", source_type="roundtable",
                          source_id="task-a", source_path="minutes-a.md")
    store.add("欢迎语使用严肃风格", project_name="B", source_type="roundtable", source_id="task-b")

    assert [row["id"] for row in store.search("欢迎语使用轻松")] == []
    assert [row["id"] for row in store.search("欢迎语使用轻松", project_name="A")] == [project_a["id"]]
    assert store.search("欢迎语使用轻松", project_name="B") == []
    assert project_a["source_id"] == "task-a" and project_a["source_path"] == "minutes-a.md"
    assert store.delete(global_memory["id"])
    assert store.get(global_memory["id"]) is None
    assert store.search("欢迎语默认正式") == []


def test_prompt_limits_and_no_implicit_project_scope(tmp_path):
    store = MemoryStore(str(tmp_path / "limits.db"))
    for index in range(5):
        store.add(f"全局结论{index}")
    for index in range(7):
        store.add(f"项目结论{index}", project_name="A", source_type="swarm", source_id=str(index))
    assert store.prompt_context("").count("|全局|") == 3
    assert "|项目:A|" not in store.prompt_context("")
    assert store.prompt_context("", project_name="A").count("|项目:A|") == 5


def test_one_evolution_per_task_is_always_injected_and_deletable(tmp_path):
    store = MemoryStore(str(tmp_path / "evolution.db"))
    first = store.add_evolution("先验证现状再修改", "task-1")
    duplicate = store.add_evolution("不应重复写入", "task-1")

    assert duplicate["id"] == first["id"]
    assert "先验证现状再修改" in store.prompt_context("完全无关的查询")
    assert store.delete(first["id"])
    assert "先验证现状再修改" not in store.prompt_context("完全无关的查询")
