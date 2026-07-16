"""Provider continuation state for opaque provider-owned response items.

Different providers have genuinely different continuation protocols:

- Anthropic Messages、OpenAI Responses、Chat Completions 在 reasoning、tool call、
  continuation 上有真实协议差异。
- OpenAI Responses 的 reasoning 是带 ``id`` 的 typed item（可能还带 ``encrypted_content``），
  多轮 reasoning + tool calling 时 Provider 可能要求把**原样**的 reasoning item 回传，
  而不是把它降级成 summary 文本再伪造一个回去。

This module provides a versioned state container:

- canonical conversation（``ConversationManager``）继续保存面向 UI / 压缩 / 跨 Provider
  展示的通用消息——这部分不变。
- ``ProviderContinuationState`` 单独、原样保存 Provider 的 opaque typed items，不翻译成
  通用文本；需要 continuation 时优先回传它，或使用 ``previous_response_id``。
- 持久化带 ``schema_version``：遇到未知版本安全降级（丢弃 opaque 状态、退回纯 canonical
  重建），而不是猜测性地重建可能已不兼容的结构。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# schema 演进时递增。持久化的 provider state 带此版本；读回时高于己知版本一律安全降级。
PROVIDER_STATE_SCHEMA_VERSION = 1


@dataclass
class ProviderTurnState:
    """单个 assistant turn 的 Provider 不透明续传状态。

    ``opaque_items`` 是 Provider 原样返回、且要求原样回传的 typed output items
    （如 OpenAI Responses 的 ``reasoning`` item，含 ``id`` / ``encrypted_content``）。
    我们**不**解释其内部结构，只负责原样存取——这正是"不把 output item 降级成文本再伪造"。

    ``response_id`` 是 Provider 侧可用于 continuation 的句柄（如 OpenAI 的
    ``previous_response_id``）。活动在线会话优先用它，避免回传大段 opaque 状态。
    """

    turn_index: int
    response_id: str | None = None
    opaque_items: list[dict[str, Any]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.opaque_items and not self.response_id


@dataclass
class ProviderContinuationState:
    """一次会话里，各 assistant turn 的 Provider 续传状态集合。

    与 canonical conversation 并列、而非混入：canonical 负责展示与压缩，这里负责协议保真。
    """

    provider: str
    schema_version: int = PROVIDER_STATE_SCHEMA_VERSION
    turns: dict[int, ProviderTurnState] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 存取
    # ------------------------------------------------------------------

    def record_turn(
        self,
        turn_index: int,
        *,
        response_id: str | None = None,
        opaque_items: list[dict[str, Any]] | None = None,
    ) -> None:
        """记录一个 assistant turn 的续传状态；空状态不落库，保持结构干净。"""
        state = ProviderTurnState(
            turn_index=turn_index,
            response_id=response_id,
            opaque_items=list(opaque_items or []),
        )
        if state.is_empty():
            return
        self.turns[turn_index] = state

    def get_turn(self, turn_index: int) -> ProviderTurnState | None:
        return self.turns.get(turn_index)

    def latest_response_id(self) -> str | None:
        """最近一个带 response_id 的 turn，用于 ``previous_response_id`` 续传。"""
        for idx in sorted(self.turns, reverse=True):
            rid = self.turns[idx].response_id
            if rid:
                return rid
        return None

    # ------------------------------------------------------------------
    # 版本化持久化 —— 未知版本安全降级，而非猜测重建
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "turns": [
                {
                    "turn_index": t.turn_index,
                    "response_id": t.response_id,
                    "opaque_items": t.opaque_items,
                }
                for t in (self.turns[i] for i in sorted(self.turns))
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProviderContinuationState | None":
        """从持久化 dict 重建；版本高于己知或结构不符时安全降级为 None。

        返回 None 表示"无法可信地恢复 provider 续传状态"——调用方应退回纯 canonical
        conversation 重建，而不是使用可能不兼容的 opaque 结构。
        """
        if not isinstance(data, dict):
            return None
        version = data.get("schema_version")
        if not isinstance(version, int) or version > PROVIDER_STATE_SCHEMA_VERSION:
            # 未来版本：本代码不认识其结构，安全降级。
            return None
        provider = data.get("provider")
        if not isinstance(provider, str):
            return None
        state = cls(provider=provider, schema_version=version)
        raw_turns = data.get("turns")
        if not isinstance(raw_turns, list):
            return state
        for entry in raw_turns:
            if not isinstance(entry, dict):
                continue
            idx = entry.get("turn_index")
            if not isinstance(idx, int):
                continue
            items = entry.get("opaque_items")
            state.record_turn(
                idx,
                response_id=entry.get("response_id"),
                opaque_items=items if isinstance(items, list) else None,
            )
        return state
