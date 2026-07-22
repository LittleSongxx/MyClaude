from __future__ import annotations

from myclaude.orchestration import OrchestrationController
from myclaude.tools import ToolRegistry
from myclaude.usage import RunLimits


class _NamedTool:
    def __init__(self, name: str) -> None:
        self.name = name


def _registry(*names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry._tools[name] = _NamedTool(name)  # type: ignore[assignment]
    return registry


def test_simple_task_stays_solo():
    controller = OrchestrationController()
    decision = controller.decide(
        "Fix the typo in README.md",
        _registry("Agent", "TeamCreate"),
        RunLimits(),
    )
    assert decision.mode == "solo"
    assert decision.max_agents == 0


def test_broad_task_gets_bounded_parallel_exploration():
    controller = OrchestrationController()
    decision = controller.decide(
        "全面分析整个项目架构，并对比多个模块后给出迁移方案",
        _registry("Agent", "TeamCreate"),
        RunLimits(),
    )
    assert decision.mode == "parallel_explore"
    assert decision.max_agents == 2


def test_explicit_team_request_enables_coordination():
    controller = OrchestrationController()
    decision = controller.decide(
        "Use a team of multiple agents to collaborate on this audit",
        _registry("Agent", "TeamCreate"),
        RunLimits(),
    )
    assert decision.mode == "coordinate"
    assert decision.max_agents == 3


def test_tight_budget_forces_solo_and_agent_guard():
    controller = OrchestrationController()
    decision = controller.decide(
        "Use a subagent for a comprehensive audit",
        _registry("Agent", "TeamCreate"),
        RunLimits(max_turns=3),
    )
    allowed, reason = controller.authorize("Agent", {"prompt": "audit"})

    assert decision.mode == "solo"
    assert not allowed
    assert "solo" in reason


def test_explicit_no_agents_overrides_positive_keywords():
    controller = OrchestrationController()
    decision = controller.decide(
        "全面审计这个架构，但不要使用子代理或团队",
        _registry("Agent", "TeamCreate"),
        RunLimits(),
    )
    allowed, reason = controller.authorize("Agent", {"prompt": "audit"})

    assert decision.mode == "solo"
    assert not allowed
    assert "solo" in reason


def test_agent_budget_is_enforced():
    controller = OrchestrationController()
    controller.decide(
        "Delegate a comprehensive architecture audit to a subagent",
        _registry("Agent"),
        RunLimits(),
    )
    first, _ = controller.authorize("Agent", {"prompt": "one"})
    second, _ = controller.authorize("Agent", {"prompt": "two"})
    third, reason = controller.authorize("Agent", {"prompt": "three"})

    assert first and second
    assert not third
    assert "limit reached" in reason
