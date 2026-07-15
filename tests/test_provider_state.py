# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
"""ProviderContinuationState 测试（报告 A3）。

重点验证设计核心：canonical 与 opaque 分层的存取、response_id 续传句柄、以及
**版本化安全降级**——未来 schema 版本读回时降级为 None 而非猜测重建。
"""
from __future__ import annotations

from myclaude.provider_state import (
    PROVIDER_STATE_SCHEMA_VERSION,
    ProviderContinuationState,
    ProviderTurnState,
)


class TestRecordAndGet:
    def test_record_and_get_turn(self) -> None:
        state = ProviderContinuationState(provider="openai")
        state.record_turn(
            0,
            response_id="resp_abc",
            opaque_items=[{"type": "reasoning", "id": "rs_1"}],
        )
        turn = state.get_turn(0)
        assert turn is not None
        assert turn.response_id == "resp_abc"
        assert turn.opaque_items == [{"type": "reasoning", "id": "rs_1"}]

    def test_empty_turn_not_recorded(self) -> None:
        """空状态（无 opaque item、无 response_id）不落库，保持结构干净。"""
        state = ProviderContinuationState(provider="openai")
        state.record_turn(0)
        assert state.get_turn(0) is None
        assert state.turns == {}

    def test_missing_turn_returns_none(self) -> None:
        state = ProviderContinuationState(provider="openai")
        assert state.get_turn(5) is None


class TestLatestResponseId:
    def test_returns_most_recent_with_id(self) -> None:
        state = ProviderContinuationState(provider="openai")
        state.record_turn(0, response_id="resp_0")
        state.record_turn(2, response_id="resp_2")
        state.record_turn(1, response_id="resp_1")
        # 最大 turn_index 且带 id 的优先
        assert state.latest_response_id() == "resp_2"

    def test_skips_turns_without_id(self) -> None:
        state = ProviderContinuationState(provider="openai")
        state.record_turn(0, response_id="resp_0")
        state.record_turn(3, opaque_items=[{"type": "reasoning"}])  # 无 response_id
        assert state.latest_response_id() == "resp_0"

    def test_none_when_no_ids(self) -> None:
        state = ProviderContinuationState(provider="openai")
        state.record_turn(0, opaque_items=[{"type": "reasoning"}])
        assert state.latest_response_id() is None


class TestRoundTrip:
    def test_to_dict_from_dict_roundtrip(self) -> None:
        state = ProviderContinuationState(provider="openai")
        state.record_turn(
            0, response_id="resp_0", opaque_items=[{"type": "reasoning", "id": "rs_0"}]
        )
        state.record_turn(1, opaque_items=[{"type": "reasoning", "id": "rs_1"}])

        restored = ProviderContinuationState.from_dict(state.to_dict())
        assert restored is not None
        assert restored.provider == "openai"
        assert restored.get_turn(0).response_id == "resp_0"
        assert restored.get_turn(0).opaque_items == [{"type": "reasoning", "id": "rs_0"}]
        assert restored.get_turn(1).opaque_items == [{"type": "reasoning", "id": "rs_1"}]

    def test_json_roundtrip(self) -> None:
        import json

        state = ProviderContinuationState(provider="openai")
        state.record_turn(0, response_id="resp_0")
        restored = ProviderContinuationState.from_dict(json.loads(state.to_json()))
        assert restored is not None
        assert restored.latest_response_id() == "resp_0"

    def test_turns_serialized_in_order(self) -> None:
        state = ProviderContinuationState(provider="openai")
        state.record_turn(2, response_id="r2")
        state.record_turn(0, response_id="r0")
        indices = [t["turn_index"] for t in state.to_dict()["turns"]]
        assert indices == [0, 2]


class TestVersionDegradation:
    """核心设计点：未知/不兼容版本安全降级，而不是猜测重建。"""

    def test_future_version_degrades_to_none(self) -> None:
        data = {
            "schema_version": PROVIDER_STATE_SCHEMA_VERSION + 1,
            "provider": "openai",
            "turns": [{"turn_index": 0, "response_id": "resp_0", "opaque_items": []}],
        }
        # 未来版本结构未知 —— 返回 None，调用方应退回纯 canonical 重建。
        assert ProviderContinuationState.from_dict(data) is None

    def test_current_version_restores(self) -> None:
        data = {
            "schema_version": PROVIDER_STATE_SCHEMA_VERSION,
            "provider": "openai",
            "turns": [],
        }
        restored = ProviderContinuationState.from_dict(data)
        assert restored is not None
        assert restored.provider == "openai"

    def test_malformed_input_degrades(self) -> None:
        assert ProviderContinuationState.from_dict(None) is None  # type: ignore[arg-type]
        assert ProviderContinuationState.from_dict({}) is None  # 无 version
        assert ProviderContinuationState.from_dict(
            {"schema_version": 1}
        ) is None  # 无 provider

    def test_missing_version_degrades(self) -> None:
        data = {"provider": "openai", "turns": []}
        assert ProviderContinuationState.from_dict(data) is None

    def test_malformed_turns_skipped_not_crash(self) -> None:
        data = {
            "schema_version": PROVIDER_STATE_SCHEMA_VERSION,
            "provider": "openai",
            "turns": [
                {"turn_index": 0, "response_id": "ok"},
                "not-a-dict",
                {"no_turn_index": True},
            ],
        }
        restored = ProviderContinuationState.from_dict(data)
        assert restored is not None
        assert restored.get_turn(0).response_id == "ok"
        assert len(restored.turns) == 1
