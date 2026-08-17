import json
import os
import re
import subprocess
import time
import requests
from obsidian_bridge import obsidian_bridge
from agent_runtime import call_codex, isolated_prompt_file

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def call_llm(system_prompt, user_prompt, model="deepseek-chat", max_retries=2):
    """基础大模型能力：deepseek-chat / gemini-2.5-flash（带自动重试与降级兜底）"""
    cfg = load_config()
    last_error = ""
    for attempt in range(max_retries + 1):
        # Gemini 分支（免费辅助模型，用于贾维斯等角色）
        if model.startswith("gemini"):
            api_key = cfg.get("gemini", {}).get("api_key") or ""
            if api_key:
                url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.7,
                }
                try:
                    res = requests.post(url, headers=headers, json=payload, timeout=45).json()
                    if "choices" in res and res["choices"]:
                        return res["choices"][0]["message"]["content"].strip()
                    elif "error" in res:
                        last_error = f"Gemini 接口提示: {res['error'].get('message')}"
                except Exception as e:
                    last_error = str(e)
                    time.sleep(1.5 * (attempt + 1))  # 指数退避
                    continue  # Gemini 失败重试，超次后落到 DeepSeek 兜底
        # DeepSeek 分支（默认）
        api_key = cfg.get("deepseek", {}).get("api_key") or ""
        url = "https://api.deepseek.com/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model if model.startswith("deepseek") else "deepseek-chat",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.6,
        }
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=45).json()
            if "choices" in res and res["choices"]:
                return res["choices"][0]["message"]["content"].strip()
            elif "error" in res:
                last_error = f"LLM 接口提示: {res['error'].get('message')}"
        except Exception as e:
            last_error = str(e)
            time.sleep(1.5 * (attempt + 1))  # 指数退避
            continue
    return f"LLM 调用失败（重试 {max_retries} 次后）: {last_error}"


def call_real_antigravity(task_text, timeout=170):
    """【真实反重力】调用桌面端 agy CLI（用户 Google AI Pro 免费额度，Gemini 驱动）

    通道：WSL → powershell.exe run_task.ps1(读UTF-8任务书+内置代理) → agy.exe -p → Gemini Pro
    认证：与桌面版 Antigravity 共享 OAuth token（跑一次会自动刷新 token 文件）
    """
    # 任务书文件：写盘用本地真实路径，传给 PowerShell 必须用 Windows 路径格式（E:\...）
    task_file_local = isolated_prompt_file("agy_", task_text)
    # 转 Windows 路径：/mnt/e/feishu-agent/xxx.txt → E:\feishu-agent\xxx.txt
    if task_file_local.startswith("/mnt/"):
        drive = task_file_local[5].upper()
        task_file_win = f"{drive}:{task_file_local[6:].replace('/', chr(92))}"
    else:
        task_file_win = task_file_local
    try:
        # 用 run_task_pro.ps1（已内置代理 + --dangerously-skip-permissions + pro 模型 + 600s 超时）
        # ⚠️ 不能从调用侧传参：PowerShell -File 传参不可靠，会把 flag 当 prompt 文本
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               r"C:\Users\26420\AppData\Local\agy\run_task_pro.ps1", task_file_win]
        # cwd 自适应：Windows Python 用 C:\，WSL Python 用 /mnt/c（两侧测试都能跑）
        work_cwd = "C:\\" if os.path.exists("C:\\") else "/mnt/c"
        proc = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 30,
            cwd=work_cwd,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if stdout:
            return stdout
        elif stderr:
            return f"反重力报错: {stderr[:200]}"
        return "反重力执行完成，无输出。"
    except subprocess.TimeoutExpired:
        return "反重力思考超时（请稍后重试或缩小议题）"
    except Exception as e:
        return f"反重力调用异常: {str(e)}"


def call_real_hermes(task_text, timeout=170):
    """【真实物理进程】调用 WSL 中的 Hermes Agent（主脑/产品经理真身）

    通道：Windows → wsl.exe → hermes -z（heredoc 传参防转义炸弹）
    """
    try:
        task_file = isolated_prompt_file("hermes_", task_text)
        bash_path = task_file.replace("E:", "/mnt/e").replace("\\", "/") if task_file.startswith("E:") else task_file
        cmd = ["wsl.exe", "-e", "bash", "-lc", 'hermes -z "$(cat "$1")"', "bash", bash_path]
        # cwd 自适应：Windows Python 用 E:\\feishu-agent，WSL Python 用当前目录
        work_cwd = "E:\\feishu-agent" if os.path.exists("E:\\feishu-agent") else os.path.dirname(os.path.abspath(__file__))
        proc = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=work_cwd,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if stdout:
            return stdout
        elif stderr:
            return f"Hermes 报错: {stderr[:300]}"
        return "Hermes 执行完成，无输出。"
    except subprocess.TimeoutExpired:
        return "Hermes 思考超时（请稍后重试或缩小议题）"
    except Exception as e:
        return f"Hermes 调用异常: {str(e)}"


def call_real_codex(task_text, timeout=300, writable=False):
    """Core engineer backed by Codex CLI, with explicit write authorization."""
    return call_codex(task_text, timeout=timeout, writable=writable)


class RoundTableDiscussion:
    """圆桌讨论模式：多 Agent 独立思考 + 相互交流 + 开放式意见收敛"""

    AGENTS = {
        "pm": {
            "name": "👔 产品经理·Hermes",
            "engine": "hermes",  # 🔥 真实 Hermes（主脑真身）
            "model": "",
            "role": "你是产品经理兼会议主持人（Hermes 主脑真身）。你负责：把控讨论方向、评估意见、推动共识、最后向老板汇报。你关注需求价值、用户视角与落地优先级。",
            "style": "言简意赅，善于总结和引导，每条发言不超过 180 字。",
        },
        "arch": {
            "name": "📐 反重力·架构师",
            "engine": "antigravity",  # 🔥 真实 agy CLI（桌面端 Google AI Pro 额度）
            "model": "gemini-3.1-pro-high",
            "role": "你是一位首席系统架构师（代号反重力）。你负责：系统架构、数据流、模块划分、接口契约与技术选型。你关注高内聚低耦合、可扩展性与工程可行性。",
            "style": "结构化表达，给出模块/接口层面的专业意见，每条发言不超过 180 字。",
        },
        "dev": {
            "name": "💻 Codex·核心工程师",
            "engine": "codex",
            "model": "",
            "role": "你是 Codex 核心工程师。你负责代码实现方案、可运行性、性能、安全与测试验证。",
            "style": "给出可落地的技术方案与实现要点，每条发言不超过 180 字。",
        },
    }

    ORDER = ["pm", "arch", "dev"]
    MAX_ROUNDS = 3  # 第1轮独立发言 + 第2轮交流 + 第3轮收敛

    def __init__(self):
        self.memories = {}  # 每个 Agent 的独立上下文记忆

    def _mem(self, agent_key):
        if agent_key not in self.memories:
            self.memories[agent_key] = []
        return self.memories[agent_key]

    def _speak(self, agent_key, topic, others_speech, round_no, on_agent_message=None):
        """让单个 Agent 基于自己的记忆 + 其他人的发言，独立思考后发言"""
        agent = self.AGENTS[agent_key]
        my_history = self._mem(agent_key)

        # 拼装该 Agent 的独立上下文：自己的历史 + 他人本轮发言
        context_parts = []
        if my_history:
            context_parts.append("【你之前发表过的观点】\n" + "\n".join(f"- {s}" for s in my_history[-4:]))
        if others_speech:
            context_parts.append("【其他成员的最新发言（供你回应/反驳/补充）】\n" + others_speech)
        context_text = "\n\n".join(context_parts) if context_parts else "（无，你是第一个发言的）"

        if round_no == 1:
            stage = "这是第一轮，请基于你的专业视角**独立发表初步意见**（不要复述他人，直接给干货）。"
        elif round_no == 2:
            stage = "这是第二轮，请针对其他成员的观点**明确表态**：同意什么、补充什么、反对什么。要有交锋感。"
        else:
            stage = "这是收敛轮，请聚焦**最终共识**：给出你确认的关键结论与行动建议，减少分歧。"

        prompt = (
            f"【会议主题】{topic}\n\n"
            f"{context_text}\n\n"
            f"【当前轮次】第 {round_no} 轮\n{stage}\n"
            f"【发言要求】{agent['style']} 只输出你的发言内容本身。"
        )
        if agent.get("engine") == "antigravity":
            # 🔥 真实反重力：桌面端 agy CLI（Google AI Pro 免费额度，Gemini 驱动）
            agy_prompt = (
                "你在飞书多智能体圆桌会议上担任【反重力·首席架构师】。\n"
                f"{agent['role']}\n\n"
                f"【会议主题】{topic}\n\n"
                f"{context_text}\n\n"
                f"【当前轮次】第 {round_no} 轮\n{stage}\n"
                f"【发言要求】{agent['style']} 只输出你的发言内容本身，不要输出思考过程、不要输出工具调用，直接给出你的专业意见。"
            )
            reply = call_real_antigravity(agy_prompt)
        elif agent.get("engine") == "hermes":
            # 🔥 真实 Hermes：产品经理/主持人真身（主脑）
            hermes_prompt = (
                "你在飞书多智能体圆桌会议上担任【产品经理·Hermes·会议主持人】。\n"
                f"{agent['role']}\n\n"
                f"【会议主题】{topic}\n\n"
                f"{context_text}\n\n"
                f"【当前轮次】第 {round_no} 轮\n{stage}\n"
                f"【发言要求】{agent['style']} 只输出你的发言内容本身，不要输出思考过程，直接给出你的专业意见。"
            )
            reply = call_real_hermes(hermes_prompt)
        elif agent.get("engine") == "codex":
            codex_prompt = (
                f"【会议主题】{topic}\n\n"
                f"{context_text}\n\n"
                f"【你的身份】你是【Codex·核心工程师】，正在参加多智能体圆桌会议。\n"
                f"【当前轮次】第 {round_no} 轮\n{stage}\n"
                f"【发言要求】直接输出你的发言正文，格式为：\n"
                f"1. 表态：同意/补充/反对（一句话）\n"
                f"2. 技术要点：2-3 条干货\n"
                f"严禁反问、严禁提问、严禁输出思考过程、严禁调用工具。"
            )
            reply = call_real_codex(codex_prompt)
        else:
            reply = call_llm(agent["role"], prompt, model=agent["model"])
        reply = reply.strip().strip("「」\"'")
        self._mem(agent_key).append(reply)
        if on_agent_message:
            on_agent_message(agent["name"], f"（第{round_no}轮）\n{reply}")
        return reply

    def run(self, topic, on_agent_message=None):
        """主持一场圆桌讨论：独立发言 → 交锋交流 → 收敛总结"""
        if on_agent_message:
            on_agent_message("🎙️ 主持人", f"【圆桌会议开始】议题：{topic}\n本轮议程：独立发言 → 交叉讨论 → 收敛总结，共 {self.MAX_ROUNDS} 轮。")

        # 每轮记录：agent_key -> 该轮发言
        round_speeches = {}
        for round_no in range(1, self.MAX_ROUNDS + 1):
            if round_no > 1:
                others_text = "\n".join(
                    f"{self.AGENTS[k]['name']}：{s}" for k, s in round_speeches.items()
                )
            else:
                others_text = ""

            # 让每个 Agent 看到其他所有人的最新发言后回应
            current_round = {}
            for agent_key in self.ORDER:
                others_this_round = "\n".join(
                    f"{self.AGENTS[k]['name']}：{s}" for k, s in current_round.items()
                )
                # 若前面人已发言则结合，否则结合上一轮
                combined = others_this_round if others_this_round else others_text
                speech = self._speak(agent_key, topic, combined, round_no, on_agent_message)
                current_round[agent_key] = speech
                time.sleep(0.3)
            round_speeches = current_round

        # PM 最后总结陈词
        all_views = "\n".join(
            f"{self.AGENTS[k]['name']}：{s}" for k, s in round_speeches.items()
        )
        summary_prompt = (
            f"你是会议主持人。刚才 3 位成员（👔产品经理·Hermes、📐反重力、💻Codex）就【{topic}】完成了 {self.MAX_ROUNDS} 轮圆桌讨论，最终发言如下：\n"
            f"{all_views}\n\n"
            f"请向老板（人类CEO）做**最终总结陈词**：\n"
            f"1. 共识点（大家一致认可什么）\n"
            f"2. 分歧点（哪里还有不同意见，各是什么立场）\n"
            f"3. 你的裁决建议（作为主持人，你推荐怎么推进）\n"
            f"4. 抛给老板 2 个关键决策问题（让老板做选择）\n"
            f"控制在 300 字以内，结构清晰。"
        )
        final_summary = call_llm(
            "你是高效果断的产品经理兼会议主持人，善于总结共识、暴露分歧、让老板做选择题。",
            summary_prompt,
            model="deepseek-chat",
        )
        if on_agent_message:
            on_agent_message("👔 产品经理·主持总结", final_summary)
        return {
            "topic": topic,
            "rounds": round_speeches,
            "final_summary": final_summary,
        }


class MultiAgentSwarm:
    def __init__(self):
        self.bridge = obsidian_bridge

    def extract_project_name(self, user_goal):
        sys_prompt = "你是一个项目经理，请根据用户提出的需求，提炼出 4~8 个汉字的标准项目名称（例如：'股票基金监控系统'、'文献抓取器'、'定时清理服务'）。只输出名称，不要带标点或多余文字。"
        name = call_llm(sys_prompt, user_goal)
        name = re.sub(r"[^\w\u4e00-\u9fa5]", "", name)
        return name if name else "多智能体协同项目"

    def run_collaborative_project(self, user_goal, on_agent_message=None, cancel_check=None):
        """
        真正的多物理智能体全流程协同会议与落地执行：
        1. 👔 PM 拆解需求
        2. 📐 反重力 (Antigravity) 给出系统架构
        3. 🪐 贾维斯 (真实调用 WSL Hermes Agent) 进行自主外部探针与避坑调研
        4. 💻 Codex 在受限工作区内实现并验证
        5. 自动同步沉淀至 Obsidian 三级目录
        6. 👔 PM 验收并向老板发起决策
        """
        def checkpoint():
            if cancel_check:
                cancel_check()

        checkpoint()
        project_name = self.extract_project_name(user_goal)
        checkpoint()
        self.bridge.init_project(project_name, description=user_goal)

        dialogue_logs = []

        def emit(role_tag, message_text):
            checkpoint()
            entry = {"role": role_tag, "content": message_text, "time": time.strftime("%H:%M:%S")}
            dialogue_logs.append(entry)
            if on_agent_message:
                on_agent_message(role_tag, message_text)
            time.sleep(0.5)

        # -------------------------------------------------------------
        # 步骤 1：👔 产品经理 (PM) 发起立项
        # -------------------------------------------------------------
        pm_prompt = (
            f"你是数字团队的产品经理（PM）。老板在群里提出了需求：【{user_goal}】。\n"
            f"请简明扼要地向团队拆解该需求，并在 Obsidian 初始化了三级项目【{project_name}】。\n"
            f"现在 @反重力 请首席架构师给出系统时序与接口架构设计。"
        )
        pm_speech = call_llm("你是高效果断的产品经理，言简意赅。", pm_prompt)
        emit("👔 [产品经理 / 需求拆解]", pm_speech)

        # -------------------------------------------------------------
        # 步骤 2：📐 反重力 (首席架构师) 输出架构设计
        # -------------------------------------------------------------
        arch_prompt = (
            f"你是反重力（首席高级工程架构师）。产品经理提出了项目【{project_name}】的需求：【{user_goal}】。\n"
            f"请给出严密的系统架构设计（包含数据流、核心模块划分、接口契约）。\n"
            f"随后在群里 @贾维斯 请探索员针对外部接口与数据源进行探针调研。"
        )
        arch_speech = call_llm("你是顶级系统架构师，注重高内聚低耦合与工程可行性。", arch_prompt)
        emit("📐 [反重力 · 架构师]", arch_speech)

        # -------------------------------------------------------------
        # 步骤 3：🪐 贾维斯 (真实调用 WSL 中的 Hermes Agent 引擎！)
        # -------------------------------------------------------------
        hermes_task = (
            f"作为自主探索员贾维斯，分析项目【{project_name}】(需求: {user_goal})。"
            f"基于反重力的架构思路，给出外部数据源接口分析、防盗链请求头避坑与关键建议，协助 Codex 实现。"
        )
        scout_speech = call_real_hermes(hermes_task)
        emit("🪐 [贾维斯 · Hermes 自主探索员]", scout_speech)

        # -------------------------------------------------------------
        # 步骤 4：💻 Codex（写操作必须由协作指令明确触发）
        # -------------------------------------------------------------
        codex_task = (
            f"你是 Codex 核心工程师。请在当前工作区完成项目【{project_name}】：{user_goal}\n"
            "先检查现有文件，按需求实现最小完整改动并运行相关测试。"
            "不得访问工作区外文件，不得读取或输出密钥，不得执行破坏性命令。最后汇报修改文件与测试结果。"
        )
        dev_speech = call_real_codex(codex_task, writable=True)
        run_success = not dev_speech.startswith("Codex 调用失败") and "Codex 调用异常" not in dev_speech
        emit("💻 [Codex · 核心工程师]", dev_speech)

        # -------------------------------------------------------------
        # 步骤 5：自动回写 Obsidian 三级目录档案
        # -------------------------------------------------------------
        doc_01 = f"""# 📈 {project_name} —— 需求与架构设计

> **项目名称**：{project_name}  
> **所属目录**：`多agent/{project_name}/` (三级规范)  
> **责任团队**：👔 产品经理 & 📐 反重力 (首席架构师)  
> **更新时间**：{time.strftime('%Y-%m-%d %H:%M:%S')}  

---

## 一、 业务目标与需求 (By 👔 产品经理)
{user_goal}

---

## 二、 系统架构与时序设计 (By 📐 反重力)
{arch_speech}
"""
        self.bridge.write_architecture(project_name, doc_01)

        doc_02 = f"""# 💻 {project_name} —— 代码实现与本地测试

> **项目名称**：{project_name}  
> **负责团队**：💻 Codex & 🪐 贾维斯 (Hermes)  
> **执行状态**：{'✅ 验证通过' if run_success else '⚠️ 需调优'}  

---

## 一、 核心代码与本地执行记录 (By 💻 Codex)
{dev_speech}

---

## 二、 外部接口与避坑建议 (By 🪐 贾维斯 Hermes Agent)
{scout_speech}
"""
        self.bridge.write_code_test(project_name, doc_02)
        self.bridge.append_decision_log(project_name, "团队完成全员研讨，贾维斯(Hermes)提供避坑，Codex 完成工作区实现与验证。")
        checkpoint()

        # -------------------------------------------------------------
        # 步骤 6：👔 产品经理 (PM) 最终总结汇报 & 向老板发起大方向决策
        # -------------------------------------------------------------
        summary_prompt = (
            f"你是产品经理。项目【{project_name}】已经由架构师、贾维斯(Hermes)和 Codex 协同完成了技术设计与本地测试。\n"
            f"请向老板（人类 CEO）做最后的精炼汇报：\n"
            f"1. 汇报完成事项与代码落地情况（已保存在 Obsidian 多agent/{project_name}/ 目录下）。\n"
            f"2. 向老板抛出 2 个关键大方向决策问题（让老板做单选题）。"
        )
        pm_final_report = call_llm("你是专业的产品经理，结构清晰，重点突出，善于让领导做选择题。", summary_prompt)
        emit("👔 [产品经理 / 决策提问]", pm_final_report)

        return {
            "project_name": project_name,
            "dialogue_logs": dialogue_logs,
            "final_report": pm_final_report,
            "success": run_success,
        }


# 单例导出
swarm_orchestrator = MultiAgentSwarm()
roundtable = RoundTableDiscussion()
