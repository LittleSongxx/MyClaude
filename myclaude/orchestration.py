from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from myclaude.usage import RunLimits


@dataclass(frozen=True)
class OrchestrationDecision:
    mode: str
    score: int
    max_agents: int
    reason: str
    explicit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "score": self.score,
            "max_agents": self.max_agents,
            "reason": self.reason,
            "explicit": self.explicit,
        }


class OrchestrationController:
    """Choose the smallest useful coordination topology for the current task."""

    def __init__(self) -> None:
        self.current: OrchestrationDecision | None = None
        self.agent_calls = 0

    def decide(
        self,
        query: str,
        registry: Any,
        run_limits: RunLimits,
        *,
        plan_mode: bool = False,
    ) -> OrchestrationDecision:
        text = query.casefold()
        has_agent = registry.get("Agent") is not None
        has_team = registry.get("TeamCreate") is not None
        forbid_delegation = any(
            marker in text
            for marker in (
                "do not use agents",
                "don't use agents",
                "do not delegate",
                "don't delegate",
                "without subagents",
                "without sub-agents",
                "single agent only",
                "no team",
                "不要使用代理",
                "不要使用子代理",
                "不要委托",
                "不要团队",
                "单代理",
                "只能自己",
            )
        )
        explicit_team = any(
            marker in text
            for marker in (
                "team",
                "swarm",
                "multiple agents",
                "parallel agents",
                "collaborate",
                "多智能体",
                "多个代理",
                "团队",
                "协作",
                "并行代理",
            )
        )
        explicit_delegate = any(
            marker in text
            for marker in (
                "subagent",
                "sub-agent",
                "delegate",
                "use an agent",
                "spawn an agent",
                "子代理",
                "子智能体",
                "委托",
                "代理来",
            )
        )
        broad_markers = (
            "architecture",
            "repository-wide",
            "across the",
            "migration",
            "audit",
            "comprehensive",
            "全面",
            "架构",
            "整个项目",
            "跨模块",
            "迁移",
            "审计",
            "广泛",
            "对比",
        )
        broad_hits = sum(marker in text for marker in broad_markers)
        broad = broad_hits > 0
        score = (3 if explicit_team else 0) + (2 if explicit_delegate else 0)
        score += min(broad_hits, 3)
        score += 1 if len(query) > 320 else 0
        budget_tight = (
            (0 < run_limits.max_turns <= 4)
            or (0 < run_limits.max_total_tokens <= 12_000)
        )
        if (
            forbid_delegation
            or not has_agent
            or budget_tight
            or (score == 0 and not plan_mode)
        ):
            decision = OrchestrationDecision(
                "solo",
                score,
                0,
                (
                    "the user explicitly prohibited delegation"
                    if forbid_delegation
                    else "local execution has the lowest coordination overhead"
                ),
                explicit_team or explicit_delegate or forbid_delegation,
            )
        elif explicit_team and has_team:
            decision = OrchestrationDecision(
                "coordinate",
                score,
                3,
                "the request explicitly asks for multi-agent coordination",
                True,
            )
        elif explicit_delegate or broad:
            decision = OrchestrationDecision(
                "parallel_explore" if score >= 3 else "delegate",
                score,
                2 if score >= 3 else 1,
                "parallel context gathering has a clear net benefit",
                explicit_delegate,
            )
        elif plan_mode:
            decision = OrchestrationDecision(
                "delegate",
                score,
                1,
                "one optional scout may reduce planning uncertainty",
                False,
            )
        else:
            decision = OrchestrationDecision(
                "solo",
                score,
                0,
                "the task does not justify delegation",
                False,
            )
        self.current = decision
        self.agent_calls = 0
        return decision

    def reminder(self) -> str:
        decision = self.current or OrchestrationDecision(
            "solo", 0, 0, "no decision recorded"
        )
        if decision.mode == "solo":
            return (
                "Adaptive orchestration: work locally. Delegate only if a new "
                "independent context or parallel investigation clearly pays for "
                "its coordination cost."
            )
        return (
            f"Adaptive orchestration: {decision.mode} "
            f"(up to {decision.max_agents} sub-agent(s)). "
            "Delegation is optional; keep the main task local and use agents only "
            "for independent, bounded work with a concrete return value."
        )

    def authorize(self, tool_name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        decision = self.current or OrchestrationDecision(
            "solo", 0, 0, "no decision recorded"
        )
        if tool_name == "TeamCreate":
            if decision.mode != "coordinate" and not decision.explicit:
                return False, "Adaptive orchestration selected local execution; teams are not needed."
            return True, ""
        if tool_name != "Agent":
            return True, ""
        if arguments.get("resume"):
            return True, ""
        if decision.max_agents <= 0:
            return False, (
                "Adaptive orchestration selected solo execution. "
                "Continue with the available local tools."
            )
        if self.agent_calls >= decision.max_agents:
            return False, (
                f"Adaptive orchestration limit reached ({decision.max_agents} sub-agent(s))."
            )
        if arguments.get("team_name") and decision.mode != "coordinate":
            return False, (
                "A teammate requires an explicit coordination decision for this task."
            )
        self.agent_calls += 1
        return True, ""
