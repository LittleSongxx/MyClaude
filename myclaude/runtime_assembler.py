from __future__ import annotations

from dataclasses import dataclass

from myclaude.agents.loader import AgentLoader
from myclaude.agents.task_manager import TaskManager
from myclaude.agents.trace import TraceManager
from myclaude.client import LLMClient
from myclaude.config import (
    MCPServerConfig,
    ProviderConfig,
    SandboxAppConfig,
    WorktreeConfig,
)
from myclaude.hooks import HookEngine
from myclaude.mcp import ConnectResult, MCPManager
from myclaude.permissions import PermissionMode
from myclaude.runtime import CoreRuntime, build_core_runtime
from myclaude.skills.loader import SkillLoader
from myclaude.teams.manager import TeamManager
from myclaude.tools import ToolRegistry
from myclaude.tools.agent_tool import AgentTool
from myclaude.tools.impl.tool_search import ToolSearchTool
from myclaude.tools.load_skill import LoadSkill
from myclaude.tools.synthetic_output import SyntheticOutputTool
from myclaude.tools.team_create import TeamCreateTool
from myclaude.tools.team_delete import TeamDeleteTool
from myclaude.usage import RunLimits


@dataclass
class StandardFeatures:
    core: CoreRuntime
    skill_loader: SkillLoader
    load_skill_tool: LoadSkill
    agent_loader: AgentLoader
    task_manager: TaskManager
    trace_manager: TraceManager
    team_manager: TeamManager


@dataclass
class MCPFeatures:
    manager: MCPManager | None
    result: ConnectResult
    instructions: str = ""


def _skill_catalog(loader: SkillLoader) -> str:
    catalog = loader.get_catalog()
    if not catalog:
        return ""
    lines = ["You can use the following Skills:", ""]
    lines.extend(f"- {name}: {description}" for name, description in catalog)
    lines.extend(
        [
            "",
            "If the user's request matches a Skill, call LoadSkill to activate it.",
        ]
    )
    return "\n".join(lines)


def _agent_catalog(loader: AgentLoader, enable_fork: bool) -> tuple[str, list[tuple[str, str]]]:
    catalog = loader.list_agents()
    if not catalog:
        return "", []
    lines = [
        "## Available Sub-Agent Types",
        "",
        "Use the Agent tool with subagent_type parameter to delegate tasks:",
        "",
    ]
    lines.extend(f"- **{name}**: {description}" for name, description in catalog)
    if enable_fork:
        lines.extend(
            [
                "",
                "Leave subagent_type empty to fork the current conversation "
                "(inherits full dialog history).",
            ]
        )
    lines.extend(
        [
            "",
            "Sub-agents run in the background. Report the returned task ID and "
            "continue when the system delivers its completion notification.",
        ]
    )
    return "\n".join(lines), catalog


def build_mcp_instructions(result: ConnectResult, registry: ToolRegistry) -> str:
    if not result.servers:
        return ""
    parts: list[str] = []
    for server in result.servers:
        section = f"## {server.name}\n"
        if server.instructions:
            section += server.instructions
        else:
            prefix = f"mcp__{server.name}__"
            names = [tool.name for tool in registry.list_tools() if tool.name.startswith(prefix)]
            if names:
                section += "Available tools: " + ", ".join(names)
        parts.append(section)
    return (
        "# MCP Server Instructions\n\n"
        "The following MCP servers have provided instructions for how to use "
        "their tools and resources:\n\n"
        + "\n\n".join(parts)
    )


class RuntimeAssembler:
    """Install the common runtime feature set for every delivery surface."""

    def __init__(
        self,
        provider: ProviderConfig,
        permission_mode: PermissionMode,
        *,
        work_dir: str,
        hook_engine: HookEngine | None = None,
        sandbox_config: SandboxAppConfig | None = None,
        worktree_config: WorktreeConfig | None = None,
        run_limits: RunLimits | None = None,
        workspace_trusted: bool = True,
        registry: ToolRegistry | None = None,
        client: LLMClient | None = None,
    ) -> None:
        self.provider = provider
        self.permission_mode = permission_mode
        self.work_dir = work_dir
        self.hook_engine = hook_engine
        self.sandbox_config = sandbox_config
        self.worktree_config = worktree_config
        self.run_limits = run_limits
        self.workspace_trusted = workspace_trusted
        self.registry = registry
        self.client = client

    def build_core(self) -> CoreRuntime:
        return build_core_runtime(
            self.provider,
            self.permission_mode,
            work_dir=self.work_dir,
            hook_engine=self.hook_engine,
            sandbox_config=self.sandbox_config,
            worktree_config=self.worktree_config,
            run_limits=self.run_limits,
            workspace_trusted=self.workspace_trusted,
            registry=self.registry,
            client=self.client,
        )

    def install_standard_features(
        self,
        core: CoreRuntime,
        *,
        interactive: bool,
        teammate_mode: str = "in-process",
        enable_fork: bool = False,
        enable_verification_agent: bool = False,
        enable_coordinator_mode: bool = False,
        task_manager: TaskManager | None = None,
        trace_manager: TraceManager | None = None,
    ) -> StandardFeatures:
        registry = core.registry
        registry.register(ToolSearchTool(registry, protocol=self.provider.protocol))

        skill_loader = SkillLoader(
            self.work_dir, include_project=self.workspace_trusted
        )
        skill_loader.load_all()
        load_skill_tool = LoadSkill()
        load_skill_tool.set_loader(skill_loader)
        load_skill_tool.set_agent(core.agent)
        registry.register(load_skill_tool)
        core.agent.set_skill_catalog(_skill_catalog(skill_loader))

        task_manager = task_manager or TaskManager()
        trace_manager = trace_manager or TraceManager()
        agent_loader = AgentLoader(
            self.work_dir,
            enable_verification=enable_verification_agent,
            include_project=self.workspace_trusted,
        )
        agent_loader.load_all()
        team_manager = TeamManager(
            worktree_manager=core.worktree_manager,
            trace_manager=trace_manager,
        )
        registry.register(
            AgentTool(
                agent_loader=agent_loader,
                task_manager=task_manager,
                trace_manager=trace_manager,
                parent_agent=core.agent,
                enable_fork=enable_fork,
                provider_config=self.provider,
                worktree_manager=core.worktree_manager,
                team_manager=team_manager,
            )
        )
        registry.register(
            TeamCreateTool(
                team_manager=team_manager,
                parent_agent=core.agent,
                teammate_mode=(
                    teammate_mode if interactive else teammate_mode or "in-process"
                ),
                is_interactive=interactive,
                enable_coordinator_mode=enable_coordinator_mode,
            )
        )
        registry.register(
            TeamDeleteTool(team_manager=team_manager, parent_agent=core.agent)
        )
        registry.register(SyntheticOutputTool())
        core.agent._team_manager = team_manager
        catalog_text, catalog = _agent_catalog(agent_loader, enable_fork)
        core.agent.set_agent_catalog(catalog_text, catalog_list=catalog)

        return StandardFeatures(
            core=core,
            skill_loader=skill_loader,
            load_skill_tool=load_skill_tool,
            agent_loader=agent_loader,
            task_manager=task_manager,
            trace_manager=trace_manager,
            team_manager=team_manager,
        )

    async def connect_mcp(
        self,
        registry: ToolRegistry,
        configs: list[MCPServerConfig],
    ) -> MCPFeatures:
        if not configs:
            return MCPFeatures(manager=None, result=ConnectResult())
        manager = MCPManager()
        manager.load_configs(configs)
        result = await manager.register_all_tools(registry)
        return MCPFeatures(
            manager=manager,
            result=result,
            instructions=build_mcp_instructions(result, registry),
        )
