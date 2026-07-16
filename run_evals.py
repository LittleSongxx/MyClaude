#!/usr/bin/env python3
"""Coding-Agent 本地评测入口（报告 B1）。

用小型真实 fixture + 确定性 oracle + 版本化 trace 证明 Agent 的成功率 / 成本 /
轨迹，而不是只看最终文本。故意保持小而可复现，不追 SWE-bench 排名。

用法：
  # 只列出发现的任务（无需模型，可离线验证 harness 装配正确）
  python run_evals.py --list

  # 跑全部任务，每个 3 次 trial（需要在 config.yaml 配好 provider / API key）
  python run_evals.py --trials 3

  # 消融：关掉记忆 / 子 Agent，与 baseline 对比成功率和成本
  python run_evals.py --no-memory
  python run_evals.py --no-subagent

  # 只跑某个任务
  python run_evals.py --task single-file-bug

trace 默认写到 evals/_out/<task>.trial<N>.jsonl，可用 jq 分析失败轨迹。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
EVALS_DIR = REPO_ROOT / "evals"
DEFAULT_OUT_DIR = EVALS_DIR / "_out"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MyClaude 本地 Coding-Agent 评测")
    p.add_argument("--evals-dir", default=str(EVALS_DIR), help="任务目录（默认 ./evals）")
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="trace 输出目录")
    p.add_argument("--task", default="", help="只跑指定 task_id（默认全部）")
    p.add_argument("--trials", type=int, default=3, help="每个任务的 trial 次数")
    p.add_argument("--list", action="store_true", help="只列出发现的任务，不运行")
    p.add_argument("--no-memory", action="store_true", help="消融：禁用记忆")
    p.add_argument("--no-subagent", action="store_true", help="消融：禁用 fork/子 Agent")
    p.add_argument("--config", default="", help="config.yaml 路径（留空走默认查找）")
    return p.parse_args(argv)


def _discover(evals_dir: Path, task_filter: str):
    from myclaude.eval import discover_tasks

    tasks = discover_tasks(evals_dir)
    if task_filter:
        tasks = [t for t in tasks if t.task_id == task_filter]
    return tasks


def _cmd_list(evals_dir: Path, task_filter: str) -> int:
    tasks = _discover(evals_dir, task_filter)
    if not tasks:
        print(f"No tasks found under {evals_dir}", file=sys.stderr)
        return 1
    print(f"Discovered {len(tasks)} eval task(s):\n")
    for t in tasks:
        kind = "explain-only" if t.expect_no_changes else "code-change"
        print(f"  {t.task_id}  [{kind}]")
        if t.description:
            print(f"      {t.description.strip().splitlines()[0]}")
        print(f"      test_target={t.test_target or '(all)'}  whitelist={t.diff_whitelist}")
    return 0


async def _cmd_run(args: argparse.Namespace) -> int:
    from myclaude.config import load_config
    from myclaude.eval import run_task
    from myclaude.eval.agent_solver import AgentSolver

    evals_dir = Path(args.evals_dir)
    out_dir = Path(args.out_dir)
    tasks = _discover(evals_dir, args.task)
    if not tasks:
        print(f"No tasks found under {evals_dir}", file=sys.stderr)
        return 1

    config = load_config(args.config) if args.config else load_config()
    if not config.providers:
        print("No provider configured; cannot run agent solver.", file=sys.stderr)
        return 2
    provider = config.providers[0]

    solver = AgentSolver(
        provider,
        memory_enabled=not args.no_memory,
        enable_fork=not args.no_subagent,
    )

    variant = "baseline"
    if args.no_memory:
        variant = "no-memory"
    elif args.no_subagent:
        variant = "no-subagent"
    print(f"Running {len(tasks)} task(s) × {args.trials} trial(s)  [variant={variant}]\n")

    reports = []
    for task in tasks:
        report = await run_task(task, solver, trials=args.trials, out_dir=out_dir)
        reports.append(report)
        print(report.summary())

    total_success = sum(r.successes for r in reports)
    total_trials = sum(r.trials for r in reports)
    rate = total_success / total_trials if total_trials else 0.0
    print(f"\nOverall: {total_success}/{total_trials} passed ({rate:.0%})  [variant={variant}]")
    print(f"Traces written under {out_dir}")
    # 只要有失败就返回非零，方便 CI / 脚本判定。
    return 0 if total_success == total_trials else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    evals_dir = Path(args.evals_dir)
    if args.list:
        return _cmd_list(evals_dir, args.task)
    return asyncio.run(_cmd_run(args))


if __name__ == "__main__":
    sys.exit(main())
