# Feishu Multi-Agent Workstation | 飞书多智能体协同工作站

[EN](#english) | [中文](#中文)

<a name="english"></a>
## English

A single-machine Python + SQLite + local-CLI + Feishu architecture that orchestrates **Hermes**, **Antigravity (Gemini)** and **Codex** through a Feishu group chat: persistent conversations, convergence-based roundtable meetings, two-stage approval collaboration, and strictly scoped long-term memory.

### Architecture

```
Feishu messages → Hermes Gateway (single inbound WebSocket)
    → ingress_bridge.py (loopback-only delegate bridge, Bearer random token)
        → bot.py (command routing / progress cards / approvals / file delivery)
            ├─ roundtable_engine.py   convergence meetings: stance extraction,
            │                         confidence-weighted consensus, blackboard, idempotency cache
            ├─ task_manager.py        persistent tasks: per-chat FIFO, cross-chat parallel, approvals
            ├─ swarm_orchestrator.py  read-only plan → approve → execute pipeline
            ├─ antigravity_first_pass.py  agy unified-diff patch validation & staging
            ├─ workflow_store.py      meeting-decision → execution state machine + decision ledger
            ├─ constraint_envelope.py system constraint envelope threaded through the whole chain
            └─ isolated_workspace.py + workspace_lease.py
                                      thread lock + SQLite lease + source-escape guard
```

### Key safety invariants

- **Collaboration cannot skip steps**: `协作 <goal>` must go meeting → dynamic multi-round convergence → owner decision → explicit `开始协作` confirmation → review task → `批准任务 <ID>` → execute.
- **Antigravity is first-pass author only**: fixed plan mode, no native write; outputs unified diff that the scheduler validates (paths, traversal, binaries, approved scope) into staging; Codex refines in the same staging; main workspace untouched until acceptance.
- **Constraint priority**: user hard constraints > owner decision > meeting consensus > agent suggestions.
- **Crash semantics**: code-write phase restarts as blocked (manual retry); read-only reports resume idempotently.

### Roundtable consensus v3

Agents self-report `立场：同意｜置信度：0.8` on the first line. Convergence requires the nominal rule (no dissent + at least one agree) **and** confidence-weighted agreement ratio ≥ 0.6; high-confidence opposition (≥ 0.7) vetoes. Speeches are broadcast to Feishu as a reply chain with automatic 1800-char chunking. Same-topic dedup (10 min window) prevents MCP-retry ghost meetings.

### Commands

| Command | Effect |
|---|---|
| `开会 [项目:X] <topic>` | roundtable meeting |
| `协作 [项目:X] <goal>` | full meeting→approval→execution chain |
| `任务列表` / `任务 <ID>` / `批准任务 <ID>` / `重试任务 <ID>` | task ops |
| `记住 [项目:X] <内容>` / `记忆列表` | scoped long-term memory |
| `状态` / `健康` / `深度健康` | service health |
| `周报` | 7-day success rates, engine latency, meeting stats |

### Setup

```powershell
copy config.example.json config.json   # fill in Feishu app_id/secret, API keys
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python bot.py
```

Run tests:

```powershell
scripts\verify.ps1    # fixed .tools\test-venv interpreter, pytest temp inside workspace\
```

See [ARCHITECTURE_UPGRADE.md](ARCHITECTURE_UPGRADE.md) for execution invariants and rollback notes.

---

<a name="中文"></a>
## 中文

单机 Python + SQLite + 本地 CLI + 飞书架构。通过飞书调用 Hermes、反重力（Gemini）和 Codex，支持持久普通对话、收敛式圆桌、二次审批协作与严格作用域长期记忆。

飞书事件只允许 Hermes gateway 建立一条入站长连接，再通过带随机令牌的 `127.0.0.1` 委托桥交给 `bot.py` 调度。

详细架构、安全不变量与命令说明见上方英文版（内容一致）。配置、启动与测试入口：

```powershell
copy config.example.json config.json   # 填入飞书 app_id/secret 与各 API key
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python bot.py

scripts\verify.ps1                     # 统一测试入口
```
