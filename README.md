# 飞书多智能体协同工作站

单机 Python + SQLite + 本地 CLI + 飞书架构。通过飞书调用 Hermes、反重力和 Codex，支持持久普通对话、收敛式圆桌、二次审批协作与严格作用域长期记忆。

## 核心结构

- `bot.py`：飞书 WebSocket 入口、命令路由、确认回复和进度卡。
- `settings.py`：唯一配置入口、启动校验和日志脱敏。
- `agent_runtime.py`：三个本地 Agent 的统一进程运行时、错误分类和健康记录。
- `task_manager.py`：SQLite 持久任务、同会话 FIFO、跨会话并行、审批和恢复。
- `swarm_orchestrator.py`：只读规划、批准后执行与 Obsidian 回写。
- `roundtable_engine.py`：圆桌收敛、成员故障隔离、黑板和纪要。
- `memory_store.py`：全局/项目严格隔离的 FTS5 长期记忆。
- `control_store.py`：Agent 健康状态与结构化任务审计事件。
- `conversation_store.py`：对话历史和飞书消息去重。
- `workspace/`、`roundtable/`：运行数据；均不纳入 Git。

## 配置与启动

复制 `config.example.json` 为不纳入 Git 的 `config.json`。飞书凭据缺失会阻止启动；单个 Agent 配置异常只会使该通道降级。

`runtime` 段统一配置 Obsidian 路径、反重力脚本、Codex/Hermes 命令、审批时限和任务并发数。若需临时覆盖 Codex 命令，可设置 `FEISHU_CODEX_COMMAND`。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python bot.py
```

现有 `启动飞书管家.bat`、`启动飞书管家_静默.vbs` 和 `_guardian.ps1` 保持原启动方式并负责崩溃恢复。

## 调度与审批

所有普通聊天、圆桌、协作和深度健康均进入持久任务系统，并先回复“已收到”。同一 `chat_id` 严格 FIFO，不同会话并行。等待审批任务不占会话执行槽；批准后进入该会话队尾。

协作分两阶段：PM、架构师和探索员只读生成预审方案；只有收到 `批准任务 <ID>` 后，Codex 才获得 `workspace-write` 权限。所有工作区写入共用一把全局锁。审批默认 30 分钟过期。

重启时，普通任务和规划阶段安全重放，等待审批保持等待；已进入写入阶段的任务标记失败，要求人工重试。

## 飞书命令

- 任务：`任务列表`、`任务列表 全部`、`任务 <ID>`、`取消任务 <ID>`、`重试任务 <ID>`。
- 审批：`批准任务 <ID>`、`拒绝任务 <ID>`。
- 健康：`健康`只做命令/脚本检查并展示最近真实调用；`深度健康`后台并行运行三条轻量探针。
- 记忆：`记住 <内容>`、`记住 [项目:名称] <内容>`、`记忆列表`、`记忆列表 [项目:名称]`、`忘记 <ID>`。
- 工作：`开会 [项目:名称] <议题>`、`协作 [项目:名称] <目标>`。

未带 `[项目:名称]` 时只检索最多 3 条全局记忆，绝不读取项目记忆；带标签时额外检索最多 5 条该项目记忆。只有带项目标签的成功圆桌和协作结论自动写入项目记忆，并保存任务/会议来源。普通聊天不自动保存长期记忆。

## 数据与审计

- `workspace/conversations.db`：对话、消息去重、任务、`engine_health`、`task_events`、长期记忆和 FTS 索引。
- `roundtable/roundtable.db`：会议、turn 和幂等缓存，继续独立保存。
- `workspace/tasks/<ID>/task.json`：可读任务快照，包含阶段、审批方案和结果。
- `task_events` 只记录状态迁移、Agent 结果类别与耗时，不记录密钥或完整 Prompt。

迁移使用 `CREATE IF NOT EXISTS` 和按列检查的 `ALTER TABLE`，不会重建或清空现有表。数据库迁移前备份位于忽略提交的 `workspace/backups/`。

## 测试与安全边界

```powershell
python -m pytest -q
```

三个本地 Agent 均使用参数数组启动，不使用 `shell=True`。网络故障最多重试一次，401 等认证错误不重试。圆桌只更新一张进度卡；单 Agent 故障会退出本场，其余成员满足法定人数时继续。
