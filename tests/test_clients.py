from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from myclaude.client import AnthropicClient, OpenAIClient, OpenAICompatClient
from myclaude.config import ProviderConfig
from myclaude.conversation import ConversationManager, ThinkingBlock
from myclaude.tools.base import StreamEnd, ToolCallComplete, ToolCallStart


class AsyncEvents:
    def __init__(self, events: list[Any]) -> None:
        self.events = events

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self.events:
            yield event


def _config(protocol: str) -> ProviderConfig:
    return ProviderConfig(
        name="test",
        protocol=protocol,
        base_url="https://example.invalid",
        model="test-model",
        api_key="test-key",
        max_output_tokens=321,
    )


@pytest.mark.asyncio
async def test_anthropic_46_uses_adaptive_thinking_payload() -> None:
    class MessageStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def __aiter__(self):
            return AsyncEvents([]).__aiter__()

        async def get_final_message(self):
            return SimpleNamespace(
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    class Messages:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] = {}

        def stream(self, **kwargs: Any) -> MessageStream:
            self.kwargs = kwargs
            return MessageStream()

    config = _config("anthropic")
    config.model = "claude-sonnet-4-6"
    config.thinking = True
    config.input_cost_per_million = 2.0
    config.output_cost_per_million = 8.0
    messages = Messages()
    client = AnthropicClient(config)
    client._client = SimpleNamespace(messages=messages)
    conversation = ConversationManager()
    conversation.add_user_message("think")

    list_events = [event async for event in client.stream(conversation)]

    assert list_events[-1].stop_reason == "end_turn"
    assert messages.kwargs["thinking"] == {"type": "adaptive"}
    usage = client.usage_ledger.snapshot()
    assert usage.request_count == 1
    assert (usage.input_tokens, usage.output_tokens) == (1, 1)
    assert usage.by_purpose == {"agent": 1}
    assert usage.estimated_cost_usd == pytest.approx(0.00001)


@pytest.mark.asyncio
async def test_responses_client_keeps_parallel_tool_calls_separate() -> None:
    items = [
        SimpleNamespace(
            type="response.output_item.added",
            item=SimpleNamespace(
                type="function_call", id="item-1", name="One", call_id="call-1"
            ),
            output_index=0,
        ),
        SimpleNamespace(
            type="response.output_item.added",
            item=SimpleNamespace(
                type="function_call", id="item-2", name="Two", call_id="call-2"
            ),
            output_index=1,
        ),
        SimpleNamespace(
            type="response.function_call_arguments.delta",
            item_id="item-1",
            delta='{"value":',
        ),
        SimpleNamespace(
            type="response.function_call_arguments.delta",
            item_id="item-2",
            delta='{"name":',
        ),
        SimpleNamespace(
            type="response.function_call_arguments.delta",
            item_id="item-1",
            delta="1}",
        ),
        SimpleNamespace(
            type="response.function_call_arguments.delta",
            item_id="item-2",
            delta='"mew"}',
        ),
        SimpleNamespace(
            type="response.function_call_arguments.done", item_id="item-1"
        ),
        SimpleNamespace(
            type="response.function_call_arguments.done", item_id="item-2"
        ),
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=10,
                    output_tokens=4,
                    input_tokens_details=SimpleNamespace(cached_tokens=2),
                )
            ),
        ),
    ]

    class Responses:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] = {}

        async def create(self, **kwargs: Any) -> AsyncEvents:
            self.kwargs = kwargs
            return AsyncEvents(items)

    responses = Responses()
    client = OpenAIClient(_config("openai"))
    client._client = SimpleNamespace(responses=responses)
    conversation = ConversationManager()
    conversation.add_user_message("run both")

    events = [event async for event in client.stream(conversation)]

    starts = [event for event in events if isinstance(event, ToolCallStart)]
    calls = [event for event in events if isinstance(event, ToolCallComplete)]
    end = next(event for event in events if isinstance(event, StreamEnd))
    assert [(event.tool_id, event.tool_name) for event in starts] == [
        ("call-1", "One"),
        ("call-2", "Two"),
    ]
    assert [(event.tool_id, event.arguments) for event in calls] == [
        ("call-1", {"value": 1}),
        ("call-2", {"name": "mew"}),
    ]
    assert end.stop_reason == "tool_use"
    assert (end.input_tokens, end.cache_read, end.output_tokens) == (8, 2, 4)
    assert responses.kwargs["max_output_tokens"] == 321


@pytest.mark.asyncio
async def test_responses_client_captures_and_replays_opaque_reasoning() -> None:
    reasoning_item = SimpleNamespace(
        type="reasoning",
        id="rs-1",
        encrypted_content="opaque-token",
        summary=[
            SimpleNamespace(type="summary_text", text="provider summary")
        ],
    )
    usage = SimpleNamespace(
        input_tokens=4,
        output_tokens=2,
        input_tokens_details=SimpleNamespace(cached_tokens=0),
    )
    batches = [
        [
            SimpleNamespace(
                type="response.output_item.added",
                item=reasoning_item,
                output_index=0,
            ),
            SimpleNamespace(
                type="response.output_item.done",
                item=reasoning_item,
                output_index=0,
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(id="resp-1", usage=usage),
            ),
        ],
        [
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(id="resp-2", usage=usage),
            ),
        ],
    ]

    class Responses:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def create(self, **kwargs: Any) -> AsyncEvents:
            batch = batches[len(self.calls)]
            self.calls.append(kwargs)
            return AsyncEvents(batch)

    responses = Responses()
    client = OpenAIClient(_config("openai"))
    client._client = SimpleNamespace(responses=responses)
    conversation = ConversationManager()
    conversation.add_user_message("think")

    await _collect(client.stream(conversation))

    assert conversation.provider_state is not None
    first_turn = conversation.provider_state.get_turn(0)
    assert first_turn is not None
    assert first_turn.response_id == "resp-1"
    assert first_turn.opaque_items == [{
        "type": "reasoning",
        "id": "rs-1",
        "encrypted_content": "opaque-token",
        "summary": [{
            "type": "summary_text",
            "text": "provider summary",
        }],
    }]

    conversation.add_assistant_message(
        "answer",
        thinking_blocks=[ThinkingBlock("fallback", "fallback-id")],
    )
    conversation.add_user_message("continue")
    await _collect(client.stream(conversation))

    second_input = responses.calls[1]["input"]
    assert second_input[1] == first_turn.opaque_items[0]
    assert all(item.get("id") != "fallback-id" for item in second_input)
    assert responses.calls[0]["include"] == ["reasoning.encrypted_content"]


@pytest.mark.asyncio
async def test_compat_client_marks_malformed_tool_json_and_always_terminates() -> None:
    first_delta = SimpleNamespace(
        content=None,
        reasoning_content=None,
        tool_calls=[
            SimpleNamespace(
                index=0,
                id="call-1",
                function=SimpleNamespace(name="Broken", arguments='{"value":'),
            )
        ],
    )
    final_delta = SimpleNamespace(
        content=None,
        reasoning_content=None,
        tool_calls=None,
    )
    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=first_delta, finish_reason=None)],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(delta=final_delta, finish_reason="tool_calls")],
            usage=None,
        ),
    ]

    class Completions:
        async def create(self, **kwargs: Any) -> AsyncEvents:
            return AsyncEvents(chunks)

    client = OpenAICompatClient(_config("openai-compat"))
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())
    )
    conversation = ConversationManager()
    conversation.add_user_message("call it")

    events = [event async for event in client.stream(conversation)]

    call = next(event for event in events if isinstance(event, ToolCallComplete))
    assert call.arguments == {}
    assert call.parse_error
    end = events[-1]
    assert isinstance(end, StreamEnd)
    assert end.stop_reason == "tool_use"


async def _collect(stream):
    return [event async for event in stream]
