from __future__ import annotations

import time

import pytest

from myclaude.context.manager import (
    RECOVERY_FILE_LIMIT,
    RECOVERY_FULL_BUDGET_WINDOW,
    RECOVERY_SKILLS_BUDGET,
    RECOVERY_TOKENS_PER_FILE,
    RECOVERY_TOKENS_PER_SKILL,
    RecoveryState,
    _RECOVERY_CHARS_PER_TOKEN,
    build_recovery_attachment,
    compute_recovery_budget,
)

def test_recovery_attachment_empty_when_nothing_recorded():
    assert build_recovery_attachment(None, None) == ""
    assert build_recovery_attachment(RecoveryState(), None) == ""

def test_recovery_attachment_emits_all_sections():
    state = RecoveryState()
    state.record_file_read("/tmp/a.py", "print('hi')\n")
    state.record_skill_invocation("planner", "step 1\nstep 2\n")
    schemas = [
        {"name": "ReadFile", "description": "Read a file and return contents.\nWith line numbers."},
        {"name": "Bash", "description": ""},
    ]
    out = build_recovery_attachment(state, schemas)
    assert "/tmp/a.py" in out
    assert "planner" in out
    assert "- ReadFile — Read a file and return contents." in out
    assert "- Bash" in out
    assert "提示" in out  # 结尾提示部分的标题

def test_recovery_file_limit_and_order():
    state = RecoveryState()
    # 记录 7 个时间分散的文件；只有最新的 5 个应当出现。
    for i in range(7):
        state.record_file_read(f"/f{i}", "x")
        # 强制设置时间戳，使顺序确定
        rec = state._files[f"/f{i}"]
        rec.timestamp = 1000.0 + i

    files = state.snapshot_files(RECOVERY_FILE_LIMIT)
    assert len(files) == 5
    assert files[0].path == "/f6"  # 最新的排在最前
    assert files[-1].path == "/f2"

def test_recovery_truncates_per_file():
    huge = "x" * int(RECOVERY_TOKENS_PER_FILE * _RECOVERY_CHARS_PER_TOKEN * 3)
    state = RecoveryState()
    state.record_file_read("/big", huge)
    out = build_recovery_attachment(state, None)
    assert "内容已截断" in out

def test_recovery_skills_budget():
    state = RecoveryState()
    body = "y" * int(RECOVERY_TOKENS_PER_SKILL * _RECOVERY_CHARS_PER_TOKEN)
    for i in range(6):
        name = f"skill-{i}"
        state.record_skill_invocation(name, body)
        rec = state._skills[name]
        rec.timestamp = 1000.0 + i

    out = build_recovery_attachment(state, None)
    emitted = out.count("### skill-")
    # 25K / 每个 skill 5K ⇒ 最多 5 个
    assert 1 <= emitted <= 5


# ---------------------------------------------------------------------------
# B5：按 context_window 缩放恢复预算
# ---------------------------------------------------------------------------

def test_recovery_budget_large_window_uses_full_constants():
    """大窗口（>= 阈值）保持既有慷慨预算不变。"""
    budget = compute_recovery_budget(200_000)
    assert budget.file_limit == RECOVERY_FILE_LIMIT
    assert budget.tokens_per_file == RECOVERY_TOKENS_PER_FILE
    assert budget.skills_budget == RECOVERY_SKILLS_BUDGET
    assert budget.tokens_per_skill == RECOVERY_TOKENS_PER_SKILL


def test_recovery_budget_unknown_window_uses_full_constants():
    """未知窗口（<= 0）退化为固定预算，不做缩放。"""
    budget = compute_recovery_budget(0)
    assert budget.tokens_per_file == RECOVERY_TOKENS_PER_FILE


def test_recovery_budget_small_window_scales_down():
    """8K 本地模型：整个附件被限制在窗口的一小部分，避免附件本身撑爆窗口。"""
    window = 8_000
    budget = compute_recovery_budget(window)
    # files + skills 的总额度必须显著小于窗口——旧固定预算（~50K）会直接溢出 8K。
    total = budget.tokens_per_file * budget.file_limit + budget.skills_budget
    assert total < window
    # 仍保留有意义的最小额度，不缩到 0。
    assert budget.file_limit >= 1
    assert budget.tokens_per_file >= 500
    assert budget.skills_budget >= 500


def test_recovery_budget_boundary_at_threshold():
    """恰好等于阈值的窗口走「大窗口」分支（>= 判定）。"""
    budget = compute_recovery_budget(RECOVERY_FULL_BUDGET_WINDOW)
    assert budget.tokens_per_file == RECOVERY_TOKENS_PER_FILE


def test_small_window_attachment_fits_budget():
    """端到端：小窗口下真实附件的 token 估算不超过缩放后的总预算。"""
    window = 8_000
    budget = compute_recovery_budget(window)
    state = RecoveryState()
    # 记录远超预算的大文件，验证会被截断到预算内。
    state.record_file_read("/big.py", "x" * 100_000)
    out = build_recovery_attachment(state, None, budget=budget)
    approx_tokens = len(out) / _RECOVERY_CHARS_PER_TOKEN
    # 附件整体（含标题/提示等固定开销）应落在窗口量级以内，不再是旧的 ~50K。
    assert approx_tokens < window
