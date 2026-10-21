"""Mosaic's Claude agent: a streaming, tool-using loop over the connector registry.

Why a manual loop rather than the SDK tool runner: the Python runner does not
resume ``pause_turn`` (it ends the loop when no client tool ran, which silently
truncates long web-search / tool-search turns); Mosaic also streams every delta
to the browser as SSE, runs a turn's tool calls concurrently and reports each
one as it finishes, persists the exact history between rounds, and applies the
refusal-fallback echo rules. Those are the per-turn control points the loop
below owns.

One run (``MosaicAgent.run``):

1. ``SessionStarted``; load (or create) the session and its frozen request prefix.
2. Stream a Messages API response (adaptive thinking with summarized display,
   ``output_config.effort``, top-level automatic prompt caching, server-side
   refusal fallback). Text and thinking deltas are forwarded live;
   ``ToolCallStarted`` fires when a ``tool_use`` block completes.
3. Branch on ``stop_reason`` before using the content:
   ``refusal`` - surface it, run nothing, drop the run's unanswered history;
   ``max_tokens`` with a ``tool_use`` - the input is truncated: do not run it,
   answer it with an error result so Claude can retry in smaller pieces;
   ``tool_use`` - run every tool concurrently, return all results in one user
   message; ``pause_turn`` - send the paused turn back as-is (no extra user
   message); anything else ends the run.
4. ``TurnCompleted`` per response, ``Done`` last (after ``AgentError`` on failure).

History is append-only: each assistant turn is stored exactly as returned
(``echo_content`` only applies the documented fallback echo rules), so
preserved thinking blocks and prompt-cache prefixes stay valid.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import anthropic
import httpx2

from ..catalog.catalog import RepositoryCatalog
from ..config import Settings
from ..http import SourceError
from ..models import ToolResult
from ..registry import Registry
from .blocks import (
    client_tool_uses,
    describe_server_call,
    describe_server_result,
    dump_block,
    dump_content,
    echo_content,
    server_result_label,
    server_tool_label,
    strip_thinking,
    unpaired_server_calls,
)
from .claude_api import (
    NOT_CONFIGURED,
    TOOL_RESULT_CHARS,
    add_usage,
    api_error_message,
    effort_setting,
    fallback_kwargs,
    fallbacks_enabled,
    is_missing_credentials,
    is_thinking_binding_error,
    served_by_fallback,
    usage_counts,
)
from .events import (
    AgentError,
    AgentEvent,
    Done,
    ServerToolActivity,
    SessionStarted,
    TableSaved,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnCompleted,
)
from .extraction import StructuredExtractor
from .meta_tools import MetaToolRegistry, ToolContext, ToolInputError, default_meta_tools, record_preview
from .results import ResultHandles
from .sessions import SessionStore, iso
from .toolset import build_prefix, prefix_defers_tools

log = logging.getLogger("mosaic.agent")

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
TOOL_TIMEOUT_S = 180.0  # per connector call; connectors also have their own HTTP timeouts
MAX_PAUSE_CONTINUATIONS = 8
MAX_TRUNCATED_TOOL_TURNS = 1
MAX_INVALID_JSON_RETRIES = 2
_TRUNCATED_STOPS = ("max_tokens", "model_context_window_exceeded")
# Prefix of the ValueError the SDK's stream accumulator raises for unparseable tool JSON
# (anthropic/lib/streaming/_beta_messages.py, accumulate_event).
_UNPARSEABLE_TOOL_JSON = "Unable to parse tool parameter JSON"
_TRUNCATED_INPUT = (
    "Not run: this tool call was cut off because the response reached the output token limit, so its "
    "input is incomplete. Retry with a smaller input: split the work across several calls, or pass a "
    "result_id instead of inline rows."
)


@dataclass
class _RunState:
    session_id: str
    text: list[str] = field(default_factory=list)
    text_gap: bool = False
    thinking_emitted: bool = False
    thinking_gap: bool = False
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)  # started, not yet finished tool calls
    server_calls: dict[str, str] = field(default_factory=dict)  # server_tool_use id -> tool name
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # display transcript
    tables: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str | None = None
    error: str | None = None
    discard_text: bool = False
    finished: bool = False

    @property
    def final_text(self) -> str:
        return "" if self.discard_text else "".join(self.text)


@dataclass
class _Turn:
    message: Any = None
    request_id: str | None = None


@dataclass
class _ToolOutcome:
    ok: bool
    content: str
    summary: str
    latency_ms: float
    result_id: str | None = None
    n_records: int | None = None
    total: int | None = None
    preview: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    events: list[Any] = field(default_factory=list)


def _connector_summary(result: ToolResult) -> str:
    n = len(result.records)
    if n == 0 and result.total is None:
        return f"{result.source}.{result.tool}: ok" + (" (no records)" if result.data is None else "")
    shown = f"{n:,}" if result.total is None or result.total == n else f"{n:,} of {result.total:,}"
    plural = "record" if (result.total if result.total is not None else n) == 1 else "records"
    return f"{result.source}: {shown} {plural}"


def _drop_unanswered_tail(messages: list[dict[str, Any]]) -> bool:
    """Pop trailing assistant messages that leave a server-side tool call without its result.

    Consecutive assistant messages (a paused turn and its continuations) are one turn to
    the API, so pairing is checked across the whole trailing run of them.
    """
    dropped = False
    while messages and messages[-1]["role"] == "assistant":
        tail: list[dict[str, Any]] = []
        for message in reversed(messages):
            if message["role"] != "assistant":
                break
            content = message.get("content")
            tail[:0] = content if isinstance(content, list) else []
        if not unpaired_server_calls(tail):
            break
        messages.pop()
        dropped = True
    return dropped


def _refusal_message(message: Any) -> str:
    details = getattr(message, "stop_details", None)
    category = getattr(details, "category", None)
    explanation = getattr(details, "explanation", None)
    text = "Claude declined to continue this request" + (f" (category: {category})." if category else ".")
    if explanation:
        text += f" {explanation}"
    text += " Any partial answer shown is incomplete and was discarded; no tools from that turn were run."
    if category == "reasoning_extraction":
        text += " Ask for a short explanation or summary rather than the model's internal reasoning."
    else:
        text += " Rephrasing or narrowing the request may help."
    return text


class MosaicAgent:
    """Implements :class:`mosaic.agent.events.AgentLike`."""

    def __init__(
        self,
        registry: Registry,
        settings: Settings | None = None,
        *,
        client: anthropic.AsyncAnthropic | None = None,
        catalog: RepositoryCatalog | None = None,
        sessions: SessionStore | None = None,
        meta_tools: MetaToolRegistry | None = None,
    ):
        self.registry = registry
        self.settings = settings or registry.settings
        self.sessions = sessions or SessionStore.from_settings(self.settings)
        self.meta_tools = meta_tools or default_meta_tools()
        self.model = self.settings.mosaic_model
        self.effort = effort_setting(self.settings.mosaic_effort, "high")
        self.deferred = (self.settings.mosaic_tool_loading or "deferred").strip().lower() != "eager"
        self.web_tools = bool(self.settings.mosaic_enable_web_tools)
        self.fallbacks = fallbacks_enabled(self.settings)
        self.max_turns = max(1, int(self.settings.mosaic_max_agent_turns))
        self._client = client
        self._owns_client = client is None
        self._catalog = catalog
        self._prefix: dict[str, Any] | None = None
        self._results = ResultHandles(self.sessions)
        self._extractor = StructuredExtractor.from_settings(
            self.settings, self._get_client, fallbacks=self.fallbacks, audit=registry.audit
        )
        self._running: set[str] = set()

    # ------------------------------------------------------------------ setup
    @property
    def catalog(self) -> RepositoryCatalog:
        if self._catalog is None:
            self._catalog = RepositoryCatalog.from_connectors(self.registry.connectors.values())
        return self._catalog

    def request_prefix(self) -> dict[str, Any]:
        """System blocks + tools for new sessions (built once per process, deterministic)."""
        if self._prefix is None:
            self._prefix = build_prefix(
                self.registry,
                self.meta_tools,
                catalog_stats=self.catalog.stats(),
                deferred=self.deferred,
                web_tools=self.web_tools,
            )
        return self._prefix

    def _get_client(self) -> anthropic.AsyncAnthropic | None:
        """The shared client, created on first use. ``None`` when no credential is configured."""
        if self._client is None:
            kwargs: dict[str, Any] = {}
            if self.settings.anthropic_api_key:
                kwargs["api_key"] = self.settings.anthropic_api_key
            try:
                client = anthropic.AsyncAnthropic(**kwargs)
            except anthropic.AnthropicError:
                log.warning("could not initialise the Anthropic client", exc_info=True)
                return None
            if not (client.api_key or client.auth_token or client.credentials):
                return None
            self._client = client
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.close()
            self._client = None

    # ------------------------------------------------------------------ AgentLike
    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.sessions.list_sessions(limit)

    def get_transcript(self, session_id: str) -> list[dict[str, Any]]:
        return self.sessions.get_transcript(session_id)

    def delete_session(self, session_id: str) -> bool:
        self._results.forget_session(session_id)
        return self.sessions.delete_session(session_id)

    async def run(self, user_message: str, *, session_id: str | None = None) -> AsyncIterator[AgentEvent]:
        if session_id is not None and not SESSION_ID_RE.fullmatch(session_id):
            fresh = uuid.uuid4().hex
            yield SessionStarted(session_id=fresh, model=self.model)
            yield AgentError(session_id=fresh, message=f"invalid session id {session_id[:40]!r}")
            yield Done(session_id=fresh, final_text="", stop_reason="error")
            return
        sid = session_id or uuid.uuid4().hex
        if not self.sessions.exists(sid):
            self.sessions.create(sid, title=user_message, model=self.model, prefix=self.request_prefix())
        info = self.sessions.info(sid) or {}
        yield SessionStarted(session_id=sid, model=self.model, title=info.get("title"))
        if sid in self._running:
            yield AgentError(
                session_id=sid, message="This session is already answering a message; wait for it to finish.",
                retryable=True,
            )
            yield Done(session_id=sid, final_text="", stop_reason="busy")
            return

        self._running.add(sid)
        state = _RunState(sid)
        self.sessions.append_transcript(
            sid, {"role": "user", "text": user_message, "tool_calls": [], "tables": [], "created_at": iso(time.time())}
        )
        try:
            async for event in self._run(state, user_message):
                yield event
            state.finished = True
        except anthropic.APIError as exc:
            message, retryable = api_error_message(exc, self.model)
            log.warning("Claude API error in session %s: %s", sid, message)
            yield self._fail(state, message, retryable)
        except httpx2.TransportError as exc:
            # A read failure while iterating an open stream surfaces unwrapped.
            log.warning("connection to Claude lost in session %s: %s", sid, exc)
            yield self._fail(state, "Lost the connection to the Claude API mid-response. Send the message again.", True)
        except Exception as exc:  # noqa: BLE001 - the server must survive any agent failure
            if is_missing_credentials(exc):
                yield self._fail(state, NOT_CONFIGURED, False)
            else:
                log.exception("agent run failed in session %s", sid)
                yield self._fail(state, f"Mosaic agent error: {type(exc).__name__}: {exc}"[:1000], False)
        finally:
            self._running.discard(sid)
            self._save_reply(state)
        yield Done(session_id=sid, final_text=state.final_text, stop_reason=state.stop_reason, usage=state.usage)

    # ------------------------------------------------------------------ the loop
    async def _run(self, state: _RunState, user_message: str) -> AsyncIterator[AgentEvent]:
        sid = state.session_id
        client = self._get_client()
        if client is None:
            yield self._fail(state, NOT_CONFIGURED, False)
            state.stop_reason = "not_configured"
            return
        prefix = self.sessions.prefix(sid)
        messages = self.sessions.messages(sid)
        if _drop_unanswered_tail(messages):
            # A paused (or truncated) turn whose server-side tool call never got its result,
            # because the run ended before the continuation: the API rejects a user message
            # after it (400 "... without a corresponding ..._tool_result block").
            log.warning("session %s: dropping a trailing turn with an unanswered server tool call", sid)
            self.sessions.truncate_messages(sid, len(messages))
        start = len(messages)
        user = {"role": "user", "content": [{"type": "text", "text": user_message}]}
        messages.append(user)
        self.sessions.append_messages(sid, [user])
        self.sessions.record_turn(sid)
        ctx = ToolContext(
            registry=self.registry,
            settings=self.settings,
            session_id=sid,
            results=self._results,
            extractor=self._extractor,
            deferred_tools=prefix_defers_tools(prefix),
            catalog_factory=lambda: self.catalog,
        )
        pauses = truncations = 0
        stripped = False

        for _ in range(self.max_turns):
            turn = _Turn()
            invalid_json = 0
            while True:
                mark = len(state.text)
                try:
                    async for event in self._stream(client, self._request(prefix, messages), state, turn):
                        yield event
                    break
                except ValueError as exc:
                    # Eager input streaming: the SDK raises from the stream iterator for tool
                    # JSON it cannot parse at all. The block never completed, so there is no
                    # tool_use_id to answer: re-issue the turn (bounded).
                    if _UNPARSEABLE_TOOL_JSON not in str(exc):
                        raise
                    invalid_json += 1
                    del state.text[mark:]
                    for event in self._finish_unrun(state, "discarded: the turn was retried after malformed tool input"):
                        yield event
                    if invalid_json > MAX_INVALID_JSON_RETRIES:
                        yield self._fail(state, f"Claude produced malformed tool input repeatedly: {exc}"[:500], True)
                        return
                    log.warning("retrying turn after unparseable tool input: %s", exc)
                except anthropic.BadRequestError as exc:
                    if stripped or not is_thinking_binding_error(exc):
                        raise
                    # Documented one-time recovery: the history no longer matches the
                    # prefix the thinking blocks were bound to. Strip them and retry.
                    stripped = True
                    log.warning("session %s: thinking blocks rejected (%s); retrying without them", sid, exc)
                    start = len(strip_thinking(messages[:start]))  # keep the refusal rollback point aligned
                    messages = strip_thinking(messages)
                    self.sessions.replace_messages(sid, messages)

            message = turn.message
            stop = message.stop_reason
            counts = usage_counts(message.usage)
            add_usage(state.usage, counts)
            state.stop_reason = stop

            # Branch on stop_reason before using the content.
            if stop == "refusal":
                # Partial output is discarded, nothing runs, and the run's unanswered messages
                # are dropped so a rephrased request is not merged with the refused one.
                self._audit_turn(sid, message, counts, 0, turn.request_id)
                yield TurnCompleted(session_id=sid, stop_reason=stop, usage=counts)
                self.sessions.truncate_messages(sid, start)
                for event in self._finish_unrun(state, "not run: Claude declined this turn"):
                    yield event
                state.discard_text = True
                yield self._fail(state, _refusal_message(message), False)
                return
            if stop is None:
                # The stream ended without message_delta: the turn is incomplete, so keep
                # it out of history (a cut-off tool_use must never run).
                self._audit_turn(sid, message, counts, 0, turn.request_id)
                for event in self._finish_unrun(state, "not run: the response ended early"):
                    yield event
                yield self._fail(state, "The response from Claude ended unexpectedly. Send the message again.", True)
                return

            content = echo_content(dump_content(message.content))
            tool_uses = client_tool_uses(content)
            self._audit_turn(sid, message, counts, len(tool_uses), turn.request_id)
            yield TurnCompleted(session_id=sid, stop_reason=stop, usage=counts)

            assistant = {"role": "assistant", "content": content}
            results: list[dict[str, Any]] = []
            if tool_uses and stop in _TRUNCATED_STOPS:
                # A truncated tool input parses as a valid partial object; never run it.
                # Answer each call with an error so the history stays valid and Claude can retry.
                results = [
                    {"type": "tool_result", "tool_use_id": b["id"], "content": _TRUNCATED_INPUT, "is_error": True}
                    for b in tool_uses
                ]
                for event in self._finish_unrun(state, "not run: input truncated at the output token limit"):
                    yield event
                messages += [assistant, {"role": "user", "content": results}]
                self.sessions.append_messages(sid, messages[-2:])
                truncations += 1
                if truncations > MAX_TRUNCATED_TOOL_TURNS:
                    yield self._fail(
                        state,
                        "Claude's tool input exceeded the output token limit twice; "
                        "try a narrower request or raise MOSAIC_MAX_TOKENS.",
                        False,
                    )
                    return
                continue

            if tool_uses:
                async for event in self._run_tools(ctx, tool_uses, state, results):
                    yield event
                messages += [assistant, {"role": "user", "content": results}]
                self.sessions.append_messages(sid, messages[-2:])
                continue

            messages.append(assistant)
            self.sessions.append_messages(sid, [assistant])
            for event in self._finish_unrun(state, "discarded: superseded by the fallback model"):
                yield event
            if stop == "pause_turn":
                # A server-side tool loop paused: send the turn back unchanged, no new user message.
                pauses += 1
                if pauses > MAX_PAUSE_CONTINUATIONS:
                    yield self._fail(state, "Claude's web/tool-search work paused too many times; ask again to continue.", True)
                    return
                continue
            return

        yield self._fail(
            state,
            f"Stopped after {self.max_turns} model turns (MOSAIC_MAX_AGENT_TURNS). "
            "Send a follow-up message to continue from here.",
            False,
        )
        state.stop_reason = "max_turns"

    def _request(self, prefix: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.settings.mosaic_max_tokens,
            "system": prefix["system"],
            "tools": prefix["tools"],
            "messages": messages,
            # Opus 5.5 always thinks; "summarized" returns readable reasoning and the
            # progress notes it writes between tool calls.
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
            **fallback_kwargs(self.fallbacks),
        }

    async def _stream(
        self, client: anthropic.AsyncAnthropic, request: dict[str, Any], state: _RunState, turn: _Turn
    ) -> AsyncIterator[AgentEvent]:
        async with client.beta.messages.stream(**request) as stream:
            async for event in stream:
                for out in self._translate(event, state):
                    yield out
            turn.message = await stream.get_final_message()
            turn.request_id = stream.request_id

    def _translate(self, event: Any, state: _RunState) -> list[AgentEvent]:
        sid = state.session_id
        kind = event.type
        if kind == "content_block_start":
            block_type = event.content_block.type
            if block_type != "text" and state.text:
                state.text_gap = True
            if state.thinking_emitted:
                state.thinking_gap = True
            return []
        if kind == "text":
            delta = event.text
            if not delta:
                return []
            if state.text_gap and state.text:
                delta = "\n\n" + delta
            state.text_gap = False
            state.text.append(delta)
            return [TextDelta(session_id=sid, text=delta)]
        if kind == "thinking":
            delta = event.thinking
            if not delta:
                return []
            if state.thinking_gap:
                delta = "\n\n" + delta
            state.thinking_gap = False
            state.thinking_emitted = True
            return [ThinkingDelta(session_id=sid, text=delta)]
        if kind != "content_block_stop":
            return []
        block = dump_block(event.content_block)
        block_type = block.get("type")
        if block_type == "tool_use":
            name = block.get("name") or ""
            raw = block.get("input")
            state.pending[block["id"]] = {"name": name, "input": raw if isinstance(raw, dict) else {}}
            source = name.partition("__")[0] if "__" in name else None
            return [
                ToolCallStarted(
                    session_id=sid, call_id=block["id"], name=name, source=source,
                    input=raw if isinstance(raw, dict) else {},
                )
            ]
        if block_type == "server_tool_use":
            state.server_calls[block.get("id", "")] = block.get("name") or ""
            return [
                ServerToolActivity(
                    session_id=sid, name=server_tool_label(block.get("name")), phase="call",
                    detail=describe_server_call(block),
                )
            ]
        if block_type == "fallback":
            source = (block.get("from") or {}).get("model")
            target = (block.get("to") or {}).get("model")
            log.info("session %s: %s declined; fallback %s continued", sid, source, target)
            return [
                ServerToolActivity(
                    session_id=sid, name="fallback", phase="result", detail=f"{source} declined; {target} continued"
                )
            ]
        if "tool_use_id" in block and str(block_type).endswith("_tool_result"):
            return [
                ServerToolActivity(
                    session_id=sid, name=server_result_label(block, state.server_calls), phase="result",
                    detail=describe_server_result(block),
                )
            ]
        return []

    # ------------------------------------------------------------------ tools
    async def _run_tools(
        self, ctx: ToolContext, tool_uses: list[dict[str, Any]], state: _RunState, results: list[dict[str, Any]]
    ) -> AsyncIterator[AgentEvent]:
        """Run every tool_use concurrently; report each as it finishes; results in request order."""
        tasks = {asyncio.ensure_future(self._execute(ctx, block)): block for block in tool_uses}
        outcomes: dict[str, _ToolOutcome] = {}
        try:
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    block = tasks[task]
                    outcome = task.result()
                    outcomes[block["id"]] = outcome
                    for event in self._finished(state, block, outcome):
                        yield event
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
        for block in tool_uses:
            outcome = outcomes[block["id"]]
            item: dict[str, Any] = {"type": "tool_result", "tool_use_id": block["id"], "content": outcome.content}
            if not outcome.ok:
                item["is_error"] = True
            results.append(item)
        for event in self._finish_unrun(state, "discarded: superseded by the fallback model"):
            yield event

    async def _execute(self, ctx: ToolContext, block: dict[str, Any]) -> _ToolOutcome:
        """Run one tool. Never raises (except cancellation): failures become error outcomes."""
        name = block.get("name") or ""
        raw = block.get("input")
        started = time.perf_counter()

        def elapsed() -> float:
            return (time.perf_counter() - started) * 1000

        try:
            if not isinstance(raw, dict):
                # Same shape the docs use for input that did not parse to an object.
                raise ToolInputError(json.dumps({"INVALID_JSON": json.dumps(raw, default=str)}))
            meta = self.meta_tools.get(name)
            if meta is not None:
                result = await meta.run(ctx, raw)
                payload = result.payload if result.payload is not None else {"text": result.text}
                stored = self._results.add(ctx.session_id, kind=result.kind, tool=name, payload=payload)
                return _ToolOutcome(
                    ok=True,
                    content=f"result_id: {stored.result_id}\n{result.text}",
                    summary=result.summary,
                    latency_ms=elapsed(),
                    result_id=stored.result_id,
                    n_records=result.n_records,
                    total=result.total,
                    preview=result.preview,
                    events=result.events,
                )
            connector, tool_def = self.registry.resolve_qualified(name)
            tool_result = await self.registry.call(
                connector.name, tool_def.name, raw, session_id=ctx.session_id, timeout=TOOL_TIMEOUT_S
            )
            stored = self._results.add(
                ctx.session_id,
                kind="records",
                tool=name,
                payload=tool_result.model_dump(mode="json"),
                source=connector.name,
            )
            return _ToolOutcome(
                ok=True,
                content=f"result_id: {stored.result_id}\n{tool_result.to_llm(max_chars=TOOL_RESULT_CHARS)}",
                summary=_connector_summary(tool_result),
                latency_ms=elapsed(),
                result_id=stored.result_id,
                n_records=len(tool_result.records),
                total=tool_result.total,
                preview=record_preview(tool_result.records),
            )
        except (SourceError, ToolInputError) as exc:
            message = str(exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a tool bug must not end the run
            log.exception("tool %s crashed", name)
            message = f"internal error in {name}: {type(exc).__name__}: {exc}"
        return _ToolOutcome(
            ok=False, content=message, summary=f"{name} failed", latency_ms=elapsed(), error=message[:2000]
        )

    def _finished(self, state: _RunState, block: dict[str, Any], outcome: _ToolOutcome) -> list[AgentEvent]:
        sid = state.session_id
        state.pending.pop(block["id"], None)
        events: list[AgentEvent] = list(outcome.events)
        for event in outcome.events:
            if isinstance(event, TableSaved):
                state.tables.append(event.model_dump(exclude={"session_id", "type"}))
        state.tool_calls.append(
            {
                "name": block.get("name"),
                "input": block.get("input") if isinstance(block.get("input"), dict) else {},
                "ok": outcome.ok,
                "summary": outcome.summary,
                "result_id": outcome.result_id,
            }
        )
        events.append(
            ToolCallFinished(
                session_id=sid,
                call_id=block["id"],
                name=block.get("name") or "",
                ok=outcome.ok,
                summary=outcome.summary,
                n_records=outcome.n_records,
                total=outcome.total,
                latency_ms=round(outcome.latency_ms, 1),
                error=outcome.error,
                result_id=outcome.result_id,
                preview=outcome.preview,
            )
        )
        return events

    def _finish_unrun(self, state: _RunState, reason: str) -> list[AgentEvent]:
        """Close the UI's pending tool calls that will never run."""
        events: list[AgentEvent] = []
        for call_id, call in list(state.pending.items()):
            del state.pending[call_id]
            summary = f"{call['name']}: not run"
            state.tool_calls.append(
                {"name": call["name"], "input": call["input"], "ok": False, "summary": summary, "result_id": None}
            )
            events.append(
                ToolCallFinished(
                    session_id=state.session_id, call_id=call_id, name=call["name"], ok=False,
                    summary=summary, error=reason,
                )
            )
        return events

    # ------------------------------------------------------------------ bookkeeping
    def _fail(self, state: _RunState, message: str, retryable: bool) -> AgentError:
        state.error = message
        if state.stop_reason in (None, "tool_use", "pause_turn"):
            state.stop_reason = "error"
        return AgentError(session_id=state.session_id, message=message, retryable=retryable)

    def _audit_turn(self, sid: str, message: Any, counts: dict[str, int], n_tools: int, request_id: str | None) -> None:
        detail: dict[str, Any] = {"request_id": request_id, "fallback": served_by_fallback(message)}
        if getattr(message, "stop_details", None) is not None:
            detail["stop_details"] = message.stop_details.to_dict(mode="json")
        try:
            self.registry.audit.record_agent_turn(
                session_id=sid,
                model=getattr(message, "model", None) or self.model,
                stop_reason=message.stop_reason,
                usage=counts,
                tool_calls=n_tools,
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - auditing must never break a run
            log.exception("failed to audit agent turn")

    def _save_reply(self, state: _RunState) -> None:
        entry: dict[str, Any] = {
            "role": "assistant",
            "text": state.final_text,
            "tool_calls": state.tool_calls,
            "tables": state.tables,
            "stop_reason": state.stop_reason,
            "created_at": iso(time.time()),
        }
        if state.error:
            entry["error"] = state.error
        if not state.finished and not state.error:
            entry["interrupted"] = True
        try:
            self.sessions.append_transcript(state.session_id, entry)
        except Exception:  # noqa: BLE001
            log.exception("failed to save the transcript for session %s", state.session_id)
