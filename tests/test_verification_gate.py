from __future__ import annotations

from typing import Any, AsyncIterator

import pytest
from pydantic import BaseModel

from myclaude.agent import Agent, VerificationEvent
from myclaude.client import LLMClient
from myclaude.conversation import ConversationManager
from myclaude.tools import ToolRegistry
from myclaude.tools.base import (
    StreamEnd,
    StreamEvent,
    TextDelta,
    Tool,
    ToolCallComplete,
    ToolResult,
)
from myclaude.verification import VerificationGate


class _Params(BaseModel):
    file_path: str = ""
    command: str = ""


class _WriteTool(Tool):
    name = "WriteFile"
    description = "test writer"
    params_model = _Params
    category = "write"

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(output="written")


class _BashTool(Tool):
    name = "Bash"
    description = "test command"
    params_model = _Params
    category = "command"

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(output="ok", metadata={"exit_code": 0})


class _Client(LLMClient):
    def __init__(self, responses: list[list[StreamEvent]]) -> None:
        self.responses = responses
        self.index = 0

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        events = self.responses[self.index]
        self.index += 1
        for event in events:
            yield event


def test_post_edit_diagnostics_are_current_revision_evidence():
    gate = VerificationGate()
    changed = gate.observe(
        "EditFile",
        {"file_path": "a.py"},
        ToolResult(
            output="edited",
            metadata={"diagnostics_engine": "python-syntax", "diagnostics": []},
        ),
        category="write",
    )

    assert changed
    assert gate.status == "passed"
    assert gate.assess_completion().blocked is False


def test_pending_gate_blocks_once_then_reports_unverified_completion():
    gate = VerificationGate()
    gate.observe(
        "DeleteFile",
        {"file_path": "a.py"},
        ToolResult(output="deleted"),
        category="write",
    )

    first = gate.assess_completion()
    second = gate.assess_completion()
    assert first.blocked
    assert not second.blocked
    assert "without verified evidence" in second.message


def test_verification_command_passes_latest_revision():
    gate = VerificationGate()
    gate.observe(
        "WriteFile",
        {"file_path": "a.py"},
        ToolResult(output="written"),
        category="write",
    )
    changed = gate.observe(
        "Bash",
        {"command": "python -m pytest -q"},
        ToolResult(output="passed", metadata={"exit_code": 0}),
        category="command",
    )

    assert changed
    assert gate.status == "passed"
    assert gate.evidence[-1].strength == "strong"


def test_new_task_resets_passed_state_but_preserves_unverified_changes():
    gate = VerificationGate()
    gate.observe(
        "WriteFile",
        {"file_path": "a.py"},
        ToolResult(
            output="written",
            metadata={"diagnostics_engine": "python-syntax", "diagnostics": []},
        ),
        category="write",
    )
    gate.start_task()
    assert gate.status == "not_required"
    assert gate.revision == 0

    gate.observe(
        "DeleteFile",
        {"file_path": "b.py"},
        ToolResult(output="deleted"),
        category="write",
    )
    gate.assess_completion()
    gate.start_task()
    assert gate.status == "pending"
    assert gate.revision == 1
    assert gate.completion_blocks == 0


def test_verification_state_can_be_restored_from_ledger_snapshot():
    original = VerificationGate()
    original.observe(
        "DeleteFile",
        {"file_path": "b.py"},
        ToolResult(output="deleted"),
        category="write",
    )
    original.assess_completion()

    restored = VerificationGate()
    restored.restore(original.snapshot())

    assert restored.status == "pending"
    assert restored.revision == 1
    assert restored.modified_paths == {"b.py"}
    assert restored.completion_blocks == 1


@pytest.mark.asyncio
async def test_agent_blocks_premature_completion_until_verification(tmp_path):
    client = _Client(
        [
            [
                ToolCallComplete(
                    "write", "WriteFile", {"file_path": "a.py"}
                ),
                StreamEnd("tool_use", input_tokens=10, output_tokens=5),
            ],
            [
                TextDelta("Premature completion."),
                StreamEnd("end_turn", input_tokens=20, output_tokens=5),
            ],
            [
                ToolCallComplete(
                    "verify", "Bash", {"command": "python -m pytest -q"}
                ),
                StreamEnd("tool_use", input_tokens=30, output_tokens=5),
            ],
            [
                TextDelta("Verified completion."),
                StreamEnd("end_turn", input_tokens=40, output_tokens=5),
            ],
        ]
    )
    registry = ToolRegistry()
    registry.register(_WriteTool())
    registry.register(_BashTool())
    agent = Agent(
        client,
        registry,
        "anthropic",
        work_dir=str(tmp_path),
        enable_runtime_contracts=True,
        persist_runtime_contracts=False,
    )
    conversation = ConversationManager()
    conversation.add_user_message("Change a.py")

    events = [event async for event in agent.run(conversation)]
    verification_events = [
        event for event in events if isinstance(event, VerificationEvent)
    ]

    assert any(event.blocked for event in verification_events)
    assert verification_events[-1].status == "passed"
    assert client.index == 4
