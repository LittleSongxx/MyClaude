"""Stable, versioned JSONL event trace for eval and failure analysis.

报告 B1：只评价最终文本不足以说明 Coding Agent 好坏，必须能复现和分析失败轨迹。
这里定义一个**版本化的事件 schema**，写成本地 JSONL——不部署可观测平台，但字段
命名对齐 OpenTelemetry GenAI semantic conventions，未来确实需要时可直接接 Collector。

设计要点：
- 单一 append-only JSONL，每行一个事件，天然可 grep / jq / pandas 分析。
- schema_version 固定在每行，schema 演进时旧 trace 仍可读。
- TraceEvent 是纯数据类，与 Agent 解耦，可独立测试、可在 eval runner 内直接构造。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


@dataclass
class TraceEvent:
    """一次运行中的单个事件（一轮 LLM 调用、一次工具调用、一次压缩等）。

    字段刻意做成扁平、可选，方便直接写成 JSONL 一行并用列式工具分析。命名参考
    OTel GenAI（gen_ai.* 语义），但保持精简，只保留 eval 真正会用到的维度。
    """

    event_type: str
    run_id: str
    schema_version: int = SCHEMA_VERSION
    parent_run_id: str | None = None
    session_id: str | None = None

    # 模型维度
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    purpose: str | None = None  # agent / compact / memory-recall / eval-oracle ...

    # 时间维度
    started_at: float = field(default_factory=time.time)
    duration_ms: float | None = None

    # 工具维度
    tool_name: str | None = None
    tool_call_id: str | None = None
    target: str | None = None  # 工具作用对象：ReadFile 的路径、Bash 的命令等
    result_size: int | None = None
    error_type: str | None = None

    # token 维度（cache 读写分列，配合 C2 计价）
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_fingerprint: str | None = None
    cache_hit_rate: float | None = None
    cache_break_reasons: list[str] | None = None

    # Runtime contract dimensions
    verification_status: str | None = None
    verification_revision: int | None = None
    orchestration_mode: str | None = None
    max_agents: int | None = None

    # 结束维度
    stop_reason: str | None = None
    limit_reason: str | None = None
    success: bool | None = None

    def to_json_line(self) -> str:
        # 丢弃 None 字段，保持 JSONL 紧凑；分析端按缺省处理缺失维度。
        data = {k: v for k, v in asdict(self).items() if v is not None}
        return json.dumps(data, ensure_ascii=False)


class TraceWriter:
    """把 TraceEvent 追加写入单个 JSONL 文件。

    append-only、每行 flush，进程被杀也能保留已写事件——失败分析最需要的正是
    "崩溃前发生了什么"。非线程安全的高频写不是目标；eval 是顺序运行。
    """

    def __init__(self, path: Path, *, run_id: str, session_id: str | None = None) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.session_id = session_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events: list[TraceEvent] = []

    def emit(self, event_type: str, **fields: Any) -> TraceEvent:
        """构造并落盘一个事件；run_id / session_id 自动填充。"""
        event = TraceEvent(
            event_type=event_type,
            run_id=self.run_id,
            session_id=self.session_id,
            **fields,
        )
        self._events.append(event)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(event.to_json_line() + "\n")
        return event

    @property
    def events(self) -> list[TraceEvent]:
        return list(self._events)


def read_trace(path: Path) -> list[dict[str, Any]]:
    """读回一个 JSONL trace 文件为 dict 列表（分析 / 测试用）。"""
    path = Path(path)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows
