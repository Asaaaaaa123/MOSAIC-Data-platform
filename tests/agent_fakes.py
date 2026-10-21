"""A scripted Claude Messages API for agent tests.

The real ``anthropic`` SDK (1.x, built on ``httpx2``) talks to an
``httpx2.MockTransport`` that replays server-sent-event streams shaped like the
live API's, so unknown kwargs, request-shape mistakes and stream-parsing
behaviour surface exactly as they would in production.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Union

import anthropic
import httpx2

MODEL = "claude-opus-5-5"

Scripted = Union[bytes, httpx2.Response, Callable[[dict[str, Any]], Union[bytes, httpx2.Response]]]


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _pieces(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _block_events(index: int, block: dict[str, Any]) -> list[str]:
    kind = block["type"]
    deltas: list[dict[str, Any]] = []
    if kind == "text":
        start: dict[str, Any] = {"type": "text", "text": ""}
        deltas = [{"type": "text_delta", "text": p} for p in _pieces(block["text"], 7)]
    elif kind == "thinking":
        start = {"type": "thinking", "thinking": "", "signature": ""}
        if block.get("thinking"):
            deltas = [{"type": "thinking_delta", "thinking": p} for p in _pieces(block["thinking"], 9)]
        deltas.append({"type": "signature_delta", "signature": block["signature"]})
    elif kind in ("tool_use", "server_tool_use"):
        start = {k: v for k, v in block.items() if k not in ("input", "partial_json")}
        start["input"] = {}
        raw = block["partial_json"] if "partial_json" in block else json.dumps(block.get("input", {}))
        deltas = [{"type": "input_json_delta", "partial_json": p} for p in _pieces(raw, 11)]
    else:  # server-tool results, tool-search results, fallback markers: complete in content_block_start
        start = block
    events = [_sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": start})]
    events += [
        _sse("content_block_delta", {"type": "content_block_delta", "index": index, "delta": d}) for d in deltas
    ]
    events.append(_sse("content_block_stop", {"type": "content_block_stop", "index": index}))
    return events


def stream(
    *blocks: dict[str, Any],
    stop_reason: str = "end_turn",
    stop_details: dict[str, Any] | None = None,
    model: str = MODEL,
    usage: dict[str, Any] | None = None,
    message_id: str = "msg_test",
) -> bytes:
    """An SSE body for one streamed message with the given final content blocks."""
    counts = {
        "input_tokens": 1200,
        "output_tokens": 80,
        "cache_creation_input_tokens": 900,
        "cache_read_input_tokens": 300,
        **(usage or {}),
    }
    start_usage = {k: counts[k] for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
    events = [
        _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {**start_usage, "output_tokens": 1},
                },
            },
        )
    ]
    for index, block in enumerate(blocks):
        events += _block_events(index, block)
    delta: dict[str, Any] = {"stop_reason": stop_reason, "stop_sequence": None}
    if stop_details is not None:
        delta["stop_details"] = stop_details
    end_usage = {"output_tokens": counts["output_tokens"]}
    if "iterations" in counts:
        end_usage["iterations"] = counts["iterations"]
    events.append(_sse("message_delta", {"type": "message_delta", "delta": delta, "usage": end_usage}))
    events.append(_sse("message_stop", {"type": "message_stop"}))
    return "".join(events).encode()


def error_response(status: int, error_type: str, message: str = "error") -> httpx2.Response:
    return httpx2.Response(
        status,
        headers={"request-id": "req_error"},
        json={"type": "error", "error": {"type": error_type, "message": message}},
    )


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def thinking(value: str, signature: str = "sig-abc123") -> dict[str, Any]:
    return {"type": "thinking", "thinking": value, "signature": signature}


def tool_use(call_id: str, name: str, payload: dict[str, Any] | None = None, *, partial_json: str | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_use", "id": call_id, "name": name, "input": payload or {}}
    if partial_json is not None:
        block["partial_json"] = partial_json
    return block


class FakeClaude:
    """Replays scripted responses; records every request body and header set.

    Requests carrying ``output_config.format`` (structured extraction) are served
    from ``extraction`` and recorded in ``extraction_requests``; all others from
    ``responses`` / ``requests``.
    """

    def __init__(self, *responses: Scripted, extraction: tuple[Scripted, ...] = ()):
        self.responses = list(responses)
        self.extraction = list(extraction)
        self.requests: list[dict[str, Any]] = []
        self.extraction_requests: list[dict[str, Any]] = []
        self.headers: list[httpx2.Headers] = []
        self.paths: list[str] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        is_extraction = "format" in (body.get("output_config") or {})
        (self.extraction_requests if is_extraction else self.requests).append(body)
        self.headers.append(request.headers)
        self.paths.append(request.url.path)
        queue = self.extraction if is_extraction else self.responses
        if not queue:
            return error_response(400, "invalid_request_error", "test script exhausted: unexpected request")
        item = queue.pop(0)
        if callable(item):
            item = item(body)
        if isinstance(item, httpx2.Response):
            return item
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream", "request-id": "req_test"}, content=item
        )

    def client(self, **kwargs: Any) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(
            api_key="test-key",
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self.handler)),
            **kwargs,
        )


async def collect(agent: Any, message: str, session_id: str | None = None) -> list[Any]:
    return [event async for event in agent.run(message, session_id=session_id)]


def of_type(events: list[Any], kind: str) -> list[Any]:
    return [e for e in events if e.type == kind]
