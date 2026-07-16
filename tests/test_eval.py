"""Eval 闭环测试（报告 B1）。

用 stub solver 端到端验证整条评测链路——隔离 fixture → solver 改动 → 确定性
oracle 评分 → 多 trial 聚合——完全不依赖真实模型即可跑。这正是"结果 oracle 与
求解过程解耦"的价值：runner 不关心 solver 内部如何完成，只客观判定结果。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from myclaude.eval import (
    EvalTask,
    ScoreCard,
    TraceEvent,
    TraceWriter,
    check_diff_whitelist,
    check_no_conflict_markers,
    check_protected_unchanged,
    compute_metrics,
    discover_tasks,
    load_task,
    read_trace,
    run_pytest,
    run_task,
    run_trial,
    snapshot_hashes,
)

EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"


# ---------------------------------------------------------------------------
# trace：版本化 JSONL 事件
# ---------------------------------------------------------------------------

class TestTrace:
    def test_event_drops_none_fields(self) -> None:
        line = TraceEvent(event_type="llm_call", run_id="r1", input_tokens=10).to_json_line()
        assert '"event_type"' in line and '"input_tokens"' in line
        # None 字段不落盘，保持 JSONL 紧凑。
        assert "duration_ms" not in line
        assert "tool_name" not in line

    def test_writer_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "trace.jsonl"
        w = TraceWriter(path, run_id="run-1", session_id="s1")
        w.emit("llm_call", input_tokens=5, output_tokens=3)
        w.emit("tool_call", tool_name="ReadFile", target="a.py")
        rows = read_trace(path)
        assert len(rows) == 2
        assert rows[0]["run_id"] == "run-1"
        assert rows[0]["session_id"] == "s1"
        assert rows[1]["tool_name"] == "ReadFile"

    def test_schema_version_stamped(self, tmp_path: Path) -> None:
        w = TraceWriter(tmp_path / "t.jsonl", run_id="r")
        ev = w.emit("task_start")
        assert ev.schema_version >= 1


# ---------------------------------------------------------------------------
# metrics：从 trace 派生的轨迹指标
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_counts_llm_and_tool_calls(self) -> None:
        events = [
            TraceEvent("llm_call", "r", input_tokens=10, output_tokens=4, cache_read_tokens=100),
            TraceEvent("tool_call", "r", tool_name="ReadFile", target="a.py"),
            TraceEvent("tool_call", "r", tool_name="Bash", target="ls", error_type="NonZeroExit"),
        ]
        m = compute_metrics(events)
        assert m.llm_calls == 1
        assert m.tool_calls == 2
        assert m.tool_errors == 1
        assert m.input_tokens == 10
        assert m.cache_read_tokens == 100
        assert m.tools_by_name == {"ReadFile": 1, "Bash": 1}

    def test_repeated_file_reads(self) -> None:
        events = [
            TraceEvent("tool_call", "r", tool_name="ReadFile", target="a.py"),
            TraceEvent("tool_call", "r", tool_name="ReadFile", target="a.py"),
            TraceEvent("tool_call", "r", tool_name="ReadFile", target="b.py"),
        ]
        m = compute_metrics(events)
        # a.py 读了两次 → 1 次多余重读；b.py 只读一次 → 0。
        assert m.repeated_file_reads == 1

    def test_repeated_failed_commands(self) -> None:
        events = [
            TraceEvent("tool_call", "r", tool_name="Bash", target="pytest", error_type="Fail"),
            TraceEvent("tool_call", "r", tool_name="Bash", target="pytest", error_type="Fail"),
        ]
        m = compute_metrics(events)
        assert m.repeated_failed_commands == 1

    def test_cost_uses_distinct_cache_rate(self) -> None:
        m = compute_metrics([
            TraceEvent("llm_call", "r", input_tokens=1_000_000, cache_read_tokens=1_000_000),
        ])
        # input 全价，cache_read 十分之一价 → 1.0 + 0.1 = 1.1。
        cost = m.estimated_cost_usd(input_rate=1.0, output_rate=1.0, cache_read_rate=0.1)
        assert cost == pytest.approx(1.1)


# ---------------------------------------------------------------------------
# oracle：确定性判据
# ---------------------------------------------------------------------------

def _make_repo(tmp_path: Path, *, buggy: bool) -> Path:
    """建一个最小 fixture：一个模块 + 一个测试。buggy=True 时测试失败。"""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    add = "a - b" if buggy else "a + b"
    (repo / "calc.py").write_text(f"def add(a, b):\n    return {add}\n", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    return repo


class TestOracle:
    def test_pytest_pass_and_fail(self, tmp_path: Path) -> None:
        good = _make_repo(tmp_path / "g", buggy=False)
        bad = _make_repo(tmp_path / "b", buggy=True)
        assert run_pytest(good).passed is True
        assert run_pytest(bad).passed is False

    def test_diff_whitelist(self, tmp_path: Path) -> None:
        import subprocess

        repo = _make_repo(tmp_path, buggy=True)
        for args in (["init", "-q"], ["config", "user.email", "e@e"],
                     ["config", "user.name", "n"], ["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run(["git", *args], cwd=repo, capture_output=True)
        # 只改 calc.py：在白名单内应通过。
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        assert check_diff_whitelist(repo, ["calc.py"]).passed is True
        # 也改测试：不在白名单内应失败。
        (repo / "tests" / "test_calc.py").write_text("# tampered\n", encoding="utf-8")
        assert check_diff_whitelist(repo, ["calc.py"]).passed is False

    def test_protected_unchanged(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, buggy=True)
        baseline = snapshot_hashes(repo, ["tests/test_calc.py"])
        # 未改：通过。
        assert check_protected_unchanged(repo, ["tests/test_calc.py"], baseline).passed is True
        # 改了受保护测试：失败（防止改测试作弊）。
        (repo / "tests" / "test_calc.py").write_text("# cheat\n", encoding="utf-8")
        assert check_protected_unchanged(repo, ["tests/test_calc.py"], baseline).passed is False

    def test_conflict_markers(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, buggy=False)
        assert check_no_conflict_markers(repo, ["calc.py"]).passed is True
        (repo / "calc.py").write_text("<<<<<<< HEAD\nx\n>>>>>>> other\n", encoding="utf-8")
        assert check_no_conflict_markers(repo, ["calc.py"]).passed is False

    def test_scorecard_success_requires_all_pass(self) -> None:
        from myclaude.eval import OracleResult

        card = ScoreCard(task_id="t", trial=1)
        card.add(OracleResult("a", True))
        assert card.success is True
        card.add(OracleResult("b", False))
        assert card.success is False


# ---------------------------------------------------------------------------
# task：加载真实 fixture 规格
# ---------------------------------------------------------------------------

class TestTask:
    def test_discover_bundled_fixtures(self) -> None:
        tasks = discover_tasks(EVALS_DIR)
        ids = {t.task_id for t in tasks}
        assert "single-file-bug" in ids
        assert "explain-no-change" in ids

    def test_load_single_file_bug_spec(self) -> None:
        task = load_task(EVALS_DIR / "single-file-bug")
        assert task.prompt
        assert task.test_target == "tests/test_ledger.py"
        assert "ledger.py" in task.diff_whitelist
        assert "tests/test_ledger.py" in task.protected


# ---------------------------------------------------------------------------
# runner：stub solver 端到端
# ---------------------------------------------------------------------------

class _FixerSolver:
    """正确修复 single-file-bug fixture 的 stub solver。"""

    async def solve(self, task: EvalTask, work_dir: Path, trace: TraceWriter) -> None:
        trace.emit("llm_call", input_tokens=50, output_tokens=20)
        trace.emit("tool_call", tool_name="EditFile", target="ledger.py")
        ledger = work_dir / "ledger.py"
        fixed = (
            '"""A tiny transaction ledger."""\n'
            "from __future__ import annotations\n\n\n"
            "def running_balance(amounts: list[float]) -> list[float]:\n"
            "    balance = 0.0\n"
            "    result: list[float] = []\n"
            "    for amount in amounts:\n"
            "        balance += amount\n"
            "        result.append(balance)\n"
            "    return result\n"
        )
        ledger.write_text(fixed, encoding="utf-8")


class _CheaterSolver:
    """通过篡改受保护测试来"通过"的 solver——必须被 oracle 抓住。"""

    async def solve(self, task: EvalTask, work_dir: Path, trace: TraceWriter) -> None:
        trace.emit("tool_call", tool_name="EditFile", target="tests/test_ledger.py")
        target = work_dir / "tests" / "test_ledger.py"
        target.write_text(
            "def test_balance_with_refunds():\n    assert True\n"
            "def test_all_positive():\n    assert True\n"
            "def test_empty():\n    assert True\n",
            encoding="utf-8",
        )


class _NoopSolver:
    """什么都不改——用于 expect_no_changes 任务。"""

    async def solve(self, task: EvalTask, work_dir: Path, trace: TraceWriter) -> None:
        trace.emit("llm_call", input_tokens=30, output_tokens=10)


class TestRunnerEndToEnd:
    @pytest.mark.asyncio
    async def test_fixer_passes_single_file_bug(self, tmp_path: Path) -> None:
        task = load_task(EVALS_DIR / "single-file-bug")
        card = await run_trial(task, _FixerSolver(), 1, out_dir=tmp_path)
        assert card.success is True, card.summary()

    @pytest.mark.asyncio
    async def test_cheater_fails_protected(self, tmp_path: Path) -> None:
        task = load_task(EVALS_DIR / "single-file-bug")
        card = await run_trial(task, _CheaterSolver(), 1, out_dir=tmp_path)
        # 即便所有测试"通过"，篡改受保护文件也让整体判定失败。
        assert card.success is False
        assert any(
            r.name == "protected_unchanged" and not r.passed for r in card.results
        )

    @pytest.mark.asyncio
    async def test_noop_passes_explain_no_change(self, tmp_path: Path) -> None:
        task = load_task(EVALS_DIR / "explain-no-change")
        card = await run_trial(task, _NoopSolver(), 1, out_dir=tmp_path)
        assert card.success is True, card.summary()

    @pytest.mark.asyncio
    async def test_noop_fails_bug_fix(self, tmp_path: Path) -> None:
        # 不改代码却要求修 bug：测试仍失败 → 任务失败。多 trial 聚合成功率为 0。
        task = load_task(EVALS_DIR / "single-file-bug")
        report = await run_task(task, _NoopSolver(), trials=3, out_dir=tmp_path)
        assert report.trials == 3
        assert report.successes == 0
        assert report.success_rate == 0.0

    @pytest.mark.asyncio
    async def test_trace_written_per_trial(self, tmp_path: Path) -> None:
        task = load_task(EVALS_DIR / "single-file-bug")
        await run_trial(task, _FixerSolver(), 1, out_dir=tmp_path)
        trace_file = tmp_path / "single-file-bug.trial1.jsonl"
        assert trace_file.exists()
        rows = read_trace(trace_file)
        # 至少有 task_start / solver 事件 / task_end。
        assert any(r["event_type"] == "task_start" for r in rows)
        assert any(r["event_type"] == "task_end" for r in rows)
