"""DataCite REST API connector: the cross-repository DOI index for research data.

Docs: https://support.datacite.org/docs/api (REST API reference: https://support.datacite.org/reference)
Auth: none for read access.
Rate limit: DataCite asks clients to stay under ~3000 requests / 5 minutes per IP; we use 5 req/s.

Quirks (verified live, 2026-10):
- ``/dois?query=`` takes Elasticsearch query-string syntax over the DOI metadata
  (``titles.title:``, ``creators.name:``, ``subjects.subject:``, ``publicationYear:[2020 TO 2024]``,
  ``relatedIdentifiers.relatedIdentifier:"10.1038/..."``).
- ``page[cursor]`` pagination silently drops relevance ranking (results come back ordered by
  creation date), so search uses ``page[number]`` pagination (DataCite caps it at the first
  10,000 hits). The cursor we return is the next page number.
- ``fields[dois]=...`` (sparse fieldsets) works on the list endpoint and keeps the base64 ``xml``
  blob out of responses; the single-DOI endpoint ignores it, so ``xml`` is simply never read.
- Zenodo, Figshare and Mendeley mint one DOI per version plus a concept DOI. ``collapse_versions``
  (default on) drops records that are ``IsVersionOf`` / ``IsIdenticalTo`` another DOI.
- Repository ("client") ids were discovered live with ``/clients?query=`` and verified to hold
  DOIs (sample DOI in the comment next to each entry of :data:`REPOSITORY_CLIENTS`).

Identifiers: ``doi`` always; repository-native ids derived from the DOI when the repository is
known (``zenodo`` record id, ``figshare`` article id, ``dryad`` DOI, ``dataverse`` persistent id,
``osf`` GUID, ``synapse`` syn id, ``openneuro`` ds id, ``dandi`` dandiset id; OpenNeuro and DANDI mint
one DOI per version) so DataCite records cluster with the native connectors.
Related works (IsSupplementTo / IsCitedBy / References ... DOIs, PMIDs, URLs) and NCT numbers
mentioned in the metadata go to ``attributes["related"]``.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import quote

from pydantic import Field, model_validator

from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, first, strip_html, tool

API = "https://api.datacite.org"

# Friendly repository name -> DataCite client id (verified live 2026-10-20).
REPOSITORY_CLIENTS: dict[str, str] = {
    "zenodo": "cern.zenodo",  # prefix 10.5281, e.g. 10.5281/zenodo.22929875
    "figshare": "figshare.ars",  # prefix 10.6084 (figshare.com + institutional portals)
    "dryad": "dryad.dryad",  # prefix 10.5061, e.g. 10.5061/dryad.zs7h44jsj
    "harvard_dataverse": "gdcc.harvard-dv",  # prefix 10.7910, e.g. 10.7910/dvn/fgbd71
    "mendeley_data": "bl.mendeley",  # prefix 10.17632, e.g. 10.17632/p4tzrd9mv9.2
    "ieee_dataport": "ieee.dataport",  # prefix 10.21227, e.g. 10.21227/yj0m-mp63
    "vivli": "vivli.search",  # prefix 10.25934, e.g. 10.25934/pr00006235.0 (IPD data packages)
    "synapse": "sagebio.synapse",  # prefix 10.7303, e.g. 10.7303/syn73927029
    "osf": "cos.osf",  # prefix 10.17605, e.g. 10.17605/osf.io/kexug
    "dataverseno": "vjhc.dataverseno",  # prefix 10.18710
}

RepositoryName = Literal[
    "zenodo",
    "figshare",
    "dryad",
    "harvard_dataverse",
    "mendeley_data",
    "ieee_dataport",
    "vivli",
    "synapse",
    "osf",
    "dataverseno",
]

ResourceType = Literal[
    "dataset", "software", "text", "journal-article", "preprint", "collection", "image", "any"
]

_LIST_FIELDS = (
    "doi,titles,creators,publisher,publicationYear,types,subjects,descriptions,rightsList,"
    "relatedIdentifiers,alternateIdentifiers,sizes,formats,url,dates,version,fundingReferences,"
    "citationCount,viewCount,downloadCount,created,registered,client"
)

_CLIENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,60}$")
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_NCT_RE = re.compile(r"\bNCT\d{8}\b", re.IGNORECASE)
_VERSION_RELATIONS = ("IsVersionOf", "IsIdenticalTo")

_TYPE_MAP = {
    "dataset": "dataset",
    "collection": "dataset",
    "datapaper": "article",
    "journalarticle": "article",
    "text": "article",
    "conferencepaper": "article",
    "preprint": "preprint",
    "book": "book",
    "bookchapter": "book",
    "studyregistration": "study",
}


class SearchDatasetsParams(SearchParams):
    resource_type: ResourceType = Field(
        "dataset",
        description="DataCite resource type to restrict to (default dataset); 'any' for all types",
    )
    repository: RepositoryName | None = Field(
        None,
        description=(
            "Restrict to one repository: zenodo, figshare, dryad, harvard_dataverse, mendeley_data, "
            "ieee_dataport, vivli, synapse, osf, dataverseno"
        ),
    )
    client_id: str | None = Field(
        None, description="Raw DataCite client id (from the repositories tool), e.g. 'cern.zenodo'"
    )
    year_from: int | None = Field(None, description="Only DOIs with publicationYear >= this")
    year_to: int | None = Field(None, description="Only DOIs with publicationYear <= this")
    sort: Literal["relevance", "-published", "-created", "-citation-count", "-view-count"] = Field(
        "relevance", description="Sort order (default relevance)"
    )
    collapse_versions: bool = Field(
        True,
        description="Hide per-version DOIs (IsVersionOf / IsIdenticalTo another DOI) so each dataset appears once",
    )


class GetDoiParams(ToolParams):
    doi: str = Field(description="DataCite DOI, with or without https://doi.org/ or doi: prefix, e.g. 10.5061/dryad.zs7h44jsj")


class DatasetsForArticleParams(ToolParams):
    doi: str | None = Field(None, description="Article DOI, e.g. 10.1073/pnas.2618249123")
    pmid: str | None = Field(None, description="PubMed ID of the article (digits)")
    resource_type: ResourceType = Field(
        "any", description="Restrict linked items by DataCite resource type (default any: supplements are often typed Text/Image)"
    )
    collapse_versions: bool = Field(True, description="Hide per-version DOIs so each item appears once")
    limit: int = Field(25, ge=1, le=100, description="Maximum number of records to return")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous result)")

    @model_validator(mode="after")
    def _one_id(self) -> "DatasetsForArticleParams":
        if not (self.doi or self.pmid):
            raise ValueError("give doi or pmid")
        return self


class RepositoriesParams(ToolParams):
    query: str = Field(description="Repository name or keyword, e.g. 'zenodo', 'dataverse', 'genomics'")
    limit: int = Field(10, ge=1, le=100, description="Maximum number of repositories to return")


# --------------------------------------------------------------------------- helpers


def _page(cursor: str | None) -> int:
    try:
        return max(1, int(cursor or 1))
    except ValueError:
        return 1


def repository_ids(doi: str) -> dict[str, str]:
    """Repository-native identifiers implied by a (normalized) DOI."""
    out: dict[str, str] = {}
    if m := re.match(r"^10\.5281/zenodo\.(\d+)$", doi):
        out["zenodo"] = m.group(1)
    elif m := re.match(r"^10\.6084/m9\.figshare\.(\d+)(?:\.v\d+)?$", doi):
        out["figshare"] = m.group(1)
    elif doi.startswith("10.5061/dryad."):
        out["dryad"] = doi
    elif doi.startswith("10.7910/dvn/"):
        out["dataverse"] = "doi:" + doi.upper()
    elif m := re.match(r"^10\.17605/osf\.io/([a-z0-9]+)$", doi):
        out["osf"] = m.group(1)
    elif m := re.match(r"^10\.7303/(syn\d+)", doi):
        out["synapse"] = m.group(1)
    elif m := re.match(r"^10\.18112/openneuro\.(ds\d{6})(?:\.v[\d.]+)?$", doi):
        out["openneuro"] = m.group(1)  # version DOIs (...v1.0.8) differ per snapshot; the dataset id does not
    elif m := re.match(r"^10\.48324/dandi\.(\d{6})(?:/[\d.]+)?$", doi):
        out["dandi"] = m.group(1)
    return out


def _related_entry(relation: str | None, scheme: str | None, value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    value = str(value).strip()
    scheme_l = (scheme or "").lower()
    if scheme_l == "doi" or (scheme_l in ("url", "") and _DOI_RE.search(value)):
        match = _DOI_RE.search(value)
        if not match:
            return None
        key, ident = "doi", normalize_doi(match.group(0)).rstrip(".")
    elif scheme_l == "pmid":
        digits = re.sub(r"\D", "", value)
        if not digits:
            return None
        key, ident = "pmid", digits
    elif scheme_l == "pmcid":
        key, ident = "pmcid", value.upper()
    else:
        key, ident = scheme_l or "other", value
    entry = {"relation": relation or "related", "scheme": key, "id": ident}
    return entry


def _date(attrs: dict[str, Any]) -> str | None:
    dates = attrs.get("dates") or []
    for wanted in ("Issued", "Available", "Created"):
        for d in dates:
            if d.get("dateType") == wanted and d.get("date"):
                text = str(d["date"])[:10]
                if re.match(r"^\d{4}(-\d{2}){0,2}$", text):
                    return text
    year = attrs.get("publicationYear")
    return str(year) if year else None


def _author(creator: dict[str, Any]) -> str:
    given, family = creator.get("givenName"), creator.get("familyName")
    if given and family:
        return f"{given} {family}"
    name = (creator.get("name") or "").strip()
    if creator.get("nameType") != "Organizational" and name.count(",") == 1:
        family, given = (x.strip() for x in name.split(","))
        if given and family:
            return f"{given} {family}"
    return name


def _record(item: dict[str, Any]) -> Record:
    attrs = item.get("attributes") or {}
    doi = normalize_doi(attrs.get("doi") or item.get("id") or "")
    types = attrs.get("types") or {}
    general = types.get("resourceTypeGeneral") or ""
    rtype = _TYPE_MAP.get(general.lower(), "other")
    titles = attrs.get("titles") or []
    title = next((t.get("title") for t in titles if not t.get("titleType")), None) or first(
        [t.get("title") for t in titles if t.get("title")]
    )
    descriptions = attrs.get("descriptions") or []
    abstract = next(
        (d.get("description") for d in descriptions if d.get("descriptionType") == "Abstract" and d.get("description")),
        None,
    ) or first([d.get("description") for d in descriptions if d.get("description")])
    if isinstance(abstract, list):
        abstract = " ".join(str(x) for x in abstract)

    identifiers: dict[str, Any] = {"doi": doi, **repository_ids(doi)}
    client = ((item.get("relationships") or {}).get("client") or {}).get("data") or {}
    if client.get("id") == "sagebio.synapse" and "synapse" not in identifiers:
        syn = re.search(r"syn\d+", attrs.get("url") or "")
        if syn:
            identifiers["synapse"] = syn.group(0)
    for alt in attrs.get("alternateIdentifiers") or []:
        kind = (alt.get("alternateIdentifierType") or "").lower()
        value = alt.get("alternateIdentifier")
        if kind in ("pmid", "pmcid", "arxiv", "isbn", "issn") and value:
            identifiers.setdefault(kind, value)

    related: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    parts = 0
    for rel in attrs.get("relatedIdentifiers") or []:
        if rel.get("relationType") == "HasPart":
            parts += 1
            continue
        entry = _related_entry(rel.get("relationType"), rel.get("relatedIdentifierType"), rel.get("relatedIdentifier"))
        if entry:
            key = (entry["relation"], entry["scheme"], entry["id"])
            if key not in seen:
                seen.add(key)
                related.append(entry)
    text_blob = " ".join(
        [title or "", abstract or ""] + [s.get("subject") or "" for s in attrs.get("subjects") or []]
    )
    for nct in sorted({m.upper() for m in _NCT_RE.findall(text_blob)}):
        related.append({"relation": "Mentions", "scheme": "nct", "id": nct})

    rights = attrs.get("rightsList") or []
    license_ = first(
        [r.get("rightsIdentifier") for r in rights if r.get("rightsIdentifier")]
        or [r.get("rights") for r in rights if r.get("rights")]
        or [r.get("rightsUri") for r in rights if r.get("rightsUri") and "info:eu-repo" not in r["rightsUri"]]
    )
    subjects = [s.get("subject") for s in attrs.get("subjects") or [] if s.get("subject")]
    funders = []
    for f in attrs.get("fundingReferences") or []:
        name = f.get("funderName")
        if name:
            award = f.get("awardNumber")
            funders.append(f"{name} ({award})" if award else name)
    if isinstance(license_, str) and len(license_) > 160:
        license_ = license_[:157].rstrip() + "..."
    attributes = {
        "publisher": attrs.get("publisher") if isinstance(attrs.get("publisher"), str) else (attrs.get("publisher") or {}).get("name"),
        "repository": client.get("id"),
        "resource_type": general or None,
        "resource_type_detail": types.get("resourceType") if types.get("resourceType") not in (None, general) else None,
        "version": attrs.get("version"),
        "license": license_,
        "subjects": list(dict.fromkeys(subjects))[:15] or None,
        "sizes": (attrs.get("sizes") or [])[:5] or None,
        "formats": list(dict.fromkeys(attrs.get("formats") or []))[:10] or None,
        "parts_count": parts or None,
        "funders": funders[:10] or None,
        "citation_count": attrs.get("citationCount") or None,
        "view_count": attrs.get("viewCount") or None,
        "download_count": attrs.get("downloadCount") or None,
        "related": related[:30] or None,
        "related_total": len(related) if len(related) > 30 else None,
    }
    return Record(
        source="datacite",
        id=doi,
        type=rtype,  # type: ignore[arg-type]
        title=strip_html(title),
        url=attrs.get("url") or (f"https://doi.org/{doi}" if doi else None),
        identifiers=identifiers,
        date=_date(attrs),
        authors=[a for a in (_author(c) for c in attrs.get("creators") or []) if a],
        summary=strip_html(abstract),
        attributes={k: v for k, v in attributes.items() if v not in (None, [], "", {})},
    )


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "").replace('"', "") + '"'


class DataCite(Connector):
    name = "datacite"
    title = "DataCite"
    category = "datasets"
    description = (
        "DataCite DOI registry: metadata for 90M+ DOIs from 3,000+ data repositories (Zenodo, Figshare, "
        "Dryad, Dataverse, Mendeley Data, IEEE DataPort, Vivli, Synapse, OSF ...). Best single index for "
        "finding research datasets across repositories and for linking datasets to the articles they support."
    )
    homepage = "https://datacite.org/"
    docs_url = "https://support.datacite.org/docs/api"
    auth = "none"
    rate_limit = 5.0
    health_query = "single-cell RNA-seq"
    nih_repositories = ("Mendeley Data", "IEEE Dataport", "Vivli")

    @tool(
        description="""
        Search the DataCite DOI index across ALL data repositories (Zenodo, Figshare, Dryad, Harvard
        Dataverse, Mendeley Data, IEEE DataPort, Vivli clinical-trial data packages, Synapse, OSF ...).
        Returns one record per DOI with title, creators, publisher/repository, year, abstract, license,
        subjects and related article DOIs/PMIDs (attributes.related). Default: datasets only, version
        DOIs collapsed. Use as the first stop for "is there public data for X"; use the native
        repository connectors (zenodo, figshare, dryad, dataverse ...) for file lists.
        Query uses Elasticsearch syntax: plain words, "exact phrase", AND/OR/NOT, field queries such
        as titles.title:glioblastoma, creators.name:"Smith", subjects.subject:"RNA-seq".
        Filters: repository (friendly name), client_id, resource_type, year_from/year_to, sort.
        """,
        federated=True,
    )
    async def search_datasets(self, params: SearchDatasetsParams) -> ToolResult:
        page = _page(params.cursor)
        query = f"({params.query})"
        if params.year_from or params.year_to:
            query += f" AND publicationYear:[{params.year_from or '*'} TO {params.year_to or '*'}]"
        if params.collapse_versions:
            query += "".join(f" AND NOT relatedIdentifiers.relationType:{r}" for r in _VERSION_RELATIONS)
        clients = []
        if params.repository:
            clients.append(REPOSITORY_CLIENTS[params.repository])
        if params.client_id:
            cid = params.client_id.strip().lower()
            if not _CLIENT_ID_RE.match(cid):
                return ToolResult(source=self.name, tool="search_datasets", notes=[f"invalid client_id {params.client_id!r}"])
            clients.append(cid)
        return await self._search(
            "search_datasets",
            query,
            limit=params.limit,
            page=page,
            resource_type=params.resource_type,
            client_id=",".join(dict.fromkeys(clients)) or None,
            sort=None if params.sort == "relevance" else params.sort,
        )

    async def _search(
        self,
        tool_name: str,
        query: str,
        *,
        limit: int,
        page: int,
        resource_type: str,
        client_id: str | None = None,
        sort: str | None = None,
    ) -> ToolResult:
        notes: list[str] = []
        if page * limit > 10_000:
            return ToolResult(
                source=self.name,
                tool=tool_name,
                notes=["DataCite page-number pagination stops at 10,000 hits; narrow the query"],
            )
        response = await self.request(
            "GET",
            f"{API}/dois",
            params={
                "query": query,
                "resource-type-id": None if resource_type == "any" else resource_type,
                "client-id": client_id,
                "sort": sort,
                "page[size]": limit,
                "page[number]": page,
                "fields[dois]": _LIST_FIELDS,
            },
        )
        payload = response.json() or {}
        items = payload.get("data") or []
        meta = payload.get("meta") or {}
        total = meta.get("total")
        total_pages = meta.get("totalPages") or 0
        has_next = bool(items) and bool((payload.get("links") or {}).get("next")) and page < total_pages
        if has_next and (page + 1) * limit > 10_000:
            has_next = False
            notes.append("more hits exist but DataCite only pages through the first 10,000; narrow the query")
        return ToolResult(
            source=self.name,
            tool=tool_name,
            records=[_record(i) for i in items],
            total=total,
            next_cursor=str(page + 1) if has_next else None,
            request_url=response.url,
            notes=notes,
        )

    @tool(
        description="""
        Fetch full DataCite metadata for one DOI (any DataCite repository): creators with
        affiliations, publisher, publicationYear, resource type, subjects, abstract, license
        (rightsList), funders, sizes/formats, version, citation/view/download counts and every related
        identifier (supplemented article DOIs, PMIDs, versions, parts) in attributes.related.
        Accepts 10.xxxx/..., doi:10.xxxx/... or https://doi.org/10.xxxx/... . Crossref article DOIs
        are not in DataCite (use crossref.get_work for those).
        """
    )
    async def get_doi(self, params: GetDoiParams) -> ToolResult:
        doi = normalize_doi(params.doi)
        response = await self.request(
            "GET", f"{API}/dois/{quote(doi, safe='/:;()')}", allow_status=(404,)
        )
        if response.status == 404:
            return ToolResult(
                source=self.name,
                tool="get_doi",
                notes=[f"DOI {doi} not found in DataCite (Crossref-registered article DOIs are not indexed here)"],
                request_url=response.url,
            )
        item = (response.json() or {}).get("data") or {}
        return ToolResult(
            source=self.name, tool="get_doi", records=[_record(item)], total=1, request_url=response.url
        )

    @tool(
        description="""
        Find datasets (and other DataCite items: supplements, code, figures) whose metadata links to
        a given ARTICLE via relatedIdentifiers (IsSupplementTo, IsCitedBy, IsReferencedBy, Cites,
        IsDescribedBy ...). Input: the article's DOI (e.g. 10.1073/pnas.2618249123) or PMID.
        This is the dataset-to-paper bridge: use it to find the data behind a publication across all
        repositories. Each record's attributes.related shows the exact relation type.
        """
    )
    async def datasets_for_article(self, params: DatasetsForArticleParams) -> ToolResult:
        parts = []
        if params.doi:
            doi = normalize_doi(params.doi)
            parts.append(
                f"relatedIdentifiers.relatedIdentifier:({_quoted(doi)} OR {_quoted('https://doi.org/' + doi)})"
            )
        if params.pmid:
            pmid = re.sub(r"\D", "", params.pmid)
            if not pmid:
                return ToolResult(source=self.name, tool="datasets_for_article", notes=["pmid must contain digits"])
            parts.append(
                f"(relatedIdentifiers.relatedIdentifier:{_quoted(pmid)} AND relatedIdentifiers.relatedIdentifierType:PMID)"
            )
        query = "(" + " OR ".join(parts) + ")"
        if params.collapse_versions:
            query += "".join(f" AND NOT relatedIdentifiers.relationType:{r}" for r in _VERSION_RELATIONS)
        result = await self._search(
            "datasets_for_article",
            query,
            limit=params.limit,
            page=_page(params.cursor),
            resource_type=params.resource_type,
        )
        if not result.records:
            result.notes.append(
                "no DataCite item references this article; also try the repository connectors' searches with the DOI"
            )
        return result

    @tool(
        description="""
        Search DataCite repositories ("clients": the data repositories that mint DOIs) by name or
        keyword. Returns client id (usable as search_datasets client_id), name, homepage, DOI
        prefixes, re3data id and description. Use to discover whether a repository registers DOIs
        with DataCite and how to filter on it. data lists the built-in friendly repository mapping.
        """
    )
    async def repositories(self, params: RepositoriesParams) -> ToolResult:
        response = await self.request(
            "GET", f"{API}/clients", params={"query": params.query, "page[size]": params.limit}
        )
        payload = response.json() or {}
        records = []
        for item in payload.get("data") or []:
            attrs = item.get("attributes") or {}
            prefixes = [
                p.get("id")
                for p in ((item.get("relationships") or {}).get("prefixes") or {}).get("data") or []
                if p.get("id")
            ]
            re3data = attrs.get("re3data")
            records.append(
                Record(
                    source=self.name,
                    id=item.get("id"),
                    type="repository",
                    title=attrs.get("name"),
                    url=attrs.get("url"),
                    identifiers={"re3data": re3data.replace("https://doi.org/", "") if re3data else None},
                    date=(attrs.get("created") or "")[:10] or None,
                    summary=strip_html(attrs.get("description")),
                    attributes={
                        k: v
                        for k, v in {
                            "client_id": item.get("id"),
                            "symbol": attrs.get("symbol"),
                            "client_type": attrs.get("clientType"),
                            "prefixes": prefixes[:10] or None,
                            "active": attrs.get("isActive"),
                            "year": attrs.get("year"),
                        }.items()
                        if v not in (None, [], "")
                    },
                )
            )
        return ToolResult(
            source=self.name,
            tool="repositories",
            records=records,
            total=(payload.get("meta") or {}).get("total"),
            data={"repository_param_mapping": REPOSITORY_CLIENTS},
            request_url=response.url,
        )

