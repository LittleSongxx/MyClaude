from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


_VERIFICATION_COMMAND = re.compile(
    r"(?:^|[;&|]\s*)("
    r"(?:python(?:\d+(?:\.\d+)?)?\s+-m\s+pytest|pytest(?:\s|$)|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:test|run\s+(?:test|build|lint|typecheck))|"
    r"(?:cargo|go)\s+(?:test|check)|"
    r"(?:ruff|mypy|pyright|tsc|make)\b|"
    r"(?:gradle|mvn|dotnet|swift)\s+(?:test|check|build)"
    r"))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class VerificationEvidence:
    revision: int
    kind: str
    success: bool
    detail: str
    strength: str = "normal"

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "kind": self.kind,
            "success": self.success,
            "detail": self.detail,
            "strength": self.strength,
        }


@dataclass(frozen=True)
class VerificationDecision:
    status: str
    blocked: bool
    message: str = ""
    evidence: tuple[VerificationEvidence, ...] = ()


@dataclass
class VerificationGate:
    """Require current-revision evidence before a modified task can finish."""

    revision: int = 0
    status: str = "not_required"
    modified_paths: set[str] = field(default_factory=set)
    evidence: list[VerificationEvidence] = field(default_factory=list)
    completion_blocks: int = 0
    max_pending_blocks: int = 1
    max_failed_blocks: int = 2

    def start_task(self) -> None:
        if self.revision > 0 and self.status in {"pending", "failed"}:
            self.completion_blocks = 0
            return
        self.revision = 0
        self.status = "not_required"
        self.modified_paths.clear()
        self.evidence.clear()
        self.completion_blocks = 0

    def restore(self, value: dict[str, Any]) -> None:
        self.status = str(value.get("status", "not_required"))
        self.revision = int(value.get("revision", 0))
        self.modified_paths = {
            str(path) for path in value.get("modified_paths", [])
        }
        self.completion_blocks = int(value.get("completion_blocks", 0))
        restored: list[VerificationEvidence] = []
        for item in value.get("evidence", []):
            if not isinstance(item, dict):
                continue
            restored.append(
                VerificationEvidence(
                    revision=int(item.get("revision", self.revision)),
                    kind=str(item.get("kind", "unknown")),
                    success=bool(item.get("success", False)),
                    detail=str(item.get("detail", "")),
                    strength=str(item.get("strength", "normal")),
                )
            )
        self.evidence = restored[-20:]

    def observe(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        *,
        category: str = "",
    ) -> bool:
        if getattr(result, "is_error", True):
            return self._observe_failed_verification(
                tool_name, arguments, result
            )
        metadata = getattr(result, "metadata", {}) or {}
        effective_name = str(metadata.get("effective_tool_name", tool_name))
        path = str(arguments.get("file_path", arguments.get("path", "")))
        changed = False
        if category == "write" or effective_name in {
            "WriteFile",
            "EditFile",
            "DeleteFile",
        }:
            self.revision += 1
            if path:
                self.modified_paths.add(path)
            self.status = "pending"
            self.completion_blocks = 0
            self.evidence = []
            changed = True

            diagnostics = metadata.get("diagnostics")
            engine = str(metadata.get("diagnostics_engine", ""))
            if isinstance(diagnostics, list) and engine not in {"", "disabled", "unavailable"}:
                if diagnostics:
                    self.status = "failed"
                    self.evidence.append(
                        VerificationEvidence(
                            self.revision,
                            "post_edit_diagnostics",
                            False,
                            "; ".join(str(item) for item in diagnostics[:3]),
                            "strong",
                        )
                    )
                else:
                    self.status = "passed"
                    self.evidence.append(
                        VerificationEvidence(
                            self.revision,
                            "post_edit_diagnostics",
                            True,
                            f"{engine}: no diagnostics",
                            "normal",
                        )
                    )
        elif effective_name == "ReadFile" and path in self.modified_paths:
            self.evidence.append(
                VerificationEvidence(
                    self.revision,
                    "readback",
                    True,
                    path,
                    "weak",
                )
            )
            changed = True
        elif effective_name == "Bash":
            command = str(arguments.get("command", ""))
            if self.is_verification_command(command):
                exit_code = metadata.get("exit_code")
                success = exit_code in (None, 0)
                self.evidence.append(
                    VerificationEvidence(
                        self.revision,
                        "verification_command",
                        success,
                        command,
                        "strong",
                    )
                )
                self.status = "passed" if success else "failed"
                self.completion_blocks = 0 if success else self.completion_blocks
                changed = True
        return changed

    def _observe_failed_verification(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> bool:
        if tool_name != "Bash":
            return False
        command = str(arguments.get("command", ""))
        if not self.is_verification_command(command):
            return False
        self.status = "failed"
        self.evidence.append(
            VerificationEvidence(
                self.revision,
                "verification_command",
                False,
                command + ": " + str(getattr(result, "output", ""))[:300],
                "strong",
            )
        )
        return True

    @staticmethod
    def is_verification_command(command: str) -> bool:
        return bool(_VERIFICATION_COMMAND.search(command.strip()))

    def waive(self, reason: str) -> bool:
        reason = reason.strip()
        if not reason:
            return False
        self.status = "waived"
        self.evidence.append(
            VerificationEvidence(self.revision, "waiver", True, reason, "explicit")
        )
        self.completion_blocks = 0
        return True

    def assess_completion(self) -> VerificationDecision:
        if self.status in {"not_required", "passed", "waived"}:
            return VerificationDecision(
                status=self.status,
                blocked=False,
                evidence=tuple(self.evidence),
            )
        limit = (
            self.max_failed_blocks
            if self.status == "failed"
            else self.max_pending_blocks
        )
        if self.completion_blocks < limit:
            self.completion_blocks += 1
            if self.status == "failed":
                message = (
                    "Verification failed for the latest changes. Run a focused "
                    "test, lint, typecheck, or build command and fix failures before "
                    "finishing."
                )
            else:
                message = (
                    "The task changed files but has no verification evidence yet. "
                    "Run the narrowest relevant test, lint, typecheck, or build "
                    "command. If verification is genuinely unavailable, record an "
                    "explicit waiver with UpdateContextLedger."
                )
            return VerificationDecision(
                status=self.status,
                blocked=True,
                message=message,
                evidence=tuple(self.evidence),
            )
        return VerificationDecision(
            status=self.status,
            blocked=False,
            message=(
                "Finishing without verified evidence after the verification retry "
                "budget was exhausted."
            ),
            evidence=tuple(self.evidence),
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "revision": self.revision,
            "modified_paths": sorted(self.modified_paths),
            "completion_blocks": self.completion_blocks,
            "evidence": [item.to_dict() for item in self.evidence[-20:]],
        }
