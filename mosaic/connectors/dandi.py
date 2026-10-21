"""DANDI Archive connector (BRAIN Initiative neurophysiology data archive).

Docs: https://api.dandiarchive.org/swagger/ (Swagger 2.0 spec at /api/docs/swagger/?format=openapi)
Base: https://api.dandiarchive.org/api
Auth: none for public (non-embargoed) dandisets.
Rate limits: none documented; we stay at 5 req/s.

Quirks
- ``/dandisets/?search=`` is free text across dandiset metadata plus Gmail-style operators
  (``species:mouse``, ``approach:"behavioral approach"``, ``technique:"spike sorting"``,
  ``standard:nwb``, ``variable:LFP``, ``published_after:2023-01-01``...). Unknown operators
  return HTTP 400 with an explanation (surfaced as SourceError).
- Listing results carry only names/sizes; descriptions, contributors, species etc. live in
  the version metadata (``/dandisets/{id}/versions/{version}/``), so search optionally fetches
  it per hit (``details``).
- DOIs are minted per *published version* (10.48324/dandi.000003/0.260218.2052); draft-only
  dandisets have no DOI.
- Pagination is page-based; our cursor is the next page number for the same ``limit``.

Identifier keys: ``dandi`` (6-digit dandiset id, e.g. 000003), ``doi`` (version DOI).
Papers that describe a dandiset (relatedResource) are kept in attributes (related_dois),
never in identifiers, because identifiers assert identity of the dandiset itself.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import quote

from pydantic import Field

from ..http import SourceError
from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, strip_html, tool

API = "https://api.dandiarchive.org/api"
WEB = "https://dandiarchive.org/dandiset"

_ORDERINGS = ("id", "-id", "name", "-name", "modified", "-modified", "size", "-size", "stars", "-stars")


class SearchDandisetsParams(SearchParams):
    ordering: str | None = Field(
        None,
        description="Sort order (default: relevance). One of: " + ", ".join(_ORDERINGS),
    )
    include_drafts: bool = Field(
        True, description="Include dandisets that have never been published (draft-only, no DOI)"
    )
    include_empty: bool = Field(False, description="Include dandisets with no assets")
    details: bool = Field(
        True,
        description="Fetch version metadata per hit (description, species, techniques, contributors, DOI). "
        "Set false for a faster, names-only listing.",
    )


class GetDandisetParams(ToolParams):
    dandiset_id: str = Field(
        description="Dandiset id: '000003', 'DANDI:000003', '3', a dandiarchive.org URL, or a DANDI DOI "
        "(10.48324/dandi.000003/0.230629.1955)"
    )
    version: str | None = Field(
        None,
        description="Version string (e.g. '0.230629.1955' or 'draft'). Default: most recent published version, "
        "else draft.",
    )


class ListAssetsParams(ToolParams):
    dandiset_id: str = Field(description="Dandiset id, e.g. '000003' or 'DANDI:000003'")
    version: str | None = Field(
        None, description="Version (default: most recent published version, else 'draft')"
    )
    glob: str | None = Field(
        None, description="Glob over asset paths, e.g. '*ecephys*' or 'sub-YutaMouse20/*'"
    )
    limit: int = Field(25, ge=1, le=100, description="Maximum number of assets to return")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous result)")


def parse_dandiset_id(value: str) -> tuple[str | None, str | None]:
    """Return (6-digit id, version-or-None) from the many ways people write a dandiset."""
    text = value.strip()
    version = None
    m = re.search(r"dandi\.(\d{6})/(\d+\.\d+\.\d+|draft)", text, re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"dandiset/(\d{1,6})(?:/(\d+\.\d+\.\d+|draft))?", text, re.IGNORECASE)
    if m:
        return m.group(1).zfill(6), m.group(2)
    m = re.fullmatch(r"(?:dandi:)?\s*(\d{1,6})(?:/(\d+\.\d+\.\d+|draft))?", text, re.IGNORECASE)
    if m:
        return m.group(1).zfill(6), m.group(2)
    return None, version


def _date(value: str | None) -> str | None:
    return value[:10] if value else None


def _names(items: Any) -> list[str]:
    out = []
    for item in items or []:
        if isinstance(item, dict):
            name = item.get("name")
        else:
            name = item
        if name and name not in out:
            out.append(str(name))
    return out


def _compact(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v not in (None, [], "", {})}


def _contributors(meta: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """(author names, compact contributor list, funder names)."""
    authors: list[str] = []
    people: list[dict[str, Any]] = []
    funders: list[str] = []
    for c in meta.get("contributor") or []:
        if not isinstance(c, dict):
            continue
        roles = [r.replace("dcite:", "") for r in c.get("roleName") or []]
        name = c.get("name")
        if c.get("schemaKey") == "Organization" and "Funder" in roles:
            label = name or ""
            if c.get("awardNumber"):
                label += f" ({c['awardNumber']})"
            if label:
                funders.append(label)
            continue
        if not name:
            continue
        if "Author" in roles or (c.get("includeInCitation") and c.get("schemaKey") == "Person"):
            authors.append(name)
        entry = {"name": name, "roles": roles or None}
        ident = c.get("identifier")
        if ident and re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", str(ident)):
            entry["orcid"] = ident
        affiliation = [a.get("name") for a in c.get("affiliation") or [] if isinstance(a, dict) and a.get("name")]
        if affiliation:
            entry["affiliation"] = affiliation[0]
        people.append(_compact(entry))
    return authors, people, funders


def _related(meta: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    resources: list[dict[str, Any]] = []
    dois: list[str] = []
    for r in meta.get("relatedResource") or []:
        if not isinstance(r, dict):
            continue
        ident = r.get("identifier") or ""
        url = r.get("url") or ""
        doi = None
        for candidate in (ident, url):
            if candidate and "10." in candidate:
                m = re.search(r"10\.\d{4,9}/\S+", candidate)
                if m:
                    doi = normalize_doi(m.group(0))
                    break
        if doi and doi not in dois:
            dois.append(doi)
        resources.append(
            _compact(
                {
                    "relation": (r.get("relation") or "").replace("dcite:", "") or None,
                    "name": r.get("name"),
                    "doi": doi,
                    "url": url or None,
                    "resource_type": (r.get("resourceType") or "").replace("dcite:", "") or None,
                }
            )
        )
    return resources, dois


def _record(listing: dict[str, Any] | None, meta: dict[str, Any] | None, *, full: bool = False) -> Record:
    listing = listing or {}
    meta = meta or {}
    ident = listing.get("identifier") or str(meta.get("identifier") or "").replace("DANDI:", "")
    published = listing.get("most_recent_published_version") or {}
    draft = listing.get("draft_version") or {}
    ver_info = published or draft
    version = meta.get("version") or ver_info.get("version")
    summary = meta.get("assetsSummary") or {}
    authors, people, funders = _contributors(meta)
    resources, related_dois = _related(meta)
    doi = meta.get("doi")
    access = [a.get("status", "").replace("dandi:", "") for a in meta.get("access") or [] if isinstance(a, dict)]
    url = f"{WEB}/{ident}"
    if version and version != "draft" and meta:
        url = f"{WEB}/{ident}/{version}"
    attrs: dict[str, Any] = {
        "version": version,
        "published": bool(published) or (bool(version) and version != "draft"),
        "embargo_status": listing.get("embargo_status"),
        "access": access[0] if access else None,
        "status": ver_info.get("status"),
        "asset_count": summary.get("numberOfFiles", ver_info.get("asset_count")),
        "size_bytes": summary.get("numberOfBytes", ver_info.get("size")),
        "subjects": summary.get("numberOfSubjects"),
        "species": _names(summary.get("species")),
        "approaches": _names(summary.get("approach")),
        "measurement_techniques": _names(summary.get("measurementTechnique")),
        "variables_measured": _names(summary.get("variableMeasured")),
        "data_standards": _names(summary.get("dataStandard")),
        "anatomy": _names(meta.get("about")),
        "keywords": meta.get("keywords") or None,
        "license": [str(x).replace("spdx:", "") for x in meta.get("license") or []] or None,
        "contact_person": listing.get("contact_person") or None,
        "star_count": listing.get("star_count") or None,
        "funders": funders,
        "related_dois": related_dois,
        "modified": _date((draft or published).get("modified") or listing.get("modified")),
    }
    if full:
        attrs.update(
            {
                "citation": meta.get("citation"),
                "contributors": people[:25],
                "related_resources": resources[:20],
                "ethics_approval": [e.get("identifier") for e in meta.get("ethicsApproval") or [] if e.get("identifier")],
                "manifest": (meta.get("manifestLocation") or [None])[0],
                "api_url": f"{API}/dandisets/{ident}/versions/{version}/" if version else None,
            }
        )
    date = meta.get("datePublished") or published.get("created") or meta.get("dateCreated") or listing.get("created")
    return Record(
        source="dandi",
        id=ident,
        type="dataset",
        title=meta.get("name") or ver_info.get("name"),
        url=url,
        identifiers={"dandi": ident, "doi": doi},
        date=_date(date),
        authors=authors,
        summary=strip_html(meta.get("description")),
        attributes=_compact(attrs),
    )


class Dandi(Connector):
    name = "dandi"
    title = "DANDI Archive"
    category = "datasets"
    description = (
        "BRAIN Initiative archive of ~1,000 cellular neurophysiology dandisets (NWB/BIDS: extracellular "
        "and intracellular electrophysiology, optical/calcium imaging, behavior, microscopy) with species, "
        "techniques, subjects and version DOIs. Best for finding and citing neurophysiology datasets."
    )
    homepage = "https://dandiarchive.org/"
    docs_url = "https://api.dandiarchive.org/swagger/"
    auth = "none"
    rate_limit = 5.0
    health_query = "hippocampus"
    nih_repositories = ("Distributed Archives for Neurophysiology Data Integration (The DANDI Archive)",)

    async def _version_meta(self, ident: str, version: str) -> dict[str, Any] | None:
        response = await self.request(
            "GET", f"{API}/dandisets/{quote(ident)}/versions/{quote(version)}/", allow_status=(404,)
        )
        if response.status == 404:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None

    @tool(
        description="""
        Search DANDI dandisets (neurophysiology datasets: ecephys, icephys, ophys/calcium imaging,
        behavior, microscopy; mostly NWB format) by free text over all dandiset metadata. Returns one
        record per dandiset with title, description, contributors, species, approaches, measurement
        techniques, variables measured, file/byte/subject counts, license and version DOI.
        Use for neuroscience data discovery (brain region, cell type, technique, species); for MRI/EEG/MEG
        BIDS datasets prefer OpenNeuro. Query supports Gmail-style operators combined with free text
        (AND): species:mouse, approach:"electrophysiological approach", technique:"spike sorting",
        standard:nwb, variable:LFP, published_after:2023-01-01, owner:<name>.
        Example: 'hippocampus technique:"spike sorting" species:mouse'.
        """,
        federated=True,
    )
    async def search_dandisets(self, params: SearchDandisetsParams) -> ToolResult:
        page = max(1, parse_offset(params.cursor) or 1)
        if params.ordering and params.ordering not in _ORDERINGS:
            raise SourceError(self.name, f"invalid ordering {params.ordering!r}; use one of {', '.join(_ORDERINGS)}")
        query = {
            "search": params.query,
            "page": page,
            "page_size": params.limit,
            "ordering": params.ordering,
            "draft": params.include_drafts,
            "empty": params.include_empty,
            "embargoed": False,
        }
        response = await self.request("GET", f"{API}/dandisets/", params=query)
        payload = response.json() or {}
        items = [i for i in payload.get("results") or [] if isinstance(i, dict)]
        metas: list[dict[str, Any] | None] = [None] * len(items)
        notes: list[str] = []
        if params.details and items:

            async def fetch(item: dict[str, Any]) -> dict[str, Any] | None:
                ver = (item.get("most_recent_published_version") or {}).get("version") or "draft"
                try:
                    return await self._version_meta(item.get("identifier", ""), ver)
                except SourceError:
                    return None

            metas = list(await asyncio.gather(*(fetch(i) for i in items)))
            missing = sum(1 for m in metas if m is None)
            if missing:
                notes.append(f"version metadata unavailable for {missing} dandiset(s); records show listing fields only")
        total = payload.get("count")
        return ToolResult(
            source=self.name,
            tool="search_dandisets",
            records=[_record(i, m) for i, m in zip(items, metas)],
            total=total,
            next_cursor=str(page + 1) if payload.get("next") and items else None,
            request_url=response.url,
            notes=notes,
        )

    @tool(
        description="""
        Get full metadata for one DANDI dandiset: name, description, contributors (with ORCID and
        roles), funders/award numbers, license, keywords, species, approaches, measurement techniques,
        variables measured, anatomy, number of files/bytes/subjects, version DOI, citation string and
        related resources (describing papers with DOIs). Defaults to the most recent published version
        (else the draft). Accepts '000003', 'DANDI:000003', dandiarchive.org URLs or DANDI DOIs.
        """
    )
    async def get_dandiset(self, params: GetDandisetParams) -> ToolResult:
        ident, version = parse_dandiset_id(params.dandiset_id)
        if not ident:
            return ToolResult(
                source=self.name,
                tool="get_dandiset",
                notes=[f"could not parse a dandiset id from {params.dandiset_id!r}; expected e.g. '000003'"],
            )
        version = params.version or version
        response = await self.request("GET", f"{API}/dandisets/{ident}/", allow_status=(404,))
        if response.status == 404:
            return ToolResult(
                source=self.name, tool="get_dandiset", notes=[f"dandiset {ident} not found (or embargoed)"],
                request_url=response.url,
            )
        listing = response.json() or {}
        if not version:
            version = (listing.get("most_recent_published_version") or {}).get("version") or "draft"
        meta = await self._version_meta(ident, version)
        notes = []
        if meta is None:
            notes.append(f"version {version} of dandiset {ident} not found; showing listing fields only")
        if version == "draft":
            notes.append("draft version: metadata may change and there is no DOI until it is published")
        record = _record(listing, meta, full=True)
        return ToolResult(
            source=self.name,
            tool="get_dandiset",
            records=[record],
            total=1,
            request_url=f"{API}/dandisets/{ident}/versions/{version}/",
            notes=notes,
        )

    @tool(
        description="""
        List the files (assets) of a DANDI dandiset version: path (BIDS/NWB-style
        sub-<subject>/sub-<subject>_ses-<session>_<modality>.nwb), size and download URL. Use to see
        which subjects/sessions/modalities a dandiset contains or to pick files for download; filter
        with a glob such as '*ecephys*' or 'sub-01/*'. Default version: most recent published, else draft.
        """
    )
    async def list_assets(self, params: ListAssetsParams) -> ToolResult:
        ident, version = parse_dandiset_id(params.dandiset_id)
        if not ident:
            return ToolResult(
                source=self.name, tool="list_assets", notes=[f"could not parse a dandiset id from {params.dandiset_id!r}"]
            )
        version = params.version or version
        if not version:
            response = await self.request("GET", f"{API}/dandisets/{ident}/", allow_status=(404,))
            if response.status == 404:
                return ToolResult(source=self.name, tool="list_assets", notes=[f"dandiset {ident} not found"])
            listing = response.json() or {}
            version = (listing.get("most_recent_published_version") or {}).get("version") or "draft"
        page = max(1, parse_offset(params.cursor) or 1)
        response = await self.request(
            "GET",
            f"{API}/dandisets/{ident}/versions/{quote(version)}/assets/",
            params={"page": page, "page_size": params.limit, "glob": params.glob, "order": "path"},
            allow_status=(404,),
        )
        if response.status == 404:
            return ToolResult(
                source=self.name, tool="list_assets", notes=[f"dandiset {ident} version {version} not found"],
                request_url=response.url,
            )
        payload = response.json() or {}
        records = []
        for a in payload.get("results") or []:
            asset_id = a.get("asset_id")
            if not asset_id:
                continue
            path = a.get("path") or ""
            subject = re.match(r"(sub-[^/]+)/", path)
            records.append(
                Record(
                    source=self.name,
                    id=asset_id,
                    type="other",
                    title=path,
                    url=f"{API}/assets/{asset_id}/download/",
                    date=_date(a.get("modified") or a.get("created")),
                    attributes=_compact(
                        {
                            "dandiset": ident,
                            "version": version,
                            "path": path,
                            "size_bytes": a.get("size"),
                            "subject": subject.group(1) if subject else None,
                            "zarr": a.get("zarr"),
                        }
                    ),
                )
            )
        return ToolResult(
            source=self.name,
            tool="list_assets",
            records=records,
            total=payload.get("count"),
            next_cursor=str(page + 1) if payload.get("next") and records else None,
            request_url=response.url,
        )
