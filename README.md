# agent-ops

让 Codex / Claude Code 像小团队一样协作的 Agent Skill：主 Agent 负责需求、方向和验收，被委派的 Agent 作为能力相当的同事负责实现，并提供查找历史会话、交接恢复和远程控制 Codex 会话的脚本。

> 前提：本 skill 假设主 Agent 与被委派的 Agent 能力相当，典型场景是 Claude Opus 调度 Codex 的前沿模型。用能力差距较大的模型（如 Opus 调度 Haiku）时，放权与精简任务包的规则并不合适。

## 功能与优势

- **按决定权分工**：主 Agent 决定“做什么、做成什么样”，任务 owner 决定“怎么做”。主 Agent 不替 owner 做调查、不规定步骤，owner 不改已拍板的方向。
- **默认放权，减少往返**：用户已授权的范围（实现、提交、部署、生产验证）整体交给 owner 一次做完，只有删除生产数据、轮换凭据等需要新授权的事项才停下汇报。
- **精简的任务包**：任务包只写 owner 拿不到的信息，即意图、决定、完成标准和授权；仓库里查得到的内容由 owner 自己查。交付摘要使用固定结构。
- **并行开发不打架**：按依赖和冲突面划分交付边界，一个边界一个 owner，独立 worktree，由主 Agent 语义合并后统一验证。
- **以证据验收**：用提交、diff、测试、产物和实物截图核实结果，不以 Agent 自述为准；历史对话只当作不可信输入。
- **会话发现**：按 cwd、关键词跨 Codex 与 Claude 历史查找会话，生成 Claude JSONL 摘要与时间线，并自动标出疑似密钥。
- **交接与恢复**：结构化交接包，明确何时继续原 owner、何时重试、何时接管。
- **Codex 会话控制**：`codex_ctl.py` 通过 `codex app-server daemon` 启动、追加（steer）、打断、等待 Codex 会话，支持空闲看门狗、上游过载自动打断和远程机器；只依赖 Python 标准库，Linux 与 Windows 通用。

## 让 Agent 代为安装

把下面这段话发给你的 Codex 或 Claude Code：

```text
安装 https://github.com/shellus/agent-ops 到本地 skills：
- Claude Code：git clone 到 ~/.claude/skills/agent-ops
- Codex：git clone 到 ~/.agents/skills/agent-ops
然后在全局指令（Claude Code 为 ~/.claude/CLAUDE.md，Codex 为 ~/.codex/AGENTS.md）中加入：

## 多 Agent 协作
- 分解或委派任务、并行开发、监督或接手其他 Agent、查找或总结历史会话时，先加载 agent-ops skill，按其中的分工模型、任务包与交付摘要格式执行。
- （可选，已安装 Codex 时）委派开发任务默认交给 Codex，用 agent-ops 的 scripts/codex_ctl.py 启动和监督。

安装后确认 SKILL.md 位于 skills/agent-ops/SKILL.md，并运行 python <skill-dir>/scripts/codex_ctl.py --help 验证脚本可用。
```

## 手动安装

```bash
# Claude Code
git clone https://github.com/shellus/agent-ops ~/.claude/skills/agent-ops
# Codex
git clone https://github.com/shellus/agent-ops ~/.agents/skills/agent-ops
```

更新：在安装目录执行 `git pull`。

## 依赖

- Python 3.10+（脚本只用标准库）
- 控制 Codex 会话需要支持 `codex app-server daemon` 的 Codex CLI

## 目录

```text
SKILL.md                            入口：角色模型、工作流选择、共同原则
references/team-orchestration.md    分解、委派、worktree、验收与返工
references/session-discovery.md     查找与总结当前/历史会话
references/handoff-and-recovery.md  交接、重试与接管
references/codex-control.md         Codex 会话控制
scripts/                            会话查询、JSONL 摘要/时间线、codex_ctl.py
```

## 许可

[MIT](LICENSE) © shellus
