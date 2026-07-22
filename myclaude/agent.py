from __future__ import annotations

import asyncio
import fnmatch
import inspect
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from pydantic import ValidationError

from myclaude.cache_contract import CacheContract, CacheInspection, CacheObservation
from myclaude.client import (
    ContextOverflowError,
    LLMClient,
    NetworkError,
    RateLimitError,
)
from myclaude.context import (
    CompactBoundary,
    CompactCircuitBreaker,
    CompactEvent,
    ContentReplacementRecord,
    ContentReplacementState,
    RecoveryState,
    append_replacement_records,
    apply_tool_result_budget,
    auto_compact,
    create_replacement_state,
    ensure_session_dir,
    load_replacement_records,
    reconstruct_replacement_state,
)
from myclaude.context.ledger import ContextLedger
from myclaude.conversation import ConversationManager, ToolResultBlock, ToolUseBlock
from myclaude.conversation import ThinkingBlock as ConvThinkingBlock
from myclaude.memory.auto_memory import MemoryManager
from myclaude.permissions import (
    Decision,
    PermissionChecker,
    PermissionMode,
)
from myclaude.hooks import HookContext, HookEngine, ToolRejectedError
from myclaude.hooks.engine import HookNotification
from myclaude.prompts import build_environment_context, build_plan_mode_reminder, build_system_prompt
from myclaude.orchestration import OrchestrationController
from myclaude.run_context import RunContext
from myclaude.tools import ToolRegistry
from myclaude.tools.ask_user import AskUserEvent, AskUserTool
from myclaude.tools.base import (
    MAX_OUTPUT_CHARS,
    StreamEnd,
    StreamEvent,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
    ToolResult,
)
from myclaude.usage import RunLimits, UsageSnapshot
from myclaude.verification import VerificationDecision, VerificationGate

log = logging.getLogger(__name__)

MEMORY_EXTRACTION_INTERVAL = 5
MAX_TOKENS_CEILING = 64000
MAX_OUTPUT_TOKENS_RECOVERIES = 3
MAX_REQUEST_RETRIES = 3


# ---------------------------------------------------------------------------
# AgentEvent 事件类型
# ---------------------------------------------------------------------------

@dataclass
class StreamText:
    text: str


@dataclass
class ThinkingText:
    text: str


@dataclass
class RetryEvent:
    reason: str
    wait: float = 0.0


@dataclass
class ToolUseEvent:
    tool_name: str
    tool_id: str
    arguments: dict[str, Any]


@dataclass
class ToolResultEvent:
    tool_id: str
    tool_name: str
    output: str
    is_error: bool
    elapsed: float
    artifact_path: str = ""
    truncated: bool = False
    total_bytes: int = 0
    next_offset: int | None = None
    content_blocks: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnComplete:
    turn: int


@dataclass
class LoopComplete:
    total_turns: int


@dataclass
class UsageEvent:
    input_tokens: int
    output_tokens: int
    cache_read: int = 0
    cache_creation: int = 0
    estimated_cost_usd: float = 0.0


@dataclass
class CacheContractEvent:
    fingerprint: str
    break_reasons: tuple[str, ...]
    request_hit_rate: float
    cumulative_hit_rate: float
    cache_read: int
    cache_creation: int
    unexpected_miss: bool = False


@dataclass
class VerificationEvent:
    status: str
    message: str = ""
    blocked: bool = False
    revision: int = 0
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class OrchestrationEvent:
    mode: str
    max_agents: int
    reason: str


@dataclass
class ErrorEvent:
    message: str


@dataclass
class CompactNotification:
    before_tokens: int
    message: str
    # 结构化 boundary（摘要 + 原文保留尾部），UI/session 层用它持久化 compact_boundary 记录。
    # 失败路径下为 None。
    boundary: "CompactBoundary | None" = None


@dataclass
class HookEvent:
    hook_id: str
    event: str
    output: str
    success: bool


class PermissionResponse(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ALLOW_ALWAYS = "allow_always"


@dataclass
class PermissionRequest:
    tool_name: str
    description: str
    future: asyncio.Future[PermissionResponse]


AgentEvent = (
    StreamText
    | ThinkingText
    | RetryEvent
    | ToolUseEvent
    | ToolResultEvent
    | TurnComplete
    | LoopComplete
    | UsageEvent
    | CacheContractEvent
    | VerificationEvent
    | OrchestrationEvent
    | ErrorEvent
    | PermissionRequest
    | AskUserEvent
    | CompactNotification
    | HookEvent
)


# ---------------------------------------------------------------------------
# LLM 响应收集器
# ---------------------------------------------------------------------------

@dataclass
class ThinkingBlock:
    thinking: str
    signature: str


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCallComplete] = field(default_factory=list)
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list)
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0


class StreamCollector:
    def __init__(self) -> None:
        self.response = LLMResponse()

    async def consume(
        self, stream: AsyncIterator[StreamEvent]
    ) -> AsyncIterator[AgentEvent]:
        async for event in stream:
            if isinstance(event, TextDelta):
                self.response.text += event.text
                yield StreamText(text=event.text)
            elif isinstance(event, ThinkingDelta):
                yield ThinkingText(text=event.text)
            elif isinstance(event, ThinkingComplete):
                self.response.thinking_blocks.append(
                    ThinkingBlock(thinking=event.thinking, signature=event.signature)
                )
            elif isinstance(event, ToolCallStart):
                pass
            elif isinstance(event, ToolCallDelta):
                pass
            elif isinstance(event, ToolCallComplete):
                self.response.tool_calls.append(event)
                yield ToolUseEvent(
                    tool_name=event.tool_name,
                    tool_id=event.tool_id,
                    arguments=event.arguments,
                )
            elif isinstance(event, StreamEnd):
                self.response.stop_reason = event.stop_reason
                self.response.input_tokens = event.input_tokens
                self.response.output_tokens = event.output_tokens
                self.response.cache_read = event.cache_read
                self.response.cache_creation = event.cache_creation


# ---------------------------------------------------------------------------
# tool 批量执行
# ---------------------------------------------------------------------------

@dataclass
class ToolBatch:
    concurrent: bool
    calls: list[ToolCallComplete]


def partition_tool_calls(
    tool_calls: list[ToolCallComplete],
    registry: ToolRegistry,
) -> list[ToolBatch]:
    batches: list[ToolBatch] = []
    for tc in tool_calls:
        tool = registry.get(tc.tool_name)
        safe = (
            tool is not None
            and tool.is_call_concurrency_safe(tc.arguments)
            and registry.is_enabled(tc.tool_name)
        )

        if safe and batches and batches[-1].concurrent:
            batches[-1].calls.append(tc)
        else:
            batches.append(ToolBatch(concurrent=safe, calls=[tc]))
    return batches


@dataclass
class _ToolExecResult:
    tool_id: str
    tool_name: str
    result: ToolResult
    elapsed: float
    is_unknown: bool


# ---------------------------------------------------------------------------
# Agent 主循环
# ---------------------------------------------------------------------------

class Agent:
    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        protocol: str,
        work_dir: str = ".",
        max_iterations: int = 0,
        permission_checker: PermissionChecker | None = None,
        context_window: int = 200_000,
        instructions_content: str = "",
        memory_manager: MemoryManager | None = None,
        hook_engine: HookEngine | None = None,
        run_limits: RunLimits | None = None,
        recall_fn: Callable[[str], Awaitable[str]] | None = None,
        instruction_resolver: Any | None = None,
        enable_runtime_contracts: bool = False,
        persist_runtime_contracts: bool = True,
    ) -> None:
        self.client = client
        self.registry = registry
        self.protocol = protocol
        self.work_dir = work_dir
        self.max_iterations = max_iterations
        self.permission_checker = permission_checker
        self.permission_mode: PermissionMode = (
            permission_checker.mode if permission_checker else PermissionMode.DEFAULT
        )
        self.context_window = context_window
        self.agent_id: str = uuid.uuid4().hex[:12]
        self.session_dir = ensure_session_dir(work_dir, self.agent_id)
        self.cache_contract: CacheContract | None = None
        self.context_ledger: ContextLedger | None = None
        self.verification_gate: VerificationGate | None = None
        self.orchestration: OrchestrationController | None = None
        if enable_runtime_contracts:
            self.cache_contract = CacheContract(
                self.work_dir,
                self.agent_id,
                persist=persist_runtime_contracts,
            )
            self.context_ledger = ContextLedger(
                self.work_dir,
                self.agent_id,
                persist=persist_runtime_contracts,
            )
            self.verification_gate = VerificationGate()
            self.orchestration = OrchestrationController()
        self._ledger_injected_version = 0
        self._orchestration_signature = ""
        self.compact_breaker = CompactCircuitBreaker()
        self.replacement_state: ContentReplacementState = create_replacement_state()
        # 保存重建工作上下文所需的快照，在 Layer 2 压缩对话后使用：
        # 最近的文件读取和 skill 调用。每次 ReadFile / skill 调用时记录，
        # auto_compact 触发阈值时消费。
        self.recovery_state: RecoveryState = RecoveryState()
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.instructions_content = instructions_content
        self.instruction_resolver = instruction_resolver
        self.memory_manager = memory_manager
        self.hook_engine = hook_engine
        self.run_limits = run_limits or RunLimits()
        # RunContext 是本次 run 的 deadline / 取消 / 后台任务 / 背压的单一真相源。
        # 在 run() 开始时构造；此前为 None（例如 fork 尚未进入循环）。
        self._run_context: RunContext | None = None
        self._loop_count = 0
        self._memory_state_at_run_start: tuple[tuple[str, int, int], ...] = ()
        # 记忆提取合并策略：
        # _extracting: 标记是否有提取正在进行
        # _pending_extraction: 提取期间又触发了新请求，保留最新快照做尾随提取
        self._extracting = False
        self._pending_extraction: ConversationManager | None = None
        self._consolidator: MemoryConsolidator | None = None
        if memory_manager is not None:
            from myclaude.memory.consolidation import MemoryConsolidator
            self._consolidator = MemoryConsolidator(work_dir)
        self._session_id: str = ""
        self.active_skills: dict[str, str] = {}
        self._active_skill_allowed_tools: set[str] = set()
        self._active_skill_disallowed_tools: set[str] = set()
        self._skill_catalog: str = ""
        self._agent_catalog: str = ""
        self._agent_catalog_list: list[tuple[str, str]] = []
        self.parent_id: str | None = None
        self.trace_id: str | None = None
        self.coordinator_mode: bool = False
        self.team_name: str = ""
        self._team_manager: Any = None
        self.notification_fn: Callable[[], list[str]] | None = None
        self.file_history: Any = None

        # 非阻塞 memory recall：prefetch task 与主 LLM 调用并行，工具执行后注入。
        # recall_fn 由共享 Runtime 注入，使 TUI / Headless / Remote 三入口获得一致的
        # 召回语义；TUI 仍可自行预置 memory_recall_task（此时 Agent 不再重复启动）。
        self.recall_fn: Callable[[str], Awaitable[str]] | None = recall_fn
        self.memory_recall_task: Any | None = None
        self._memory_recall_consumed: bool = False
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._queued_user_messages: deque[str] = deque()
        self._steering_event = asyncio.Event()

    def queue_user_message(self, message: str) -> int:
        text = message.strip()
        if text:
            self._queued_user_messages.append(text)
            self._steering_event.set()
        return len(self._queued_user_messages)

    def _inject_queued_user_messages(
        self, conversation: ConversationManager
    ) -> int:
        count = 0
        while self._queued_user_messages:
            conversation.add_user_message(self._queued_user_messages.popleft())
            count += 1
        if not self._queued_user_messages:
            self._steering_event.clear()
        return count

    def _usage_snapshot(self) -> UsageSnapshot:
        ledger = getattr(self.client, "usage_ledger", None)
        if ledger is None:
            return UsageSnapshot()
        return ledger.snapshot()

    def _sync_usage(self, response: LLMResponse) -> UsageSnapshot:
        snapshot = self._usage_snapshot()
        if snapshot.request_count:
            self.total_input_tokens = snapshot.input_tokens
            self.total_output_tokens = snapshot.output_tokens
            return snapshot
        self.total_input_tokens += response.input_tokens
        self.total_output_tokens += response.output_tokens
        return UsageSnapshot(
            input_tokens=self.total_input_tokens,
            output_tokens=self.total_output_tokens,
        )

    @staticmethod
    def _cache_contract_event(
        observation: CacheObservation,
    ) -> CacheContractEvent:
        return CacheContractEvent(
            fingerprint=observation.fingerprint,
            break_reasons=observation.break_reasons,
            request_hit_rate=observation.request_hit_rate,
            cumulative_hit_rate=observation.cumulative_hit_rate,
            cache_read=observation.cache_read,
            cache_creation=observation.cache_creation,
            unexpected_miss=observation.unexpected_miss,
        )

    def _verification_event(
        self, decision: VerificationDecision
    ) -> VerificationEvent:
        revision = (
            self.verification_gate.revision
            if self.verification_gate is not None
            else 0
        )
        return VerificationEvent(
            status=decision.status,
            message=decision.message,
            blocked=decision.blocked,
            revision=revision,
            evidence=[item.to_dict() for item in decision.evidence],
        )

    def _remaining_wall_time(self) -> float | None:
        if self._run_context is None:
            return None
        return self._run_context.remaining()

    async def _await_run(self, awaitable: Awaitable[Any]) -> Any:
        if self._run_context is None:
            return await awaitable
        return await self._run_context.wait_for(awaitable)

    def _limit_reason(
        self,
        iteration: int,
        *,
        usage: UsageSnapshot | None = None,
    ) -> str | None:
        if self.max_iterations > 0 and iteration > self.max_iterations:
            return f"maximum iterations reached ({self.max_iterations})"
        if self.run_limits.max_turns > 0 and iteration > self.run_limits.max_turns:
            return f"maximum turns reached ({self.run_limits.max_turns})"
        remaining = self._remaining_wall_time()
        if remaining is not None and remaining <= 0:
            return "maximum wall time reached"
        snapshot = usage or self._usage_snapshot()
        if (
            self.run_limits.max_total_tokens > 0
            and snapshot.total_tokens >= self.run_limits.max_total_tokens
        ):
            return f"maximum token budget reached ({self.run_limits.max_total_tokens})"
        if (
            self.run_limits.max_cost_usd > 0
            and snapshot.estimated_cost_usd >= self.run_limits.max_cost_usd
        ):
            return f"maximum cost reached (${self.run_limits.max_cost_usd:.4f})"
        return None

    @property
    def work_dir(self) -> str:
        return self._work_dir

    @work_dir.setter
    def work_dir(self, value: str) -> None:
        resolved = str(Path(value).expanduser().resolve())
        self._work_dir = resolved
        registry = getattr(self, "registry", None)
        if registry is not None:
            registry.set_work_dir(resolved)
        checker = getattr(self, "permission_checker", None)
        if checker is not None:
            checker.sandbox.set_project_root(resolved)
        conversation = getattr(self, "_current_conversation", None)
        if conversation is not None and hasattr(self, "active_skills"):
            conversation.inject_environment(
                build_environment_context(
                    resolved,
                    self.active_skills,
                    self._skill_catalog,
                    self._agent_catalog,
                )
            )
        resolver = getattr(self, "instruction_resolver", None)
        if resolver is not None and str(resolver.work_dir) != resolved:
            from myclaude.memory.instructions import InstructionResolver

            refreshed = InstructionResolver(
                resolved, include_project=resolver.include_project
            )
            self.instruction_resolver = refreshed
            initial = refreshed.initial_content
            if initial and initial not in getattr(self, "instructions_content", ""):
                if self.instructions_content:
                    self.instructions_content += "\n\n---\n\n" + initial
                else:
                    self.instructions_content = initial

    @property
    def session_id(self) -> str:
        return self._session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        normalized = str(value or "")
        self._session_id = normalized
        owner_id = normalized or getattr(self, "agent_id", "")
        cache_contract = getattr(self, "cache_contract", None)
        if cache_contract is not None and owner_id:
            cache_contract.rebind(owner_id)
        context_ledger = getattr(self, "context_ledger", None)
        if context_ledger is not None and owner_id:
            context_ledger.rebind(owner_id)
            self._ledger_injected_version = 0
            verification_gate = getattr(self, "verification_gate", None)
            if verification_gate is not None:
                verification_gate.restore(context_ledger.verification)

    @property
    def _transcript_path(self) -> str:
        if self.session_id:
            return str(Path(self.work_dir) / ".myclaude" / "sessions" / f"{self.session_id}.jsonl")
        return ""

    def _spawn_background(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def done(completed: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                error = completed.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                log.debug("Background agent task failed: %s", error)

        task.add_done_callback(done)
        return task

    async def _consume_memory_recall(
        self, conversation: ConversationManager, *, wait: bool = False
    ) -> None:
        task = self.memory_recall_task
        if task is None or self._memory_recall_consumed:
            return
        if not wait and not task.done():
            return
        try:
            recall = (
                await self._await_run(task)
                if wait
                else await task
            )
            if recall:
                conversation.add_system_reminder(recall)
        except TimeoutError:
            task.cancel()
            raise
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.debug("Memory recall failed: %s", exc)
        finally:
            if task is self.memory_recall_task:
                self._memory_recall_consumed = True
                self.memory_recall_task = None

    @staticmethod
    def _latest_user_query(conversation: ConversationManager) -> str:
        """取最近一条真实用户消息作为召回 query（跳过工具结果消息）。"""
        for msg in reversed(conversation.history):
            if (
                msg.role == "user"
                and msg.source in ("", "user")
                and msg.content
                and not msg.tool_results
            ):
                return msg.content
        return ""

    def _inject_context_ledger(
        self, conversation: ConversationManager
    ) -> None:
        ledger = self.context_ledger
        if ledger is None or ledger.version <= self._ledger_injected_version:
            return
        content = ledger.render_updates(self._ledger_injected_version)
        if content:
            conversation.add_system_reminder(content)
        self._ledger_injected_version = ledger.version

    def _refresh_adaptive_contracts(
        self,
        conversation: ConversationManager,
        *,
        new_task: bool,
    ) -> OrchestrationEvent | None:
        query = self._latest_user_query(conversation)
        if new_task and query and self.verification_gate is not None:
            self.verification_gate.start_task()
        if self.context_ledger is not None:
            if new_task:
                self.context_ledger.start_task(query)
            else:
                self.context_ledger.apply_steering(query)
        event: OrchestrationEvent | None = None
        if self.orchestration is not None and query:
            decision = self.orchestration.decide(
                query,
                self.registry,
                self.run_limits,
                plan_mode=self.plan_mode,
            )
            signature = (
                f"{decision.mode}:{decision.max_agents}:{decision.reason}"
            )
            if signature != self._orchestration_signature:
                conversation.add_system_reminder(self.orchestration.reminder())
                self._orchestration_signature = signature
            if self.context_ledger is not None:
                self.context_ledger.set_orchestration(decision.to_dict())
            event = OrchestrationEvent(
                mode=decision.mode,
                max_agents=decision.max_agents,
                reason=decision.reason,
            )
        if (
            self.context_ledger is not None
            and self.verification_gate is not None
        ):
            self.context_ledger.set_verification(
                self.verification_gate.snapshot()
            )
        self._inject_context_ledger(conversation)
        return event

    def _maybe_start_recall(self, conversation: ConversationManager) -> None:
        """由共享 Runtime 注入 recall_fn 时，Agent 自行启动动态召回。

        这让 Headless / Remote 与 TUI 获得一致的召回语义，而不再是 TUI 专属。
        若 memory_recall_task 已被外部设置（TUI 的 prefetch 路径），则不重复启动，
        保持既有行为与相关测试不变。召回任务登记进 RunContext，随 run 生命周期
        统一 drain / cancel，不会成为无人认领的后台任务。
        """
        if self.recall_fn is None:
            return
        if self.memory_recall_task is not None:
            return
        query = self._latest_user_query(conversation)
        if not query:
            return
        task = asyncio.create_task(self.recall_fn(query))
        self.memory_recall_task = task
        self._memory_recall_consumed = False
        if self._run_context is not None:
            self._run_context.register(task)

    @property
    def plan_mode(self) -> bool:
        return self.permission_mode == PermissionMode.PLAN

    _plan_path_cache: Path | None = None

    def _get_plan_path(self) -> Path:
        if self._plan_path_cache is not None:
            return self._plan_path_cache
        import random
        import datetime
        _ADJECTIVES = ["bold", "bright", "calm", "cool", "deep", "fair", "fast", "fine",
                       "glad", "keen", "kind", "lean", "mild", "neat", "pure", "safe",
                       "slim", "soft", "tall", "warm", "wise", "grand", "swift", "vivid"]
        _NOUNS = ["sketch", "draft", "spark", "bloom", "trail", "ridge", "creek", "grove",
                  "cliff", "cloud", "field", "forge", "frost", "haven", "pearl", "stone",
                  "storm", "river", "tower", "delta", "flame", "orbit", "pulse", "shore"]
        plans_dir = Path(self.work_dir) / ".myclaude" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%m%d-%H%M")
        slug = f"{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}-{ts}"
        self._plan_path_cache = plans_dir / f"{slug}.md"
        return self._plan_path_cache

    def set_permission_mode(self, mode: PermissionMode) -> None:
        self.permission_mode = mode
        if self.permission_checker:
            self.permission_checker.mode = mode

    def activate_skill(
        self,
        name: str,
        prompt_body: str,
        *,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
    ) -> bool:
        if self.active_skills.get(name) == prompt_body:
            return False
        self.active_skills[name] = prompt_body
        allowed = getattr(self, "_active_skill_allowed_tools", set())
        disallowed = getattr(self, "_active_skill_disallowed_tools", set())
        allowed.update(allowed_tools or [])
        disallowed.update(disallowed_tools or [])
        self._active_skill_allowed_tools = allowed
        self._active_skill_disallowed_tools = disallowed
        # 统一在领域方法里记录 recovery，确保无论从哪个入口激活（模型工具
        # LoadSkill、斜杠命令 inline），auto-compact 后的恢复附件都能带上该 skill
        # 的 SOP，不会因入口不同而静默丢失。getattr 与 executor fork 路径保持一致，
        # 也对未完整初始化的场景（如测试 mock）稳健。
        recovery = getattr(self, "recovery_state", None)
        if recovery is not None:
            recovery.record_skill_invocation(name, prompt_body)
        context_ledger = getattr(self, "context_ledger", None)
        if context_ledger is not None:
            context_ledger.update(decisions=[f"activated skill: {name}"])
        return True

    def clear_active_skills(self) -> None:
        self.active_skills.clear()
        self._active_skill_allowed_tools.clear()
        self._active_skill_disallowed_tools.clear()

    def set_skill_catalog(self, catalog: str) -> None:
        self._skill_catalog = catalog


    def set_agent_catalog(self, catalog: str, catalog_list: list[tuple[str, str]] | None = None) -> None:
        self._agent_catalog = catalog
        if catalog_list is not None:
            self._agent_catalog_list = catalog_list

    def _build_hook_context(self, event: str, **kwargs: str | dict) -> HookContext:
        return HookContext(
            event_name=event,
            tool_name=str(kwargs.get("tool_name", "")),
            tool_args=kwargs.get("tool_args", {}),
            file_path=str(kwargs.get("file_path", "")),
            message=str(kwargs.get("message", "")),
            error=str(kwargs.get("error", "")),
        )

    def _infer_file_path(self, args: dict) -> str:
        return str(args.get("file_path", args.get("path", "")))

    @staticmethod
    def _skill_tool_pattern_matches(
        pattern: str, tool_name: str, arguments: dict[str, Any], tool: Any
    ) -> bool:
        pattern = pattern.strip()
        selector = ""
        name_pattern = pattern
        if "(" in pattern and pattern.endswith(")"):
            name_pattern, selector = pattern.split("(", 1)
            selector = selector[:-1]
        if not fnmatch.fnmatchcase(tool_name.casefold(), name_pattern.casefold()):
            return False
        if not selector:
            return True
        scope = tool.permission_scope(arguments)
        values = [scope.content, scope.path, str(arguments.get("command", ""))]
        selector_variants = {selector}
        if ":" in selector:
            selector_variants.add(selector.replace(":", " ", 1))
        return any(
            fnmatch.fnmatchcase(value, candidate)
            for value in values
            if value
            for candidate in selector_variants
        )

    def _skill_tool_policy(
        self, tool_name: str, arguments: dict[str, Any], tool: Any
    ) -> tuple[bool, bool]:
        denied = any(
            self._skill_tool_pattern_matches(pattern, tool_name, arguments, tool)
            for pattern in self._active_skill_disallowed_tools
        )
        allowed = any(
            self._skill_tool_pattern_matches(pattern, tool_name, arguments, tool)
            for pattern in self._active_skill_allowed_tools
        )
        return denied, allowed

    def _drain_hook_events(self) -> list[HookEvent]:
        if not self.hook_engine:
            return []
        return [
            HookEvent(
                hook_id=n.hook_id,
                event=n.event,
                output=n.output,
                success=n.success,
            )
            for n in self.hook_engine.drain_notifications()
        ]

    def prepare_conversation(self, conversation: ConversationManager) -> str:
        """Inject transient runtime context before a delivery surface records cursors.

        Environment and memory messages are intentionally part of the provider
        input but not the durable user transcript.  Preparing them before the
        UI appends a user message prevents front insertions from invalidating
        session persistence cursors.
        """
        env_context = build_environment_context(
            self.work_dir,
            self.active_skills,
            self._skill_catalog,
            self._agent_catalog,
        )
        conversation.inject_environment(env_context)
        memory_content = self.memory_manager.load() if self.memory_manager else ""
        conversation.inject_long_term_memory(
            self.instructions_content,
            memory_content,
        )
        return env_context

    async def run(self, conversation: ConversationManager) -> AsyncIterator[AgentEvent]:
        self._current_conversation = conversation
        if self.memory_manager is not None:
            self._memory_state_at_run_start = self.memory_manager.state_token()
        env_context = self.prepare_conversation(conversation)
        orchestration_event = self._refresh_adaptive_contracts(
            conversation, new_task=True
        )
        if orchestration_event is not None:
            yield orchestration_event
        # The deadline starts before lifecycle hooks so every external await in
        # this run shares the same wall-clock budget.
        self._run_context = RunContext.from_wall_time(
            self.run_limits.max_wall_time_seconds,
        )

        if self.hook_engine:
            ctx = self._build_hook_context("session_start")
            try:
                await self._await_run(
                    self.hook_engine.run_hooks("session_start", ctx)
                )
            except TimeoutError:
                yield ErrorEvent(
                    message="Agent run limit exceeded: maximum wall time reached"
                )
                return
            for he in self._drain_hook_events():
                yield he

        iteration = 0
        consecutive_unknown = 0
        max_tokens_escalated = False
        output_recoveries = 0
        # 动态召回移入共享 Runtime：只要注入了 recall_fn 且本轮尚未有召回任务
        # （TUI 会自行 prefetch 并预置 memory_recall_task），就在这里启动召回。
        # 这样 Headless / Remote 也能获得与 TUI 一致的召回语义，而不再是 UI 专属。
        self._maybe_start_recall(conversation)

        while True:
            iteration += 1

            limit_reason = self._limit_reason(iteration)
            if limit_reason is not None:
                yield ErrorEvent(
                    message=f"Agent run limit exceeded: {limit_reason}"
                )
                break

            if self.hook_engine:
                ctx = self._build_hook_context("turn_start")
                try:
                    await self._await_run(
                        self.hook_engine.run_hooks("turn_start", ctx)
                    )
                except TimeoutError:
                    yield ErrorEvent(
                        message="Agent run limit exceeded: maximum wall time reached"
                    )
                    break
                for he in self._drain_hook_events():
                    yield he

            self._consume_mailbox(conversation)
            queued_at_start = self._inject_queued_user_messages(conversation)
            if queued_at_start:
                orchestration_event = self._refresh_adaptive_contracts(
                    conversation, new_task=False
                )
                if orchestration_event is not None:
                    yield orchestration_event
            await self._consume_memory_recall(conversation)
            if self.notification_fn:
                for note in self.notification_fn():
                    conversation.add_system_reminder(note)
            self._inject_context_ledger(conversation)

            if self.hook_engine:
                ctx = self._build_hook_context("pre_send")
                try:
                    await self._await_run(
                        self.hook_engine.run_hooks("pre_send", ctx)
                    )
                except TimeoutError:
                    yield ErrorEvent(
                        message="Agent run limit exceeded: maximum wall time reached"
                    )
                    break
                for he in self._drain_hook_events():
                    yield he

            hook_prompts = (
                self.hook_engine.get_prompt_messages() if self.hook_engine else None
            )
            system = build_system_prompt(
                hook_prompts=hook_prompts,
                coordinator_mode=self.coordinator_mode,
                agent_catalog=self._agent_catalog_list or None,
                work_dir=self.work_dir,
            )

            if self.plan_mode:
                plan_path = str(self._get_plan_path())
                if self.permission_checker:
                    self.permission_checker.plan_file_path = plan_path
                plan_exists = self._get_plan_path().exists()
                plan_reminder = build_plan_mode_reminder(
                    plan_path, plan_exists, iteration
                )
                conversation.add_system_reminder(plan_reminder)

            if self.hook_engine:
                for note in self.hook_engine.drain_notifications():
                    conversation.add_system_reminder(
                        f"Hook [{note.hook_id}] {note.event}: {note.output}"
                    )

            deferred_names = self.registry.get_deferred_tool_names()
            if deferred_names and not self.registry.native_deferred_loading:
                conversation.add_system_reminder(
                    "The following deferred tools are available via ToolSearch. "
                    "Their schemas are NOT loaded - use ToolSearch with "
                    'query "select:<name>[,<name>...]" to load tool schemas before calling them:\n'
                    + "\n".join(deferred_names)
                )

            tools = self.registry.get_all_schemas(self.protocol)

            # Layer 1: apply tool-result budget（就地修改 conversation）
            new_records = apply_tool_result_budget(
                conversation, self.session_dir, self.replacement_state
            )
            if new_records:
                append_replacement_records(self.session_dir, new_records)

            # Layer 2: 接近 context window 上限时自动 compact
            # tool-result budget 已就地修改 conversation，直接用 conversation.history 估算
            try:
                compact_result = await self._await_run(
                    auto_compact(
                        conversation,
                        self.client,
                        self.context_window,
                        self.session_dir,
                        protocol=self.protocol,
                        breaker=self.compact_breaker,
                        recovery=self.recovery_state,
                        tool_schemas=self.registry.get_all_schemas(self.protocol),
                        transcript_path=self._transcript_path,
                        context_ledger=(
                            self.context_ledger.render_for_prompt()
                            if self.context_ledger is not None
                            else ""
                        ),
                        active_skill_names=sorted(self.active_skills),
                    )
                )
            except TimeoutError:
                yield ErrorEvent(
                    message="Agent run limit exceeded: maximum wall time reached"
                )
                break
            if isinstance(compact_result, CompactEvent):
                conversation.inject_environment(env_context)
                mem = self.memory_manager.load() if self.memory_manager else ""
                conversation.inject_long_term_memory(
                    self.instructions_content, mem
                )
                # 压缩后重新应用 budget（就地修改）
                apply_tool_result_budget(
                    conversation, self.session_dir, self.replacement_state
                )
                if self.context_ledger is not None:
                    self._ledger_injected_version = self.context_ledger.version
                yield CompactNotification(
                    before_tokens=compact_result.before_tokens,
                    message=f"上下文已压缩（压缩前 {compact_result.before_tokens:,} tokens）",
                    boundary=compact_result.boundary,
                )
            elif isinstance(compact_result, str):
                yield ErrorEvent(message=compact_result)

            request_attempt = 0
            overflow_recovered = False
            fatal_request_error = False
            while True:
                collector = StreamCollector()
                received_event = False
                cache_inspection: CacheInspection | None = None
                if self.cache_contract is not None:
                    cache_inspection = self.cache_contract.inspect(
                        model=str(getattr(self.client, "model", "")),
                        system=system,
                        tools=tools,
                        messages=conversation.history,
                    )
                try:
                    llm_stream = self.client.stream(
                        conversation, system=system, tools=tools
                    )
                    async with self._run_context.timeout_scope():
                        async for event in collector.consume(llm_stream):
                            received_event = True
                            yield event
                    break
                except ContextOverflowError as exc:
                    if received_event or overflow_recovered:
                        yield ErrorEvent(message=str(exc))
                        fatal_request_error = True
                        break
                    overflow_recovered = True
                    try:
                        recovery_result = await self._await_run(
                            auto_compact(
                                conversation,
                                self.client,
                                self.context_window,
                                self.session_dir,
                                protocol=self.protocol,
                                manual=True,
                                breaker=self.compact_breaker,
                                recovery=self.recovery_state,
                                tool_schemas=self.registry.get_all_schemas(
                                    self.protocol
                                ),
                                transcript_path=self._transcript_path,
                                context_ledger=(
                                    self.context_ledger.render_for_prompt()
                                    if self.context_ledger is not None
                                    else ""
                                ),
                                active_skill_names=sorted(self.active_skills),
                            )
                        )
                    except TimeoutError:
                        yield ErrorEvent(
                            message=(
                                "Agent run limit exceeded: "
                                "maximum wall time reached"
                            )
                        )
                        fatal_request_error = True
                        break
                    if not isinstance(recovery_result, CompactEvent):
                        detail = recovery_result if isinstance(recovery_result, str) else str(exc)
                        yield ErrorEvent(message=f"Reactive compaction failed: {detail}")
                        fatal_request_error = True
                        break
                    conversation.inject_environment(env_context)
                    memory = self.memory_manager.load() if self.memory_manager else ""
                    conversation.inject_long_term_memory(
                        self.instructions_content, memory
                    )
                    apply_tool_result_budget(
                        conversation, self.session_dir, self.replacement_state
                    )
                    if self.context_ledger is not None:
                        self._ledger_injected_version = self.context_ledger.version
                    yield CompactNotification(
                        before_tokens=recovery_result.before_tokens,
                        message="API context overflow recovered by compacting once",
                        boundary=recovery_result.boundary,
                    )
                    yield RetryEvent(reason="context overflow recovery")
                except TimeoutError:
                    yield ErrorEvent(message="Agent run limit exceeded: maximum wall time reached")
                    fatal_request_error = True
                    break
                except (RateLimitError, NetworkError) as exc:
                    request_attempt += 1
                    if received_event or request_attempt >= MAX_REQUEST_RETRIES:
                        raise
                    wait = (
                        exc.retry_after
                        if isinstance(exc, RateLimitError) and exc.retry_after
                        else float(2 ** (request_attempt - 1))
                    )
                    yield RetryEvent(reason=str(exc), wait=wait)
                    # 退避睡眠必须受总 deadline 约束：不能睡过 deadline 再回来发现
                    # 早已超时。若剩余预算不足以完成退避，直接判定超时退出。
                    if not await self._run_context.sleep(wait):
                        yield ErrorEvent(
                            message="Agent run limit exceeded: maximum wall time reached"
                        )
                        fatal_request_error = True
                        break

            if fatal_request_error:
                break

            response = collector.response
            if self.cache_contract is not None and cache_inspection is not None:
                cache_observation = self.cache_contract.complete(
                    cache_inspection,
                    input_tokens=response.input_tokens,
                    cache_read=response.cache_read,
                    cache_creation=response.cache_creation,
                )
                yield self._cache_contract_event(cache_observation)

            if self.hook_engine:
                ctx = self._build_hook_context("post_receive", message=response.text)
                try:
                    await self._await_run(
                        self.hook_engine.run_hooks("post_receive", ctx)
                    )
                except TimeoutError:
                    yield ErrorEvent(
                        message="Agent run limit exceeded: maximum wall time reached"
                    )
                    break
                for he in self._drain_hook_events():
                    yield he

            usage_snapshot = self._sync_usage(response)
            yield UsageEvent(
                input_tokens=self.total_input_tokens,
                output_tokens=self.total_output_tokens,
                cache_read=usage_snapshot.cache_read,
                cache_creation=usage_snapshot.cache_creation,
                estimated_cost_usd=usage_snapshot.estimated_cost_usd,
            )

            conv_thinking = [
                ConvThinkingBlock(thinking=tb.thinking, signature=tb.signature)
                for tb in response.thinking_blocks
            ]

            usage_limit = self._limit_reason(iteration, usage=usage_snapshot)
            if usage_limit is not None:
                tool_uses = [
                    ToolUseBlock(
                        tool_use_id=tc.tool_id,
                        tool_name=tc.tool_name,
                        arguments=tc.arguments,
                    )
                    for tc in response.tool_calls
                ]
                conversation.add_assistant_message(
                    response.text, tool_uses, thinking_blocks=conv_thinking
                )
                if response.tool_calls:
                    conversation.add_tool_results_message(
                        [
                            ToolResultBlock(
                                tool_use_id=tc.tool_id,
                                content=f"Skipped: run limit exceeded ({usage_limit})",
                                is_error=True,
                            )
                            for tc in response.tool_calls
                        ]
                    )
                yield ErrorEvent(message=f"Agent run limit exceeded: {usage_limit}")
                break

            if response.stop_reason == "max_tokens":
                if not max_tokens_escalated:
                    self.client.set_max_output_tokens(MAX_TOKENS_CEILING)
                    max_tokens_escalated = True
                    if response.text:
                        conversation.add_assistant_message(
                            response.text, thinking_blocks=conv_thinking
                        )
                        conversation.add_user_message(
                            "Output token limit hit. Resume directly from where you stopped. "
                            "Do not apologize or repeat previous content. Pick up mid-thought if needed."
                        )
                    yield RetryEvent(reason="max_tokens escalation")
                    continue
                elif output_recoveries < MAX_OUTPUT_TOKENS_RECOVERIES:
                    output_recoveries += 1
                    conversation.add_assistant_message(
                        response.text, thinking_blocks=conv_thinking
                    )
                    conversation.add_user_message(
                        "Output token limit hit. Resume directly from where you stopped. "
                        "Break remaining work into smaller pieces."
                    )
                    yield RetryEvent(
                        reason=f"max_tokens recovery {output_recoveries}/{MAX_OUTPUT_TOKENS_RECOVERIES}"
                    )
                    continue
                else:
                    # A truncated response may contain a syntactically complete-looking
                    # tool call.  It is still untrusted until the model finishes normally.
                    yield ErrorEvent(
                        message="Model repeatedly exhausted its output token limit"
                    )
                    break
            else:
                output_recoveries = 0

            if response.stop_reason not in (
                "",
                "end_turn",
                "stop",
                "tool_use",
                "max_tokens",
            ):
                yield ErrorEvent(
                    message=f"Model stopped unexpectedly: {response.stop_reason}"
                )
                break

            if not response.tool_calls:
                conversation.add_assistant_message(
                    response.text, thinking_blocks=conv_thinking
                )
                if self.verification_gate is not None:
                    verification = self.verification_gate.assess_completion()
                    if self.context_ledger is not None:
                        self.context_ledger.set_verification(
                            self.verification_gate.snapshot()
                        )
                    event = self._verification_event(verification)
                    if verification.blocked:
                        conversation.add_system_reminder(verification.message)
                        self._inject_context_ledger(conversation)
                        yield event
                        yield TurnComplete(turn=iteration)
                        continue
                    if verification.status != "not_required" or verification.message:
                        yield event
                self._loop_count += 1
                if self._should_extract_memories(conversation):
                    self._spawn_background(
                        self._extract_memories(conversation.snapshot())
                    )
                if self._consolidator is not None:
                    self._spawn_background(
                        self._consolidator.maybe_run(
                            self.client,
                            conversation.snapshot(),
                            self.protocol,
                            run_limits=self.run_limits,
                        )
                    )
                if self.memory_recall_task and not self._memory_recall_consumed:
                    try:
                        await self._consume_memory_recall(
                            conversation, wait=True
                        )
                    except TimeoutError:
                        yield ErrorEvent(
                            message=(
                                "Agent run limit exceeded: "
                                "maximum wall time reached"
                            )
                        )
                        break
                queued_count = self._inject_queued_user_messages(conversation)
                if queued_count:
                    orchestration_event = self._refresh_adaptive_contracts(
                        conversation, new_task=False
                    )
                    if orchestration_event is not None:
                        yield orchestration_event
                if self.hook_engine:
                    ctx = self._build_hook_context("turn_end")
                    try:
                        await self._await_run(
                            self.hook_engine.run_hooks("turn_end", ctx)
                        )
                    except TimeoutError:
                        yield ErrorEvent(
                            message=(
                                "Agent run limit exceeded: "
                                "maximum wall time reached"
                            )
                        )
                        break
                    if not queued_count:
                        ctx = self._build_hook_context("session_end")
                        try:
                            await self._await_run(
                                self.hook_engine.run_hooks("session_end", ctx)
                            )
                        except TimeoutError:
                            yield ErrorEvent(
                                message=(
                                    "Agent run limit exceeded: "
                                    "maximum wall time reached"
                                )
                            )
                            break
                    for he in self._drain_hook_events():
                        yield he
                if self.file_history is not None:
                    summary = response.text[:60] + "..." if len(response.text) > 60 else response.text
                    self.file_history.make_snapshot(len(conversation.history), summary)
                if queued_count:
                    yield TurnComplete(turn=iteration)
                    continue
                yield LoopComplete(total_turns=iteration)
                break

            tool_uses = [
                ToolUseBlock(
                    tool_use_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    arguments=tc.arguments,
                )
                for tc in response.tool_calls
            ]
            conversation.add_assistant_message(
                response.text, tool_uses, thinking_blocks=conv_thinking
            )
            # 在 assistant 回复加入历史后锚定实际用量：基线（input + cache + output）
            # 覆盖到当前位置，因此下一轮迭代顶部的 auto-compact 检查只需对
            # 接下来追加的 tool results 做字符估算。
            conversation.record_usage_anchor(
                response.input_tokens,
                response.output_tokens,
                response.cache_read,
                response.cache_creation,
            )

            # 只有在完整响应及 stop_reason 都已确认后才执行工具，避免模型输出被
            # max_tokens 截断或调用方取消时仍留下写操作。可并发工具按连续批次
            # 并行，写入/Bash/Agent 等非并发安全工具保持独占和接收顺序。
            execution_results: list[_ToolExecResult] = []
            for batch in partition_tool_calls(response.tool_calls, self.registry):
                requires_prompt = False
                if batch.concurrent and self.permission_checker:
                    for tc in batch.calls:
                        tool = self.registry.get(tc.tool_name)
                        if tool is not None:
                            decision = self.permission_checker.check(tool, tc.arguments)
                            if decision.effect == "ask":
                                requires_prompt = True
                                break

                if batch.concurrent and not requires_prompt:
                    execution_results.extend(
                        await self._execute_batch_parallel(batch.calls)
                    )
                    continue

                for tc in batch.calls:
                    if self._steering_event.is_set():
                        execution_results.append(
                            _ToolExecResult(
                                tool_id=tc.tool_id,
                                tool_name=tc.tool_name,
                                result=ToolResult(
                                    output=(
                                        "Tool execution interrupted by queued user "
                                        "message before it started"
                                    ),
                                    is_error=True,
                                ),
                                elapsed=0.0,
                                is_unknown=False,
                            )
                        )
                        continue
                    result: ToolResult | None = None
                    elapsed = 0.0
                    is_unknown = False
                    async for item in self._execute_tool(tc):
                        if isinstance(item, (PermissionRequest, AskUserEvent)):
                            yield item
                        else:
                            result, elapsed, is_unknown = item
                    if result is None:
                        result = ToolResult(
                            output="Error: no result from tool", is_error=True
                        )
                    execution_results.append(
                        _ToolExecResult(
                            tool_id=tc.tool_id,
                            tool_name=tc.tool_name,
                            result=result,
                            elapsed=elapsed,
                            is_unknown=is_unknown,
                        )
                    )

            tool_results: list[ToolResultBlock] = []
            calls_by_id = {call.tool_id: call for call in response.tool_calls}
            for br in execution_results:
                if br.is_unknown:
                    consecutive_unknown += 1
                else:
                    consecutive_unknown = 0
                original_call = calls_by_id.get(br.tool_id)
                original_arguments = (
                    original_call.arguments if original_call is not None else {}
                )
                effective_name = str(
                    br.result.metadata.get("effective_tool_name", br.tool_name)
                )
                effective_arguments = br.result.metadata.get(
                    "effective_arguments", original_arguments
                )
                if not isinstance(effective_arguments, dict):
                    effective_arguments = original_arguments
                effective_tool = self.registry.get(effective_name)
                category = (
                    effective_tool.category if effective_tool is not None else ""
                )
                if self.context_ledger is not None:
                    self.context_ledger.observe_tool(
                        effective_name,
                        effective_arguments,
                        br.result,
                        category=category,
                    )
                verification_changed = False
                if self.verification_gate is not None:
                    verification_changed = self.verification_gate.observe(
                        effective_name,
                        effective_arguments,
                        br.result,
                        category=category,
                    )
                    if self.context_ledger is not None:
                        self.context_ledger.set_verification(
                            self.verification_gate.snapshot()
                        )
                content = self._prepare_tool_result(br.tool_id, br.result)
                tool_results.append(
                    ToolResultBlock(
                        tool_use_id=br.tool_id,
                        content=content,
                        is_error=br.result.is_error,
                        content_blocks=br.result.content_blocks,
                    )
                )
                yield ToolResultEvent(
                    tool_id=br.tool_id,
                    tool_name=br.tool_name,
                    output=br.result.output,
                    is_error=br.result.is_error,
                    elapsed=br.elapsed,
                    artifact_path=br.result.artifact_path,
                    truncated=br.result.truncated,
                    total_bytes=br.result.total_bytes,
                    next_offset=br.result.next_offset,
                    content_blocks=br.result.content_blocks,
                    metadata=br.result.metadata,
                )
                if verification_changed and self.verification_gate is not None:
                    yield VerificationEvent(
                        status=self.verification_gate.status,
                        revision=self.verification_gate.revision,
                        evidence=[
                            item.to_dict()
                            for item in self.verification_gate.evidence
                        ],
                    )

            if consecutive_unknown >= 3:
                yield ErrorEvent(
                    message="Agent terminated: too many consecutive unknown tool calls"
                )
                break

            exit_plan_called = any(
                tc.tool_name == "ExitPlanMode" for tc in response.tool_calls
            )
            conversation.add_tool_results_message(tool_results)
            queued_count = self._inject_queued_user_messages(conversation)
            if queued_count:
                orchestration_event = self._refresh_adaptive_contracts(
                    conversation, new_task=False
                )
                if orchestration_event is not None:
                    yield orchestration_event

            # 非阻塞 memory recall：工具执行完后检查 prefetch 是否就绪
            await self._consume_memory_recall(conversation)

            if exit_plan_called:
                yield TurnComplete(turn=iteration)
                yield LoopComplete(total_turns=iteration)
                break

            if self.hook_engine:
                ctx = self._build_hook_context("turn_end")
                try:
                    await self._await_run(
                        self.hook_engine.run_hooks("turn_end", ctx)
                    )
                except TimeoutError:
                    yield ErrorEvent(
                        message="Agent run limit exceeded: maximum wall time reached"
                    )
                    break
                for he in self._drain_hook_events():
                    yield he
            yield TurnComplete(turn=iteration)


    def _consume_mailbox(self, conversation: ConversationManager) -> None:
        if not self.team_name or not self._team_manager:
            return
        try:
            mailbox = self._team_manager.get_mailbox(self.team_name)
            if mailbox is None:
                return
            messages = mailbox.consume(self.agent_id)
            for msg in messages:
                prefix = f"[Message from {msg.from_agent}]"
                if msg.message_type != "text":
                    prefix = f"[{msg.message_type} from {msg.from_agent}]"
                content = f"{prefix} {msg.content}"
                conversation.add_user_message(content)
        except Exception as e:
            log.debug("Mailbox consumption failed: %s", e)

    def _build_permission_description(self, tc: ToolCallComplete) -> str:
        """为 HITL 权限确认生成人类可读的操作描述。"""
        tool = self.registry.get(tc.tool_name)
        return (
            PermissionChecker.describe_tool_action(tool, tc.arguments)
            if tool is not None
            else tc.tool_name
        )

    async def _execute_single_tool_direct(
        self, tc: ToolCallComplete
    ) -> _ToolExecResult:
        result: ToolResult | None = None
        elapsed = 0.0
        is_unknown = False
        async for item in self._execute_tool(tc):
            if isinstance(item, PermissionRequest):
                # This path is used only for calls pre-classified as non-interactive.
                # If a rule changed between classification and execution, fail closed.
                if not item.future.done():
                    item.future.set_result(PermissionResponse.DENY)
            elif isinstance(item, AskUserEvent):
                if not item.future.done():
                    item.future.set_result({})
            else:
                result, elapsed, is_unknown = item
        if result is None:
            result = ToolResult(output="Error: no result from tool", is_error=True)
        return _ToolExecResult(
            tool_id=tc.tool_id,
            tool_name=tc.tool_name,
            result=result,
            elapsed=elapsed,
            is_unknown=is_unknown,
        )


    async def _execute_batch_parallel(
        self, calls: list[ToolCallComplete]
    ) -> list[_ToolExecResult]:
        # 并发只读工具经 RunContext 信号量限流，形成简单背压：并行读多个文件没问题，
        # 但不至于让一次超大批次把文件句柄 / 连接打满。无 RunContext 时退化为原行为。
        if self._run_context is not None:
            tasks = [
                self._run_context.run_bounded(self._execute_single_tool_direct(tc))
                for tc in calls
            ]
        else:
            tasks = [self._execute_single_tool_direct(tc) for tc in calls]
        return list(await asyncio.gather(*tasks))

    async def _execute_tool(
        self, tc: ToolCallComplete
    ) -> AsyncIterator[
        PermissionRequest | AskUserEvent | tuple[ToolResult, float, bool]
    ]:
        requested_tool_name = tc.tool_name
        requested_arguments = tc.arguments
        if tc.tool_name == "CallDeferredTool" and not tc.parse_error:
            dispatcher = self.registry.get(tc.tool_name)
            resolver = getattr(dispatcher, "resolve_target", None)
            resolved = resolver(tc.arguments) if callable(resolver) else None
            if resolved is None:
                result = ToolResult(
                    output=(
                        "Deferred tool dispatch failed. Use ToolSearch first, then "
                        "pass an exact discovered tool name and matching arguments."
                    ),
                    is_error=True,
                )
                yield result, 0.0, False
                return
            target_name, target_arguments = resolved
            tc = ToolCallComplete(
                tool_id=tc.tool_id,
                tool_name=target_name,
                arguments=target_arguments,
            )

        tool = self.registry.get(tc.tool_name)
        start = time.monotonic()
        is_unknown = False

        if tool is None:
            result = ToolResult(
                output=f"Error: unknown tool '{tc.tool_name}'", is_error=True
            )
            is_unknown = True
            elapsed = time.monotonic() - start
            yield result, elapsed, is_unknown
            return

        if not self.registry.is_enabled(tc.tool_name):
            result = ToolResult(
                output=f"Error: tool '{tc.tool_name}' is disabled in current mode",
                is_error=True,
            )
            elapsed = time.monotonic() - start
            yield result, elapsed, is_unknown
            return

        if self.orchestration is not None:
            authorized, reason = self.orchestration.authorize(
                tc.tool_name, tc.arguments
            )
            if not authorized:
                result = ToolResult(output=reason, is_error=True)
                yield result, time.monotonic() - start, is_unknown
                return

        skill_denied, skill_allowed = self._skill_tool_policy(
            tc.tool_name, tc.arguments, tool
        )
        if skill_denied:
            result = ToolResult(
                output=f"Skill policy denied tool '{tc.tool_name}'",
                is_error=True,
            )
            yield result, time.monotonic() - start, is_unknown
            return

        if tc.parse_error:
            result = ToolResult(
                output=f"Tool arguments are not valid JSON: {tc.parse_error}",
                is_error=True,
            )
            yield result, time.monotonic() - start, is_unknown
            return

        try:
            params = tool.params_model.model_validate(tc.arguments)
        except ValidationError as e:
            result = ToolResult(
                output=f"Parameter validation error: {e}", is_error=True
            )
            yield result, time.monotonic() - start, is_unknown
            return

        if tool.category == "write":
            new_instructions = self._load_path_instructions(tc)
            if new_instructions:
                result = ToolResult(
                    output=(
                        "No write was performed because additional instructions "
                        "became applicable for this path. Review them and retry "
                        "the operation if it still complies.\n\n"
                        "<system-reminder>\n"
                        + new_instructions
                        + "\n</system-reminder>"
                    ),
                    is_error=True,
                    metadata={
                        "instruction_sources": self.instruction_resolver.diagnostics()[
                            "loaded"
                        ]
                    },
                )
                yield result, time.monotonic() - start, is_unknown
                return

        # Every delivery surface and sub-agent goes through the same hook path.
        if self.hook_engine:
            file_path = self._infer_file_path(tc.arguments)
            hook_ctx = self._build_hook_context(
                "pre_tool_use",
                tool_name=tc.tool_name,
                tool_args=tc.arguments,
                file_path=file_path,
            )
            try:
                rejection = await self._await_run(
                    self.hook_engine.run_pre_tool_hooks(hook_ctx)
                )
            except TimeoutError:
                result = ToolResult(
                    output="Pre-tool hook exceeded run deadline",
                    is_error=True,
                )
                yield result, time.monotonic() - start, is_unknown
                return
            if rejection is not None:
                result = ToolResult(
                    output=f"Hook rejected: {rejection.reason}", is_error=True
                )
                yield result, time.monotonic() - start, is_unknown
                return

        # 权限检查
        if self.permission_checker and not skill_allowed:
            decision = self.permission_checker.check(tool, tc.arguments)

            if decision.effect == "deny":
                result = ToolResult(
                    output=f"Permission denied: {decision.reason}",
                    is_error=True,
                )
                elapsed = time.monotonic() - start
                yield result, elapsed, is_unknown
                return

            if decision.effect == "ask":
                loop = asyncio.get_running_loop()
                future: asyncio.Future[PermissionResponse] = loop.create_future()
                desc = self._build_permission_description(tc)
                # 向调用方 yield 权限请求事件，由调用方处理
                yield PermissionRequest(
                    tool_name=tc.tool_name,
                    description=desc,
                    future=future,
                )
                try:
                    response = await self._await_run(future)
                except TimeoutError:
                    if not future.done():
                        future.cancel()
                    result = ToolResult(
                        output="Permission request exceeded run deadline",
                        is_error=True,
                    )
                    yield result, time.monotonic() - start, is_unknown
                    return

                if response == PermissionResponse.DENY:
                    result = ToolResult(
                        output="Permission denied: 用户拒绝了此操作",
                        is_error=True,
                    )
                    elapsed = time.monotonic() - start
                    yield result, elapsed, is_unknown
                    return

                if response == PermissionResponse.ALLOW_ALWAYS:
                    from myclaude.permissions.rules import Rule
                    content = tool.permission_scope(tc.arguments).content
                    # 存储完整命令作为精确匹配规则，避免截断+通配符导致意外匹配
                    # 其他不相关的命令（S-5）
                    pattern = content
                    # 持久化规则写入本地文件
                    rule = Rule(
                        tool_name=tool.permission_rule_name(tc.arguments),
                        pattern=pattern,
                        effect="allow",
                        match="literal",
                    )
                    self.permission_checker.rule_engine.append_local_rule(rule)
                    # 同时加入会话级放行集合，本轮立即生效无需磁盘读取
                    self.permission_checker.add_session_allow(
                        tool.permission_rule_name(tc.arguments), content
                    )

        try:
            execution = asyncio.create_task(tool.execute(params))
            if isinstance(tool, AskUserTool):
                # Let the tool publish its pending event before the Agent waits
                # for the answer. The event is a first-class delivery-surface
                # event, avoiding UI polling and the previous deadlock.
                await asyncio.sleep(0)
                if tool._pending_event is not None:
                    yield tool._pending_event
            # 工具执行也必须受总 deadline 约束——一个没有自身超时的 Bash 命令或
            # MCP 调用（MCP 工具正是经由此路径执行）不能无限拖过运行时限。
            remaining = (
                self._run_context.remaining() if self._run_context else None
            )
            if tool.interrupt_behavior == "cancel":
                steering = asyncio.create_task(self._steering_event.wait())
                done, _pending = await asyncio.wait(
                    {execution, steering},
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=remaining,
                )
                if not done:
                    # 到达运行总时限：两个目标都未完成——取消工具执行，交由外层
                    # 循环下一轮的 limit 检查统一收敛为「wall time reached」。
                    execution.cancel()
                    try:
                        await execution
                    except asyncio.CancelledError:
                        pass
                    result = ToolResult(
                        output="Tool execution exceeded run deadline",
                        is_error=True,
                    )
                elif steering in done and execution not in done:
                    execution.cancel()
                    try:
                        await execution
                    except asyncio.CancelledError:
                        pass
                    result = ToolResult(
                        output="Tool execution interrupted by queued user message",
                        is_error=True,
                    )
                else:
                    result = await execution
                steering.cancel()
            elif remaining is None:
                result = await execution
            else:
                try:
                    async with asyncio.timeout(remaining):
                        result = await execution
                except TimeoutError:
                    execution.cancel()
                    try:
                        await execution
                    except asyncio.CancelledError:
                        pass
                    result = ToolResult(
                        output="Tool execution exceeded run deadline",
                        is_error=True,
                    )
        except Exception as e:
            result = ToolResult(
                output=f"Tool execution error: {e}", is_error=True
            )

        if requested_tool_name != tc.tool_name:
            result.metadata.setdefault("effective_tool_name", tc.tool_name)
            result.metadata.setdefault("effective_arguments", tc.arguments)
            result.metadata.setdefault("dispatcher_tool_name", requested_tool_name)
            result.metadata.setdefault("dispatcher_arguments", requested_arguments)
        self._snapshot_for_recovery(tc, result)

        if self.hook_engine:
            file_path = self._infer_file_path(tc.arguments)
            hook_ctx = self._build_hook_context(
                "post_tool_use",
                tool_name=tc.tool_name,
                tool_args=tc.arguments,
                file_path=file_path,
                message=result.output if not result.is_error else "",
                error=result.output if result.is_error else "",
            )
            try:
                await self._await_run(
                    self.hook_engine.run_hooks("post_tool_use", hook_ctx)
                )
            except TimeoutError:
                if not result.is_error:
                    result = ToolResult(
                        output=(
                            result.output
                            + "\nPost-tool hook exceeded run deadline"
                        ).strip(),
                        is_error=True,
                    )

        self._apply_path_instructions(tc, result)
        elapsed = time.monotonic() - start
        yield result, elapsed, is_unknown

    def _apply_path_instructions(
        self, tc: ToolCallComplete, result: ToolResult
    ) -> None:
        if result.is_error:
            return
        instructions = self._load_path_instructions(tc)
        if not instructions:
            return
        result.output = (
            result.output.rstrip()
            + "\n\n<system-reminder>\n"
            + "Additional instructions became applicable for this path:\n\n"
            + instructions
            + "\n</system-reminder>"
        )
        result.metadata["instruction_sources"] = self.instruction_resolver.diagnostics()[
            "loaded"
        ]

    def _load_path_instructions(self, tc: ToolCallComplete) -> str:
        if self.instruction_resolver is None:
            return ""
        if tc.tool_name not in {"ReadFile", "EditFile", "WriteFile", "DeleteFile"}:
            return ""
        value = self._infer_file_path(tc.arguments)
        if not value:
            return ""
        tool = self.registry.get(tc.tool_name)
        path = tool.resolve_path(value) if tool is not None else Path(value)
        instructions = self.instruction_resolver.on_file_access(path)
        if not instructions:
            return ""
        if self.instructions_content:
            self.instructions_content += "\n\n---\n\n" + instructions
        else:
            self.instructions_content = instructions
        return instructions

    def _snapshot_for_recovery(
        self, tc: ToolCallComplete, result: ToolResult
    ) -> None:
        """Keep only a path pointer for post-compaction recovery."""
        if result.is_error or tc.tool_name != "ReadFile":
            return
        path = tc.arguments.get("file_path") if isinstance(tc.arguments, dict) else None
        if not path:
            return
        try:
            tool = self.registry.get(tc.tool_name)
            resolved = tool.resolve_path(path) if tool is not None else Path(path)
        except (OSError, RuntimeError, ValueError):
            return
        self.recovery_state.record_file_reference(str(resolved))

    async def _extract_memories(
        self, conversation: ConversationManager
    ) -> None:
        """触发记忆提取，采用 in-progress + pending 合并策略。

        当提取正在进行时，新的触发不会启动并发提取，而是标记 _pending_extraction。
        当前提取完成后检查该标志，如果有 pending 则立即执行一次尾随提取，
        防止多个触发器同时执行导致重复提取。
        """
        if not self.memory_manager:
            return

        # 合并策略：正在提取时暂存新请求，等当前提取完成后尾随执行
        if self._extracting:
            log.debug("[extractMemories] extraction in progress — stashing for trailing run")
            self._pending_extraction = conversation
            return

        self._extracting = True
        try:
            await self.memory_manager.extract(
                self.client, conversation, self.protocol
            )
        except Exception as e:
            log.debug("Memory extraction failed: %s", e)
        finally:
            self._extracting = False
            # 检查是否有尾随提取请求
            pending = self._pending_extraction
            if pending is not None:
                self._pending_extraction = None
                log.debug("[extractMemories] running trailing extraction for stashed context")
                await self._extract_memories(pending)

    def _should_extract_memories(self, conversation: ConversationManager) -> bool:
        if self.memory_manager is None:
            return False
        if self._loop_count % MEMORY_EXTRACTION_INTERVAL != 0:
            return False
        if self.memory_manager.state_token() != self._memory_state_at_run_start:
            return False

        user_text = "\n".join(
            message.content.casefold()
            for message in conversation.history[-20:]
            if message.role == "user" and message.source == "user" and message.content
        )
        candidate_terms = (
            "remember",
            "keep in mind",
            "from now on",
            "i prefer",
            "my preference",
            "actually,",
            "that's wrong",
            "do not do that",
            "记住",
            "以后",
            "我的偏好",
            "我更喜欢",
            "不对",
            "不要再",
        )
        explicit_signal = any(term in user_text for term in candidate_terms)
        recent_errors = sum(
            1
            for message in conversation.history[-12:]
            for result in message.tool_results
            if result.is_error
        )
        content_messages = sum(
            1 for message in conversation.history if message.content.strip()
        )
        return explicit_signal or recent_errors >= 2 or content_messages >= 30

    async def manual_compact(
        self, conversation: ConversationManager
    ) -> CompactNotification | ErrorEvent:
        # auto_compact 会用摘要替换 conversation.history，所有 tool-result 内容
        # （原始或已替换的）都将被丢弃。这里跳过 apply_tool_result_budget —
        # 它在主循环中的唯一目的是为 LLM 调用生成 api_conv，而本路径不需要
        # 发起看到替换结果的 LLM 调用（auto_compact 内部的摘要调用操作的是原始对话）。
        result = await auto_compact(
            conversation,
            self.client,
            self.context_window,
            self.session_dir,
            protocol=self.protocol,
            manual=True,
            breaker=self.compact_breaker,
            recovery=self.recovery_state,
            tool_schemas=self.registry.get_all_schemas(self.protocol),
            transcript_path=self._transcript_path,
            context_ledger=(
                self.context_ledger.render_for_prompt()
                if self.context_ledger is not None
                else ""
            ),
            active_skill_names=sorted(self.active_skills),
        )
        if isinstance(result, CompactEvent):
            env_context = build_environment_context(
            self.work_dir, self.active_skills, self._skill_catalog, self._agent_catalog
        )
            conversation.inject_environment(env_context)
            memory_content = self.memory_manager.load() if self.memory_manager else ""
            conversation.inject_long_term_memory(
                self.instructions_content, memory_content
            )
            if self.context_ledger is not None:
                self._ledger_injected_version = self.context_ledger.version
            return CompactNotification(
                before_tokens=result.before_tokens,
                message=f"上下文已压缩（压缩前 {result.before_tokens:,} tokens）",
                boundary=result.boundary,
            )
        return ErrorEvent(message=result or "压缩失败：对话历史为空或未达到压缩条件")

    async def run_to_completion(
        self, task: str, conversation: ConversationManager | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        permission_handler: Callable[
            [PermissionRequest],
            Awaitable[PermissionResponse | None] | PermissionResponse | None,
        ] | None = None,
    ) -> str:
        if conversation is None:
            conversation = ConversationManager()
        if task:
            conversation.add_user_message(task)
        current_text = ""
        last_text = ""
        async for event in self.run(conversation):
            if isinstance(event, StreamText):
                current_text += event.text
                if event_callback:
                    event_callback({"type": "stream_text", "text": event.text})
            elif isinstance(event, ToolUseEvent):
                if event_callback:
                    event_callback(
                        {
                            "type": "tool_use",
                            "toolName": event.tool_name,
                            "args": event.arguments,
                        }
                    )
            elif isinstance(event, ToolResultEvent):
                if event_callback:
                    event_callback(
                        {
                            "type": "tool_result",
                            "toolName": event.tool_name,
                            "output": event.output,
                            "isError": event.is_error,
                            "artifactPath": event.artifact_path,
                            "truncated": event.truncated,
                            "totalBytes": event.total_bytes,
                            "nextOffset": event.next_offset,
                            "contentBlocks": event.content_blocks,
                        }
                    )
            elif isinstance(event, UsageEvent):
                if event_callback:
                    event_callback(
                        {
                            "type": "usage",
                            "usage": {
                                "inputTokens": event.input_tokens,
                                "outputTokens": event.output_tokens,
                                "cacheRead": event.cache_read,
                                "cacheCreation": event.cache_creation,
                                "estimatedCostUsd": event.estimated_cost_usd,
                            },
                        }
                    )
            elif isinstance(event, PermissionRequest):
                if permission_handler is None:
                    if not event.future.done():
                        event.future.set_result(PermissionResponse.DENY)
                else:
                    response = permission_handler(event)
                    if inspect.isawaitable(response):
                        response = await response
                    if response is not None and not event.future.done():
                        event.future.set_result(response)
            elif isinstance(event, TurnComplete):
                if event_callback:
                    event_callback({"type": "turn_complete", "turn": event.turn})
                if current_text:
                    last_text = current_text
                current_text = ""
            elif isinstance(event, LoopComplete):
                if event_callback:
                    event_callback(
                        {"type": "loop_complete", "totalTurns": event.total_turns}
                    )
                if current_text:
                    last_text = current_text
            elif isinstance(event, ErrorEvent):
                raise RuntimeError(event.message)
        return last_text or current_text

    async def _execute_tool_noninteractive(
        self, tc: ToolCallComplete
    ) -> ToolResult:
        execution = await self._execute_single_tool_direct(tc)
        return execution.result

    def _prepare_tool_result(self, tool_use_id: str, result: ToolResult) -> str:
        from myclaude.context.manager import (
            make_persisted_preview,
            persist_tool_result,
        )

        text = result.output
        result.total_bytes = result.total_bytes or len(text.encode("utf-8"))
        if result.artifact_path:
            return text
        if len(text) > MAX_OUTPUT_CHARS:
            fp = persist_tool_result(tool_use_id, text, self.session_dir)
            result.artifact_path = str(fp)
            result.truncated = True
            return make_persisted_preview(text, fp)
        return text

    def _maybe_persist_or_truncate(self, tool_use_id: str, text: str) -> str:
        """Compatibility wrapper retained for callers outside the main loop."""
        return self._prepare_tool_result(tool_use_id, ToolResult(output=text))
