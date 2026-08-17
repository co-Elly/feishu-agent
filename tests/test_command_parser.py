from command_parser import extract_project_tag, parse_memory_command


def test_project_tag_is_explicit_and_removed():
    text, project = extract_project_tag("开会 [项目:飞书工作站] 欢迎语")
    assert project == "飞书工作站"
    assert text == "开会 欢迎语"


def test_absent_project_tag_never_infers_project():
    assert extract_project_tag("讨论飞书欢迎语") == ("讨论飞书欢迎语", None)


def test_memory_commands():
    assert parse_memory_command("记住 保持正式风格") == {
        "action": "remember", "content": "保持正式风格", "project": None,
    }
    assert parse_memory_command("记住 [项目:机器人] 欢迎语轻松") == {
        "action": "remember", "content": "欢迎语轻松", "project": "机器人",
    }
    assert parse_memory_command("记忆列表 [项目:机器人]") == {
        "action": "list_memories", "project": "机器人",
    }
