from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from myclaude.tools.file_io import atomic_write_text


_CONSTRAINT_MARKERS = (
    "must",
    "only",
    "do not",
    "don't",
    "without",
    "required",
    "必须",
    "只能",
    "不要",
    "不能",
    "不得",
    "务必",
    "要求",
)


def _unique(values: Iterable[str], *, limit: int = 40) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


class ContextLedger:
    """Durable, pointer-oriented state for reconstructing a coding task."""

    def __init__(
        self,
        work_dir: str | Path,
        owner_id: str,
        *,
        persist: bool = True,
    ) -> None:
        self.work_dir = Path(work_dir).expanduser().resolve()
        self.owner_id = owner_id
        self.persist = persist
        self._lock = threading.RLock()
        self._path = self._build_path(owner_id)
        self.version = 0
        self.goal = ""
        self.constraints: list[str] = []
        self.decisions: list[str] = []
        self.acceptance_criteria: list[str] = []
        self.referenced_files: list[str] = []
        self.modified_files: dict[str, str] = {}
        self.artifacts: list[str] = []
        self.unresolved: list[str] = []
        self.verification: dict[str, Any] = {
            "status": "not_required",
            "revision": 0,
            "evidence": [],
        }
        self.orchestration: dict[str, Any] = {}
        self._updates: list[dict[str, Any]] = []
        self._load()

    def _build_path(self, owner_id: str) -> Path:
        safe_owner = "".join(
            char if char.isalnum() or char in "._-" else "_"
            for char in owner_id
        ) or "default"
        return self.work_dir / ".myclaude" / "ledgers" / f"{safe_owner}.json"

    def rebind(self, owner_id: str) -> None:
        if not owner_id or owner_id == self.owner_id:
            return
        with self._lock:
            self.owner_id = owner_id
            self._path = self._build_path(owner_id)
            self._reset()
            self._load()

    def _reset(self) -> None:
        self.version = 0
        self.goal = ""
        self.constraints = []
        self.decisions = []
        self.acceptance_criteria = []
        self.referenced_files = []
        self.modified_files = {}
        self.artifacts = []
        self.unresolved = []
        self.verification = {
            "status": "not_required",
            "revision": 0,
            "evidence": [],
        }
        self.orchestration = {}
        self._updates = []

    @property
    def path(self) -> Path:
        return self._path

    def start_task(self, query: str) -> bool:
        goal = query.strip()
        if not goal or goal == self.goal:
            return False
        constraints = self._extract_constraints(goal)
        with self._lock:
            self.goal = goal
            self.constraints = _unique(constraints)
            self.decisions = []
            self.acceptance_criteria = []
            self.modified_files = {}
            self.artifacts = []
            self.unresolved = []
            self.verification = {
                "status": "not_required",
                "revision": 0,
                "evidence": [],
            }
            self.orchestration = {}
            self._commit("new task: " + self._short(goal))
        return True

    def apply_steering(self, query: str) -> bool:
        steering = query.strip()
        if not steering:
            return False
        with self._lock:
            if steering in self.goal:
                return False
            self.goal = (
                self.goal + "\nSteering: " + steering
                if self.goal
                else steering
            )
            self.constraints = _unique(
                [*self.constraints, *self._extract_constraints(steering)]
            )
            self._commit("steering: " + self._short(steering))
        return True

    def update(
        self,
        *,
        constraints: Iterable[str] = (),
        decisions: Iterable[str] = (),
        acceptance_criteria: Iterable[str] = (),
        unresolved: Iterable[str] = (),
        resolve: Iterable[str] = (),
        artifacts: Iterable[str] = (),
        verification_waiver: str = "",
    ) -> bool:
        changed: list[str] = []
        with self._lock:
            for label, values, target in (
                ("constraints", constraints, self.constraints),
                ("decisions", decisions, self.decisions),
                ("acceptance criteria", acceptance_criteria, self.acceptance_criteria),
                ("unresolved", unresolved, self.unresolved),
                ("artifacts", artifacts, self.artifacts),
            ):
                incoming = _unique(values)
                for value in incoming:
                    if value not in target:
                        target.append(value)
                        changed.append(f"{label}: {self._short(value)}")
            for value in _unique(resolve):
                before = len(self.unresolved)
                self.unresolved = [
                    item for item in self.unresolved if item.casefold() != value.casefold()
                ]
                if len(self.unresolved) != before:
                    changed.append("resolved: " + self._short(value))
            if verification_waiver.strip():
                self.verification = {
                    **self.verification,
                    "status": "waived",
                    "waiver": verification_waiver.strip(),
                }
                changed.append("verification waived: " + self._short(verification_waiver))
            if not changed:
                return False
            self._commit("; ".join(changed))
        return True

    def record_reference(self, path: str) -> bool:
        normalized = self._normalize_path(path)
        if not normalized:
            return False
        with self._lock:
            if normalized in self.referenced_files:
                return False
            self.referenced_files.append(normalized)
            self._commit("referenced " + normalized)
        return True

    def record_modified(self, path: str, operation: str) -> bool:
        normalized = self._normalize_path(path)
        if not normalized:
            return False
        with self._lock:
            previous = self.modified_files.get(normalized)
            self.modified_files[normalized] = operation
            if previous == operation:
                return False
            self._commit(f"{operation} {normalized}")
        return True

    def record_artifact(self, path: str) -> bool:
        normalized = self._normalize_path(path)
        if not normalized:
            return False
        with self._lock:
            if normalized in self.artifacts:
                return False
            self.artifacts.append(normalized)
            self._commit("artifact " + normalized)
        return True

    def set_verification(self, value: dict[str, Any]) -> bool:
        normalized = json.loads(json.dumps(value, ensure_ascii=False, default=str))
        with self._lock:
            if normalized == self.verification:
                return False
            self.verification = normalized
            self._commit(
                "verification "
                + str(normalized.get("status", "unknown"))
                + f" at revision {normalized.get('revision', 0)}"
            )
        return True

    def set_orchestration(self, value: dict[str, Any]) -> bool:
        normalized = json.loads(json.dumps(value, ensure_ascii=False, default=str))
        with self._lock:
            if normalized == self.orchestration:
                return False
            self.orchestration = normalized
            self._commit(
                "orchestration " + str(normalized.get("mode", "solo"))
            )
        return True

    def observe_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        *,
        category: str = "",
    ) -> None:
        effective_name = str(
            getattr(result, "metadata", {}).get("effective_tool_name", tool_name)
        )
        path = str(
            arguments.get("file_path", arguments.get("path", ""))
        )
        if effective_name in {"ReadFile", "Glob", "Grep"} and path:
            self.record_reference(path)
        if category == "write" or effective_name in {
            "WriteFile",
            "EditFile",
            "DeleteFile",
        }:
            if not getattr(result, "is_error", True) and path:
                self.record_modified(path, effective_name)
        artifact_path = str(getattr(result, "artifact_path", "") or "")
        if artifact_path:
            self.record_artifact(artifact_path)

    def render_for_prompt(self, *, max_chars: int = 7000) -> str:
        with self._lock:
            lines = [
                "## Context Ledger",
                f"Ledger version: {self.version}",
                f"Goal: {self.goal or '(not recorded)'}",
            ]
            self._append_section(lines, "Constraints", self.constraints)
            self._append_section(lines, "Decisions", self.decisions)
            self._append_section(
                lines, "Acceptance criteria", self.acceptance_criteria
            )
            if self.referenced_files:
                lines.append("Referenced files (re-read for current bytes):")
                lines.extend(f"- {path}" for path in self.referenced_files[-30:])
            if self.modified_files:
                lines.append("Modified files:")
                lines.extend(
                    f"- {path} ({operation})"
                    for path, operation in list(self.modified_files.items())[-30:]
                )
            self._append_section(lines, "Artifacts", self.artifacts)
            self._append_section(lines, "Unresolved", self.unresolved)
            status = self.verification.get("status", "not_required")
            lines.append(
                "Verification: "
                + f"{status} (revision {self.verification.get('revision', 0)})"
            )
            if self.orchestration:
                lines.append(
                    "Orchestration: "
                    + str(self.orchestration.get("mode", "solo"))
                    + f", max agents={self.orchestration.get('max_agents', 0)}"
                )
            lines.append(
                "Ledger entries are pointers and decisions, not authoritative file "
                "contents. Re-read files before editing or asserting exact code."
            )
            rendered = "\n".join(lines)
        return rendered[:max_chars]

    def render_updates(self, since_version: int) -> str:
        with self._lock:
            if since_version >= self.version:
                return ""
            if since_version <= 0 or not self._updates:
                return self.render_for_prompt()
            updates = [
                update
                for update in self._updates
                if int(update.get("version", 0)) > since_version
            ]
            if not updates:
                return self.render_for_prompt()
            lines = [
                "## Context Ledger updates",
                f"Current ledger version: {self.version}",
            ]
            lines.extend(
                f"- v{item['version']}: {item['summary']}" for item in updates
            )
            lines.append(
                "Use the ledger as pointers only; re-read files for current bytes."
            )
            return "\n".join(lines)

    def _append_section(
        self, lines: list[str], title: str, values: Iterable[str]
    ) -> None:
        values = list(values)
        if values:
            lines.append(title + ":")
            lines.extend(f"- {value}" for value in values[-30:])

    @staticmethod
    def _extract_constraints(query: str) -> list[str]:
        pieces = re.split(r"(?<=[.!?。！？])\s+|\n+", query)
        return [
            piece.strip(" -")
            for piece in pieces
            if any(marker in piece.casefold() for marker in _CONSTRAINT_MARKERS)
        ]

    def _normalize_path(self, value: str) -> str:
        if not value:
            return ""
        try:
            path = Path(value).expanduser()
            if path.is_absolute():
                resolved = path.resolve()
                try:
                    return str(resolved.relative_to(self.work_dir))
                except ValueError:
                    return str(resolved)
            return str(path)
        except (OSError, RuntimeError, ValueError):
            return value.strip()

    @staticmethod
    def _short(value: str, limit: int = 180) -> str:
        value = " ".join(value.split())
        return value if len(value) <= limit else value[: limit - 3] + "..."

    def _commit(self, summary: str) -> None:
        self.version += 1
        self._updates.append(
            {
                "version": self.version,
                "summary": self._short(summary),
                "timestamp": time.time(),
            }
        )
        self._updates = self._updates[-80:]
        self._persist()

    def _as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "version": self.version,
            "goal": self.goal,
            "constraints": self.constraints,
            "decisions": self.decisions,
            "acceptance_criteria": self.acceptance_criteria,
            "referenced_files": self.referenced_files,
            "modified_files": self.modified_files,
            "artifacts": self.artifacts,
            "unresolved": self.unresolved,
            "verification": self.verification,
            "orchestration": self.orchestration,
            "updates": self._updates,
        }

    def _load(self) -> None:
        if not self.persist or not self._path.exists():
            return
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(value, dict):
            return
        self.version = int(value.get("version", 0))
        self.goal = str(value.get("goal", ""))
        for key in (
            "constraints",
            "decisions",
            "acceptance_criteria",
            "referenced_files",
            "artifacts",
            "unresolved",
        ):
            setattr(self, key, _unique(value.get(key, [])))
        self.modified_files = {
            str(key): str(item)
            for key, item in dict(value.get("modified_files", {})).items()
        }
        if isinstance(value.get("verification"), dict):
            self.verification = dict(value["verification"])
        if isinstance(value.get("orchestration"), dict):
            self.orchestration = dict(value["orchestration"])
        self._updates = list(value.get("updates", []))[-80:]

    def _persist(self) -> None:
        if not self.persist:
            return
        try:
            atomic_write_text(self._path, json.dumps(self._as_dict(), ensure_ascii=False, indent=2))
        except OSError:
            return
