# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from myclaude import __version__
from myclaude.config import ConfigError, load_config
from myclaude.hooks import HookConfigError, HookEngine, load_hooks
from myclaude.permissions import PermissionMode


def main() -> None:
    parser = argparse.ArgumentParser(prog="myclaude", description="MyClaude AI coding assistant")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--mode",
        choices=[m.value for m in PermissionMode],
        default=None,
        help="Permission mode (overrides config.yaml)",
    )
    parser.add_argument(
        "-p",
        metavar="PROMPT",
        default=None,
        help="Run non-interactively: execute the prompt and print the result to stdout",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "stream-json"],
        default="text",
        help="Output format for -p mode: 'text' (default) prints final text, 'stream-json' emits NDJSON events",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        default=False,
        help="Start the authenticated browser UI on 127.0.0.1:18888",
    )
    parser.add_argument(
        "--remote-addr",
        default="127.0.0.1",
        help="Remote UI bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--remote-port",
        type=int,
        default=18888,
        help="Remote UI port (default: 18888)",
    )
    parser.add_argument(
        "--trust-workspace",
        action="store_true",
        help="Trust this workspace and enable project configuration/customizations",
    )
    parser.add_argument(
        "--no-project-config",
        action="store_true",
        help="Run without project configuration or project customizations",
    )
    parser.add_argument(
        "--revoke-workspace-trust",
        action="store_true",
        help="Revoke trust for this workspace and exit",
    )
    args = parser.parse_args()

    from myclaude.trust import WorkspaceTrustManager

    trust_manager = WorkspaceTrustManager()
    if args.revoke_workspace_trust:
        root = trust_manager.revoke(Path.cwd())
        print(f"Workspace trust revoked: {root}")
        return

    if args.no_project_config:
        workspace_trusted = False
    elif trust_manager.is_trusted(Path.cwd()):
        workspace_trusted = True
    elif args.trust_workspace:
        root = trust_manager.trust(Path.cwd())
        workspace_trusted = True
        print(f"Workspace trusted: {root}", file=sys.stderr)
    elif args.p is not None or args.remote or not sys.stdin.isatty():
        root = trust_manager.status(Path.cwd()).root
        print(
            f"Error: workspace is not trusted: {root}. "
            "Review the repository, then rerun with --trust-workspace, or use "
            "--no-project-config with a user-level provider config.",
            file=sys.stderr,
        )
        sys.exit(2)
    else:
        root = trust_manager.status(Path.cwd()).root
        answer = input(
            f"Trust workspace {root}? Project configuration can execute MCP servers "
            "and Hooks. Type 'yes' to trust: "
        ).strip().lower()
        if answer not in {"y", "yes"}:
            print("Workspace not trusted; exiting.", file=sys.stderr)
            return
        trust_manager.trust(root)
        workspace_trusted = True

    # Help/version must remain read-only, and a read-only project should still
    # be able to start.  Prefer a project-local log but fall back gracefully.
    try:
        Path(".myclaude").mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(message)s",
            filename=".myclaude/debug.log",
            filemode="w",
            encoding="utf-8",
        )
    except OSError:
        logging.basicConfig(level=logging.WARNING)

    try:
        config = load_config(include_project=workspace_trusted)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    mode_str = args.mode if args.mode else config.permission_mode
    permission_mode = PermissionMode(mode_str)

    try:
        hooks = load_hooks(config.raw_hooks)
    except HookConfigError as e:
        print(f"Hook config error: {e}", file=sys.stderr)
        sys.exit(1)

    hook_engine = HookEngine(hooks) if hooks else None

    if args.p is not None:
        output_format = getattr(args, "output_format", "text")
        asyncio.run(
            _run_prompt(
                config,
                permission_mode,
                hook_engine,
                args.p,
                output_format,
                workspace_trusted=workspace_trusted,
            )
        )
        return

    # Remote 模式：启动 WebSocket 服务器，浏览器访问 http://localhost:18888
    if args.remote:
        from myclaude.remote import RemoteServer

        server = RemoteServer(
            providers=config.providers,
            mcp_servers=config.mcp_servers,
            hook_engine=hook_engine,
            permission_mode=permission_mode,
            sandbox_config=config.sandbox,
            worktree_config=config.worktree,
            addr=args.remote_addr,
            port=args.remote_port,
            workspace_trusted=workspace_trusted,
            run_limits=config.run_limits,
            enable_fork=config.enable_fork,
            enable_verification_agent=config.enable_verification_agent,
            teammate_mode=config.teammate_mode,
            enable_coordinator_mode=config.enable_coordinator_mode,
        )
        asyncio.run(server.run())
        return

    from myclaude.app import MyClaudeApp
    from myclaude.driver import NoAltScreenDriver

    app = MyClaudeApp(
        providers=config.providers,
        permission_mode=permission_mode,
        mcp_servers=config.mcp_servers,
        hook_engine=hook_engine,
        enable_fork=config.enable_fork,
        enable_verification_agent=config.enable_verification_agent,
        worktree_config=config.worktree,
        teammate_mode=config.teammate_mode,
        enable_coordinator_mode=config.enable_coordinator_mode,
        driver_class=NoAltScreenDriver,
        sandbox_config=config.sandbox,
        workspace_trusted=workspace_trusted,
        run_limits=config.run_limits,
    )
    app.run()


async def _run_prompt(
    config,
    permission_mode,
    hook_engine,
    prompt: str,
    output_format: str = "text",
    *,
    workspace_trusted: bool = True,
) -> None:
    from myclaude.agent import (
        CompactNotification,
        ErrorEvent,
        LoopComplete,
        PermissionRequest,
        PermissionResponse,
        RetryEvent,
        StreamText,
        ThinkingText,
        ToolResultEvent,
        ToolUseEvent,
        TurnComplete,
        UsageEvent,
    )
    from myclaude.client import resolve_context_window
    from myclaude.conversation import ConversationManager
    from myclaude.runtime_assembler import RuntimeAssembler

    is_json = output_format == "stream-json"

    def emit_json(obj: dict) -> None:
        """输出一行 NDJSON 到 stdout"""
        print(json.dumps(obj, ensure_ascii=False), flush=True)

    provider = config.providers[0]
    # 第 2 层：尽力从 provider 自动拉取模型的 context window（缓存在 provider 上）。
    # 不会抛异常或阻塞启动；失败则退化到映射表。
    await resolve_context_window(provider)
    work_dir = os.getcwd()
    assembler = RuntimeAssembler(
        provider,
        permission_mode,
        work_dir=work_dir,
        hook_engine=hook_engine,
        sandbox_config=config.sandbox,
        worktree_config=config.worktree,
        workspace_trusted=workspace_trusted,
        run_limits=config.run_limits,
    )
    runtime = assembler.build_core()
    features = assembler.install_standard_features(
        runtime,
        interactive=False,
        teammate_mode=config.teammate_mode or "in-process",
        enable_fork=config.enable_fork,
        enable_verification_agent=config.enable_verification_agent,
        enable_coordinator_mode=config.enable_coordinator_mode,
    )
    registry = runtime.registry
    agent = runtime.agent
    wt_manager = runtime.worktree_manager
    assert wt_manager is not None
    task_manager = features.task_manager
    team_manager = features.team_manager

    mcp_features = await assembler.connect_mcp(registry, config.mcp_servers)
    for error in mcp_features.result.errors:
        print(f"MCP warning: {error}", file=sys.stderr)

    def drain_notifications() -> list[str]:
        notes: list[str] = []
        for t in task_manager.poll_completed():
            notes.append(
                f"<task-notification>\n<task_id>{t.id}</task_id>\n"
                f"<status>{t.status}</status>\n<result>{t.result}</result>\n"
                f"</task-notification>"
            )
        notes.extend(team_manager.drain_lead_mailbox())
        return notes

    def drain_mailbox_only() -> list[str]:
        return team_manager.drain_lead_mailbox()

    agent.notification_fn = drain_mailbox_only

    # 使用事件驱动的 agent.run()，支持 text 和 stream-json 两种输出格式
    conv = ConversationManager()
    conv.add_user_message(prompt)
    if mcp_features.instructions:
        conv.add_system_reminder(mcp_features.instructions)

    start = time.monotonic()
    text_buf = ""
    total_input = 0
    total_output = 0
    tool_calls: list[dict] = []

    async for event in agent.run(conv):
        if isinstance(event, StreamText):
            text_buf += event.text
            if is_json:
                emit_json({"type": "assistant", "text": event.text})

        elif isinstance(event, ThinkingText):
            if is_json:
                emit_json({"type": "thinking", "text": event.text})

        elif isinstance(event, ToolUseEvent):
            tool_calls.append({"name": event.tool_name, "is_error": False})
            if is_json:
                emit_json({
                    "type": "tool_use",
                    "tool_name": event.tool_name,
                    "tool_id": event.tool_id,
                    "args": event.arguments,
                })

        elif isinstance(event, ToolResultEvent):
            # 回填最后一个同名 tool_call 的 is_error
            if tool_calls:
                tool_calls[-1]["is_error"] = event.is_error
            if is_json:
                emit_json({
                    "type": "tool_result",
                    "tool_name": event.tool_name,
                    "tool_id": event.tool_id,
                    "output": event.output,
                    "is_error": event.is_error,
                    "elapsed": round(event.elapsed, 3),
                })

        elif isinstance(event, UsageEvent):
            total_input = event.input_tokens
            total_output = event.output_tokens
            if is_json:
                emit_json({
                    "type": "usage",
                    "input_tokens": event.input_tokens,
                    "output_tokens": event.output_tokens,
                })

        elif isinstance(event, TurnComplete):
            if is_json:
                emit_json({"type": "turn_complete", "turn": event.turn})

        elif isinstance(event, LoopComplete):
            # 最终结果：stream-json 输出 result 行，text 模式直接打印文本
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if is_json:
                emit_json({
                    "type": "result",
                    "result": text_buf,
                    "duration_ms": elapsed_ms,
                    "num_turns": event.total_turns,
                    "tool_calls": tool_calls,
                    "usage": {
                        "input_tokens": total_input,
                        "output_tokens": total_output,
                    },
                    "stop_reason": "end_turn",
                })
            else:
                print(text_buf, end="", flush=True)
            break

        elif isinstance(event, ErrorEvent):
            if is_json:
                emit_json({"type": "error", "message": event.message})
            else:
                print(f"Error: {event.message}", file=sys.stderr, flush=True)

        elif isinstance(event, CompactNotification):
            if is_json:
                emit_json({"type": "compact", "message": event.message})

        elif isinstance(event, RetryEvent):
            if is_json:
                emit_json({"type": "retry", "reason": event.reason})

        elif isinstance(event, PermissionRequest):
            # Non-interactive mode cannot obtain informed consent.  Fail closed;
            # callers that intentionally want unrestricted execution must opt in
            # with --mode bypassPermissions.
            event.future.set_result(PermissionResponse.DENY)

    # 如果有 team 在运行，轮询等待 teammate 完成
    if not team_manager._teams:
        if mcp_features.manager is not None:
            await mcp_features.manager.shutdown()
        return

    for i in range(90):
        await asyncio.sleep(2)
        running = {k: not t.done() for k, t in task_manager._async_tasks.items()}
        completed_ids = [t.id for t in task_manager._tasks.values() if t.status != "running"]
        print(f"[poll {i}] running={running} completed={completed_ids} teams={list(team_manager._teams.keys())} queue_size={task_manager._notify_queue.qsize()}", file=sys.stderr, flush=True)
        notes = drain_notifications()
        if not notes:
            has_running = any(v for v in running.values())
            if not has_running:
                print(f"[poll {i}] no running tasks, breaking", file=sys.stderr, flush=True)
                break
            continue
        for note in notes:
            conv.add_system_reminder(note)
        # 后续 team 轮询仍用 run_to_completion，避免重复事件循环
        last_result = await agent.run_to_completion(
            "Teammate notifications received. Process them and continue.", conv
        )
        if is_json:
            emit_json({"type": "assistant", "text": last_result})
        else:
            print(last_result, flush=True)

    if mcp_features.manager is not None:
        await mcp_features.manager.shutdown()


if __name__ == "__main__":
    main()
