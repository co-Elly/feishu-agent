# 飞书多智能体协同工作站

单机 Python + SQLite + 本地 CLI + 飞书架构。通过飞书调用 Hermes、反重力和 Codex，支持持久普通对话、收敛式圆桌、二次审批协作与严格作用域长期记忆。

飞书事件只允许 Hermes gateway 建立一条入站长连接，再通过带随机令牌的 `127.0.0.1` 委托桥交给 `bot.py` 调度。`bot.py` 默认不再争抢同一 App ID 的飞书连接；仅在显式 `FEISHU_INGRESS_MODE=direct` 时启用直连，并通过进程锁和同 App ID 检查失败关闭。

## 核心结构

- `bot.py`：Hermes 委托入口、命令路由、确认回复、进度卡和文件交付。
- `ingress_bridge.py`：仅回环监听、Bearer 鉴权的 Hermes 入站委托桥。
- `settings.py`：唯一配置入口、启动校验和日志脱敏。
- `agent_runtime.py`：三个本地 Agent 的统一进程运行时、错误分类和健康记录。
- `task_manager.py`：SQLite 持久任务、同会话 FIFO、跨会话并行、审批和恢复。
- `swarm_orchestrator.py`：只读规划、批准后执行与 Obsidian 回写。
- `antigravity_first_pass.py`：反重力首版代码的有界上下文、补丁校验与 staging 落盘。
- `roundtable_engine.py`：圆桌收敛、成员故障隔离、黑板和纪要。
- `workflow_store.py`：会议拍板到协作执行的持久业务状态机与决策账本。
- `memory_store.py`：全局/项目严格隔离的 FTS5 长期记忆。
- `control_store.py`：Agent 健康状态与结构化任务审计事件。
- `conversation_store.py`：对话历史和飞书消息去重。
- `workspace/`、`roundtable/`：运行数据；均不纳入 Git。

## 配置与启动

复制 `config.example.json` 为不纳入 Git 的 `config.json`。飞书凭据缺失会阻止启动；单个 Agent 配置异常只会使该通道降级。

`runtime` 段统一配置 Obsidian 路径、外置 staging 根目录、反重力服务隔离配置、Codex/Hermes 命令、审批时限和任务并发数。`execution_dir` 与 `antigravity_service_profile` 必须位于主项目之外。若需临时覆盖 Codex 命令，可设置 `FEISHU_CODEX_COMMAND`。

初始化一次工作区内的测试环境：

```powershell
python -m venv .tools\test-venv
.tools\test-venv\Scripts\python.exe -m pip install -r requirements.txt
```

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python bot.py
```

现有 `启动飞书管家.bat`、`启动飞书管家_静默.vbs` 和 `_guardian.ps1` 保持原启动方式并负责崩溃恢复。

## 调度与审批

所有普通聊天、圆桌、协作和深度健康均进入持久任务系统，并先回复“已收到”。同一 `chat_id` 严格 FIFO，不同会话并行。等待审批任务不占会话执行槽；批准后进入该会话队尾。

完整协作链路不可跳步：`协作 <目标>` 先创建圆桌会议 → Agent 动态多轮收敛 → 系统请发起人拍板 → 默认携带拍板意见续会（明确说“无需继续讨论”时可直接收口）→ 系统询问是否开始协作 → 发起人回复 `开始协作` → 创建只读预审任务 → `批准任务 <ID>` → 执行。普通消息、历史会议或裸协作命令均不能伪造“开始协作”回执；缺少 workflow、会议任务和确认消息三者绑定的旧任务不能批准、重试或执行。

代码协作的执行分工为：反重力编写第一版代码，Codex 完善并验证。反重力仍固定运行在 `plan` 模式，不获得不可靠的原生 `write_file(*)` 权限；调度器向它提供批准范围内的有界源文件上下文，接收其 unified diff，拒绝绝对路径、路径穿越、控制文件、符号链接、二进制和未批准文件，校验后仅应用到任务 staging。Codex 随后在同一 staging 使用 `workspace-write` 检查首版、修正、补测试并运行验证。所有写入共用线程锁与 SQLite 租约，审批默认 30 分钟过期。

包含“外部 PDF + 项目目录 + 仅生成报告 + 不修改项目”的协作请求同样先经过会议、拍板和开始协作确认；预审执行独立的只读报告流程：批准前只做确定性的本地 PDF/项目可读性预检，不调用 Agent；批准后对项目做前后全量快照，并构建有界、脱敏、带相对路径的项目证据包，Codex 在项目外的 `read-only` 分析目录中基于 PDF 与证据包生成报告。仅当项目零变化时才在任务目录原子生成唯一 DOCX，并通过飞书文件消息发送。该流程不会进入源码 staging、代码测试或合并，因此不存在“首版代码”阶段。

每个会议或协作任务在入口创建系统约束信封，保存原始用户请求、硬约束和允许文件。信封会原样贯穿动态续会、协作交接、预审、批准、执行、重试和最终验收；会议共识和 Agent 建议只能在其内细化，不得扩大范围。含严格文件限制却解析不出允许路径时失败关闭，禁止以空范围放行。

约束优先级固定为：用户原始硬约束 > 用户拍板 > 会议共识 > Agent 建议。如需改变原始硬约束，应结束当前链路并用新请求创建新信封。

重启时，普通任务和规划阶段安全重放，等待审批保持等待；已批准且进入代码写入阶段的任务标记为阻塞并释放遗留租约，禁止自动重放，要求人工重试。只读报告任务按“已生成/已发送”检查点幂等恢复，同一任务不会生成第二份文件或重复发送已确认交付的文件。

## 飞书命令

- 任务：`任务列表`、`任务列表 全部`、`任务 <ID>`、`取消任务 <ID>`、`重试任务 <ID>`。
- 审批：`批准任务 <ID>`、`拒绝任务 <ID>`。
- 健康：`健康`只做命令/脚本检查并展示最近真实调用；`深度健康`后台并行运行三条轻量探针。
- 记忆：`记住 <内容>`、`记住 [项目:名称] <内容>`、`记忆列表`、`记忆列表 [项目:名称]`、`忘记 <ID>`。
- 工作：`开会 [项目:名称] <议题>`；`协作 [项目:名称] <目标>` 从会议阶段启动完整链路；收到系统询问后用 `开始协作 [项目:名称]` 创建预审。

未带 `[项目:名称]` 时只检索最多 3 条全局记忆，绝不读取项目记忆；带标签时额外检索最多 5 条该项目记忆。只有带项目标签的成功圆桌和协作结论自动写入项目记忆，并保存任务/会议来源。普通聊天不自动保存长期记忆。

## 数据与审计

- `workspace/conversations.db`：对话、消息去重、任务、`engine_health`、`task_events`、长期记忆和 FTS 索引。
- `roundtable/roundtable.db`：会议、turn 和幂等缓存，继续独立保存。
- `workspace/tasks/<ID>/task.json`：可读任务快照，包含阶段、审批方案和结果。
- `task_events` 只记录状态迁移、Agent 结果类别与耗时，不记录密钥或完整 Prompt。

迁移使用 `CREATE IF NOT EXISTS` 和按列检查的 `ALTER TABLE`，不会重建或清空现有表。数据库迁移前备份位于忽略提交的 `workspace/backups/`。

## 测试与安全边界

```powershell
scripts\verify.ps1
```

该入口固定使用 `E:\feishu-agent\.tools\test-venv`，并把 pytest 临时目录放在项目 `workspace` 中。Agent、人工验收和自动化不得改用系统 Python、Anaconda 或 WSL Python。

守护进程同样使用上述 E 盘虚拟环境启动机器人，并将最近退出原因、退出码、运行时长及一小时重启次数写入 `workspace/guardian_status.json`；一小时内退出达到 5 次会停止自动重启，等待人工处理。“健康”命令会显示这份状态。

三个本地 Agent 均使用参数数组启动，不使用 `shell=True`。Codex 调用使用 `--ephemeral` 并按任务选择 `read-only` 或 staging `workspace-write`；反重力使用独立 Windows 用户配置目录、无全局工具授权且固定 `plan` 模式，通过受控补丁通道成为首版代码作者。运行时会把“退出码 0 但输出包含沙箱/步骤执行错误”判为失败。网络故障最多重试一次，401 等认证错误不重试。圆桌只更新一张进度卡；单 Agent 故障会退出本场，其余成员满足法定人数时继续。
