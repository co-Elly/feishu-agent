"""Two-phase collaborative planning and explicitly approved workspace execution."""

import re
import time

import requests

from agent_runtime import call_codex, call_hermes
from obsidian_bridge import obsidian_bridge
from settings import load_config
from task_manager import WORKSPACE_WRITE_LOCK


def call_llm(system_prompt, user_prompt, model="deepseek-chat", max_retries=2):
    config = load_config()
    last_error = ""
    for attempt in range(max_retries + 1):
        provider = "gemini" if model.startswith("gemini") else "deepseek"
        section = config.get(provider) or {}
        api_key = section.get("api_key", "")
        if not api_key and provider == "gemini":
            provider, section, model = "deepseek", config.get("deepseek") or {}, "deepseek-chat"
            api_key = section.get("api_key", "")
        if not api_key:
            return f"LLM 调用失败: 未配置 {provider} API Key"
        if provider == "gemini":
            url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        else:
            url = "https://api.deepseek.com/chat/completions"
        try:
            response = requests.post(url, headers={
                "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
            }, json={
                "model": model, "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ], "temperature": 0.6,
            }, timeout=45)
            data = response.json()
            if response.status_code == 200 and data.get("choices"):
                return data["choices"][0]["message"]["content"].strip()
            last_error = (data.get("error") or {}).get("message") or response.text[-300:]
            if response.status_code in {400, 401, 403}:
                break
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < max_retries:
                time.sleep(1.5 * (attempt + 1))
    return f"LLM 调用失败（重试 {max_retries} 次后）: {last_error}"


class MultiAgentSwarm:
    def __init__(self, bridge=None):
        self.bridge = bridge or obsidian_bridge

    def _project_name(self, goal):
        raw = call_llm("把需求概括成 4-12 个中文字符的项目名，只输出项目名。", goal)
        clean = re.sub(r"[\\/:*?\"<>|\r\n]", "", raw).strip(" #`《》")
        return (clean[:24] or "未命名协作项目")

    def plan_collaborative_project(self, user_goal, project_name=None, memory_context="",
                                   on_agent_message=None, cancel_check=None):
        """Read-only phase: PM, architect and scout produce an approval plan."""
        def checkpoint():
            if cancel_check:
                cancel_check()

        def emit(role, text):
            if on_agent_message:
                on_agent_message(role, text)

        checkpoint()
        project_name = project_name or self._project_name(user_goal)
        background = f"\n\n{memory_context}" if memory_context else ""
        pm = call_llm(
            "你是产品经理，只做只读规划，不得修改文件。",
            f"项目：{project_name}\n目标：{user_goal}{background}\n输出验收标准、范围和非目标。",
        )
        emit("👔 产品经理", pm)
        checkpoint()
        architecture = call_llm(
            "你是首席架构师，只做只读设计，不得修改文件。",
            f"项目：{project_name}\n目标：{user_goal}\nPM方案：{pm}{background}\n"
            "输出最小架构、影响文件、数据迁移、风险和回退方案。",
        )
        emit("📐 架构师", architecture)
        checkpoint()
        scout_result = call_hermes(
            f"你是只读探索员。项目【{project_name}】，目标：{user_goal}\n架构方案：{architecture}\n"
            "只调查兼容性、可能遗漏和验证建议，不要修改任何文件。",
            timeout=180,
        )
        scout = scout_result.text
        emit("🔎 探索员", scout)
        checkpoint()
        return {
            "project_name": project_name,
            "goal": user_goal,
            "requirements": pm,
            "architecture": architecture,
            "research": scout,
            "research_ok": scout_result.ok,
            "impact_and_risks": architecture,
        }

    def execute_collaborative_project(self, plan, on_agent_message=None, cancel_check=None):
        """Write phase. Caller approval is represented by reaching this method."""
        def checkpoint():
            if cancel_check:
                cancel_check()

        project_name, goal = plan["project_name"], plan["goal"]
        implementation_prompt = (
            "你是获批后的核心工程师。只在当前工作区内实现并测试以下方案；保留兼容性，"
            "不得修改工作区外文件。完成后汇报改动文件、测试结果和剩余风险。\n\n"
            f"项目：{project_name}\n目标：{goal}\nPM：{plan['requirements']}\n"
            f"架构：{plan['architecture']}\n探索：{plan['research']}"
        )
        with WORKSPACE_WRITE_LOCK:
            checkpoint()
            result = call_codex(implementation_prompt, timeout=600, writable=True)
            if on_agent_message:
                on_agent_message("💻 Codex", result.text)
            if not result.ok:
                return {"project_name": project_name, "success": False, "final_report": result.text}
            checkpoint()
            self.bridge.init_project(project_name, goal)
            self.bridge.write_architecture(project_name, (
                f"# {project_name} · 需求与架构\n\n## 目标\n{goal}\n\n"
                f"## 产品方案\n{plan['requirements']}\n\n## 架构与风险\n{plan['architecture']}\n"
            ))
            self.bridge.write_code_test(project_name, (
                f"# {project_name} · 实现与测试\n\n## Codex 执行记录\n{result.text}\n\n"
                f"## 只读探索结论\n{plan['research']}\n"
            ))
            self.bridge.append_decision_log(project_name, "写入方案经飞书二次批准后执行。")
        final_report = call_llm(
            "你是产品经理，精炼汇报已完成事项、测试和剩余风险。",
            f"项目：{project_name}\n目标：{goal}\n执行结果：{result.text}",
        )
        return {"project_name": project_name, "success": True,
                "final_report": final_report, "implementation": result.text}


swarm_orchestrator = MultiAgentSwarm()
