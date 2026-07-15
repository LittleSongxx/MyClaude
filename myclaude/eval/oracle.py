"""Deterministic outcome oracles for coding-agent eval.

报告 B1：可靠评测必须用**确定性 oracle**，而不是"回答看起来对不对"。对 Coding
Agent 而言，正确性来自可复现的客观信号：

- 目标测试是否通过（pytest 退出码）。
- git diff 是否只落在允许路径（diff 白名单）。
- 受保护文件是否保持不变（防止 Agent 改测试 / 改配置作弊）。
- 是否留下意外产物或未完成冲突标记。

这些函数都不依赖 LLM，可在 CI 里稳定复现，也可单独测试。
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class OracleResult:
    """单个 oracle 的判定结果。"""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class ScoreCard:
    """一个任务一次 trial 的综合评分。"""

    task_id: str
    trial: int
    results: list[OracleResult] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """全部 oracle 通过才算任务成功——木桶取最短板，避免"测试过了但改了保护文件"。"""
        return bool(self.results) and all(r.passed for r in self.results)

    def add(self, result: OracleResult) -> None:
        self.results.append(result)

    def summary(self) -> str:
        lines = [f"[{'PASS' if self.success else 'FAIL'}] {self.task_id} trial={self.trial}"]
        for r in self.results:
            lines.append(f"  {'✓' if r.passed else '✗'} {r.name}: {r.detail}")
        return "\n".join(lines)


def run_pytest(work_dir: Path, target: str = "", timeout: float = 120.0) -> OracleResult:
    """在 work_dir 跑 pytest，退出码 0 视为通过。

    target 为空时跑全部；否则只跑指定节点（如 ``tests/test_x.py::test_y``）。
    捕获超时与找不到 pytest 的情况，转成失败结果而非抛异常——oracle 绝不能因为
    环境问题让整个 eval 崩掉。
    """
    cmd = ["python", "-m", "pytest", "-q"]
    if target:
        cmd.append(target)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return OracleResult("pytest", False, f"timed out after {timeout}s")
    except FileNotFoundError:
        return OracleResult("pytest", False, "python/pytest not found")
    passed = proc.returncode == 0
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()
    detail = tail[-1] if tail else f"exit={proc.returncode}"
    return OracleResult("pytest", passed, detail)


def _git_changed_files(work_dir: Path) -> list[str] | None:
    """返回相对仓库根的已变更文件（含未跟踪）；非 git 仓库返回 None。"""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    files: list[str] = []
    for line in proc.stdout.splitlines():
        # porcelain 格式：XY <path>，重命名为 "R  old -> new"。
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            files.append(path)
    return files


def check_diff_whitelist(work_dir: Path, allowed: list[str]) -> OracleResult:
    """校验所有变更文件都落在 allowed 前缀白名单内。

    allowed 是一组路径前缀（相对仓库根）。任何变更文件不匹配任一前缀即失败——
    这直接对应"是否越界修改了不该动的文件"。
    """
    changed = _git_changed_files(work_dir)
    if changed is None:
        return OracleResult("diff_whitelist", False, "not a git repo / git unavailable")
    offending = [
        f for f in changed
        if not any(f == a or f.startswith(a.rstrip("/") + "/") or f.startswith(a) for a in allowed)
    ]
    if offending:
        return OracleResult(
            "diff_whitelist", False, f"changed outside whitelist: {offending}"
        )
    return OracleResult("diff_whitelist", True, f"{len(changed)} file(s) within whitelist")


def check_protected_unchanged(
    work_dir: Path, protected: list[str], baseline: dict[str, str]
) -> OracleResult:
    """校验受保护文件相对 baseline 未被修改。

    baseline 是 {相对路径: sha256} 的快照（由 runner 在任务开始前采集）。任一受保护
    文件的当前哈希与 baseline 不符（或被删除）即失败——防止 Agent 通过改测试 /
    改断言来"通过"任务。
    """
    import hashlib

    for rel in protected:
        p = work_dir / rel
        expected = baseline.get(rel)
        if expected is None:
            continue
        if not p.exists():
            return OracleResult("protected_unchanged", False, f"protected file deleted: {rel}")
        actual = hashlib.sha256(p.read_bytes()).hexdigest()
        if actual != expected:
            return OracleResult("protected_unchanged", False, f"protected file modified: {rel}")
    return OracleResult("protected_unchanged", True, f"{len(protected)} protected file(s) intact")


def check_no_conflict_markers(work_dir: Path, search_paths: list[str]) -> OracleResult:
    """扫描是否残留合并冲突标记（<<<<<<<, =======, >>>>>>>）。"""
    markers = ("<<<<<<<", ">>>>>>>")
    hits: list[str] = []
    for rel in search_paths:
        p = work_dir / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(m in text for m in markers):
            hits.append(rel)
    if hits:
        return OracleResult("no_conflict_markers", False, f"conflict markers in: {hits}")
    return OracleResult("no_conflict_markers", True, "no conflict markers")


def snapshot_hashes(work_dir: Path, rel_paths: list[str]) -> dict[str, str]:
    """采集给定文件的 sha256 baseline（供 check_protected_unchanged 使用）。"""
    import hashlib

    out: dict[str, str] = {}
    for rel in rel_paths:
        p = work_dir / rel
        if p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out
