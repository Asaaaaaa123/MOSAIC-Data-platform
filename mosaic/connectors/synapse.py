"""Synapse connector (Sage Bionetworks data platform behind AD Knowledge Portal, NF, PsychENCODE ...).

Docs: https://rest-docs.synapse.org/ (base https://repo-prod.prod.sagebase.org/repo/v1)
Auth: anonymous access works for search and public entity metadata (verified live 2026-10:
``POST /search`` -> 201, ``POST /entity/{id}/bundle2`` -> 200). Optional ``SYNAPSE_AUTH_TOKEN``
(personal access token) is sent as ``Authorization: Bearer`` and exposes entities the user can see.
Downloading files always needs an account; many datasets are controlled-access.
Rate limit: Synapse throttles per user/IP (no public number); we use 3 req/s.

Quirks (verified live):
- ``POST /search`` body ``{"queryTerm": [...], "start", "size" (<=100), "returnFields": [...],
  "booleanQuery": [{"key": "node_type", "value": "dataset"}]}``. Terms are OR-ed (more terms ->
  more hits, ranked by relevance); prefer one or two specific terms. ``description`` in hits is
  the wiki markdown; ``created_on`` is epoch seconds; ``path`` gives the project.
- ``POST /entity/{id}/bundle2`` returns entity + annotations + restrictionInformation
  (restrictionLevel OPEN / RESTRICTED_BY_TERMS_OF_USE / CONTROLLED_BY_ACT) + DOI association in one
  call. Most entity ``description`` fields are empty; the root wiki holds the description.
- DOIs are 10.7303/<synId> (DataCite client sagebio.synapse).

Identifiers: ``synapse`` (syn id) and ``doi`` when one is associated.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field

from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, strip_html, tool

API = "https://repo-prod.prod.sagebase.org/repo/v1"
WEB = "https://www.synapse.org/Synapse:"

_SYN_RE = re.compile(r"\b(syn\d{3,12})\b", re.IGNORECASE)
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>)\]]+", re.IGNORECASE)
_PMID_KEYS = {"pmid", "pubmed", "pubmedid", "pubmed_id"}

NodeType = Literal["project", "folder", "file", "table", "dataset", "entityview", "any"]
_CHILD_TYPES = ["folder", "file", "table", "dataset", "entityview", "link", "dockerrepo", "submissionview", "materializedview", "virtualtable", "datasetcollection"]


class SynapseSearchParams(SearchParams):
    node_type: NodeType = Field("any", description="Restrict to one entity type (dataset, project, folder, file, table ...)")


class GetEntityParams(ToolParams):
    syn_id: str = Field(description="Synapse id, e.g. syn3388564 (a synapse.org URL or 10.7303/syn... DOI also works)")
    include_wiki: bool = Field(True, description="Fetch the root wiki text as the description when the entity has none")


class ListChildrenParams(ToolParams):
    syn_id: str = Field(description="Parent project/folder Synapse id, e.g. syn2580853")
    types: list[Literal["folder", "file", "table", "dataset", "entityview", "link"]] | None = Field(
        None, description="Child entity types to include (default: all common types)"
    )
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous result)")


def _syn(value: str) -> str | None:
    match = _SYN_RE.search(value or "")
    return match.group(1).lower() if match else None


def _epoch_date(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


def _rtype(concrete_or_node: str | None) -> str:
    name = (concrete_or_node or "").split(".")[-1].lower()
    if name in ("dataset", "datasetcollection", "fileentity", "file", "tableentity", "table", "folder", "project", "entityview", "materializedview", "virtualtable"):
        return "dataset"
    return "other"


def _markdown_text(md: str | None, limit: int = 4000) -> str | None:
    if not md:
        return None
    text = re.sub(r"\$\{[^}]*\}", " ", md)  # Synapse widgets
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)  # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # links -> label
    text = re.sub(r"[#*_`>|]+", " ", text)
    text = strip_html(text) or ""
    return (text[:limit].rstrip() + " ...") if len(text) > limit else (text or None)


def _hit_record(hit: dict[str, Any]) -> Record:
    sid = (hit.get("id") or "").lower()
    path = ((hit.get("path") or {}).get("path")) or []
    project = next((p.get("name") for p in path if str(p.get("type", "")).endswith(".Project")), None)
    node_type = hit.get("node_type")
    attributes = {
        "entity_type": node_type,
        "project": project if project != hit.get("name") else None,
        "project_id": next((p.get("id") for p in path if str(p.get("type", "")).endswith(".Project")), None),
        "modified": _epoch_date(hit.get("modified_on")),
    }
    return Record(
        source="synapse",
        id=sid,
        type=_rtype(node_type),  # type: ignore[arg-type]
        title=hit.get("name"),
        url=f"{WEB}{sid}",
        identifiers={"synapse": sid},
        date=_epoch_date(hit.get("created_on")),
        summary=_markdown_text(hit.get("description"), 1500),
        attributes={k: v for k, v in attributes.items() if v not in (None, [], "", {})},
    )


def _annotations(raw: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    compact: dict[str, Any] = {}
    related: list[dict[str, str]] = []
    for key, entry in list((raw or {}).items()):
        values = entry.get("value") if isinstance(entry, dict) else entry
        if not isinstance(values, list):
            values = [values]
        values = [v for v in values if v not in (None, "")]
        if not values:
            continue
        lkey = key.lower()
        for v in values:
            text = str(v)
            if lkey in _PMID_KEYS and re.sub(r"\D", "", text):
                related.append({"relation": "annotation:" + key, "scheme": "pmid", "id": re.sub(r"\D", "", text)})
            elif "doi" in lkey or "publication" in lkey or "citation" in lkey:
                for match in _DOI_RE.findall(text):
                    related.append({"relation": "annotation:" + key, "scheme": "doi", "id": normalize_doi(match).rstrip(".")})
        if len(compact) < 30:
            shown = [str(v)[:120] for v in values[:8]]
            compact[key] = shown[0] if len(shown) == 1 else shown
    return compact, related


class Synapse(Connector):
    name = "synapse"
    title = "Synapse (Sage Bionetworks)"
    category = "datasets"
    description = (
        "Sage Bionetworks' Synapse: collaborative platform hosting large biomedical consortium data (AD "
        "Knowledge Portal/AMP-AD, ROSMAP, PsychENCODE, NF, cancer and digital-health studies) with rich "
        "annotations and controlled-access governance. Best for neurodegeneration and multi-omic consortium data."
    )
    homepage = "https://www.synapse.org/"
    docs_url = "https://rest-docs.synapse.org/"
    auth = "optional_api_key"
    rate_limit = 3.0
    health_query = "alzheimer"
    nih_repositories = ("Synapse",)

    def _headers(self) -> dict[str, str]:
        token = self.setting("SYNAPSE_AUTH_TOKEN")
        return {"Authorization": f"Bearer {token}"} if token else {}

    @tool(
        description="""
        Search Synapse entities (projects, folders, datasets, tables, files) by keywords. Returns
        syn id, name, entity type, owning project, created/modified dates and the wiki text as
        summary. Use for consortium omics/clinical data (AD Knowledge Portal, AMP-AD, ROSMAP,
        PsychENCODE, NF, CommonMind ...). Query terms are OR-ed and relevance-ranked, so use one or
        two specific words (e.g. "ROSMAP proteomics"). Filter node_type=dataset|project|folder|file|
        table. Then call get_entity for annotations, DOI and access restrictions.
        """,
        federated=True,
    )
    async def search(self, params: SynapseSearchParams) -> ToolResult:
        start = parse_offset(params.cursor)
        terms = [t for t in re.split(r"\s+", params.query.strip()) if t] or [params.query]
        body: dict[str, Any] = {
            "queryTerm": terms,
            "start": start,
            "size": params.limit,
            "returnFields": ["name", "description", "node_type", "created_on", "modified_on", "path"],
        }
        if params.node_type != "any":
            body["booleanQuery"] = [{"key": "node_type", "value": params.node_type}]
        response = await self.request(
            "POST", f"{API}/search", json_body=body, headers=self._headers(), cache_ttl=3600
        )
        payload = response.json() or {}
        hits = payload.get("hits") or []
        total = payload.get("found")
        nxt = start + len(hits)
        notes = ["Synapse ORs query terms; results are relevance-ranked"] if len(terms) > 1 else []
        return ToolResult(
            source=self.name,
            tool="search",
            records=[_hit_record(h) for h in hits],
            total=total,
            next_cursor=str(nxt) if hits and total is not None and nxt < total else None,
            request_url=response.url,
            notes=notes,
        )

    @tool(
        description="""
        Fetch one Synapse entity by syn id: name, entity type, parent, dates, version, DOI
        (10.7303/...), compact annotations (study, assay, species, tissue, dataType ...), access
        restriction level (OPEN / RESTRICTED_BY_TERMS_OF_USE / CONTROLLED_BY_ACT), the list of access
        requirements, whether it has children, and the wiki text as description. Use to judge
        whether data are downloadable and under which terms.
        """
    )
    async def get_entity(self, params: GetEntityParams) -> ToolResult:
        sid = _syn(params.syn_id)
        if not sid:
            return ToolResult(source=self.name, tool="get_entity", notes=[f"not a Synapse id: {params.syn_id!r}"])
        response = await self.request(
            "POST",
            f"{API}/entity/{sid}/bundle2",
            json_body={
                "includeEntity": True,
                "includeAnnotations": True,
                "includeHasChildren": True,
                "includeRestrictionInformation": True,
                "includeDOIAssociation": True,
            },
            headers=self._headers(),
            cache_ttl=3600,
            allow_status=(403, 404),
        )
        if response.status in (403, 404):
            return ToolResult(
                source=self.name,
                tool="get_entity",
                request_url=response.url,
                notes=[
                    f"Synapse entity {sid} "
                    + ("not found" if response.status == 404 else "is not visible anonymously (set SYNAPSE_AUTH_TOKEN)")
                ],
            )
        bundle = response.json() or {}
        entity = bundle.get("entity") or {}
        restriction = bundle.get("restrictionInformation") or {}
        doi_assoc = bundle.get("doiAssociation") or {}
        annotations, related = _annotations((bundle.get("annotations") or {}).get("annotations") or {})
        notes: list[str] = []

        requirements = []
        ar = await self.request(
            "GET", f"{API}/entity/{sid}/accessRequirement", headers=self._headers(), allow_status=(403, 404)
        )
        if ar.status == 200:
            for req in (ar.json() or {}).get("results") or []:
                kind = str(req.get("concreteType") or "").split(".")[-1]
                requirements.append(
                    {
                        "id": req.get("id"),
                        "type": kind,
                        "access_type": req.get("accessType"),
                        "terms": (strip_html(req.get("termsOfUse")) or "")[:300] or None,
                    }
                )
        summary = strip_html(entity.get("description")) or None
        if not summary and params.include_wiki:
            wiki = await self.request("GET", f"{API}/entity/{sid}/wiki", headers=self._headers(), allow_status=(403, 404))
            if wiki.status == 200:
                summary = _markdown_text((wiki.json() or {}).get("markdown"))
        level = restriction.get("restrictionLevel")
        attributes = {
            "entity_type": bundle.get("entityType") or str(entity.get("concreteType") or "").split(".")[-1],
            "parent_id": entity.get("parentId"),
            "modified": (entity.get("modifiedOn") or "")[:10] or None,
            "version": entity.get("versionNumber"),
            "has_children": bundle.get("hasChildren"),
            "item_count": len(entity.get("items") or []) or None,
            "restriction_level": level,
            "controlled_access": level == "CONTROLLED_BY_ACT" if level else None,
            "has_unmet_access_requirement": restriction.get("hasUnmetAccessRequirement"),
            "access_requirements": [{k: v for k, v in r.items() if v is not None} for r in requirements] or None,
            "annotations": annotations or None,
            "related": related[:20] or None,
        }
        if level and level != "OPEN":
            notes.append(f"access restricted ({level}); downloads require a Synapse account and accepting the requirements")
        doi = normalize_doi(doi_assoc["doiUri"]) if doi_assoc.get("doiUri") else None
        record = Record(
            source=self.name,
            id=sid,
            type=_rtype(entity.get("concreteType")),  # type: ignore[arg-type]
            title=entity.get("name"),
            url=f"{WEB}{sid}",
            identifiers={"synapse": sid, "doi": doi},
            date=(entity.get("createdOn") or "")[:10] or None,
            summary=summary,
            attributes={k: v for k, v in attributes.items() if v not in (None, [], "", {})},
        )
        return ToolResult(source=self.name, tool="get_entity", records=[record], total=1, request_url=response.url, notes=notes)

    @tool(
        description="""
        List the children (folders, files, tables, datasets, views) of a Synapse project or folder,
        e.g. syn2580853 (AD Knowledge Portal backend). Returns syn id, name, type and dates for each
        child; page with next_cursor. Use to browse a study's structure before get_entity.
        """
    )
    async def list_children(self, params: ListChildrenParams) -> ToolResult:
        sid = _syn(params.syn_id)
        if not sid:
            return ToolResult(source=self.name, tool="list_children", notes=[f"not a Synapse id: {params.syn_id!r}"])
        body: dict[str, Any] = {
            "parentId": sid,
            "includeTypes": list(params.types) if params.types else _CHILD_TYPES,
            "includeTotalChildCount": True,
        }
        if params.cursor:
            body["nextPageToken"] = params.cursor
        response = await self.request(
            "POST", f"{API}/entity/children", json_body=body, headers=self._headers(), cache_ttl=3600,
            allow_status=(403, 404),
        )
        if response.status in (403, 404):
            return ToolResult(
                source=self.name, tool="list_children", request_url=response.url,
                notes=[f"Synapse entity {sid} not found or not visible anonymously"],
            )
        payload = response.json() or {}
        records = []
        for child in payload.get("page") or []:
            cid = (child.get("id") or "").lower()
            records.append(
                Record(
                    source=self.name,
                    id=cid,
                    type=_rtype(child.get("type")),  # type: ignore[arg-type]
                    title=child.get("name"),
                    url=f"{WEB}{cid}",
                    identifiers={"synapse": cid},
                    date=(child.get("createdOn") or "")[:10] or None,
                    attributes={
                        k: v
                        for k, v in {
                            "entity_type": str(child.get("type") or "").split(".")[-1],
                            "parent_id": sid,
                            "modified": (child.get("modifiedOn") or "")[:10] or None,
                            "version": child.get("versionNumber"),
                        }.items()
                        if v not in (None, "")
                    },
                )
            )
        return ToolResult(
            source=self.name,
            tool="list_children",
            records=records,
            total=payload.get("totalChildCount"),
            next_cursor=payload.get("nextPageToken") or None,
            request_url=response.url,
        )
