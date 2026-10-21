"""Zenodo connector (CERN's open repository for data, software and publications).

Docs: https://developers.zenodo.org/ (REST API: https://zenodo.org/api/records)
Auth: keyless for public records. Optional ``ZENODO_ACCESS_TOKEN`` (personal access token) is sent
as ``Authorization: Bearer`` and raises the page-size cap and rate limit.
Rate limits (documented): guests 60 req/min (2000/h); authenticated 100 req/min (5000/h).

Quirks (verified live, 2026-10):
- ``q`` is Elasticsearch query-string syntax over the record JSON: plain words, "phrases",
  AND/OR/NOT, fields like ``title:``, ``creators.name:``, ``keywords:``, ``doi:"10.5281/zenodo.123"``,
  ``metadata.related_identifiers.identifier:"10.1038/..."``.
- Anonymous requests are capped at ``size<=25`` (HTTP 400 above that); with a token 100.
- Every version is its own record (``id``/``doi``) under a concept record (``conceptrecid`` /
  ``conceptdoi``); GET of a concept record id redirects to the latest version.
- ``communities`` takes community slugs (e.g. ``covid-19``); ``type`` filters ``resource_type.type``
  and ``subtype`` its subtype (e.g. type=publication + subtype=preprint).

Identifiers: ``doi`` (version DOI), ``zenodo`` (record id); alternate identifiers (arXiv, PMID,
...) when present. Concept DOI, files, license, keywords and related works (attributes.related,
from ``related_identifiers``) go to attributes.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field

from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, strip_html, tool

API = "https://zenodo.org/api/records"

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_ZENODO_DOI = re.compile(r"^10\.5281/zenodo\.(\d+)$")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,99}$")
_ANON_MAX = 25

ZenodoType = Literal[
    "dataset", "software", "publication", "poster", "presentation", "image", "video", "lesson", "other", "any"
]


class ZenodoSearchParams(SearchParams):
    type: ZenodoType = Field("dataset", description="Resource type filter (default dataset); 'any' for all types")
    subtype: str | None = Field(
        None, description="Resource subtype, e.g. preprint, article, figure, plot (with type=publication/image)"
    )
    communities: str | None = Field(None, description="Zenodo community slug, e.g. 'covid-19'")
    sort: Literal["bestmatch", "mostrecent", "-mostrecent", "mostviewed", "mostdownloaded"] = Field(
        "bestmatch", description="Sort order (default bestmatch = relevance)"
    )


class GetRecordParams(ToolParams):
    record: str = Field(
        description="Zenodo record id (e.g. 10057779), record URL, or DOI (10.5281/zenodo.10057779 or another DOI hosted on Zenodo)"
    )
    include_versions: bool = Field(True, description="Also list the other versions of this record")


class RecordsForArticleParams(ToolParams):
    doi: str | None = Field(None, description="Article DOI the Zenodo record relates to, e.g. 10.1038/s41586-024-07493-y")
    pmid: str | None = Field(None, description="PubMed ID the Zenodo record relates to")
    limit: int = Field(10, ge=1, le=100, description="Maximum number of records to return")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous result)")


def _page(cursor: str | None) -> int:
    try:
        return max(1, int(cursor or 1))
    except ValueError:
        return 1


def _author(name: str | None) -> str:
    name = (name or "").strip()
    if name.count(",") == 1:
        family, given = (x.strip() for x in name.split(","))
        if family and given:
            return f"{given} {family}"
    return name


def _related(rel: dict[str, Any]) -> dict[str, str] | None:
    value = str(rel.get("identifier") or "").strip()
    if not value:
        return None
    scheme = (rel.get("scheme") or "").lower()
    if scheme == "doi" or (scheme == "url" and _DOI_RE.search(value)):
        match = _DOI_RE.search(value)
        if not match:
            return None
        scheme, value = "doi", normalize_doi(match.group(0)).rstrip(".")
    elif scheme == "pmid":
        value = re.sub(r"\D", "", value)
    entry = {"relation": rel.get("relation") or "related", "scheme": scheme or "other", "id": value}
    if rel.get("resource_type"):
        entry["resource_type"] = rel["resource_type"]
    return entry


_TYPE_MAP = {"dataset": "dataset", "software": "other", "publication": "article"}


def _record(hit: dict[str, Any], *, with_files: bool = False) -> Record:
    meta = hit.get("metadata") or {}
    rid = str(hit.get("id") or hit.get("recid") or "")
    doi = normalize_doi(hit.get("doi") or meta.get("doi") or "")
    rtype_info = meta.get("resource_type") or {}
    rtype = _TYPE_MAP.get(rtype_info.get("type") or "", "other")
    if rtype_info.get("subtype") == "preprint":
        rtype = "preprint"
    identifiers: dict[str, Any] = {"doi": doi, "zenodo": rid}
    for alt in meta.get("alternate_identifiers") or []:
        scheme = (alt.get("scheme") or "").lower()
        if scheme in ("pmid", "pmcid", "arxiv", "isbn", "issn") and alt.get("identifier"):
            identifiers.setdefault(scheme, alt["identifier"])
    related = [r for r in (_related(x) for x in meta.get("related_identifiers") or []) if r]
    files = hit.get("files") or []
    if isinstance(files, dict):  # some payloads nest files as {"entries": {...}}
        files = list((files.get("entries") or {}).values())
    total_size = sum(int(f.get("size") or 0) for f in files) or None
    stats = hit.get("stats") or {}
    links = hit.get("links") or {}
    license_ = meta.get("license")
    attributes: dict[str, Any] = {
        "resource_type": rtype_info.get("title") or rtype_info.get("type"),
        "access_right": meta.get("access_right"),
        "license": license_.get("id") if isinstance(license_, dict) else license_,
        "version": meta.get("version"),
        "keywords": (meta.get("keywords") or [])[:20] or None,
        "subjects": [s.get("term") for s in meta.get("subjects") or [] if s.get("term")][:15] or None,
        "communities": [c.get("id") for c in meta.get("communities") or [] if c.get("id")] or None,
        "concept_doi": normalize_doi(hit["conceptdoi"]) if hit.get("conceptdoi") else None,
        "concept_record_id": hit.get("conceptrecid"),
        "journal": (meta.get("journal") or {}).get("title"),
        "grants": [
            " - ".join(x for x in ((g.get("funder") or {}).get("name"), g.get("code"), g.get("title")) if x)
            for g in meta.get("grants") or []
        ][:10]
        or None,
        "file_count": len(files) or None,
        "total_size_bytes": total_size,
        "views": stats.get("unique_views") or stats.get("views"),
        "downloads": stats.get("unique_downloads") or stats.get("downloads"),
        "related": related[:30] or None,
        "related_total": len(related) if len(related) > 30 else None,
    }
    if with_files and files:
        attributes["files"] = [
            {
                "name": f.get("key") or f.get("filename"),
                "size": f.get("size"),
                "checksum": f.get("checksum"),
                "link": (f.get("links") or {}).get("self") or (f.get("links") or {}).get("content"),
            }
            for f in files[:50]
        ]
    return Record(
        source="zenodo",
        id=rid,
        type=rtype,  # type: ignore[arg-type]
        title=strip_html(meta.get("title") or hit.get("title")),
        url=links.get("self_html") or (f"https://zenodo.org/records/{rid}" if rid else None),
        identifiers=identifiers,
        date=meta.get("publication_date"),
        authors=[a for a in (_author(c.get("name")) for c in meta.get("creators") or []) if a],
        summary=strip_html(meta.get("description")),
        attributes={k: v for k, v in attributes.items() if v not in (None, [], "", {})},
    )


class Zenodo(Connector):
    name = "zenodo"
    title = "Zenodo"
    category = "datasets"
    description = (
        "CERN-run open repository with 4M+ records: research datasets, software, preprints, posters and "
        "figures, each with a DOI, files, license and links to the related publications. NIH-designated "
        "generalist repository; best for finding and downloading open data/code behind papers."
    )
    homepage = "https://zenodo.org/"
    docs_url = "https://developers.zenodo.org/"
    auth = "optional_api_key"
    rate_limit = 1.0
    rate_burst = 5
    health_query = "single-cell RNA-seq"
    nih_repositories = ("Zenodo",)

    def effective_rate(self) -> float:
        return 1.6 if self._token() else self.rate_limit

    def _token(self) -> str | None:
        return self.setting("ZENODO_ACCESS_TOKEN")

    def _headers(self) -> dict[str, str]:
        token = self._token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _size(self, limit: int, notes: list[str]) -> int:
        if not self._token() and limit > _ANON_MAX:
            notes.append(f"Zenodo caps anonymous pages at {_ANON_MAX} records; set ZENODO_ACCESS_TOKEN for up to 100")
            return _ANON_MAX
        return limit

    async def _query(self, tool_name: str, params: dict[str, Any], limit: int, page: int) -> ToolResult:
        notes: list[str] = []
        size = self._size(limit, notes)
        response = await self.request(
            "GET", API, params={**params, "size": size, "page": page}, headers=self._headers()
        )
        payload = response.json() or {}
        hits = (payload.get("hits") or {}).get("hits") or []
        total = (payload.get("hits") or {}).get("total")
        if isinstance(total, dict):
            total = total.get("value")
        has_next = bool(hits) and bool((payload.get("links") or {}).get("next")) and (
            total is None or page * size < total
        )
        return ToolResult(
            source=self.name,
            tool=tool_name,
            records=[_record(h) for h in hits],
            total=total,
            next_cursor=str(page + 1) if has_next else None,
            request_url=response.url,
            notes=notes,
        )

    @tool(
        description="""
        Search Zenodo records (default: datasets) by free text. Returns DOI, Zenodo record id, title,
        creators, publication date, description, license, keywords, communities, file count/size and
        related article DOIs (attributes.related). Use for open data/code deposits, especially
        European and physics/biology lab data; prefer datacite.search_datasets to search all
        repositories at once. Query: Elasticsearch syntax, e.g. glioblastoma AND "single-cell",
        title:"proteomics", creators.name:"Smith", keywords:CRISPR. Filters: type
        (dataset|software|publication|poster|image|...|any), subtype, communities (slug), sort
        (bestmatch|mostrecent|mostviewed|mostdownloaded). Anonymous page size max 25.
        """,
        federated=True,
    )
    async def search(self, params: ZenodoSearchParams) -> ToolResult:
        if params.communities and not _SLUG.match(params.communities):
            return ToolResult(source=self.name, tool="search", notes=["invalid community slug"])
        query = {
            "q": params.query,
            "type": None if params.type == "any" else params.type,
            "subtype": params.subtype,
            "communities": params.communities,
            "sort": params.sort,
        }
        return await self._query("search", query, params.limit, _page(params.cursor))

    @tool(
        description="""
        Fetch one Zenodo record by record id, zenodo.org URL or DOI: full metadata plus the file list
        (name, size, checksum, download link; first 50), related identifiers (supplemented article
        DOIs/PMIDs, versions, parts) and the list of all versions of the record. Use after a search to
        see what files a deposit contains or to resolve a 10.5281/zenodo.* DOI.
        """
    )
    async def get_record(self, params: GetRecordParams) -> ToolResult:
        text = params.record.strip()
        notes: list[str] = []
        rid: str | None = None
        url_match = re.search(r"zenodo\.org/(?:api/)?records?/(\d+)", text)
        if text.isdigit():
            rid = text
        elif url_match:
            rid = url_match.group(1)
        else:
            doi = normalize_doi(text)
            if m := _ZENODO_DOI.match(doi):
                rid = m.group(1)
            elif _DOI_RE.match(doi):
                found = await self._query("get_record", {"q": f'doi:"{doi}"'}, 1, 1)
                if not found.records:
                    return ToolResult(
                        source=self.name, tool="get_record", notes=[f"DOI {doi} not found in Zenodo"],
                        request_url=found.request_url,
                    )
                rid = found.records[0].id
            else:
                return ToolResult(
                    source=self.name, tool="get_record", notes=[f"not a Zenodo record id or DOI: {text!r}"]
                )
        response = await self.request("GET", f"{API}/{rid}", headers=self._headers(), allow_status=(404, 410))
        if response.status in (404, 410):
            return ToolResult(
                source=self.name,
                tool="get_record",
                notes=[f"Zenodo record {rid} not found" + (" (deleted)" if response.status == 410 else "")],
                request_url=response.url,
            )
        hit = response.json() or {}
        record = _record(hit, with_files=True)
        if params.include_versions and record.id:
            versions = await self.request(
                "GET",
                f"{API}/{record.id}/versions",
                params={"size": 25, "sort": "version"},
                headers=self._headers(),
                allow_status=(404,),
            )
            if versions.status == 200:
                vhits = ((versions.json() or {}).get("hits") or {})
                items = vhits.get("hits") or []
                record.attributes["versions"] = [
                    {
                        "id": str(v.get("id")),
                        "doi": normalize_doi(v.get("doi") or ""),
                        "version": (v.get("metadata") or {}).get("version"),
                        "date": (v.get("metadata") or {}).get("publication_date"),
                        "latest": ((v.get("metadata") or {}).get("relations") or {}).get("version", [{}])[0].get("is_last"),
                    }
                    for v in items
                ]
                vtotal = vhits.get("total")
                if isinstance(vtotal, int) and vtotal > len(items):
                    notes.append(f"{vtotal} versions exist; listed the first {len(items)}")
        if record.attributes.get("file_count", 0) > 50:
            notes.append(f"record has {record.attributes['file_count']} files; listed the first 50")
        return ToolResult(
            source=self.name, tool="get_record", records=[record], total=1, request_url=response.url, notes=notes
        )

    @tool(
        description="""
        Find Zenodo records (datasets, code, supplements) whose related_identifiers point to a given
        article DOI or PMID (isSupplementTo, isPublishedIn, isCitedBy, references ...). Use to get the
        Zenodo-deposited data/code behind a paper; datacite.datasets_for_article covers all
        repositories. Example: doi=10.1038/s41586-024-07493-y.
        """
    )
    async def records_for_article(self, params: RecordsForArticleParams) -> ToolResult:
        values = []
        if params.doi:
            doi = normalize_doi(params.doi).replace('"', "")
            values += [f'"{doi}"', f'"https://doi.org/{doi}"']
        if params.pmid:
            pmid = re.sub(r"\D", "", params.pmid)
            if pmid:
                values.append(f'"{pmid}"')
        if not values:
            return ToolResult(source=self.name, tool="records_for_article", notes=["give doi or pmid"])
        query = "metadata.related_identifiers.identifier:(" + " OR ".join(values) + ")"
        result = await self._query("records_for_article", {"q": query}, params.limit, _page(params.cursor))
        if not result.records:
            result.notes.append("no Zenodo record lists this article in its related identifiers")
        return result
