from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from myclaude.tools.base import PermissionScope, Tool, ToolCategory, ToolResult

if TYPE_CHECKING:
    from myclaude.agent import Agent
    from myclaude.skills.loader import SkillLoader


class LoadSkillParams(BaseModel):
    name: str = Field(description="The name of the skill to load")
    arguments: str = Field(
        default="", description="Optional arguments substituted into the skill body"
    )


class LoadSkill(Tool):
    name = "LoadSkill"
    description = (
        "Load and activate a skill by name. "
        "Returns the full SOP body so you can follow its instructions."
    )
    params_model = LoadSkillParams
    category = "read"
    is_concurrency_safe = False
    is_system_tool = True


    def __init__(self) -> None:
        self._loader: SkillLoader | None = None
        self._agent: Agent | None = None


    def set_loader(self, loader: SkillLoader) -> None:
        self._loader = loader

    def set_agent(self, agent: Agent) -> None:
        self._agent = agent

    def _dynamic_commands(self, arguments: dict[str, Any]) -> list[str]:
        if self._loader is None:
            return []
        skill = self._loader.get(str(arguments.get("name", "")))
        if skill is None:
            return []
        from myclaude.skills.parser import dynamic_context_commands

        return dynamic_context_commands(skill.prompt_body)

    def permission_category(self, arguments: dict[str, Any]) -> ToolCategory:
        return "command" if self._dynamic_commands(arguments) else "read"

    def permission_rule_name(self, arguments: dict[str, Any]) -> str:
        return "Bash" if self._dynamic_commands(arguments) else self.name

    def permission_scope(self, arguments: dict[str, Any]) -> PermissionScope:
        commands = self._dynamic_commands(arguments)
        if not commands:
            return super().permission_scope(arguments)
        content = " && ".join(commands)
        return PermissionScope(
            content=content,
            description=(
                f"Load skill '{arguments.get('name', '')}' and run dynamic context: "
                + content
            ),
        )


    async def execute(self, params: BaseModel) -> ToolResult:
        assert isinstance(params, LoadSkillParams)

        if self._loader is None or self._agent is None:
            return ToolResult(
                output="Error: LoadSkill not properly initialized",
                is_error=True,
            )

        skill = self._loader.get(params.name)
        if skill is None:
            available = ", ".join(n for n, _ in self._loader.get_catalog())
            return ToolResult(
                output=f"Error: unknown skill '{params.name}'. Available skills: {available}",
                is_error=True,
            )

        if skill.disable_model_invocation:
            return ToolResult(
                output=(
                    f"Error: skill '{skill.name}' is user-invocable only and cannot "
                    "be loaded by the model"
                ),
                is_error=True,
            )

        from myclaude.skills.parser import expand_dynamic_context, substitute_arguments

        prompt = substitute_arguments(skill.prompt_body, params.arguments)
        prompt = expand_dynamic_context(prompt, self._agent.work_dir)
        if skill.allowed_tools or skill.disallowed_tools:
            activated = self._agent.activate_skill(
                skill.name,
                prompt,
                allowed_tools=skill.allowed_tools,
                disallowed_tools=skill.disallowed_tools,
            )
        else:
            activated = self._agent.activate_skill(skill.name, prompt)

        header = f"# Skill: {skill.name}\n\n"
        if activated is False:
            return ToolResult(output=f"Skill '{skill.name}' is already active; reused it.")
        return ToolResult(output=header + prompt)
