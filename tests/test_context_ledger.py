from __future__ import annotations

from myclaude.context.ledger import ContextLedger
from myclaude.context.manager import RecoveryState, build_recovery_attachment


def test_context_ledger_persists_structured_task_state(tmp_path):
    ledger = ContextLedger(tmp_path, "session", persist=True)
    ledger.start_task(
        "Implement cache support. You must keep schemas stable. 不要保存文件正文。"
    )
    ledger.update(
        decisions=["Use a stable dispatcher"],
        acceptance_criteria=["Discovery does not change tool schemas"],
        unresolved=["Confirm provider behavior"],
    )
    ledger.record_reference(str(tmp_path / "src" / "agent.py"))
    ledger.record_modified(str(tmp_path / "src" / "agent.py"), "EditFile")

    restored = ContextLedger(tmp_path, "session", persist=True)
    assert restored.goal.startswith("Implement cache support")
    assert any("schemas stable" in item for item in restored.constraints)
    assert any("不要保存文件正文" in item for item in restored.constraints)
    assert restored.decisions == ["Use a stable dispatcher"]
    assert restored.modified_files == {"src/agent.py": "EditFile"}


def test_context_ledger_updates_are_incremental(tmp_path):
    ledger = ContextLedger(tmp_path, "session", persist=False)
    ledger.start_task("Inspect the runtime")
    first_version = ledger.version
    ledger.update(decisions=["Keep the prefix stable"])
    delta = ledger.render_updates(first_version)

    assert "Keep the prefix stable" in delta
    assert "Goal:" not in delta


def test_steering_preserves_work_state_and_adds_constraints(tmp_path):
    ledger = ContextLedger(tmp_path, "session", persist=False)
    ledger.start_task("Implement the feature")
    ledger.record_modified("src/agent.py", "EditFile")
    ledger.set_verification({"status": "pending", "revision": 1, "evidence": []})

    ledger.apply_steering("Also update tests, but do not change the public API.")

    assert "Steering:" in ledger.goal
    assert ledger.modified_files == {"src/agent.py": "EditFile"}
    assert ledger.verification["revision"] == 1
    assert any("do not change" in item for item in ledger.constraints)


def test_compaction_attachment_uses_pointers_not_stale_file_bodies(tmp_path):
    state = RecoveryState()
    state.record_file_read("/tmp/example.py", "STALE_SECRET_CONTENT")
    ledger = ContextLedger(tmp_path, "session", persist=False)
    ledger.start_task("Continue implementation")
    ledger.record_reference("/tmp/example.py")

    attachment = build_recovery_attachment(
        state,
        [{"name": "ReadFile", "description": "Read a file"}],
        context_ledger=ledger.render_for_prompt(),
        active_skill_names=["planner"],
    )

    assert "/tmp/example.py" in attachment
    assert "STALE_SECRET_CONTENT" not in attachment
    assert "planner" in attachment
    assert "re-read" in attachment.casefold()
