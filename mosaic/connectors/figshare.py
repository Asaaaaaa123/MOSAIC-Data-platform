"""Figshare connector (figshare.com and the institutional / publisher Figshare portals).

Docs: https://docs.figshare.com/ (API v2 base https://api.figshare.com/v2)
Auth: none for public items. Rate limits are not published; we stay at 3 req/s.

Quirks (verified live, 2026-10):
- ``POST /articles/search`` takes a JSON body: ``search_for`` (free text; field syntax such as
  ``:title: glioblastoma``, ``:tag: rna-seq``, ``:author: Smith`` is supported), ``item_type``
  (3 = dataset), ``page``/``page_size`` (max 1000), ``order`` (published_date, modified_date, views,
  shares, downloads, cites, created_date; there is no "relevance" value), ``order_direction``,
  ``resource_doi`` (the DOI of the article an item supplements) and ``doi``.
- The search endpoint returns a list with no total count, and only light metadata (no authors or
  description); ``GET /articles/{id}`` has the full record.
- Publisher portals (Frontiers, PLOS, Springer Nature, Taylor & Francis, ...) deposit article
  supplements here: ``resource_doi`` / ``resource_title`` name the parent article, which makes
  Figshare a strong dataset-to-paper bridge.

Identifiers: ``doi`` and ``figshare`` (article id). The parent article DOI, related materials
and reference DOIs are in ``attributes["related"]``.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, model_validator

from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, strip_html, tool

API = "https://api.figshare.com/v2"

ITEM_TYPES: dict[str, int] = {
    "figure": 1,
    "media": 2,
    "dataset": 3,
    "poster": 5,
    "journal_contribution": 6,
    "presentation": 7,
    "thesis": 8,
    "software": 9,
    "online_resource": 11,
    "preprint": 12,
    "book": 13,
    "conference_contribution": 14,
    "report": 18,
}
ItemType = Literal[
    "figure", "media", "dataset", "poster", "journal_contribution", "presentation", "thesis", "software",
    "online_resource", "preprint", "book", "conference_contribution", "report", "any",
]

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_TYPE_MAP = {
    "dataset": "dataset",
    "journal contribution": "article",
    "preprint": "preprint",
    "book": "book",
    "chapter": "book",
    "thesis": "article",
    "conference contribution": "article",
    "report": "article",
}


class FigshareSearchParams(SearchParams):
    item_type: ItemType = Field("dataset", description="Item type filter (default dataset); 'any' for all types")
    order: Literal["published_date", "modified_date", "views", "downloads", "cites", "shares"] | None = Field(
        None, description="Sort field (default: Figshare's own ranking)"
    )
    order_direction: Literal["asc", "desc"] = Field("desc", description="Sort direction when order is set")
    published_since: str | None = Field(None, description="Only items published on/after this date (YYYY-MM-DD)")


class GetArticleParams(ToolParams):
    article_id: str = Field(
        description="Figshare article id (e.g. 34700316), figshare URL, or a 10.6084/m9.figshare.<id> DOI"
    )


class ItemsForArticleParams(ToolParams):
    doi: str = Field(description="DOI of the journal article whose supplementary items you want, e.g. 10.3389/fonc.2022.943064")
    item_type: ItemType = Field("any", description="Restrict by item type (default any: supplements are often figures/tables)")
    limit: int = Field(25, ge=1, le=100, description="Maximum number of items to return")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous result)")

    @model_validator(mode="after")
    def _check(self) -> "ItemsForArticleParams":
        if not _DOI_RE.search(self.doi):
            raise ValueError("doi must look like 10.xxxx/...")
        return self


def _page(cursor: str | None) -> int:
    try:
        return max(1, int(cursor or 1))
    except ValueError:
        return 1


def _doi_entry(relation: str, value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    match = _DOI_RE.search(str(value))
    if not match:
        return None
    return {"relation": relation, "scheme": "doi", "id": normalize_doi(match.group(0)).rstrip(".")}


def _related_material(rel: dict[str, Any]) -> dict[str, str] | None:
    relation = rel.get("relation") or "related"
    ident = str(rel.get("identifier") or "").strip()
    kind = (rel.get("identifier_type") or "").lower()
    if not ident:
        return None
    entry = _doi_entry(relation, ident) if kind in ("doi", "url", "") else None
    if entry:
        return entry
    if kind == "pmid":
        return {"relation": relation, "scheme": "pmid", "id": re.sub(r"\D", "", ident)}
    return {"relation": relation, "scheme": kind or "other", "id": ident}


def _record(item: dict[str, Any], *, full: bool = False) -> Record:
    aid = str(item.get("id") or "")
    doi = normalize_doi(item.get("doi") or "")
    type_name = (item.get("defined_type_name") or "").lower()
    related: list[dict[str, str]] = []
    if item.get("resource_doi"):
        entry = _doi_entry("IsSupplementTo", item["resource_doi"])
        if entry:
            if item.get("resource_title"):
                entry["title"] = strip_html(item["resource_title"]) or ""
            related.append(entry)
    attributes: dict[str, Any] = {
        "item_type": item.get("defined_type_name"),
        "published": item.get("published_date"),
        "group_id": item.get("group_id"),
        "portal": _portal(item.get("url_public_html") or item.get("figshare_url")),
    }
    authors: list[str] = []
    summary = None
    if full:
        seen = {(r["relation"], r["id"]) for r in related}
        for rel in item.get("related_materials") or []:
            entry = _related_material(rel)
            if entry and (entry["relation"], entry["id"]) not in seen:
                seen.add((entry["relation"], entry["id"]))
                related.append(entry)
        for ref in item.get("references") or []:
            entry = _doi_entry("References", ref)
            if entry and (entry["relation"], entry["id"]) not in seen:
                seen.add((entry["relation"], entry["id"]))
                related.append(entry)
        authors = [a.get("full_name") for a in item.get("authors") or [] if a.get("full_name")]
        orcids = [a.get("orcid_id") for a in item.get("authors") or [] if a.get("orcid_id")]
        summary = strip_html(item.get("description"))
        files = item.get("files") or []
        license_ = item.get("license") or {}
        custom = {}
        for cf in item.get("custom_fields") or []:
            value = cf.get("value")
            if cf.get("name") and value not in (None, "", []):
                custom[cf["name"]] = value if not isinstance(value, str) else value[:200]
        attributes.update(
            {
                "license": license_.get("name"),
                "categories": [c.get("title") for c in item.get("categories") or [] if c.get("title")][:15] or None,
                "keywords": (item.get("tags") or item.get("keywords") or [])[:20] or None,
                "funding": [f.get("title") for f in item.get("funding_list") or [] if f.get("title")][:10] or None,
                "version": item.get("version"),
                "size_bytes": item.get("size"),
                "file_count": len(files) or None,
                "files": [
                    {
                        "name": f.get("name"),
                        "size": f.get("size"),
                        "md5": f.get("computed_md5") or f.get("supplied_md5"),
                        "mimetype": f.get("mimetype"),
                        "link": f.get("download_url"),
                    }
                    for f in files[:50]
                ]
                or None,
                "embargoed": item.get("is_embargoed") or None,
                "metadata_only": item.get("is_metadata_record") or None,
                "author_orcids": orcids[:10] or None,
                "custom_fields": dict(list(custom.items())[:10]) or None,
                "citation": item.get("citation"),
            }
        )
    # Keep every non-bibliography relation; the copied reference list is capped at 10.
    refs = [r for r in related if r["relation"] == "References"]
    others = [r for r in related if r["relation"] != "References"]
    attributes["related"] = (others[:30] + refs[:10]) or None
    if len(refs) > 10:
        attributes["references_total"] = len(refs)
    date = (item.get("published_date") or (item.get("timeline") or {}).get("posted") or "")[:10] or None
    return Record(
        source="figshare",
        id=aid,
        type=_TYPE_MAP.get(type_name, "other"),  # type: ignore[arg-type]
        title=strip_html(item.get("title")),
        url=item.get("url_public_html") or item.get("figshare_url") or (f"https://figshare.com/articles/{aid}" if aid else None),
        identifiers={"doi": doi, "figshare": aid},
        date=date,
        authors=authors,
        summary=summary,
        attributes={k: v for k, v in attributes.items() if v not in (None, [], "", {})},
    )


def _portal(url: str | None) -> str | None:
    match = re.match(r"https?://([^/]+)/", url or "")
    return match.group(1) if match else None


class Figshare(Connector):
    name = "figshare"
    title = "Figshare"
    category = "datasets"
    description = (
        "Figshare generalist repository (figshare.com plus institutional and publisher portals such as "
        "Frontiers, PLOS, Springer Nature): millions of datasets, figures, supplementary files and code, "
        "each with a DOI. Best for finding the supplementary data of journal articles (by article DOI)."
    )
    homepage = "https://figshare.com/"
    docs_url = "https://docs.figshare.com/"
    auth = "none"
    rate_limit = 3.0
    health_query = "RNA-seq"
    nih_repositories = ("Figshare",)

    async def _search(self, tool_name: str, body: dict[str, Any], limit: int, page: int) -> ToolResult:
        body = {k: v for k, v in body.items() if v is not None}
        body.update({"page": page, "page_size": limit})
        response = await self.request("POST", f"{API}/articles/search", json_body=body)
        items = response.json() or []
        if not isinstance(items, list):
            items = []
        return ToolResult(
            source=self.name,
            tool=tool_name,
            records=[_record(i) for i in items],
            next_cursor=str(page + 1) if len(items) >= limit else None,
            request_url=response.url,
            notes=["search results carry light metadata; call get_article for authors, description, files and license"]
            if items
            else [],
        )

    @tool(
        description="""
        Search public Figshare items (default: datasets) by free text across figshare.com and the
        institutional/publisher portals. Returns id, DOI, title, item type, publication date, portal,
        and - for publisher supplements - the parent article DOI (attributes.related IsSupplementTo).
        Results carry no authors/abstract: call get_article for those and for the file list.
        Query syntax: plain words, or field searches like ':title: glioblastoma', ':tag: rna-seq',
        ':author: Smith'. Filters: item_type (dataset|figure|software|preprint|...|any), order
        (published_date|views|downloads|cites...), order_direction, published_since (YYYY-MM-DD).
        To find the supplementary files of a paper use items_for_article instead.
        """,
        federated=True,
    )
    async def search(self, params: FigshareSearchParams) -> ToolResult:
        body = {
            "search_for": params.query,
            "item_type": None if params.item_type == "any" else ITEM_TYPES[params.item_type],
            "order": params.order,
            "order_direction": params.order_direction if params.order else None,
            "published_since": params.published_since,
        }
        return await self._search("search", body, params.limit, _page(params.cursor))

    @tool(
        description="""
        Fetch one public Figshare item by article id, figshare URL or 10.6084/m9.figshare.<id> DOI:
        title, authors (with ORCIDs), description, categories, tags, license, funding, version,
        total size, file list (name, size, md5, mimetype, download link; first 50), custom fields,
        and related identifiers (parent article DOI, related materials, reference DOIs) in
        attributes.related.
        """
    )
    async def get_article(self, params: GetArticleParams) -> ToolResult:
        text = params.article_id.strip()
        match = (
            re.fullmatch(r"\d+", text)
            or re.search(r"figshare\.(\d+)(?:\.v\d+)?$", text, re.IGNORECASE)
            or re.search(r"figshare\.com/.*?/(\d+)(?:/\d+)?/?$", text)
            or re.search(r"/articles/(\d+)", text)
        )
        if not match:
            return ToolResult(
                source=self.name,
                tool="get_article",
                notes=[f"not a Figshare article id: {text!r}; use items_for_article or search for other DOIs"],
            )
        aid = match.group(1) if match.groups() else match.group(0)
        response = await self.request("GET", f"{API}/articles/{aid}", allow_status=(404,))
        if response.status == 404:
            return ToolResult(
                source=self.name, tool="get_article", notes=[f"Figshare article {aid} not found or not public"],
                request_url=response.url,
            )
        item = response.json() or {}
        record = _record(item, full=True)
        notes = []
        if len(item.get("files") or []) > 50:
            notes.append(f"item has {len(item['files'])} files; listed the first 50")
        return ToolResult(
            source=self.name, tool="get_article", records=[record], total=1, request_url=response.url, notes=notes
        )

    @tool(
        description="""
        List the Figshare items (supplementary datasets, figures, tables, videos) attached to a
        journal article, by the ARTICLE's DOI (Figshare resource_doi), e.g. 10.3389/fonc.2022.943064.
        Publisher portals (Frontiers, PLOS, Springer Nature, Taylor & Francis, SAGE ...) deposit
        supplements this way. Use it to get the data behind a paper; datacite.datasets_for_article
        covers other repositories.
        """
    )
    async def items_for_article(self, params: ItemsForArticleParams) -> ToolResult:
        match = _DOI_RE.search(params.doi)
        doi = normalize_doi(match.group(0)) if match else params.doi
        body = {
            "resource_doi": doi,
            "item_type": None if params.item_type == "any" else ITEM_TYPES[params.item_type],
        }
        result = await self._search("items_for_article", body, params.limit, _page(params.cursor))
        if not result.records:
            result.notes.append(f"no Figshare items list {doi} as their parent article")
        return result
