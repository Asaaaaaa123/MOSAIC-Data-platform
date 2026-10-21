"""Open Science Framework (OSF) connector.

Search: SHARE/OSF index ("trove") https://share.osf.io/api/v3/index-card-search (JSON:API),
restricted to items whose accessService is https://osf.io/.
Details: OSF API v2 https://api.osf.io/v2/ (docs https://developer.osf.io/).
Auth: none for public projects / registrations. OSF documents ~100 req/h anonymous bursts are
fine but asks clients to be gentle; we use 2 req/s.

Quirks (verified live, 2026-10):
- index-card-search params: ``cardSearchText``, ``cardSearchFilter[resourceType]`` (Project,
  ProjectComponent, Registration, RegistrationComponent, Preprint, File; comma = any-of),
  ``cardSearchFilter[accessService]=https://osf.io/``, ``page[size]``, ``page[cursor]``.
  Results: ``included`` holds ``search-result`` items pointing at ``index-card`` items whose
  ``resourceMetadata`` is OSFMAP JSON-LD (values wrapped as ``[{"@value": ...}]``).
  ``data.attributes.totalResultCount`` is an int (or a "trove:ten-thousands-and-more" marker).
- Registrations are not served by ``/v2/nodes/{guid}``: get_node falls back to
  ``/v2/registrations/{guid}`` on 404 (registrations carry ``article_doi`` and has_data flags).
- DOIs are minted under 10.17605/OSF.IO/<GUID>; the identifiers relationship lists them.

Identifiers: ``osf`` (GUID, lowercase) and ``doi``. Linked article DOI (registrations'
``article_doi``) and parent project go to attributes.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import Field

from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, strip_html, tool

SHARE = "https://share.osf.io/api/v3/index-card-search"
API = "https://api.osf.io/v2"

_GUID_RE = re.compile(r"^[a-z0-9]{5,6}$")
_FOLDER_RE = re.compile(r"^[a-f0-9]{24}$")
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)

RESOURCE_TYPES: dict[str, str] = {
    "projects": "Project,ProjectComponent",
    "registrations": "Registration,RegistrationComponent",
    "projects_and_registrations": "Project,ProjectComponent,Registration,RegistrationComponent",
    "preprints": "Preprint",
    "files": "File",
}
_RTYPE = {
    "Project": "dataset",
    "ProjectComponent": "dataset",
    "Registration": "study",
    "RegistrationComponent": "study",
    "Preprint": "preprint",
}


class OsfSearchParams(SearchParams):
    resource_type: Literal["projects", "registrations", "projects_and_registrations", "preprints", "files", "all"] = Field(
        "projects_and_registrations",
        description="OSF item kinds to return (default projects + registrations); 'all' adds preprints and files",
    )


class GetNodeParams(ToolParams):
    guid: str = Field(description="OSF GUID (5-6 chars, e.g. gws8n), an osf.io URL, or the 10.17605/OSF.IO/<GUID> DOI")
    files_limit: int = Field(25, ge=0, le=100, description="Top-level OSF Storage entries to list (0 = none)")


class ListFilesParams(ToolParams):
    guid: str = Field(description="OSF project/component/registration GUID")
    folder_id: str | None = Field(
        None, description="OSF Storage folder id (24 hex chars, from a previous file listing); omit for the root"
    )
    limit: int = Field(50, ge=1, le=100, description="Maximum number of entries to return")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous result)")


def guid_from(value: str) -> str | None:
    text = value.strip().rstrip("/")
    match = re.search(r"osf\.io/([a-z0-9]{5,6})(?:/|$)", text, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    match = re.match(r"^(?:https?://(?:dx\.)?doi\.org/)?10\.17605/osf\.io/([a-z0-9]{5,6})$", text, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    if _GUID_RE.match(text.lower()):
        return text.lower()
    return None


def _vals(meta: dict[str, Any], key: str) -> list[Any]:
    out = []
    for item in meta.get(key) or []:
        if isinstance(item, dict):
            if "@value" in item:
                out.append(item["@value"])
            elif "@id" in item:
                out.append(item["@id"])
        elif item is not None:
            out.append(item)
    return out


def _names(meta: dict[str, Any], key: str) -> list[str]:
    names = []
    for item in meta.get(key) or []:
        if isinstance(item, dict):
            name = next(iter(_vals(item, "name")), None)
            if name:
                names.append(str(name))
    return names


def _card_record(card: dict[str, Any]) -> Record | None:
    attrs = card.get("attributes") or {}
    meta = attrs.get("resourceMetadata") or {}
    ids = [str(x) for x in (attrs.get("resourceIdentifier") or []) + _vals(meta, "identifier")]
    guid = None
    for value in [meta.get("@id") or ""] + ids:
        guid = guid_from(value) if "osf.io/" in value and "doi.org" not in value else None
        if guid:
            break
    doi = next((normalize_doi(m.group(0)) for v in ids if (m := _DOI_RE.search(v))), None)
    if not guid and not doi:
        return None
    rtypes = [str(x) for x in _vals(meta, "resourceType")]
    rtype = next((_RTYPE[t] for t in rtypes if t in _RTYPE), "other")
    usage = (meta.get("usage") or [{}])[0] if meta.get("usage") else {}
    parent = next(iter(meta.get("isPartOf") or []), None)
    rights = next(iter(_names(meta, "rights")), None) or next(iter(_vals(meta, "rights")), None)
    storage = next(iter(_vals(meta, "storageByteCount")), None)
    attributes = {
        "resource_type": rtypes[0] if rtypes else None,
        "keywords": [str(k) for k in _vals(meta, "keyword")][:20] or None,
        "subjects": [s for s in _names(meta, "subject")][:10] or None,
        "license": rights,
        "date_modified": next(iter(_vals(meta, "dateModified")), None),
        "parent": guid_from(parent.get("@id", "")) if isinstance(parent, dict) else None,
        "institutions": _names(meta, "affiliation")[:5] or None,
        "funders": _names(meta, "funder")[:10] or None,
        "storage_bytes": int(storage) if str(storage or "").isdigit() else None,
        "views": int(v) if (v := next(iter(_vals(usage, "viewCount")), None)) and str(v).isdigit() else None,
        "downloads": int(v) if (v := next(iter(_vals(usage, "downloadCount")), None)) and str(v).isdigit() else None,
    }
    date = next(iter(_vals(meta, "dateCreated")), None) or next(iter(_vals(meta, "dateCopyrighted")), None)
    description = next(iter(_vals(meta, "description")), None)
    return Record(
        source="osf",
        id=guid or doi or "",
        type=rtype,  # type: ignore[arg-type]
        title=strip_html(str(next(iter(_vals(meta, "title")), "") or "")) or None,
        url=f"https://osf.io/{guid}/" if guid else meta.get("@id"),
        identifiers={"osf": guid, "doi": doi},
        date=str(date)[:10] if date else None,
        authors=_names(meta, "creator"),
        summary=strip_html(str(description)) if description else None,
        attributes={k: v for k, v in attributes.items() if v not in (None, [], "", {})},
    )


def _file_entry(item: dict[str, Any]) -> dict[str, Any]:
    attrs = item.get("attributes") or {}
    links = item.get("links") or {}
    hashes = (attrs.get("extra") or {}).get("hashes") or {}
    entry = {
        "name": attrs.get("name"),
        "kind": attrs.get("kind"),
        "path": attrs.get("materialized_path"),
        "size": attrs.get("size"),
        "md5": hashes.get("md5"),
        "modified": attrs.get("date_modified"),
        "link": links.get("download"),
    }
    if attrs.get("kind") == "folder":
        folder = (attrs.get("path") or "").strip("/")
        entry["folder_id"] = folder if _FOLDER_RE.match(folder) else None
    return {k: v for k, v in entry.items() if v not in (None, "")}


class Osf(Connector):
    name = "osf"
    title = "Open Science Framework"
    category = "datasets"
    description = (
        "Open Science Framework (Center for Open Science): public research projects, components with data, "
        "code and materials, and preregistrations; indexed via SHARE. NIH generalist repository; best for "
        "psychology/behavioral/meta-research data, preregistered study protocols and replication materials."
    )
    homepage = "https://osf.io/"
    docs_url = "https://developer.osf.io/"
    auth = "none"
    rate_limit = 2.0
    health_query = "replication"
    nih_repositories = ("Open Science Framework",)

    @tool(
        description="""
        Search public OSF projects, components and preregistrations (default) via the SHARE/OSF
        index. Returns OSF GUID, DOI (10.17605/OSF.IO/...), title, contributors, created date,
        description, tags, license, parent project and view/download counts. Use for psychology,
        behavioral, neuroscience and meta-research data and preregistered protocols. Plain
        free-text query (words / "phrases"). resource_type: projects | registrations |
        projects_and_registrations (default) | preprints | files | all. Then call get_node for
        contributors' ORCIDs, linked article DOI and the file list.
        """,
        federated=True,
    )
    async def search(self, params: OsfSearchParams) -> ToolResult:
        query: dict[str, Any] = {
            "cardSearchText": params.query,
            "cardSearchFilter[accessService]": "https://osf.io/",
            "page[size]": params.limit,
            "page[cursor]": params.cursor,
        }
        if params.resource_type != "all":
            query["cardSearchFilter[resourceType]"] = RESOURCE_TYPES[params.resource_type]
        response = await self.request(
            "GET", SHARE, params=query, headers={"Accept": "application/vnd.api+json"}
        )
        payload = response.json() or {}
        data = payload.get("data") or {}
        included = payload.get("included") or []
        cards = {i.get("id"): i for i in included if i.get("type") == "index-card"}
        results = [i for i in included if i.get("type") == "search-result"]
        order = [r.get("id") for r in ((data.get("relationships") or {}).get("searchResultPage") or {}).get("data") or []]
        by_id = {r.get("id"): r for r in results}
        records: list[Record] = []
        seen: set[str] = set()
        for rid in order or list(by_id):
            result = by_id.get(rid) or {}
            card_ref = ((result.get("relationships") or {}).get("indexCard") or {}).get("data") or {}
            card = cards.get(card_ref.get("id"))
            if not card:
                continue
            record = _card_record(card)
            if record and record.id not in seen:
                seen.add(record.id)
                records.append(record)
        if not order and not results:  # defensive: older payloads list cards only
            for card in cards.values():
                record = _card_record(card)
                if record and record.id not in seen:
                    seen.add(record.id)
                    records.append(record)
        total = (data.get("attributes") or {}).get("totalResultCount")
        next_href = (
            (((data.get("relationships") or {}).get("searchResultPage") or {}).get("links") or {}).get("next") or {}
        ).get("href")
        next_cursor = None
        if next_href and records:
            next_cursor = (parse_qs(urlsplit(next_href).query).get("page[cursor]") or [None])[0]
        return ToolResult(
            source=self.name,
            tool="search",
            records=records,
            total=total if isinstance(total, int) else None,
            next_cursor=next_cursor,
            request_url=response.url,
            notes=[] if isinstance(total, int) or total is None else ["more than 10,000 matches"],
        )

    async def _resource(self, guid: str) -> tuple[str, Any] | None:
        for kind in ("nodes", "registrations"):
            response = await self.request(
                "GET",
                f"{API}/{kind}/{guid}/",
                params=[("embed", "contributors"), ("embed", "license"), ("embed", "identifiers"), ("embed", "affiliated_institutions")],
                allow_status=(404, 410, 401, 403),
            )
            if response.status == 200:
                return kind, response
            if response.status in (401, 403, 410):
                return kind, response
        return None

    @tool(
        description="""
        Fetch one public OSF project, component or registration by GUID (e.g. gws8n), osf.io URL or
        10.17605/OSF.IO/<GUID> DOI: title, description, category, tags, license, dates, contributors
        (with ORCIDs), DOI, affiliated institutions, parent/root project, for registrations the
        linked article DOI and has_data / has_materials flags, and the top-level OSF Storage file
        list (use list_files to open folders).
        """
    )
    async def get_node(self, params: GetNodeParams) -> ToolResult:
        guid = guid_from(params.guid)
        if not guid:
            return ToolResult(source=self.name, tool="get_node", notes=[f"not an OSF GUID: {params.guid!r}"])
        found = await self._resource(guid)
        if not found:
            return ToolResult(
                source=self.name, tool="get_node", notes=[f"OSF node/registration {guid} not found (preprints and files are not nodes)"]
            )
        kind, response = found
        if response.status != 200:
            return ToolResult(
                source=self.name, tool="get_node", request_url=response.url,
                notes=[f"OSF {kind[:-1]} {guid} is private, withdrawn or deleted (HTTP {response.status})"],
            )
        data = (response.json() or {}).get("data") or {}
        record = self._node_record(data, kind)
        notes: list[str] = []
        if params.files_limit:
            fresp = await self.request(
                "GET", f"{API}/{kind}/{guid}/files/osfstorage/", params={"page[size]": params.files_limit},
                allow_status=(401, 403, 404),
            )
            if fresp.status == 200:
                fpayload = fresp.json() or {}
                files = [_file_entry(f) for f in fpayload.get("data") or []]
                total = ((fpayload.get("links") or {}).get("meta") or {}).get("total")
                record.attributes["files"] = files or None
                record.attributes["top_level_entries"] = total if total is not None else len(files)
                if total and total > len(files):
                    notes.append(f"{total} top-level entries; listed {len(files)} (use list_files to page)")
        record.attributes = {k: v for k, v in record.attributes.items() if v not in (None, [], "", {})}
        return ToolResult(source=self.name, tool="get_node", records=[record], total=1, request_url=response.url, notes=notes)

    def _node_record(self, data: dict[str, Any], kind: str) -> Record:
        attrs = data.get("attributes") or {}
        embeds = data.get("embeds") or {}
        guid = data.get("id") or ""
        authors, orcids = [], []
        for contrib in ((embeds.get("contributors") or {}).get("data") or []):
            cattrs = contrib.get("attributes") or {}
            if cattrs.get("bibliographic") is False:
                continue
            user = (((contrib.get("embeds") or {}).get("users") or {}).get("data") or {}).get("attributes") or {}
            name = user.get("full_name") or cattrs.get("unregistered_contributor")
            if name:
                authors.append(name)
            orcid = (user.get("social") or {}).get("orcid")
            if orcid:
                orcids.append(orcid)
        doi = None
        for ident in ((embeds.get("identifiers") or {}).get("data") or []):
            iattrs = ident.get("attributes") or {}
            if iattrs.get("category") == "doi" and iattrs.get("value"):
                doi = normalize_doi(iattrs["value"])
        license_data = ((embeds.get("license") or {}).get("data") or {}).get("attributes") or {}
        related = []
        if attrs.get("article_doi"):
            related.append({"relation": "IsSupplementTo", "scheme": "doi", "id": normalize_doi(attrs["article_doi"])})
        rel = data.get("relationships") or {}
        parent = ((rel.get("parent") or {}).get("data") or {}).get("id") or ""
        if not parent:
            href = (((rel.get("parent") or {}).get("links") or {}).get("related") or {}).get("href") or ""
            match = re.search(r"/(?:nodes|registrations)/([a-z0-9]{5,6})/", href)
            parent = match.group(1) if match else ""
        root = ((((rel.get("root") or {}).get("data") or {}).get("id"))) or None
        institutions = [
            (i.get("attributes") or {}).get("name")
            for i in ((embeds.get("affiliated_institutions") or {}).get("data") or [])
            if (i.get("attributes") or {}).get("name")
        ]
        is_registration = kind == "registrations"
        attributes = {
            "kind": "registration" if is_registration else "node",
            "category": attrs.get("category"),
            "tags": (attrs.get("tags") or [])[:20] or None,
            "subjects": [s.get("text") for group in attrs.get("subjects") or [] for s in (group if isinstance(group, list) else [group]) if isinstance(s, dict) and s.get("text")][:10] or None,
            "license": license_data.get("name"),
            "license_year": (attrs.get("node_license") or {}).get("year"),
            "date_modified": attrs.get("date_modified"),
            "date_registered": attrs.get("date_registered"),
            "public": attrs.get("public"),
            "parent": parent or None,
            "root": root if root and root != guid else None,
            "institutions": institutions[:5] or None,
            "author_orcids": orcids[:10] or None,
            "has_data": attrs.get("has_data") if is_registration else None,
            "has_analytic_code": attrs.get("has_analytic_code") if is_registration else None,
            "has_materials": attrs.get("has_materials") if is_registration else None,
            "embargoed": attrs.get("embargoed") or None,
            "withdrawn": attrs.get("withdrawn") or None,
            "related": related or None,
        }
        return Record(
            source=self.name,
            id=guid,
            type="study" if is_registration else "dataset",
            title=strip_html(attrs.get("title")),
            url=f"https://osf.io/{guid}/",
            identifiers={"osf": guid, "doi": doi},
            date=((attrs.get("date_registered") if is_registration else None) or attrs.get("date_created") or "")[:10] or None,
            authors=authors,
            summary=strip_html(attrs.get("description")),
            attributes=attributes,
        )

    @tool(
        description="""
        List files and folders in an OSF project/component/registration's OSF Storage: name, kind,
        path, size, md5, modified date and download link; folders carry folder_id to drill down.
        Use after get_node when you need a specific data file.
        """
    )
    async def list_files(self, params: ListFilesParams) -> ToolResult:
        guid = guid_from(params.guid)
        if not guid:
            return ToolResult(source=self.name, tool="list_files", notes=[f"not an OSF GUID: {params.guid!r}"])
        if params.folder_id and not _FOLDER_RE.match(params.folder_id):
            return ToolResult(source=self.name, tool="list_files", notes=["folder_id must be 24 hex characters"])
        suffix = f"{params.folder_id}/" if params.folder_id else ""
        page = max(1, int(params.cursor)) if (params.cursor or "").isdigit() else 1
        response = None
        for kind in ("nodes", "registrations"):
            response = await self.request(
                "GET",
                f"{API}/{kind}/{guid}/files/osfstorage/{suffix}",
                params={"page[size]": params.limit, "page": page},
                allow_status=(401, 403, 404, 410),
            )
            if response.status != 404:
                break
        assert response is not None
        if response.status != 200:
            return ToolResult(
                source=self.name, tool="list_files", request_url=response.url,
                notes=[f"files of {guid} unavailable (HTTP {response.status}: not found or private)"],
            )
        payload = response.json() or {}
        entries = [_file_entry(f) for f in payload.get("data") or []]
        total = ((payload.get("links") or {}).get("meta") or {}).get("total")
        has_next = bool((payload.get("links") or {}).get("next"))
        return ToolResult(
            source=self.name,
            tool="list_files",
            data=entries,
            total=total,
            next_cursor=str(page + 1) if has_next else None,
            request_url=response.url,
        )
