from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from myclaude.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from myclaude.context.ledger import ContextLedger
    from myclaude.verification import VerificationGate


class ContextLedgerParams(BaseModel):
    constraints: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    resolve: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    verification_waiver: str = ""


class UpdateContextLedgerTool(Tool):
    name = "UpdateContextLedger"
    description = (
        "Record durable task constraints, decisions, acceptance criteria, unresolved "
        "items, and artifact paths. Use verification_waiver only when a relevant "
        "verification command is genuinely unavailable or unsafe, and explain why."
    )
    params_model = ContextLedgerParams
    category = "read"
    is_system_tool = True

    def __init__(
        self,
        ledger: ContextLedger,
        verification_gate: VerificationGate | None = None,
    ) -> None:
        self._ledger = ledger
        self._verification_gate = verification_gate

    async def execute(self, params: BaseModel) -> ToolResult:
        assert isinstance(params, ContextLedgerParams)
        if params.verification_waiver and self._verification_gate is not None:
            self._verification_gate.waive(params.verification_waiver)
        changed = self._ledger.update(
            constraints=params.constraints,
            decisions=params.decisions,
            acceptance_criteria=params.acceptance_criteria,
            unresolved=params.unresolved,
            resolve=params.resolve,
            artifacts=params.artifacts,
            verification_waiver=params.verification_waiver,
        )
        return ToolResult(
            output=(
                "Context Ledger updated."
                if changed
                else "Context Ledger already contains those entries."
            ),
            metadata={"ledger_version": self._ledger.version},
        )

