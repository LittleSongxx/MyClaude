from __future__ import annotations

from enum import Enum
from typing import Literal

from myclaude.tools.base import ToolCategory


DecisionEffect = Literal["allow", "deny", "ask"]


class PermissionMode(str, Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    BYPASS = "bypassPermissions"


_MODE_MATRIX: dict[PermissionMode, dict[ToolCategory, DecisionEffect]] = {
    PermissionMode.DEFAULT: {"read": "allow", "write": "ask", "command": "ask"},
    PermissionMode.ACCEPT_EDITS: {"read": "allow", "write": "allow", "command": "ask"},
    PermissionMode.PLAN: {"read": "allow", "write": "deny", "command": "deny"},
    PermissionMode.BYPASS: {"read": "allow", "write": "allow", "command": "allow"},
}


def mode_decide(mode: PermissionMode, category: ToolCategory) -> DecisionEffect:
    return _MODE_MATRIX[mode][category]


def restrict_child_mode(
    parent: PermissionMode, requested: PermissionMode
) -> PermissionMode:
    """Prevent a child Agent from escalating beyond its parent's mode."""

    rank = {
        PermissionMode.PLAN: 0,
        PermissionMode.DEFAULT: 1,
        PermissionMode.ACCEPT_EDITS: 2,
        PermissionMode.BYPASS: 3,
    }
    return requested if rank[requested] <= rank[parent] else parent
