"""Trajectory metrics derived from a run's trace events.

报告 B1：好的 eval 既看结果，也看**轨迹**。同样"成功"的两次运行，一个 3 轮干净
完成、一个 20 轮反复试错烧掉十倍 token，工程价值完全不同。这里从 TraceEvent 列表
派生一组与结果无关的效率 / 质量指标，用于消融对比（no-memory / no-subagent …）。

纯函数、无副作用，输入是 trace 事件（TraceEvent 或等价 dict），可独立测试。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from myclaude.eval.trace import TraceEvent


@dataclass
class TrajectoryMetrics:
    # 效率
    llm_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_duration_ms: float = 0.0
    # 轨迹质量
    repeated_file_reads: int = 0          # 同一文件被读多于一次的"多余"次数
    repeated_failed_commands: int = 0     # 同一命令重复失败的次数
    tools_by_name: dict[str, int] = field(default_factory=dict)

    def estimated_cost_usd(
        self,
        *,
        input_rate: float,
        output_rate: float,
        cache_read_rate: float | None = None,
        cache_write_rate: float | None = None,
    ) -> float:
        """按 C2 的分列单价估算成本（cache 单价缺省回退 input 单价）。"""
        cr = cache_read_rate if cache_read_rate is not None else input_rate
        cw = cache_write_rate if cache_write_rate is not None else input_rate
        return (
            self.input_tokens * input_rate
            + self.cache_read_tokens * cr
            + self.cache_write_tokens * cw
            + self.output_tokens * output_rate
        ) / 1_000_000


def _as_dict(event: TraceEvent | dict[str, Any]) -> dict[str, Any]:
    if isinstance(event, TraceEvent):
        from dataclasses import asdict
        return asdict(event)
    return event


def compute_metrics(events: list[TraceEvent | dict[str, Any]]) -> TrajectoryMetrics:
    """从 trace 事件派生轨迹指标。

    识别的事件类型（event_type）：
      - ``llm_call``：一次模型调用，携带 token 维度。
      - ``tool_call``：一次工具调用，携带 tool_name / error_type / result_size。
    其它事件类型被忽略，方便 schema 增长而不破坏此函数。
    """
    m = TrajectoryMetrics()
    file_reads: Counter[str] = Counter()
    failed_cmds: Counter[str] = Counter()
    tool_names: Counter[str] = Counter()

    for raw in events:
        e = _as_dict(raw)
        etype = e.get("event_type")
        if e.get("duration_ms"):
            m.total_duration_ms += float(e["duration_ms"])

        if etype == "llm_call":
            m.llm_calls += 1
            m.input_tokens += int(e.get("input_tokens") or 0)
            m.output_tokens += int(e.get("output_tokens") or 0)
            m.cache_read_tokens += int(e.get("cache_read_tokens") or 0)
            m.cache_write_tokens += int(e.get("cache_write_tokens") or 0)

        elif etype == "tool_call":
            m.tool_calls += 1
            name = e.get("tool_name") or "unknown"
            tool_names[name] += 1
            failed = bool(e.get("error_type"))
            if failed:
                m.tool_errors += 1
            target = e.get("target")
            # 重复读取：同一 ReadFile 目标出现多次（多余的重读是浪费信号）。
            if name == "ReadFile" and target:
                file_reads[str(target)] += 1
            # 重复失败命令：同一 Bash 命令重复失败而不改变策略（无效循环信号）。
            if name == "Bash" and failed and target:
                failed_cmds[str(target)] += 1

    m.repeated_file_reads = sum(c - 1 for c in file_reads.values() if c > 1)
    m.repeated_failed_commands = sum(c - 1 for c in failed_cmds.values() if c > 1)
    m.tools_by_name = dict(tool_names)
    return m
