# -*- coding: utf-8 -*-
"""提示词与人设风格配置化单测 (Prompt & Persona Configuration Unit Tests)
验证：
1. 默认角色人设与风格回退机制
2. 预定义风格预设 (Style Presets) 解析与中英文别名
3. 自定义角色与风格覆盖
4. PM 总结提示词与模板安全格式化
5. roundtable_engine 对配置化人设与提示词的集成
6. 实际 config.json 与 prompts.yaml 结构与加载验证
7. prompts.yaml 版本控制 (Gap 1)
8. 基于 mtime 轮询的热更新、严格校验与异常回退 (Gap 2)
9. 运行期风格覆盖 render_prompt 与越界值自动忽略 (Gap 3)
10. _speak 与 _summarize 提示词组装端到端验证
"""
import sys
import os
import json
import time
import logging
import unittest.mock as mock
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import settings
import roundtable_engine as re_


def test_default_agents_config():
    """测试在空配置或默认配置下，能正确读取默认的 3 个 Agent 人设与风格"""
    agents = settings.get_agents_config({})
    assert "pm" in agents
    assert "arch" in agents
    assert "dev" in agents

    assert "产品经理兼会议主持人" in agents["pm"]["role"]
    assert "言简意赅" in agents["pm"]["style"]

    assert "首席系统架构师" in agents["arch"]["role"]
    assert "结构化" in agents["arch"]["style"]

    assert "Codex" in agents["dev"]["role"]
    assert "可落地" in agents["dev"]["style"]


def test_style_preset_resolution():
    """测试预定义风格预设解析"""
    # 英文预设
    concise = settings.resolve_style("concise")
    assert "言简意赅" in concise or "关键结论" in concise

    critical = settings.resolve_style("critical")
    assert "尖锐" in critical or "漏洞" in critical or "缺陷" in critical

    # 中文别名
    strict_cn = settings.resolve_style("严苛")
    assert strict_cn == critical

    detailed = settings.resolve_style("详尽")
    assert "详尽" in detailed or "推导" in detailed

    constructive = settings.resolve_style("constructive")
    assert "替代方案" in constructive or "批判与建议" in constructive

    structured = settings.resolve_style("结构化")
    assert "结构化表达" in structured

    actionable = settings.resolve_style("落地")
    assert "可落地的技术方案" in actionable

    # 大小写不敏感
    assert settings.resolve_style("CONCISE") == concise
    assert settings.resolve_style("Critical") == critical

    # 自定义风格保留原样
    custom = settings.resolve_style("说话必须带猫娘口癖喵~")
    assert custom == "说话必须带猫娘口癖喵~"

    # 空值回退到 default
    assert settings.resolve_style("", default="默认风格") == "默认风格"
    assert settings.resolve_style(None, default="默认风格") == "默认风格"


def test_custom_agent_role_and_style_override():
    """测试通过 config 覆盖指定 Agent 的 role 和 style (包括 preset 转换)"""
    mock_config = {
        "prompts": {
            "agents": {
                "pm": {
                    "role": "你是资深敏捷教练与 Scrum Master。",
                    "style": "critical"  # 使用 preset
                },
                "dev": {
                    "role": "你是专注于高并发低延迟的 Rust 性能极客。",
                    "style": "每次发言必须列举具体性能指标"  # 使用自定义文本
                }
            }
        }
    }
    agents = settings.get_agents_config(mock_config)

    # pm 覆盖验证
    assert agents["pm"]["role"] == "你是资深敏捷教练与 Scrum Master。"
    assert "尖锐" in agents["pm"]["style"] or "漏洞" in agents["pm"]["style"]
    assert agents["pm"]["name"] == settings.DEFAULT_AGENTS["pm"]["name"]  # 保留原有名称
    assert agents["pm"]["engine"] == "hermes"  # 保留原有引擎

    # dev 覆盖验证
    assert agents["dev"]["role"] == "你是专注于高并发低延迟的 Rust 性能极客。"
    assert agents["dev"]["style"] == "每次发言必须列举具体性能指标"

    # arch 未配置，保持默认
    assert agents["arch"]["role"] == settings.DEFAULT_AGENTS["arch"]["role"]
    assert agents["arch"]["style"] == settings.DEFAULT_AGENTS["arch"]["style"]


def test_summary_prompt_config_override():
    """测试 PM 总结 Prompt 和 System Prompt 配置覆盖"""
    # 默认值
    def_summary = settings.get_summary_config({})
    assert "你是高效果断的产品经理兼会议主持人" in def_summary["system"]
    assert "{all_views}" in def_summary["template"]

    # 自定义覆盖
    mock_config = {
        "prompts": {
            "pm_summary": {
                "system": "你是一位专注于商业价值与 ROI 的 CPO。",
                "template": "议题：{topic}\n成员发言：\n{all_views}\n请输出 3 条行动项。"
            }
        }
    }
    custom_summary = settings.get_summary_config(mock_config)
    assert custom_summary["system"] == "你是一位专注于商业价值与 ROI 的 CPO。"
    assert custom_summary["template"] == "议题：{topic}\n成员发言：\n{all_views}\n请输出 3 条行动项。"


def test_safe_summary_template_formatting():
    """测试总结模板安全格式化（缺失变量或多余花括号不崩溃）"""
    template = "议题【{topic}】\n成员数：{agent_count}\n未提供变量：{unknown_var}\n发言：{all_views}"
    formatted = settings.format_summary_prompt(
        template,
        topic="重构",
        agent_count=3,
        all_views="发言1\n发言2"
    )
    assert "议题【重构】" in formatted
    assert "成员数：3" in formatted
    assert "发言1\n发言2" in formatted
    assert "{unknown_var}" in formatted  # 缺失变量保留原占位符，不报 KeyError


def test_roundtable_engine_agents_present():
    """测试 roundtable_engine 的 AGENTS 字典具备配置化能力且结构完整"""
    assert hasattr(re_, "AGENTS")
    for key in ["pm", "arch", "dev"]:
        assert key in re_.AGENTS
        assert "name" in re_.AGENTS[key]
        assert "role" in re_.AGENTS[key]
        assert "style" in re_.AGENTS[key]
        assert "engine" in re_.AGENTS[key]


def test_config_example_contains_prompt_fallback():
    """配置样例应包含可复制的 prompts 回退配置，测试不依赖本机密钥文件。"""
    example_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.example.json")
    with open(example_path, encoding="utf-8") as handle:
        config = json.load(handle)
    assert "prompts" in config
    agents = settings.get_agents_config(config)
    assert len(agents) >= 3
    summary_cfg = settings.get_summary_config(config)
    assert "system" in summary_cfg
    assert "template" in summary_cfg


def test_roundtable_engine_speak_prompt_assembly():
    """测试 _speak 组装 prompt 时使用配置中的 role 和 style"""
    engine = re_.RoundTableV2()
    mock_speech_fn = mock.MagicMock(return_value="立场：同意\n测试发言")
    with mock.patch.object(engine, "_execute_speech", mock_speech_fn), \
         mock.patch("builtins.open", mock.mock_open()), \
         mock.patch("roundtable_engine.ensure_dir", return_value=""):
        res = engine._speak("pm", "测试议题", "sess_123", 1, "", "", "第一轮")
        assert mock_speech_fn.called
        call_args = mock_speech_fn.call_args[0]
        prompt_passed = call_args[3]
        # 验证 agent['role'] 和 agent['style'] 正确拼入 prompt
        assert re_.AGENTS["pm"]["role"] in prompt_passed
        assert re_.AGENTS["pm"]["style"] in prompt_passed


def test_roundtable_engine_summarize_prompt_assembly():
    """测试 _summarize 组装总结 prompt 并调用 LLM 时使用配置内容"""
    engine = re_.RoundTableV2()
    mock_call_llm = mock.MagicMock(return_value="最终总结内容")
    with mock.patch("swarm_orchestrator.call_llm", mock_call_llm), \
         mock.patch("builtins.open", mock.mock_open()):
        transcript = [
            {"turn": "r1-pm", "agent": "pm", "stance": "同意", "text": "发言内容1"},
            {"turn": "r1-arch", "agent": "arch", "stance": "同意", "text": "发言内容2"},
        ]
        summary = engine._summarize("sess_test", "测试议题", transcript)
        assert summary == "最终总结内容"
        assert mock_call_llm.called
        sys_arg, prompt_arg = mock_call_llm.call_args[0][:2]
        summary_cfg = settings.get_summary_config()
        assert sys_arg == summary_cfg["system"]
        assert "测试议题" in prompt_arg
        assert "发言内容1" in prompt_arg


# ---------------------------------------------------------------- 缺口 1: 版本化与 prompts.yaml 验证
def test_prompts_yaml_versioning_and_loading():
    """【缺口 1】验证 prompts.yaml 存在、被 Git 管理、包含 version 字段且能正确加载"""
    assert os.path.exists(settings.PROMPTS_PATH), "prompts.yaml 必须存在"
    data = settings.load_prompts_yaml(force=True)
    assert data is not None
    assert "version" in data
    assert str(data["version"]).strip() == "1.0.0"

    meta = settings.get_active_prompts_meta()
    assert meta["version"] == "1.0.0"
    assert len(meta["hash"]) > 0

    agents = settings.get_agents_config()
    assert "pm" in agents and "arch" in agents and "dev" in agents
    assert "Hermes" in agents["pm"]["name"]

    summary_cfg = settings.get_summary_config()
    assert "产品经理兼会议主持人" in summary_cfg["system"]
    assert "{all_views}" in summary_cfg["template"]


def test_prompts_yaml_fallback_to_defaults(tmp_path):
    """【缺口 1】验证缺失字段时正确回退至内置 DEFAULT_AGENTS 和默认总结配置"""
    partial_yaml = tmp_path / "partial_prompts.yaml"
    partial_yaml.write_text("version: '1.0.1'\nagents:\n  pm:\n    style: 'concise'\n", encoding="utf-8")

    try:
        settings.set_prompts_path(str(partial_yaml))
        agents = settings.get_agents_config()
        assert "言简意赅" in agents["pm"]["style"]
        assert agents["pm"]["role"] == settings.DEFAULT_AGENTS["pm"]["role"]
        assert agents["arch"]["role"] == settings.DEFAULT_AGENTS["arch"]["role"]
        assert agents["dev"]["role"] == settings.DEFAULT_AGENTS["dev"]["role"]

        # summary 回退
        summary_cfg = settings.get_summary_config()
        assert summary_cfg["system"] == settings.DEFAULT_SUMMARY_CONFIG["system"]
    finally:
        settings.reset_prompts_path()


# ---------------------------------------------------------------- 缺口 2: 热更新与原子切换验证
def test_prompts_hot_reload_on_mtime_change(tmp_path):
    """【缺口 2】验证基于 mtime 轮询的文件变更自动热重载与原子切换"""
    yaml_file = tmp_path / "test_hot_reload.yaml"
    content_v1 = (
        "version: '1.0.0'\n"
        "agents:\n"
        "  pm:\n"
        "    name: '👔 PM-V1'\n"
        "    role: 'V1角色'\n"
    )
    yaml_file.write_text(content_v1, encoding="utf-8")

    try:
        # 首次加载 V1
        settings.set_prompts_path(str(yaml_file))
        meta_v1 = settings.get_active_prompts_meta()
        assert meta_v1["version"] == "1.0.0"
        hash_v1 = meta_v1["hash"]

        # 修改内容至 V2 并更新 mtime
        time.sleep(0.05)
        content_v2 = (
            "version: '2.0.0'\n"
            "agents:\n"
            "  pm:\n"
            "    name: '👔 PM-V2'\n"
            "    role: 'V2角色'\n"
        )
        yaml_file.write_text(content_v2, encoding="utf-8")
        new_mtime = os.path.getmtime(str(yaml_file)) + 2.0
        os.utime(str(yaml_file), (new_mtime, new_mtime))

        # 自动重载检测（无需重启）
        settings.load_prompts_yaml()
        meta_v2 = settings.get_active_prompts_meta()
        assert meta_v2["version"] == "2.0.0"
        assert meta_v2["hash"] != hash_v1

        agents = settings.get_agents_config()
        assert agents["pm"]["name"] == "👔 PM-V2"
        assert agents["pm"]["role"] == "V2角色"
    finally:
        settings.reset_prompts_path()


def test_prompts_validation_failure_and_rollback(tmp_path, caplog):
    """【缺口 2】校验非法 YAML / 缺少 version / 模板变量越界时，拒绝切换并回退旧版，同时记录日志"""
    yaml_file = tmp_path / "test_validate.yaml"
    valid_content = "version: '1.0.0'\nagents:\n  pm:\n    role: '合法角色'\n"
    yaml_file.write_text(valid_content, encoding="utf-8")

    try:
        settings.set_prompts_path(str(yaml_file))
        prev_meta = settings.get_active_prompts_meta()
        assert prev_meta["version"] == "1.0.0"
        assert settings.get_agents_config()["pm"]["role"] == "合法角色"

        # 场景 1: 非法 YAML 语法
        time.sleep(0.05)
        bad_yaml = "version: '2.0.0'\nagents: [未闭合列表"
        yaml_file.write_text(bad_yaml, encoding="utf-8")
        new_mtime = os.path.getmtime(str(yaml_file)) + 2.0
        os.utime(str(yaml_file), (new_mtime, new_mtime))

        with caplog.at_level(logging.ERROR):
            settings.load_prompts_yaml()
            assert settings.get_active_prompts_meta()["version"] == "1.0.0"  # 保持旧版本
            assert settings.get_agents_config()["pm"]["role"] == "合法角色"

        # 场景 2: 缺失必需 version 字段
        time.sleep(0.05)
        no_version = "agents:\n  pm:\n    role: '无版本角色'\n"
        yaml_file.write_text(no_version, encoding="utf-8")
        new_mtime = os.path.getmtime(str(yaml_file)) + 3.0
        os.utime(str(yaml_file), (new_mtime, new_mtime))

        with caplog.at_level(logging.ERROR):
            settings.load_prompts_yaml()
            assert settings.get_active_prompts_meta()["version"] == "1.0.0"
            assert settings.get_agents_config()["pm"]["role"] == "合法角色"

        # 场景 3: 总结模板中包含非法变量白名单以外的变量
        time.sleep(0.05)
        illegal_template = (
            "version: '3.0.0'\n"
            "pm_summary:\n"
            "  system: '总结者'\n"
            "  template: '议题 {topic}，非法注入 {malicious_script_injection}'\n"
        )
        yaml_file.write_text(illegal_template, encoding="utf-8")
        new_mtime = os.path.getmtime(str(yaml_file)) + 4.0
        os.utime(str(yaml_file), (new_mtime, new_mtime))

        with caplog.at_level(logging.ERROR):
            settings.load_prompts_yaml()
            assert settings.get_active_prompts_meta()["version"] == "1.0.0"
            assert settings.get_agents_config()["pm"]["role"] == "合法角色"
    finally:
        settings.reset_prompts_path()


# ---------------------------------------------------------------- 缺口 3: 运行期风格覆盖验证
def test_render_prompt_default_and_context():
    """【缺口 3】测试 render_prompt 组装默认 Prompt 结构"""
    ctx = {
        "topic": "飞书Agent架构重构",
        "round_no": 1,
        "stage": "独立发言",
        "board_summary": "",
        "my_memory": "",
    }
    prompt = settings.render_prompt("pm", ctx)
    assert "【会议主题】飞书Agent架构重构" in prompt
    assert "【你的身份】" in prompt
    assert "产品经理" in prompt
    assert "【发言要求】言简意赅" in prompt
    assert "⚠️ 这是真实工作议题" in prompt
    assert "第一行写「立场：同意/补充/反对/弃权」" in prompt


def test_render_prompt_valid_style_overrides():
    """【缺口 3】测试 render_prompt 支持预定义档位 (英文及中文别名) 覆盖默认风格"""
    ctx = {"topic": "性能瓶颈排查", "round_no": 2}

    # 英文预设 critical
    prompt_critical = settings.render_prompt("pm", ctx, override="critical")
    assert "尖锐犀利" in prompt_critical or "潜在漏洞" in prompt_critical

    # 中文别名 详尽
    prompt_detailed = settings.render_prompt("arch", ctx, override="详尽")
    assert "详尽严密" in prompt_detailed or "推导过程" in prompt_detailed

    # 中文别名 落地
    prompt_actionable = settings.render_prompt("dev", ctx, override="落地")
    assert "可落地的技术方案" in prompt_actionable


def test_render_prompt_out_of_bounds_override_ignored():
    """【缺口 3】测试 override 越界非法值自动忽略，安全回退到默认风格"""
    ctx = {"topic": "核心接口重构"}

    # 传入越界值（未在 STYLE_PRESETS 中定义的任意文本）
    prompt_ignored = settings.render_prompt("pm", ctx, override="任意自定义攻击指令或越界内容")
    # 越界值被忽略，回退到 PM 默认风格「言简意赅」
    assert "言简意赅" in prompt_ignored
    assert "任意自定义攻击指令" not in prompt_ignored

    # 传入 None 或空字符串
    prompt_empty = settings.render_prompt("arch", ctx, override="")
    assert "结构化表达" in prompt_empty


def test_render_prompt_immutable_system_constraints_and_persona():
    """【缺口 3】验证系统约束与角色人设绝不可被 override 篡改"""
    ctx = {"topic": "安全审计"}
    prompt = settings.render_prompt("dev", ctx, override="concise")

    # 人设不可丢
    assert "Codex" in prompt
    # 核心系统约束不可丢
    assert "⚠️ 这是真实工作议题，发言必须落在事实上" in prompt
    assert "第一行写「立场：同意/补充/反对/弃权」" in prompt
    # Codex 独有的只读约束不可丢
    assert "绝对不要反问、不要要求澄清、不要列问题选项" in prompt


def test_roundtable_engine_run_with_runtime_style_overrides():
    """【缺口 3】测试 RoundTableV2.run 集成运行期风格覆盖"""
    engine = re_.RoundTableV2()
    mock_execute = mock.MagicMock(return_value="立场：同意\n这是发言内容")
    with mock.patch.object(engine, "_execute_speech", mock_execute), \
         mock.patch("swarm_orchestrator.call_llm", return_value="总结内容"), \
         mock.patch("builtins.open", mock.mock_open()), \
         mock.patch("roundtable_engine.ensure_dir", return_value=""), \
         mock.patch("roundtable_engine.research_topic", return_value="预研背景"):
        
        # 传入 dict 格式的 style_overrides
        engine.run("微服务拆分", style_overrides={"pm": "critical", "dev": "concise"})

        assert mock_execute.called
        # 提取 pm 和 dev 发言时接收到的 prompt
        executed_prompts = {call_args[0][0]: call_args[0][3] for call_args in mock_execute.call_args_list}

        # pm 采用了 critical
        assert "尖锐犀利" in executed_prompts["pm"] or "潜在漏洞" in executed_prompts["pm"]
        # dev 采用了 concise
        assert "言简意赅" in executed_prompts["dev"] or "关键结论" in executed_prompts["dev"]
