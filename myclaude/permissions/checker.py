from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from myclaude.permissions.dangerous import DangerousCommandDetector, is_safe_command
from myclaude.permissions.modes import DecisionEffect, PermissionMode, mode_decide
from myclaude.permissions.rules import RuleEngine
from myclaude.permissions.sandbox import PathSandbox
from myclaude.tools.base import Tool

_PLAN_MODE_ALLOWED_TOOLS = frozenset({"ToolSearch", "AskUserQuestion", "ExitPlanMode"})


@dataclass
class Decision:
    effect: DecisionEffect
    reason: str


class PermissionChecker:


    def __init__(
        self,
        detector: DangerousCommandDetector,
        sandbox: PathSandbox,
        rule_engine: RuleEngine,
        mode: PermissionMode = PermissionMode.DEFAULT,
        sandbox_enabled: bool = False,
    ) -> None:
        self.detector = detector
        self.sandbox = sandbox
        self.rule_engine = rule_engine
        self.mode = mode
        self.plan_file_path: str = ""
        # OS 级沙箱是否启用（开启后命令类工具可自动放行，因为内核会兜底）
        self.sandbox_enabled = sandbox_enabled
        # Layer 4b: 会话级 allow-always 集合（内存中，不持久化）
        # 存放格式为 "ToolName:pattern"，用户选择 "don't ask again" 时记录
        self._session_allowed: set[str] = set()


    def add_session_allow(self, tool_name: str, content: str) -> None:
        """将工具+内容模式加入会话级放行集合（Layer 4b）。

        比持久化规则引擎优先级更高，但不写入磁盘——会话结束即消失。
        """
        key = f"{tool_name}:{content}"
        self._session_allowed.add(key)

    def _check_session_allowed(self, tool_name: str, content: str) -> bool:
        """检查是否匹配会话级放行记录。"""
        if not self._session_allowed:
            return False
        key = f"{tool_name}:{content}"
        return key in self._session_allowed

    @staticmethod
    def describe_tool_action(tool: Tool, arguments: dict[str, Any]) -> str:
        """为 HITL 确认生成人类可读的操作描述。"""
        scope = tool.permission_scope(arguments)
        if scope.description:
            return scope.description
        if scope.content:
            return scope.content
        # 无法从标准字段提取时，拼接参数摘要
        parts = []
        for k, v in arguments.items():
            sv = str(v)
            if len(sv) > 80:
                sv = sv[:77] + "..."
            parts.append(f"{k}={sv}")
        return ", ".join(parts) if parts else tool.name


    def check(self, tool: Tool, arguments: dict[str, Any]) -> Decision:
        scope = tool.permission_scope(arguments)
        content = scope.content
        path = scope.path
        category = tool.permission_category(arguments)
        rule_name = tool.permission_rule_name(arguments)

        # Catastrophic commands are denied before every allow-list, rule and
        # permission mode.  Previously a command with a "safe" prefix skipped
        # both this detector and explicit deny rules.
        if category == "command":
            hit, reason = self.detector.detect(content)
            if hit:
                return Decision(effect="deny", reason=f"危险命令拦截: {reason}")

        rule_result = self.rule_engine.evaluate(rule_name, content)
        if rule_result == "deny":
            return Decision(effect="deny", reason="权限规则拒绝")
        if rule_result == "ask":
            return Decision(effect="ask", reason="权限规则要求确认")

        # Filesystem scope is independent from the text used by permission
        # rules.  Grep/Glob rules match their pattern, while sandboxing checks
        # the base path.
        if category == "write" and path and self.sandbox.is_write_denied(path):
            return Decision(effect="deny", reason="受保护的 MyClaude 配置不可写")

        if category in ("read", "write") and path:
            ok, reason = self.sandbox.check(
                path,
                write=category == "write",
            )
            if not ok and self.mode != PermissionMode.BYPASS:
                return Decision(effect="ask", reason=f"路径沙箱拦截: {reason}")

        if rule_result == "allow":
            return Decision(effect="allow", reason="权限规则放行")

        # Plan mode grants only coordination tools and the exact generated plan
        # file.  Spawning an arbitrary Agent is intentionally not auto-approved:
        # a project-defined child could otherwise select bypassPermissions.
        if self.mode == PermissionMode.PLAN:
            if tool.name in _PLAN_MODE_ALLOWED_TOOLS:
                return Decision(effect="allow", reason="Plan mode: allowed tool")
            if tool.name in ("WriteFile", "EditFile") and path:
                if self._is_plan_file(path):
                    return Decision(effect="allow", reason="Plan mode: plan file write")

        if category == "command" and is_safe_command(content or ""):
            return Decision(effect="allow", reason="Safe read-only command")

        # When the OS sandbox is explicitly configured for auto-allow, it is a
        # final command boundary after dangerous-command and user-rule checks.
        if self.sandbox_enabled and category == "command":
            return Decision(effect="allow", reason="OS 沙箱自动放行")

        # Layer 4b: 会话级放行（内存中，优先于模式兜底）
        if self._check_session_allowed(rule_name, content or ""):
            return Decision(effect="allow", reason="会话级放行（session allow-always）")

        # Layer 4: 权限模式兜底判定
        effect = mode_decide(self.mode, category)
        if effect == "allow":
            return Decision(effect="allow", reason=f"权限模式 {self.mode.value} 放行")
        if effect == "deny":
            return Decision(effect="deny", reason=f"权限模式 {self.mode.value} 拒绝")

        # Layer 5: 触发人工确认（HITL）
        return Decision(effect="ask", reason="需要用户确认")


    def _is_plan_file(self, target_path: str) -> bool:
        if not target_path:
            return False
        try:
            target = Path(target_path).expanduser()
            if not target.is_absolute():
                target = self.sandbox.project_root / target
            target = target.resolve()
            if self.plan_file_path:
                return target == Path(self.plan_file_path).expanduser().resolve()
            plans_dir = (self.sandbox.project_root / ".myclaude" / "plans").resolve()
            target.relative_to(plans_dir)
            return True
        except (OSError, ValueError):
            return False
