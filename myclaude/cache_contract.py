from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


def _digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _message_fingerprint(message: Any) -> str:
    """Hash only provider-visible message fields."""
    return _digest(
        {
            "role": getattr(message, "role", ""),
            "content": getattr(message, "content", ""),
            "tool_uses": [
                {
                    "id": getattr(tool_use, "tool_use_id", ""),
                    "name": getattr(tool_use, "tool_name", ""),
                    "arguments": getattr(tool_use, "arguments", {}),
                }
                for tool_use in getattr(message, "tool_uses", [])
            ],
            "tool_results": [
                {
                    "id": getattr(tool_result, "tool_use_id", ""),
                    "content": getattr(tool_result, "content", ""),
                    "is_error": getattr(tool_result, "is_error", False),
                    "content_blocks": getattr(tool_result, "content_blocks", []),
                }
                for tool_result in getattr(message, "tool_results", [])
            ],
            "thinking": [
                {
                    "thinking": getattr(block, "thinking", ""),
                    "signature": getattr(block, "signature", ""),
                }
                for block in getattr(message, "thinking_blocks", [])
            ],
        }
    )


@dataclass(frozen=True)
class CacheSnapshot:
    model: str
    system_hash: str
    tool_names: tuple[str, ...]
    tool_hashes: tuple[tuple[str, str], ...]
    message_hashes: tuple[str, ...]
    prefix_fingerprint: str
    request_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "system_hash": self.system_hash,
            "tool_names": list(self.tool_names),
            "tool_hashes": [list(item) for item in self.tool_hashes],
            "message_hashes": list(self.message_hashes),
            "prefix_fingerprint": self.prefix_fingerprint,
            "request_fingerprint": self.request_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CacheSnapshot":
        return cls(
            model=str(value.get("model", "")),
            system_hash=str(value.get("system_hash", "")),
            tool_names=tuple(str(item) for item in value.get("tool_names", [])),
            tool_hashes=tuple(
                (str(item[0]), str(item[1]))
                for item in value.get("tool_hashes", [])
                if isinstance(item, (list, tuple)) and len(item) == 2
            ),
            message_hashes=tuple(
                str(item) for item in value.get("message_hashes", [])
            ),
            prefix_fingerprint=str(value.get("prefix_fingerprint", "")),
            request_fingerprint=str(value.get("request_fingerprint", "")),
        )


@dataclass(frozen=True)
class CacheInspection:
    snapshot: CacheSnapshot
    break_reasons: tuple[str, ...] = ()
    expected_reuse: bool = False


@dataclass(frozen=True)
class CacheObservation:
    fingerprint: str
    break_reasons: tuple[str, ...]
    expected_reuse: bool
    prompt_tokens: int
    cache_read: int
    cache_creation: int
    request_hit_rate: float
    cumulative_hit_rate: float
    unexpected_miss: bool


@dataclass
class _CacheTotals:
    prompt_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    requests: int = 0


class CacheContract:
    """Track the stable prefix promised to the provider cache."""

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
        self._previous: CacheSnapshot | None = None
        self._totals = _CacheTotals()
        self._path = self._build_path(owner_id)
        self._load()

    def _build_path(self, owner_id: str) -> Path:
        safe_owner = "".join(
            char if char.isalnum() or char in "._-" else "_"
            for char in owner_id
        ) or "default"
        return self.work_dir / ".myclaude" / "cache-contract" / f"{safe_owner}.jsonl"

    def rebind(self, owner_id: str) -> None:
        if not owner_id or owner_id == self.owner_id:
            return
        self.owner_id = owner_id
        self._previous = None
        self._totals = _CacheTotals()
        self._path = self._build_path(owner_id)
        self._load()

    @property
    def previous(self) -> CacheSnapshot | None:
        return self._previous

    def inspect(
        self,
        *,
        model: str,
        system: str,
        tools: Iterable[dict[str, Any]] | None,
        messages: Iterable[Any],
    ) -> CacheInspection:
        normalized_tools = list(tools or [])
        tool_names = tuple(str(tool.get("name", "")) for tool in normalized_tools)
        tool_hashes = tuple(
            (name, _digest(tool))
            for name, tool in zip(tool_names, normalized_tools, strict=False)
        )
        system_hash = _digest(system)
        message_hashes = tuple(_message_fingerprint(message) for message in messages)
        prefix_fingerprint = _digest(
            {
                "model": model,
                "system": system_hash,
                "tools": tool_hashes,
            }
        )
        request_fingerprint = _digest(
            {
                "prefix": prefix_fingerprint,
                "messages": message_hashes,
            }
        )
        snapshot = CacheSnapshot(
            model=model,
            system_hash=system_hash,
            tool_names=tool_names,
            tool_hashes=tool_hashes,
            message_hashes=message_hashes,
            prefix_fingerprint=prefix_fingerprint,
            request_fingerprint=request_fingerprint,
        )
        if self._previous is None:
            return CacheInspection(snapshot, ("cold_start",), False)

        reasons: list[str] = []
        if self._previous.model != model:
            reasons.append("model_changed")
        if self._previous.system_hash != system_hash:
            reasons.append("system_changed")
        if self._previous.tool_names != tool_names:
            previous_names = set(self._previous.tool_names)
            current_names = set(tool_names)
            if previous_names != current_names:
                reasons.append("tool_set_changed")
            else:
                reasons.append("tool_order_changed")
        else:
            previous_hashes = dict(self._previous.tool_hashes)
            changed = [
                name
                for name, digest in tool_hashes
                if previous_hashes.get(name) != digest
            ]
            if changed:
                reasons.append("tool_schema_changed:" + ",".join(changed))

        previous_messages = self._previous.message_hashes
        if (
            len(message_hashes) < len(previous_messages)
            or tuple(message_hashes[: len(previous_messages)]) != previous_messages
        ):
            reasons.append("conversation_prefix_changed")

        return CacheInspection(snapshot, tuple(reasons), not reasons)

    def complete(
        self,
        inspection: CacheInspection,
        *,
        input_tokens: int,
        cache_read: int = 0,
        cache_creation: int = 0,
    ) -> CacheObservation:
        prompt_tokens = max(input_tokens, 0) + max(cache_read, 0) + max(
            cache_creation, 0
        )
        cache_read = max(cache_read, 0)
        cache_creation = max(cache_creation, 0)
        self._totals.prompt_tokens += prompt_tokens
        self._totals.cache_read += cache_read
        self._totals.cache_creation += cache_creation
        self._totals.requests += 1
        request_hit_rate = cache_read / prompt_tokens if prompt_tokens else 0.0
        total_prompt = self._totals.prompt_tokens
        cumulative_hit_rate = (
            self._totals.cache_read / total_prompt if total_prompt else 0.0
        )
        observation = CacheObservation(
            fingerprint=inspection.snapshot.prefix_fingerprint,
            break_reasons=inspection.break_reasons,
            expected_reuse=inspection.expected_reuse,
            prompt_tokens=prompt_tokens,
            cache_read=cache_read,
            cache_creation=cache_creation,
            request_hit_rate=request_hit_rate,
            cumulative_hit_rate=cumulative_hit_rate,
            unexpected_miss=(
                inspection.expected_reuse
                and prompt_tokens >= 1024
                and cache_read == 0
                and cache_creation == 0
            ),
        )
        self._previous = inspection.snapshot
        self._persist(observation)
        return observation

    def _load(self) -> None:
        if not self.persist or not self._path.exists():
            return
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            snapshot = row.get("snapshot")
            if isinstance(snapshot, dict):
                self._previous = CacheSnapshot.from_dict(snapshot)
                totals = row.get("totals")
                if isinstance(totals, dict):
                    self._totals = _CacheTotals(
                        prompt_tokens=int(totals.get("prompt_tokens", 0)),
                        cache_read=int(totals.get("cache_read", 0)),
                        cache_creation=int(totals.get("cache_creation", 0)),
                        requests=int(totals.get("requests", 0)),
                    )
                return

    def _persist(self, observation: CacheObservation) -> None:
        if not self.persist or self._previous is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            row = {
                "timestamp": time.time(),
                "observation": {
                    "fingerprint": observation.fingerprint,
                    "break_reasons": list(observation.break_reasons),
                    "expected_reuse": observation.expected_reuse,
                    "prompt_tokens": observation.prompt_tokens,
                    "cache_read": observation.cache_read,
                    "cache_creation": observation.cache_creation,
                    "request_hit_rate": observation.request_hit_rate,
                    "cumulative_hit_rate": observation.cumulative_hit_rate,
                    "unexpected_miss": observation.unexpected_miss,
                },
                "snapshot": self._previous.to_dict(),
                "totals": {
                    "prompt_tokens": self._totals.prompt_tokens,
                    "cache_read": self._totals.cache_read,
                    "cache_creation": self._totals.cache_creation,
                    "requests": self._totals.requests,
                },
            }
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            if os.name == "posix":
                os.chmod(self._path, 0o600)
        except OSError:
            # Observability must never stop an agent run.
            return
