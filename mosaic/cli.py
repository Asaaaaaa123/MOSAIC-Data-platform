"""Command-line interface: ``mosaic <command>`` (or ``python -m mosaic``).

Commands::

    mosaic serve [--host H] [--port P] [--reload]      run the API + web UI
    mosaic sources [--category C] [--configured]       list data sources
    mosaic health [NAME ...]                           live health checks
    mosaic call SOURCE TOOL [key=value ...] [--params JSON]
    mosaic search "QUERY" [--categories C ...] [--limit N]
    mosaic catalog [QUERY] [--kind K] [--access A]     NIH repository catalog
    mosaic ask "QUESTION" [--session ID]               ask Claude (streams)
    mosaic exports                                     saved extraction tables

Every command accepts ``--json`` for machine-readable output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable, Coroutine, Sequence

from . import __version__
from .config import Settings, get_settings
from .connectors.base import CATEGORIES

KINDS = ("domain_specific", "generalist")
ACCESS_TYPES = ("open", "controlled", "registered")


# --------------------------------------------------------------------- plumbing
def _settings() -> Settings:
    return get_settings()


def _make_registry(settings: Settings) -> Any:
    """Create the connector registry (patched in tests)."""
    from .registry import Registry

    return Registry.create(settings)


def _make_agent(registry: Any, settings: Settings) -> Any:
    """Construct the Claude agent (patched in tests). Raises RuntimeError with a reason."""
    from .api.app import load_agent

    agent, error = load_agent(registry, settings)
    if agent is None:
        raise RuntimeError(error or "the agent is unavailable")
    return agent


def _loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    # psycopg's async mode (DrugCentral) cannot run on Windows' default Proactor loop.
    return asyncio.SelectorEventLoop if sys.platform == "win32" else None


def _run(coro: Coroutine[Any, Any, int]) -> int:
    try:
        with asyncio.Runner(loop_factory=_loop_factory()) as runner:
            return runner.run(coro)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


async def _with_registry(fn: Callable[[Any], Coroutine[Any, Any, int]]) -> int:
    registry = _make_registry(_settings())
    try:
        return await fn(registry)
    finally:
        await registry.aclose()


def _dump(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _clip(text: Any, width: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= width else value[: max(width - 3, 1)] + "..."


def _table(rows: list[list[Any]], headers: list[str]) -> str:
    cells = [[str(h) for h in headers]] + [[str(c) for c in row] for row in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip() for row in cells]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


# ------------------------------------------------------------- param parsing
def _coerce(value: str, schema: dict[str, Any] | None) -> Any:
    """Convert a ``key=value`` string using the property's JSON schema."""
    schema = schema or {}
    kind = schema.get("type")
    if kind is None and isinstance(schema.get("anyOf"), list):
        kinds = [s.get("type") for s in schema["anyOf"] if isinstance(s, dict) and s.get("type") != "null"]
        kind = kinds[0] if len(kinds) == 1 else None
        if kind is None:
            try:
                return json.loads(value)
            except ValueError:
                return value
    if kind == "string" or "enum" in schema and kind in (None, "string"):
        return value
    if kind == "integer":
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"expected an integer, got {value!r}") from None
    if kind == "number":
        try:
            return float(value)
        except ValueError:
            raise ValueError(f"expected a number, got {value!r}") from None
    if kind == "boolean":
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "y", "on"):
            return True
        if lowered in ("false", "0", "no", "n", "off"):
            return False
        raise ValueError(f"expected true/false, got {value!r}")
    if kind == "array":
        stripped = value.strip()
        if stripped.startswith("["):
            return json.loads(stripped)
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        return [_coerce(part.strip(), item_schema) for part in stripped.split(",") if part.strip()]
    if kind == "object":
        return json.loads(value)
    try:
        return json.loads(value)
    except ValueError:
        return value


def parse_params(
    pairs: Sequence[str], params_json: str | None = None, schema: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Merge ``--params JSON`` with ``key=value`` pairs (pairs win), typed by ``schema``."""
    params: dict[str, Any] = {}
    if params_json:
        loaded = json.loads(params_json)
        if not isinstance(loaded, dict):
            raise ValueError("--params must be a JSON object")
        params.update(loaded)
    properties = (schema or {}).get("properties") or {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"expected key=value, got {pair!r}")
        try:
            params[key] = _coerce(value, properties.get(key))
        except ValueError as exc:
            raise ValueError(f"{key}: {exc}") from None
    return params


# ----------------------------------------------------------------- commands
def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = _settings()
    host = args.host or settings.biopha_api_host
    port = args.port or settings.biopha_api_port
    from .api.security import LOOPBACK_HOSTS

    if host not in LOOPBACK_HOSTS:
        _err(
            f"warning: binding to {host}: Mosaic has no authentication; anyone who can reach this "
            "address can run queries with your API keys."
        )
    os.environ["MOSAIC_BIND_HOST"] = host  # read by create_app (also in the --reload worker)
    _out(f"Mosaic {__version__} -> http://{'localhost' if host in ('127.0.0.1', '::1') else host}:{port}/")
    uvicorn.run(
        "mosaic.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=args.reload,
        reload_dirs=[str(Path(__file__).resolve().parent)] if args.reload else None,
        log_level=(args.log_level or settings.biopha_log_level).lower(),
        loop="asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto",
        timeout_graceful_shutdown=5,
    )
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    async def body(registry: Any) -> int:
        infos = registry.sources(args.category)
        if args.configured:
            infos = [i for i in infos if i.configured]
        if args.json:
            _out(_dump([i.model_dump(mode="json") for i in infos]))
            return 0
        if registry.load_errors:
            for name, msg in registry.load_errors.items():
                _err(f"warning: connector {name} failed to load: {msg}")
        for key, label in CATEGORIES.items():
            group = [i for i in infos if i.category == key]
            if not group:
                continue
            _out(f"\n{label} [{key}]")
            rows = [
                [
                    i.name,
                    "ready" if i.configured else "needs " + ",".join(i.missing_settings),
                    i.auth,
                    len(i.tools),
                    _clip(i.title, 40),
                ]
                for i in group
            ]
            _out(textwrap.indent(_table(rows, ["name", "status", "auth", "tools", "title"]), "  "))
        configured = sum(1 for i in infos if i.configured)
        _out(f"\n{len(infos)} sources, {configured} configured")
        return 0

    return _run(_with_registry(body))


def cmd_health(args: argparse.Namespace) -> int:
    async def body(registry: Any) -> int:
        unknown = [n for n in args.names if n not in registry.connectors]
        if unknown:
            _err(f"unknown source(s): {', '.join(unknown)}")
            return 2
        results = await registry.healthcheck(args.names or None, timeout=args.timeout)
        if args.json:
            _out(_dump([r.model_dump(mode="json") for r in results]))
        else:
            rows = [
                [
                    r.source,
                    "ok" if r.ok else "FAIL",
                    f"{r.latency_ms:.0f} ms" if r.latency_ms is not None else "-",
                    _clip(r.detail, 90),
                ]
                for r in sorted(results, key=lambda r: (r.ok, r.source))
            ]
            _out(_table(rows, ["source", "status", "latency", "detail"]))
            _out(f"\n{sum(r.ok for r in results)}/{len(results)} healthy")
        return 0 if all(r.ok for r in results) else 1

    return _run(_with_registry(body))


def _print_records(records: list[Any], indent: str = "  ") -> None:
    for i, record in enumerate(records, 1):
        bits = [b for b in (record.type, record.date) if b]
        _out(f"{indent}{i:>2}. {_clip(record.title or record.id, 100)}" + (f"  ({', '.join(bits)})" if bits else ""))
        ids = ", ".join(f"{k}:{v}" for k, v in list(record.identifiers.items())[:4])
        if ids:
            _out(f"{indent}    {_clip(ids, 110)}")
        if record.url:
            _out(f"{indent}    {record.url}")


def cmd_call(args: argparse.Namespace) -> int:
    from .http import SourceError

    async def body(registry: Any) -> int:
        connector = registry.connectors.get(args.source)
        if connector is None:
            _err(f"unknown source {args.source!r}; try: mosaic sources")
            return 2
        spec = next((s for s in connector.tool_specs() if s.name == args.tool), None)
        if spec is None:
            _err(f"unknown tool {args.tool!r} for {args.source}; available: {', '.join(d.name for d in connector.tool_defs())}")
            return 2
        try:
            params = parse_params(args.pairs, args.params, spec.input_schema)
        except ValueError as exc:
            _err(f"invalid parameters: {exc}")
            return 2
        try:
            result = await registry.call(args.source, args.tool, params, timeout=args.timeout)
        except SourceError as exc:
            _err(f"error: {exc}")
            return 1
        if args.json:
            _out(_dump(result))
            return 0
        total = f" of {result.total:,}" if result.total is not None else ""
        _out(f"{result.source}.{result.tool}: {len(result.records)}{total} record(s)")
        for note in result.notes:
            _out(f"  note: {note}")
        _print_records(result.records)
        if result.data is not None:
            _out("  data: " + _clip(json.dumps(result.data, default=str), 600))
        if result.next_cursor:
            _out(f"  next page: cursor={result.next_cursor}")
        return 0

    return _run(_with_registry(body))


def cmd_search(args: argparse.Namespace) -> int:
    categories = [c for item in (args.categories or []) for c in item.split(",") if c]
    unknown = [c for c in categories if c not in CATEGORIES]
    if unknown:
        _err(f"unknown category(s): {', '.join(unknown)}; choose from {', '.join(CATEGORIES)}")
        return 2
    sources = [s for item in (args.sources or []) for s in item.split(",") if s]

    async def body(registry: Any) -> int:
        result = await registry.federated_search(
            args.query,
            sources=sources or None,
            categories=categories or None,
            limit_per_source=args.limit,
            timeout=args.timeout,
        )
        if args.json:
            _out(_dump(result))
            return 0
        if not result.sources:
            _out("no configured sources with a federated search match those filters")
            return 1
        _out(f'Searched {len(result.sources)} source(s) for "{result.query}"')
        for status in sorted(result.sources, key=lambda s: (not s.ok, s.source)):
            if status.ok:
                total = f" of {status.total:,}" if status.total is not None else ""
                _out(f"  ok    {status.source}: {status.returned}{total}  ({status.latency_ms or 0:.0f} ms)")
            else:
                _out(f"  FAIL  {status.source}: {_clip(status.error, 100)}")
        curated = result.curated
        if curated is None or not curated.entities:
            _out("\nno data matched")
            return 0 if any(s.ok for s in result.sources) else 1
        st = curated.stats
        _out(
            f"\nCleaned and merged {st['records_in']} records into {st['entities_out']} entities "
            f"({st['duplicates_merged']} duplicates merged, {st['multi_source_entities']} confirmed by 2+ databases); "
            f"grades {', '.join(f'{g}:{n}' for g, n in st['grades'].items())}"
        )
        for entity in curated.entities[: args.limit * 4]:
            ev = entity.evaluation
            date = f"  ({entity.date})" if entity.date else ""
            _out(f"\n[{ev.grade} {ev.score:>3}] {entity.type}: {_clip(entity.title or '(untitled)', 90)}{date}")
            for key, value in [(k, v) for k, v in entity.fields.items() if v not in (None, "", [], {})][:5]:
                text = "; ".join(map(str, value)) if isinstance(value, list) else str(value)
                _out(f"      {key.replace('_', ' ')}: {_clip(text, 100)}")
            ids = ", ".join(f"{k}:{v}" for k, v in list(entity.identifiers.items())[:5])
            if ids:
                _out(f"      ids: {_clip(ids, 110)}")
            if ev.flags:
                _out(f"      ! {'; '.join(ev.flags)}")
            _out(f"      sources: {', '.join(s['source'] + ':' + s['id'] for s in entity.sources)}")
        return 0 if any(s.ok for s in result.sources) else 1

    return _run(_with_registry(body))


def cmd_resolve(args: argparse.Namespace) -> int:
    from .crosslink.resolver import EntityResolver

    async def body(registry: Any) -> int:
        result = await EntityResolver(registry).resolve(args.kind, args.query)
        if args.json:
            _out(_dump(result))
            return 0
        _out(f'{args.kind} "{args.query}": {len(result.identifiers)} identifier(s) from {len(result.candidates)} source(s)')
        for key, agreement in result.agreement.items():
            _out(f"  {key:16s} {agreement.value:40s} [{', '.join(agreement.sources)}]")
        for key, options in result.conflicts.items():
            alt = "; ".join(f"{a.value} ({', '.join(a.sources)})" for a in options[1:])
            _out(f"  conflict {key}: also {_clip(alt, 100)}")
        for source, error in result.errors.items():
            _out(f"  FAIL {source}: {_clip(error, 100)}")
        for note in result.notes:
            _out(f"  note: {note}")
        return 0 if result.identifiers else 1

    return _run(_with_registry(body))


def cmd_species(args: argparse.Namespace) -> int:
    from .species import SPECIES_BY_KEY
    from .species.service import run_comparison

    species = [k for item in (args.species or []) for k in item.split(",") if k]
    unknown = [k for k in species if k not in SPECIES_BY_KEY]
    if unknown:
        _err(f"unknown species: {', '.join(unknown)}; choose from {', '.join(SPECIES_BY_KEY)}")
        return 2

    async def body(registry: Any) -> int:
        comparison = await run_comparison(
            registry, _settings(), args.query, species=species or None, max_papers=args.max_papers,
            extract_with_claude=args.extract,
        )
        if args.json:
            _out(comparison.model_dump_json(exclude={"records"}, indent=2))
            return 0
        targets = ", ".join(f"{t.gene_symbol} ({t.action or 'target'})" for t in comparison.targets) or "none found"
        _out(f"{comparison.compound.name or args.query}: targets {targets}")
        rows = []
        for s in comparison.species:
            ident = f"{s.mean_identity_pct:.1f}%" if s.mean_identity_pct is not None else "-"
            rows.append([f"{s.label_zh} {s.label}", ident, s.n_papers, s.n_measurements, s.evidence_level])
        _out(_table(rows, ["species", "target identity", "papers", "measurements", "evidence"]))
        for note in comparison.notes:
            _out(f"  note: {note}")
        for source, error in comparison.errors.items():
            _out(f"  FAIL {source}: {_clip(error, 100)}")
        if comparison.package:
            _out(f"\ndata package: {comparison.package.get('folder')}")
        _out(f"dashboard: http://127.0.0.1:{_settings().biopha_api_port}/#species?id={comparison.comparison_id}")
        return 0

    return _run(_with_registry(body))


def cmd_catalog(args: argparse.Namespace) -> int:
    from .catalog import RepositoryCatalog
    from .registry import discover_connector_classes

    classes, _ = discover_connector_classes()
    catalog = RepositoryCatalog.from_connectors(classes)
    results = catalog.search(
        args.query,
        kind=args.kind,
        access_type=args.access,
        institute=args.institute,
        has_connector=True if args.live else None,
        limit=args.limit,
    )
    if args.json:
        _out(_dump(results))
        return 0
    if not results:
        _out("no repositories match")
        return 1
    rows = [
        [
            _clip(r["name"], 48),
            "generalist" if r["kind"] == "generalist" else "domain",
            _clip(r.get("access_type") or "-", 26),
            _clip(",".join(r.get("institutes") or []) or "-", 18),
            ",".join(r["connectors"]) or "-",
        ]
        for r in results
    ]
    _out(_table(rows, ["repository", "kind", "access", "institutes", "live connector"]))
    stats = catalog.stats()
    _out(f"\n{len(results)} shown; catalog has {stats['total']} repositories ({stats['with_live_connector']} with live connectors)")
    if len(results) <= 3:
        for r in results:
            _out(f"\n{r['name']}\n  {r.get('url') or ''}\n  data access: {r.get('data_access_url') or '-'}")
            if r.get("description"):
                _out(textwrap.indent(textwrap.fill(_clip(r["description"], 600), 96), "  "))
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    settings = _settings()

    async def body(registry: Any) -> int:
        try:
            agent = _make_agent(registry, settings)
        except Exception as exc:  # noqa: BLE001
            _err(f"Ask Mosaic is unavailable: {exc}")
            return 2
        status = 0
        at_line_start = True

        def line(text: str) -> None:
            nonlocal at_line_start
            if not at_line_start:
                sys.stdout.write("\n")
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
            at_line_start = True

        try:
            iterator = agent.run(args.question, session_id=args.session)
            if asyncio.iscoroutine(iterator):
                iterator = await iterator
            async for event in iterator:
                kind = getattr(event, "type", None)
                if args.json:
                    _out(json.dumps(event.model_dump(mode="json"), ensure_ascii=False, default=str))
                    if kind == "error":
                        status = 1
                    continue
                if kind == "session_started":
                    line(f"[session {event.session_id} | {event.model}]")
                elif kind == "text_delta":
                    sys.stdout.write(event.text)
                    sys.stdout.flush()
                    at_line_start = event.text.endswith("\n")
                elif kind == "thinking_delta" and args.show_thinking:
                    sys.stdout.write(event.text)
                    sys.stdout.flush()
                    at_line_start = event.text.endswith("\n")
                elif kind == "tool_call_started":
                    line(f"  -> {event.name} {_clip(json.dumps(event.input, ensure_ascii=False, default=str), 110)}")
                elif kind == "tool_call_finished":
                    latency = f" ({event.latency_ms:.0f} ms)" if event.latency_ms is not None else ""
                    marker = "ok" if event.ok else "!!"
                    detail = event.summary if event.ok else (event.error or event.summary)
                    line(f"  {marker} {_clip(detail, 120)}{latency}")
                elif kind == "server_tool":
                    line(f"  .. {event.name} {event.phase}" + (f": {_clip(event.detail, 100)}" if event.detail else ""))
                elif kind == "table_saved" and getattr(event, "kind", "table") == "package":
                    files = ", ".join(f"{f['file']} ({f['rows']})" for f in event.files)
                    line(f"  [data package] {event.name}: {files} + mosaic_data.xlsx -> {event.folder}")
                elif kind == "table_saved":
                    path = settings.exports_dir / f"{event.export_id}.csv"
                    line(f"  [table saved] {event.name}: {event.n_rows} rows x {len(event.columns)} columns -> {path}")
                elif kind == "error":
                    status = 1
                    line(f"  error: {event.message}")
                elif kind == "done":
                    usage = event.usage or {}
                    parts = [f"{usage[k]:,} {k.replace('_tokens', '').replace('_', ' ')}" for k in (
                        "input_tokens", "output_tokens", "cache_read_input_tokens") if usage.get(k)]
                    line("")
                    _out(f"[done{': ' + event.stop_reason if event.stop_reason else ''}"
                         + (f" | {', '.join(parts)} tokens" if parts else "")
                         + f" | continue with --session {event.session_id}]")
        finally:
            closer = getattr(agent, "aclose", None)
            if callable(closer):
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
        return status

    return _run(_with_registry(body))


def cmd_exports(args: argparse.Namespace) -> int:
    from .api.exports import list_exports
    from .audit import AuditLog

    settings = _settings()
    audit = AuditLog(settings.audit_db_path)
    try:
        items = list_exports(settings.exports_dir, audit, args.limit)
    finally:
        audit.close()
    if args.json:
        _out(_dump(items))
        return 0
    if not items:
        _out(f"no exports yet in {settings.exports_dir}")
        return 0
    rows = [
        [
            e["export_id"][:12],
            _clip(e["name"], 40),
            e.get("n_rows") if e.get("n_rows") is not None else "-",
            len(e.get("columns") or []),
            (e.get("created_at") or "")[:19].replace("T", " "),
            ",".join(e.get("formats") or []),
        ]
        for e in items
    ]
    _out(_table(rows, ["id", "name", "rows", "cols", "created (UTC)", "files"]))
    _out(f"\nfiles: {settings.exports_dir}")
    return 0


# ------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mosaic",
        description="Mosaic: Claude-powered federated search and cross-database extraction "
        "over biomedical, literature, drug, clinical and NIH data sources.",
    )
    parser.add_argument("--version", action="version", version=f"mosaic {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    def add(name: str, help_text: str, func: Callable[[argparse.Namespace], int], json_flag: bool = True):
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        if json_flag:
            p.add_argument("--json", action="store_true", help="print machine-readable JSON")
        return p

    p = add("serve", "run the HTTP API and web UI", cmd_serve, json_flag=False)
    p.add_argument("--host", help="bind address (default BIOPHA_API_HOST, 127.0.0.1)")
    p.add_argument("--port", type=int, help="port (default BIOPHA_API_PORT, 8000)")
    p.add_argument("--reload", action="store_true", help="restart on code changes (development)")
    p.add_argument("--log-level", choices=["critical", "error", "warning", "info", "debug"], help="server log level")

    p = add("sources", "list data sources and whether they are configured", cmd_sources)
    p.add_argument("--category", choices=list(CATEGORIES), help="only this category")
    p.add_argument("--configured", action="store_true", help="only sources that are ready to use")

    p = add("health", "run live health checks against sources", cmd_health)
    p.add_argument("names", nargs="*", metavar="NAME", help="sources to check (default: all)")
    p.add_argument("--timeout", type=float, default=20.0, help="seconds per source (default 20)")

    p = add("call", "run one connector tool", cmd_call)
    p.add_argument("source", help="source name, e.g. crossref")
    p.add_argument("tool", help="tool name, e.g. search_works")
    p.add_argument("pairs", nargs="*", metavar="key=value", help="tool parameters (typed from the tool schema)")
    p.add_argument("--params", metavar="JSON", help="parameters as a JSON object (key=value pairs override)")
    p.add_argument("--timeout", type=float, default=60.0, help="seconds (default 60)")

    p = add("search", "federated search across databases, with cross-database linking", cmd_search)
    p.add_argument("query", help="free-text query")
    p.add_argument("--categories", nargs="+", metavar="CATEGORY", help=f"limit to: {', '.join(CATEGORIES)}")
    p.add_argument("--sources", nargs="+", metavar="SOURCE", help="limit to these sources")
    p.add_argument("--limit", type=int, default=5, help="records per source (default 5)")
    p.add_argument("--timeout", type=float, default=30.0, help="seconds per source (default 30)")

    p = add("resolve", "map a gene, drug, disease, article or variant to identifiers across databases", cmd_resolve)
    p.add_argument("kind", choices=["gene", "drug", "disease", "article", "variant"])
    p.add_argument("query", help="name or identifier, e.g. BRAF, imatinib, 'cystic fibrosis', a DOI, rs113488022")

    p = add("species", "compare a drug or compound in human vs other species (target similarity, evidence)", cmd_species)
    p.add_argument("query", help="drug or chemical compound, e.g. sotorasib, imatinib, 'bisphenol A'")
    p.add_argument("--species", nargs="+", metavar="KEY", help="species keys (default all), e.g. mouse rat zebrafish")
    p.add_argument("--max-papers", type=int, default=5, help="top PubMed papers per species (default 5)")
    p.add_argument("--extract", action="store_true", help="extract dose/effect from abstracts with Claude (uses credits)")

    p = add("catalog", "search the NIH scientific data repository catalog", cmd_catalog)
    p.add_argument("query", nargs="?", help="free-text query (omit to list all)")
    p.add_argument("--kind", choices=KINDS, help="repository kind")
    p.add_argument("--access", choices=ACCESS_TYPES, help="access type")
    p.add_argument("--institute", help="NIH institute/center, e.g. NCI")
    p.add_argument("--live", action="store_true", help="only repositories with a live Mosaic connector")
    p.add_argument("--limit", type=int, default=25, help="maximum results (default 25)")

    p = add("ask", "ask Claude a research question (streams the answer)", cmd_ask)
    p.add_argument("question", help="the question, in quotes")
    p.add_argument("--session", help="continue an earlier session")
    p.add_argument("--show-thinking", action="store_true", help="also print Claude's reasoning summary")

    p = add("exports", "list saved cross-database extraction tables", cmd_exports)
    p.add_argument("--limit", type=int, default=50, help="maximum rows (default 50)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            # UTF-8 everywhere: Windows otherwise writes redirected output in the ANSI codepage.
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "limit", 1) is not None and getattr(args, "limit", 1) < 1:
        parser.error("--limit must be at least 1")
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
