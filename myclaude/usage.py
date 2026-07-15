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
    """Thread-safe accounting shared by primary and secondary model calls.

    缓存读/写与普通输入的单价不同（Anthropic cache read ~0.1x、cache write
    ~1.25x-2x input；OpenAI cache read ~0.25-0.5x）。用同一 input 单价计费会在
    cache-heavy 场景高估成本。这里允许分别配置 cache_read / cache_write 单价，
    未显式配置（None）时回退到 input 单价，保持与旧行为一致。
    """

    def __init__(
        self,
        *,
        input_cost_per_million: float = 0.0,
        output_cost_per_million: float = 0.0,
        cache_read_cost_per_million: float | None = None,
        cache_write_cost_per_million: float | None = None,
    ) -> None:
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        # None 表示"未配置" —— 回退到 input 单价，避免在没有价目表时凭空造数。
        self.cache_read_cost_per_million = (
            cache_read_cost_per_million
            if cache_read_cost_per_million is not None
            else input_cost_per_million
        )
        self.cache_write_cost_per_million = (
            cache_write_cost_per_million
            if cache_write_cost_per_million is not None
            else input_cost_per_million
        )
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
        uncached_input = max(input_tokens, 0)
        cache_read_tokens = max(cache_read, 0)
        cache_write_tokens = max(cache_creation, 0)
        completion_tokens = max(output_tokens, 0)
        cost = (
            uncached_input * self.input_cost_per_million
            + cache_read_tokens * self.cache_read_cost_per_million
            + cache_write_tokens * self.cache_write_cost_per_million
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
