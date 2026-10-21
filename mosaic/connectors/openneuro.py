"""OpenNeuro connector (BIDS neuroimaging / electrophysiology datasets).

Docs: GraphQL endpoint https://openneuro.org/crn/graphql (introspectable; schema source
https://github.com/OpenNeuroOrg/openneuro/tree/master/packages/openneuro-server/src/graphql).
Auth: none for public datasets. Rate limits: none documented; we stay at 3 req/s.

Quirks (verified live, server 5.9)
- The legacy ``search(q:)`` query returns null; free-text search is
  ``advancedSearch(query: DatasetSearchInput!, first, after)`` with ``keywords: [String!]``
  (Elasticsearch-backed, relevance sort via ``sortBy: relevance``). ``modality`` filter values are
  lowercase BIDS modalities (mri, eeg, meg, ieeg, pet, beh, nirs...).
- Hits the anonymous user cannot read come back as ``null`` edges plus a partial GraphQL error
  ("You do not have access to read this dataset."); we drop them. ``pageInfo.count`` includes them.
- Pagination is a relay cursor (``pageInfo.endCursor``), passed back as ``next_cursor``.
- ``dataset(id)`` for an unknown id returns ``{"dataset": null}`` with an error -> not-found note.
- Descriptions live in the snapshot ``dataset_description.json`` (``description``) and README.

Identifier keys: ``openneuro`` (ds######), ``doi`` (snapshot DOI 10.18112/openneuro.ds000117.v2.0.0).
Associated paper DOIs / PubMed links stay in attributes (they identify papers, not the dataset).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import Field

from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, strip_html, tool

GRAPHQL = "https://openneuro.org/crn/graphql"
WEB = "https://openneuro.org/datasets"

_SORTS = ("relevance", "newest", "oldest", "last_updated", "activity", "name_asc", "name_desc")

_SNAPSHOT_FIELDS = """
  id tag created size
  description { Name Authors DatasetDOI License Funding ReferencesAndLinks Acknowledgements
                HowToAcknowledge EthicsApprovals BIDSVersion DatasetType SeniorAuthor }
  summary { modalities primaryModality secondaryModalities subjects sessions tasks totalFiles size dataProcessed }
"""

_DATASET_FIELDS = """
  id name created publishDate
  metadata { modalities species studyDomain studyDesign associatedPaperDOI openneuroPaperDOI dxStatus
             tasksCompleted grantFunderName grantIdentifier seniorAuthor trialCount studyLongitudinal }
"""

SEARCH_QUERY = (
    "query($query: DatasetSearchInput!, $first: Int, $after: String) {"
    " advancedSearch(query: $query, first: $first, after: $after) {"
    " pageInfo { hasNextPage endCursor count }"
    " edges { id node {" + _DATASET_FIELDS + " latestSnapshot {" + _SNAPSHOT_FIELDS + " readme } } } } }"
)

DATASET_QUERY = (
    "query($id: ID!) { dataset(id: $id) {" + _DATASET_FIELDS + " public"
    " snapshots { tag created }"
    " latestSnapshot {" + _SNAPSHOT_FIELDS + " readme } } }"
)

SNAPSHOT_QUERY = (
    "query($id: ID!, $tag: String!) { snapshot(datasetId: $id, tag: $tag) {" + _SNAPSHOT_FIELDS + " readme"
    " dataset {" + _DATASET_FIELDS + " public snapshots { tag created } } } }"
)

FILES_QUERY = (
    "query($id: ID!, $tag: String!, $tree: String) { snapshot(datasetId: $id, tag: $tag) {"
    " id tag files(tree: $tree) { id filename size directory annexed urls } } }"
)

LATEST_TAG_QUERY = "query($id: ID!) { dataset(id: $id) { id latestSnapshot { tag } } }"


class SearchDatasetsParams(SearchParams):
    modality: str | None = Field(
        None, description="BIDS modality filter, lowercase: mri, eeg, meg, ieeg, pet, beh, nirs, motion"
    )
    species: str | None = Field(None, description="Species filter, e.g. 'Human', 'Rat', 'Mouse'")
    sort: str = Field("relevance", description="Sort: " + ", ".join(_SORTS))


class GetDatasetParams(ToolParams):
    dataset_id: str = Field(
        description="OpenNeuro accession 'ds000117' (also accepts '117', an openneuro.org URL or the "
        "snapshot DOI 10.18112/openneuro.ds000117.v1.0.5)"
    )
    tag: str | None = Field(None, description="Snapshot version tag, e.g. '1.0.5'. Default: latest snapshot")


class ListFilesParams(ToolParams):
    dataset_id: str = Field(description="OpenNeuro accession, e.g. 'ds000117'")
    tag: str | None = Field(None, description="Snapshot version tag (default: latest snapshot)")
    directory_id: str | None = Field(
        None, description="id of a directory record from a previous list_files call, to list inside it"
    )
    limit: int = Field(100, ge=1, le=100, description="Maximum number of entries to return")


def parse_dataset_id(value: str) -> tuple[str | None, str | None]:
    """Return (ds######, tag-or-None)."""
    text = value.strip()
    m = re.search(r"openneuro\.(ds\d{6})\.v(\d[\w.\-]*)", text, re.IGNORECASE)
    if m:
        return m.group(1).lower(), m.group(2)
    m = re.search(r"(ds\d{6})(?:/versions/([\w.\-]+))?", text, re.IGNORECASE)
    if m:
        return m.group(1).lower(), m.group(2)
    m = re.fullmatch(r"(?:ds)?(\d{1,6})", text, re.IGNORECASE)
    if m:
        return f"ds{int(m.group(1)):06d}", None
    return None, None


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v not in (None, "")]
    return [value] if value != "" else []


def _date(value: str | None) -> str | None:
    return value[:10] if value else None


def _compact(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v not in (None, [], "", {})}


def _paper_links(meta: dict[str, Any], desc: dict[str, Any]) -> tuple[list[str], list[str]]:
    dois: list[str] = []
    pmids: list[str] = []
    texts = [meta.get("associatedPaperDOI"), meta.get("openneuroPaperDOI"), *(_list(desc.get("ReferencesAndLinks")))]
    for text in texts:
        if not text:
            continue
        text = str(text)
        for m in re.finditer(r"10\.\d{4,9}/[^\s,;\"'<>)\]]+", text):
            doi = normalize_doi(m.group(0).rstrip("."))
            if doi not in dois and "openneuro.ds" not in doi:
                dois.append(doi)
        for m in re.finditer(r"(?:pubmed(?:\.ncbi\.nlm\.nih\.gov)?/|PMID:?\s*)(\d{5,9})", text, re.IGNORECASE):
            if m.group(1) not in pmids:
                pmids.append(m.group(1))
    return dois, pmids


def _readme_text(readme: str | None, limit: int = 4000) -> str | None:
    """README (markdown/plain text) -> compact summary: drop heading rules, tables and blank lines."""
    if not readme:
        return None
    lines = []
    for line in str(readme)[: limit * 2].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("|") or re.fullmatch(r"[=\-#*_|: ]+", stripped):
            continue
        lines.append(stripped.lstrip("#").strip())
    text = strip_html(" ".join(lines))
    return text[:limit] if text else None


def _record(node: dict[str, Any], snapshot: dict[str, Any] | None, *, full: bool = False) -> Record:
    snapshot = snapshot or {}
    meta = node.get("metadata") or {}
    desc = snapshot.get("description") or {}
    summary = snapshot.get("summary") or {}
    ds_id = node.get("id") or str(snapshot.get("id", "")).split(":")[0]
    tag = snapshot.get("tag")
    paper_dois, pmids = _paper_links(meta, desc)
    subjects = _list(summary.get("subjects"))
    subjects = [s for s in subjects if str(s).lower() != "emptyroom"]
    summary_text = _readme_text(snapshot.get("readme"))
    attrs: dict[str, Any] = {
        "snapshot": tag,
        "modalities": _list(summary.get("modalities")) or _list(meta.get("modalities")),
        "primary_modality": summary.get("primaryModality"),
        "secondary_modalities": _list(summary.get("secondaryModalities")),
        "tasks": _list(summary.get("tasks")) or _list(meta.get("tasksCompleted")),
        "subject_count": len(subjects) if subjects else None,
        "session_count": len(_list(summary.get("sessions"))) or None,
        "total_files": summary.get("totalFiles"),
        "size_bytes": snapshot.get("size") or summary.get("size"),
        "species": meta.get("species"),
        "study_domain": meta.get("studyDomain"),
        "study_design": meta.get("studyDesign"),
        "diagnosis": meta.get("dxStatus"),
        "license": desc.get("License"),
        "funding": _list(desc.get("Funding"))[:10],
        "grant": " ".join(x for x in (meta.get("grantFunderName"), meta.get("grantIdentifier")) if x) or None,
        "senior_author": desc.get("SeniorAuthor") or meta.get("seniorAuthor"),
        "bids_version": desc.get("BIDSVersion"),
        "dataset_type": desc.get("DatasetType"),
        "data_processed": summary.get("dataProcessed"),
        "paper_dois": paper_dois,
        "paper_pmids": pmids,
        "first_published": _date(node.get("publishDate")),
    }
    if full:
        attrs.update(
            {
                "references": _list(desc.get("ReferencesAndLinks"))[:15],
                "how_to_acknowledge": desc.get("HowToAcknowledge"),
                "ethics_approvals": _list(desc.get("EthicsApprovals")),
                "versions": [s.get("tag") for s in node.get("snapshots") or [] if s.get("tag")],
                "public": node.get("public"),
            }
        )
    title = desc.get("Name") or node.get("name")
    url = f"{WEB}/{ds_id}/versions/{tag}" if tag else f"{WEB}/{ds_id}"
    return Record(
        source="openneuro",
        id=ds_id,
        type="dataset",
        title=title,
        url=url,
        identifiers={"openneuro": ds_id, "doi": desc.get("DatasetDOI")},
        date=_date(snapshot.get("created") or node.get("publishDate") or node.get("created")),
        authors=[str(a) for a in _list(desc.get("Authors"))],
        summary=summary_text,
        attributes=_compact(attrs),
    )


class OpenNeuro(Connector):
    name = "openneuro"
    title = "OpenNeuro"
    category = "datasets"
    description = (
        "Open archive of 1,500+ BIDS-formatted neuroimaging and electrophysiology datasets (MRI/fMRI, "
        "EEG, MEG, iEEG, PET) with tasks, subject counts, authors and snapshot DOIs. Best for finding "
        "human brain-imaging datasets by topic, task or modality."
    )
    homepage = "https://openneuro.org/"
    docs_url = "https://docs.openneuro.org/api.html"
    auth = "none"
    rate_limit = 3.0
    health_query = "working memory"
    nih_repositories = ("OpenNeuro",)

    @tool(
        description="""
        Search OpenNeuro public datasets (BIDS: MRI/fMRI, EEG, MEG, iEEG, PET, behavior) by free text
        over names, descriptions, README, tasks and authors, ranked by relevance. Returns one record
        per dataset with title, authors, snapshot DOI, modalities, tasks, subject/session counts, size,
        license, species and associated paper DOIs. Use for human neuroimaging data discovery
        (e.g. 'visual working memory', 'resting state', 'Parkinson'); for animal cellular
        neurophysiology (NWB) prefer DANDI. Optional filters: modality (lowercase, e.g. 'eeg'),
        species, sort (relevance, newest, ...).
        """,
        federated=True,
    )
    async def search_datasets(self, params: SearchDatasetsParams) -> ToolResult:
        sort = params.sort if params.sort in _SORTS else "relevance"
        query: dict[str, Any] = {"keywords": [params.query], "sortBy": sort}
        if params.modality:
            query["modality"] = params.modality.strip().lower()
        if params.species:
            query["species"] = params.species.strip()
        variables = {"query": query, "first": params.limit, "after": params.cursor}
        data = await self.graphql(GRAPHQL, SEARCH_QUERY, variables)
        conn = data.get("advancedSearch") or {}
        edges = conn.get("edges") or []
        page = conn.get("pageInfo") or {}
        records = []
        hidden = 0
        for edge in edges:
            node = (edge or {}).get("node")
            if not node:
                hidden += 1
                continue
            records.append(_record(node, node.get("latestSnapshot")))
        notes = []
        if hidden:
            notes.append(f"{hidden} hit(s) omitted: not publicly readable (embargoed/private datasets)")
        if sort == "relevance" and not records and not edges:
            notes.append("no matches; try fewer or broader keywords")
        return ToolResult(
            source=self.name,
            tool="search_datasets",
            records=records,
            total=page.get("count"),
            next_cursor=page.get("endCursor") if page.get("hasNextPage") and edges else None,
            request_url=GRAPHQL,
            notes=notes,
        )

    @tool(
        description="""
        Get one OpenNeuro dataset by accession (ds######) at its latest snapshot (or a given tag):
        name, authors, snapshot DOI, license, funding, modalities, tasks, subject and session counts,
        size, BIDS version, references (paper DOIs / PubMed IDs), how-to-acknowledge, list of
        versions and a README excerpt as the summary.
        """
    )
    async def get_dataset(self, params: GetDatasetParams) -> ToolResult:
        ds_id, tag = parse_dataset_id(params.dataset_id)
        if not ds_id:
            return ToolResult(
                source=self.name,
                tool="get_dataset",
                notes=[f"could not parse an OpenNeuro accession from {params.dataset_id!r}; expected 'ds000117'"],
            )
        tag = params.tag or tag
        if tag:
            data = await self.graphql(GRAPHQL, SNAPSHOT_QUERY, {"id": ds_id, "tag": tag})
            snapshot = data.get("snapshot")
            node = (snapshot or {}).get("dataset") or {}
        else:
            data = await self.graphql(GRAPHQL, DATASET_QUERY, {"id": ds_id})
            node = data.get("dataset") or {}
            snapshot = node.get("latestSnapshot")
        if not node and not snapshot:
            what = f"{ds_id} snapshot {tag}" if tag else ds_id
            return ToolResult(
                source=self.name, tool="get_dataset", notes=[f"OpenNeuro dataset {what} not found or not public"],
                request_url=GRAPHQL,
            )
        notes = []
        if not snapshot:
            notes.append("dataset has no public snapshot yet; showing draft-level metadata only")
        record = _record({**node, "id": node.get("id") or ds_id}, {**(snapshot or {})}, full=True)
        return ToolResult(
            source=self.name, tool="get_dataset", records=[record], total=1, request_url=GRAPHQL, notes=notes
        )

    @tool(
        description="""
        List files and folders of an OpenNeuro dataset snapshot (BIDS layout: dataset_description.json,
        participants.tsv, sub-XX/ folders...). Each entry has filename, size, whether it is a
        directory, and a direct S3 download URL. Pass directory_id from a folder entry to list inside it.
        """
    )
    async def list_files(self, params: ListFilesParams) -> ToolResult:
        ds_id, tag = parse_dataset_id(params.dataset_id)
        if not ds_id:
            return ToolResult(
                source=self.name, tool="list_files", notes=[f"could not parse an accession from {params.dataset_id!r}"]
            )
        tag = params.tag or tag
        if not tag:
            data = await self.graphql(GRAPHQL, LATEST_TAG_QUERY, {"id": ds_id})
            tag = ((data.get("dataset") or {}).get("latestSnapshot") or {}).get("tag")
            if not tag:
                return ToolResult(
                    source=self.name, tool="list_files", notes=[f"OpenNeuro dataset {ds_id} not found or has no snapshot"]
                )
        data = await self.graphql(GRAPHQL, FILES_QUERY, {"id": ds_id, "tag": tag, "tree": params.directory_id})
        snapshot = data.get("snapshot")
        if not snapshot:
            return ToolResult(
                source=self.name, tool="list_files", notes=[f"snapshot {ds_id}:{tag} not found"], request_url=GRAPHQL
            )
        files = [f for f in snapshot.get("files") or [] if isinstance(f, dict)]
        records = [
            Record(
                source=self.name,
                id=f.get("id") or f.get("filename") or "",
                type="other",
                title=(f.get("filename") or "") + ("/" if f.get("directory") else ""),
                url=(_list(f.get("urls")) or [None])[0] if not f.get("directory") else None,
                attributes=_compact(
                    {
                        "dataset": ds_id,
                        "snapshot": tag,
                        "directory": bool(f.get("directory")),
                        "size_bytes": f.get("size"),
                        "annexed": f.get("annexed"),
                    }
                ),
            )
            for f in files[: params.limit]
        ]
        notes = []
        if len(files) > params.limit:
            notes.append(f"{len(files) - params.limit} more entries not shown; raise limit or list a subdirectory")
        return ToolResult(
            source=self.name, tool="list_files", records=records, total=len(files), request_url=GRAPHQL, notes=notes
        )
