from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from mewcode.client import LLMClient
from mewcode.config import ProviderConfig, _load_single_file, _merge_config
from mewcode.conversation import ConversationManager
from mewcode.memory.auto_memory import MemoryManager
from mewcode.memory.session import SessionMeta
from mewcode.permissions import PermissionMode
from mewcode.runtime import build_core_runtime
from mewcode.tools import create_default_registry
from mewcode.tools.base import StreamEnd, StreamEvent, TextDelta


class DummyClient(LLMClient):
    def __init__(self, text: str = "done") -> None:
        self.text = text

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield TextDelta(self.text)
        yield StreamEnd("end_turn", 1, 1)


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="test",
        protocol="anthropic",
        base_url="https://example.invalid",
        model="claude-test",
    )


def test_runtime_enables_memory_and_worktree_by_default(tmp_path: Path) -> None:
    runtime = build_core_runtime(
        _provider(),
        PermissionMode.DEFAULT,
        work_dir=str(tmp_path),
        client=DummyClient(),
    )

    assert runtime.memory_manager is not None
    assert runtime.worktree_manager is not None
    assert runtime.registry.get("EnterWorktree") is not None
    assert runtime.registry.get("ExitWorktree") is not None
    assert runtime.agent.work_dir == str(tmp_path.resolve())


def test_config_layer_can_explicitly_disable_and_partially_override(
    tmp_path: Path,
) -> None:
    base_path = tmp_path / "base.yaml"
    base_path.write_text(
        """
providers:
  - name: test
    protocol: anthropic
    base_url: https://example.invalid
    model: claude-test
enable_fork: true
worktree:
  stale_cleanup_interval: 123
  stale_cutoff_hours: 24
""",
        encoding="utf-8",
    )
    override_path = tmp_path / "override.yaml"
    override_path.write_text(
        """
enable_fork: false
worktree:
  stale_cutoff_hours: 48
""",
        encoding="utf-8",
    )

    merged = _merge_config(
        _load_single_file(base_path),
        _load_single_file(override_path, require_providers=False),
    )

    assert len(merged.providers) == 1
    assert merged.enable_fork is False
    assert merged.worktree.stale_cleanup_interval == 123
    assert merged.worktree.stale_cutoff_hours == 48


@pytest.mark.asyncio
async def test_delete_requires_fresh_read_and_refuses_directories(
    tmp_path: Path,
) -> None:
    registry = create_default_registry()
    registry.set_work_dir(str(tmp_path))
    read = registry.get("ReadFile")
    delete = registry.get("DeleteFile")
    assert read is not None and delete is not None

    target = tmp_path / "target.txt"
    target.write_text("first", encoding="utf-8")

    result = await delete.execute(delete.params_model(file_path="target.txt"))
    assert result.is_error and target.exists()

    await read.execute(read.params_model(file_path="target.txt"))
    target.write_text("changed externally", encoding="utf-8")
    result = await delete.execute(delete.params_model(file_path="target.txt"))
    assert result.is_error and target.exists()

    await read.execute(read.params_model(file_path="target.txt"))
    result = await delete.execute(delete.params_model(file_path="target.txt"))
    assert not result.is_error and not target.exists()

    directory = tmp_path / "directory"
    directory.mkdir()
    result = await delete.execute(delete.params_model(file_path="directory"))
    assert result.is_error and directory.is_dir()


@pytest.mark.asyncio
async def test_memory_rejects_traversal_and_secrets_but_keeps_multiline(
    tmp_path: Path,
) -> None:
    response = """MEMORY_NAME: ../escape
MEMORY_TYPE: project
MEMORY_DESC: invalid path
MEMORY_BODY: should not escape
---
MEMORY_NAME: credentials
MEMORY_TYPE: project
MEMORY_DESC: leaked credential
MEMORY_BODY: api_key = sk-example-secret-value
---
MEMORY_NAME: project-conventions
MEMORY_TYPE: project
MEMORY_DESC: durable convention
MEMORY_BODY: first line
second line
---
"""
    manager = MemoryManager(str(tmp_path))
    manager._user_mem_dir = ""
    conversation = ConversationManager()
    conversation.add_user_message("Remember our durable project convention")

    await manager.extract(DummyClient(response), conversation, "anthropic")

    assert not (tmp_path / "escape.md").exists()
    assert not (manager.project_mem_dir / "credentials.md").exists()
    saved = manager.project_mem_dir / "project-conventions.md"
    assert saved.exists()
    content = saved.read_text(encoding="utf-8")
    assert "type: project" in content
    assert "first line\nsecond line" in content


def test_missing_session_metadata_returns_none(tmp_path: Path) -> None:
    assert SessionMeta.load(tmp_path / "missing.meta") is None


def test_version_does_not_create_project_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mewcode.__main__ import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mewcode", "--version"])
    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    assert "mewcode 0.3.0" in capsys.readouterr().out
    assert not (tmp_path / ".mewcode").exists()
