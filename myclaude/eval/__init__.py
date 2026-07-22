"""轻量 Coding-Agent 评测闭环（报告 B1）。

回答"你怎么知道 Agent 好用"：用小型真实 fixture + 确定性 oracle + 版本化 trace，
证明成功率、成本、轨迹，而不是只看最终文本。故意保持小而可复现，不追 SWE-bench 排名。

- trace：版本化 JSONL 事件（可复现失败分析）。
- oracle：测试 / diff 白名单 / 受保护文件 / 冲突标记等确定性判据。
- metrics：从 trace 派生的轨迹质量指标（turns、tool error、重复读取……）。
- task：YAML 任务规格加载。
- runner：隔离 fixture → solver → oracle 评分 → 多 trial 聚合。
"""
from __future__ import annotations

from myclaude.eval.metrics import TrajectoryMetrics, compute_metrics
from myclaude.eval.oracle import (
    OracleResult,
    ScoreCard,
    check_diff_whitelist,
    check_no_conflict_markers,
    check_protected_unchanged,
    run_pytest,
    run_verification_command,
    snapshot_hashes,
)
from myclaude.eval.task import EvalTask, discover_tasks, load_suite, load_task
from myclaude.eval.runner import (
    Solver,
    SuiteReport,
    TaskReport,
    run_suite,
    run_task,
    run_trial,
)
from myclaude.eval.trace import (
    SCHEMA_VERSION,
    TraceEvent,
    TraceWriter,
    read_trace,
)

__all__ = [
    "TrajectoryMetrics",
    "compute_metrics",
    "OracleResult",
    "ScoreCard",
    "check_diff_whitelist",
    "check_no_conflict_markers",
    "check_protected_unchanged",
    "run_pytest",
    "run_verification_command",
    "snapshot_hashes",
    "EvalTask",
    "load_task",
    "load_suite",
    "discover_tasks",
    "Solver",
    "TaskReport",
    "SuiteReport",
    "run_suite",
    "run_task",
    "run_trial",
    "SCHEMA_VERSION",
    "TraceEvent",
    "TraceWriter",
    "read_trace",
]
