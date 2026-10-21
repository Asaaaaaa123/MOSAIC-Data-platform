"""Dryad connector (curated open research data, mostly life & environmental sciences).

Docs: https://datadryad.org/api/v2/docs (API notes: https://github.com/datadryad/dryad-app/tree/main/documentation/apis)
Auth: none for published datasets (an API account only raises rate limits / shows unpublished data).
Rate limit: Dryad throttles anonymous clients (commonly cited as 30 req/min); we use 0.5 req/s with a
small burst so get_dataset's 3 calls are not slowed down.

Quirks (verified live, 2026-10):
- ``/search?q=&per_page=&page=`` returns HAL JSON: ``_embedded["stash:datasets"]``, ``total``.
  ``q`` also matches DOIs inside ``relatedWorks``, so ``q="10.1016/j.neuropharm.2024.110259"``
  finds the datasets supplementing that article.
- Datasets are addressed by URL-encoded DOI: ``/datasets/doi%3A10.5061%2Fdryad.xxxx``.
  The dataset payload links its latest version (``_links["stash:version"]``); files are listed at
  ``/versions/{id}/files`` (paged, ``per_page`` max 100).
- Author objects contain e-mail addresses: never copied into records.

Identifiers: ``doi`` and ``dryad`` (both the dataset DOI). ``relatedWorks`` (primary_article,
article, dataset, software, preprint ...) go to ``attributes["related"]``.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from pydantic import Field, model_validator

from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, strip_html, tool

BASE = "https://datadryad.org"
API = f"{BASE}/api/v2"

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)


class DryadSearchParams(SearchParams):
    pass


class GetDatasetParams(ToolParams):
    doi: str = Field(
        description="Dryad dataset DOI: 10.5061/dryad.xxxx, doi:10.5061/dryad.xxxx, or a datadryad.org / doi.org URL"
    )
    include_versions: bool = Field(True, description="Also list the dataset's versions")
    files_limit: int = Field(50, ge=0, le=100, description="Maximum number of files of the latest version to list (0 = none)")


class DatasetsForArticleParams(ToolParams):
    doi: str = Field(description="DOI of the article, e.g. 10.1016/j.neuropharm.2024.110259")
    limit: int = Field(20, ge=1, le=100, description="Maximum number of datasets to return")

    @model_validator(mode="after")
    def _check(self) -> "DatasetsForArticleParams":
        if not _DOI_RE.search(self.doi):
            raise ValueError("doi must look like 10.xxxx/...")
        return self


def _page(cursor: str | None) -> int:
    try:
        return max(1, int(cursor or 1))
    except ValueError:
        return 1


def _bare_doi(identifier: str | None) -> str:
    match = _DOI_RE.search(identifier or "")
    return normalize_doi(match.group(0)) if match else ""


def _related(work: dict[str, Any]) -> dict[str, str] | None:
    ident = str(work.get("identifier") or "").strip()
    if not ident:
        return None
    kind = (work.get("identifierType") or "").lower()
    relation = work.get("relationship") or "related"
    if kind == "doi" or _DOI_RE.search(ident):
        doi = _bare_doi(ident)
        if doi:
            return {"relation": relation, "scheme": "doi", "id": doi.rstrip(".")}
    if kind == "pmid":
        return {"relation": relation, "scheme": "pmid", "id": re.sub(r"\D", "", ident)}
    return {"relation": relation, "scheme": kind or "url", "id": ident}


def _record(item: dict[str, Any]) -> Record:
    doi = _bare_doi(item.get("identifier"))
    authors, orcids = [], []
    for a in item.get("authors") or []:
        name = " ".join(x for x in (a.get("firstName"), a.get("lastName")) if x).strip()
        if name:
            authors.append(name)
        if a.get("orcid"):
            orcids.append(a["orcid"])
    funders = []
    for f in item.get("funders") or []:
        org = f.get("organization")
        if org:
            funders.append(f"{org} ({f['awardNumber']})" if f.get("awardNumber") else org)
    metrics = item.get("metrics") or {}
    related = [r for r in (_related(w) for w in item.get("relatedWorks") or []) if r]
    license_ = item.get("license") or ""
    license_ = re.sub(r"^https://spdx\.org/licenses/(.+?)\.html$", r"\1", license_) or None
    attributes = {
        "keywords": (item.get("keywords") or [])[:20] or None,
        "field_of_science": item.get("fieldOfScience"),
        "license": license_,
        "storage_size_bytes": item.get("storageSize"),
        "version_number": item.get("versionNumber"),
        "curation_status": item.get("curationStatus"),
        "last_modified": item.get("lastModificationDate"),
        "funders": funders[:10] or None,
        "views": metrics.get("views"),
        "downloads": metrics.get("downloads"),
        "citations": metrics.get("citations"),
        "related_publication_issn": item.get("relatedPublicationISSN"),
        "author_orcids": orcids[:10] or None,
        "related": related[:30] or None,
    }
    url = item.get("sharingLink") or (f"{BASE}/dataset/doi:{doi}" if doi else None)
    if url and url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return Record(
        source="dryad",
        id=doi or str(item.get("id") or ""),
        type="dataset",
        title=strip_html(item.get("title")),
        url=url,
        identifiers={"doi": doi, "dryad": doi},
        date=item.get("publicationDate"),
        authors=authors,
        summary=strip_html(item.get("abstract")),
        attributes={k: v for k, v in attributes.items() if v not in (None, [], "", {})},
    )


class Dryad(Connector):
    name = "dryad"
    title = "Dryad"
    category = "datasets"
    description = (
        "Dryad: curated, CC0-licensed research data (~70k datasets), mostly ecology, evolution, biology "
        "and medicine, each with a DOI and linked to its primary article. NIH generalist repository; best "
        "for the data underlying published papers."
    )
    homepage = "https://datadryad.org/"
    docs_url = "https://datadryad.org/api/v2/docs"
    auth = "none"
    rate_limit = 0.5
    rate_burst = 4
    health_query = "microbiome"
    nih_repositories = ("Dryad",)

    @tool(
        description="""
        Search Dryad datasets by free text (title, abstract, keywords, authors, related DOIs).
        Returns dataset DOI, title, authors, publication date, abstract, keywords, license, size,
        funders, metrics and related works (primary article DOI etc. in attributes.related).
        Use for curated data behind ecology/evolution/biomedical papers. A quoted article DOI as the
        query finds the datasets linked to that article. Max 100 per page.
        """,
        federated=True,
    )
    async def search(self, params: DryadSearchParams) -> ToolResult:
        page = _page(params.cursor)
        response = await self.request(
            "GET", f"{API}/search", params={"q": params.query, "per_page": params.limit, "page": page}
        )
        payload = response.json() or {}
        items = (payload.get("_embedded") or {}).get("stash:datasets") or []
        total = payload.get("total")
        has_next = bool(items) and (total is None or page * params.limit < total)
        return ToolResult(
            source=self.name,
            tool="search",
            records=[_record(i) for i in items],
            total=total,
            next_cursor=str(page + 1) if has_next else None,
            request_url=response.url,
        )

    @tool(
        description="""
        Fetch one Dryad dataset by DOI (10.5061/dryad.xxxx; doi: prefix and datadryad.org / doi.org
        URLs accepted): full metadata, related works (primary article, other datasets, software),
        funders, license, metrics, the version history and the file list of the latest version
        (path, size, mime type, digest, download link).
        """
    )
    async def get_dataset(self, params: GetDatasetParams) -> ToolResult:
        doi = _bare_doi(params.doi)
        if not doi:
            return ToolResult(source=self.name, tool="get_dataset", notes=[f"not a DOI: {params.doi!r}"])
        encoded = quote(f"doi:{doi}", safe="")
        response = await self.request("GET", f"{API}/datasets/{encoded}", allow_status=(404,))
        if response.status == 404:
            return ToolResult(
                source=self.name, tool="get_dataset", notes=[f"Dryad dataset {doi} not found"], request_url=response.url
            )
        item = response.json() or {}
        record = _record(item)
        notes: list[str] = []
        if params.include_versions:
            vresp = await self.request("GET", f"{API}/datasets/{encoded}/versions", allow_status=(404,))
            if vresp.status == 200:
                versions = ((vresp.json() or {}).get("_embedded") or {}).get("stash:versions") or []
                record.attributes["versions"] = [
                    {
                        "version": v.get("versionNumber"),
                        "status": v.get("versionStatus"),
                        "date": v.get("publicationDate") or v.get("lastModificationDate"),
                        "changes": v.get("versionChanges"),
                    }
                    for v in versions[-25:]
                ]
        version_href = ((item.get("_links") or {}).get("stash:version") or {}).get("href")
        if params.files_limit and version_href and re.fullmatch(r"/api/v2/versions/\d+", version_href):
            fresp = await self.request(
                "GET", f"{BASE}{version_href}/files", params={"per_page": params.files_limit}, allow_status=(401, 403, 404)
            )
            if fresp.status == 200:
                fpayload = fresp.json() or {}
                files = (fpayload.get("_embedded") or {}).get("stash:files") or []
                record.attributes["file_count"] = fpayload.get("total", len(files))
                record.attributes["files"] = [
                    {
                        "name": f.get("path"),
                        "size": f.get("size"),
                        "mimetype": f.get("mimeType") or None,
                        "digest": f"{f.get('digestType')}:{f.get('digest')}" if f.get("digest") else None,
                        "link": BASE + (((f.get("_links") or {}).get("stash:download") or {}).get("href") or "")
                        if ((f.get("_links") or {}).get("stash:download") or {}).get("href")
                        else None,
                    }
                    for f in files
                ]
                if (fpayload.get("total") or 0) > len(files):
                    notes.append(f"latest version has {fpayload['total']} files; listed the first {len(files)}")
            else:
                notes.append(f"file list unavailable (HTTP {fresp.status})")
        return ToolResult(
            source=self.name, tool="get_dataset", records=[record], total=1, request_url=response.url, notes=notes
        )

    @tool(
        description="""
        Find Dryad datasets linked to a journal article by the article's DOI (Dryad relatedWorks:
        primary_article, article, preprint ...). Returns only datasets whose related works really
        contain the DOI. Use to get the Dryad data behind a paper; datacite.datasets_for_article
        covers all repositories.
        """
    )
    async def datasets_for_article(self, params: DatasetsForArticleParams) -> ToolResult:
        doi = _bare_doi(params.doi)
        response = await self.request(
            "GET", f"{API}/search", params={"q": f'"{doi}"', "per_page": params.limit, "page": 1}
        )
        payload = response.json() or {}
        items = (payload.get("_embedded") or {}).get("stash:datasets") or []
        records = [_record(i) for i in items]
        linked = [r for r in records if any(x.get("id") == doi for x in r.attributes.get("related") or [])]
        notes = []
        if len(linked) < len(records):
            notes.append(f"{len(records) - len(linked)} text-only match(es) without a relatedWorks link were dropped")
        if not linked:
            notes.append(f"no Dryad dataset lists {doi} in its related works")
        return ToolResult(
            source=self.name,
            tool="datasets_for_article",
            records=linked,
            total=len(linked),
            request_url=response.url,
            notes=notes,
        )
