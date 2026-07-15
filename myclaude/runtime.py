from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from myclaude.agent import Agent
from myclaude.client import LLMClient, create_client
from myclaude.config import ProviderConfig, SandboxAppConfig, WorktreeConfig
from myclaude.hooks import HookEngine
from myclaude.memory import MemoryManager, load_instructions, make_recall_fn
from myclaude.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from myclaude.tools import ToolRegistry, create_default_registry
from myclaude.tools.enter_worktree import EnterWorktreeTool
from myclaude.tools.exit_worktree import ExitWorktreeTool
from myclaude.worktree import WorktreeManager
from myclaude.usage import RunLimits


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
    workspace_trusted: bool = True,
) -> PermissionChecker:
    root = Path(work_dir).expanduser().resolve()
    home = Path.home()
    sandbox_config = sandbox_config or SandboxAppConfig()
    return PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(root)),
        rule_engine=RuleEngine(
            user_rules_path=home / ".myclaude" / "permissions.yaml",
            project_rules_path=(
                root / ".myclaude" / "permissions.yaml"
                if workspace_trusted
                else None
            ),
            local_rules_path=(
                root / ".myclaude" / "permissions.local.yaml"
                if workspace_trusted
                else None
            ),
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
    from myclaude.sandbox import SandboxConfig, create_sandbox

    os_sandbox = create_sandbox()
    if os_sandbox is None or not os_sandbox.available():
        return False
    root = str(Path(work_dir).expanduser().resolve())
    bash_tool = registry.get("Bash")
    if bash_tool is None:
        return False
    bash_tool.sandbox = os_sandbox
    # 构建用户级全局配置路径列表（Path.home() 在无 HOME 的环境可能抛 RuntimeError）
    try:
        _home = Path.home()
        _user_deny = [
            str(_home / ".myclaude" / "config.yaml"),
            str(_home / ".myclaude" / "config.local.yaml"),
            str(_home / ".myclaude" / "permissions.yaml"),
            str(_home / ".myclaude" / "permissions.local.yaml"),
        ]
    except RuntimeError:
        _user_deny = []
    bash_tool.sandbox_config = SandboxConfig(
        allow_write=[root, "/tmp"],
        deny_write=[
            f"{root}/.myclaude/config.yaml",
            f"{root}/.myclaude/config.local.yaml",
            f"{root}/.myclaude/permissions.yaml",
            f"{root}/.myclaude/permissions.local.yaml",
            f"{root}/.myclaude/skills",
            # 用户级全局配置也必须保护：沙箱内 bash 命令不应能修改 API key 或全局权限（F-3）
            *_user_deny,
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
    workspace_trusted: bool = True,
    run_limits: RunLimits | None = None,
    allow_long_term_memory: bool = False,
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
        workspace_trusted=workspace_trusted,
    )
    memory_manager = (
        MemoryManager(root, allow_long_term=allow_long_term_memory)
        if memory_enabled and workspace_trusted
        else None
    )
    # 动态召回收敛到共享 Runtime：三入口（TUI / Headless / Remote）都经由
    # build_core_runtime，因此注入一次即可让召回语义一致。side client 复用主
    # client 的 usage 账本。TUI 仍可用自己的 prefetch 抢先设置 memory_recall_task，
    # Agent 检测到已设置就不重复启动（见 Agent._maybe_start_recall）。
    recall_fn = (
        make_recall_fn(provider, memory_manager, ledger_source=client)
        if memory_manager is not None
        else None
    )
    agent = Agent(
        client=client,
        registry=registry,
        protocol=provider.protocol,
        work_dir=root,
        permission_checker=checker,
        context_window=provider.get_context_window(),
        instructions_content=load_instructions(root, include_project=workspace_trusted),
        memory_manager=memory_manager,
        hook_engine=hook_engine,
        run_limits=run_limits,
        recall_fn=recall_fn,
    )

    worktree_manager: WorktreeManager | None = None
    if worktree_enabled:
        config = worktree_config or WorktreeConfig()
        worktree_manager = WorktreeManager(
            repo_root=root,
            symlink_directories=config.symlink_directories,
        )
        restored = worktree_manager.restore_session() if workspace_trusted else None
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
