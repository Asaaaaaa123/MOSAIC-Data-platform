"""The ``tools`` array and system blocks sent to Claude (the request prefix).

Order is fixed and every list is sorted, so two processes with the same sources
build byte-identical prefixes and share prompt-cache entries:

1. the BM25 tool-search tool (deferred mode only; never deferred itself),
2. Mosaic meta tools (sorted by name; never deferred),
3. ``web_search`` / ``web_fetch`` server tools (when enabled),
4. connector tools sorted by qualified name (``defer_loading`` in deferred mode).

Client tools (meta + connector) set ``eager_input_streaming``: requests are
streamed, and both kinds validate their input with pydantic before running, so
the buffering-time validation the API would otherwise do is not lost. Server
tools never carry it (not a valid field there). No tool carries
``cache_control``: a deferred tool may not, and the request uses top-level
automatic caching plus one explicit breakpoint on the system prompt.
"""

from __future__ import annotations

from typing import Any

from ..registry import Registry
from .claude_api import TOOL_SEARCH_TOOL, WEB_FETCH_TOOL, WEB_SEARCH_TOOL
from .meta_tools import MetaToolRegistry
from .prompts import build_system_prompt


def build_tools(registry: Registry, meta_tools: MetaToolRegistry, *, deferred: bool, web_tools: bool) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    if deferred:
        tools.append(dict(TOOL_SEARCH_TOOL))
    for meta in meta_tools:
        tools.append({**meta.definition(), "eager_input_streaming": True})
    if web_tools:
        tools += [dict(WEB_SEARCH_TOOL), dict(WEB_FETCH_TOOL)]
    for spec in sorted(registry.tool_specs(configured_only=True), key=lambda s: s.qualified_name):
        title = registry.connectors[spec.source].title
        tool: dict[str, Any] = {
            "name": spec.qualified_name,
            # The source title helps tool search match "Europe PMC", "ClinicalTrials.gov" ...
            "description": f"[{title}] {spec.description}",
            "input_schema": spec.input_schema,
            "eager_input_streaming": True,
        }
        if deferred:
            tool["defer_loading"] = True
        tools.append(tool)
    return tools


def build_prefix(
    registry: Registry,
    meta_tools: MetaToolRegistry,
    *,
    catalog_stats: dict[str, Any],
    deferred: bool,
    web_tools: bool,
) -> dict[str, Any]:
    """``{"system": [...], "tools": [...]}`` for a new session."""
    prompt = build_system_prompt(
        registry.sources(),
        catalog_stats=catalog_stats,
        deferred_tools=deferred,
        web_tools=web_tools,
        load_errors=registry.load_errors,
    )
    return {
        # Explicit breakpoint on the static prefix (tools + system); the request's
        # top-level cache_control caches the growing conversation tail.
        "system": [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}],
        "tools": build_tools(registry, meta_tools, deferred=deferred, web_tools=web_tools),
    }


def prefix_defers_tools(prefix: dict[str, Any]) -> bool:
    return any(t.get("defer_loading") for t in prefix.get("tools") or [])
