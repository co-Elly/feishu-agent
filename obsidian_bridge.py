import os
import json
import time

VAULT_BASE = r"E:\Obsidian_Vault\多agent"


class ObsidianBridge:
    def __init__(self, base_dir=VAULT_BASE):
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)

    def get_project_dir(self, project_name):
        """获取二级项目目录路径"""
        proj_dir = os.path.join(self.base_dir, project_name)
        os.makedirs(proj_dir, exist_ok=True)
        return proj_dir

    def init_project(self, project_name, description=""):
        """初始化一个三级项目结构"""
        proj_dir = self.get_project_dir(project_name)
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")

        # 1. 01_需求与架构设计.md
        file_01 = os.path.join(proj_dir, "01_需求与架构设计.md")
        if not os.path.exists(file_01):
            content_01 = f"""# 📈 {project_name} —— 需求与架构设计

> **项目名称**：{project_name}  
> **所属目录**：`多agent/{project_name}/` (三级规范)  
> **责任团队**：👔 产品经理 (PM) & 📐 反重力 (首席架构师)  
> **创建日期**：{now_str}  
> **当前状态**：`In Progress (架构研讨中)`

---

## 一、 业务目标与需求 (By 👔 产品经理)
{description if description else "待团队研讨细化..."}

---

## 二、 系统架构与时序设计 (By 📐 反重力)
（待架构师输出接口与模块规范...）
"""
            with open(file_01, "w", encoding="utf-8") as f:
                f.write(content_01)

        # 2. 02_代码实现与本地测试.md
        file_02 = os.path.join(proj_dir, "02_代码实现与本地测试.md")
        if not os.path.exists(file_02):
            content_02 = f"""# 💻 {project_name} —— 代码实现与本地测试

> **项目名称**：{project_name}  
> **所属目录**：`多agent/{project_name}/`  
> **负责工程师**：💻 Codex & 🪐 贾维斯 (数据探针)  
> **执行状态**：`Pending Execution`

---

## 一、 核心代码实现 (By 💻 Codex)
（待 Codex 编写并验证...）

---

## 二、 本地测试与运行记录
（待记录本地终端实际输出...）
"""
            with open(file_02, "w", encoding="utf-8") as f:
                f.write(content_02)

        # 3. 03_决策记录与交接日志.md
        file_03 = os.path.join(proj_dir, "03_决策记录与交接日志.md")
        if not os.path.exists(file_03):
            content_03 = f"""# 📜 {project_name} —— 决策记录与交接日志

> **项目名称**：{project_name}  
> **所属目录**：`多agent/{project_name}/`  
> **记录人**：👔 产品经理 (PM)  
> **更新时间**：{now_str}  

---

## 一、 团队协同研讨与关键决策 (ADR)
- `{now_str}`: 👔 产品经理在 Obsidian 建立 `多agent/{project_name}/` 三级项目规范。

---

## 二、 老板方向审批记录 (Human-in-the-Loop)
（待老板定夺大方向...）
"""
            with open(file_03, "w", encoding="utf-8") as f:
                f.write(content_03)

        self._update_readme_index(project_name, description)
        return proj_dir

    def _update_readme_index(self, project_name, description):
        """更新一级 README 项目总索引"""
        readme_file = os.path.join(self.base_dir, "README.md")
        if not os.path.exists(readme_file):
            return
        with open(readme_file, "r", encoding="utf-8") as f:
            content = f.read()

        if project_name not in content:
            new_row = f"\n| **[[{project_name}/01_需求与架构设计|{project_name}]]** | 🟡 进行中 | PM + 反重力 + Codex + 贾维斯 | {description[:50]} |"
            content += new_row
            with open(readme_file, "w", encoding="utf-8") as f:
                f.write(content)

    def write_architecture(self, project_name, content_text):
        """更新 01_需求与架构设计.md"""
        proj_dir = self.get_project_dir(project_name)
        file_path = os.path.join(proj_dir, "01_需求与架构设计.md")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content_text)

    def write_code_test(self, project_name, content_text):
        """更新 02_代码实现与本地测试.md"""
        proj_dir = self.get_project_dir(project_name)
        file_path = os.path.join(proj_dir, "02_代码实现与本地测试.md")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content_text)

    def append_decision_log(self, project_name, log_entry):
        """追加决策与日志到 03_决策记录与交接日志.md"""
        proj_dir = self.get_project_dir(project_name)
        file_path = os.path.join(proj_dir, "03_决策记录与交接日志.md")
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(f"\n- `{now_str}`: {log_entry}\n")


# 单例导出
obsidian_bridge = ObsidianBridge()
