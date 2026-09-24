# Agent-Eval-Evaluator

WorkBuddy 单专家插件，用于智能体测试集设计、任务执行链记录、循证评分和多轮能力评估报告。

## Structure

```text
agent-eval-evaluator/
├── .codebuddy-plugin/plugin.json
├── agents/agent-eval-evaluator.md
├── avatars/expert.png
└── skills/agent-evaluation/SKILL.md
```

## Capabilities

- 生成冒烟、回归、能力与对抗测试集。
- 执行经用户授权的被测任务，并记录可观察执行链。
- 按预制六维 100 分量表输出严格评分 JSON。
- 汇总多轮评分 JSON，输出风险、趋势、改进优先级与产品决策。

不声明外部 MCP 或连接器依赖；运行时可用工具由 WorkBuddy 系统统一分配。
