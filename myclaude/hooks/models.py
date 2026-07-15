# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from myclaude.hooks.conditions import ConditionGroup


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

    def expand(self, template: str) -> str:
        result = template
        result = result.replace("$EVENT", self.event_name)
        result = result.replace("$TOOL_NAME", self.tool_name)
        result = result.replace("$FILE_PATH", self.file_path)
        result = result.replace("$MESSAGE", self.message)
        result = result.replace("$ERROR", self.error)
        for key, value in self.tool_args.items():
            result = result.replace(f"$TOOL_ARGS.{key}", str(value))
        return result

    def shell_safe_expand(self, template: str) -> str:
        """展开模板，对所有来自 LLM 工具参数的值做 shell 转义。

        与 expand() 不同，此方法对每个替换值调用 shlex.quote()，防止 LLM
        生成的工具参数中包含 shell 元字符（如 `; rm -rf /`）时注入到
        create_subprocess_shell 命令中。适用于 execute_command()。
        """
        import shlex
        result = template
        # 非工具参数字段：固定来自配置或内部状态，风险较低，保持原样替换
        result = result.replace("$EVENT", self.event_name)
        result = result.replace("$TOOL_NAME", self.tool_name)
        result = result.replace("$FILE_PATH", shlex.quote(self.file_path) if self.file_path else "")
        result = result.replace("$MESSAGE", shlex.quote(self.message) if self.message else "")
        result = result.replace("$ERROR", shlex.quote(self.error) if self.error else "")
        # 工具参数：直接来自 LLM 输出，必须 shell 转义
        for key, value in self.tool_args.items():
            str_val = str(value)
            result = result.replace(f"$TOOL_ARGS.{key}", shlex.quote(str_val) if str_val else "")
        return result


class ToolRejectedError(Exception):
    def __init__(self, tool: str, reason: str, hook_id: str) -> None:
        self.tool = tool
        self.reason = reason
        self.hook_id = hook_id
        super().__init__(f"Tool '{tool}' rejected by hook '{hook_id}': {reason}")
