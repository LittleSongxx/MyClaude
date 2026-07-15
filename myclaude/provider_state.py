# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
"""Provider continuation state：把「给人看的对话」与「Provider 要求回传的不透明状态」分层。

报告 A3。不同 Provider 的会话状态不是通用聊天消息：

- Anthropic Messages、OpenAI Responses、Chat Completions 在 reasoning、tool call、
  continuation 上有真实协议差异。
- OpenAI Responses 的 reasoning 是带 ``id`` 的 typed item（可能还带 ``encrypted_content``），
  多轮 reasoning + tool calling 时 Provider 可能要求把**原样**的 reasoning item 回传，
  而不是把它降级成 summary 文本再伪造一个回去。

当前实现（``serialization.build_openai_input``）正是"用 summary 文本 + id 伪造 reasoning
item"。这在默认配置下不出问题（默认根本没开 reasoning summary，什么都不回传），但一旦
将来开启 reasoning 参数，伪造的 item 就可能被 Provider 拒绝。

这个模块提供一个**版本化的状态容器**，把设计意图固化下来：

- canonical conversation（``ConversationManager``）继续保存面向 UI / 压缩 / 跨 Provider
  展示的通用消息——这部分不变。
- ``ProviderContinuationState`` 单独、原样保存 Provider 的 opaque typed items，不翻译成
  通用文本；需要 continuation 时优先回传它，或使用 ``previous_response_id``。
- 持久化带 ``schema_version``：遇到未知版本安全降级（丢弃 opaque 状态、退回纯 canonical
  重建），而不是猜测性地重建可能已不兼容的结构。

它是一个可独立测试的领域对象，不依赖具体 client；wiring 到 serialization 时只需在 assistant
turn 优先取 ``opaque_items``、缺失时退回现有的 summary 重建路径（见模块末尾 wiring 说明）。
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


# ---------------------------------------------------------------------------
# Wiring 说明（供后续接入 serialization / client，不在本模块内改动全局行为）
# ---------------------------------------------------------------------------
#
# 接入点在 assistant turn 的 provider input 构建（serialization.build_openai_input）：
#
#   turn_state = provider_state.get_turn(turn_index)
#   if turn_state and turn_state.opaque_items:
#       result.extend(turn_state.opaque_items)          # 原样回传，不伪造
#   else:
#       ... 现有的 summary-文本重建路径（向后兼容）...
#
# 在线会话则优先用 provider_state.latest_response_id() 走 previous_response_id，
# 避免回传大段 opaque 状态。client.stream 在收到 Responses typed items 时，用
# provider_state.record_turn(turn_index, response_id=..., opaque_items=[原样 item])
# 记录。持久化时存 to_dict()，读回时用 from_dict() —— 未知版本自动降级到纯 canonical。
#
# 之所以先落地为独立、版本化、可测试的领域对象而不直接改写 serialization：当前默认配置
# 不开 reasoning summary，改写主链路对 demo 无实际收益却有回归风险；而这个对象已经把
# "canonical 与 opaque 分层 + 版本化安全降级"的设计固化并可在面试中讲清楚。
