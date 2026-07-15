from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RunLimits:
    max_turns: int = 0
    max_wall_time_seconds: float = 0.0
    max_total_tokens: int = 0
    max_cost_usd: float = 0.0


@dataclass(frozen=True)
class UsageSnapshot:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    request_count: int = 0
    estimated_cost_usd: float = 0.0
    by_purpose: dict[str, int] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read
            + self.cache_creation
        )


class UsageLedger:
    """Thread-safe accounting shared by primary and secondary model calls."""

    def __init__(
        self,
        *,
        input_cost_per_million: float = 0.0,
        output_cost_per_million: float = 0.0,
    ) -> None:
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        self._input_tokens = 0
        self._output_tokens = 0
        self._cache_read = 0
        self._cache_creation = 0
        self._request_count = 0
        self._estimated_cost_usd = 0.0
        self._by_purpose: dict[str, int] = {}
        self._lock = threading.Lock()

    def record(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read: int = 0,
        cache_creation: int = 0,
        purpose: str = "agent",
    ) -> None:
        prompt_tokens = max(input_tokens, 0) + max(cache_read, 0) + max(cache_creation, 0)
        completion_tokens = max(output_tokens, 0)
        cost = (
            prompt_tokens * self.input_cost_per_million
            + completion_tokens * self.output_cost_per_million
        ) / 1_000_000
        with self._lock:
            self._input_tokens += max(input_tokens, 0)
            self._output_tokens += completion_tokens
            self._cache_read += max(cache_read, 0)
            self._cache_creation += max(cache_creation, 0)
            self._request_count += 1
            self._estimated_cost_usd += cost
            self._by_purpose[purpose] = self._by_purpose.get(purpose, 0) + 1

    def snapshot(self) -> UsageSnapshot:
        with self._lock:
            return UsageSnapshot(
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                cache_read=self._cache_read,
                cache_creation=self._cache_creation,
                request_count=self._request_count,
                estimated_cost_usd=self._estimated_cost_usd,
                by_purpose=dict(self._by_purpose),
            )
