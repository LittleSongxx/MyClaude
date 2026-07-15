"""Eval task specification: a small fixture repo + a deterministic scoring contract.

报告 B1：准备 8～12 个小型 fixture repository，每个任务用独立临时目录隔离运行，
结束后用确定性 oracle 评分。这里定义任务的**声明式规格**（YAML），把"要 Agent 做
什么"和"如何客观判定成功"分开：

- prompt：交给 Agent 的自然语言任务。
- test_target：oracle 要跑的 pytest 目标（空=全部）。
- diff_whitelist：允许改动的路径前缀；越界即失败。
- protected：必须保持不变的文件（防止改测试作弊）。

规格是纯数据，可独立于任何模型加载和校验。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

TASK_SPEC_FILENAME = "task.yaml"
# fixture 仓库内容放在任务目录的这个子目录下，与 task.yaml 分开。
REPO_SUBDIR = "repo"


@dataclass
class EvalTask:
    """一个 eval 任务的声明式规格 + 其 fixture 仓库路径。"""

    task_id: str
    prompt: str
    repo_path: Path
    description: str = ""
    test_target: str = ""
    diff_whitelist: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    # 期望模型"识别出不应改代码、只解释原因"的任务：无 diff 才算成功。
    expect_no_changes: bool = False

    @property
    def conflict_search_paths(self) -> list[str]:
        """扫描冲突标记的范围：默认用 diff 白名单，退化为整仓不现实。"""
        return self.diff_whitelist or []


def load_task(task_dir: Path) -> EvalTask:
    """从任务目录加载 ``task.yaml`` + 定位 ``repo/`` 子目录。"""
    task_dir = Path(task_dir)
    spec_path = task_dir / TASK_SPEC_FILENAME
    if not spec_path.exists():
        raise FileNotFoundError(f"missing {TASK_SPEC_FILENAME} in {task_dir}")
    raw: dict[str, Any] = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}

    task_id = raw.get("id") or task_dir.name
    prompt = raw.get("prompt", "")
    if not prompt and not raw.get("expect_no_changes"):
        raise ValueError(f"task {task_id}: 'prompt' is required")

    repo_path = task_dir / REPO_SUBDIR
    if not repo_path.is_dir():
        raise FileNotFoundError(f"task {task_id}: missing repo/ dir at {repo_path}")

    return EvalTask(
        task_id=task_id,
        prompt=prompt,
        repo_path=repo_path,
        description=raw.get("description", ""),
        test_target=raw.get("test_target", ""),
        diff_whitelist=list(raw.get("diff_whitelist", [])),
        protected=list(raw.get("protected", [])),
        expect_no_changes=bool(raw.get("expect_no_changes", False)),
    )


def discover_tasks(evals_dir: Path) -> list[EvalTask]:
    """扫描 evals 目录下所有包含 task.yaml 的子目录，按 task_id 排序返回。"""
    evals_dir = Path(evals_dir)
    tasks: list[EvalTask] = []
    for spec in sorted(evals_dir.glob(f"*/{TASK_SPEC_FILENAME}")):
        tasks.append(load_task(spec.parent))
    return tasks
