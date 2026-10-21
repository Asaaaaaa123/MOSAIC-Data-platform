"""FastAPI application: Mosaic's JSON API, chat event stream and web UI.

``create_app()`` is what ``mosaic serve`` runs (as a uvicorn factory). Tests
inject a registry and a fake agent::

    app = create_app(registry=registry, agent=FakeAgent())

The Claude agent (``mosaic.agent.MosaicAgent``) is imported lazily in the
lifespan. If it cannot be imported or constructed (missing ANTHROPIC_API_KEY,
package not installed ...), chat and session endpoints answer 503 with the
reason while every other endpoint keeps working.
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Literal

from fastapi import Body, FastAPI, Query, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..catalog import RepositoryCatalog
from ..config import Settings, get_settings
from ..connectors.base import CATEGORIES, Connector
from ..http import SourceError
from ..registry import Registry
from .errors import ApiError, source_api_error
from .exports import (
    PACKAGE_MEDIA_TYPES,
    audit_name,
    download_name,
    list_exports,
    package_download_name,
    preview_csv,
    read_manifest,
    resolve_export_file,
    resolve_package_file,
)
from .schemas import (
    SESSION_ID_PATTERN,
    CallRequest,
    ChatRequest,
    HealthRequest,
    ResolveRequest,
    SearchExportRequest,
    SearchRequest,
    SpeciesCompareRequest,
)
from .security import SecretScrubber, SecurityMiddleware, default_allowed_hosts
from .streaming import SSE_HEADERS, sse_stream

log = logging.getLogger("mosaic.api")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_AGENT_NOT_LOADED = "the agent has not been loaded"

SessionId = Annotated[str, PathParam(pattern=SESSION_ID_PATTERN, description="Agent session id")]


@dataclass
class MosaicState:
    """Everything the endpoints share (kept on ``app.state.mosaic``)."""

    settings: Settings
    registry: Registry | None = None
    agent: Any = None
    agent_error: str | None = None
    catalog: RepositoryCatalog | None = None
    catalog_facets: dict[str, Any] = field(default_factory=dict)
    scrub: SecretScrubber | None = None
    heartbeat: float = 15.0
    started_at: float = field(default_factory=time.time)
    active_sessions: set[str] = field(default_factory=set)

    def attach_registry(self, registry: Registry) -> None:
        self.registry = registry
        self.catalog = RepositoryCatalog.from_connectors(registry.connectors.values())
        self.catalog_facets = _catalog_facets(self.catalog)
        names = {s for c in registry.connectors.values() for s in c.required_settings}
        self.scrub = SecretScrubber(self.settings, names)

    def require_registry(self) -> Registry:
        if self.registry is None:
            raise ApiError(503, "the source registry is not initialised (the app lifespan has not run)")
        return self.registry

    def require_agent(self) -> Any:
        if self.agent is None:
            reason = self.clean(self.agent_error) or _AGENT_NOT_LOADED
            raise ApiError(503, f"Ask Mosaic is unavailable: {reason}", kind="agent_unavailable", reason=reason)
        return self.agent

    def clean(self, text: Any) -> Any:
        return self.scrub(text) if self.scrub is not None else text


def load_agent(registry: Registry, settings: Settings) -> tuple[Any | None, str | None]:
    """Import and construct ``MosaicAgent``; return ``(agent, None)`` or ``(None, reason)``."""
    if not (settings.anthropic_api_key or settings.env("ANTHROPIC_API_KEY") or settings.env("ANTHROPIC_AUTH_TOKEN")):
        return None, "ANTHROPIC_API_KEY is not set; add it to .env and restart Mosaic"
    try:
        from mosaic.agent import MosaicAgent  # deferred on purpose: optional, heavy component
    except Exception as exc:  # noqa: BLE001
        log.warning("Ask Mosaic disabled: cannot import mosaic.agent (%s: %s)", type(exc).__name__, exc)
        return None, f"cannot import mosaic.agent ({type(exc).__name__}: {exc})"
    try:
        return MosaicAgent(registry, settings), None
    except Exception as exc:  # noqa: BLE001
        log.exception("Ask Mosaic disabled: MosaicAgent failed to start")
        return None, f"MosaicAgent failed to start ({type(exc).__name__}: {exc})"


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _close_agent(agent: Any) -> None:
    for attr in ("aclose", "close"):
        closer = getattr(agent, attr, None)
        if callable(closer):
            try:
                await _maybe_await(closer())
            except Exception:  # noqa: BLE001
                log.exception("error closing the agent")
            return


async def _aclose_iter(iterator: Any) -> None:
    closer = getattr(iterator, "aclose", None)
    if closer is not None:
        try:
            await closer()
        except Exception:  # noqa: BLE001
            log.debug("error closing agent stream", exc_info=True)


def _catalog_facets(catalog: RepositoryCatalog) -> dict[str, Any]:
    kinds: Counter[str] = Counter()
    access: Counter[str] = Counter()
    institutes: Counter[str] = Counter()
    connectors: Counter[str] = Counter()
    for repo in catalog.repositories:
        kinds[repo.get("kind") or "unknown"] += 1
        for token in (repo.get("access_type") or "").split(","):
            if token.strip():
                access[token.strip().lower()] += 1
        for inst in repo.get("institutes") or []:
            institutes[inst] += 1
        for name in repo.get("connectors") or []:
            connectors[name] += 1
    return {
        "kind": dict(kinds.most_common()),
        "access_type": dict(access.most_common()),
        "institutes": dict(sorted(institutes.items(), key=lambda kv: (-kv[1], kv[0]))),
        "connectors": dict(sorted(connectors.items())),
    }


def _json_field(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _configure_logging(settings: Settings) -> None:
    try:
        level = logging.getLevelName(settings.biopha_log_level.upper())
        if not isinstance(level, int):
            level = logging.INFO
    except Exception:  # noqa: BLE001
        level = logging.INFO
    if not logging.getLogger().handlers:
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("mosaic").setLevel(level)


def _split_env(settings: Settings, name: str) -> list[str] | None:
    raw = settings.env(name)
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


class _NoCacheStaticFiles(StaticFiles):
    """Static files that the browser revalidates (the UI changes with the code)."""

    def file_response(self, *args: Any, **kwargs: Any) -> Any:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    settings: Settings | None = None,
    registry: Registry | None = None,
    agent: Any = None,
    *,
    load_agent_on_startup: bool = True,
    heartbeat_interval: float = 15.0,
    allowed_hosts: list[str] | None = None,
    cors_origins: list[str] | None = None,
) -> FastAPI:
    """Build the Mosaic FastAPI app.

    ``registry`` / ``agent`` are used as given (and not closed) when injected;
    otherwise the lifespan creates ``Registry.create(settings)`` and tries to load
    ``MosaicAgent``. ``allowed_hosts`` defaults to loopback names when the bind
    host (``MOSAIC_BIND_HOST`` or ``BIOPHA_API_HOST``) is loopback; ``cors_origins``
    defaults to ``MOSAIC_CORS_ORIGINS`` (comma separated) and CORS is off when empty.
    """
    if settings is None:
        settings = registry.settings if registry is not None else get_settings()
    _configure_logging(settings)
    state = MosaicState(settings=settings, agent=agent, heartbeat=heartbeat_interval)
    state.scrub = SecretScrubber(settings)
    if registry is not None:
        state.attach_registry(registry)
    if agent is None and not load_agent_on_startup:
        state.agent_error = "the agent is disabled for this app instance"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        created: Registry | None = None
        loaded_agent = False
        if state.registry is None:
            created = Registry.create(settings)
            state.attach_registry(created)
        if state.agent is None and load_agent_on_startup:
            state.agent, state.agent_error = load_agent(state.registry, settings)
            loaded_agent = state.agent is not None
        registry_now = state.require_registry()
        log.info(
            "Mosaic %s ready: %d sources (%d configured), agent %s",
            __version__,
            len(registry_now.connectors),
            sum(1 for c in registry_now.connectors.values() if c.configured),
            "available" if state.agent is not None else f"unavailable ({state.agent_error})",
        )
        try:
            yield
        finally:
            if loaded_agent:
                await _close_agent(state.agent)
                state.agent = None
            if created is not None:
                await created.aclose()
                try:
                    created.audit.close()
                except Exception:  # noqa: BLE001
                    log.debug("error closing audit log", exc_info=True)
                state.registry = None

    app = FastAPI(
        title="Mosaic",
        version=__version__,
        description="Claude-powered federated search and cross-database extraction over biomedical data sources.",
        lifespan=lifespan,
        docs_url=None,  # the bundled Swagger UI loads from a CDN; keep the app offline-clean
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.mosaic = state

    origins = cors_origins if cors_origins is not None else (_split_env(settings, "MOSAIC_CORS_ORIGINS") or [])
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["content-type"],
        )
    if allowed_hosts is None:
        allowed_hosts = _split_env(settings, "MOSAIC_ALLOWED_HOSTS")
        if allowed_hosts is None:
            bind_host = settings.env("MOSAIC_BIND_HOST") or settings.biopha_api_host
            hosts = default_allowed_hosts(bind_host)
            allowed_hosts = sorted(hosts) if hosts is not None else None
    app.add_middleware(SecurityMiddleware, allowed_hosts=allowed_hosts, allowed_origins=origins)

    # ------------------------------------------------------------------ errors
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            {"error": str(exc.detail), "status": exc.status_code},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        summary = "; ".join(
            f"{'.'.join(p for p in d['loc'] if p != 'body') or 'body'}: {d['msg']}" for d in details
        )
        return JSONResponse(
            {"error": f"invalid request: {summary}", "status": 422, "details": details}, status_code=422
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"error": "internal server error", "status": 500}, status_code=500)

    # ----------------------------------------------------------------- helpers
    def connector_or_404(name: str) -> Connector:
        registry = state.require_registry()
        connector = registry.connectors.get(name)
        if connector is None:
            raise ApiError(404, f"unknown source {name!r}", kind="unknown_source", source=name)
        return connector

    def health_dict(result: Any) -> dict[str, Any]:
        data = result.model_dump(mode="json")
        data["detail"] = state.clean(data.get("detail"))
        return data

    # ------------------------------------------------------------------ status
    @app.get("/api/health", tags=["status"])
    async def health() -> dict[str, Any]:
        registry = state.require_registry()
        connectors = list(registry.connectors.values())
        configured = sum(1 for c in connectors if c.configured)
        agent_model = getattr(state.agent, "model", None)
        return {
            "status": "ok",
            "version": __version__,
            "sources": len(connectors),
            "configured": configured,
            "unconfigured": len(connectors) - configured,
            "categories": len(CATEGORIES),
            "agent_available": state.agent is not None,
            "agent_error": None if state.agent is not None else (state.clean(state.agent_error) or _AGENT_NOT_LOADED),
            "model": agent_model if isinstance(agent_model, str) else settings.mosaic_model,
            "web_tools": bool(settings.mosaic_enable_web_tools),
            "catalog_entries": len(state.catalog.repositories) if state.catalog else 0,
            "load_errors": {name: state.clean(msg) for name, msg in registry.load_errors.items()},
            "uptime_s": round(time.time() - state.started_at, 1),
        }

    # ----------------------------------------------------------------- sources
    @app.get("/api/sources", tags=["sources"])
    async def list_sources(
        category: Annotated[str | None, Query(max_length=40)] = None,
        configured: bool | None = None,
    ) -> dict[str, Any]:
        registry = state.require_registry()
        if category is not None and category not in CATEGORIES:
            raise ApiError(400, f"unknown category {category!r}", categories=list(CATEGORIES))
        infos = registry.sources(category)
        if configured is not None:
            infos = [info for info in infos if info.configured == configured]
        counts: dict[str, dict[str, int]] = {key: {"total": 0, "configured": 0} for key in CATEGORIES}
        for connector in registry.connectors.values():
            bucket = counts.setdefault(connector.category, {"total": 0, "configured": 0})
            bucket["total"] += 1
            bucket["configured"] += int(connector.configured)
        return {
            "categories": registry.categories(),
            "counts": counts,
            "total": len(infos),
            "sources": [info.model_dump(mode="json") for info in infos],
        }

    @app.get("/api/sources/{name}", tags=["sources"])
    async def get_source(name: str) -> dict[str, Any]:
        return connector_or_404(name).info().model_dump(mode="json")

    @app.post("/api/sources/{name}/health", tags=["sources"])
    async def source_health(name: str, timeout: Annotated[float, Query(gt=0, le=120)] = 20.0) -> dict[str, Any]:
        connector_or_404(name)
        [result] = await state.require_registry().healthcheck([name], timeout=timeout)
        return health_dict(result)

    @app.post("/api/health/sources", tags=["sources"])
    async def sources_health(body: Annotated[HealthRequest | None, Body()] = None) -> dict[str, Any]:
        registry = state.require_registry()
        body = body or HealthRequest()
        names = list(dict.fromkeys(body.sources)) if body.sources else list(registry.connectors)
        unknown = [n for n in names if n not in registry.connectors]
        if unknown:
            raise ApiError(404, f"unknown source(s): {', '.join(unknown)}", kind="unknown_source", sources=unknown)
        started = time.perf_counter()
        results = [health_dict(r) for r in await registry.healthcheck(names, timeout=body.timeout)]
        n_ok = sum(1 for r in results if r["ok"])
        return {
            "results": results,
            "ok": n_ok,
            "failed": len(results) - n_ok,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    @app.get("/api/tools", tags=["sources"])
    async def list_tools(
        source: Annotated[str | None, Query(max_length=64)] = None, configured_only: bool = False
    ) -> dict[str, Any]:
        registry = state.require_registry()
        connectors = [connector_or_404(source)] if source else list(registry.connectors.values())
        tools: list[dict[str, Any]] = []
        for connector in connectors:
            if configured_only and not connector.configured:
                continue
            for spec in connector.tool_specs():
                tools.append(
                    {**spec.model_dump(mode="json"), "category": connector.category, "configured": connector.configured}
                )
        return {"total": len(tools), "tools": tools}

    @app.post("/api/call", tags=["sources"])
    async def call_tool(body: CallRequest) -> dict[str, Any]:
        registry = state.require_registry()
        connector = connector_or_404(body.source)
        available = [d.name for d in connector.tool_defs()]
        if body.tool not in available:
            raise ApiError(
                400,
                f"[{body.source}] unknown tool {body.tool!r}; available: {', '.join(available)}",
                source=body.source,
                kind="unknown_tool",
                available=available,
            )
        try:
            result = await registry.call(body.source, body.tool, body.params, timeout=body.timeout)
        except SourceError as exc:
            raise source_api_error(exc, state.clean) from None
        return result.model_dump(mode="json")

    @app.post("/api/resolve", tags=["search"])
    async def resolve_entity(body: ResolveRequest) -> dict[str, Any]:
        from ..crosslink.resolver import EntityResolver

        result = await EntityResolver(state.require_registry()).resolve(body.kind, body.query.strip())
        payload = result.model_dump(mode="json")
        payload["errors"] = {k: state.clean(v) for k, v in payload["errors"].items()}
        return payload

    @app.post("/api/search", tags=["search"])
    async def federated_search(body: SearchRequest) -> dict[str, Any]:
        registry = state.require_registry()
        if body.categories:
            unknown = [c for c in body.categories if c not in CATEGORIES]
            if unknown:
                raise ApiError(400, f"unknown category(s): {', '.join(unknown)}", categories=list(CATEGORIES))
        if body.sources:
            unknown = [s for s in body.sources if s not in registry.connectors]
            if unknown:
                raise ApiError(404, f"unknown source(s): {', '.join(unknown)}", kind="unknown_source", sources=unknown)
        started = time.perf_counter()
        result = await registry.federated_search(
            body.query,
            sources=body.sources or None,
            categories=body.categories or None,
            limit_per_source=body.limit_per_source,
            timeout=body.timeout,
        )
        payload = result.model_dump(mode="json")
        for status in payload["sources"]:
            status["error"] = state.clean(status.get("error"))
        payload["linked"] = sum(1 for c in payload["clusters"] if len(c.get("sources") or []) > 1)
        payload["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return payload

    # ----------------------------------------------------------------- catalog
    @app.get("/api/catalog", tags=["catalog"])
    async def catalog_search(
        q: Annotated[str | None, Query(max_length=500)] = None,
        kind: Literal["domain_specific", "generalist"] | None = None,
        access_type: Annotated[str | None, Query(max_length=40)] = None,
        institute: Annotated[str | None, Query(max_length=40)] = None,
        has_connector: bool | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> dict[str, Any]:
        state.require_registry()
        assert state.catalog is not None
        matches = state.catalog.search(
            q or None,
            kind=kind,
            access_type=access_type or None,
            institute=institute or None,
            has_connector=has_connector,
            limit=len(state.catalog.repositories) + 1,
        )
        return {
            "query": q,
            "count": len(matches),
            "returned": min(limit, len(matches)),
            "repositories": matches[:limit],
        }

    @app.get("/api/catalog/stats", tags=["catalog"])
    async def catalog_stats() -> dict[str, Any]:
        state.require_registry()
        assert state.catalog is not None
        meta = state.catalog.meta
        return {
            **state.catalog.stats(),
            "source": meta.get("source"),
            "retrieved": meta.get("retrieved"),
            "facets": state.catalog_facets,
        }

    # -------------------------------------------------------------------- chat
    async def guarded_run(agent: Any, message: str, session_id: str | None) -> AsyncIterator[Any]:
        iterator = await _maybe_await(agent.run(message, session_id=session_id))
        claimed: str | None = None
        if session_id:
            state.active_sessions.add(session_id)
            claimed = session_id
        try:
            async for event in iterator:
                sid = getattr(event, "session_id", None)
                if claimed is None and isinstance(sid, str) and sid:
                    state.active_sessions.add(sid)
                    claimed = sid
                yield event
        finally:
            if claimed:
                state.active_sessions.discard(claimed)
            await _aclose_iter(iterator)

    @app.post("/api/chat", tags=["agent"])
    async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
        agent = state.require_agent()
        if body.session_id and body.session_id in state.active_sessions:
            raise ApiError(409, "this session is still answering a previous message", kind="session_busy")
        stream = sse_stream(
            lambda: guarded_run(agent, body.message, body.session_id),
            is_disconnected=request.is_disconnected,
            heartbeat=state.heartbeat,
            session_id=body.session_id,
        )

        async def scrubbed() -> AsyncIterator[str]:
            # Tool results and model text can quote an upstream error that echoed a key.
            try:
                async for frame in stream:
                    yield state.clean(frame)
            finally:
                await stream.aclose()

        return StreamingResponse(scrubbed(), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.get("/api/sessions", tags=["agent"])
    async def list_sessions(limit: Annotated[int, Query(ge=1, le=500)] = 50) -> dict[str, Any]:
        agent = state.require_agent()
        sessions = list(await _maybe_await(agent.list_sessions(limit)) or [])
        for item in sessions:
            if isinstance(item, dict):
                item["active"] = item.get("session_id") in state.active_sessions
        return {"sessions": sessions, "total": len(sessions)}

    @app.get("/api/sessions/{session_id}", tags=["agent"])
    async def get_session(session_id: SessionId) -> dict[str, Any]:
        agent = state.require_agent()
        try:
            transcript = await _maybe_await(agent.get_transcript(session_id))
        except KeyError:
            raise ApiError(404, f"unknown session {session_id!r}", kind="unknown_session") from None
        sessions = await _maybe_await(agent.list_sessions(500)) or []
        meta = next((s for s in sessions if isinstance(s, dict) and s.get("session_id") == session_id), None)
        if not transcript and meta is None:
            raise ApiError(404, f"unknown session {session_id!r}", kind="unknown_session")
        return {
            "session_id": session_id,
            "title": (meta or {}).get("title"),
            "meta": meta,
            "active": session_id in state.active_sessions,
            "transcript": list(transcript or []),
        }

    @app.delete("/api/sessions/{session_id}", tags=["agent"])
    async def delete_session(session_id: SessionId) -> dict[str, Any]:
        agent = state.require_agent()
        if session_id in state.active_sessions:
            raise ApiError(409, "this session is still answering; stop it before deleting", kind="session_busy")
        deleted = await _maybe_await(agent.delete_session(session_id))
        if not deleted:
            raise ApiError(404, f"unknown session {session_id!r}", kind="unknown_session")
        return {"deleted": True, "session_id": session_id}

    # ----------------------------------------------------------------- exports
    @app.get("/api/exports", tags=["exports"])
    async def exports(limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> dict[str, Any]:
        registry = state.require_registry()
        items = list_exports(settings.exports_dir, registry.audit, limit)
        return {"total": len(items), "exports": items}

    @app.get("/api/exports/{filename}", tags=["exports"])
    async def export_file(filename: str) -> FileResponse:
        registry = state.require_registry()
        resolved = resolve_export_file(settings.exports_dir, filename)
        if resolved is None:
            raise ApiError(400, "invalid export file name: expected <32 lowercase hex characters>.csv or .json")
        export_id, ext, path = resolved
        if not path.is_file():
            raise ApiError(404, f"export {export_id} has no {ext} file")
        media_type = "text/csv; charset=utf-8" if ext == "csv" else "application/json"
        return FileResponse(
            path,
            media_type=media_type,
            filename=download_name(audit_name(registry.audit, export_id), export_id, ext),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/exports/{export_id}/{filename}", tags=["exports"])
    async def export_package_file(export_id: str, filename: str) -> FileResponse:
        resolved = resolve_package_file(settings.exports_dir, export_id, filename)
        if resolved is None:
            raise ApiError(404, "no such data package file")
        path, ext = resolved
        manifest = read_manifest(path.parent)
        inline = ext in ("txt", "json")
        return FileResponse(
            path,
            media_type=PACKAGE_MEDIA_TYPES[ext],
            filename=None if inline else package_download_name(manifest, export_id, filename),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/exports/{export_id}/{filename}/preview", tags=["exports"])
    async def export_package_preview(
        export_id: str, filename: str, limit: Annotated[int, Query(ge=1, le=1000)] = 200
    ) -> dict[str, Any]:
        resolved = resolve_package_file(settings.exports_dir, export_id, filename)
        if resolved is None or resolved[1] != "csv":
            raise ApiError(404, "no such data package table")
        return {"export_id": export_id, "file": filename, **preview_csv(resolved[0], limit)}

    @app.post("/api/search/export", tags=["search", "exports"])
    async def export_search(body: SearchExportRequest) -> dict[str, Any]:
        """Run a cross-database search and save its cleaned, graded data as a data package."""
        from ..datapackage import build_package, write_package

        registry = state.require_registry()
        if body.categories:
            unknown = [c for c in body.categories if c not in CATEGORIES]
            if unknown:
                raise ApiError(400, f"unknown category(s): {', '.join(unknown)}", categories=list(CATEGORIES))
        result = await registry.federated_search(
            body.query,
            sources=body.sources or None,
            categories=body.categories or None,
            limit_per_source=body.limit_per_source,
            timeout=body.timeout,
        )
        if not result.records:
            raise ApiError(404, "the search returned no records to export")
        from ..datafiles import collect_data_files
        from ..enrich import enrich_records

        records, notes = result.records, []
        if body.full_records:
            records, enrich_stats = await enrich_records(registry, records)
            notes = enrich_stats.notes()
        data_files = await collect_data_files(registry.http, records, download=body.download_files)
        package = build_package(records, body.group_by)
        name = body.name or f"Search {body.query}"
        saved = write_package(settings.exports_dir, package, name=name, query=body.query, data_files=data_files, notes=notes,
                              description=f"Cross-database search of {len(result.sources)} sources, split by {body.group_by}")
        registry.audit.record_export(name=name, path=str(saved.folder), n_rows=saved.n_rows,
                                     columns=[f["file"] for f in saved.files])
        entry = next((e for e in list_exports(settings.exports_dir, registry.audit, 1000) if e["export_id"] == saved.export_id), None)
        return entry or {"export_id": saved.export_id}

    # ----------------------------------------------------------------- species
    @app.get("/api/species/meta", tags=["species"])
    async def species_meta() -> dict[str, Any]:
        from ..species import DEFAULT_SPECIES, SPECIES

        return {"species": [s.model_dump() for s in SPECIES], "default": DEFAULT_SPECIES}

    @app.get("/api/species", tags=["species"])
    async def species_list(limit: Annotated[int, Query(ge=1, le=500)] = 50) -> dict[str, Any]:
        from ..species.store import list_comparisons

        items = list_comparisons(settings, limit)
        return {"total": len(items), "comparisons": items}

    @app.get("/api/species/{comparison_id}", tags=["species"])
    async def species_get(comparison_id: str) -> dict[str, Any]:
        from ..species.store import load_comparison

        comparison = load_comparison(settings, comparison_id)
        if comparison is None:
            raise ApiError(404, "no such cross-species comparison")
        return comparison.model_dump(mode="json", exclude={"records"})

    @app.post("/api/species/compare", tags=["species"])
    async def species_compare(body: SpeciesCompareRequest) -> dict[str, Any]:
        """Compare a drug / compound across species: target ortholog similarity + evidence per species."""
        from ..species import SPECIES_BY_KEY
        from ..species.service import run_comparison

        unknown = [k for k in body.species or [] if k not in SPECIES_BY_KEY]
        if unknown:
            raise ApiError(400, f"unknown species: {', '.join(unknown)}", species=sorted(SPECIES_BY_KEY))
        registry = state.require_registry()
        try:
            comparison = await run_comparison(
                registry, settings, body.query, species=body.species, max_papers=body.max_papers,
                extract_with_claude=body.extract_with_claude,
            )
        except SourceError as exc:
            raise source_api_error(exc, state.clean) from exc
        return comparison.model_dump(mode="json", exclude={"records"})

    # ------------------------------------------------------------------- audit
    @app.get("/api/audit/tool-calls", tags=["audit"])
    async def audit_tool_calls(
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        session_id: Annotated[str | None, Query(max_length=128)] = None,
        source: Annotated[str | None, Query(max_length=64)] = None,
    ) -> dict[str, Any]:
        registry = state.require_registry()
        rows = registry.audit.recent_tool_calls(limit=limit, session_id=session_id or None, source=source or None)
        for row in rows:
            row["params"] = _json_field(row.get("params"))
            row["ok"] = bool(row.get("ok"))
            row["error"] = state.clean(row.get("error"))
            row["request_url"] = state.clean(row.get("request_url"))
        return {"total": len(rows), "tool_calls": rows}

    @app.get("/api/audit/agent-turns", tags=["audit"])
    async def audit_agent_turns(
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        session_id: Annotated[str | None, Query(max_length=128)] = None,
    ) -> dict[str, Any]:
        registry = state.require_registry()
        rows = registry.audit.recent_agent_turns(limit=limit, session_id=session_id or None)
        for row in rows:
            row["detail"] = _json_field(row.get("detail"))
        return {"total": len(rows), "agent_turns": rows}

    @app.get("/api/audit/stats", tags=["audit"])
    async def audit_stats(since_hours: Annotated[float, Query(gt=0, le=24 * 366)] = 24.0) -> dict[str, Any]:
        registry = state.require_registry()
        window = since_hours * 3600
        cutoff = time.time() - window
        per_source = registry.audit.source_stats(since_seconds=window)
        calls = sum(int(s.get("calls") or 0) for s in per_source)
        ok_calls = sum(int(s.get("ok_calls") or 0) for s in per_source)
        weighted = sum(float(s.get("avg_latency_ms") or 0) * int(s.get("calls") or 0) for s in per_source)
        for item in per_source:
            item["failed_calls"] = int(item.get("calls") or 0) - int(item.get("ok_calls") or 0)
        turns = [t for t in registry.audit.recent_agent_turns(limit=100_000) if (t.get("ts") or 0) >= cutoff]
        exports_in_window = [e for e in registry.audit.recent_exports(limit=100_000) if (e.get("ts") or 0) >= cutoff]

        def total(key: str) -> int:
            return sum(int(t.get(key) or 0) for t in turns)

        return {
            "since_hours": since_hours,
            "tool_calls": {
                "calls": calls,
                "ok": ok_calls,
                "failed": calls - ok_calls,
                "success_rate": round(ok_calls / calls, 4) if calls else None,
                "avg_latency_ms": round(weighted / calls, 1) if calls else None,
            },
            "sources": per_source,
            "agent": {
                "turns": len(turns),
                "sessions": len({t.get("session_id") for t in turns}),
                "input_tokens": total("input_tokens"),
                "output_tokens": total("output_tokens"),
                "cache_read_tokens": total("cache_read_tokens"),
                "cache_write_tokens": total("cache_write_tokens"),
                "tool_calls": total("tool_calls"),
            },
            "exports": len(exports_in_window),
        }

    # ------------------------------------------------------------------ web UI
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        index_path = WEB_DIR / "index.html"
        if not index_path.is_file():
            raise ApiError(404, "the web UI (mosaic/web/index.html) is missing")
        return FileResponse(index_path, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})

    if WEB_DIR.is_dir():
        app.mount("/static", _NoCacheStaticFiles(directory=WEB_DIR), name="static")

    return app
