from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from myclaude.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from myclaude.tools import ToolRegistry


class CallDeferredToolParams(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class CallDeferredTool(Tool):
    name = "CallDeferredTool"
    description = (
        "Invoke a deferred tool after ToolSearch has discovered it. Provide the "
        "exact deferred tool name and an arguments object matching the schema "
        "returned by ToolSearch."
    )
    params_model = CallDeferredToolParams
    category = "command"
    is_system_tool = True

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def resolve_target(self, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        name = str(arguments.get("tool_name", "")).strip()
        target_arguments = arguments.get("arguments", {})
        if not name or not isinstance(target_arguments, dict):
            return None
        if not self._registry.is_discovered(name):
            return None
        tool = self._registry.get(name)
        if tool is None or not getattr(tool, "should_defer", False):
            return None
        if not self._registry.is_enabled(name):
            return None
        return name, target_arguments

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(
            output=(
                "CallDeferredTool is resolved by the agent runtime; the target "
                "tool was not invoked."
            ),
            is_error=True,
        )

