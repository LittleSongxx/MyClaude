from __future__ import annotations

import asyncio
import json
import textwrap
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from myclaude.agent import Agent, PermissionRequest, PermissionResponse
from myclaude.agents.parser import AgentDef
from myclaude.agents.task_manager import TaskManager
from myclaude.agents.trace import TraceManager
from myclaude.conversation import ConversationManager
from myclaude.filehistory import FileHistory
from myclaude.memory.instructions import InstructionResolver
from myclaude.skills.loader import SkillLoader
from myclaude.skills.parser import (
    SkillDef,
    expand_dynamic_context,
    parse_skill_file,
    substitute_arguments,
)
from myclaude.skills.executor import SkillExecutor
from myclaude.tools import ToolRegistry
from myclaude.tools.agent_tool import AgentTool
from myclaude.tools.base import ToolResult, ToolCallComplete
from myclaude.tools.load_skill import LoadSkill
from myclaude.tools.write_file import WriteFile


def test_instruction_resolver_loads_global_and_path_rules_once(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "MYCLAUDE.md").write_text("root rule", encoding="utf-8")
    rules = tmp_path / ".myclaude" / "rules"
    rules.mkdir(parents=True)
    (rules / "global.md").write_text("always loaded", encoding="utf-8")
    (rules / "python.md").write_text(
        textwrap.dedent(
            """\
            ---
            paths:
              - src/**/*.py
            ---
            python-only rule
            """
        ),
        encoding="utf-8",
    )
    source = tmp_path / "src" / "pkg"
    source.mkdir(parents=True)
    (tmp_path / "src" / "AGENTS.md").write_text(
        "source subtree rule", encoding="utf-8"
    )
    target = source / "main.py"
    target.write_text("pass\n", encoding="utf-8")

    resolver = InstructionResolver(str(tmp_path))

    assert "root rule" in resolver.initial_content
    assert "always loaded" in resolver.initial_content
    assert "python-only rule" not in resolver.initial_content
    loaded = resolver.on_file_access(target)
    assert "source subtree rule" in loaded
    assert "python-only rule" in loaded
    assert resolver.on_file_access(target) == ""
    diagnostics = resolver.diagnostics()
    assert ".myclaude/rules/python.md" in diagnostics["loaded"]
    assert diagnostics["pending_path_rules"] == []


def test_agent_appends_lazy_instructions_to_tool_result(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    rules = tmp_path / ".myclaude" / "rules"
    rules.mkdir(parents=True)
    (rules / "python.md").write_text(
        "---\npaths: ['*.py']\n---\nUse Python guidance.", encoding="utf-8"
    )
    target = tmp_path / "main.py"
    target.write_text("pass\n", encoding="utf-8")
    registry = ToolRegistry()
    tool = MagicMock()
    tool.resolve_path.return_value = target
    registry._tools["ReadFile"] = tool
    agent = Agent(
        client=MagicMock(),
        registry=registry,
        protocol="anthropic",
        work_dir=str(tmp_path),
        instruction_resolver=InstructionResolver(str(tmp_path)),
    )
    result = ToolResult(output="1\tpass")

    agent._apply_path_instructions(
        ToolCallComplete("id", "ReadFile", {"file_path": "main.py"}), result
    )

    assert "Additional instructions became applicable" in result.output
    assert "Use Python guidance" in result.output


@pytest.mark.asyncio
async def test_first_write_is_preflighted_when_path_rules_become_applicable(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    rules = tmp_path / ".myclaude" / "rules"
    rules.mkdir(parents=True)
    (rules / "python.md").write_text(
        "---\npaths: ['*.py']\n---\nUse Python guidance.", encoding="utf-8"
    )
    registry = ToolRegistry()
    registry.register(WriteFile())
    agent = Agent(
        client=MagicMock(),
        registry=registry,
        protocol="anthropic",
        work_dir=str(tmp_path),
        instruction_resolver=InstructionResolver(str(tmp_path)),
    )
    call = ToolCallComplete(
        "write", "WriteFile", {"file_path": "new.py", "content": "pass\n"}
    )

    events = [event async for event in agent._execute_tool(call)]
    result = events[-1][0]

    assert result.is_error is True
    assert "No write was performed" in result.output
    assert "Use Python guidance" in result.output
    assert not (tmp_path / "new.py").exists()


def test_file_history_survives_restart_and_restores_existing_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tracked.txt"
    target.write_text("before", encoding="utf-8")
    history = FileHistory(str(tmp_path), "session")
    history.track_edit(str(target))
    target.write_text("after", encoding="utf-8")
    history.make_snapshot(1, "edit")

    restored = FileHistory(str(tmp_path), "session")
    changed = restored.rewind(0)

    assert str(target.resolve()) in changed
    assert target.read_text(encoding="utf-8") == "before"


def test_file_history_restart_deletes_new_file_on_rewind(tmp_path: Path) -> None:
    target = tmp_path / "new.txt"
    history = FileHistory(str(tmp_path), "session")
    history.track_edit(str(target))
    target.write_text("new", encoding="utf-8")
    history.make_snapshot(1, "create")

    restored = FileHistory(str(tmp_path), "session")
    restored.rewind(0)

    assert not target.exists()


def test_file_history_rewind_truncation_is_persisted(tmp_path: Path) -> None:
    target = tmp_path / "tracked.txt"
    target.write_text("zero", encoding="utf-8")
    history = FileHistory(str(tmp_path), "session")
    history.track_edit(str(target))
    target.write_text("one", encoding="utf-8")
    history.make_snapshot(1, "one")
    history.track_edit(str(target))
    target.write_text("two", encoding="utf-8")
    history.make_snapshot(2, "two")
    history.rewind(0)

    restored = FileHistory(str(tmp_path), "session")
    assert len(restored.get_snapshots()) == 1


@pytest.mark.asyncio
async def test_background_permission_is_forwarded_and_serialized() -> None:
    manager = TaskManager()
    seen: list[str] = []

    async def handler(request: PermissionRequest) -> None:
        seen.append(request.tool_name)
        request.future.set_result(PermissionResponse.ALLOW)

    manager.set_permission_handler(handler)
    future = asyncio.get_running_loop().create_future()
    request = PermissionRequest("Bash", "run command", future)

    response = await manager.handle_permission_request(request)

    assert response is PermissionResponse.ALLOW
    assert seen == ["Bash"]


@pytest.mark.asyncio
async def test_background_task_transcript_persists_and_can_resume(
    tmp_path: Path,
) -> None:
    agent = MagicMock()
    agent.agent_id = "agent-one"
    agent._agent_type = "general-purpose"
    agent.team_name = ""
    agent._team_manager = None
    agent.total_input_tokens = 12
    agent.total_output_tokens = 3
    agent.run_to_completion = AsyncMock(return_value="first result")
    manager = TaskManager(tmp_path)
    task_id = manager.launch(agent, "first")
    await manager.drain(timeout=1)

    loaded = TaskManager(tmp_path)
    task = loaded.get(task_id)
    assert task is not None
    assert task.status == "completed"
    assert Path(task.transcript_path).exists()
    assert stat.S_IMODE(Path(task.transcript_path).stat().st_mode) == 0o600
    assert "task_finished" in Path(task.transcript_path).read_text(encoding="utf-8")

    replacement = MagicMock()
    replacement.agent_id = "agent-two"
    replacement._agent_type = "general-purpose"
    replacement.team_name = ""
    replacement._team_manager = None
    replacement.total_input_tokens = 20
    replacement.total_output_tokens = 5
    replacement.run_to_completion = AsyncMock(return_value="second result")
    loaded.resume(task_id, "follow up", agent=replacement)
    await loaded.drain(timeout=1)

    assert loaded.get(task_id).result == "second result"


def test_trace_state_persists_and_running_nodes_become_detached(
    tmp_path: Path,
) -> None:
    manager = TraceManager(tmp_path)
    node = manager.create("Explore", parent_id="parent", trace_id="trace")
    manager.update(node.agent_id, input_tokens=10)

    loaded = TraceManager(tmp_path)
    restored = loaded.get(node.agent_id)

    assert restored is not None
    assert restored.status == "detached"
    assert restored.input_tokens == 10


def test_agent_tool_dynamic_concurrency_and_instruction_inheritance() -> None:
    loader = MagicMock()
    loader.get.side_effect = lambda name: AgentDef(
        agent_type=name,
        when_to_use="test",
        system_prompt=f"{name} prompt",
        background=name == "background",
    )
    parent = SimpleNamespace(
        instructions_content="project instructions", instruction_resolver=None
    )
    tool = AgentTool(
        loader,
        MagicMock(),
        MagicMock(),
        parent,
    )

    assert tool.is_call_concurrency_safe({"run_in_background": True})
    assert tool.is_call_concurrency_safe({"subagent_type": "Explore"})
    assert not tool.is_call_concurrency_safe({"subagent_type": "general-purpose"})
    assert tool._instructions_for(loader.get("Explore")) == "Explore prompt"
    assert "project instructions" in tool._instructions_for(
        loader.get("general-purpose")
    )


def test_agent_skills_standard_fields_catalog_and_arguments(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".myclaude" / "skills" / "release"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        textwrap.dedent(
            """\
            ---
            name: release
            description: Prepare a release
            allowed-tools: [ReadFile, "Bash(git *)"]
            disallowed-tools: [DeleteFile]
            disable-model-invocation: true
            user-invocable: true
            argument-hint: "<version>"
            context: fork
            ---
            Release $0 from $ARGUMENTS[1]. All: $ARGUMENTS
            """
        ),
        encoding="utf-8",
    )
    skill = parse_skill_file(skill_file)

    assert skill.mode == "fork"
    assert skill.context == "none"
    assert skill.allowed_tools == ["ReadFile", "Bash(git *)"]
    assert skill.disallowed_tools == ["DeleteFile"]
    assert skill.disable_model_invocation is True
    assert substitute_arguments(skill.prompt_body, "v1 stable") == (
        "Release v1 from stable. All: v1 stable\n"
    )

    loader = SkillLoader(str(tmp_path))
    loader.load_all()
    assert loader.get_catalog() == []
    assert loader.get_user_catalog() == [("release", "Prepare a release")]


def test_skill_dynamic_context_is_expanded(tmp_path: Path) -> None:
    expanded = expand_dynamic_context("Version:\n!`printf 1.2.3`", str(tmp_path))
    assert expanded == "Version:\n1.2.3"


def test_inline_skill_is_injected_once_into_current_conversation(
    tmp_path: Path,
) -> None:
    agent = Agent(
        client=MagicMock(),
        registry=ToolRegistry(),
        protocol="anthropic",
        work_dir=str(tmp_path),
    )
    conversation = ConversationManager()
    agent._current_conversation = conversation
    executor = SkillExecutor(agent, MagicMock(), "anthropic")
    skill = SkillDef("review", "Review", prompt_body="Review $ARGUMENTS")

    executor.execute_inline(skill, "src")
    executor.execute_inline(skill, "src")

    reminders = [
        message
        for message in conversation.history
        if message.source == "system_reminder"
    ]
    assert len(reminders) == 1
    assert "Review src" in reminders[0].content


def test_skill_dynamic_context_uses_command_permission_category() -> None:
    loader = MagicMock()
    loader.get.return_value = SimpleNamespace(prompt_body="!`deploy production`")
    tool = LoadSkill()
    tool.set_loader(loader)

    arguments = {"name": "release", "arguments": ""}
    assert tool.permission_category(arguments) == "command"
    assert tool.permission_rule_name(arguments) == "Bash"
    assert tool.permission_scope(arguments).content == "deploy production"


def test_plain_skill_remains_read_permission_category() -> None:
    loader = MagicMock()
    loader.get.return_value = SimpleNamespace(prompt_body="Review the code")
    tool = LoadSkill()
    tool.set_loader(loader)

    assert tool.permission_category({"name": "review"}) == "read"


def test_memory_extraction_is_signal_driven(tmp_path: Path) -> None:
    manager = MagicMock()
    manager.state_token.return_value = ()
    agent = Agent(
        client=MagicMock(),
        registry=ToolRegistry(),
        protocol="anthropic",
        work_dir=str(tmp_path),
        memory_manager=manager,
    )
    agent._memory_state_at_run_start = ()
    agent._loop_count = 5
    ordinary = ConversationManager()
    ordinary.add_user_message("fix this bug")
    assert not agent._should_extract_memories(ordinary)

    signaled = ConversationManager()
    signaled.add_user_message("Remember that I prefer focused tests")
    assert agent._should_extract_memories(signaled)

    manager.state_token.return_value = (("memory.md", 1, 10),)
    assert not agent._should_extract_memories(signaled)


@pytest.mark.asyncio
async def test_run_to_completion_awaits_permission_handler(tmp_path: Path) -> None:
    agent = Agent(
        client=MagicMock(),
        registry=ToolRegistry(),
        protocol="anthropic",
        work_dir=str(tmp_path),
    )
    future = asyncio.get_running_loop().create_future()

    async def events(_conversation: ConversationManager):
        yield PermissionRequest("Bash", "run", future)

    agent.run = events  # type: ignore[method-assign]

    async def allow(_request: PermissionRequest) -> PermissionResponse:
        return PermissionResponse.ALLOW

    await agent.run_to_completion("task", permission_handler=allow)
    assert future.result() is PermissionResponse.ALLOW


def test_task_manifest_is_valid_json(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path)
    manifest = tmp_path / ".myclaude" / "agents" / "tasks.json"
    assert not manifest.exists()
    manager._persist_manifest()
    assert json.loads(manifest.read_text(encoding="utf-8"))["version"] == 1
