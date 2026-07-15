from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from myclaude.agent import (
    Agent,
    CompactNotification,
    ErrorEvent,
    LoopComplete,
    StreamText,
    ToolResultEvent,
    ToolUseEvent,
)
from myclaude.client import ContextOverflowError, LLMClient
from myclaude.config import MCPServerConfig, ProviderConfig
from myclaude.context import CompactBoundary
from myclaude.conversation import ConversationManager, Message
from myclaude.mcp import ConnectResult
from myclaude.memory.session import (
    SESSION_SCHEMA_VERSION,
    RecordType,
    SessionMeta,
    SessionRecord,
    parse_compact_boundary,
)
from myclaude.permissions import PermissionMode
from myclaude.runtime_assembler import MCPFeatures, RuntimeAssembler
from myclaude.tools import ToolRegistry
from myclaude.tools.base import (
    StreamEnd,
    StreamEvent,
    TextDelta,
    Tool,
    ToolCallComplete,
    ToolResult,
)
from myclaude.usage import RunLimits, UsageLedger


class SequenceClient(LLMClient):
    def __init__(self, responses: list[list[StreamEvent]]) -> None:
        super().__init__()
        self.responses = responses
        self.calls = 0

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        response = self.responses[self.calls]
        self.calls += 1
        for event in response:
            yield event


class NoParams(BaseModel):
    pass


class SideEffectTool(Tool):
    name = "SideEffect"
    description = "test side effect"
    params_model = NoParams
    category = "command"

    def __init__(self) -> None:
        self.executed = False

    async def execute(self, params: BaseModel) -> ToolResult:
        self.executed = True
        return ToolResult("executed")


class SlowCancelableTool(Tool):
    name = "SlowCancelable"
    description = "slow test tool"
    params_model = NoParams
    category = "command"
    interrupt_behavior = "cancel"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def execute(self, params: BaseModel) -> ToolResult:
        self.started.set()
        await asyncio.sleep(10)
        return ToolResult("finished")


@pytest.mark.asyncio
async def test_context_overflow_compacts_once_and_retries(tmp_path: Path) -> None:
    class OverflowClient(LLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def stream(self, conversation, system="", tools=None):
            self.calls += 1
            if self.calls == 1:
                raise ContextOverflowError("context too long")
            if self.calls == 2:
                yield TextDelta(
                    "<analysis>old work</analysis><summary>condensed work</summary>"
                )
                yield StreamEnd("end_turn", 100, 10)
                return
            yield TextDelta("recovered")
            yield StreamEnd("end_turn", 20, 2)

    conversation = ConversationManager()
    for index in range(10):
        conversation.history.append(
            Message(
                role="user" if index % 2 == 0 else "assistant",
                content=f"message-{index}:" + "x" * 2000,
            )
        )
    client = OverflowClient()
    agent = Agent(
        client,
        ToolRegistry(),
        "anthropic",
        work_dir=str(tmp_path),
        context_window=200_000,
    )

    events = [event async for event in agent.run(conversation)]

    assert client.calls == 3
    assert any(isinstance(event, CompactNotification) for event in events)
    assert any(isinstance(event, StreamText) and event.text == "recovered" for event in events)
    assert isinstance(events[-1], LoopComplete)


@pytest.mark.asyncio
async def test_token_limit_skips_pending_tool_side_effect(tmp_path: Path) -> None:
    client = SequenceClient(
        [
            [
                ToolCallComplete("call-1", "SideEffect", {}),
                StreamEnd("tool_use", input_tokens=10, output_tokens=2),
            ]
        ]
    )
    registry = ToolRegistry()
    tool = SideEffectTool()
    registry.register(tool)
    conversation = ConversationManager()
    conversation.add_user_message("run it")
    agent = Agent(
        client,
        registry,
        "anthropic",
        work_dir=str(tmp_path),
        run_limits=RunLimits(max_total_tokens=5),
    )

    events = [event async for event in agent.run(conversation)]

    assert tool.executed is False
    assert any(
        isinstance(event, ErrorEvent) and "token budget" in event.message
        for event in events
    )


@pytest.mark.asyncio
async def test_wall_time_limit_interrupts_model_wait(tmp_path: Path) -> None:
    class SlowClient(LLMClient):
        async def stream(self, conversation, system="", tools=None):
            await asyncio.sleep(1)
            yield StreamEnd("end_turn", 1, 1)

    conversation = ConversationManager()
    conversation.add_user_message("wait")
    agent = Agent(
        SlowClient(),
        ToolRegistry(),
        "anthropic",
        work_dir=str(tmp_path),
        run_limits=RunLimits(max_wall_time_seconds=0.01),
    )

    events = [event async for event in agent.run(conversation)]

    assert any(
        isinstance(event, ErrorEvent) and "wall time" in event.message
        for event in events
    )


@pytest.mark.asyncio
async def test_message_queued_during_stream_becomes_next_turn(tmp_path: Path) -> None:
    class SteeringClient(LLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.saw_steering = False

        async def stream(self, conversation, system="", tools=None):
            self.calls += 1
            if self.calls == 1:
                yield TextDelta("first")
                yield StreamEnd("end_turn", 1, 1)
                return
            self.saw_steering = any(
                message.role == "user" and message.content == "steer now"
                for message in conversation.history
            )
            yield TextDelta("second")
            yield StreamEnd("end_turn", 1, 1)

    client = SteeringClient()
    conversation = ConversationManager()
    conversation.add_user_message("start")
    agent = Agent(client, ToolRegistry(), "anthropic", work_dir=str(tmp_path))
    events = []
    async for event in agent.run(conversation):
        events.append(event)
        if isinstance(event, StreamText) and event.text == "first":
            agent.queue_user_message("steer now")

    assert client.calls == 2
    assert client.saw_steering
    assert any(isinstance(event, StreamText) and event.text == "second" for event in events)


@pytest.mark.asyncio
async def test_cancelable_tool_stops_for_queued_message(tmp_path: Path) -> None:
    client = SequenceClient(
        [
            [
                ToolCallComplete("slow-1", "SlowCancelable", {}),
                ToolCallComplete("side-1", "SideEffect", {}),
                StreamEnd("tool_use", 1, 1),
            ],
            [TextDelta("steered"), StreamEnd("end_turn", 1, 1)],
        ]
    )
    registry = ToolRegistry()
    slow_tool = SlowCancelableTool()
    side_effect = SideEffectTool()
    registry.register(slow_tool)
    registry.register(side_effect)
    conversation = ConversationManager()
    conversation.add_user_message("start")
    agent = Agent(client, registry, "anthropic", work_dir=str(tmp_path))
    async def collect_events():
        return [event async for event in agent.run(conversation)]

    run_task = asyncio.create_task(collect_events())
    await asyncio.wait_for(slow_tool.started.wait(), timeout=1)
    agent.queue_user_message("change direction")
    events = await asyncio.wait_for(run_task, timeout=1)

    interrupted = [
        event
        for event in events
        if isinstance(event, ToolResultEvent) and "interrupted" in event.output
    ]
    assert interrupted
    assert side_effect.executed is False
    assert client.calls == 2


def test_session_schema_migrates_unversioned_records() -> None:
    legacy = json.dumps(
        {
            "type": "user",
            "content": "hello",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    record = SessionRecord.from_jsonl(legacy)
    assert record is not None
    assert record.schema_version == SESSION_SCHEMA_VERSION
    assert json.loads(record.to_jsonl())["schema_version"] == SESSION_SCHEMA_VERSION

    future = json.loads(legacy)
    future["schema_version"] = SESSION_SCHEMA_VERSION + 1
    assert SessionRecord.from_jsonl(json.dumps(future)) is None
    invalid = json.loads(legacy)
    invalid["schema_version"] = True
    assert SessionRecord.from_jsonl(json.dumps(invalid)) is None


def test_session_meta_migrates_unversioned_file(tmp_path: Path) -> None:
    path = tmp_path / "legacy.meta"
    now = datetime.now(timezone.utc).isoformat()
    path.write_text(
        json.dumps({"id": "legacy", "created_at": now, "last_active": now}),
        encoding="utf-8",
    )
    meta = SessionMeta.load(path)
    assert meta is not None
    assert meta.schema_version == SESSION_SCHEMA_VERSION

    invalid = tmp_path / "invalid.meta"
    invalid.write_text(
        json.dumps(
            {
                "schema_version": -1,
                "id": "invalid",
                "created_at": now,
                "last_active": now,
            }
        ),
        encoding="utf-8",
    )
    assert SessionMeta.load(invalid) is None


def test_runtime_assembler_installs_same_standard_tool_surface(tmp_path: Path) -> None:
    provider = ProviderConfig(
        name="test",
        protocol="anthropic",
        base_url="https://example.invalid",
        model="claude-test",
    )
    client = SequenceClient([[TextDelta("ok"), StreamEnd("end_turn", 1, 1)]])
    assembler = RuntimeAssembler(
        provider,
        PermissionMode.DEFAULT,
        work_dir=str(tmp_path),
        client=client,
    )
    core = assembler.build_core()
    assembler.install_standard_features(core, interactive=False)

    for name in (
        "ToolSearch",
        "LoadSkill",
        "Agent",
        "TeamCreate",
        "TeamDelete",
        "SyntheticOutput",
    ):
        assert core.registry.get(name) is not None


def test_usage_ledger_accounts_secondary_purposes_and_cost() -> None:
    ledger = UsageLedger(
        input_cost_per_million=2.0,
        output_cost_per_million=10.0,
    )
    ledger.record(
        input_tokens=100,
        output_tokens=20,
        cache_read=50,
        purpose="compact",
    )
    snapshot = ledger.snapshot()
    assert snapshot.total_tokens == 170
    assert snapshot.by_purpose == {"compact": 1}
    assert snapshot.estimated_cost_usd == pytest.approx(0.0005)


@pytest.mark.asyncio
async def test_tui_memory_recall_reuses_main_usage_ledger(tmp_path: Path) -> None:
    from myclaude.app import MyClaudeApp

    class AccountingClient(LLMClient):
        async def stream(self, conversation, system="", tools=None):
            yield TextDelta("none")
            yield self._account(StreamEnd("end_turn", 7, 3))

    provider = ProviderConfig(
        name="test",
        protocol="anthropic",
        base_url="https://example.invalid",
        model="claude-test",
    )
    main_client = SequenceClient([])
    side_client = AccountingClient(usage_ledger=main_client.usage_ledger)
    app_like = SimpleNamespace(
        memory_manager=SimpleNamespace(
            user_mem_dir=str(tmp_path / "user-memory"),
            project_mem_dir=str(tmp_path / "project-memory"),
        ),
        _selected_provider=provider,
        client=main_client,
    )

    async def run_selector(**kwargs):
        await kwargs["selector"]("selector system", "selector input")
        return []

    with (
        patch("myclaude.app.create_client", return_value=side_client) as create,
        patch("myclaude.app.find_relevant_memories", side_effect=run_selector),
    ):
        result = await MyClaudeApp._prefetch_relevant_memories(
            app_like, "find this"
        )

    assert result == ""
    create.assert_called_once_with(
        provider, usage_ledger=main_client.usage_ledger
    )
    snapshot = main_client.usage_ledger.snapshot()
    assert snapshot.by_purpose == {"memory-recall": 1}
    assert (snapshot.input_tokens, snapshot.output_tokens) == (7, 3)


@pytest.mark.asyncio
async def test_memory_consolidation_inherits_limits_and_usage_scope(
    tmp_path: Path,
) -> None:
    from myclaude.memory.consolidation import MemoryConsolidator

    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        async def run(self, conversation):
            captured["purpose"] = client._usage_purpose.get()
            if False:
                yield None

    client = SequenceClient([])
    limits = RunLimits(max_total_tokens=123)
    consolidator = MemoryConsolidator(str(tmp_path))

    with patch("myclaude.agent.Agent", FakeAgent):
        await consolidator._do_consolidation(
            client,
            ConversationManager(),
            "anthropic",
            ["session-1"],
            run_limits=limits,
        )

    assert captured["kwargs"]["run_limits"] is limits
    assert captured["purpose"] == "memory-consolidation"


@pytest.mark.asyncio
async def test_headless_runtime_connects_mcp_and_injects_instructions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from myclaude.__main__ import _run_prompt

    provider = ProviderConfig(
        name="test",
        protocol="anthropic",
        base_url="https://example.invalid",
        model="claude-test",
    )
    mcp_config = MCPServerConfig(name="docs", command="fake-mcp")
    manager = SimpleNamespace(shutdown=AsyncMock())

    class FakeAgent:
        notification_fn = None

        async def run(self, conversation):
            self.conversation = conversation
            yield StreamText("done")
            yield LoopComplete(total_turns=1)

    fake_agent = FakeAgent()
    runtime = SimpleNamespace(
        client=MagicMock(),
        registry=ToolRegistry(),
        agent=fake_agent,
        worktree_manager=object(),
    )
    task_manager = SimpleNamespace(
        poll_completed=lambda: [],
        _async_tasks={},
        _tasks={},
    )
    team_manager = SimpleNamespace(
        _teams={},
        drain_lead_mailbox=lambda: [],
    )

    class FakeAssembler:
        instance = None

        def __init__(self, *args, **kwargs):
            type(self).instance = self
            self.connected = None

        def build_core(self):
            return runtime

        def install_standard_features(self, core, **kwargs):
            return SimpleNamespace(
                task_manager=task_manager,
                team_manager=team_manager,
            )

        async def connect_mcp(self, registry, configs):
            self.connected = (registry, configs)
            return MCPFeatures(
                manager=manager,
                result=ConnectResult(),
                instructions="Use the docs MCP server.",
            )

    config = SimpleNamespace(
        providers=[provider],
        sandbox=None,
        worktree=None,
        run_limits=RunLimits(),
        teammate_mode="",
        enable_fork=False,
        enable_verification_agent=False,
        enable_coordinator_mode=False,
        mcp_servers=[mcp_config],
    )

    with (
        patch(
            "myclaude.client.resolve_context_window",
            new=AsyncMock(return_value=provider.get_context_window()),
        ),
        patch("myclaude.runtime_assembler.RuntimeAssembler", FakeAssembler),
    ):
        await _run_prompt(
            config,
            PermissionMode.DEFAULT,
            None,
            "hello",
            workspace_trusted=True,
        )

    assembler = FakeAssembler.instance
    assert assembler is not None
    assert assembler.connected == (runtime.registry, [mcp_config])
    assert any(
        message.role == "user" and "Use the docs MCP server." in message.content
        for message in fake_agent.conversation.history
    )
    manager.shutdown.assert_awaited_once()
    assert capsys.readouterr().out == "done"


@pytest.mark.asyncio
async def test_remote_manual_compact_persists_boundary(tmp_path: Path) -> None:
    from myclaude.remote import RemoteServer

    provider = ProviderConfig(
        name="test",
        protocol="anthropic",
        base_url="https://example.invalid",
        model="claude-test",
    )
    keep = [Message(role="user", content="recent")]
    notification = CompactNotification(
        before_tokens=10_000,
        message="compacted",
        boundary=CompactBoundary(summary="older work", keep=keep),
    )
    server = RemoteServer([provider])
    server.agent = SimpleNamespace(
        manual_compact=AsyncMock(return_value=notification)
    )
    server.conversation = ConversationManager()
    server.session = MagicMock()

    await server._handle_compact()

    record = server.session.append_record.call_args.args[0]
    assert record.type is RecordType.COMPACT_BOUNDARY
    summary, restored_keep = parse_compact_boundary(record)
    assert summary == "older work"
    assert [(message.role, message.content) for message in restored_keep] == [
        ("user", "recent")
    ]


@pytest.mark.asyncio
async def test_remote_prompt_command_schedules_agent_after_dispatch() -> None:
    from myclaude.commands import Command, CommandType
    from myclaude.remote import RemoteServer

    provider = ProviderConfig(
        name="test",
        protocol="anthropic",
        base_url="https://example.invalid",
        model="claude-test",
    )
    server = RemoteServer([provider])

    async def prompt_handler(ctx):
        ctx.ui.send_user_message(f"expanded: {ctx.args}")

    server.command_registry.register_sync(
        Command(
            name="custom",
            description="test",
            type=CommandType.PROMPT,
            handler=prompt_handler,
        )
    )
    server._agent_task = asyncio.current_task()
    with patch.object(server, "_start_agent_task") as start:
        await server._handle_slash_command("/custom request")
        await asyncio.sleep(0)

    start.assert_called_once_with("expanded: request")
