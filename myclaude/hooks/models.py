# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Literal

from myclaude.hooks.conditions import ConditionGroup

_PLACEHOLDER_RE = re.compile(
    r"\$(?:EVENT|TOOL_NAME|FILE_PATH|MESSAGE|ERROR|TOOL_ARGS\.[A-Za-z0-9_.-]+)"
)


@dataclass
class Action:
    type: str
    command: str = ""
    message: str = ""
    url: str = ""
    method: str = "POST"
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    prompt: str = ""
    timeout: int = 30


@dataclass
class ActionResult:
    output: str = ""
    success: bool = True
    decision: Literal["none", "allow", "deny"] = "none"


@dataclass
class Hook:
    id: str
    event: str
    action: Action
    condition: ConditionGroup | None = None
    reject: bool = False
    once: bool = False
    async_exec: bool = False
    executed: bool = False


    def should_run(self) -> bool:
        if self.once and self.executed:
            return False
        return True


    def mark_executed(self) -> None:
        self.executed = True


@dataclass
class HookContext:
    event_name: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""
    message: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event_name,
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "file_path": self.file_path,
            "message": self.message,
            "error": self.error,
        }

    def get_field(self, name: str) -> str:
        if name == "tool":
            return self.tool_name
        if name == "event":
            return self.event_name
        if name.startswith("args."):
            key = name[5:]
            value = self.tool_args.get(key, "")
            return str(value) if value else ""
        return ""

    def _placeholder_value(self, placeholder: str) -> tuple[bool, str]:
        values = {
            "$EVENT": self.event_name,
            "$TOOL_NAME": self.tool_name,
            "$FILE_PATH": self.file_path,
            "$MESSAGE": self.message,
            "$ERROR": self.error,
        }
        if placeholder in values:
            return True, values[placeholder]
        if placeholder.startswith("$TOOL_ARGS."):
            key = placeholder[len("$TOOL_ARGS."):]
            if key in self.tool_args:
                return True, str(self.tool_args[key])
        return False, ""

    def expand(self, template: str) -> str:
        def replace(match: re.Match[str]) -> str:
            found, value = self._placeholder_value(match.group(0))
            return value if found else match.group(0)

        return _PLACEHOLDER_RE.sub(replace, template)

    def shell_safe_expand(self, template: str) -> str:
        """Single-pass expansion with shell quoting for every dynamic value.

        Replacement values are never scanned again. This prevents a value such
        as ``$TOOL_ARGS.command`` from introducing a second placeholder after
        it has already been quoted.
        """

        def replace(match: re.Match[str]) -> str:
            found, value = self._placeholder_value(match.group(0))
            if not found:
                return match.group(0)
            return shlex.quote(value) if value else ""

        return _PLACEHOLDER_RE.sub(replace, template)


class ToolRejectedError(Exception):
    def __init__(self, tool: str, reason: str, hook_id: str) -> None:
        self.tool = tool
        self.reason = reason
        self.hook_id = hook_id
        super().__init__(f"Tool '{tool}' rejected by hook '{hook_id}': {reason}")
