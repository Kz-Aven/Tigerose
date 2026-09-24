# Avent Agent — Project Instructions

本文件定义 **Lead Agent、子智能体、自治队友** 在本仓库中的行为规范。运行时由 macOS App（`server/runtime/turn.py`）自动加载并注入 system prompt；打包分发时随 `Resources/runtime/AGENTS.md` 一并交付；Cursor 等 IDE 也会读取本文件。

架构、安装与实现细节见 **README.md**。

## Conventions

- **Act, don't explain.** 优先执行工具调用，少写冗长说明。
- 使用已有工具接口，**不要**用 `bash` 模拟 `read_file` / `write_file` 等工具。
- 改代码前读相关文件；只改与任务相关的路径。
- 多文件阅读、代码搜索、多步骤任务：用 `task` 委派子智能体，并同步更新任务板。
- 复杂工作流：先 `get_skill` 加载 `skills/**/SKILL.md`，再执行。
- 会话内短期待办用 `todo_write`；跨会话持久工作用任务板（`create_task` / `claim_task` / `complete_task`）。
- 慢命令（install/build/test 等）或对 `bash` 设置 `run_in_background: true`。
- 上下文过长时使用 `compact`，或依赖自动压缩。

## Task board

1. **先查后建**：开工前查看已有任务快照；**不要**重复创建相同工作。
2. **恢复 in_progress**：已有进行中的任务应继续，不要 duplicate。
3. **卡住时**：`release_task` 再 `claim_task`。
4. **仅对新工作** `create_task`。
5. **委派**：多步/多文件用 `task`；过程中更新 `claim_task` / `complete_task` / `fail_task`。
6. 任务绑定 worktree 时，**必须**在该 worktree 目录内读写与执行 bash。

## Memory

- **尊重**运行时从 `.memory/` 注入的用户偏好与项目事实。
- 用户说「记住」或表达明确偏好时，可要求写入记忆。
- **分工**：本文件 = 仓库级稳定规范；`.memory/` = 动态偏好。

## Skills

- 遇到专用流程（PR、部署、审查清单等）优先 `get_skill` 再动手。
- 不要把长流程步骤堆进本文件；写到 `skills/**/SKILL.md`。

## Do / Don't

**Do**

- 改后做最小必要验证（compile / 相关测试）。
- 用工具 API 而非 shell 包装。
- 处理 `<task_notification>` 与 `[Inbox]` 消息后再继续。

**Don't**

- 不要重复创建已有 pending/in_progress 任务。
- 不要用 bash 调用已注册的工具名。
- 不要在未批准计划前让队友执行大规模变更。
- 不要 force push / 硬重置 git（除非用户明确要求）。
- 不要修改与本任务无关的文件。
- 不要提交 `.env`、密钥、token；不要把 secret 写入 `.memory/`。

## Verification

1. `python3 -m py_compile server/app.py`（或实际变更的主模块）
2. 若用户提供了测试命令，运行并通过
3. 确认无新增明显语法 / linter 错误

## Delegation

委派子智能体或队友时，在任务描述中写清：

- 目标与完成标准
- 允许修改的路径或 worktree 名
- 需要运行的验证命令
- 是否需要 plan 审批或完成后 message Lead

子智能体完成委派后**只返回简洁摘要**，禁止再 spawn；不要把完整日志贴回主对话。

## Security

- 不提交 secret；不将凭据写入代码或记忆。
- 不 force push / 硬重置（除非用户明确要求）。
- 破坏性 MCP 操作与 workspace 外路径访问需用户确认（非交互模式默认拒绝）。



# 终极原则

你必须在回答前，先向我提问。要求一次只问一个问题，根据我的回答继续追问，直到你有 95%的信心，完全理解我的真实需求、目标和限制条件时，再给出最终方案及落地风险。
