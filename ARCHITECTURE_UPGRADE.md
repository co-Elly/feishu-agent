# 多 Agent 架构无痛升级说明

本升级保持飞书命令、单入口 Bot、Hermes/反重力/Codex 角色和 SQLite 数据不变，所有数据库变更使用 `CREATE TABLE IF NOT EXISTS` 与增量列迁移。

## 新执行不变量

1. 新会议使用持久化 workflow 状态；旧任务没有会议确认回执时仍可读取，但不能批准、重试或执行。
2. `协作` 只能从会议开始；会议动态多轮讨论后必须经过老板拍板，必要时带着拍板续会，之后才询问是否开始协作。
3. 任务控制命令仅允许任务发起人执行。
4. 批准绑定批准人、飞书消息、计划哈希和约束哈希；执行前重新验签。
5. 反重力是首版代码作者，但固定为无工具模式：它输出 unified diff，由调度器做路径、类型、大小和批准范围校验后写入 staging；Codex 再在同一 staging 完善与验证。主工作区在验收前保持不变。
6. staging 越界或测试失败直接丢弃；语义复核通过后才合并。
7. 合并后回归失败自动回滚；进程在合并途中退出时，下次启动按 merge journal 恢复。
8. 工作区同时受线程锁、SQLite 跨进程租约和源码逃逸保护。
9. staging 和反重力服务配置位于主项目之外；控制脚本不复制进 staging。
10. 外部 PDF 报告任务走专用只读流程：本地确定性预审、全项目双快照、有界脱敏证据包、项目外 Codex 分析、唯一 DOCX、飞书文件交付，不进入源码合并。
11. 已批准代码写入任务遇服务重启时阻塞而不自动重放，并清理遗留租约；只读报告按生成/发送检查点幂等恢复。
12. 第三方执行器即使以退出码 0 返回沙箱错误，也会被运行时判为失败。
13. workflow ID、已完成会议 ID、确认人、确认消息和确认时间构成“开始协作”回执；创建、批准、重试和执行阶段均失败关闭。

## 运维

- 标准验证：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify.ps1`
- 计划重启：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\restart_bot.ps1 "升级原因"`
- 守护器不会把五分钟内的计划重启计入崩溃熔断。
- 服务日志与聊天审计日志达到 5 MB 后轮转，最多保留 10 份。

## 回退

新表和新列不会改变旧记录。代码回退后旧版本会忽略新增的 workflow、checkpoint、trace、lease 表；无需删除数据库。任务 staging 位于配置的外部 `execution_dir`；启动时同时扫描外部目录与旧版 `workspace/executions/`，恢复未完成合并。
