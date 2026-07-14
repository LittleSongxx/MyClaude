# MewCode

[![CI](https://github.com/LittleSongxx/MyClaude/actions/workflows/ci.yml/badge.svg)](https://github.com/LittleSongxx/MyClaude/actions/workflows/ci.yml)

MewCode 是一个轻量但完整的终端 Coding Agent。它把主线收敛在三件事上：可靠地完成工具循环、把文件与命令副作用限制在清晰边界内、让长任务可以恢复和延续。

当前版本为 `0.3.0`，支持 TUI、无头模式和带临时令牌认证的本地 Remote UI。

## 核心能力

- Anthropic、OpenAI Responses 与 OpenAI-compatible 流式协议
- 原子文件写入、精确编辑、显式删除、搜索和有界 Bash 执行
- `default`、`acceptEdits`、`plan`、`bypassPermissions` 四种权限模式
- 路径沙箱、危险命令拦截和可选 OS 沙箱
- 会话持久化、自动压缩、工具结果预算与故障恢复
- 默认启用自动记忆；记忆按项目保存，并过滤疑似密钥内容
- 默认提供 Git Worktree 隔离工具；进入后 Agent、工具和权限边界会一起切换
- Skills、MCP、Hooks、子 Agent 与团队协作

Worktree “默认启用”表示相关工具开箱可用，并不会在每次启动时强制创建分支。进入 Worktree 仍受权限系统控制。

## 快速开始

需要 Python 3.11+ 和 Git。

```bash
git clone https://github.com/LittleSongxx/MyClaude.git
cd MyClaude
python -m venv .venv
source .venv/bin/activate
pip install -e .

mkdir -p .mewcode
cp config.example.yaml .mewcode/config.yaml
export ANTHROPIC_API_KEY="your-key"
mewcode
```

Windows PowerShell 激活虚拟环境时使用：

```powershell
.venv\Scripts\Activate.ps1
```

常用入口：

```bash
mewcode                              # 交互 TUI
mewcode -p "修复失败的测试"           # 无头执行
mewcode -p "分析项目" --output-format stream-json
mewcode --remote                     # 仅监听 127.0.0.1，启动时打印临时访问令牌
```

## 配置与安全边界

配置按 `~/.mewcode/config.yaml`、项目 `.mewcode/config.yaml`、项目 `.mewcode/config.local.yaml` 的顺序分层合并。完整起点见 [`config.example.yaml`](config.example.yaml)。API Key 建议只放环境变量。

默认权限模式不会静默执行写操作或一般命令；显式拒绝规则优先于安全命令白名单。`.mewcode/config.yaml` 和权限文件不可由 Agent 直接改写。`bypassPermissions` 会减少交互确认，但灾难性命令拦截仍然生效。

OS 沙箱属于可选的额外边界：Linux 使用 bubblewrap，macOS 使用 Seatbelt。若系统不支持或沙箱不可用，MewCode 不会因为配置了 `auto_allow` 就错误地自动放行命令。

Remote UI 默认只绑定 loopback，并要求启动时生成的随机令牌。它没有内置 TLS；不要直接暴露到公网，远程使用请通过可信 SSH 隧道。

## 开发

```bash
uv sync --group dev
uv run ruff check .
uv run pytest
```

核心回归测试覆盖 Agent 循环、权限、上下文压缩、记忆、Worktree、Hooks、MCP、Skills 和多 Agent 协作。

## 项目说明

本项目由小林 coding 发布的教学项目持续演进而来，并参考了 Claude Code、mini-swe-agent 与 mistral-vibe 的设计思路；它不是 Anthropic 官方项目。源码中保留了原始来源标记。
