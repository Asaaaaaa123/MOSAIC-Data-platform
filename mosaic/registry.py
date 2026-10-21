"""Connector registry: discovery, dispatch, auditing and federated search."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import pkgutil
import time
from typing import Any, Iterable

from pydantic import BaseModel, Field

from . import connectors as connectors_pkg
from .audit import AuditLog
from .cache import ResponseCache
from .config import Settings, get_settings
from .connectors.base import CATEGORIES, Connector, ToolDef
from .crosslink.merge import cluster_records, merge_cluster
from .http import HttpClient, SourceError
from .models import HealthResult, Record, SourceInfo, ToolResult, ToolSpec

log = logging.getLogger("mosaic.registry")

_SKIP_MODULES = {"base"}


def discover_connector_classes() -> tuple[list[type[Connector]], dict[str, str]]:
    """Import every module in ``mosaic.connectors`` and collect Connector subclasses.

    A module that fails to import is reported in the returned error map instead of
    breaking the platform.
    """
    found: dict[str, type[Connector]] = {}
    errors: dict[str, str] = {}
    for module_info in pkgutil.iter_modules(connectors_pkg.__path__):
        if module_info.name.startswith("_") or module_info.name in _SKIP_MODULES:
            continue
        module_name = f"{connectors_pkg.__name__}.{module_info.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - isolate broken connectors
            log.exception("failed to import connector module %s", module_name)
            errors[module_info.name] = f"{type(exc).__name__}: {exc}"
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, Connector)
                and obj is not Connector
                and obj.__module__ == module.__name__
                and "name" in obj.__dict__
            ):
                if obj.name in found and found[obj.name] is not obj:
                    errors[obj.name] = f"duplicate connector name in {module_name}"
                    continue
                found[obj.name] = obj
    return sorted(found.values(), key=lambda c: (c.category, c.name)), errors


class SourceSearchResult(BaseModel):
    source: str
    ok: bool
    total: int | None = None
    returned: int = 0
    error: str | None = None
    latency_ms: float | None = None


class FederatedResult(BaseModel):
    query: str
    sources: list[SourceSearchResult] = Field(default_factory=list)
    records: list[Record] = Field(default_factory=list)
    clusters: list[dict[str, Any]] = Field(default_factory=list)

    def to_llm(self, max_chars: int = 40_000) -> str:
        import json

        summary = {
            "query": self.query,
            "sources": [s.model_dump(exclude_none=True) for s in self.sources],
            "linked_entities": [c for c in self.clusters if len(c["sources"]) > 1][:30],
        }
        by_source: dict[str, list[dict[str, Any]]] = {}
        for record in self.records:
            item = record.compact(summary_chars=200)
            item.pop("source", None)
            by_source.setdefault(record.source, []).append(item)
        summary["records_by_source"] = by_source
        text = json.dumps(summary, ensure_ascii=False, default=str)
        if len(text) <= max_chars:
            return text
        for record_list in by_source.values():
            for item in record_list:
                item.pop("summary", None)
                item.pop("attributes", None)
        text = json.dumps(summary, ensure_ascii=False, default=str)
        # Drop trailing records per source until it fits; never slice the JSON text.
        while len(text) > max_chars and any(len(v) > 1 for v in by_source.values()):
            for record_list in by_source.values():
                if len(record_list) > 1:
                    record_list.pop()
            text = json.dumps(summary, ensure_ascii=False, default=str)
        if len(text) > max_chars:
            summary.pop("records_by_source", None)
            summary["linked_entities"] = summary["linked_entities"][:5]
            text = json.dumps(summary, ensure_ascii=False, default=str)
        return text


class Registry:
    def __init__(
        self,
        settings: Settings,
        http: HttpClient,
        audit: AuditLog,
        connectors: dict[str, Connector],
        load_errors: dict[str, str] | None = None,
    ):
        self.settings = settings
        self.http = http
        self.audit = audit
        self.connectors = connectors
        self.load_errors = load_errors or {}

    @classmethod
    def create(
        cls,
        settings: Settings | None = None,
        *,
        only: Iterable[str] | None = None,
        transport: Any = None,
        audit: AuditLog | None = None,
        cache: ResponseCache | None = None,
    ) -> "Registry":
        settings = settings or get_settings()
        if cache is None:
            cache = ResponseCache(settings.cache_db_path, settings.biopha_cache_ttl)
        http = HttpClient(settings, cache=cache, transport=transport)
        audit = audit or AuditLog(settings.audit_db_path)
        classes, errors = discover_connector_classes()
        wanted = set(only) if only else None
        instances: dict[str, Connector] = {}
        for klass in classes:
            if wanted is not None and klass.name not in wanted:
                continue
            try:
                instances[klass.name] = klass(settings, http)
            except Exception as exc:  # noqa: BLE001
                log.exception("failed to initialise connector %s", klass.name)
                errors[klass.name] = f"{type(exc).__name__}: {exc}"
        return cls(settings, http, audit, instances, errors)

    # ---- lookup ----
    def get(self, name: str) -> Connector:
        try:
            return self.connectors[name]
        except KeyError:
            raise SourceError(
                name, f"unknown source {name!r}; available: {', '.join(sorted(self.connectors))}"
            ) from None

    def sources(self, category: str | None = None) -> list[SourceInfo]:
        return [c.info() for c in self.connectors.values() if category in (None, c.category)]

    def categories(self) -> dict[str, str]:
        return dict(CATEGORIES)

    def tool_specs(self, configured_only: bool = True) -> list[ToolSpec]:
        specs: list[ToolSpec] = []
        for connector in self.connectors.values():
            if configured_only and not connector.configured:
                continue
            specs.extend(connector.tool_specs())
        return specs

    def resolve_qualified(self, qualified_name: str) -> tuple[Connector, ToolDef]:
        source, sep, tool_name = qualified_name.partition("__")
        if not sep:
            raise SourceError("mosaic", f"tool name {qualified_name!r} must look like <source>__<tool>")
        connector = self.get(source)
        tool_def = connector._tool_defs.get(tool_name)
        if tool_def is None:
            raise SourceError(source, f"unknown tool {tool_name!r}; available: {', '.join(connector._tool_defs)}")
        return connector, tool_def

    # ---- dispatch ----
    async def call(
        self,
        source: str,
        tool: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> ToolResult:
        """Run one tool with auditing. Raises SourceError (already audited)."""
        params = params or {}
        started = time.perf_counter()
        try:
            connector = self.get(source)
            coro = connector.call(tool, params)
            result = await (asyncio.wait_for(coro, timeout) if timeout else coro)
        except asyncio.TimeoutError:
            err = SourceError(source, f"{tool} timed out after {timeout:.0f}s", retryable=True)
            self._audit_fail(source, tool, params, started, err, session_id)
            raise err from None
        except SourceError as exc:
            self._audit_fail(source, tool, params, started, exc, session_id)
            raise
        except Exception as exc:  # noqa: BLE001 - connector bug; surface as SourceError
            log.exception("connector %s.%s crashed", source, tool)
            err = SourceError(source, f"internal connector error: {type(exc).__name__}: {exc}")
            self._audit_fail(source, tool, params, started, err, session_id)
            raise err from exc
        self.audit.record_tool_call(
            source=source,
            tool=tool,
            params=params,
            ok=True,
            latency_ms=(time.perf_counter() - started) * 1000,
            session_id=session_id,
            n_records=len(result.records),
            total=result.total,
            request_url=result.request_url,
        )
        return result

    def _audit_fail(
        self, source: str, tool: str, params: dict[str, Any], started: float, exc: SourceError, session_id: str | None
    ) -> None:
        self.audit.record_tool_call(
            source=source,
            tool=tool,
            params=params,
            ok=False,
            latency_ms=(time.perf_counter() - started) * 1000,
            session_id=session_id,
            error=str(exc)[:1000],
            request_url=exc.url,
        )

    async def call_qualified(
        self,
        qualified_name: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> ToolResult:
        connector, tool_def = self.resolve_qualified(qualified_name)
        return await self.call(connector.name, tool_def.name, params, session_id=session_id, timeout=timeout)

    # ---- cross-database ----
    def federated_sources(self, categories: Iterable[str] | None = None) -> list[str]:
        cats = set(categories) if categories else None
        return [
            c.name
            for c in self.connectors.values()
            if c.configured and c.federated_tool() is not None and (cats is None or c.category in cats)
        ]

    async def federated_search(
        self,
        query: str,
        *,
        sources: Iterable[str] | None = None,
        categories: Iterable[str] | None = None,
        limit_per_source: int = 5,
        timeout: float = 30.0,
        session_id: str | None = None,
    ) -> FederatedResult:
        """Run each selected source's primary search concurrently and link the results."""
        names = list(sources) if sources else self.federated_sources(categories)
        outcome = FederatedResult(query=query)

        async def one(name: str) -> tuple[str, ToolResult | None, str | None, float]:
            started = time.perf_counter()
            try:
                connector = self.get(name)
                tool_def = connector.federated_tool()
                if tool_def is None:
                    raise SourceError(name, "source has no federated search tool")
                result = await self.call(
                    name,
                    tool_def.name,
                    {"query": query, "limit": limit_per_source},
                    session_id=session_id,
                    timeout=timeout,
                )
                return name, result, None, (time.perf_counter() - started) * 1000
            except SourceError as exc:
                return name, None, str(exc), (time.perf_counter() - started) * 1000

        for name, result, error, latency in await asyncio.gather(*(one(n) for n in names)):
            if result is None:
                outcome.sources.append(SourceSearchResult(source=name, ok=False, error=error, latency_ms=latency))
                continue
            outcome.sources.append(
                SourceSearchResult(
                    source=name, ok=True, total=result.total, returned=len(result.records), latency_ms=latency
                )
            )
            outcome.records.extend(result.records)
        outcome.clusters = [merge_cluster(c) for c in cluster_records(outcome.records)]
        return outcome

    async def healthcheck(self, sources: Iterable[str] | None = None, timeout: float = 30.0) -> list[HealthResult]:
        names = list(sources) if sources else list(self.connectors)

        async def one(name: str) -> HealthResult:
            try:
                return await asyncio.wait_for(self.get(name).healthcheck(), timeout)
            except asyncio.TimeoutError:
                return HealthResult(source=name, ok=False, detail=f"timed out after {timeout:.0f}s")
            except Exception as exc:  # noqa: BLE001
                return HealthResult(source=name, ok=False, detail=str(exc)[:300])

        return list(await asyncio.gather(*(one(n) for n in names)))

    async def aclose(self) -> None:
        for connector in self.connectors.values():
            try:
                await connector.aclose()
            except Exception:  # noqa: BLE001
                log.exception("error closing %s", connector.name)
        await self.http.aclose()
