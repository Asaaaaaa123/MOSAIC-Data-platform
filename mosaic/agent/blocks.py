"""Messages API content blocks: dumping, history echo rules and activity labels.

Assistant turns are stored exactly as the API returned them (``block.to_dict()``
keeps only the fields the API sent, under their wire names), so thinking
signatures, server-tool results, tool-search results and fallback markers
round-trip unchanged. The one documented exception is a turn that a server-side
refusal fallback took over mid-output: :func:`echo_content` applies the echo
rules from https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback
("Continuing the conversation") before the turn is appended to history.
"""

from __future__ import annotations

from typing import Any, Iterable

THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})

# Display names for Anthropic-hosted tools (ServerToolActivity.name).
_SERVER_TOOL_LABELS = {
    "tool_search_tool_bm25": "tool_search",
    "tool_search_tool_regex": "tool_search",
}


def dump_block(block: Any) -> dict[str, Any]:
    """The API-shaped dict for one response content block, exactly as received."""
    if isinstance(block, dict):
        return block
    return block.to_dict(mode="json")


def dump_content(blocks: Iterable[Any]) -> list[dict[str, Any]]:
    return [dump_block(b) for b in blocks]


def is_server_call(block: dict[str, Any]) -> bool:
    """A server-side tool call (``server_tool_use``, ``mcp_tool_use`` ...), not a client ``tool_use``."""
    kind = str(block.get("type") or "")
    return kind.endswith("_tool_use") and kind != "tool_use"


def is_server_result(block: dict[str, Any]) -> bool:
    """A server-side tool result (``web_search_tool_result``, ``tool_search_tool_result`` ...)."""
    return "tool_use_id" in block and str(block.get("type") or "").endswith("_tool_result")


def unpaired_server_calls(blocks: Iterable[dict[str, Any]]) -> list[str]:
    """Ids of server-side tool calls in ``blocks`` that have no result block."""
    items = list(blocks)
    answered = {b.get("tool_use_id") for b in items if is_server_result(b)}
    return [str(b.get("id")) for b in items if is_server_call(b) and b.get("id") not in answered]


def echo_content(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The assistant content to append to history for a finished response.

    Unchanged unless the response contains a ``fallback`` block. Then, before the
    final ``fallback`` block, only text blocks, paired server-tool blocks and the
    ``fallback`` markers are kept: thinking, redacted thinking, client ``tool_use``,
    a server-tool call without its result and any other model-internal block type
    are dropped. Every block after the final ``fallback`` block is kept in place.
    """
    last = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=None)
    if last is None:
        return list(blocks)
    unpaired = set(unpaired_server_calls(blocks[:last]))
    kept: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if index < last:
            kind = block.get("type")
            if is_server_call(block):
                if block.get("id") in unpaired:
                    continue
            elif kind not in ("text", "fallback") and not is_server_result(block):
                continue
        kept.append(block)
    return kept


def client_tool_uses(blocks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in blocks if b.get("type") == "tool_use"]


def strip_thinking(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove every thinking / redacted_thinking block (one-time recovery path).

    Documented recovery for a 400 "thinking block is bound to a different
    conversation": strip the blocks, keep each turn's text and tool calls, retry
    once. Assistant messages left empty are dropped (consecutive user turns are
    merged by the API).
    """
    cleaned: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if message.get("role") != "assistant" or not isinstance(content, list):
            cleaned.append(message)
            continue
        kept = [b for b in content if b.get("type") not in THINKING_TYPES]
        if kept:
            cleaned.append({**message, "content": kept})
    return cleaned


def server_tool_label(name: str | None) -> str:
    return _SERVER_TOOL_LABELS.get(name or "", name or "server_tool")


def describe_server_call(block: dict[str, Any]) -> str | None:
    """Short detail for a ``server_tool_use`` block (the query, URL or pattern)."""
    payload = block.get("input") or {}
    if not isinstance(payload, dict):
        return None
    for key in ("query", "url", "pattern", "code", "command"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value[:300]
    return None


def describe_server_result(block: dict[str, Any]) -> str | None:
    """Short detail for a server-tool result block (counts, tools loaded or error code)."""
    content = block.get("content")
    kind = block.get("type")
    if isinstance(content, dict):
        if content.get("error_code"):
            return f"error: {content['error_code']}"
        if kind == "tool_search_tool_result":
            names = [r.get("tool_name") for r in content.get("tool_references") or [] if r.get("tool_name")]
            return "loaded: " + ", ".join(names) if names else "no matching tools"
        if kind == "web_fetch_tool_result":
            return content.get("url")
        return None
    if isinstance(content, list):
        if kind == "web_search_tool_result":
            return f"{len(content)} result(s)"
        return None
    return None


def server_result_label(block: dict[str, Any], calls: dict[str, str]) -> str:
    """Display name for a server-tool result, from the call it answers."""
    call_name = calls.get(block.get("tool_use_id") or "")
    if call_name:
        return server_tool_label(call_name)
    kind = str(block.get("type") or "")
    if kind == "tool_search_tool_result":
        return "tool_search"
    return kind.removesuffix("_tool_result") or "server_tool"
