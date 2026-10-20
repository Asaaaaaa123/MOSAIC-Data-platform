"""Events streamed from the Claude agent to the API / UI (as Server-Sent Events).

This module is the contract between ``mosaic.agent`` (producer) and
``mosaic.api`` / ``mosaic/web`` (consumers). Every event has a ``type`` field;
the SSE ``event:`` name equals ``type``.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Literal, Protocol, Union

from pydantic import BaseModel, Field


class _Event(BaseModel):
    session_id: str

    def to_sse(self) -> str:
        return f"event: {self.type}\ndata: {json.dumps(self.model_dump(), ensure_ascii=False, default=str)}\n\n"  # type: ignore[attr-defined]


class SessionStarted(_Event):
    type: Literal["session_started"] = "session_started"
    model: str
    title: str | None = None


class TextDelta(_Event):
    """A chunk of Claude's visible answer (markdown)."""

    type: Literal["text_delta"] = "text_delta"
    text: str


class ThinkingDelta(_Event):
    """A chunk of Claude's summarized reasoning / between-tool progress note."""

    type: Literal["thinking_delta"] = "thinking_delta"
    text: str


class ToolCallStarted(_Event):
    """Claude asked Mosaic to run a connector or meta tool."""

    type: Literal["tool_call_started"] = "tool_call_started"
    call_id: str
    name: str  # qualified tool name, e.g. "pubmed__search" or "federated_search"
    source: str | None = None  # connector name when it is a connector tool
    input: dict[str, Any] = Field(default_factory=dict)


class ToolCallFinished(_Event):
    type: Literal["tool_call_finished"] = "tool_call_finished"
    call_id: str
    name: str
    ok: bool
    summary: str  # one line for the UI, e.g. "pubmed: 10 of 5,321 records"
    n_records: int | None = None
    total: int | None = None
    latency_ms: float | None = None
    error: str | None = None
    result_id: str | None = None  # session-scoped handle ("r3") Claude can pass to extraction/save tools
    preview: list[dict[str, Any]] = Field(default_factory=list)  # first few compact records


class ServerToolActivity(_Event):
    """Anthropic-hosted tool activity (web_search, web_fetch, tool_search)."""

    type: Literal["server_tool"] = "server_tool"
    name: str
    phase: Literal["call", "result"]
    detail: str | None = None


class TableSaved(_Event):
    """A cross-database extraction table was written to disk."""

    type: Literal["table_saved"] = "table_saved"
    export_id: str
    name: str
    n_rows: int
    columns: list[str]
    csv_url: str
    json_url: str


class TurnCompleted(_Event):
    """One Claude API response finished (an agent run may contain several)."""

    type: Literal["turn_completed"] = "turn_completed"
    stop_reason: str | None
    usage: dict[str, int] = Field(default_factory=dict)


class AgentError(_Event):
    type: Literal["error"] = "error"
    message: str
    retryable: bool = False


class Done(_Event):
    """The agent run is finished; ``final_text`` is the full answer."""

    type: Literal["done"] = "done"
    final_text: str
    stop_reason: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)


AgentEvent = Union[
    SessionStarted,
    TextDelta,
    ThinkingDelta,
    ToolCallStarted,
    ToolCallFinished,
    ServerToolActivity,
    TableSaved,
    TurnCompleted,
    AgentError,
    Done,
]


class AgentLike(Protocol):
    """What the API layer needs from an agent implementation."""

    def run(self, user_message: str, *, session_id: str | None = None) -> AsyncIterator[AgentEvent]:
        """Run Claude on ``user_message`` (continuing ``session_id`` if given).

        Always yields ``SessionStarted`` first and ``Done`` (or ``AgentError``
        then ``Done``) last.
        """
        ...

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        """``[{"session_id", "title", "created_at", "updated_at", "turns"}]``, newest first."""
        ...

    def get_transcript(self, session_id: str) -> list[dict[str, Any]]:
        """Display transcript: ``[{"role": "user"|"assistant", "text": str, "tool_calls": [...]}]``."""
        ...

    def delete_session(self, session_id: str) -> bool:
        ...
