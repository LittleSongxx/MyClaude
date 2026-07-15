from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from myclaude.client import LLMClient
from myclaude.config import ConfigError, ProviderConfig, _load_single_file, _merge_config
from myclaude.conversation import ConversationManager
from myclaude.memory.auto_memory import MemoryManager
from myclaude.memory.session import SessionMeta
from myclaude.permissions import PermissionMode
from myclaude.runtime import build_core_runtime
from myclaude.tools import create_default_registry
from myclaude.tools.base import StreamEnd, StreamEvent, TextDelta


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


def test_config_loads_provider_pricing_and_run_limits(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
providers:
  - name: priced
    protocol: anthropic
    base_url: https://example.invalid
    model: claude-test
    input_cost_per_million: 3.5
    output_cost_per_million: 15
run_limits:
  max_turns: 25
  max_wall_time_seconds: 90.5
  max_total_tokens: 500000
  max_cost_usd: 2.75
""",
        encoding="utf-8",
    )

    config = _load_single_file(config_path)

    assert config.providers[0].input_cost_per_million == 3.5
    assert config.providers[0].output_cost_per_million == 15.0
    assert config.run_limits.max_turns == 25
    assert config.run_limits.max_wall_time_seconds == 90.5
    assert config.run_limits.max_total_tokens == 500_000
    assert config.run_limits.max_cost_usd == 2.75


def test_config_layer_partially_merges_run_limits(tmp_path: Path) -> None:
    base_path = tmp_path / "base.yaml"
    base_path.write_text(
        """
providers:
  - name: test
    protocol: anthropic
    base_url: https://example.invalid
    model: claude-test
run_limits:
  max_turns: 12
  max_wall_time_seconds: 60
  max_cost_usd: 4
""",
        encoding="utf-8",
    )
    override_path = tmp_path / "override.yaml"
    override_path.write_text(
        """
run_limits:
  max_total_tokens: 123456
""",
        encoding="utf-8",
    )

    merged = _merge_config(
        _load_single_file(base_path),
        _load_single_file(override_path, require_providers=False),
    )

    assert merged.run_limits.max_turns == 12
    assert merged.run_limits.max_wall_time_seconds == 60.0
    assert merged.run_limits.max_total_tokens == 123_456
    assert merged.run_limits.max_cost_usd == 4.0


@pytest.mark.parametrize(
    ("section"),
    [
        "run_limits:\n  max_turns: -1",
        "run_limits:\n  max_total_tokens: 1.5",
        "run_limits:\n  max_wall_time_seconds: never",
        "run_limits:\n  max_cost_usd: true",
        "run_limits:\n  max_cost_usd: .inf",
        "run_limits:\n  max_tokens: 1000",
        "providers:\n  - name: test\n    protocol: anthropic\n"
        "    base_url: https://example.invalid\n    model: claude-test\n"
        "    input_cost_per_million: -0.1",
        "providers:\n  - name: test\n    protocol: anthropic\n"
        "    base_url: https://example.invalid\n    model: claude-test\n"
        "    output_cost_per_million: unknown",
        "providers:\n  - name: test\n    protocol: anthropic\n"
        "    base_url: https://example.invalid\n    model: claude-test\n"
        "    input_cost_per_million: .nan",
    ],
)
def test_config_rejects_invalid_limits_and_pricing(
    tmp_path: Path, section: str
) -> None:
    config_path = tmp_path / "invalid.yaml"
    if section.startswith("providers:"):
        contents = section
    else:
        contents = (
            "providers:\n"
            "  - name: test\n"
            "    protocol: anthropic\n"
            "    base_url: https://example.invalid\n"
            "    model: claude-test\n"
            f"{section}\n"
        )
    config_path.write_text(contents, encoding="utf-8")

    with pytest.raises(ConfigError):
        _load_single_file(config_path)


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


_MIXED_MEMORY_RESPONSE = """MEMORY_NAME: project-convention
MEMORY_TYPE: project
MEMORY_DESC: durable project rule
MEMORY_BODY: always use spaces
---
MEMORY_NAME: user-preference
MEMORY_TYPE: user
MEMORY_DESC: cross-project user pref
MEMORY_BODY: prefers concise output
---
"""


@pytest.mark.asyncio
async def test_long_term_memory_is_opt_in_by_default(tmp_path: Path) -> None:
    """B3：默认只写项目级记忆，跨项目用户级记忆需 opt-in。"""
    manager = MemoryManager(str(tmp_path / "project"))  # allow_long_term 默认 False
    manager._user_mem_dir = str(tmp_path / "user")
    conversation = ConversationManager()
    conversation.add_user_message("remember these")

    await manager.extract(DummyClient(_MIXED_MEMORY_RESPONSE), conversation, "anthropic")

    # 项目级记忆照常写入
    assert (manager.project_mem_dir / "project-convention.md").exists()
    # 用户级（跨项目）记忆被 opt-in 挡下，不写入
    assert not (Path(manager._user_mem_dir) / "user-preference.md").exists()


@pytest.mark.asyncio
async def test_long_term_memory_written_when_opted_in(tmp_path: Path) -> None:
    """B3：显式开启 allow_long_term 后，用户级记忆才写入。"""
    manager = MemoryManager(str(tmp_path / "project"), allow_long_term=True)
    manager._user_mem_dir = str(tmp_path / "user")
    conversation = ConversationManager()
    conversation.add_user_message("remember these")

    await manager.extract(DummyClient(_MIXED_MEMORY_RESPONSE), conversation, "anthropic")

    assert (manager.project_mem_dir / "project-convention.md").exists()
    assert (Path(manager._user_mem_dir) / "user-preference.md").exists()


def test_missing_session_metadata_returns_none(tmp_path: Path) -> None:
    assert SessionMeta.load(tmp_path / "missing.meta") is None


def test_version_does_not_create_project_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from myclaude.__main__ import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["myclaude", "--version"])
    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    assert "myclaude 0.3.0" in capsys.readouterr().out
    assert not (tmp_path / ".myclaude").exists()
