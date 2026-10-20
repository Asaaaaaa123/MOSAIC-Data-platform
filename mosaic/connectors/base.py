"""Connector base class and the ``@tool`` decorator.

A connector wraps one upstream database. Each public capability is an async
method decorated with :func:`tool`, taking exactly one pydantic params model and
returning a :class:`~mosaic.models.ToolResult`. The decorator turns the params
model into the JSON schema Claude sees, and :meth:`Connector.call` validates
inputs against it before the method runs.

Minimal example::

    class SearchWorks(SearchParams):
        year_from: int | None = Field(None, description="Earliest publication year")

    class Crossref(Connector):
        name = "crossref"
        title = "Crossref"
        category = "literature"
        description = "DOI registration metadata for 150M+ scholarly works."
        homepage = "https://www.crossref.org/"

        @tool(description="Search Crossref works by free text ...", federated=True)
        async def search_works(self, params: SearchWorks) -> ToolResult:
            payload = await self.get_json("https://api.crossref.org/works", params={...})
            return ToolResult(records=[...], total=...)

Connector modules are discovered automatically: any module in
``mosaic/connectors/`` that defines a ``Connector`` subclass with a ``name`` is
registered. Do not edit a central list.
"""

from __future__ import annotations

import inspect
import time
import typing
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import Settings
from ..http import HttpClient, HttpResponse, NotConfiguredError, SourceError
from ..models import HealthResult, SourceInfo, ToolResult, ToolSpec

CATEGORIES: dict[str, str] = {
    "literature": "Literature, citations & scholarly metadata",
    "clinical": "Clinical trials, regulatory & safety",
    "drugs": "Drugs & pharmacology",
    "chemistry": "Chemistry & bioactivity",
    "proteins": "Targets, proteins & structures",
    "genomics": "Genomics, variants, expression & cancer genomics",
    "biology": "Pathways, interactions, ontologies & model organisms",
    "datasets": "Data repositories (NIH generalist & domain-specific)",
}

MAX_LIMIT = 100


class ToolParams(BaseModel):
    """Base for every tool params model. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class SearchParams(ToolParams):
    """Standard params for a source's primary free-text search.

    A tool marked ``federated=True`` must take this model or a subclass whose
    extra fields all have defaults, so cross-database search can call it with
    just ``query`` and ``limit``.
    """

    query: str = Field(description="Free-text search query")
    limit: int = Field(10, ge=1, le=MAX_LIMIT, description="Maximum number of records to return")
    cursor: str | None = Field(
        None, description="Pagination cursor: pass next_cursor from a previous result to get the next page"
    )


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    params_model: type[BaseModel]
    func: Callable[..., Awaitable[ToolResult]]
    federated: bool = False


def tool(
    name: str | None = None, *, description: str, federated: bool = False
) -> Callable[[Callable[..., Awaitable[ToolResult]]], Callable[..., Awaitable[ToolResult]]]:
    """Mark a connector method as a tool.

    ``description`` is what Claude reads to decide when to call the tool: say what
    it returns, when to use it, and give query-syntax tips. ``federated=True``
    marks the source's primary search (used by cross-database search); at most
    one per connector.
    """

    def decorator(func: Callable[..., Awaitable[ToolResult]]) -> Callable[..., Awaitable[ToolResult]]:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(f"@tool {func.__qualname__} must be async")
        func.__mosaic_tool__ = {  # type: ignore[attr-defined]
            "name": name or func.__name__,
            "description": inspect.cleandoc(description),
            "federated": federated,
        }
        return func

    return decorator


def _params_model(func: Callable[..., Any]) -> type[BaseModel]:
    hints = typing.get_type_hints(func)
    params = [p for p in inspect.signature(func).parameters.values() if p.name != "self"]
    if len(params) != 1:
        raise TypeError(f"@tool {func.__qualname__} must take exactly one params argument")
    model = hints.get(params[0].name)
    if not (inspect.isclass(model) and issubclass(model, BaseModel)):
        raise TypeError(f"@tool {func.__qualname__}: params must be annotated with a pydantic model")
    return model


def clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a pydantic JSON schema compact and Claude-friendly.

    Inlines ``$defs``, drops ``title`` keys and collapses ``Optional[X]``
    (``anyOf: [X, null]``) to ``X``.
    """
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"].split("/")[-1]
                merged = {**defs.get(ref, {}), **{k: v for k, v in node.items() if k != "$ref"}}
                return walk(merged)
            out: dict[str, Any] = {}
            for key, value in node.items():
                if key in ("title", "$defs"):
                    continue
                out[key] = walk(value)
            any_of = out.get("anyOf")
            if isinstance(any_of, list):
                non_null = [s for s in any_of if s != {"type": "null"}]
                if len(non_null) == 1 and len(non_null) < len(any_of):
                    out.pop("anyOf")
                    out = {**non_null[0], **out}
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    cleaned = walk(schema)
    cleaned.setdefault("type", "object")
    cleaned.setdefault("properties", {})
    return cleaned


class Connector:
    # ---- class metadata (override in subclasses) ----
    name: ClassVar[str]
    title: ClassVar[str]
    category: ClassVar[str]
    description: ClassVar[str]
    homepage: ClassVar[str]
    docs_url: ClassVar[str | None] = None
    auth: ClassVar[str] = "none"  # none | api_key | optional_api_key | database
    required_settings: ClassVar[tuple[str, ...]] = ()  # Settings fields / env names that must be set
    rate_limit: ClassVar[float] = 5.0  # requests per second
    rate_burst: ClassVar[float | None] = None
    health_query: ClassVar[str] = "cancer"
    # Exact ``repository_name`` values from mosaic/catalog/nih_repositories.json this connector serves.
    nih_repositories: ClassVar[tuple[str, ...]] = ()

    _tool_defs: ClassVar[dict[str, ToolDef]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        defs: dict[str, ToolDef] = {}
        for base in reversed(cls.__mro__[1:]):
            defs.update(getattr(base, "_tool_defs", {}) or {})
        for attr, func in cls.__dict__.items():
            meta = getattr(func, "__mosaic_tool__", None)
            if meta is None:
                continue
            defs[meta["name"]] = ToolDef(
                name=meta["name"],
                description=meta["description"],
                params_model=_params_model(func),
                func=func,
                federated=meta["federated"],
            )
        cls._tool_defs = defs
        if "name" in cls.__dict__:
            federated = [d.name for d in defs.values() if d.federated]
            if len(federated) > 1:
                raise TypeError(f"{cls.__name__}: at most one federated tool, got {federated}")

    def __init__(self, settings: Settings, http: HttpClient):
        self.settings = settings
        self.http = http
        http.set_rate(self.name, self.effective_rate(), self.rate_burst)

    # ---- configuration ----
    def effective_rate(self) -> float:
        """Requests/second actually allowed (override when a key raises the limit)."""
        return self.rate_limit

    def setting(self, name: str) -> str | None:
        """Read a setting by field name or env var name (``NCBI_API_KEY``)."""
        value = getattr(self.settings, name.lower(), None)
        if value not in (None, ""):
            return str(value)
        return self.settings.env(name)

    def missing_settings(self) -> list[str]:
        return [s.upper() for s in self.required_settings if not self.setting(s)]

    @property
    def configured(self) -> bool:
        return not self.missing_settings()

    def require_configured(self) -> None:
        missing = self.missing_settings()
        if missing:
            raise NotConfiguredError(self.name, f"not configured: set {', '.join(missing)} in .env")

    # ---- tools ----
    @classmethod
    def tool_defs(cls) -> list[ToolDef]:
        return list(cls._tool_defs.values())

    @classmethod
    def federated_tool(cls) -> ToolDef | None:
        return next((d for d in cls._tool_defs.values() if d.federated), None)

    @classmethod
    def tool_specs(cls) -> list[ToolSpec]:
        return [
            ToolSpec(
                source=cls.name,
                name=d.name,
                qualified_name=f"{cls.name}__{d.name}",
                description=d.description,
                input_schema=clean_schema(d.params_model.model_json_schema()),
                federated=d.federated,
            )
            for d in cls._tool_defs.values()
        ]

    async def call(self, tool_name: str, params: dict[str, Any] | None = None) -> ToolResult:
        """Validate ``params`` and run a tool. Raises :class:`SourceError`."""
        tool_def = self._tool_defs.get(tool_name)
        if tool_def is None:
            raise SourceError(
                self.name, f"unknown tool {tool_name!r}; available: {', '.join(self._tool_defs)}"
            )
        self.require_configured()
        try:
            parsed = tool_def.params_model.model_validate(params or {})
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'params'}: {e['msg']}" for e in exc.errors()
            )
            raise SourceError(self.name, f"invalid parameters for {tool_name}: {problems}") from exc
        result = await tool_def.func(self, parsed)
        if not isinstance(result, ToolResult):
            raise SourceError(self.name, f"{tool_name} returned {type(result).__name__}, expected ToolResult")
        result.source = self.name
        result.tool = tool_name
        result.query = parsed.model_dump(exclude_none=True)
        for record in result.records:
            record.source = record.source or self.name
        return result

    # ---- HTTP shortcuts (rate-limited, cached, redacted) ----
    async def get_json(self, url: str, **kwargs: Any) -> Any:
        return await self.http.get_json(self.name, url, **kwargs)

    async def post_json(self, url: str, json_body: Any = None, **kwargs: Any) -> Any:
        return await self.http.post_json(self.name, url, json_body, **kwargs)

    async def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
        return await self.http.request(self.name, method, url, **kwargs)

    async def graphql(self, url: str, query: str, variables: dict[str, Any] | None = None, **kwargs: Any) -> dict:
        return await self.http.graphql(self.name, url, query, variables, **kwargs)

    # ---- health / lifecycle ----
    async def healthcheck(self) -> HealthResult:
        """Default: run the federated search with ``health_query``, limit 1."""
        if not self.configured:
            return HealthResult(source=self.name, ok=False, detail=f"missing {', '.join(self.missing_settings())}")
        tool_def = self.federated_tool() or (self.tool_defs()[0] if self.tool_defs() else None)
        if tool_def is None:
            return HealthResult(source=self.name, ok=False, detail="no tools")
        started = time.perf_counter()
        try:
            result = await self.call(tool_def.name, {"query": self.health_query, "limit": 1})
        except Exception as exc:  # report, never raise
            return HealthResult(
                source=self.name,
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                detail=str(exc)[:300],
            )
        return HealthResult(
            source=self.name,
            ok=True,
            latency_ms=(time.perf_counter() - started) * 1000,
            detail=f"{tool_def.name}: {len(result.records)} record(s), total={result.total}",
        )

    async def aclose(self) -> None:
        """Release resources (DB pools etc.)."""

    def info(self) -> SourceInfo:
        return SourceInfo(
            name=self.name,
            title=self.title,
            category=self.category,
            description=self.description,
            homepage=self.homepage,
            docs_url=self.docs_url,
            auth=self.auth,
            configured=self.configured,
            missing_settings=self.missing_settings(),
            nih_repository=", ".join(self.nih_repositories) or None,
            tools=self.tool_specs(),
        )


def first(value: Any, default: Any = None) -> Any:
    """First element of a list (or the value itself if not a list)."""
    if isinstance(value, list):
        return value[0] if value else default
    return value if value is not None else default


def strip_html(text: str | None) -> str | None:
    """Remove HTML/JATS tags and collapse whitespace."""
    if not text:
        return text
    import html
    import re

    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip() or None


def parse_offset(cursor: str | None) -> int:
    """Interpret a cursor produced by offset pagination."""
    if not cursor:
        return 0
    try:
        return max(0, int(cursor))
    except ValueError:
        return 0
