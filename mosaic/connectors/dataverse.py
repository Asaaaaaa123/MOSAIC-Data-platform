"""Dataverse connector (Harvard Dataverse and a few other large public Dataverse installations).

Docs: https://guides.dataverse.org/en/latest/api/ (Search API: /api/search; Native API:
/api/datasets/:persistentId/?persistentId=doi:...)
Auth: none for published data. Rate limits are installation-specific and unpublished; 3 req/s.

Hosts are FIXED (no user-supplied servers): ``installation`` picks one of
:data:`INSTALLATIONS`, each verified live (search + native dataset GET) in 2026-10.

Quirks (verified live):
- Search: ``q`` is Solr syntax; field queries work on indexed metadata, e.g.
  ``publicationIDNumber:"30824204"`` (related publication PMID/DOI), ``authorName:"Smith"``,
  ``keyword:"RNA-seq"``, ``subject:"Medicine, Health and Life Sciences"``. ``per_page`` max is 1000
  (we cap at 100); offset pagination with ``start``; ``sort=date&order=desc`` for newest first.
- Search items lack related publications; the native API has them in the ``publication`` compound
  field of the citation block (publicationIDType doi/pmid/..., publicationIDNumber, publicationURL).
- The native API answers 404 for unknown persistent ids.

Identifiers: ``dataverse`` (persistent id ``doi:10.7910/DVN/XXXXXX`` with an upper-cased DOI part)
and ``doi``. Related publications (pmid/doi) go to ``attributes["related"]``.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, model_validator

from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, strip_html, tool

INSTALLATIONS: dict[str, str] = {
    "harvard": "https://dataverse.harvard.edu",
    "borealis": "https://borealisdata.ca",
    "dataverseno": "https://dataverse.no",
    "dataverse_nl": "https://dataverse.nl",
}
Installation = Literal["harvard", "borealis", "dataverseno", "dataverse_nl"]

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_PID_RE = re.compile(r"^(?:doi:10\.\d{4,9}/\S+|hdl:\d[\d.]*/\S+|perma:\S+)$", re.IGNORECASE)


class DataverseSearchParams(SearchParams):
    limit: int = Field(10, ge=1, le=100, description="Maximum number of datasets to return")
    installation: Installation = Field(
        "harvard",
        description="Dataverse installation: harvard (default, largest), borealis (Canada), dataverseno (Norway), dataverse_nl (Netherlands)",
    )
    sort: Literal["relevance", "date", "name"] = Field("relevance", description="Sort order (date = newest first)")


class GetDatasetParams(ToolParams):
    persistent_id: str = Field(
        description="Dataset persistent id, e.g. doi:10.7910/DVN/QRYIQZ (a bare DOI or doi.org URL is also accepted)"
    )
    installation: Installation = Field("harvard", description="Installation hosting the dataset (default harvard)")
    files_limit: int = Field(50, ge=0, le=200, description="Maximum number of files to list (0 = none)")


class DatasetsForArticleParams(ToolParams):
    doi: str | None = Field(None, description="DOI of the related publication, e.g. 10.1016/S1470-2045(18)30909-4")
    pmid: str | None = Field(None, description="PubMed ID of the related publication")
    installation: Installation = Field("harvard", description="Installation to search (default harvard)")
    limit: int = Field(20, ge=1, le=100, description="Maximum number of datasets to return")

    @model_validator(mode="after")
    def _one(self) -> "DatasetsForArticleParams":
        if not (self.doi or self.pmid):
            raise ValueError("give doi or pmid")
        return self


def _pid(value: str) -> str | None:
    text = value.strip()
    if _PID_RE.match(text):
        if text.lower().startswith("doi:"):
            return "doi:" + text[4:].upper()
        return text
    match = _DOI_RE.search(text)
    if match:
        return "doi:" + normalize_doi(match.group(0)).upper()
    return None


def _author(name: str | None) -> str:
    name = (name or "").strip()
    if name.count(",") == 1:
        family, given = (x.strip() for x in name.split(","))
        if family and given:
            return f"{given} {family}"
    return name


def _ids(global_id: str | None) -> dict[str, str]:
    pid = _pid(global_id or "") or (global_id or "")
    out = {"dataverse": pid}
    if pid.lower().startswith("doi:"):
        out["doi"] = pid[4:]
    return out


def _search_record(item: dict[str, Any], installation: str) -> Record:
    gid = item.get("global_id") or ""
    version = None
    if item.get("majorVersion") is not None:
        version = f"{item.get('majorVersion')}.{item.get('minorVersion') or 0}"
    attributes = {
        "installation": installation,
        "publisher": item.get("publisher"),
        "collection": item.get("name_of_dataverse"),
        "keywords": (item.get("keywords") or [])[:20] or None,
        "subjects": item.get("subjects") or None,
        "file_count": item.get("fileCount"),
        "version": version,
        "status": item.get("versionState"),
    }
    return Record(
        source="dataverse",
        id=_ids(gid).get("dataverse") or gid,
        type="dataset",
        title=strip_html(item.get("name")),
        url=item.get("url") or None,
        identifiers=_ids(gid),
        date=(item.get("published_at") or "")[:10] or None,
        authors=[a for a in (_author(x) for x in item.get("authors") or []) if a],
        summary=strip_html(item.get("description")),
        attributes={k: v for k, v in attributes.items() if v not in (None, [], "", {})},
    )


def _field_values(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten a metadata block's field list into {typeName: python value}."""
    out: dict[str, Any] = {}
    for field in fields or []:
        out[field.get("typeName")] = _unwrap(field.get("value"))
    return out


def _unwrap(value: Any) -> Any:
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    if isinstance(value, dict):
        if "typeName" in value and "value" in value:
            return _unwrap(value["value"])
        return {k: _unwrap(v) for k, v in value.items()}
    return value


def _publication_entry(pub: dict[str, Any]) -> dict[str, str] | None:
    kind = str(pub.get("publicationIDType") or "").lower()
    number = str(pub.get("publicationIDNumber") or "").strip()
    url = str(pub.get("publicationURL") or "").strip()
    relation = pub.get("publicationRelationType") or "IsSupplementTo"
    entry: dict[str, str] | None = None
    if kind == "pmid" and re.sub(r"\D", "", number):
        entry = {"relation": relation, "scheme": "pmid", "id": re.sub(r"\D", "", number)}
    elif (kind == "doi" and number) or _DOI_RE.search(number) or _DOI_RE.search(url):
        match = _DOI_RE.search(number) or _DOI_RE.search(url)
        if match:
            entry = {"relation": relation, "scheme": "doi", "id": normalize_doi(match.group(0)).rstrip(".")}
    elif kind and number:
        entry = {"relation": relation, "scheme": kind, "id": number}
    elif url:
        entry = {"relation": relation, "scheme": "url", "id": url}
    if entry and pub.get("publicationCitation"):
        entry["citation"] = strip_html(str(pub["publicationCitation"]))[:300] if pub["publicationCitation"] else ""
    return entry


def _native_record(data: dict[str, Any], installation: str, base: str, files_limit: int) -> Record:
    latest = data.get("latestVersion") or {}
    blocks = latest.get("metadataBlocks") or {}
    citation = _field_values((blocks.get("citation") or {}).get("fields") or [])
    pid = latest.get("datasetPersistentId") or (
        f"{data.get('protocol')}:{data.get('authority')}{data.get('separator') or '/'}{data.get('identifier')}"
    )
    authors = [
        _author(a.get("authorName")) for a in citation.get("author") or [] if isinstance(a, dict) and a.get("authorName")
    ]
    orcids = [
        a.get("authorIdentifier")
        for a in citation.get("author") or []
        if isinstance(a, dict) and str(a.get("authorIdentifierScheme") or "").upper() == "ORCID" and a.get("authorIdentifier")
    ]
    descriptions = [
        d.get("dsDescriptionValue") for d in citation.get("dsDescription") or [] if isinstance(d, dict) and d.get("dsDescriptionValue")
    ]
    keywords = [k.get("keywordValue") for k in citation.get("keyword") or [] if isinstance(k, dict) and k.get("keywordValue")]
    related = [e for e in (_publication_entry(p) for p in citation.get("publication") or [] if isinstance(p, dict)) if e]
    for ds in citation.get("relatedDatasets") or []:
        match = _DOI_RE.search(str(ds))
        if match:
            related.append({"relation": "IsRelatedTo", "scheme": "doi", "id": normalize_doi(match.group(0))})
    grants = [
        " ".join(x for x in (g.get("grantNumberAgency"), g.get("grantNumberValue")) if x)
        for g in citation.get("grantNumber") or []
        if isinstance(g, dict)
    ]
    other_ids = [
        f"{o.get('otherIdAgency')}: {o.get('otherIdValue')}" if o.get("otherIdAgency") else o.get("otherIdValue")
        for o in citation.get("otherId") or []
        if isinstance(o, dict) and o.get("otherIdValue")
    ]
    files = latest.get("files") or []
    license_ = latest.get("license") or {}
    if isinstance(license_, dict):
        license_name = license_.get("rightsIdentifier") or license_.get("name")
    else:
        license_name = license_ or latest.get("termsOfUse")
    file_list = []
    for f in files[:files_limit]:
        df = f.get("dataFile") or {}
        file_list.append(
            {
                "name": df.get("filename") or f.get("label"),
                "size": df.get("filesize"),
                "content_type": df.get("contentType"),
                "md5": (df.get("checksum") or {}).get("value") or df.get("md5"),
                "restricted": bool(f.get("restricted")),
                "link": f"{base}/api/access/datafile/{df['id']}" if df.get("id") else None,
            }
        )
    ids = _ids(pid)
    attributes = {
        "installation": installation,
        "publisher": data.get("publisher"),
        "version": f"{latest.get('versionNumber')}.{latest.get('versionMinorNumber') or 0}"
        if latest.get("versionNumber") is not None
        else None,
        "status": latest.get("versionState"),
        "license": license_name,
        "subjects": citation.get("subject") or None,
        "keywords": keywords[:20] or None,
        "metadata_blocks": sorted(blocks.keys()) or None,
        "grants": [g for g in grants if g][:10] or None,
        "other_ids": other_ids[:10] or None,
        "author_orcids": orcids[:10] or None,
        "file_count": len(files),
        "total_size_bytes": sum(int((f.get("dataFile") or {}).get("filesize") or 0) for f in files) or None,
        "restricted_files": sum(1 for f in files if f.get("restricted")) or None,
        "files": file_list or None,
        "related": related[:30] or None,
    }
    return Record(
        source="dataverse",
        id=ids.get("dataverse") or str(data.get("id")),
        type="dataset",
        title=strip_html(citation.get("title")),
        url=f"{base}/dataset.xhtml?persistentId={ids.get('dataverse')}",
        identifiers=ids,
        date=data.get("publicationDate") or latest.get("publicationDate"),
        authors=[a for a in authors if a],
        summary=strip_html(" ".join(str(d) for d in descriptions)) if descriptions else None,
        attributes={k: v for k, v in attributes.items() if v not in (None, [], "", {})},
    )


class Dataverse(Connector):
    name = "dataverse"
    title = "Harvard Dataverse"
    category = "datasets"
    description = (
        "Dataverse repositories: Harvard Dataverse (the largest public installation, 200k+ datasets across "
        "social, health and life sciences) plus Borealis, DataverseNO and DataverseNL. NIH generalist "
        "repository; best for tabular/survey/clinical-study data and replication data linked to papers."
    )
    homepage = "https://dataverse.harvard.edu/"
    docs_url = "https://guides.dataverse.org/en/latest/api/"
    auth = "none"
    rate_limit = 3.0
    health_query = "cancer"
    nih_repositories = ("Dataverse",)

    async def _search(
        self, tool_name: str, installation: str, query: str, limit: int, offset: int, sort: str = "relevance"
    ) -> ToolResult:
        base = INSTALLATIONS[installation]
        response = await self.request(
            "GET",
            f"{base}/api/search",
            params={
                "q": query,
                "type": "dataset",
                "per_page": limit,
                "start": offset,
                "sort": sort if sort != "relevance" else None,
                "order": ("desc" if sort == "date" else "asc") if sort != "relevance" else None,
            },
        )
        data = (response.json() or {}).get("data") or {}
        items = data.get("items") or []
        total = data.get("total_count")
        nxt = offset + len(items)
        return ToolResult(
            source=self.name,
            tool=tool_name,
            records=[_search_record(i, installation) for i in items],
            total=total,
            next_cursor=str(nxt) if items and total is not None and nxt < total else None,
            request_url=response.url,
        )

    @tool(
        description="""
        Search published datasets in Harvard Dataverse (default) or another public Dataverse
        installation (borealis, dataverseno, dataverse_nl). Returns persistent id (doi:...), DOI,
        title, authors, publication date, description, keywords, subjects, file count and version.
        Use for social-science, public-health, clinical-study and replication datasets. Query is Solr
        syntax: words, "phrases", AND/OR, and field queries such as authorName:"Smith",
        keyword:"RNA-seq", subject:"Medicine, Health and Life Sciences", publicationIDNumber:"<pmid>".
        Max 100 per page; sort relevance|date|name.
        """,
        federated=True,
    )
    async def search(self, params: DataverseSearchParams) -> ToolResult:
        return await self._search(
            "search", params.installation, params.query, params.limit, parse_offset(params.cursor), params.sort
        )

    @tool(
        description="""
        Fetch one Dataverse dataset by persistent id (doi:10.7910/DVN/XXXXXX; bare DOI accepted):
        citation metadata (title, authors with ORCIDs, description, subjects, keywords, grants,
        other ids), license, version, related publications (PMID/DOI in attributes.related) and the
        file list (name, size, content type, md5, restricted flag, download link).
        """
    )
    async def get_dataset(self, params: GetDatasetParams) -> ToolResult:
        pid = _pid(params.persistent_id)
        if not pid:
            return ToolResult(source=self.name, tool="get_dataset", notes=[f"not a persistent id: {params.persistent_id!r}"])
        base = INSTALLATIONS[params.installation]
        response = await self.request(
            "GET", f"{base}/api/datasets/:persistentId/", params={"persistentId": pid}, allow_status=(404, 403)
        )
        if response.status in (403, 404):
            return ToolResult(
                source=self.name,
                tool="get_dataset",
                notes=[
                    f"dataset {pid} not found in {params.installation}"
                    + (" (not public)" if response.status == 403 else "")
                    + "; check the installation parameter"
                ],
                request_url=response.url,
            )
        data = (response.json() or {}).get("data") or {}
        record = _native_record(data, params.installation, base, params.files_limit)
        notes = []
        if record.attributes.get("file_count", 0) > params.files_limit and params.files_limit:
            notes.append(f"dataset has {record.attributes['file_count']} files; listed the first {params.files_limit}")
        return ToolResult(
            source=self.name, tool="get_dataset", records=[record], total=1, request_url=response.url, notes=notes
        )

    @tool(
        description="""
        Find Dataverse datasets whose "related publication" metadata cites a given article, by
        PMID or DOI (Solr publicationIDNumber field). Use to get replication / underlying data of a
        paper deposited in Harvard Dataverse (or another installation). Example: pmid=30824204.
        """
    )
    async def datasets_for_article(self, params: DatasetsForArticleParams) -> ToolResult:
        values: list[str] = []
        if params.pmid:
            pmid = re.sub(r"\D", "", params.pmid)
            if pmid:
                values.append(f'publicationIDNumber:"{pmid}"')
        if params.doi:
            match = _DOI_RE.search(params.doi)
            if match:
                doi = normalize_doi(match.group(0)).replace('"', "")
                values += [
                    f'publicationIDNumber:"{doi}"',
                    f'publicationIDNumber:"doi:{doi}"',
                    f'publicationIDNumber:"https://doi.org/{doi}"',
                    f'publicationURL:"https://doi.org/{doi}"',
                ]
        if not values:
            return ToolResult(source=self.name, tool="datasets_for_article", notes=["no usable doi/pmid given"])
        result = await self._search("datasets_for_article", params.installation, " OR ".join(values), params.limit, 0)
        if not result.records:
            result.notes.append("no dataset in this installation lists the article as a related publication")
        return result
