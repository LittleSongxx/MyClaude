"""Eval runner: isolate a fixture, run a solver, score with deterministic oracles.

报告 B1：每个任务用独立临时目录隔离运行，每个配置跑 3 次以观察模型随机性，报告
成功率时同时展示 trial 数。runner 负责这套编排，并把 solver 抽象成协议——真实
solver 包一个 Agent，测试用 stub solver 应用已知补丁，从而整条评测链路无需真实
模型即可测试。

流程：复制 fixture → git init 基线（供 diff 白名单）→ 采集 protected 基线哈希 →
跑 solver（改动 work_dir、写 trace）→ 跑 oracle 评分 → 汇总多次 trial。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from myclaude.eval.oracle import (
    ScoreCard,
    check_diff_whitelist,
    check_no_conflict_markers,
    check_protected_unchanged,
    run_pytest,
    snapshot_hashes,
)
from myclaude.eval.task import EvalTask
from myclaude.eval.trace import TraceWriter


class Solver(Protocol):
    """把一个任务落到 work_dir 的求解器。

    真实实现包一个 Agent：读 task.prompt，在 work_dir 上跑 agent loop，用 trace 记录
    轨迹。测试实现可以只应用一个已知补丁。runner 不关心内部如何完成，只在其结束后
    用 oracle 客观评分——这正是"结果 oracle 与求解过程解耦"。
    """

    async def solve(self, task: EvalTask, work_dir: Path, trace: TraceWriter) -> None:
        ...


def _git(work_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(work_dir),
        capture_output=True,
        text=True,
        timeout=30.0,
    )


def _init_baseline_repo(work_dir: Path) -> None:
    """在隔离副本里初始化 git 并提交基线，使 diff/protected oracle 可用。

    用最小的本地身份配置，避免依赖运行者的全局 git 配置。
    """
    _git(work_dir, "init", "-q")
    _git(work_dir, "config", "user.email", "eval@myclaude.local")
    _git(work_dir, "config", "user.name", "myclaude-eval")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "eval baseline")


def _score(task: EvalTask, work_dir: Path, trial: int, baseline: dict[str, str]) -> ScoreCard:
    """按任务规格组合 oracle 打分。"""
    card = ScoreCard(task_id=task.task_id, trial=trial)

    if task.expect_no_changes:
        # "不应改代码"类任务：有任何白名单外改动才算失败；这里用空白名单表达
        # "任何改动都算越界"。仍跑测试确认没把仓库改坏。
        card.add(check_diff_whitelist(work_dir, task.diff_whitelist))
    else:
        if task.diff_whitelist:
            card.add(check_diff_whitelist(work_dir, task.diff_whitelist))
        if task.protected:
            card.add(check_protected_unchanged(work_dir, task.protected, baseline))
        if task.conflict_search_paths:
            card.add(check_no_conflict_markers(work_dir, task.conflict_search_paths))

    # 测试永远跑：它是"任务是否真正完成"的最终裁判。
    card.add(run_pytest(work_dir, task.test_target))
    return card


async def run_trial(
    task: EvalTask,
    solver: Solver,
    trial: int,
    *,
    out_dir: Path,
) -> ScoreCard:
    """跑单个任务的一次 trial：隔离副本 → solver → oracle 评分。"""
    out_dir = Path(out_dir)
    with tempfile.TemporaryDirectory(prefix=f"eval-{task.task_id}-") as tmp:
        work_dir = Path(tmp) / "repo"
        shutil.copytree(task.repo_path, work_dir)
        _init_baseline_repo(work_dir)
        baseline = snapshot_hashes(work_dir, task.protected)

        trace_path = out_dir / f"{task.task_id}.trial{trial}.jsonl"
        trace = TraceWriter(trace_path, run_id=f"{task.task_id}-t{trial}")
        trace.emit("task_start", purpose="eval", tool_name=None)

        try:
            await solver.solve(task, work_dir, trace)
            solver_error: str | None = None
        except Exception as exc:  # solver 崩溃不应中断整个 suite
            solver_error = f"{type(exc).__name__}: {exc}"
            trace.emit("solver_error", error_type=type(exc).__name__, success=False)

        card = _score(task, work_dir, trial, baseline)
        trace.emit(
            "task_end",
            purpose="eval",
            success=card.success,
            error_type=solver_error,
        )
        return card


@dataclass
class TaskReport:
    """一个任务跨多次 trial 的汇总。"""

    task_id: str
    cards: list[ScoreCard] = field(default_factory=list)

    @property
    def trials(self) -> int:
        return len(self.cards)

    @property
    def successes(self) -> int:
        return sum(1 for c in self.cards if c.success)

    @property
    def success_rate(self) -> float:
        return self.successes / self.trials if self.trials else 0.0

    def summary(self) -> str:
        # 同时展示成功数与 trial 数——不把一次偶然成功包装成稳定能力。
        return f"{self.task_id}: {self.successes}/{self.trials} passed ({self.success_rate:.0%})"


async def run_task(
    task: EvalTask,
    solver: Solver,
    *,
    trials: int = 3,
    out_dir: Path,
) -> TaskReport:
    """跑一个任务的 N 次 trial，聚合成功率。"""
    report = TaskReport(task_id=task.task_id)
    for trial in range(1, trials + 1):
        card = await run_trial(task, solver, trial, out_dir=out_dir)
        report.cards.append(card)
    return report
