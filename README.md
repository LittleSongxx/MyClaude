# MyClaude

MyClaude 是一个使用 Python 3.11+ 实现的终端 Coding Agent。它提供 Textual TUI、非交互 CLI 和带认证的本地 Remote UI，核心目标是把流式模型调用、工具执行、权限控制、长上下文恢复和多 Agent 协作放在同一条可测试的运行链上。

当前版本为 `0.3.0`。核心执行链已经完整，自动压缩、会话恢复、Skills、Hooks、MCP、子 Agent、Team、Worktree 和操作系统沙箱等能力均已有实现；项目仍处于主动开发阶段，更适合作为可实际使用和继续演进的个人 Coding Agent，而不是强隔离、无人值守的生产执行平台。

> MyClaude 是独立项目，不是 Anthropic 官方产品，也不承诺兼容 Claude Code 的命令、配置或会话格式。

## 核心能力

- 支持 Anthropic Messages、OpenAI Responses 和 OpenAI-compatible Chat Completions 三种流式协议。
- 使用统一事件流处理文本、思考内容、工具调用、权限请求、Hooks、重试、压缩和用量统计。
- 内置文件读写、精确编辑、删除、Glob、Grep、Bash、Worktree、Skills、Agent 和 Team 工具。
- 工具调用仅在模型响应完整结束且 stop reason 合法后执行；写入、命令和 Agent 工具保持顺序执行。
- 文件写入采用原子替换；编辑和删除要求近期读取状态，避免静默覆盖外部并发修改。
- 支持大型工具结果落盘、引用感知清理、主动压缩和真实 API context overflow 后的一次响应式压缩重试。
- 支持 schema 版本化的 JSONL 会话、压缩边界、文件历史、回退、项目记忆和恢复上下文。
- 支持 workspace trust、四种权限模式、用户/项目规则、危险命令硬拦截和可选 OS 级 Bash 沙箱。
- 支持共享用量账本、模型调用用途分类、成本估算以及 turn、时间、token、成本四类运行上限。
- 流式输出期间的新消息会排队进入下一轮；可取消的 Bash/Agent 调用可被转向消息打断，文件写入不会被中途取消。

## 架构

三个入口通过 `RuntimeAssembler` 安装共同能力，并共享同一个 `CoreRuntime`：

```text
Textual TUI       myclaude -p       Remote UI
      \                |                /
               RuntimeAssembler
                       |
                  CoreRuntime
                       |
        Agent + Conversation + LLMClient
                       |
      ToolRegistry + PermissionChecker
                       |
 Context / Usage / Memory / Session / Worktree
```

| 模块 | 职责 |
| --- | --- |
| `myclaude/agent.py` | Agent 循环、转向消息、重试、工具调度和运行上限 |
| `myclaude/client.py` | 三类 Provider 客户端、错误分类和用量上报 |
| `myclaude/runtime.py` | 与界面无关的核心运行时 |
| `myclaude/runtime_assembler.py` | Skills、Agent、Team、ToolSearch 和 MCP 的统一装配 |
| `myclaude/model_capabilities.py` | 上下文窗口、默认输出和 thinking 模式注册表 |
| `myclaude/context/` | Token 预算、大结果持久化、压缩与恢复状态 |
| `myclaude/permissions/` | 权限模式、规则、路径边界和危险命令检测 |
| `myclaude/usage.py` | 线程安全用量账本、成本估算和运行限制 |
| `myclaude/memory/` | 会话、压缩边界、指令、项目记忆和记忆整合 |
| `myclaude/trust.py` | 仓库根目录解析和 workspace trust 持久化 |
| `myclaude/app.py` | Textual TUI |
| `myclaude/remote.py` | 带随机令牌认证的本地浏览器 UI |

## 三种入口

基础运行时和标准工具面已经统一，差异主要来自是否具备交互式 UI：

| 能力 | TUI | `-p` | Remote UI |
| --- | :---: | :---: | :---: |
| 文件、搜索、Bash、Worktree | 是 | 是 | 是 |
| 自动/响应式压缩 | 是 | 是 | 是 |
| Hooks、Skills、MCP | 是 | 是 | 是 |
| 子 Agent 与 Team 工具 | 是 | 是 | 是 |
| 安装 Skill | 是 | 否 | 否 |
| AskUser 与交互式 Plan UI | 是 | 否 | 否 |
| 项目自定义斜杠命令 | 是 | 不适用 | 是 |
| 会话持久化 | 是 | 单次运行 | 是 |
| 完整会话恢复界面 | 是 | 否 | 否 |
| 完整斜杠命令支持 | 是 | 否 | 部分 |

自动化调用建议使用 `-p --output-format stream-json`。需要人工权限确认、会话恢复、Skill 安装或完整 Plan 流程时使用 TUI。

## 安装

### Conda

```bash
conda create -n claude python=3.11 -y
conda activate claude
python -m pip install -e .
```

### venv

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

验证入口：

```bash
myclaude --version
myclaude --help
```

## Workspace Trust

项目配置可以启动 stdio MCP 进程和 command Hooks，因此 MyClaude 在加载项目级配置和扩展前要求信任当前仓库：

- 首次交互启动会显示仓库根目录，并要求输入 `yes`。
- `-p`、Remote 或非 TTY 启动在未信任时直接退出，不会自动接受项目内容。
- `--trust-workspace` 显式信任当前仓库根目录。
- `--no-project-config` 仅使用用户级配置和扩展，不加载项目指令、配置、权限规则、Skills、Agents、命令、记忆或 Worktree 恢复状态。
- `--revoke-workspace-trust` 撤销当前仓库的信任记录并退出。

信任记录保存在 `~/.myclaude/trusted_workspaces.json`，使用 schema 版本和规范化仓库根路径，写入权限设为 `0600`。信任是持久决定；仓库内容发生变化后不会自动撤销，需要调用者自行复核或显式 revoke。

## 配置

信任项目后，配置按以下顺序合并，后面的显式字段覆盖前面的字段：

1. `~/.myclaude/config.yaml`
2. `<work-dir>/.myclaude/config.yaml`
3. `<work-dir>/.myclaude/config.local.yaml`

未信任或使用 `--no-project-config` 时，只读取第一层。可从仓库示例开始：

```bash
mkdir -p .myclaude
cp config.example.yaml .myclaude/config.yaml
```

最小配置：

```yaml
providers:
  - name: anthropic
    protocol: anthropic
    base_url: https://api.anthropic.com
    model: claude-sonnet-4-6
    thinking: true
    context_window: 200000
    max_output_tokens: 64000
    input_cost_per_million: 3.0
    output_cost_per_million: 15.0

permission_mode: default

run_limits:
  max_turns: 0
  max_wall_time_seconds: 0
  max_total_tokens: 0
  max_cost_usd: 0
```

所有 `run_limits` 的 `0` 都表示禁用。Token 和成本账本会在父 Agent、子 Agent 和 compact、记忆召回/提取/整理、会话摘要等辅助调用之间共享。`max_cost_usd` 只有在 Provider 配置了价格后才有实际约束意义；该数值是估算，不是供应商账单。

建议通过环境变量提供密钥：

```bash
export ANTHROPIC_API_KEY="your-key"
export OPENAI_API_KEY="your-key"
```

| `protocol` | 后端 API |
| --- | --- |
| `anthropic` | Anthropic Messages |
| `openai` | OpenAI Responses |
| `openai-compat` | OpenAI-compatible Chat Completions |

多个 Provider 可同时配置。TUI 提供选择界面；`-p` 和 Remote 使用列表中的第一个 Provider。Anthropic 协议会尽力从模型端点获取 context window，失败后回退到集中维护的模型能力表和保守默认值。Claude Sonnet/Opus 4.6 在开启 thinking 时使用 adaptive thinking。

更多配置项见 [config.example.yaml](config.example.yaml)。

## 使用

交互式 TUI：

```bash
myclaude
myclaude --mode acceptEdits
myclaude --trust-workspace
```

非交互运行：

```bash
myclaude -p "分析当前仓库并修复失败测试"
myclaude -p "只分析问题，不修改文件" --mode plan
myclaude -p "检查项目结构" --output-format stream-json
```

非交互模式无法获得人工确认，因此所有 `ask` 权限请求都会失败关闭。只有调用方显式选择 `--mode bypassPermissions` 时，常规写入和命令才会自动放行。

Remote UI：

```bash
myclaude --remote
myclaude --remote --remote-addr 127.0.0.1 --remote-port 18888
```

终端会打印带随机令牌的 URL。Remote 同一时间只接受一个已认证控制连接；默认没有 TLS，不应直接绑定公网地址。

## 权限与安全

| 模式 | 读取 | 写入 | 命令 |
| --- | --- | --- | --- |
| `default` | 允许 | 询问 | 询问 |
| `acceptEdits` | 允许 | 允许 | 询问 |
| `plan` | 允许 | 拒绝 | 拒绝 |
| `bypassPermissions` | 允许 | 允许 | 允许 |

每个文件和搜索工具自行声明权限匹配内容与路径范围，权限检查器不再依赖集中式参数猜测。子 Agent 请求的权限模式会被限制在父 Agent 权限以内，并继承父级规则和危险命令检测器。

权限规则可放在用户或可信项目中：

```yaml
- rule: "Bash(git status*)"
  effect: allow
- rule: "Bash(git push*)"
  effect: deny
```

支持 `allow`、`ask`、`deny`。灾难性命令检测先于规则、会话放行和 permission mode，可识别拆分选项、wrapper、绝对程序路径等常见变体，例如递归删除根目录、根目录递归 chmod/chown、`find / -delete` 和磁盘设备破坏操作。

结构化文件工具不能修改 `.myclaude/config.yaml`、权限文件和受保护的 Skill 路径，即使处于 `bypassPermissions`。这不等于 Bash 沙箱：未启用 OS 沙箱时，Shell 子进程仍拥有当前用户权限，也无法通过有限的命令解析覆盖所有等价写法。

可选沙箱支持 Linux bubblewrap 和 macOS Seatbelt。只有沙箱实际可用时，`sandbox.auto_allow` 才会成为命令自动放行的兜底条件。

## Hooks

Hooks 支持 command、prompt 和 HTTP action，以及生命周期条件、once、async 和 pre-tool reject。command Hook 会通过 stdin 接收 JSON，同时在 `MYCLAUDE_HOOK_CONTEXT` 中收到相同内容：

```json
{
  "event": "pre_tool_use",
  "tool_name": "WriteFile",
  "tool_args": {"file_path": "src/main.py"},
  "file_path": "src/main.py",
  "message": "",
  "error": ""
}
```

pre-tool command Hook 可输出结构化决定：

```json
{"decision": "deny", "reason": "generated files are read-only"}
```

`decision` 支持 `allow` 和 `deny`。普通文本输出仍被记录为通知。`agent` Hook action 目前尚未实现，会明确返回失败而不会伪报成功。

## 扩展目录

可信项目可提供：

```text
.myclaude/skills/
.myclaude/agents/
.myclaude/commands/
```

项目指令支持从 Git 根目录到当前工作目录逐层加载 `MYCLAUDE.md` 和 `AGENTS.md`，并支持 `MYCLAUDE.local.md`。用户级指令位于 `~/.myclaude/MYCLAUDE.md` 和 `~/.myclaude/AGENTS.md`。

MCP 工具统一命名为 `mcp__<server>__<tool>`。TUI、`-p` 和 Remote 都会连接已配置服务器并把服务器 instructions 注入对话。

## 会话与数据

会话记录使用 `SESSION_SCHEMA_VERSION = 1`。未带版本的早期记录会在内存中迁移，未来版本会被安全拒绝。自动压缩、API overflow 恢复和 Remote 手动 `/compact` 都会持久化结构化 `compact_boundary`，其中包含摘要和需要原样保留的近期消息。

项目运行状态默认位于 `.myclaude/`：

| 路径 | 内容 |
| --- | --- |
| `.myclaude/sessions/` | JSONL 会话、metadata 和压缩边界 |
| `.myclaude/session/tool-results/` | 超预算工具结果与替换记录 |
| `.myclaude/memory/` | 项目记忆 |
| `.myclaude/file-history/` | 文件修改历史与回退数据 |
| `.myclaude/worktrees/` | Worktree 状态 |
| `.myclaude/plans/` | Plan 文档 |
| `.myclaude/debug.log` | 当前运行日志 |

仓库 `.gitignore` 默认忽略 `.myclaude/`。其中可能包含源码片段、会话和私有上下文，不应提交。

## 已知限制

- `bypassPermissions` 不是安全沙箱；Shell 的完整语义无法靠模式检测可靠证明安全。
- Hook 的 `agent` action 仍是占位实现。
- Remote 支持核心运行和会话持久化，但部分 UI 型斜杠命令及完整会话恢复仍只在 TUI 可用。
- 模型能力表和未知模型 fallback 需要持续维护；新 Provider 建议显式配置窗口和输出上限。
- tmux/iTerm2 Team 后端依赖本机终端、Git 和 Worktree 条件；`in-process` 是最可移植模式。
- 默认测试使用模拟 Provider 和 MCP，不替代针对实际供应商端点的集成测试。
- 仓库目前没有 `LICENSE` 文件，公开分发或复用前需要补充明确许可证。

## 开发与验证

使用 uv：

```bash
uv sync --group dev
uv run pytest -q
uv run ruff check .
```

使用 Conda 环境：

```bash
conda activate claude
pytest -q
ruff check .
```

测试覆盖 Provider 序列化、Agent 循环、运行预算、权限、危险命令、文件工具、上下文压缩、会话迁移、记忆、Worktree、Hooks、MCP、Skills、子 Agent、Team、workspace trust 和三入口运行时装配。

## 项目来源

MyClaude 是自主开发的 Coding Agent。设计研究参考了 Claude Code、mini-swe-agent 和 Mistral Vibe，它们不是本项目的运行时依赖。部分源码文件保留了原始教学资料的来源标记。
