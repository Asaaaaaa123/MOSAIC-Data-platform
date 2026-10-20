"""Crossref REST API connector (reference implementation for other connectors).

Docs: https://api.crossref.org/swagger-ui/index.html
Keyless. Requests carry ``mailto`` (polite pool) when BIOPHA_CONTACT_EMAIL is set.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, first, parse_offset, strip_html, tool

API = "https://api.crossref.org"

_SELECT = (
    "DOI,title,author,issued,container-title,abstract,type,URL,ISSN,publisher,"
    "is-referenced-by-count,subject,funder,license,link"
)


class SearchWorksParams(SearchParams):
    year_from: int | None = Field(None, description="Only works published in or after this year")
    year_to: int | None = Field(None, description="Only works published in or before this year")
    type: str | None = Field(
        None, description="Crossref work type, e.g. journal-article, posted-content (preprints), book-chapter, dataset"
    )
    sort: str | None = Field(
        None, description="relevance (default), published, is-referenced-by-count (citations)"
    )


class GetWorkParams(ToolParams):
    doi: str = Field(description="DOI, with or without https://doi.org/ prefix")


def _date(item: dict[str, Any]) -> str | None:
    for key in ("published-print", "published-online", "issued", "created"):
        parts = (item.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            return "-".join(f"{int(p):02d}" if i else str(int(p)) for i, p in enumerate(parts[0]))
    return None


def _record(item: dict[str, Any]) -> Record:
    doi = item.get("DOI", "")
    authors = [
        " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name", "")
        for a in item.get("author") or []
    ]
    rtype = item.get("type")
    return Record(
        source="crossref",
        id=doi,
        type="dataset" if rtype == "dataset" else "preprint" if rtype == "posted-content" else "article",
        title=strip_html(first(item.get("title"))),
        url=item.get("URL") or (f"https://doi.org/{doi}" if doi else None),
        identifiers={"doi": doi, "issn": first(item.get("ISSN"))},
        date=_date(item),
        authors=[a for a in authors if a],
        summary=strip_html(item.get("abstract")),
        attributes={
            k: v
            for k, v in {
                "journal": first(item.get("container-title")),
                "publisher": item.get("publisher"),
                "work_type": rtype,
                "cited_by": item.get("is-referenced-by-count"),
                "subjects": item.get("subject"),
                "funders": [f.get("name") for f in item.get("funder") or [] if f.get("name")] or None,
                "license": first([lic.get("URL") for lic in item.get("license") or []]),
            }.items()
            if v not in (None, [], "")
        },
    )


class Crossref(Connector):
    name = "crossref"
    title = "Crossref"
    category = "literature"
    description = (
        "DOI registration agency metadata for ~170M scholarly works: titles, authors, journals, dates, "
        "funders, licenses, citation counts. Best for resolving and validating DOIs."
    )
    homepage = "https://www.crossref.org/"
    docs_url = "https://api.crossref.org/swagger-ui/index.html"
    rate_limit = 10.0
    health_query = "CRISPR"

    def _polite(self, params: dict[str, Any]) -> dict[str, Any]:
        email = self.setting("BIOPHA_CONTACT_EMAIL")
        if email:
            params["mailto"] = email
        return params

    @tool(
        description="""
        Search Crossref scholarly works (articles, preprints, book chapters, datasets) by free text
        over titles, authors and abstracts. Returns DOIs, titles, authors, journal, date and
        citation counts. Use for bibliographic lookups and to find DOIs; prefer PubMed / Europe PMC
        for biomedical topic searches. Filters: year_from/year_to, type, sort.
        """,
        federated=True,
    )
    async def search_works(self, params: SearchWorksParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        filters = []
        if params.year_from:
            filters.append(f"from-pub-date:{params.year_from}")
        if params.year_to:
            filters.append(f"until-pub-date:{params.year_to}")
        if params.type:
            filters.append(f"type:{params.type}")
        query = self._polite(
            {
                "query": params.query,
                "rows": params.limit,
                "offset": offset,
                "select": _SELECT,
                "filter": ",".join(filters) or None,
                "sort": params.sort if params.sort and params.sort != "relevance" else None,
                "order": "desc" if params.sort and params.sort != "relevance" else None,
            }
        )
        response = await self.request("GET", f"{API}/works", params=query)
        message = response.json().get("message", {})
        items = message.get("items") or []
        total = message.get("total-results")
        nxt = offset + len(items)
        return ToolResult(
            source=self.name,
            tool="search_works",
            records=[_record(i) for i in items],
            total=total,
            next_cursor=str(nxt) if items and total and nxt < total else None,
            request_url=response.url,
        )

    @tool(description="Fetch full Crossref metadata for one DOI (title, authors, journal, funders, license, citations).")
    async def get_work(self, params: GetWorkParams) -> ToolResult:
        from ..ids import normalize_doi

        doi = normalize_doi(params.doi)
        response = await self.request("GET", f"{API}/works/{doi}", params=self._polite({}), allow_status=(404,))
        if response.status == 404:
            return ToolResult(source=self.name, tool="get_work", notes=[f"DOI {doi} not found in Crossref"])
        item = response.json().get("message", {})
        return ToolResult(source=self.name, tool="get_work", records=[_record(item)], total=1, request_url=response.url)
