"""
Remote Control 服务器：通过 WebSocket 桥接 Agent 事件和 Web UI。

使用 websockets 库提供 HTTP（静态 HTML）+ WebSocket 服务，
让用户在浏览器中与 MyClaude Agent 交互。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.http11 import Request, Response

from myclaude.agent import (
    Agent,
    CacheContractEvent,
    CompactNotification,
    ErrorEvent,
    HookEvent,
    LoopComplete,
    OrchestrationEvent,
    PermissionRequest,
    PermissionResponse,
    RetryEvent,
    StreamText,
    ThinkingText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
    UsageEvent,
    VerificationEvent,
)
from myclaude.client import resolve_context_window
from myclaude.commands import (
    CommandContext,
    CommandRegistry,
    CommandType,
    register_user_commands,
)
from myclaude.commands.handlers import register_all_commands
from myclaude.commands.parser import parse_command
from myclaude.config import (
    MCPServerConfig,
    ProviderConfig,
    SandboxAppConfig,
    WorktreeConfig,
)
from myclaude.conversation import ConversationManager
from myclaude.hooks import HookContext, HookEngine
from myclaude.mcp import MCPManager
from myclaude.memory import MemoryManager
from myclaude.memory.session import Session, SessionManager, make_compact_boundary
from myclaude.permissions import PermissionMode
from myclaude.prompts import build_plan_mode_exit_reminder
from myclaude.runtime_assembler import RuntimeAssembler
from myclaude.skills.loader import SkillLoader
from myclaude.tools import ToolRegistry
from myclaude.tools.ask_user import AskUserEvent
from myclaude.web_content import INDEX_HTML

log = logging.getLogger(__name__)


class RemoteServer:
    """Remote Control 核心：桥接 Agent 事件和 WebSocket 客户端。"""

    def __init__(
        self,
        providers: list[ProviderConfig],
        mcp_servers: list[MCPServerConfig] | None = None,
        hook_engine: HookEngine | None = None,
        permission_mode: PermissionMode = PermissionMode.DEFAULT,
        sandbox_config: SandboxAppConfig | None = None,
        worktree_config: WorktreeConfig | None = None,
        addr: str = "127.0.0.1",
        port: int = 18888,
        auth_token: str | None = None,
        workspace_trusted: bool = True,
        run_limits: Any = None,
        enable_fork: bool = False,
        enable_verification_agent: bool = False,
        teammate_mode: str = "",
        enable_coordinator_mode: bool = False,
    ) -> None:
        self.providers = providers
        self._mcp_server_configs = mcp_servers or []
        self.hook_engine = hook_engine
        self.permission_mode = permission_mode
        self.sandbox_config = sandbox_config or SandboxAppConfig()
        self.worktree_config = worktree_config or WorktreeConfig()
        self.addr = addr
        self.port = port
        self.auth_token = auth_token or secrets.token_urlsafe(24)
        self.workspace_trusted = workspace_trusted
        self.run_limits = run_limits
        self.enable_fork = enable_fork
        self.enable_verification_agent = enable_verification_agent
        self.teammate_mode = teammate_mode
        self.enable_coordinator_mode = enable_coordinator_mode

        # WebSocket connection set; remote control deliberately permits one
        # authenticated controller at a time.
        self._connections: set[ServerConnection] = set()

        # Agent 相关状态
        self.agent: Agent | None = None
        self.conversation: ConversationManager | None = None
        self.registry: ToolRegistry | None = None
        self.session_id: str = ""
        self._streaming = False
        self._cancel_event: asyncio.Event | None = None
        self._agent_task: asyncio.Task[None] | None = None

        # 权限请求的 pending 队列：id -> Future
        self._pending_perms: dict[str, asyncio.Future[PermissionResponse]] = {}
        self._pending_asks: dict[str, asyncio.Future[dict[str, str]]] = {}
        self._pre_plan_mode = permission_mode

        # 命令注册表
        self.command_registry = CommandRegistry()
        register_all_commands(self.command_registry)
        self._command_state: dict[str, Any] = {}
        for error in register_user_commands(
            self.command_registry,
            os.getcwd(),
            include_project=self.workspace_trusted,
        ):
            log.warning("Custom command skipped: %s", error)

        # MCP 相关
        self.mcp_manager: MCPManager | None = None
        self._mcp_instructions: str = ""

        # Skill 加载器
        self.skill_loader: SkillLoader | None = None

        # Memory / Session
        self.memory_manager: MemoryManager | None = None
        self.session_manager: SessionManager | None = None
        self.session: Session | None = None
        self._assembler: RuntimeAssembler | None = None
        self.task_manager = None
        self.trace_manager = None
        self.team_manager = None
        self.agent_loader = None

    # ------------------------------------------------------------------
    # 启动入口
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """启动 HTTP + WebSocket 服务器。"""
        try:
            await resolve_context_window(self.providers[0])
            self._init_agent()
            await self._init_mcp()

            print(
                f"\n  Remote UI: http://localhost:{self.port}/?token={self.auth_token}\n"
            )

            async with websockets.serve(
                self._ws_handler,
                self.addr,
                self.port,
                process_request=self._process_http_request,
                max_size=4 * 1024 * 1024,
            ):
                await asyncio.Future()
        finally:
            await self._shutdown()

    # ------------------------------------------------------------------
    # HTTP 请求处理（为 / 路径提供前端 HTML）
    # ------------------------------------------------------------------

    def _process_http_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        """拦截 HTTP 请求，对 / 路径返回 HTML 页面。
        返回 None 表示继续走 WebSocket 升级流程。
        """
        parsed = urlsplit(request.path)
        token = parse_qs(parsed.query).get("token", [""])[0]
        if not secrets.compare_digest(token, self.auth_token):
            return Response(
                401,
                "Unauthorized",
                websockets.Headers({"Content-Type": "text/plain; charset=utf-8"}),
                b"Unauthorized",
            )
        if parsed.path == "/":
            return Response(
                200,
                "OK",
                websockets.Headers({"Content-Type": "text/html; charset=utf-8"}),
                INDEX_HTML.encode("utf-8"),
            )
        if parsed.path != "/ws":
            return Response(404, "Not Found", websockets.Headers(), b"404 Not Found")
        # /ws 路径 → 继续 WebSocket 升级
        return None

    # ------------------------------------------------------------------
    # WebSocket 连接处理
    # ------------------------------------------------------------------

    async def _ws_handler(self, websocket: ServerConnection) -> None:
        """处理单个 WebSocket 连接的全生命周期。"""
        if self._connections:
            await websocket.close(1008, "A remote control client is already connected")
            return
        self._connections.add(websocket)
        try:
            # 连接建立时推送会话信息
            await self._broadcast({
                "type": "connected",
                "data": {
                    "session": self.session_id,
                    "cwd": self.agent.work_dir if self.agent else os.getcwd(),
                },
            })

            # 推送命令列表
            await self._broadcast({
                "type": "commands",
                "data": self._build_command_list(),
            })

            # 消息循环
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")
                data = msg.get("data", {})

                if msg_type == "user_message":
                    content = data.get("content", "").strip()
                    if content:
                        self._start_agent_task(content)

                elif msg_type == "permission_response":
                    self._handle_permission_response(data)

                elif msg_type == "ask_user_response":
                    self._handle_ask_user_response(data)

                elif msg_type == "plan_response":
                    self._handle_plan_response(data)

                elif msg_type == "cancel":
                    if self._cancel_event is not None:
                        self._cancel_event.set()
                    if self._agent_task is not None and not self._agent_task.done():
                        self._agent_task.cancel()
                    self._deny_pending_permissions()
                    self._cancel_pending_asks()

                elif msg_type == "ping":
                    # 应用层保活
                    await self._broadcast({"type": "pong", "data": None})

        except websockets.ConnectionClosed:
            pass
        finally:
            self._connections.discard(websocket)
            if self._agent_task is not None and not self._agent_task.done():
                self._agent_task.cancel()
            self._deny_pending_permissions()
            self._cancel_pending_asks()

    # ------------------------------------------------------------------
    # Agent 初始化（复刻 TUI 的 _select_provider 流程）
    # ------------------------------------------------------------------

    def _init_agent(self) -> None:
        """初始化 Agent 及相关子系统。"""
        provider = self.providers[0]
        work_dir = os.getcwd()

        self.session_manager = SessionManager(work_dir)
        self.session = self.session_manager.create()
        self.session_id = self.session.session_id

        self._assembler = RuntimeAssembler(
            provider,
            self.permission_mode,
            work_dir=work_dir,
            hook_engine=self.hook_engine,
            sandbox_config=self.sandbox_config,
            worktree_config=self.worktree_config,
            workspace_trusted=self.workspace_trusted,
            run_limits=self.run_limits,
        )
        runtime = self._assembler.build_core()
        features = self._assembler.install_standard_features(
            runtime,
            interactive=True,
            teammate_mode=self.teammate_mode or "in-process",
            enable_fork=self.enable_fork,
            enable_verification_agent=self.enable_verification_agent,
            enable_coordinator_mode=self.enable_coordinator_mode,
        )
        self.registry = runtime.registry
        self.agent = runtime.agent
        self.memory_manager = runtime.memory_manager
        self.skill_loader = features.skill_loader
        self.task_manager = features.task_manager
        self.trace_manager = features.trace_manager
        self.team_manager = features.team_manager
        self.agent_loader = features.agent_loader
        self.task_manager.set_permission_handler(self._forward_permission_request)

        self.agent.session_id = self.session_id

        from myclaude.filehistory import FileHistory

        file_history = FileHistory(work_dir, self.session_id)
        self.agent.file_history = file_history
        for tool in self.registry.list_tools():
            if hasattr(tool, "file_history"):
                tool.file_history = file_history

        def drain_notifications() -> list[str]:
            notes: list[str] = []
            if self.task_manager is not None:
                for task in self.task_manager.poll_completed():
                    notes.append(
                        f"<task-notification>\n<task_id>{task.id}</task_id>\n"
                        f"<status>{task.status}</status>\n<result>{task.result}</result>\n"
                        "</task-notification>"
                    )
            if self.team_manager is not None:
                notes.extend(self.team_manager.drain_lead_mailbox())
            return notes

        self.agent.notification_fn = drain_notifications

        # 初始化对话管理器
        self.conversation = ConversationManager()

        log.info("Agent initialized: session=%s, model=%s", self.session_id, provider.model)

    # ------------------------------------------------------------------
    # MCP 初始化
    # ------------------------------------------------------------------

    async def _init_mcp(self) -> None:
        """连接所有配置的 MCP 服务器，注册工具。"""
        if not self._mcp_server_configs or self.registry is None:
            return

        if self._assembler is None:
            return
        features = await self._assembler.connect_mcp(
            self.registry, self._mcp_server_configs
        )
        connect_result = features.result
        self.mcp_manager = features.manager

        for err in connect_result.errors:
            log.warning("MCP error: %s", err)

        # 构建 MCP 指令（首次发送消息时注入 conversation）
        self._mcp_instructions = features.instructions

    # ------------------------------------------------------------------
    # 用户消息处理
    # ------------------------------------------------------------------

    async def _handle_user_message(self, content: str) -> None:
        """处理来自 Web UI 的用户消息或斜杠命令。"""
        if self._streaming:
            if self.agent is not None and content and not content.startswith("/"):
                position = self.agent.queue_user_message(content)
                await self._broadcast({
                    "type": "system",
                    "data": {"message": f"Message queued ({position})"},
                })
            return

        # 斜杠命令
        if content.startswith("/"):
            await self._handle_slash_command(content)
            return

        # 普通消息 → 发给 Agent
        self._streaming = True
        assert self.conversation is not None
        assert self.agent is not None

        self.agent.prepare_conversation(self.conversation)
        self.conversation.add_user_message(content)
        if self.session is not None:
            self.session.append(self.conversation.history[-1])

        # 首次注入 MCP 指令
        if self._mcp_instructions:
            self.conversation.add_system_reminder(self._mcp_instructions)
            self._mcp_instructions = ""
        history_cursor = len(self.conversation.history)

        # 创建取消事件
        self._cancel_event = asyncio.Event()
        start_time = time.monotonic()
        stream_buf = ""

        try:
            async for event in self.agent.run(self.conversation):
                # 检查取消信号
                if self._cancel_event.is_set():
                    break

                if isinstance(event, StreamText):
                    stream_buf += event.text
                    await self._broadcast({
                        "type": "stream_text",
                        "data": {"text": event.text},
                    })

                elif isinstance(event, ThinkingText):
                    await self._broadcast({
                        "type": "thinking_text",
                        "data": {"text": event.text},
                    })

                elif isinstance(event, ToolUseEvent):
                    await self._broadcast({
                        "type": "tool_use",
                        "data": {
                            "toolId": event.tool_id,
                            "toolName": event.tool_name,
                            "args": event.arguments,
                        },
                    })

                elif isinstance(event, ToolResultEvent):
                    # 如果之前有累积的流式文本，先结束它
                    if stream_buf:
                        await self._broadcast({
                            "type": "stream_end",
                            "data": {"text": stream_buf},
                        })
                        stream_buf = ""
                    await self._broadcast({
                        "type": "tool_result",
                        "data": {
                            "toolId": event.tool_id,
                            "toolName": event.tool_name,
                            "output": event.output,
                            "isError": event.is_error,
                            "elapsed": event.elapsed,
                            "metadata": event.metadata,
                        },
                    })

                elif isinstance(event, PermissionRequest):
                    await self.task_manager.handle_permission_request(event)

                elif isinstance(event, AskUserEvent):
                    ask_id = f"ask_{time.time_ns()}"
                    self._pending_asks[ask_id] = event.future
                    await self._broadcast({
                        "type": "ask_user",
                        "data": {
                            "id": ask_id,
                            "questions": event.questions,
                        },
                    })

                elif isinstance(event, TurnComplete):
                    if self.session is not None:
                        for message in self.conversation.history[history_cursor:]:
                            self.session.append(message)
                        history_cursor = len(self.conversation.history)
                        self.session.update_provider_state(
                            self.conversation.provider_state
                        )
                    if stream_buf:
                        await self._broadcast({
                            "type": "stream_end",
                            "data": {"text": stream_buf},
                        })
                        stream_buf = ""
                    await self._broadcast({
                        "type": "turn_complete",
                        "data": {"turn": event.turn},
                    })

                elif isinstance(event, LoopComplete):
                    if self.session is not None:
                        for message in self.conversation.history[history_cursor:]:
                            self.session.append(message)
                        history_cursor = len(self.conversation.history)
                        self.session.meta.total_tokens = (
                            self.agent.total_input_tokens
                            + self.agent.total_output_tokens
                        )
                        self.session.update_provider_state(
                            self.conversation.provider_state
                        )
                        self.session.meta.save(
                            self.session._sessions_dir
                            / f"{self.session.session_id}.meta"
                        )
                    if stream_buf:
                        await self._broadcast({
                            "type": "stream_end",
                            "data": {"text": stream_buf},
                        })
                        stream_buf = ""
                    elapsed = time.monotonic() - start_time
                    await self._broadcast({
                        "type": "loop_complete",
                        "data": {
                            "totalTurns": event.total_turns,
                            "elapsed": elapsed,
                        },
                    })
                    if self.agent.plan_mode:
                        plan_path = self.agent._get_plan_path()
                        try:
                            plan_content = plan_path.read_text(encoding="utf-8")
                        except OSError:
                            plan_content = ""
                        await self._broadcast({
                            "type": "plan_approval",
                            "data": {
                                "path": str(plan_path),
                                "plan": plan_content,
                            },
                        })

                elif isinstance(event, UsageEvent):
                    await self._broadcast({
                        "type": "usage",
                        "data": {
                            "inputTokens": event.input_tokens,
                            "outputTokens": event.output_tokens,
                        },
                    })

                elif isinstance(event, CacheContractEvent):
                    await self._broadcast({
                        "type": "cache_contract",
                        "data": {
                            "fingerprint": event.fingerprint,
                            "breakReasons": list(event.break_reasons),
                            "requestHitRate": event.request_hit_rate,
                            "cumulativeHitRate": event.cumulative_hit_rate,
                            "unexpectedMiss": event.unexpected_miss,
                        },
                    })

                elif isinstance(event, VerificationEvent):
                    await self._broadcast({
                        "type": "verification",
                        "data": {
                            "status": event.status,
                            "blocked": event.blocked,
                            "message": event.message,
                            "revision": event.revision,
                            "evidence": event.evidence,
                        },
                    })

                elif isinstance(event, OrchestrationEvent):
                    await self._broadcast({
                        "type": "orchestration",
                        "data": {
                            "mode": event.mode,
                            "maxAgents": event.max_agents,
                            "reason": event.reason,
                        },
                    })

                elif isinstance(event, ErrorEvent):
                    await self._broadcast({
                        "type": "error",
                        "data": {"message": event.message},
                    })

                elif isinstance(event, CompactNotification):
                    if self.session is not None and event.boundary is not None:
                        self.session.append_record(
                            make_compact_boundary(
                                event.boundary.summary,
                                event.boundary.keep,
                            )
                        )
                        self.session.update_provider_state(
                            self.conversation.provider_state
                        )
                        history_cursor = len(self.conversation.history)
                    await self._broadcast({
                        "type": "compact",
                        "data": {"message": event.message},
                    })

                elif isinstance(event, RetryEvent):
                    await self._broadcast({
                        "type": "retry",
                        "data": {
                            "reason": event.reason,
                            "waitMs": int(event.wait * 1000),
                        },
                    })

                elif isinstance(event, HookEvent):
                    status = "ok" if event.success else "error"
                    await self._broadcast({
                        "type": "system",
                        "data": {
                            "message": f"Hook [{event.hook_id}] {status}: {event.output}"
                        },
                    })

        except asyncio.CancelledError:
            await self._broadcast({
                "type": "error",
                "data": {"message": "Operation cancelled"},
            })
        except Exception as exc:
            log.exception("Agent run error")
            await self._broadcast({
                "type": "error",
                "data": {"message": str(exc)},
            })
        finally:
            self._streaming = False
            self._cancel_event = None
            self._pending_perms = {
                key: future
                for key, future in self._pending_perms.items()
                if not future.done()
            }
            self._pending_asks = {
                key: future
                for key, future in self._pending_asks.items()
                if not future.done()
            }
            self._agent_task = None

    # ------------------------------------------------------------------
    # 斜杠命令处理
    # ------------------------------------------------------------------

    async def _handle_slash_command(self, input_text: str) -> None:
        """分发斜杠命令。"""
        name, args, is_command = parse_command(input_text)
        if not is_command or not name:
            return

        cmd = self.command_registry.find(name)
        if cmd is None:
            await self._broadcast({
                "type": "error",
                "data": {"message": f"Unknown command: /{name} — type /help to see available commands"},
            })
            await self._broadcast({"type": "command_done", "data": None})
            return

        # 需要参数但没给
        if not args and cmd.arg_prompt:
            await self._broadcast({
                "type": "system",
                "data": {"message": cmd.arg_prompt},
            })
            await self._broadcast({"type": "command_done", "data": None})
            return

        if cmd.type == CommandType.LOCAL:
            # 本地命令直接执行
            ctx = self._build_command_context(args)
            try:
                await cmd.handler(ctx)
            except Exception as exc:
                await self._broadcast({
                    "type": "error",
                    "data": {"message": f"Command error: {exc}"},
                })
            await self._broadcast({"type": "command_done", "data": None})

        elif cmd.type == CommandType.LOCAL_UI:
            # UI 命令需要特殊处理
            if name == "clear":
                self.conversation = ConversationManager()
                if self.agent is not None:
                    self.agent.clear_active_skills()
                await self._broadcast({"type": "clear", "data": None})

            elif name == "compact":
                await self._handle_compact()
                return

            else:
                await self._broadcast({
                    "type": "system",
                    "data": {"message": f"/{name} is not fully supported in remote mode."},
                })

            await self._broadcast({"type": "command_done", "data": None})

        elif cmd.type == CommandType.PROMPT:
            # Prompt 类命令：handler 返回 prompt 文本，注入给 agent
            ctx = self._build_command_context(args)
            try:
                await cmd.handler(ctx)
            except Exception as exc:
                await self._broadcast({
                    "type": "error",
                    "data": {"message": f"Command error: {exc}"},
                })
                await self._broadcast({"type": "command_done", "data": None})

    def _build_command_context(self, args: str) -> CommandContext:
        """构建命令上下文。"""
        self._command_state.update(
            {
                "registry": self.command_registry,
                "set_session": self._set_session,
                "set_conversation": self._set_conversation,
                "clear_chat": self._clear_chat,
                "render_restored": self._render_restored_messages,
            }
        )
        return CommandContext(
            args=args,
            agent=self.agent,
            conversation=self.conversation,
            session=self.session,
            session_manager=self.session_manager,
            memory_manager=self.memory_manager,
            ui=self,  # type: ignore[arg-type]
            config=self._command_state,
        )

    def _set_session(self, session: Session) -> None:
        self.session = session
        self.session_id = session.session_id
        if self.agent is not None:
            self.agent.session_id = session.session_id
            from myclaude.filehistory import FileHistory

            base_dir = self._assembler.work_dir if self._assembler else self.agent.work_dir
            file_history = FileHistory(base_dir, session.session_id)
            self.agent.file_history = file_history
            for tool in self.agent.registry.list_tools():
                if hasattr(tool, "file_history"):
                    tool.file_history = file_history

    def _set_conversation(self, conversation: ConversationManager) -> None:
        self.conversation = conversation

    def _clear_chat(self) -> None:
        asyncio.create_task(self._broadcast({"type": "clear", "data": None}))

    async def _render_restored_messages(self, messages: list[Any]) -> None:
        await self._broadcast({"type": "clear", "data": None})
        for message in messages:
            if message.tool_results or not message.content:
                continue
            if message.source != "user" and message.role == "user":
                continue
            event_type = (
                "replay_user" if message.role == "user" else "replay_assistant"
            )
            await self._broadcast({
                "type": event_type,
                "data": {"content": message.content},
            })

    async def _handle_compact(self) -> None:
        """处理 /compact 命令。"""
        if self.agent is None or self.conversation is None:
            await self._broadcast({
                "type": "error",
                "data": {"message": "Compact requires an active agent."},
            })
            await self._broadcast({"type": "command_done", "data": None})
            return

        await self._broadcast({
            "type": "system",
            "data": {"message": "Compacting conversation..."},
        })

        result = await self.agent.manual_compact(self.conversation)
        if isinstance(result, CompactNotification):
            if self.session is not None and result.boundary is not None:
                self.session.append_record(
                    make_compact_boundary(
                        result.boundary.summary,
                        result.boundary.keep,
                    )
                )
                self.session.update_provider_state(
                    self.conversation.provider_state
                )
            await self._broadcast({
                "type": "system",
                "data": {"message": result.message},
            })
        elif isinstance(result, ErrorEvent):
            await self._broadcast({
                "type": "error",
                "data": {"message": result.message},
            })

        await self._broadcast({"type": "command_done", "data": None})

    # ------------------------------------------------------------------
    # UIController 协议实现（供命令系统回调）
    # ------------------------------------------------------------------

    def add_system_message(self, text: str) -> None:
        """同步接口 — 在事件循环中调度广播。"""
        asyncio.ensure_future(self._broadcast({
            "type": "system",
            "data": {"message": text},
        }))

    def send_user_message(self, text: str) -> None:
        """同步接口 — 注入用户消息并触发 agent。"""
        if self._agent_task is asyncio.current_task():
            asyncio.get_running_loop().call_soon(self._start_agent_task, text)
            return
        self._start_agent_task(text)

    def set_plan_mode(self, enabled: bool) -> None:
        if self.agent is None:
            return
        if enabled:
            self._pre_plan_mode = self.agent.permission_mode
            self.agent.set_permission_mode(PermissionMode.PLAN)
        else:
            self.agent.set_permission_mode(self._pre_plan_mode)

    def get_token_count(self) -> tuple[int, int]:
        if self.agent:
            return self.agent.total_input_tokens, self.agent.total_output_tokens
        return 0, 0

    def refresh_status(self) -> None:
        pass  # Remote 模式不需要刷新 TUI 状态栏

    # ------------------------------------------------------------------
    # 权限响应处理
    # ------------------------------------------------------------------

    async def _forward_permission_request(
        self, event: PermissionRequest
    ) -> PermissionResponse:
        perm_id = f"perm_{time.time_ns()}"
        self._pending_perms[perm_id] = event.future
        await self._broadcast(
            {
                "type": "permission_request",
                "data": {
                    "id": perm_id,
                    "toolName": event.tool_name,
                    "description": event.description,
                },
            }
        )
        return await event.future

    def _handle_permission_response(self, data: dict[str, Any]) -> None:
        """处理来自 Web UI 的权限回复。"""
        perm_id = data.get("id", "")
        response_str = data.get("response", "deny")

        future = self._pending_perms.pop(perm_id, None)
        if future is None or future.done():
            return

        # 映射字符串到枚举
        mapping = {
            "allow": PermissionResponse.ALLOW,
            "deny": PermissionResponse.DENY,
            "allowAlways": PermissionResponse.ALLOW_ALWAYS,
        }
        response = mapping.get(response_str, PermissionResponse.DENY)
        future.set_result(response)

    def _handle_ask_user_response(self, data: dict[str, Any]) -> None:
        ask_id = data.get("id", "")
        answers = data.get("answers", {})
        future = self._pending_asks.pop(ask_id, None)
        if future is None or future.done():
            return
        future.set_result(
            {
                str(key): str(value)
                for key, value in answers.items()
            }
            if isinstance(answers, dict)
            else {}
        )

    def _handle_plan_response(self, data: dict[str, Any]) -> None:
        if self.agent is None:
            return
        choice = str(data.get("choice", "manual"))
        feedback = str(data.get("feedback", "")).strip()
        if choice == "feedback":
            if feedback:
                self._start_after_current(feedback)
            return

        mode = (
            PermissionMode.BYPASS
            if choice == "bypass"
            else self._pre_plan_mode
        )
        self.agent.set_permission_mode(mode)
        plan_path = self.agent._get_plan_path()
        plan_exists = plan_path.exists()
        try:
            plan_content = plan_path.read_text(encoding="utf-8")
        except OSError:
            plan_content = ""
        execute_text = (
            build_plan_mode_exit_reminder(str(plan_path), plan_exists)
            + "\n\nUser has approved your plan. You can now start coding."
        )
        if plan_content:
            execute_text += "\n\nApproved Plan:\n" + plan_content
        self._start_after_current(execute_text)

    def _start_after_current(self, content: str) -> None:
        async def restart() -> None:
            current = self._agent_task
            if current is not None and not current.done():
                await asyncio.gather(current, return_exceptions=True)
            self._start_agent_task(content)

        asyncio.create_task(restart())

    def _start_agent_task(self, content: str) -> None:
        if self._agent_task is None or self._agent_task.done():
            self._agent_task = asyncio.create_task(
                self._handle_user_message(content)
            )

    def _deny_pending_permissions(self) -> None:
        for future in self._pending_perms.values():
            if not future.done():
                future.set_result(PermissionResponse.DENY)
        self._pending_perms.clear()

    def _cancel_pending_asks(self) -> None:
        for future in self._pending_asks.values():
            if not future.done():
                future.set_result({})
        self._pending_asks.clear()

    async def _shutdown(self) -> None:
        if self._agent_task is not None and not self._agent_task.done():
            self._agent_task.cancel()
            await asyncio.gather(self._agent_task, return_exceptions=True)
        self._deny_pending_permissions()
        self._cancel_pending_asks()

        tasks: list[asyncio.Task[Any]] = []
        if self.agent is not None and self.agent.memory_manager is not None:
            if self.conversation is not None:
                tasks.append(asyncio.create_task(
                    self.agent._extract_memories(self.conversation.snapshot())
                ))
        if self.hook_engine is not None:
            tasks.append(asyncio.create_task(
                self.hook_engine.run_hooks(
                    "shutdown", HookContext(event_name="shutdown")
                )
            ))
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=3.0)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                try:
                    task.exception()
                except asyncio.CancelledError:
                    pass

        if self.mcp_manager is not None:
            await self.mcp_manager.shutdown()
            self.mcp_manager = None
        if self.session is not None:
            self.session.close()

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _build_command_list(self) -> list[dict[str, str]]:
        """构建命令列表，推送给前端用于斜杠命令菜单。"""
        result = []
        for cmd in self.command_registry.list_commands():
            result.append({
                "name": cmd.name,
                "description": cmd.description,
            })
        return result

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        """向所有已连接的 WebSocket 客户端广播消息。"""
        if not self._connections:
            return
        data = json.dumps(msg, ensure_ascii=False)
        # 复制集合避免迭代中修改
        closed = []
        for ws in list(self._connections):
            try:
                await ws.send(data)
            except websockets.ConnectionClosed:
                closed.append(ws)
            except Exception:
                closed.append(ws)
        for ws in closed:
            self._connections.discard(ws)
