from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mewcode.agent import Agent
from mewcode.client import LLMClient, create_client
from mewcode.config import ProviderConfig, SandboxAppConfig, WorktreeConfig
from mewcode.hooks import HookEngine
from mewcode.memory import MemoryManager, load_instructions
from mewcode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from mewcode.tools import ToolRegistry, create_default_registry
from mewcode.tools.enter_worktree import EnterWorktreeTool
from mewcode.tools.exit_worktree import ExitWorktreeTool
from mewcode.worktree import WorktreeManager


@dataclass
class CoreRuntime:
    client: LLMClient
    registry: ToolRegistry
    permission_checker: PermissionChecker
    agent: Agent
    memory_manager: MemoryManager | None
    worktree_manager: WorktreeManager | None


def create_permission_checker(
    work_dir: str,
    mode: PermissionMode,
    sandbox_config: SandboxAppConfig | None = None,
    *,
    sandbox_active: bool = False,
) -> PermissionChecker:
    root = Path(work_dir).expanduser().resolve()
    home = Path.home()
    sandbox_config = sandbox_config or SandboxAppConfig()
    return PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(root)),
        rule_engine=RuleEngine(
            user_rules_path=home / ".mewcode" / "permissions.yaml",
            project_rules_path=root / ".mewcode" / "permissions.yaml",
            local_rules_path=root / ".mewcode" / "permissions.local.yaml",
        ),
        mode=mode,
        sandbox_enabled=sandbox_active and sandbox_config.auto_allow,
    )


def configure_os_sandbox(
    registry: ToolRegistry,
    work_dir: str,
    sandbox_config: SandboxAppConfig | None,
) -> bool:
    if sandbox_config is None or not sandbox_config.enabled:
        return False
    from mewcode.sandbox import SandboxConfig, create_sandbox

    os_sandbox = create_sandbox()
    if os_sandbox is None or not os_sandbox.available():
        return False
    root = str(Path(work_dir).expanduser().resolve())
    bash_tool = registry.get("Bash")
    if bash_tool is None:
        return False
    bash_tool.sandbox = os_sandbox
    bash_tool.sandbox_config = SandboxConfig(
        allow_write=[root, "/tmp"],
        deny_write=[
            f"{root}/.mewcode/config.yaml",
            f"{root}/.mewcode/permissions.yaml",
            f"{root}/.mewcode/permissions.local.yaml",
        ],
        network_enabled=sandbox_config.network_enabled,
    )
    return True


def build_core_runtime(
    provider: ProviderConfig,
    permission_mode: PermissionMode,
    *,
    work_dir: str,
    hook_engine: HookEngine | None = None,
    sandbox_config: SandboxAppConfig | None = None,
    worktree_config: WorktreeConfig | None = None,
    registry: ToolRegistry | None = None,
    client: LLMClient | None = None,
    memory_enabled: bool = True,
    worktree_enabled: bool = True,
) -> CoreRuntime:
    """Build the shared agent kernel used by TUI, headless and remote modes."""
    root = str(Path(work_dir).expanduser().resolve())
    registry = registry or create_default_registry()
    client = client or create_client(provider)
    sandbox_active = configure_os_sandbox(registry, root, sandbox_config)
    checker = create_permission_checker(
        root,
        permission_mode,
        sandbox_config,
        sandbox_active=sandbox_active,
    )
    memory_manager = MemoryManager(root) if memory_enabled else None
    agent = Agent(
        client=client,
        registry=registry,
        protocol=provider.protocol,
        work_dir=root,
        permission_checker=checker,
        context_window=provider.get_context_window(),
        instructions_content=load_instructions(root),
        memory_manager=memory_manager,
        hook_engine=hook_engine,
    )

    worktree_manager: WorktreeManager | None = None
    if worktree_enabled:
        config = worktree_config or WorktreeConfig()
        worktree_manager = WorktreeManager(
            repo_root=root,
            symlink_directories=config.symlink_directories,
        )
        restored = worktree_manager.restore_session()
        if restored is not None:
            agent.work_dir = restored.worktree_path
        def set_work_dir(path: str) -> None:
            agent.work_dir = path
        registry.register(
            EnterWorktreeTool(
                worktree_manager=worktree_manager,
                on_work_dir_change=set_work_dir,
            )
        )
        registry.register(
            ExitWorktreeTool(
                worktree_manager=worktree_manager,
                on_work_dir_change=set_work_dir,
            )
        )

    return CoreRuntime(
        client=client,
        registry=registry,
        permission_checker=checker,
        agent=agent,
        memory_manager=memory_manager,
        worktree_manager=worktree_manager,
    )
