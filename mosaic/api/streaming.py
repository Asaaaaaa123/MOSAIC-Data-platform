"""Server-Sent Events plumbing for ``POST /api/chat``.

The agent's event iterator runs in its own task and feeds a bounded queue; the
response generator drains the queue, emits ``: keep-alive`` comments while the
agent is busy (long tool calls, extended thinking), and cancels the agent when
the client goes away.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, AsyncIterator, Awaitable, Callable

log = logging.getLogger("mosaic.api.sse")

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",  # nginx: do not buffer the stream
    "Connection": "keep-alive",
}
HEARTBEAT = ": keep-alive\n\n"

_END = object()
# Strong references to producer tasks that were cancelled but have not finished
# unwinding yet (asyncio only keeps weak references to tasks).
_UNWINDING: set[asyncio.Task[Any]] = set()


def format_sse(event: str, data: dict[str, Any]) -> str:
    """One SSE frame in the same framing as ``AgentEvent.to_sse()``."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def event_to_sse(event: Any) -> str:
    to_sse = getattr(event, "to_sse", None)
    if callable(to_sse):
        return to_sse()
    if isinstance(event, dict):
        return format_sse(str(event.get("type", "message")), event)
    raise TypeError(f"agent yielded {type(event).__name__}, expected an AgentEvent")


async def _aclose(iterator: Any) -> None:
    closer = getattr(iterator, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception:  # noqa: BLE001 - best effort
        log.debug("error closing agent iterator", exc_info=True)


async def sse_stream(
    start: Callable[[], Any],
    *,
    is_disconnected: Callable[[], Awaitable[bool]],
    heartbeat: float = 15.0,
    session_id: str | None = None,
    queue_size: int = 512,
) -> AsyncIterator[str]:
    """Yield SSE frames for the agent run produced by ``start()``.

    ``start`` returns an async iterator of agent events (or an awaitable of one).
    If the run raises or ends without a ``done`` event, a synthetic ``error`` and
    ``done`` pair is emitted so clients always see a terminal event.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_size)
    state: dict[str, Any] = {"session_id": session_id or "", "done": False}

    async def produce() -> None:
        iterator: Any = None
        failure: Exception | None = None
        try:
            iterator = start()
            if inspect.isawaitable(iterator):
                iterator = await iterator
            async for event in iterator:
                frame = event_to_sse(event)
                sid = getattr(event, "session_id", None) or (event.get("session_id") if isinstance(event, dict) else None)
                if sid:
                    state["session_id"] = sid
                kind = getattr(event, "type", None) or (event.get("type") if isinstance(event, dict) else None)
                if kind == "done":
                    state["done"] = True
                await queue.put(frame)
        except Exception as exc:  # noqa: BLE001 - surfaced to the client as an error event
            failure = exc
            log.exception("agent run failed")
        finally:
            await _aclose(iterator)
        if failure is not None:
            await queue.put(
                format_sse(
                    "error",
                    {
                        "session_id": state["session_id"],
                        "type": "error",
                        "message": f"agent failed: {type(failure).__name__}: {failure}"[:2000],
                        "retryable": False,
                    },
                )
            )
        if not state["done"]:
            await queue.put(
                format_sse(
                    "done",
                    {
                        "session_id": state["session_id"],
                        "type": "done",
                        "final_text": "",
                        "stop_reason": "error" if failure is not None else None,
                        "usage": {},
                    },
                )
            )
        await queue.put(_END)

    producer = asyncio.create_task(produce(), name="mosaic-agent-run")
    getter: asyncio.Future[Any] | None = None
    try:
        while True:
            if getter is None:
                getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({getter}, timeout=heartbeat)
            if not done:
                if await is_disconnected():
                    log.info("chat client disconnected; cancelling agent run")
                    break
                yield HEARTBEAT
                continue
            item = getter.result()
            getter = None
            if item is _END:
                break
            yield item
    finally:
        if getter is not None and not getter.done():
            getter.cancel()
        if not producer.done():
            producer.cancel()
            _UNWINDING.add(producer)
            producer.add_done_callback(_UNWINDING.discard)
