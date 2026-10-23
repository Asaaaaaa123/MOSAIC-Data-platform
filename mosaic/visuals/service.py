"""Search runs with visuals: one or several queries, the snapshot the web UI's figures come from,
and rendering through Figure Studio off the event loop.

``SearchSnapshots`` keeps the last searches in memory (the figures of a results page are drawn from
exactly the data that page shows); an export re-runs the searches, fetches full records and builds
its own visuals from the package's entities, so the package's figures match its tables.
"""

from __future__ import annotations

import asyncio
import csv
import io
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from ..models import Record
from .build import QueryRun, Visuals, build_visuals
from .figures import FORMATS, load_engine, render, render_all

MAX_SNAPSHOTS = 24
FIGURE_COLUMNS = ["chart", "chart_title", "category", "series", "value", "counted", "definition"]


def record_key(r: Record) -> tuple[str, str]:
    return r.source, str(r.id)


async def run_queries(registry: Any, queries: list[str], **kwargs: Any) -> list[Any]:
    """``registry.federated_search`` for every query, concurrently (``FederatedResult`` each)."""
    return list(await asyncio.gather(*(registry.federated_search(q, **kwargs) for q in queries)))


def union_records(results: list[Any]) -> list[Record]:
    """All records of all queries, each (source, id) once."""
    seen: set[tuple[str, str]] = set()
    out: list[Record] = []
    for result in results:
        for r in result.records:
            if record_key(r) not in seen:
                seen.add(record_key(r))
                out.append(r)
    return out


def source_titles(registry: Any) -> dict[str, str]:
    return {name: getattr(c, "title", name) for name, c in getattr(registry, "connectors", {}).items()}


def make_runs(queries: list[str], results: list[Any], limit: int | None,
              replaced: dict[tuple[str, str], Record] | None = None) -> list[QueryRun]:
    """One ``QueryRun`` per query; ``replaced`` swaps hits for their full records (same order)."""
    runs = []
    for q, result in zip(queries, results):
        records = [replaced.get(record_key(r), r) for r in result.records] if replaced else list(result.records)
        runs.append(QueryRun(query=q, records=records, sources=list(result.sources), limit_per_source=limit))
    return runs


def figure_data_csv(visuals: Visuals) -> bytes:
    from ..agent.exports import csv_cell  # formula-injection-safe cells

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(FIGURE_COLUMNS)
    for row in visuals.figure_rows():
        writer.writerow([csv_cell(row.get(c)) for c in FIGURE_COLUMNS])
    return buffer.getvalue().encode("utf-8-sig")


def engine_status(configured_path: str | None) -> dict[str, Any]:
    engine, error = load_engine(configured_path)
    return {"available": engine is not None, "error": error, "path": str(engine.path) if engine else None}


async def render_one(configured_path: str | None, visuals: Visuals, name: str, fmt: str) -> bytes:
    engine, error = load_engine(configured_path)
    if engine is None:
        raise RuntimeError(error or "Figure Studio is not available")
    return await asyncio.to_thread(render, engine, visuals, name, fmt)


async def render_package_figures(configured_path: str | None, visuals: Visuals) -> tuple[dict[str, bytes], list[str]]:
    """Every figure as SVG, PNG and PDF for a data package, or no figures and a note saying why."""
    engine, error = load_engine(configured_path)
    if engine is None:
        return {}, [f"figures were not drawn: {error}"]
    return await asyncio.to_thread(render_all, engine, visuals)


@dataclass
class SearchSnapshot:
    search_id: str
    queries: list[str]
    visuals: Visuals
    created: float = field(default_factory=time.time)
    rendered: dict[tuple[str, str], bytes] = field(default_factory=dict)


class SearchSnapshots:
    """The last ``MAX_SNAPSHOTS`` searches, so the results page can fetch their figures."""

    def __init__(self, limit: int = MAX_SNAPSHOTS) -> None:
        self._items: OrderedDict[str, SearchSnapshot] = OrderedDict()
        self._limit = limit
        self._lock = threading.Lock()

    def add(self, queries: list[str], visuals: Visuals) -> SearchSnapshot:
        snap = SearchSnapshot(search_id=uuid.uuid4().hex, queries=list(queries), visuals=visuals)
        with self._lock:
            self._items[snap.search_id] = snap
            while len(self._items) > self._limit:
                self._items.popitem(last=False)
        return snap

    def get(self, search_id: str) -> SearchSnapshot | None:
        with self._lock:
            snap = self._items.get(search_id)
            if snap is not None:
                self._items.move_to_end(search_id)
            return snap

    async def figure(self, configured_path: str | None, snap: SearchSnapshot, name: str, fmt: str) -> bytes:
        if fmt not in FORMATS:
            raise ValueError(fmt)
        key = (name, fmt)
        if key not in snap.rendered:
            snap.rendered[key] = await render_one(configured_path, snap.visuals, name, fmt)
        return snap.rendered[key]


def search_visuals(registry: Any, queries: list[str], results: list[Any], entities: list[Any],
                   limit: int | None) -> Visuals:
    return build_visuals(make_runs(queries, results, limit), entities, source_titles=source_titles(registry))
