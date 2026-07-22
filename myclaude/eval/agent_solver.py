"""A Solver that drives a real Agent against an eval task, recording a trace.

报告 B1 的消融实验（baseline / no-memory / no-subagent / …）需要一个"把真实
Agent 跑在隔离 fixture 上、并把轨迹写成 trace"的桥。AgentSolver 就是这座桥：

- 用 build_core_runtime 在 work_dir 上装配一个真实 Agent。
- 把 Agent 事件流映射成版本化 TraceEvent（llm_call / tool_call）。
- 通过构造参数开关 memory / fork，直接支持消融对比。

它是 eval 与 Agent 的真实集成点，需要真实模型才能端到端运行，因此不进单测；
harness 的其余部分用 stub solver 全覆盖测试。
"""
from __future__ import annotations

from pathlib import Path

from myclaude.config import ProviderConfig
from myclaude.eval.task import EvalTask
from myclaude.eval.trace import TraceWriter
from myclaude.permissions import PermissionMode


class AgentSolver:
    """把真实 Agent 跑在 fixture 上的 solver（支持消融开关）。"""

    def __init__(
        self,
        provider: ProviderConfig,
        *,
        memory_enabled: bool = True,
        enable_fork: bool = False,
        max_wall_time_seconds: float = 300.0,
    ) -> None:
        self.provider = provider
        self.memory_enabled = memory_enabled
        self.enable_fork = enable_fork
        self.max_wall_time_seconds = max_wall_time_seconds

    async def solve(self, task: EvalTask, work_dir: Path, trace: TraceWriter) -> None:
        # 延迟 import：避免 eval 包在无需真实运行时就拉起整个 runtime 依赖链。
        from myclaude.agent import (
            CacheContractEvent,
            ErrorEvent,
            LoopComplete,
            PermissionRequest,
            PermissionResponse,
            OrchestrationEvent,
            ToolResultEvent,
            ToolUseEvent,
            UsageEvent,
            VerificationEvent,
        )
        from myclaude.conversation import ConversationManager
        from myclaude.runtime import build_core_runtime
        from myclaude.usage import RunLimits

        runtime = build_core_runtime(
            self.provider,
            # eval 在隔离临时目录里运行，非交互——用 bypass 让 Agent 能真正改文件，
            # 由 diff 白名单 / 受保护文件 oracle 事后客观约束，而不是运行时挡下来。
            PermissionMode.BYPASS,
            work_dir=str(work_dir),
            memory_enabled=self.memory_enabled,
            worktree_enabled=False,
            workspace_trusted=True,
            run_limits=RunLimits(max_wall_time_seconds=self.max_wall_time_seconds),
        )
        agent = runtime.agent
        # fork 是消融维度：关闭时子 Agent 能力不参与，用于对比其对成功率/成本的贡献。
        agent.enable_fork = self.enable_fork  # type: ignore[attr-defined]

        conv = ConversationManager()
        conv.add_user_message(task.prompt)

        # 记录 tool_use 的目标，供 tool_call 事件填 target（重复读取/失败命令检测）。
        pending_targets: dict[str, str] = {}

        async for event in agent.run(conv):
            if isinstance(event, UsageEvent):
                trace.emit(
                    "llm_call",
                    provider=self.provider.name,
                    model=self.provider.model,
                    purpose="agent",
                    input_tokens=event.input_tokens,
                    output_tokens=event.output_tokens,
                    cache_read_tokens=getattr(event, "cache_read", None),
                    cache_write_tokens=getattr(event, "cache_creation", None),
                )
            elif isinstance(event, ToolUseEvent):
                args = event.arguments if isinstance(event.arguments, dict) else {}
                target = args.get("file_path") or args.get("command") or args.get("pattern")
                if target is not None:
                    pending_targets[event.tool_id] = str(target)
            elif isinstance(event, CacheContractEvent):
                trace.emit(
                    "cache_contract",
                    cache_fingerprint=event.fingerprint,
                    cache_hit_rate=event.request_hit_rate,
                    cache_break_reasons=list(event.break_reasons),
                    success=not event.unexpected_miss,
                )
            elif isinstance(event, VerificationEvent):
                trace.emit(
                    "verification",
                    verification_status=event.status,
                    verification_revision=event.revision,
                    success=event.status in {"passed", "waived", "not_required"},
                )
            elif isinstance(event, OrchestrationEvent):
                trace.emit(
                    "orchestration",
                    orchestration_mode=event.mode,
                    max_agents=event.max_agents,
                    success=True,
                )
            elif isinstance(event, ToolResultEvent):
                trace.emit(
                    "tool_call",
                    tool_name=event.tool_name,
                    tool_call_id=event.tool_id,
                    target=pending_targets.pop(event.tool_id, None),
                    result_size=len(event.output or ""),
                    error_type="tool_error" if event.is_error else None,
                    duration_ms=round(event.elapsed * 1000, 3),
                    success=not event.is_error,
                )
            elif isinstance(event, ErrorEvent):
                trace.emit("agent_error", error_type="error_event", success=False)
            elif isinstance(event, LoopComplete):
                trace.emit("agent_done", stop_reason="end_turn", success=True)
                break
            elif isinstance(event, PermissionRequest):
                # 隔离环境下不应触发（BYPASS），但兜底 fail-closed。
                event.future.set_result(PermissionResponse.DENY)
