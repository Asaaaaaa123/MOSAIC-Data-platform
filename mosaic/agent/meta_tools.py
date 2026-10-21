"""Meta tools: Mosaic's own tools, always loaded (never deferred).

A meta tool is an async handler taking a :class:`ToolContext` and one pydantic
params model, registered on a :class:`MetaToolRegistry`. The params model is
the JSON schema Claude sees and is validated before the handler runs, the same
guarantee ``Connector.call`` gives connector tools. Adding a tool::

    @BUILTIN_META_TOOLS.tool(description="Resolve a gene, drug or disease name to identifiers ...")
    async def resolve_entity(ctx: ToolContext, params: ResolveEntityParams) -> MetaResult:
        ...
        return MetaResult(text=json.dumps(payload), summary="...", payload=payload)

or build a registry with ``default_meta_tools()`` and pass it to
``MosaicAgent(meta_tools=...)``. Names must not contain ``__`` (reserved for
``<source>__<tool>`` connector tools).

Every result is stored under a session-scoped handle by the agent; handlers
return the payload to store and the text Claude reads.
"""

from __future__ import annotations

import inspect
import json
import typing
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterator, Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError

from ..catalog.catalog import RepositoryCatalog
from ..config import Settings
from ..connectors.base import CATEGORIES, ToolParams, clean_schema
from ..crosslink.resolver import EntityResolver
from ..models import Record
from ..registry import Registry
from .claude_api import TOOL_RESULT_CHARS, api_error_message
from .events import SpeciesComparisonReady, TableSaved
from .exports import write_export
from ..datapackage import build_package, write_package
from .extraction import MAX_FIELDS, MAX_RECORDS, FieldSpec, SourceRecord, StructuredExtractor
from .results import ResultHandles, ResultKind, StoredResult

CategoryName = Literal[tuple(CATEGORIES)]  # type: ignore[valid-type]

MAX_EXPORT_ROWS = 100_000
RECORD_COLUMNS = [
    "source", "record_id", "type", "title", "date", "authors", "identifiers",
    "quality_grade", "quality_score", "quality_flags", "url", "summary",
]
_TABLE_PREVIEW_ROWS = 30


class ToolInputError(Exception):
    """A problem Claude can fix (bad handle, missing argument). The message is sent back as the error."""


@dataclass
class MetaResult:
    text: str  # what Claude reads (the agent prefixes the result handle)
    summary: str  # one line for the UI
    kind: ResultKind = "data"
    payload: dict[str, Any] | None = None  # stored under the handle; defaults to the parsed text
    n_records: int | None = None
    total: int | None = None
    preview: list[dict[str, Any]] = field(default_factory=list)
    events: list[Any] = field(default_factory=list)  # extra AgentEvents, e.g. TableSaved


@dataclass
class ToolContext:
    registry: Registry
    settings: Settings
    session_id: str
    results: ResultHandles
    extractor: StructuredExtractor
    deferred_tools: bool
    catalog_factory: Callable[[], RepositoryCatalog]

    @property
    def catalog(self) -> RepositoryCatalog:
        return self.catalog_factory()

    def result(self, result_id: str) -> StoredResult:
        stored = self.results.get(self.session_id, result_id)
        if stored is None:
            known = ", ".join(self.results.known(self.session_id)[-20:]) or "none yet"
            raise ToolInputError(f"unknown result_id {result_id!r}; this session has: {known}")
        return stored


Handler = Callable[[ToolContext, Any], Awaitable[MetaResult]]


@dataclass(frozen=True)
class MetaTool:
    name: str
    description: str
    params_model: type[BaseModel]
    handler: Handler

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": clean_schema(self.params_model.model_json_schema()),
        }

    async def run(self, ctx: ToolContext, raw: dict[str, Any]) -> MetaResult:
        try:
            params = self.params_model.model_validate(raw)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'params'}: {e['msg']}" for e in exc.errors()
            )
            raise ToolInputError(f"invalid parameters for {self.name}: {problems}") from exc
        return await self.handler(ctx, params)


class MetaToolRegistry:
    def __init__(self, tools: typing.Iterable[MetaTool] = ()):
        self._tools: dict[str, MetaTool] = {}
        for item in tools:
            self.register(item)

    def register(self, item: MetaTool) -> MetaTool:
        if "__" in item.name:
            raise ValueError(f"meta tool name {item.name!r} must not contain '__' (reserved for connector tools)")
        if item.name in self._tools:
            raise ValueError(f"meta tool {item.name!r} is already registered")
        self._tools[item.name] = item
        return item

    def tool(self, name: str | None = None, *, description: str) -> Callable[[Handler], Handler]:
        """Decorator: register ``async def handler(ctx, params: Model) -> MetaResult``."""

        def decorator(func: Handler) -> Handler:
            if not inspect.iscoroutinefunction(func):
                raise TypeError(f"meta tool {func.__qualname__} must be async")
            hints = typing.get_type_hints(func)
            params = list(inspect.signature(func).parameters.values())
            model = hints.get(params[1].name) if len(params) == 2 else None
            if not (inspect.isclass(model) and issubclass(model, BaseModel)):
                raise TypeError(f"meta tool {func.__qualname__} must take (ctx, params: <pydantic model>)")
            text = " ".join(inspect.cleandoc(description).split())
            self.register(MetaTool(name or func.__name__, text, model, func))
            return func

        return decorator

    def get(self, name: str) -> MetaTool | None:
        return self._tools.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[MetaTool]:
        return iter(sorted(self._tools.values(), key=lambda t: t.name))

    def names(self) -> list[str]:
        return sorted(self._tools)

    def copy(self) -> "MetaToolRegistry":
        return MetaToolRegistry(self._tools.values())


BUILTIN_META_TOOLS = MetaToolRegistry()


def default_meta_tools() -> MetaToolRegistry:
    """A fresh registry with the built-in tools (extend it without touching the global one)."""
    return BUILTIN_META_TOOLS.copy()


# ---------------------------------------------------------------- helpers
def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def record_preview(records: list[Record], n: int = 3) -> list[dict[str, Any]]:
    return [r.compact(summary_chars=200) for r in records[:n]]


def _category_order(category: str) -> int:
    keys = list(CATEGORIES)
    return keys.index(category) if category in keys else len(keys)


def records_table(records: list[Record]) -> tuple[list[str], list[dict[str, Any]]]:
    rows = [
        {
            "source": r.source,
            "record_id": r.id,
            "type": r.type,
            "title": r.title,
            "date": r.date,
            "authors": "; ".join(r.authors) or None,
            "identifiers": "; ".join(f"{k}:{v}" for k, v in sorted(r.identifiers.items())) or None,
            "quality_grade": (r.quality or {}).get("grade"),
            "quality_score": (r.quality or {}).get("score"),
            "quality_flags": "; ".join((r.quality or {}).get("flags") or []) or None,
            "url": r.url,
            "summary": r.summary,
        }
        for r in records
    ]
    return list(RECORD_COLUMNS), rows


def _table_text(columns: list[str], rows: list[dict[str, Any]], extra: dict[str, Any]) -> str:
    """Rows Claude reads, trimmed to the tool-result budget."""
    shown = rows[:_TABLE_PREVIEW_ROWS]
    while True:
        payload = {**extra, "columns": columns, "n_rows": len(rows), "rows_shown": len(shown), "rows": shown}
        text = _json(payload)
        if len(text) <= TOOL_RESULT_CHARS or not shown:
            return text[:TOOL_RESULT_CHARS]
        shown = shown[: len(shown) // 2]


# ---------------------------------------------------------------- list_sources
class ListSourcesParams(ToolParams):
    category: CategoryName | None = Field(None, description="Only list sources in this category")


@BUILTIN_META_TOOLS.tool(
    description="""
    List the databases Mosaic can query live, grouped by category, with what each covers and the
    names of its tools (<source>__<tool>). Also lists sources that exist but are not configured
    (missing API keys). Call this when choosing which databases fit a question, or when the user
    asks what Mosaic can access.
    """
)
async def list_sources(ctx: ToolContext, params: ListSourcesParams) -> MetaResult:
    infos = sorted(ctx.registry.sources(params.category), key=lambda s: (_category_order(s.category), s.name))
    live = [
        {
            "name": s.name,
            "title": s.title,
            "category": s.category,
            "description": s.description,
            "tools": [t.qualified_name for t in s.tools],
            **({"nih_repository": s.nih_repository} if s.nih_repository else {}),
        }
        for s in infos
        if s.configured
    ]
    missing = [
        {"name": s.name, "title": s.title, "category": s.category, "missing_settings": s.missing_settings}
        for s in infos
        if not s.configured
    ]
    payload: dict[str, Any] = {
        "categories": {k: v for k, v in CATEGORIES.items() if params.category in (None, k)},
        "sources": live,
        "not_configured": missing,
    }
    if ctx.registry.load_errors and params.category is None:
        payload["failed_to_load"] = sorted(ctx.registry.load_errors)
    summary = f"{len(live)} configured source(s)" + (f", {len(missing)} not configured" if missing else "")
    return MetaResult(text=_json(payload), summary=summary, payload=payload)


# ---------------------------------------------------------------- describe_source
class DescribeSourceParams(ToolParams):
    source: str = Field(min_length=1, max_length=64, description="Source name from list_sources, e.g. 'pubmed'")


@BUILTIN_META_TOOLS.tool(
    description="""
    Show one source in detail: coverage, access/licensing notes, configuration status and the full
    input schema of every tool it offers. Call this before using a source you have not used yet in
    this conversation, or when a tool call failed because of its parameters.
    """
)
async def describe_source(ctx: ToolContext, params: DescribeSourceParams) -> MetaResult:
    connector = ctx.registry.get(params.source.strip().lower())
    info = connector.info()
    payload: dict[str, Any] = {
        "name": info.name,
        "title": info.title,
        "category": info.category,
        "description": info.description,
        "homepage": info.homepage,
        "docs_url": info.docs_url,
        "auth": info.auth,
        "configured": info.configured,
        "missing_settings": info.missing_settings,
        "nih_repository": info.nih_repository,
        "tools": [
            {
                "name": t.qualified_name,
                "description": t.description,
                "input_schema": t.input_schema,
                "federated_search": t.federated,
            }
            for t in info.tools
        ],
    }
    if not info.configured:
        payload["note"] = f"Not configured: set {', '.join(info.missing_settings)} in .env to enable it."
    elif ctx.deferred_tools:
        payload["note"] = (
            "These tools load on demand: search the tool catalog with the source and task "
            f"(for example '{info.name} search') to load them before calling."
        )
    payload = {k: v for k, v in payload.items() if v not in (None, [], "")}
    return MetaResult(text=_json(payload), summary=f"{info.name}: {len(info.tools)} tool(s)", payload=payload)


# ---------------------------------------------------------------- search_repository_catalog
class CatalogParams(ToolParams):
    query: str | None = Field(
        None, max_length=500, description="Free text: disease, data type, assay, organism, program or repository name"
    )
    kind: Literal["domain_specific", "generalist"] | None = Field(None, description="Repository kind")
    access_type: Literal["open", "registered", "controlled"] | None = Field(
        None, description="Only repositories that offer this access type"
    )
    institute: str | None = Field(None, max_length=20, description="NIH institute or center, e.g. NCI, NHLBI, NIMH, NIA")
    has_connector: bool | None = Field(
        None, description="true: only repositories Mosaic queries live; false: only those without a connector"
    )
    limit: int = Field(10, ge=1, le=50, description="Maximum repositories to return")


def _how_to_access(repo: dict[str, Any]) -> str:
    connectors = repo.get("connectors") or []
    if connectors:
        return f"Live in Mosaic: query it with the {', '.join(connectors)} tools."
    access = (repo.get("access_type") or "").lower()
    parts = ["No Mosaic connector: send the user to data_access_url (web_fetch can read its public pages)."]
    if "controlled" in access:
        parts.append(
            "Controlled-access data need an approved data access request (for example through dbGaP); "
            "Mosaic cannot retrieve them."
        )
    if "registered" in access:
        parts.append("Registered access needs a (usually free) account.")
    return " ".join(parts)


@BUILTIN_META_TOOLS.tool(
    description="""
    Search the NIH catalog of 232 data repositories (223 NIH-supported domain-specific repositories
    plus the 9 NIH generalist repositories such as Zenodo, Figshare, Dryad, Dataverse and OSF).
    Each result says whether Mosaic has a live connector for it or the user must go to its
    data_access_url, and whether access is open, registered or controlled. Call this when the user
    asks where data of some kind are deposited, which repository to use or submit to, or about a
    repository Mosaic may not query directly.
    """
)
async def search_repository_catalog(ctx: ToolContext, params: CatalogParams) -> MetaResult:
    catalog = ctx.catalog
    repos = catalog.search(
        params.query,
        kind=params.kind,
        access_type=params.access_type,
        institute=params.institute,
        has_connector=params.has_connector,
        limit=params.limit,
    )
    items = []
    for repo in repos:
        description = repo.get("description") or ""
        items.append(
            {
                "name": repo["name"],
                "kind": repo["kind"],
                "subject_area": repo.get("subject_area"),
                "institutes": repo.get("institutes") or [],
                "access_type": repo.get("access_type"),
                "model_system": repo.get("model_system"),
                "url": repo.get("url"),
                "data_access_url": repo.get("data_access_url"),
                "description": description if len(description) <= 400 else description[:400].rstrip() + " ...",
                "mosaic_connectors": repo.get("connectors") or [],
                "how_to_access": _how_to_access(repo),
            }
        )
    payload = {"query": params.query, "stats": catalog.stats(), "repositories": items}
    live = sum(1 for i in items if i["mosaic_connectors"])
    return MetaResult(
        text=_json(payload),
        summary=f"NIH catalog: {len(items)} repositor{'y' if len(items) == 1 else 'ies'} ({live} live in Mosaic)",
        payload=payload,
        n_records=len(items),
        preview=[{"name": i["name"], "access_type": i["access_type"], "live": bool(i["mosaic_connectors"])} for i in items[:3]],
    )


# ---------------------------------------------------------------- federated_search
class FederatedSearchParams(ToolParams):
    query: str = Field(min_length=1, max_length=500, description="Search terms, ideally including identifiers")
    categories: list[CategoryName] | None = Field(None, description="Only sources in these categories")
    sources: list[str] | None = Field(None, max_length=50, description="Exactly these sources (overrides categories)")
    limit_per_source: int = Field(5, ge=1, le=20, description="Records per source")


@BUILTIN_META_TOOLS.tool(
    description="""
    Run the same query against many databases at once (each source's primary search, in parallel)
    and link the results: records from different databases that share an identifier (DOI, PMID,
    NCT ID, ChEMBL ID, UniProt accession, InChIKey ...) are grouped under linked_entities. Call this
    for a broad first pass across sources or categories; then use individual source tools for depth.
    Failed sources are reported per source.
    """
)
async def federated_search(ctx: ToolContext, params: FederatedSearchParams) -> MetaResult:
    result = await ctx.registry.federated_search(
        params.query,
        sources=params.sources or None,
        categories=params.categories or None,
        limit_per_source=params.limit_per_source,
        session_id=ctx.session_id,
    )
    if not result.sources:
        raise ToolInputError("no configured source with a federated search matches those categories/sources")
    ok = sum(1 for s in result.sources if s.ok)
    linked = sum(1 for c in result.clusters if len(c["sources"]) > 1)
    return MetaResult(
        text=result.to_llm(max_chars=TOOL_RESULT_CHARS),
        summary=(
            f"federated: {ok}/{len(result.sources)} sources ok, {len(result.records)} records, "
            f"{linked} linked across databases"
        ),
        kind="records",
        payload=result.model_dump(mode="json"),
        n_records=len(result.records),
        preview=record_preview(result.records),
    )


# ---------------------------------------------------------------- resolve_entity
class ResolveEntityParams(ToolParams):
    kind: Literal["gene", "drug", "disease", "article", "variant"] = Field(description="What the query names")
    query: str = Field(
        min_length=1,
        max_length=300,
        description=(
            "Name or identifier: gene symbol (BRAF), drug name or InChIKey (imatinib), disease name "
            "(cystic fibrosis), article DOI / PMID / PMCID or title, variant rsID or gnomAD id (7-140753336-A-T)"
        ),
    )


@BUILTIN_META_TOOLS.tool(
    description="""
    Pin an entity to its identifiers across databases before searching them. Asks the authoritative
    sources in parallel (genes: HGNC, UniProt, NCBI Gene, Open Targets; drugs: PubChem, ChEMBL,
    RxNorm, DrugCentral, Open Targets; diseases: MONDO/EFO/DOID via OLS, Monarch, Open Targets, MeSH;
    articles: Crossref, OpenAlex, Semantic Scholar, Europe PMC, Unpaywall, PubMed; variants: gnomAD,
    dbSNP, ClinVar) and returns the identifiers they agree on, which sources agree, and conflicts.
    Call this first whenever a question names a gene, drug, disease, paper or variant, then query
    other sources with those identifiers (ChEMBL ID, UniProt accession, MONDO/EFO ID, DOI, rsID...).
    """
)
async def resolve_entity(ctx: ToolContext, params: ResolveEntityParams) -> MetaResult:
    result = await EntityResolver(ctx.registry).resolve(params.kind, params.query, session_id=ctx.session_id)
    payload = result.model_dump(mode="json")
    payload["records"] = [r.model_dump(mode="json") for r in result.records]
    text_payload = {k: v for k, v in payload.items() if k != "records"}
    agreed = sum(1 for a in result.agreement.values() if len(a.sources) > 1)
    return MetaResult(
        text=_json(text_payload),
        summary=(
            f"resolve {params.kind} '{params.query}': {len(result.identifiers)} identifiers from "
            f"{len(result.candidates)} sources ({agreed} cross-confirmed, {len(result.conflicts)} conflicts)"
        ),
        kind="records",
        payload=payload,
        n_records=len(result.records),
        preview=[{"key": k, "value": v} for k, v in list(result.identifiers.items())[:3]],
    )


# ---------------------------------------------------------------- extract_structured
class ExtractStructuredParams(ToolParams):
    result_ids: list[str] = Field(
        min_length=1, max_length=50, description="Handles of earlier search/fetch results (e.g. ['r1', 'r4'])"
    )
    fields: list[FieldSpec] = Field(min_length=1, max_length=MAX_FIELDS, description="Columns to extract per record")
    instructions: str | None = Field(
        None, max_length=4000, description="Extra guidance: units, vocabularies, how to treat ambiguous values"
    )


@BUILTIN_META_TOOLS.tool(
    description=f"""
    Build a table from the full records behind earlier result handles: for every record, extract the
    fields you define (name, type, description). Runs a separate structured-output extraction over
    complete abstracts and attributes (not the trimmed text you saw) and attaches provenance per row
    (source, record_id, title, url). Returns a new result_id holding the table; pass it to save_table
    to export CSV/JSON. Call this when the user asks to extract, tabulate or compare specific values
    across many records. Up to {MAX_RECORDS} records and {MAX_FIELDS} fields per call.
    """
)
async def extract_structured(ctx: ToolContext, params: ExtractStructuredParams) -> MetaResult:
    names = [f.name for f in params.fields]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ToolInputError(f"duplicate field names: {', '.join(duplicates)}")
    items: list[SourceRecord] = []
    seen: set[tuple[str, str]] = set()
    for result_id in dict.fromkeys(r.strip() for r in params.result_ids):
        stored = ctx.result(result_id)
        if stored.kind != "records":
            raise ToolInputError(f"{result_id} holds a {stored.kind} result ({stored.tool}), not database records")
        for record in stored.records():
            key = (record.source, record.id)
            if key not in seen:
                seen.add(key)
                items.append(SourceRecord(stored.result_id, record))
    if not items:
        raise ToolInputError("those results contain no records to extract from")
    if len(items) > MAX_RECORDS:
        raise ToolInputError(f"{len(items)} records exceeds the {MAX_RECORDS}-record limit; extract in batches")
    try:
        table = await ctx.extractor.extract(items, params.fields, params.instructions, session_id=ctx.session_id)
    except anthropic.APIError as exc:
        message, _ = api_error_message(exc, ctx.extractor.model)
        raise ToolInputError(f"extraction call failed: {message}") from exc
    payload = {
        "columns": table.columns,
        "rows": table.rows,
        "provenance": table.provenance,
        "notes": table.notes,
    }
    text = _table_text(
        table.columns,
        table.rows,
        {"notes": table.notes, "next_step": "Pass this result_id to save_table to export the full table."},
    )
    return MetaResult(
        text=text,
        summary=f"extracted {len(params.fields)} field(s) from {len(table.rows)} record(s)",
        kind="table",
        payload=payload,
        n_records=len(table.rows),
        preview=table.rows[:3],
    )


# ---------------------------------------------------------------- save_table
class SaveTableParams(ToolParams):
    name: str = Field(min_length=1, max_length=200, description="Short descriptive table name")
    columns: list[str] | None = Field(
        None,
        max_length=200,
        description="Column order. Required with rows; with result_id it selects and orders columns",
    )
    rows: list[dict[str, Any] | list[Any]] | None = Field(
        None, description="Rows, each an object keyed by column or an array aligned with columns"
    )
    result_id: str | None = Field(
        None, description="Save the table from extract_structured, or the records of any search result, instead of rows"
    )
    description: str | None = Field(None, max_length=4000, description="What the table contains and how it was built")
    derived_from: list[str] | None = Field(
        None, max_length=100, description="Result handles the explicit rows were built from (recorded as provenance)"
    )


def _explicit_rows(columns: list[str], rows: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if isinstance(row, dict):
            unknown = sorted(set(row) - set(columns))
            if unknown:
                raise ToolInputError(f"row {index} has keys not in columns: {', '.join(unknown)}")
            normalized.append({c: row.get(c) for c in columns})
        else:
            if len(row) != len(columns):
                raise ToolInputError(f"row {index} has {len(row)} values but there are {len(columns)} columns")
            normalized.append(dict(zip(columns, row)))
    return normalized


@BUILTIN_META_TOOLS.tool(
    description="""
    Save ONE table as CSV and JSON: a table from extract_structured (result_id) or a summary
    table you assembled (columns plus rows). For exporting the data you found (papers, trials,
    drugs, datasets ... as separate files) use export_dataset instead; use save_table in addition
    only when the user wants a specific custom table. Include provenance columns (source, id, url).
    """
)
async def save_table(ctx: ToolContext, params: SaveTableParams) -> MetaResult:
    if params.result_id and params.rows is not None:
        raise ToolInputError("pass either rows or result_id, not both")
    if params.result_id:
        stored = ctx.result(params.result_id)
        if stored.kind == "table":
            columns = list(stored.payload.get("columns") or [])
            rows = list(stored.payload.get("rows") or [])
            provenance = {**(stored.payload.get("provenance") or {}), "result_id": stored.result_id}
        elif stored.kind == "records":
            columns, rows = records_table(stored.records())
            provenance = {
                "method": "records",
                "result_id": stored.result_id,
                "tool": stored.tool,
                "query": stored.payload.get("query"),
            }
        else:
            raise ToolInputError(f"{stored.result_id} holds {stored.tool} output, which is not a table or records")
        if params.columns:
            unknown = [c for c in params.columns if c not in columns]
            if unknown:
                raise ToolInputError(f"unknown columns {unknown}; available: {columns}")
            columns = list(params.columns)
    elif params.rows is not None:
        if not params.columns:
            raise ToolInputError("columns is required when passing rows")
        columns = list(params.columns)
        rows = _explicit_rows(columns, params.rows)
        provenance = {"method": "rows", "derived_from": params.derived_from or []}
    else:
        raise ToolInputError("pass rows (with columns) or a result_id")
    if not columns:
        raise ToolInputError("the table has no columns")
    if len(set(columns)) != len(columns):
        raise ToolInputError("column names must be unique")
    if len(rows) > MAX_EXPORT_ROWS:
        raise ToolInputError(f"{len(rows)} rows exceeds the {MAX_EXPORT_ROWS}-row limit")

    saved = write_export(
        ctx.settings.exports_dir,
        name=params.name,
        columns=columns,
        rows=rows,
        description=params.description,
        session_id=ctx.session_id,
        provenance=provenance,
    )
    ctx.registry.audit.record_export(
        name=params.name,
        path=str(saved.csv_path),
        n_rows=saved.n_rows,
        columns=saved.columns,
        session_id=ctx.session_id,
    )
    payload = {
        "export_id": saved.export_id,
        "name": saved.name,
        "n_rows": saved.n_rows,
        "columns": saved.columns,
        "csv_url": saved.csv_url,
        "json_url": saved.json_url,
    }
    event = TableSaved(
        session_id=ctx.session_id,
        export_id=saved.export_id,
        name=saved.name,
        n_rows=saved.n_rows,
        columns=saved.columns,
        csv_url=saved.csv_url,
        json_url=saved.json_url,
    )
    text = _json({**payload, "note": "Saved. The user sees download links in the interface."})
    return MetaResult(
        text=text,
        summary=f"saved table '{saved.name}': {saved.n_rows} rows x {len(saved.columns)} columns",
        payload=payload,
        n_records=saved.n_rows,
        preview=rows[:3],
        events=[event],
    )


# ---------------------------------------------------------------- export_dataset
class ExportDatasetParams(ToolParams):
    name: str = Field(min_length=1, max_length=120, description="Short name for the package, e.g. 'KRAS G12C inhibitors'")
    group_by: Literal["type", "species", "source"] = Field(
        "type",
        description=(
            "How to split the data into files: 'type' (papers, clinical_trials, projects_studies, "
            "datasets_repositories, drugs_compounds, safety_regulatory, genes_proteins, ...), 'species' "
            "(human, mouse, rat, zebrafish, fly, worm, yeast, bird, plant, ...) or 'source' (one per database)"
        ),
    )
    result_ids: list[str] | None = Field(
        None, max_length=500, description="Result handles to include (default: every records result in this conversation)"
    )
    description: str | None = Field(None, max_length=2000, description="What the package contains")
    fetch_full_records: bool = Field(
        True, description="Re-fetch each search hit's complete record from its source first (default true: real data, not hits)"
    )
    download_data_files: bool = Field(
        True, description="List every dataset file and include small open data tables (CSV/TSV/Excel/JSON) in the package"
    )


@BUILTIN_META_TOOLS.tool(
    description="""
    Export the real data found in this conversation as a data package the user can open: one CSV per
    group plus an Excel workbook (one sheet per group), a README and a ZIP. Records are cleaned,
    merged across databases and quality-graded first; each file has the columns that fit its rows
    (identifiers, cleaned fields, quality grade and flags, sources). Call this whenever the user asks
    to save, export or download data or tables, after gathering the records (fetch full records
    first when the search hits lack the fields they need). group_by: 'type' (default), 'species' when
    the user wants human / mouse / plant ... separated, or 'source'. Then tell the user which files
    were created and how many rows each has.
    """
)
async def export_dataset(ctx: ToolContext, params: ExportDatasetParams) -> MetaResult:
    handles = params.result_ids or ctx.results.known(ctx.session_id)
    records: list[Record] = []
    used: list[str] = []
    for result_id in handles:
        stored = ctx.result(result_id)
        if stored.kind != "records":
            continue
        found = stored.records()
        if found:
            records.extend(found)
            used.append(stored.result_id)
    if not records:
        raise ToolInputError("no records to export: search or fetch data first (tables from extract_structured go to save_table)")
    from ..datafiles import collect_data_files
    from ..enrich import enrich_records

    notes: list[str] = []
    if params.fetch_full_records:
        records, enrich_stats = await enrich_records(ctx.registry, records, session_id=ctx.session_id)
        notes = enrich_stats.notes()
    data_files = await collect_data_files(ctx.registry.http, records, download=params.download_data_files)
    package = build_package(records, params.group_by)
    saved = write_package(
        ctx.settings.exports_dir, package, name=params.name, description=params.description, session_id=ctx.session_id,
        data_files=data_files, notes=notes,
    )
    ctx.registry.audit.record_export(
        name=params.name, path=str(saved.folder), n_rows=saved.n_rows,
        columns=[f["file"] for f in saved.files], session_id=ctx.session_id,
    )
    files = [{**{k: f[k] for k in ("file", "group", "title", "rows")}, "url": saved.url(f["file"])} for f in saved.files]
    event = TableSaved(
        session_id=ctx.session_id,
        export_id=saved.export_id,
        name=saved.name,
        n_rows=saved.n_rows,
        columns=[f["file"] for f in saved.files],
        csv_url=files[0]["url"],
        json_url=saved.url("manifest.json"),
        kind="package",
        files=files,
        xlsx_url=saved.url("mosaic_data.xlsx"),
        zip_url=saved.url("mosaic_data.zip"),
        folder=str(saved.folder),
    )
    payload = {
        "export_id": saved.export_id,
        "name": saved.name,
        "group_by": params.group_by,
        "files": [{k: f[k] for k in ("file", "title", "rows")} for f in files],
        "workbook": "mosaic_data.xlsx",
        "stats": {k: package.stats.get(k) for k in ("records_in", "entities_out", "duplicates_merged", "grades")},
        "from_results": used,
    }
    text = _json({**payload, "note": "Saved. The user sees download links for every file, the Excel workbook and a ZIP."})
    return MetaResult(
        text=text,
        summary=f"exported '{saved.name}': {len(files)} file(s), {saved.n_rows} rows (split by {params.group_by})",
        payload=payload,
        n_records=saved.n_rows,
        preview=[{"file": f["file"], "rows": f["rows"]} for f in files[:3]],
        events=[event],
    )


# ---------------------------------------------------------------- compare_species
class CompareSpeciesParams(ToolParams):
    query: str = Field(min_length=1, max_length=200, description="Drug, medicine or chemical compound (name, ChEMBL ID or InChIKey)")
    species: list[str] | None = Field(
        None, max_length=20,
        description="Species keys to compare with human (default all): macaque, mouse, rat, dog, pig, chicken, frog, "
                    "zebrafish, fly, worm, yeast, arabidopsis",
    )
    max_papers: int = Field(5, ge=1, le=20, description="Top PubMed papers kept per species")
    extract_with_claude: bool = Field(
        False, description="Also extract model / dose / effect from the paper abstracts (slower, uses extra API credits)"
    )


@BUILTIN_META_TOOLS.tool(
    description="""
    Compare a drug, medicine or chemical compound between human and other species (macaque, mouse, rat,
    dog, pig, chicken, frog, zebrafish, fly, worm, yeast, Arabidopsis). Returns its human targets, the
    sequence identity / similarity % of each target's ortholog in every species, and the evidence per
    species: PubMed studies (counts and top papers), ChEMBL potency, pharmacokinetic, toxicity and in vivo
    measurements with doses, plus human clinical trials. The user sees an interactive dashboard (overview
    graph, heatmap, charts, paper list) and gets a data package with one CSV per species. Call this when the
    user asks how a compound or treatment works, is used or is tested in other species, or to compare
    human with animal models; then summarize the key differences with the numbers and references.
    """
)
async def compare_species(ctx: ToolContext, params: CompareSpeciesParams) -> MetaResult:
    from ..species import SPECIES_BY_KEY
    from ..species.service import event_summary, run_comparison

    unknown = [k for k in params.species or [] if k not in SPECIES_BY_KEY]
    if unknown:
        raise ToolInputError(f"unknown species {unknown}; available: {', '.join(SPECIES_BY_KEY)}")
    comparison = await run_comparison(
        ctx.registry, ctx.settings, params.query, species=params.species, max_papers=params.max_papers,
        extract_with_claude=params.extract_with_claude, session_id=ctx.session_id,
    )
    title = f"Cross-species: {comparison.compound.name or params.query}"
    events: list[Any] = [
        SpeciesComparisonReady(
            session_id=ctx.session_id,
            comparison_id=comparison.comparison_id,
            title=title,
            url=f"/#species?id={comparison.comparison_id}",
            summary=event_summary(comparison),
        )
    ]
    pkg = comparison.package
    if pkg and pkg.get("files"):
        events.append(TableSaved(
            session_id=ctx.session_id, export_id=pkg["export_id"], name=pkg["name"], n_rows=pkg["n_rows"],
            columns=[f["file"] for f in pkg["files"]], csv_url=pkg["files"][0]["url"],
            json_url=f"/api/exports/{pkg['export_id']}/manifest.json", kind="package", files=pkg["files"],
            xlsx_url=pkg["xlsx_url"], zip_url=pkg["zip_url"], folder=pkg.get("folder"),
        ))
    payload = comparison.model_dump(mode="json", exclude={"records"})
    payload["records"] = [r.model_dump(mode="json") for r in comparison.records]
    rows = ", ".join(
        f"{s.label} {s.mean_identity_pct:.0f}%" if s.mean_identity_pct is not None else f"{s.label} -"
        for s in comparison.species[:6]
    )
    return MetaResult(
        text=comparison.to_llm(),
        summary=f"{title}: {len(comparison.targets)} target(s), {len(comparison.species)} species ({rows} ...)",
        kind="records",
        payload=payload,
        n_records=len(comparison.records),
        preview=[{"species": s.label, "identity_pct": s.mean_identity_pct, "papers": s.n_papers} for s in comparison.species[:3]],
        events=events,
    )

