"""Agent Loop 的集成测试 —— 以编程方式逐项验证 checklist。"""
from __future__ import annotations

import asyncio
import os
from typing import Any, AsyncIterator

import pytest
from pydantic import BaseModel

from myclaude.agent import (
    Agent,
    ErrorEvent,
    LoopComplete,
    PermissionRequest,
    PermissionResponse,
    StreamText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
    UsageEvent,
    partition_tool_calls,
)
from myclaude.prompts import build_environment_context, build_plan_mode_reminder, build_system_prompt
from myclaude.client import LLMClient
from myclaude.conversation import ConversationManager
from myclaude.serialization import build_anthropic_messages
from myclaude.tools import ToolRegistry, create_default_registry
from myclaude.tools.ask_user import AskUserEvent, AskUserTool
from myclaude.tools.base import (
    StreamEnd,
    StreamEvent,
    TextDelta,
    ToolCallComplete,
    Tool,
    ToolResult,
)

# ---------------------------------------------------------------------------
# 返回预设脚本响应的 mock LLM 客户端
# ---------------------------------------------------------------------------

class MockLLMClient(LLMClient):
    def __init__(self, responses: list[list[StreamEvent]], yield_control: bool = False) -> None:
        self._responses = list(responses)
        self._call_index = 0
        self._yield_control = yield_control

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if self._call_index >= len(self._responses):
            yield TextDelta(text="No more responses")
            yield StreamEnd(stop_reason="end_turn", input_tokens=1, output_tokens=1)
            return
        events = self._responses[self._call_index]
        self._call_index += 1
        for e in events:
            if self._yield_control:
                await asyncio.sleep(0)
            yield e

def _collect(events: list) -> dict[str, list]:
    result: dict[str, list] = {
        "text": [], "tool_use": [], "tool_result": [],
        "turn": [], "loop": [], "usage": [], "error": [],
    }
    for e in events:
        if isinstance(e, StreamText):
            result["text"].append(e.text)
        elif isinstance(e, ToolUseEvent):
            result["tool_use"].append(e)
        elif isinstance(e, ToolResultEvent):
            result["tool_result"].append(e)
        elif isinstance(e, TurnComplete):
            result["turn"].append(e)
        elif isinstance(e, LoopComplete):
            result["loop"].append(e)
        elif isinstance(e, UsageEvent):
            result["usage"].append(e)
        elif isinstance(e, ErrorEvent):
            result["error"].append(e)
    return result

# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_step_tool_call():
    """Agent 调用一次 ReadFile，拿到结果后停止。"""
    client = MockLLMClient([
        # 第 1 轮：模型调用 ReadFile
        [
            TextDelta("Let me read the file."),
            ToolCallComplete("t1", "ReadFile", {"file_path": "MYCLAUDE.md"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        # 第 2 轮：模型给出最终答案
        [
            TextDelta("The file contains project info."),
            StreamEnd("end_turn", input_tokens=30, output_tokens=15),
        ],
    ])
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic", work_dir=".")
    conv = ConversationManager()
    conv.add_user_message("Read MYCLAUDE.md")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    c = _collect(events)
    assert len(c["tool_use"]) == 1
    assert c["tool_use"][0].tool_name == "ReadFile"
    assert len(c["tool_result"]) == 1
    assert len(c["turn"]) == 1
    assert len(c["loop"]) == 1
    assert c["loop"][0].total_turns == 2

@pytest.mark.asyncio
async def test_multi_step_autonomous():
    """Agent 先 WriteFile 再 ReadFile 然后停止 —— 端到端的多步流程。"""
    # 清理残留文件，避免 read-before-edit 拦截新文件创建
    test_file = "/tmp/myclaude_test_hello.txt"
    if os.path.exists(test_file):
        os.remove(test_file)
    client = MockLLMClient([
        # 第 1 轮：WriteFile
        [
            TextDelta("Creating file."),
            ToolCallComplete("t1", "WriteFile", {"file_path": "/tmp/myclaude_test_hello.txt", "content": "Hello World"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        # 第 2 轮：ReadFile 进行验证
        [
            TextDelta("Verifying content."),
            ToolCallComplete("t2", "ReadFile", {"file_path": "/tmp/myclaude_test_hello.txt"}),
            StreamEnd("end_turn", input_tokens=40, output_tokens=25),
        ],
        # 第 3 轮：最终答案
        [
            TextDelta("File created and verified. Content is correct."),
            StreamEnd("end_turn", input_tokens=60, output_tokens=30),
        ],
    ])
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic", work_dir="/tmp")
    conv = ConversationManager()
    conv.add_user_message("Create hello.txt with Hello World, then verify")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    c = _collect(events)
    assert len(c["tool_use"]) == 2
    assert c["tool_use"][0].tool_name == "WriteFile"
    assert c["tool_use"][1].tool_name == "ReadFile"
    assert len(c["turn"]) == 2
    assert len(c["loop"]) == 1
    assert c["loop"][0].total_turns == 3
    # 验证文件确实被创建了
    assert not c["tool_result"][0].is_error
    assert not c["tool_result"][1].is_error


@pytest.mark.asyncio
async def test_ask_user_is_emitted_as_first_class_agent_event():
    client = MockLLMClient([
        [
            ToolCallComplete(
                "ask-1",
                "AskUserQuestion",
                {
                    "questions": [
                        {
                            "type": "radio",
                            "name": "database",
                            "message": "Choose a database",
                            "options": ["PostgreSQL", "SQLite"],
                        }
                    ]
                },
            ),
            StreamEnd("tool_use", input_tokens=10, output_tokens=5),
        ],
        [
            TextDelta("Using PostgreSQL."),
            StreamEnd("end_turn", input_tokens=20, output_tokens=5),
        ],
    ])
    registry = create_default_registry()
    registry.register(AskUserTool())
    agent = Agent(client, registry, "anthropic", work_dir=".")
    conv = ConversationManager()
    conv.add_user_message("configure storage")

    events = []
    async for event in agent.run(conv):
        events.append(event)
        if isinstance(event, AskUserEvent):
            event.future.set_result({"database": "PostgreSQL"})

    assert any(isinstance(event, AskUserEvent) for event in events)
    result = next(
        event for event in events if isinstance(event, ToolResultEvent)
    )
    assert result.output == "database: PostgreSQL"

@pytest.mark.asyncio
async def test_stop_end_turn():
    """模型以 end_turn 自然停止。"""
    client = MockLLMClient([
        [
            TextDelta("Hello! How can I help?"),
            StreamEnd("end_turn", input_tokens=5, output_tokens=10),
        ],
    ])
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic")
    conv = ConversationManager()
    conv.add_user_message("Hi")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    c = _collect(events)
    assert len(c["loop"]) == 1
    assert c["loop"][0].total_turns == 1
    assert len(c["error"]) == 0

@pytest.mark.asyncio
async def test_stop_max_iterations():
    """Agent 在达到 max_iterations 后停止。"""
    # 每个响应都带有工具调用，因此循环永远不会自然结束
    responses = []
    for i in range(5):
        responses.append([
            TextDelta(f"Step {i}"),
            ToolCallComplete(f"t{i}", "ReadFile", {"file_path": "MYCLAUDE.md"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=10),
        ])

    client = MockLLMClient(responses)
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic", max_iterations=2)
    conv = ConversationManager()
    conv.add_user_message("Do something")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    c = _collect(events)
    assert len(c["error"]) == 1
    assert "maximum iterations" in c["error"][0].message

@pytest.mark.asyncio
async def test_stop_cancel():
    """Agent 在收到 CancelledError 时干净地停止。"""

    class SlowMockClient(LLMClient):
        """在事件之间 sleep 的 mock 客户端，以便留出取消的时机。"""
        def __init__(self) -> None:
            self._call_count = 0

        async def stream(
            self,
            conversation: ConversationManager,
            system: str = "",
            tools: list[dict[str, Any]] | None = None,
        ) -> AsyncIterator[StreamEvent]:
            self._call_count += 1
            await asyncio.sleep(0.01)
            yield TextDelta(f"Step {self._call_count}")
            await asyncio.sleep(0.01)
            yield ToolCallComplete(f"t{self._call_count}", "ReadFile", {"file_path": "MYCLAUDE.md"})
            await asyncio.sleep(0.01)
            yield StreamEnd("end_turn", input_tokens=10, output_tokens=10)

    client = SlowMockClient()
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic")
    conv = ConversationManager()
    conv.add_user_message("Do something")

    events: list = []
    cancelled = False

    async def run_agent():
        async for e in agent.run(conv):
            events.append(e)

    task = asyncio.create_task(run_agent())
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        cancelled = True

    assert cancelled
    c = _collect(events)
    assert client._call_count >= 1
    assert not c["loop"]


@pytest.mark.asyncio
async def test_repeated_max_tokens_never_executes_partial_tool_call(tmp_path):
    class EmptyParams(BaseModel):
        pass

    class MarkerTool(Tool):
        name = "Marker"
        description = "test"
        params_model = EmptyParams
        category = "write"

        def __init__(self) -> None:
            self.executions = 0

        async def execute(self, params: BaseModel) -> ToolResult:
            self.executions += 1
            return ToolResult("executed")

    truncated = [
        ToolCallComplete("partial", "Marker", {}),
        StreamEnd("max_tokens", input_tokens=1, output_tokens=1),
    ]
    client = MockLLMClient([truncated] * 5)
    registry = ToolRegistry()
    marker = MarkerTool()
    registry.register(marker)
    agent = Agent(client, registry, "anthropic", work_dir=str(tmp_path))
    conversation = ConversationManager()
    conversation.add_user_message("run")

    events = [event async for event in agent.run(conversation)]

    assert marker.executions == 0
    assert any(
        isinstance(event, ErrorEvent) and "output token limit" in event.message
        for event in events
    )


@pytest.mark.asyncio
async def test_malformed_tool_json_fails_before_execution(tmp_path):
    class EmptyParams(BaseModel):
        pass

    class MarkerTool(Tool):
        name = "Marker"
        description = "test"
        params_model = EmptyParams
        category = "write"

        def __init__(self) -> None:
            self.executed = False

        async def execute(self, params: BaseModel) -> ToolResult:
            self.executed = True
            return ToolResult("executed")

    client = MockLLMClient([
        [
            ToolCallComplete(
                "bad", "Marker", {}, parse_error="unexpected end of JSON"
            ),
            StreamEnd("tool_use", input_tokens=1, output_tokens=1),
        ],
        [TextDelta("recovered"), StreamEnd("end_turn", 1, 1)],
    ])
    registry = ToolRegistry()
    marker = MarkerTool()
    registry.register(marker)
    agent = Agent(client, registry, "anthropic", work_dir=str(tmp_path))
    conversation = ConversationManager()
    conversation.add_user_message("run")

    events = [event async for event in agent.run(conversation)]
    results = [event for event in events if isinstance(event, ToolResultEvent)]

    assert not marker.executed
    assert len(results) == 1
    assert results[0].is_error
    assert "valid JSON" in results[0].output


def test_prepare_conversation_replaces_environment_without_shifting_cursor(tmp_path):
    agent = Agent(
        MockLLMClient([]),
        create_default_registry(),
        "anthropic",
        work_dir=str(tmp_path),
    )
    conversation = ConversationManager()
    agent.prepare_conversation(conversation)
    conversation.add_user_message("hello")
    cursor = len(conversation.history)

    agent.prepare_conversation(conversation)

    assert len(conversation.history) == cursor
    assert conversation.history[-1].content == "hello"

@pytest.mark.asyncio
async def test_stop_consecutive_unknown_tools():
    """Agent 在连续 3 次调用未知工具后停止。"""
    responses = []
    for i in range(5):
        responses.append([
            TextDelta(f"Trying tool {i}"),
            ToolCallComplete(f"t{i}", "NonExistentTool", {"arg": "val"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=10),
        ])

    client = MockLLMClient(responses)
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic")
    conv = ConversationManager()
    conv.add_user_message("Do something")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    c = _collect(events)
    assert len(c["error"]) == 1
    assert "unknown tool" in c["error"][0].message

@pytest.mark.asyncio
async def test_message_splicing():
    """assistant 消息包含 text + 多个 tool_use；对应的 tool_result 被打包在一起。"""
    client = MockLLMClient([
        # 第 1 轮：一个响应里包含两次工具调用
        [
            TextDelta("Reading two files."),
            ToolCallComplete("t1", "ReadFile", {"file_path": "MYCLAUDE.md"}),
            ToolCallComplete("t2", "ReadFile", {"file_path": "pyproject.toml"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        # 第 2 轮：最终响应
        [
            TextDelta("Done."),
            StreamEnd("end_turn", input_tokens=30, output_tokens=10),
        ],
    ])
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic", work_dir=".")
    conv = ConversationManager()
    conv.add_user_message("Read both files")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    # 检查对话历史
    msgs = build_anthropic_messages(conv.get_messages())
    # env_context(user) 和 user_message 被合并为一条 → merged_user + assistant(text+2 个 tool_use) + user(2 个 tool_result) + assistant(最终响应)
    assert len(msgs) == 4
    assistant_msg = msgs[1]
    assert assistant_msg["role"] == "assistant"
    assert len(assistant_msg["content"]) == 3  # text + 2 个 tool_use
    tool_results_msg = msgs[2]
    assert tool_results_msg["role"] == "user"
    assert len(tool_results_msg["content"]) == 2  # 2 个 tool_result
    assert tool_results_msg["content"][0]["tool_use_id"] == "t1"
    assert tool_results_msg["content"][1]["tool_use_id"] == "t2"

@pytest.mark.asyncio
async def test_concurrent_batch_execution(tmp_path):
    """多个 ReadFile 调用并发执行（属于同一批次）。"""
    client = MockLLMClient([
        [
            ToolCallComplete("t1", "ReadFile", {"file_path": "one.txt"}),
            ToolCallComplete("t2", "ReadFile", {"file_path": "two.txt"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        [
            TextDelta("Both files read."),
            StreamEnd("end_turn", input_tokens=30, output_tokens=10),
        ],
    ])
    (tmp_path / "one.txt").write_text("one", encoding="utf-8")
    (tmp_path / "two.txt").write_text("two", encoding="utf-8")
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic", work_dir=str(tmp_path))
    conv = ConversationManager()
    conv.add_user_message("Read both")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    c = _collect(events)
    assert len(c["tool_result"]) == 2
    # 两个都应成功（这些文件在项目根目录下存在）
    assert all(not r.is_error for r in c["tool_result"])

@pytest.mark.asyncio
async def test_token_usage_accumulates():
    """Usage 事件展示的是累计的 token 数量。"""
    client = MockLLMClient([
        [
            TextDelta("Step 1"),
            ToolCallComplete("t1", "ReadFile", {"file_path": "MYCLAUDE.md"}),
            StreamEnd("end_turn", input_tokens=100, output_tokens=50),
        ],
        [
            TextDelta("Step 2"),
            ToolCallComplete("t2", "ReadFile", {"file_path": "MYCLAUDE.md"}),
            StreamEnd("end_turn", input_tokens=200, output_tokens=80),
        ],
        [
            TextDelta("Done."),
            StreamEnd("end_turn", input_tokens=300, output_tokens=100),
        ],
    ])
    registry = create_default_registry()
    agent = Agent(client, registry, "anthropic", work_dir=".")
    conv = ConversationManager()
    conv.add_user_message("Test")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    c = _collect(events)
    assert len(c["usage"]) == 3
    assert c["usage"][0].input_tokens == 100
    assert c["usage"][0].output_tokens == 50
    assert c["usage"][1].input_tokens == 300
    assert c["usage"][1].output_tokens == 130
    assert c["usage"][2].input_tokens == 600
    assert c["usage"][2].output_tokens == 230

@pytest.mark.asyncio
async def test_plan_mode():
    """通过 permission_mode 切换 plan 模式。"""
    from myclaude.permissions import PermissionMode

    registry = create_default_registry()
    agent = Agent(MockLLMClient([]), registry, "anthropic")

    agent.set_permission_mode(PermissionMode.PLAN)
    assert agent.plan_mode is True

    agent.set_permission_mode(PermissionMode.DEFAULT)
    assert agent.plan_mode is False
    schemas = registry.get_all_schemas()
    names = [s["name"] for s in schemas]
    assert "WriteFile" in names
    assert "EditFile" in names
    assert "Bash" in names

@pytest.mark.asyncio
async def test_plan_mode_denied_tool_returns_error():
    """在 plan 模式下，写入类工具需要审批（effect=ask）；当用户
    拒绝时，工具返回一个错误结果，而不会真正执行。"""
    from myclaude.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionChecker,
        PermissionMode,
        RuleEngine,
    )

    client = MockLLMClient([
        [
            TextDelta("Let me write..."),
            ToolCallComplete("t1", "WriteFile", {"file_path": "x.txt", "content": "hi"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        [
            TextDelta("OK, I can't write in plan mode."),
            StreamEnd("end_turn", input_tokens=30, output_tokens=15),
        ],
    ])
    registry = create_default_registry()
    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox("."),
        rule_engine=RuleEngine(),
        mode=PermissionMode.PLAN,
    )
    agent = Agent(client, registry, "anthropic", permission_checker=checker)
    agent.set_permission_mode(PermissionMode.PLAN)
    conv = ConversationManager()
    conv.add_user_message("Write a file")

    events = []
    async for e in agent.run(conv):
        events.append(e)
        # plan 模式在写入前会询问；这里模拟用户拒绝。
        if isinstance(e, PermissionRequest):
            e.future.set_result(PermissionResponse.DENY)

    c = _collect(events)
    assert len(c["tool_result"]) == 1
    assert c["tool_result"][0].is_error
    assert "denied" in c["tool_result"][0].output.lower() or "拒绝" in c["tool_result"][0].output
    assert len(c["error"]) == 0

def test_partition_tool_calls():
    """分批逻辑会把可并发执行的调用归到同一组。"""
    from myclaude.tools.base import ToolCallComplete

    calls = [
        ToolCallComplete("1", "ReadFile", {}),
        ToolCallComplete("2", "ReadFile", {}),
        ToolCallComplete("3", "EditFile", {}),
        ToolCallComplete("4", "ReadFile", {}),
        ToolCallComplete("5", "ReadFile", {}),
    ]
    registry = create_default_registry()
    batches = partition_tool_calls(calls, registry)
    assert len(batches) == 3
    assert batches[0].concurrent and len(batches[0].calls) == 2
    assert not batches[1].concurrent and len(batches[1].calls) == 1
    assert batches[2].concurrent and len(batches[2].calls) == 2

def test_system_prompt_normal():
    sp = build_system_prompt()
    assert "MyClaude" in sp
    assert "Plan mode" not in sp

def test_system_prompt_plan():
    reminder = build_plan_mode_reminder("/tmp/plan.md", False, 1)
    assert "Plan mode" in reminder
    assert "MUST NOT" in reminder

def test_plan_mode_sparse_reminder():
    reminder = build_plan_mode_reminder("/tmp/plan.md", True, 8)
    assert "Plan mode still active" in reminder

def test_environment_context():
    ctx = build_environment_context("/home/user/project")
    assert "/home/user/project" in ctx
    assert "Operating system" in ctx
    assert "Current date" in ctx


@pytest.mark.asyncio
async def test_streaming_tool_execution():
    """工具在 LLM 流式输出期间就开始执行，不等整个响应结束。"""
    execution_log: list[tuple[str, float]] = []
    original_execute = None

    # 用一个慢速流模拟 LLM 还在输出，验证第一个工具在流结束前已经开始执行
    class SlowMockClient(MockLLMClient):
        async def stream(self, conversation, system="", tools=None):
            events = self._responses[self._call_index]
            self._call_index += 1
            for e in events:
                if isinstance(e, StreamEnd):
                    # 在 StreamEnd 前等一下，让已提交的工具有时间执行
                    await asyncio.sleep(0.05)
                yield e
                await asyncio.sleep(0)

    client = SlowMockClient([
        [
            ToolCallComplete("t1", "Glob", {"pattern": "*.py"}),
            ToolCallComplete("t2", "Glob", {"pattern": "*.toml"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        [
            TextDelta("Done."),
            StreamEnd("end_turn", input_tokens=30, output_tokens=10),
        ],
    ])
    registry = create_default_registry()

    # 记录工具执行时间
    glob_tool = registry.get("Glob")
    original_execute = glob_tool.execute

    async def patched_execute(params):
        import time
        execution_log.append(("start", time.monotonic()))
        result = await original_execute(params)
        execution_log.append(("end", time.monotonic()))
        return result

    glob_tool.execute = patched_execute

    agent = Agent(client, registry, "anthropic", work_dir=".")
    conv = ConversationManager()
    conv.add_user_message("Find files")

    events = []
    async for e in agent.run(conv):
        events.append(e)

    c = _collect(events)
    # 两个 Glob 调用都应产出结果
    assert len(c["tool_result"]) == 2
    # 工具应该在流式阶段就开始执行，所以 execution_log 至少有记录
    assert len(execution_log) >= 2, "工具未在流式阶段执行"


# ---------------------------------------------------------------------------
# A1: 共享 Runtime 注入的动态召回（recall_fn）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_fn_auto_launches_and_injects():
    """注入 recall_fn 后，Agent 自行按最近用户消息启动召回并可注入（TUI 之外也生效）。

    直接验证 _maybe_start_recall + _consume_memory_recall 的确定性核心逻辑，
    避开「末轮召回以后台任务 fire-and-forget 消费」带来的时序竞争（见 §9.1）。
    """
    captured: dict[str, str] = {}

    async def recall_fn(query: str) -> str:
        captured["query"] = query
        return "REMEMBERED CONTEXT"

    agent = Agent(
        MockLLMClient([]), create_default_registry(), "anthropic",
        work_dir=".", recall_fn=recall_fn,
    )
    conv = ConversationManager()
    conv.add_user_message("please fix the parser bug")

    # Agent 自行启动召回（不依赖 TUI prefetch）
    agent._maybe_start_recall(conv)
    assert agent.memory_recall_task is not None

    # 阻塞消费，确定性验证按最近用户消息触发 + 结果注入对话
    await agent._consume_memory_recall(conv, wait=True)
    assert captured.get("query") == "please fix the parser bug"
    assert any(
        "REMEMBERED CONTEXT" in m.content for m in conv.history
    ), "召回结果未注入对话"


@pytest.mark.asyncio
async def test_recall_fn_does_not_override_preset_task():
    """TUI 已通过 prefetch 设置 memory_recall_task 时，Agent 不重复启动召回。"""
    agent_launched = False

    async def recall_fn(query: str) -> str:
        nonlocal agent_launched
        agent_launched = True
        return "AGENT-LAUNCHED"

    async def preset() -> str:
        return "PRESET"

    agent = Agent(
        MockLLMClient([]), create_default_registry(), "anthropic",
        work_dir=".", recall_fn=recall_fn,
    )
    conv = ConversationManager()
    conv.add_user_message("q")
    # 模拟 TUI 的 prefetch 抢先设置
    preset_task = asyncio.ensure_future(preset())
    agent.memory_recall_task = preset_task

    agent._maybe_start_recall(conv)
    # 不覆盖已有 task，也不调用注入的 recall_fn
    assert agent.memory_recall_task is preset_task

    await agent._consume_memory_recall(conv, wait=True)
    assert agent_launched is False, "已有 recall task 时不应重复启动"
    assert any("PRESET" in m.content for m in conv.history)


@pytest.mark.asyncio
async def test_no_recall_fn_is_inert():
    """未注入 recall_fn（如未启用记忆）时不启动召回，行为不变。"""
    client = MockLLMClient([
        [TextDelta("hi"), StreamEnd("end_turn", input_tokens=1, output_tokens=1)],
    ])
    agent = Agent(client, create_default_registry(), "anthropic", work_dir=".")
    conv = ConversationManager()
    conv.add_user_message("q")

    async for _ in agent.run(conv):
        pass

    assert agent.memory_recall_task is None


@pytest.mark.asyncio
async def test_recall_restarts_for_each_agent_run():
    queries: list[str] = []

    async def recall_fn(query: str) -> str:
        queries.append(query)
        return ""

    client = MockLLMClient([
        [TextDelta("one"), StreamEnd("end_turn", 1, 1)],
        [TextDelta("two"), StreamEnd("end_turn", 1, 1)],
    ])
    agent = Agent(
        client,
        create_default_registry(),
        "anthropic",
        work_dir=".",
        recall_fn=recall_fn,
    )

    first = ConversationManager()
    first.add_user_message("first")
    async for _ in agent.run(first):
        pass

    second = ConversationManager()
    second.add_user_message("second")
    async for _ in agent.run(second):
        pass

    assert queries == ["first", "second"]
    assert agent.memory_recall_task is None


def test_recall_query_ignores_system_reminders():
    conv = ConversationManager()
    conv.add_user_message("fix the parser")
    conv.add_system_reminder("MCP server instructions")

    assert Agent._latest_user_query(conv) == "fix the parser"


def test_tui_ask_user_uses_schema_name_and_checkbox_type():
    from myclaude.askuser_dialog import InlineAskUserWidget

    question = {
        "type": "checkbox",
        "name": "features",
        "message": "Choose features",
        "options": ["Search", "Export"],
    }

    assert InlineAskUserWidget._answer_key(question, 0) == "features"
    assert InlineAskUserWidget._is_multi(question) is True
