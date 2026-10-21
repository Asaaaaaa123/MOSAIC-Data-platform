"""Messages API pieces shared by the agent loop and the extractor.

Versions and shapes here were checked against the bundled Claude API docs, the
platform docs and the installed ``anthropic`` 1.x SDK (``client.beta.messages``):

* Refusal fallback: ``fallbacks="default"`` with beta ``server-side-fallback-2026-07-01``
  (the array form uses ``-2026-06-01``; pairing a header with the other form is a 400).
* Tool search (BM25): ``{"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}``,
  no beta header; it must never itself be deferred.
* Web tools: ``web_search_20260318`` / ``web_fetch_20260318`` (dynamic filtering +
  response inclusion), no beta header.
"""

from __future__ import annotations

from typing import Any

import anthropic

FALLBACK_BETA = "server-side-fallback-2026-07-01"
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
TOOL_RESULT_CHARS = 30_000  # budget for one tool_result's text (ToolResult.to_llm)

TOOL_SEARCH_TOOL: dict[str, Any] = {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}
WEB_SEARCH_TOOL: dict[str, Any] = {"type": "web_search_20260318", "name": "web_search", "max_uses": 10}
WEB_FETCH_TOOL: dict[str, Any] = {
    "type": "web_fetch_20260318",
    "name": "web_fetch",
    "max_uses": 10,
    "max_content_tokens": 25_000,
}

NOT_CONFIGURED = (
    "Claude is not configured: set ANTHROPIC_API_KEY in .env (or the environment) and restart Mosaic. "
    "The database tools still work through the REST API without it."
)


def fallback_kwargs(enabled: bool) -> dict[str, Any]:
    """Request kwargs that opt into server-side refusal fallback (beta)."""
    if not enabled:
        return {}
    return {"fallbacks": "default", "betas": [FALLBACK_BETA]}


def fallbacks_enabled(settings: Any) -> bool:
    """``MOSAIC_FALLBACKS`` = ``default`` (the default) or ``off``."""
    value = (settings.env("MOSAIC_FALLBACKS") or "default").strip().lower()
    return value not in ("off", "false", "0", "none", "disabled")


def effort_setting(value: str | None, default: str) -> str:
    effort = (value or "").strip().lower()
    return effort if effort in VALID_EFFORTS else default


def usage_counts(usage: Any) -> dict[str, int]:
    """Integer usage fields of a response (top-level attempt) plus server-tool counts."""
    if usage is None:
        return {}
    counts: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            counts[key] = value
    server = getattr(usage, "server_tool_use", None)
    for key in ("web_search_requests", "web_fetch_requests"):
        value = getattr(server, key, None) if server is not None else None
        if isinstance(value, int) and value:
            counts[key] = value
    return counts


def add_usage(total: dict[str, int], counts: dict[str, int]) -> None:
    for key, value in counts.items():
        total[key] = total.get(key, 0) + value


def served_by_fallback(message: Any) -> bool:
    """True when a fallback model produced (part of) this response."""
    iterations = getattr(getattr(message, "usage", None), "iterations", None) or []
    return any(getattr(entry, "type", None) == "fallback_message" for entry in iterations)


def is_thinking_binding_error(exc: Exception) -> bool:
    """400 for a replayed thinking block whose signature no longer matches the history."""
    if not isinstance(exc, anthropic.BadRequestError):
        return False
    text = str(getattr(exc, "message", "") or exc)
    return "signature" in text and "thinking" in text


def is_missing_credentials(exc: BaseException) -> bool:
    """The SDK raises TypeError at request time when no credential could be resolved."""
    if isinstance(exc, anthropic.CredentialsError):
        return True
    return isinstance(exc, TypeError) and "authentication method" in str(exc)


def api_error_message(exc: anthropic.APIError, model: str) -> tuple[str, bool]:
    """User-facing message and retryability for an API failure (most specific first)."""
    request_id = getattr(exc, "request_id", None)
    suffix = f" (request id {request_id})" if request_id else ""
    detail = str(getattr(exc, "message", "") or exc)[:500]
    if isinstance(exc, anthropic.AuthenticationError):
        return f"{NOT_CONFIGURED} The API rejected the configured key.{suffix}", False
    if isinstance(exc, anthropic.PermissionDeniedError):
        return f"This API key is not allowed to use {model}: {detail}{suffix}", False
    if isinstance(exc, anthropic.NotFoundError):
        return f"Model {model!r} was not found or is not available to this API key; check MOSAIC_MODEL.{suffix}", False
    if isinstance(exc, anthropic.RateLimitError):
        return f"Claude API rate limit reached. Wait a moment and send the message again.{suffix}", True
    if isinstance(exc, anthropic.OverloadedError) or getattr(exc, "type", None) == "overloaded_error":
        return f"Claude is temporarily overloaded. Try again in a minute.{suffix}", True
    if isinstance(exc, anthropic.BadRequestError):
        hint = ""
        if "web search" in detail.lower() or "web fetch" in detail.lower():
            hint = " If web tools are disabled for your organization, set MOSAIC_ENABLE_WEB_TOOLS=false."
        return f"Claude rejected the request: {detail}{hint}{suffix}", False
    if isinstance(exc, anthropic.APIStatusError):
        retryable = exc.status_code >= 500 or getattr(exc, "type", None) in ("api_error", "overloaded_error")
        return f"Claude API error (HTTP {exc.status_code}): {detail}{suffix}", retryable
    if isinstance(exc, anthropic.APITimeoutError):
        return "The request to Claude timed out. Try again.", True
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not reach the Claude API (network error). Check the connection and try again.", True
    return f"Claude API error: {detail}{suffix}", False
