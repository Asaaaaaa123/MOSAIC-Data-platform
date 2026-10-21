"""Fault-tolerant registry calls for the comparison engine: one failing source never breaks a comparison."""

from __future__ import annotations

from typing import Any

from ..models import ToolResult


class SafeCaller:
    """Wraps ``registry.call``; failures are collected in ``errors`` (label -> message) instead of raised."""

    def __init__(self, registry: Any, session_id: str | None = None):
        self.registry = registry
        self.session_id = session_id
        self.errors: dict[str, str] = {}
        self.sources_used: set[str] = set()

    def available(self, source: str, tool: str | None = None) -> bool:
        connectors = getattr(self.registry, "connectors", None)
        if connectors is None:
            return True
        connector = connectors.get(source)
        if connector is None or not getattr(connector, "configured", True):
            return False
        return tool is None or tool in getattr(connector, "_tool_defs", {tool: None})

    async def call(
        self, source: str, tool: str, params: dict[str, Any], *, label: str | None = None, timeout: float = 60.0
    ) -> ToolResult | None:
        key = label or f"{source}.{tool}"
        if not self.available(source, tool):
            self.errors.setdefault(key, f"{source} is not available or not configured")
            return None
        try:
            result = await self.registry.call(source, tool, params, session_id=self.session_id, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - SourceError, timeouts, connector bugs: record, continue
            self.errors[key] = (str(exc) or type(exc).__name__)[:300]
            return None
        self.sources_used.add(source)
        return result
