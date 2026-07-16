# MyClaude

> 一个运行在终端里的 Coding Agent —— 把流式模型调用、工具执行、权限控制、长上下文恢复和多 Agent 协作，放在同一条可测试的运行链上。

MyClaude 用 Python 3.11+ 实现，提供三种入口：交互式 Textual TUI、非交互 CLI（`-p`）以及带令牌认证的本地浏览器 UI（`--remote`）。三种入口共享同一套核心运行时与工具面，差异只来自是否具备交互式界面。

它兼容 Anthropic、OpenAI 及 OpenAI-compatible 三类流式协议，内置文件读写、精确编辑、Shell、代码搜索、子 Agent、Team、Git Worktree 隔离、Skills、Hooks 与 MCP，并带有自动上下文压缩、会话恢复、项目记忆和可选的操作系统级命令沙箱。

> [!NOTE]
> MyClaude 是一个独立的实验性项目，**不是 Anthropic 官方产品**，也不承诺兼容 Claude Code 的命令、配置或会话格式。它更适合作为可实际使用、可持续演进的个人 Coding Agent，而不是强隔离、无人值守的生产执行平台。

---

## 目录

- [核心特性](#核心特性)
- [架构](#架构)
- [三种入口对比](#三种入口对比)
- [安装](#安装)
- [快速开始](#快速开始)
- [配置](#配置)
- [权限与安全](#权限与安全)
- [工具集](#工具集)
- [扩展机制](#扩展机制)
- [上下文、会话与记忆](#上下文会话与记忆)
- [斜杠命令](#斜杠命令)
- [数据与目录布局](#数据与目录布局)
- [开发与测试](#开发与测试)
- [评测](#评测)
- [已知限制](#已知限制)
- [致谢](#致谢)

---

## 核心特性

- **多协议流式**：`anthropic`（Messages）、`openai`（Responses）、`openai-compat`（Chat Completions）三类后端，统一的错误分类与用量上报。支持 thinking / reasoning、Anthropic 提示缓存与加密 reasoning 续写。
- **统一事件流**：文本、思考、工具调用、权限请求、AskUser、Hooks、重试、压缩、用量都以同一套事件驱动，TUI / Headless / Remote 三端复用。
- **稳健的工具执行**：工具仅在模型响应完整结束、`stop_reason` 合法后才执行。只读工具按批并发，写入 / Bash / Agent 等非并发安全工具保持顺序独占。文件写入原子替换，编辑与删除要求近期读取状态以避免静默覆盖外部并发修改。
- **长上下文恢复**：两层上下文管理——超预算工具结果落盘 + 引用感知清理，以及接近窗口上限时的对话摘要压缩；真实 API context overflow 后还能做一次响应式压缩重试。压缩边界持久化，支持会话恢复。
- **权限与安全分层**：Workspace Trust、四种权限模式、用户/项目/本地三级规则、灾难性命令硬拦截，以及可选的 bubblewrap / Seatbelt OS 级命令沙箱。
- **多 Agent 协作**：后台子 Agent、对话 fork、Git Worktree 隔离、基于共享任务与信箱的 Team，以及可选的 Coordinator 编排模式。
- **可扩展**：Skills、自定义子 Agent、自定义斜杠命令、生命周期 Hooks（command / prompt / http），以及 stdio 与 streamable HTTP 两类 MCP 服务器。
- **运行预算**：线程安全的共享用量账本，支持成本估算与 turn / 墙钟时间 / token / 成本四类运行上限；预算在主 Agent、子 Agent、压缩、记忆与摘要调用之间共享。
- **可操控**：流式输出期间到达的新消息排队进入下一轮；可取消的 Bash / Agent 调用可被转向消息打断，而文件写入不会被中途取消。

---

## 架构

三个入口通过 `RuntimeAssembler` 安装共同能力，并共享同一个 `CoreRuntime`：

```text
   Textual TUI          myclaude -p           Remote UI
        \                    |                    /
         \                   |                   /
                     RuntimeAssembler
                            |
                       CoreRuntime
                            |
            Agent + Conversation + LLMClient
                            |
              ToolRegistry + PermissionChecker
                            |
     Context · Usage · Memory · Session · Worktree
```

| 模块 | 职责 |
| --- | --- |
| `myclaude/agent.py` | Agent 主循环：事件流、重试、转向消息、工具调度、运行上限 |
| `myclaude/client.py` | 三类 Provider 客户端、错误分类、用量与提示缓存 |
| `myclaude/runtime.py` | 与界面无关的核心运行时装配 |
| `myclaude/runtime_assembler.py` | Skills / Agent / Team / ToolSearch / MCP 的统一安装 |
| `myclaude/model_capabilities.py` | 上下文窗口、默认输出与 thinking 模式注册表 |
| `myclaude/context/` | Token 预算、大结果落盘、对话压缩与恢复状态 |
| `myclaude/permissions/` | 权限模式、规则引擎、路径边界、危险命令检测 |
| `myclaude/usage.py` | 线程安全用量账本、成本估算、运行限制 |
| `myclaude/memory/` | 项目记忆、召回、提取、整合与会话状态 |
| `myclaude/worktree/` | Git Worktree 创建、进入/退出、清理与恢复 |
| `myclaude/teams/` | Team、共享任务、信箱、Coordinator 编排 |
| `myclaude/hooks/` | 生命周期事件、条件、执行器与引擎 |
| `myclaude/sandbox/` | bubblewrap（Linux）/ Seatbelt（macOS）OS 级沙箱 |
| `myclaude/trust.py` | 仓库根解析与 Workspace Trust 持久化 |
| `myclaude/app.py` | Textual TUI |
| `myclaude/remote.py` | 令牌认证的本地浏览器 UI |

---

## 三种入口对比

| 能力 | TUI | `-p` | Remote UI |
| --- | :---: | :---: | :---: |
| 文件 / 搜索 / Bash / Worktree | ✅ | ✅ | ✅ |
| 自动 & 响应式压缩 | ✅ | ✅ | ✅ |
| Hooks / Skills / MCP | ✅ | ✅ | ✅ |
| 子 Agent 与 Team 工具 | ✅ | ✅ | ✅ |
| 项目记忆与恢复上下文 | ✅ | ✅ | ✅ |
| 交互式权限确认 | ✅ | ❌（`ask` 一律拒绝） | ✅ |
| AskUser 与交互式 Plan UI | ✅ | ❌ | ✅ |
| Skill 安装 | ✅ | ❌ | ❌ |
| 项目自定义斜杠命令 | ✅ | 不适用 | ✅ |
| 会话持久化 | ✅ | 单次运行 | ✅ |
| 完整会话恢复界面 | ✅ | ❌ | 部分 |
| 完整斜杠命令支持 | ✅ | ❌ | 部分 |

- 自动化 / CI 场景建议 `myclaude -p --output-format stream-json`，输出稳定的 NDJSON 事件与退出码。
- 需要人工权限确认、Plan 审批、AskUser、Skill 安装或完整会话恢复时使用 TUI。

---

## 安装

要求 Python **3.11+**。项目使用 `hatchling` 构建，可用 uv、conda 或 venv 安装。

### uv（推荐）

```bash
uv sync --group dev
uv run myclaude --version
```

### conda

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

---

## 快速开始

```bash
# 1. 准备配置
mkdir -p .myclaude
cp config.example.yaml .myclaude/config.yaml

# 2. 提供 API Key（推荐用环境变量）
export ANTHROPIC_API_KEY="your-key"

# 3. 启动交互式 TUI（首次会要求信任当前仓库）
myclaude
```

其他常见用法：

```bash
# 非交互执行，直接打印结果
myclaude -p "分析当前仓库并修复失败的测试"

# 只做分析，不修改文件
myclaude -p "定位这个 bug 的根因，不要改代码" --mode plan

# 输出结构化事件流，便于脚本消费
myclaude -p "检查项目结构" --output-format stream-json

# 自动接受文件编辑（命令仍需确认）
myclaude --mode acceptEdits

# 启动本地浏览器 UI
myclaude --remote
```

---

## 配置

信任项目后，配置按以下顺序合并，**后面的显式字段覆盖前面的**：

1. `~/.myclaude/config.yaml`（用户级）
2. `<work-dir>/.myclaude/config.yaml`（项目级）
3. `<work-dir>/.myclaude/config.local.yaml`（项目本地覆盖）

未信任仓库或使用 `--no-project-config` 时，只读取第一层。

最小配置示例：

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

| `protocol` | 后端 API |
| --- | --- |
| `anthropic` | Anthropic Messages |
| `openai` | OpenAI Responses |
| `openai-compat` | OpenAI-compatible Chat Completions（vLLM / Ollama / Together / Azure 等） |

要点：

- API Key 优先取配置中的 `api_key`，否则回退到环境变量（`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`），支持 `${VAR}` 展开。
- 可同时配置多个 Provider。TUI 提供选择界面；`-p` 与 Remote 使用列表中的第一个。
- 所有 `run_limits` 的 `0` 表示禁用。`max_cost_usd` 只有在 Provider 配置了价格时才有约束意义，且是**估算值**，不等于供应商账单。
- Anthropic 协议会尽力从模型端点获取 context window，失败后回退到集中维护的模型能力表与保守默认值。
- 完整配置项（worktree、sandbox、mcp_servers、hooks、fork、coordinator 等）见 [config.example.yaml](config.example.yaml)。

---

## 权限与安全

### Workspace Trust

项目级配置可以启动 stdio MCP 进程和 command Hooks，因此加载项目配置与扩展前，MyClaude 要求先信任当前仓库：

- 首次交互启动会显示仓库根目录并要求输入 `yes`。
- `-p`、Remote 或非 TTY 启动在未信任时**直接退出**，不会自动接受项目内容。
- `--trust-workspace` 显式信任当前仓库根目录。
- `--no-project-config` 仅使用用户级配置与扩展，不加载任何项目指令、配置、规则、Skills、Agents、命令、记忆与 Worktree 恢复状态。
- `--revoke-workspace-trust` 撤销当前仓库信任并退出。

信任记录写入 `~/.myclaude/trusted_workspaces.json`（schema 版本化、规范化根路径、权限 `0600`、原子写）。信任是持久决定，仓库内容变化后不会自动撤销。

### 权限模式

| 模式 | 读取 | 写入 | 命令 |
| --- | :---: | :---: | :---: |
| `default` | 允许 | 询问 | 询问 |
| `acceptEdits` | 允许 | 允许 | 询问 |
| `plan` | 允许 | 拒绝 | 拒绝 |
| `bypassPermissions` | 允许 | 允许 | 允许 |

子 Agent 的权限模式会被限制在父 Agent 之内（`plan < default < acceptEdits < bypass`），并继承父级规则与危险命令检测。

### 权限规则

规则可放在用户或可信项目中，采用 `ToolName(pattern)` 语法，支持 `allow` / `ask` / `deny`：

```yaml
- rule: "Bash(git status*)"
  effect: allow
- rule: "Bash(git push*)"
  effect: deny
```

三级规则（用户 → 项目 → 本地）按序评估，同级内后定义者优先。

### 检查器优先级

权限检查大致按如下顺序收敛：**灾难性命令硬拦截 → 规则 deny/ask → 受保护配置写入拦截 → 路径边界 → 规则 allow → plan 模式白名单 → 只读安全命令放行 → OS 沙箱兜底放行 → 会话级放行 → 模式矩阵兜底**。

- **灾难性命令检测**先于一切规则与模式生效。它会解开 `sudo`/`env`/`nice`/`timeout` 等包装，识别递归删根、根目录递归 `chmod/chown`、`find / -delete`、`dd of=/dev/…`、fork bomb、`curl | sh` 等常见变体。
- **路径沙箱**把读写限制在项目根 + 临时目录内，并对 `.myclaude/config.yaml`、权限文件、`skills/` 等路径**硬拒绝写入**——即便处于 `bypassPermissions`。
- 可选 **OS 级沙箱**（Linux `bubblewrap` / macOS Seatbelt）为 Bash 提供内核级隔离。仅当沙箱在本机实际可用时，`sandbox.auto_allow` 才会成为命令自动放行的兜底条件。

> [!WARNING]
> `bypassPermissions` **不是安全沙箱**。未启用 OS 沙箱时，Shell 子进程仍拥有当前用户权限，有限的命令解析也无法覆盖所有等价写法。请只在受控环境中使用。

---

## 工具集

内置工具（部分按上下文注册，如 Team / Worktree / Plan 模式相关工具）：

| 类别 | 工具 |
| --- | --- |
| 文件 | `ReadFile`、`WriteFile`、`EditFile`、`DeleteFile` |
| 搜索 | `Glob`、`Grep` |
| 执行 | `Bash` |
| 子 Agent | `Agent` |
| Team | `TeamCreate`、`TeamDelete`、`SendMessage`、`TaskCreate`、`TaskUpdate`、`TaskGet`、`TaskList` |
| Worktree | `EnterWorktree`、`ExitWorktree` |
| Skills | `LoadSkill`、`InstallSkill` |
| 系统 | `ToolSearch`、`AskUserQuestion`、`ExitPlanMode`、`SyntheticOutput` |

其中：

- `ReadFile` 返回带行号内容并缓存文件状态；`WriteFile` / `EditFile` / `DeleteFile` 要求近期读取状态、原子写入并快照进文件历史。
- `Bash` 合并 stdout/stderr、按进程组超时终止、输出上限 1 MB，可被转向消息取消，并可选套 OS 沙箱。
- `Glob` / `Grep` 是只读、并发安全工具，自动跳过 `.git` / `.venv` / `node_modules` / `__pycache__` 等目录。
- `ToolSearch` 用于按需加载「延迟工具」的 schema，控制上下文里的工具描述规模。

---

## 扩展机制

可信项目可提供以下目录（用户级同名目录位于 `~/.myclaude/` 下，项目级优先）：

```text
.myclaude/skills/      # Skills
.myclaude/agents/      # 自定义子 Agent
.myclaude/commands/    # 自定义斜杠命令
```

### 子 Agent

Markdown + YAML frontmatter（`name`、`description`、`tools`、`model`、`maxTurns`、`permissionMode`、`background`、`isolation` 等）。加载优先级：项目 > 用户 > 内置。内置类型：

- `general-purpose` —— 全工具、独立上下文的通用体。
- `Explore` —— 只读检索（禁用写工具）。
- `Plan` —— 只读架构师，输出实现计划与关键文件。
- `Verification` —— 只读缺陷猎手，后台运行，输出 `VERDICT`（需 `enable_verification_agent`）。

`subagent_type` 留空且开启 `enable_fork` 时，会 **fork 当前对话**（继承完整历史，后台运行）。

### Team 与 Coordinator

`TeamManager` 在 `~/.myclaude/teams/<slug>/` 下维护共享任务与文件信箱，每个成员运行在独立 Git Worktree 中。后端支持 `in-process`（最可移植，默认）、`tmux`、`iterm2`。开启 Coordinator 模式后，主 Agent 作为编排者，通过 `Agent` / `SendMessage` / `Task*` 委派给 worker。

### Skills

可复用的提示包，支持单文件 `.md` 或 `skill.yaml` + `prompt.md` 目录形式，`mode` 可选 `inline`（注入当前对话）或 `fork`（在新 Agent 中执行）。支持 `$ARGUMENTS` 替换，`InstallSkill` 可从 GitHub 类 URL 安装到 `~/.myclaude/skills/`。

### Hooks

生命周期事件（`session_start/end`、`turn_start/end`、`pre_tool_use`、`post_tool_use`、`pre_send`、`post_receive` 等）上挂载动作，支持条件表达式、`once`、`async`、pre-tool reject。动作类型：

- `command` —— 子进程，通过 stdin 与 `MYCLAUDE_HOOK_CONTEXT` 环境变量收到 JSON 上下文；占位符经 `shlex.quote` 转义防注入。
- `prompt` —— 注入提示消息。
- `http` —— HTTP 回调。
- `agent` —— **尚未实现，会显式返回失败**而非伪报成功。

`pre_tool_use` 的 command Hook 可输出结构化决定阻断工具调用：

```json
{"decision": "deny", "reason": "generated files are read-only"}
```

### MCP

支持 **stdio**（command/args/env）与 **streamable HTTP**（url/headers）两类服务器。工具统一命名为 `mcp__<server>__<tool>`，并把服务器 instructions 注入对话。TUI、`-p` 与 Remote 都会连接已配置服务器。

### 项目指令

从 Git 根目录到当前工作目录逐层加载 `MYCLAUDE.md` / `AGENTS.md`（含 `MYCLAUDE.local.md`），用户级指令位于 `~/.myclaude/MYCLAUDE.md` 与 `~/.myclaude/AGENTS.md`。

---

## 上下文、会话与记忆

- **上下文压缩（两层）**：Layer 1 把超过 50K 字符的单个工具结果落盘到 `.myclaude/session/tool-results/`（带路径穿越防护与 `0600` 权限），并做引用感知清理；Layer 2 在接近窗口上限时用无工具的 LLM 调用摘要历史前缀，保留近期消息尾部。结构化 `compact_boundary` 会被持久化以支持恢复。
- **恢复上下文**：压缩后自动重附最近读取的文件内容与已激活 Skill 的 SOP，尽量减少压缩带来的信息损失。
- **会话**：JSONL 记录（`SESSION_SCHEMA_VERSION = 1`），无版本的早期记录在内存中迁移，未来版本会被安全拒绝。
- **项目记忆**：四类记忆 —— `user` / `feedback`（用户级 `~/.myclaude/memory/`）与 `project` / `reference`（项目级 `.myclaude/memory/`）。后台自动提取会过滤密钥类内容，并支持跨会话召回与整合。

---

## 斜杠命令

TUI 内置以下斜杠命令（Remote 部分可用）：

| 命令 | 别名 | 说明 |
| --- | --- | --- |
| `/help` | `h`、`?` | 显示帮助 |
| `/status` | `s` | 显示运行状态 |
| `/compact` | `c` | 手动压缩上下文 |
| `/clear` | | 清空对话历史 |
| `/plan` | `p` | 切换 Plan 模式 |
| `/review` | | 审查代码改动 |
| `/session` | | 会话管理与恢复 |
| `/rewind` | | 回退到先前检查点 |
| `/memory` | | 记忆管理 |
| `/permission` | | 权限规则管理 |
| `/sandbox` | | 沙箱管理 |
| `/mcp` | | 查看 MCP 服务器状态 |
| `/skill` | `skills` | 管理 Skill |

此外，可信项目与用户目录下的 Markdown 自定义命令会被加载，子目录形成 `namespace:command` 命名，支持 `$ARGUMENTS` 与 frontmatter。

---

## 数据与目录布局

项目运行状态默认位于 `.myclaude/`：

| 路径 | 内容 |
| --- | --- |
| `.myclaude/sessions/` | JSONL 会话、元数据与压缩边界 |
| `.myclaude/session/tool-results/` | 超预算工具结果与替换记录 |
| `.myclaude/memory/` | 项目记忆 |
| `.myclaude/file-history/` | 文件修改历史与回退数据 |
| `.myclaude/worktrees/` | Worktree 状态 |
| `.myclaude/plans/` | Plan 文档 |
| `.myclaude/debug.log` | 当前运行日志 |

用户级状态位于 `~/.myclaude/`（配置、信任记录、记忆、Skills、Agents、命令、Team）。

> [!IMPORTANT]
> 仓库默认在 `.gitignore` 中忽略 `.myclaude/`。其中可能包含源码片段、会话与私有上下文，**不应提交**。

---

## 开发与测试

```bash
# uv
uv sync --group dev
uv run pytest -q
uv run ruff check .

# conda / venv
pytest -q
ruff check .
```

CI（GitHub Actions）在 Python **3.11** 与 **3.13** 上运行 `ruff check .` 与 `pytest -q`。

测试覆盖 Provider 序列化、Agent 循环、运行预算、权限、危险命令、文件工具、上下文压缩与恢复、会话迁移、记忆与整合、Worktree、Hooks、MCP、Skills、子 Agent、Team、Coordinator、Workspace Trust 以及三入口运行时装配。

> 默认测试使用模拟 Provider 与 MCP，不替代针对真实供应商端点的集成测试。

---

## 评测

`run_evals.py` 提供一个小而确定的本地 Coding-Agent 评测：用真实 fixture + 确定性 oracle + 版本化 trace 度量成功率、成本与轨迹，而非只看最终文本。

```bash
# 离线列出发现的任务（无需模型）
python run_evals.py --list

# 跑全部任务，每个 3 次 trial（需要配好 provider / API key）
python run_evals.py --trials 3

# 只跑单个任务
python run_evals.py --task single-file-bug

# 消融对比：关闭记忆 / 子 Agent
python run_evals.py --no-memory
python run_evals.py --no-subagent
```

任务以 `evals/<task>/task.yaml` + `repo/` fixture 声明（当前含 `single-file-bug`、`explain-no-change`），oracle 基于 pytest 退出码、diff 白名单、受保护文件哈希不变与无冲突标记来打分，trace 默认写入 `evals/_out/`。

---

## 已知限制

- `bypassPermissions` 不是安全沙箱；Shell 的完整语义无法靠模式检测可靠证明安全。
- Hook 的 `agent` action 仍是占位实现，调用会显式失败。
- Remote 支持核心运行与会话持久化，但部分 UI 型斜杠命令与完整会话恢复仅在 TUI 可用。
- 模型能力表与未知模型 fallback 需要持续维护；新 Provider 建议显式配置 `context_window` 与 `max_output_tokens`。
- `tmux` / `iterm2` Team 后端依赖本机终端、Git 与 Worktree 条件；`in-process` 是最可移植模式。

---

## 致谢

MyClaude 是自主开发的 Coding Agent。设计上参考了 Claude Code、mini-swe-agent 与 Mistral Vibe 等项目的思路，它们**不是**本项目的运行时依赖。

---

## 许可证

本项目基于 [MIT License](LICENSE) 发布。
