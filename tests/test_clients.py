from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from mewcode.client import OpenAIClient, OpenAICompatClient
from mewcode.config import ProviderConfig
from mewcode.conversation import ConversationManager
from mewcode.tools.base import StreamEnd, ToolCallComplete, ToolCallStart


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
