# 飞书多智能体协同工作室

通过飞书消息调用 Hermes、反重力和 Codex，支持普通对话、收敛式圆桌会议、项目协作及 Obsidian 归档。

## 成员

- Hermes：产品经理、主持与日常对话。
- 反重力：架构设计与技术选型。
- Codex：受限工作区内的代码实现、测试和问题排查。

旧 DeepSeek Harness“小d”已从运行时代码中移除。历史会议档案不会被改写。

## 目录结构

- `bot.py`：飞书 WebSocket 入口、消息路由与回复。
- `roundtable_engine.py`：圆桌编排、收敛判断、纪要和会议状态。
- `swarm_orchestrator.py`：项目协作流程与 Obsidian 回写。
- `agent_runtime.py`：Codex 进程适配与 CLI 任务书临时文件管理。
- `conversation_store.py`、`task_manager.py`：SQLite 对话与后台任务状态。
- `tests/`：可离线运行的回归测试。
- `workspace/`：任务快照、SQLite 数据库及历史调试产物归档。
- `roundtable/`：会议黑板、成员记忆、完整发言和纪要。
- `.tools/`：机器人服务使用的本地 CLI 工具。
- `启动飞书管家.bat`、`启动飞书管家_静默.vbs`、`_guardian.ps1`：常驻启动与崩溃恢复。

## 安装与启动

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python bot.py
```

本机还需要可用的 Hermes、反重力和 Codex CLI。若 `codex` 不在服务账户的 `PATH` 中，设置：

```powershell
$env:FEISHU_CODEX_COMMAND = "完整的 Codex CLI 命令"
```

其他运行参数：

- `FEISHU_MAX_WORKERS`：全局任务并发上限，默认 4；同一飞书会话始终串行。
- Codex 圆桌发言使用只读沙箱；只有明确的“协作/开发”任务才使用工作区写权限。

## 数据与恢复

- 对话历史保存在 `workspace/conversations.db`，首次启动会自动导入旧 `chat_history.json`。
- 飞书 `message_id` 会持久化去重，记录保留 7 天。
- 长任务保存在同一 SQLite 的 `tasks` 表；每个任务另有 `workspace/tasks/<任务ID>/task.json` 快照。
- 服务重启后，排队任务和可安全重放的圆桌任务自动恢复；可能修改文件的协作任务会标记失败，需人工发送“重试任务 <ID>”，避免重复副作用。
- 圆桌状态、纪要和 Agent 记忆位于 `roundtable/`。
- 运行数据、聊天日志、数据库和真实配置均已加入 `.gitignore`。

## 测试

```powershell
python -m pytest tests -q
```

飞书任务命令：`任务列表`、`任务 <ID>`、`取消任务 <ID>`、`重试任务 <ID>`、`健康`。

圆桌采用动态法定人数：首轮并行调用成员，失败成员立即熔断并退出后续轮次；至少保留两名有效成员才继续，否则任务快速失败。额度错误会按服务返回的恢复时间熔断，避免重复等待和扣费。

## 当前安全边界

三个本地 Agent 均通过参数数组启动，不使用 `shell=True`。项目协作允许 Codex 写入当前工作区；圆桌讨论只允许只读访问。
