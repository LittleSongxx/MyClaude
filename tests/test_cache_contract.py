from __future__ import annotations

from myclaude.cache_contract import CacheContract
from myclaude.conversation import ConversationManager


def _tools(description: str = "Read a file") -> list[dict]:
    return [
        {
            "name": "ReadFile",
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
            },
        }
    ]


def test_cache_contract_detects_stable_conversation_prefix(tmp_path):
    conversation = ConversationManager()
    conversation.add_user_message("Inspect the project")
    contract = CacheContract(tmp_path, "agent", persist=False)

    first = contract.inspect(
        model="claude-test",
        system="stable system",
        tools=_tools(),
        messages=conversation.history,
    )
    assert first.break_reasons == ("cold_start",)
    contract.complete(first, input_tokens=100, cache_creation=100)

    conversation.add_assistant_message("I will inspect it.")
    second = contract.inspect(
        model="claude-test",
        system="stable system",
        tools=_tools(),
        messages=conversation.history,
    )
    assert second.break_reasons == ()
    assert second.expected_reuse

    observation = contract.complete(
        second,
        input_tokens=20,
        cache_read=180,
    )
    assert observation.request_hit_rate == 0.9
    assert observation.cumulative_hit_rate == 0.45


def test_cache_contract_reports_precise_break_reasons(tmp_path):
    conversation = ConversationManager()
    conversation.add_user_message("Inspect")
    contract = CacheContract(tmp_path, "agent", persist=False)
    first = contract.inspect(
        model="model-a",
        system="system-a",
        tools=_tools(),
        messages=conversation.history,
    )
    contract.complete(first, input_tokens=10)

    changed = contract.inspect(
        model="model-b",
        system="system-b",
        tools=_tools("Changed description"),
        messages=conversation.history,
    )
    assert "model_changed" in changed.break_reasons
    assert "system_changed" in changed.break_reasons
    assert any(
        reason.startswith("tool_schema_changed:")
        for reason in changed.break_reasons
    )


def test_cache_contract_persists_last_successful_snapshot(tmp_path):
    conversation = ConversationManager()
    conversation.add_user_message("Inspect")
    first_contract = CacheContract(tmp_path, "session", persist=True)
    inspection = first_contract.inspect(
        model="model",
        system="system",
        tools=_tools(),
        messages=conversation.history,
    )
    first_contract.complete(inspection, input_tokens=50, cache_creation=50)

    restored = CacheContract(tmp_path, "session", persist=True)
    next_inspection = restored.inspect(
        model="model",
        system="system",
        tools=_tools(),
        messages=conversation.history,
    )
    assert next_inspection.expected_reuse
    assert next_inspection.break_reasons == ()

