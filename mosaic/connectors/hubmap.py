"""HuBMAP (Human BioMolecular Atlas Program) connector.

Docs: Search API https://smart-api.info/ui/7aaf02b838022d564da776b03f357158 (Elasticsearch DSL proxy),
Entity API https://smart-api.info/ui/0065e419668f3336a40d1f5ab89c6ba3
Hosts (verified live): https://search.api.hubmapconsortium.org/v3/portal/search (POST ES query;
the older search.hubmapconsortium.org host no longer resolves) and
https://entity.api.hubmapconsortium.org/entities/{uuid|HBM id}.
Auth: none for public entities (anonymous requests see the public portal index only).
Rate limits: none documented; we stay at 4 req/s.

Quirks
- ``query_string`` with no field list matches nothing on the portal index, so free text uses
  ``simple_query_string`` over explicit fields (title, description, dataset_type, organs, anatomy...).
- Portal documents are huge (ancestors, files, donors): always send ``_source`` includes.
- ``data_access_level`` 'protected' means raw sequence files require dbGaP authorization; metadata
  and processed results are still public. Shown prominently in attributes['access'].
- Entity API returns HTTP 400 for malformed ids and 404 for unknown ids.
- Publications are separate ``Publication`` entities whose ``ancestor_ids`` include the datasets used.

Identifier keys: non-canonical ``hubmap`` (HBM###.XXXX.### display id), ``doi`` (DataCite DOI
10.35079/HBM... for published datasets; article DOI for Publication records). The dbGaP study
accession (phs...) is shared by many datasets, so it is kept in attributes['dbgap_study'], not identifiers.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from pydantic import Field

from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, strip_html, tool

SEARCH = "https://search.api.hubmapconsortium.org/v3/portal/search"
ENTITY = "https://entity.api.hubmapconsortium.org/entities"
PORTAL = "https://portal.hubmapconsortium.org/browse"

_TEXT_FIELDS = [
    "title^3",
    "description",
    "dataset_type^2",
    "assay_display_name^2",
    "raw_dataset_type",
    "origin_samples_unique_mapped_organs^2",
    "anatomy_1",
    "anatomy_2",
    "hubmap_id",
    "group_name",
    "dataset_info",
]

_DATASET_SOURCE = [
    "uuid", "hubmap_id", "entity_type", "title", "description", "dataset_type", "assay_display_name",
    "raw_dataset_type", "origin_samples_unique_mapped_organs", "status", "data_access_level",
    "published_timestamp", "created_timestamp", "last_modified_timestamp", "group_name", "doi_url",
    "registered_doi", "processing", "pipeline", "donor_demographics", "mapped_consortium", "contributors",
    "is_spatial", "dbgap_study_url", "dbgap_sra_experiment_url", "analyte_class", "anatomy_2",
    "contains_human_genetic_sequences",
]

_ID_RE = re.compile(r"^(HBM\d{3}\.[A-Z]{4}\.\d{3}|[0-9a-f]{32})$", re.IGNORECASE)


class SearchDatasetsParams(SearchParams):
    limit: int = Field(10, ge=1, le=100, description="Maximum number of datasets to return")
    organ: str | None = Field(None, description="Organ filter, e.g. 'Kidney', 'Heart', 'Lymph Node', 'Placenta'")
    assay: str | None = Field(
        None, description="Assay / dataset type filter, e.g. 'RNAseq', 'ATACseq', 'CODEX', 'Light Sheet', 'Histology'"
    )
    processing: str | None = Field(None, description="'raw' (submitted data) or 'processed' (HuBMAP pipeline output)")
    access: str | None = Field(None, description="'public' or 'protected' (raw sequences behind dbGaP)")


class GetDatasetParams(ToolParams):
    dataset_id: str = Field(description="HuBMAP id 'HBM455.ZPNN.623' or 32-hex uuid")


class GetEntityParams(ToolParams):
    entity_id: str = Field(description="Any HuBMAP entity id (Donor, Sample, Dataset, Collection): HBM###.XXXX.### or uuid")


class SearchPublicationsParams(SearchParams):
    limit: int = Field(10, ge=1, le=100, description="Maximum number of publications to return")


class FacetParams(ToolParams):
    query: str | None = Field(None, description="Optional free text to restrict the counted datasets, e.g. 'kidney'")
    size: int = Field(25, ge=1, le=100, description="Maximum buckets per facet")


def _ts(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _compact(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v not in (None, [], "", {})}


def _dbgap(*urls: Any) -> str | None:
    for url in urls:
        m = re.search(r"phs\d{6}", str(url or ""))
        if m:
            return m.group(0)
    return None


def _demographics(demo: dict[str, Any] | None) -> dict[str, Any]:
    demo = demo or {}
    out: dict[str, Any] = {}
    for key, label in (("age_value", "age"), ("body_mass_index_value", "bmi")):
        values = demo.get(key)
        if values:
            out[label] = values[0] if len(values) == 1 else values[:10]
    if demo.get("age_unit"):
        out["age_unit"] = _first(demo["age_unit"])
    for key in ("sex", "race", "cause_of_death", "medical_history"):
        if demo.get(key):
            value = demo[key]
            if isinstance(value, list):
                value = value[0] if len(value) == 1 else value[:10]
            out[key] = value
    return out


def _authors(contributors: Any) -> list[str]:
    names = []
    for c in contributors or []:
        if isinstance(c, dict):
            name = c.get("name") or " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x)
            if name and name not in names:
                names.append(name)
    return names


def _dataset_record(src: dict[str, Any], *, full: bool = False) -> Record:
    uuid = src.get("uuid") or ""
    hubmap_id = src.get("hubmap_id")
    organs = src.get("origin_samples_unique_mapped_organs") or []
    files = [f for f in src.get("files") or [] if isinstance(f, dict)]
    attrs: dict[str, Any] = {
        "hubmap_id": hubmap_id,
        "uuid": uuid,
        "access": src.get("data_access_level"),
        "status": src.get("status"),
        "assay": _first(src.get("assay_display_name")) or src.get("dataset_type"),
        "dataset_type": src.get("dataset_type"),
        "organs": organs,
        "processing": src.get("processing"),
        "pipeline": src.get("pipeline"),
        "analyte_class": src.get("analyte_class"),
        "spatial": src.get("is_spatial"),
        "contains_human_genetic_sequences": src.get("contains_human_genetic_sequences"),
        "lab": src.get("group_name"),
        "consortium": src.get("mapped_consortium"),
        "donor": _demographics(src.get("donor_demographics")),
        "dbgap_study": _dbgap(src.get("dbgap_study_url")),
    }
    if full:
        if files:
            attrs["file_count"] = len(files)
            attrs["total_size_bytes"] = sum(int(f.get("size") or 0) for f in files)
        attrs["anatomy"] = (src.get("anatomy_2") or [])[:15]
        attrs["dbgap_study_url"] = src.get("dbgap_study_url")
        attrs["sra_experiment_url"] = src.get("dbgap_sra_experiment_url")
        attrs["derived_datasets"] = [
            _compact({"hubmap_id": d.get("hubmap_id"), "uuid": d.get("uuid"), "type": d.get("dataset_type")})
            for d in src.get("immediate_descendants") or []
            if isinstance(d, dict) and d.get("entity_type") == "Dataset"
        ][:20]
        attrs["created"] = _ts(src.get("created_timestamp"))
        attrs["last_modified"] = _ts(src.get("last_modified_timestamp"))
    return Record(
        source="hubmap",
        id=hubmap_id or uuid,
        type="dataset",
        title=src.get("title") or hubmap_id,
        url=f"{PORTAL}/dataset/{uuid}" if uuid else None,
        identifiers={
            "hubmap": hubmap_id,
            "doi": src.get("registered_doi") or src.get("doi_url"),
        },
        date=_ts(src.get("published_timestamp")) or _ts(src.get("created_timestamp")),
        authors=_authors(src.get("contributors")),
        summary=strip_html(src.get("description")),
        attributes=_compact(attrs),
    )


def _publication_record(src: dict[str, Any]) -> Record:
    uuid = src.get("uuid") or ""
    return Record(
        source="hubmap",
        id=src.get("hubmap_id") or uuid,
        type="article",
        title=src.get("title"),
        url=src.get("publication_url") or (f"{PORTAL}/publication/{uuid}" if uuid else None),
        identifiers={"doi": src.get("publication_doi"), "hubmap": src.get("hubmap_id")},
        date=(src.get("publication_date") or "")[:10] or None,
        authors=_authors(src.get("contributors")),
        summary=strip_html(src.get("description")),
        attributes=_compact(
            {
                "venue": src.get("publication_venue"),
                "published": src.get("publication_status"),
                "portal_url": f"{PORTAL}/publication/{uuid}" if uuid else None,
                "datasets_used": (src.get("ancestor_counts") or {}).get("entity_type", {}).get("Dataset"),
            }
        ),
    )


def _text_query(query: str | None) -> dict[str, Any]:
    if not query or query.strip() in ("", "*"):
        return {"match_all": {}}
    return {"simple_query_string": {"query": query, "fields": _TEXT_FIELDS, "default_operator": "and"}}


class HuBMAP(Connector):
    name = "hubmap"
    title = "HuBMAP Data Portal"
    category = "datasets"
    description = (
        "Human BioMolecular Atlas Program: ~10,000 single-cell, spatial and imaging datasets (snRNA-seq, "
        "ATAC-seq, CODEX, imaging mass spec, light sheet, histology) from healthy human organs, with donor "
        "demographics, organ/assay metadata, DOIs and linked publications. Best for healthy-tissue atlas data."
    )
    homepage = "https://portal.hubmapconsortium.org/"
    docs_url = "https://docs.hubmapconsortium.org/apis.html"
    auth = "none"
    rate_limit = 4.0
    health_query = "kidney"
    nih_repositories = ("HuBMAP Data Portal",)

    async def _search(self, body: dict[str, Any]):
        response = await self.request("POST", SEARCH, json_body=body, cache_ttl=3600)
        return response, response.json() or {}

    @tool(
        description="""
        Search HuBMAP datasets by free text over title, description, assay/dataset type, organ and
        anatomy (all words must match; supports "quoted phrases", -exclusion and | for OR). Returns one
        record per dataset with HuBMAP id, DOI, assay, organ(s), processing level, access level
        (public vs protected/dbGaP), lab, donor demographics (age, sex, race, BMI) and contributors.
        Use for healthy human tissue single-cell / spatial atlas data, e.g. 'kidney snRNAseq',
        'lymph node CODEX'. Filters: organ, assay, processing ('raw'/'processed'), access.
        For cancer imaging prefer TCIA; for neuroimaging prefer OpenNeuro.
        """,
        federated=True,
    )
    async def search_datasets(self, params: SearchDatasetsParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        filters: list[dict[str, Any]] = [{"term": {"entity_type.keyword": "Dataset"}}]
        must: list[dict[str, Any]] = [_text_query(params.query)]
        if params.organ:
            must.append({"match": {"origin_samples_unique_mapped_organs": {"query": params.organ, "operator": "and"}}})
        if params.assay:
            must.append(
                {"multi_match": {"query": params.assay, "fields": ["dataset_type", "assay_display_name", "raw_dataset_type"], "operator": "and"}}
            )
        if params.processing:
            filters.append({"term": {"processing.keyword": params.processing.strip().lower()}})
        if params.access:
            filters.append({"term": {"data_access_level.keyword": params.access.strip().lower()}})
        body = {
            "from": offset,
            "size": params.limit,
            "track_total_hits": True,
            "query": {"bool": {"must": must, "filter": filters}},
            "sort": ["_score", {"published_timestamp": {"order": "desc", "missing": "_last"}}],
            "_source": _DATASET_SOURCE,
        }
        response, payload = await self._search(body)
        hits = (payload.get("hits") or {}).get("hits") or []
        total = ((payload.get("hits") or {}).get("total") or {}).get("value")
        records = [_dataset_record(h.get("_source") or {}) for h in hits]
        nxt = offset + len(records)
        return ToolResult(
            source=self.name,
            tool="search_datasets",
            records=records,
            total=total,
            next_cursor=str(nxt) if records and total and nxt < total else None,
            request_url=response.url,
        )

    @tool(
        description="""
        Get one HuBMAP dataset by HuBMAP id (HBM455.ZPNN.623) or uuid: title, description, assay,
        organ(s) and anatomy, processing level and pipeline, access level, DOI, dbGaP study / SRA
        links, compact donor demographics, contributors, file count and total size (processed
        datasets), derived (processed) datasets, and publications that used it (with article DOIs).
        """
    )
    async def get_dataset(self, params: GetDatasetParams) -> ToolResult:
        ident = params.dataset_id.strip()
        if not _ID_RE.match(ident):
            return ToolResult(
                source=self.name, tool="get_dataset",
                notes=[f"{ident!r} is not a HuBMAP id; expected HBM###.XXXX.### or a 32-character uuid"],
            )
        field = "uuid" if len(ident) == 32 else "hubmap_id.keyword"
        value = ident.lower() if len(ident) == 32 else ident.upper()
        body = {
            "size": 1,
            "query": {"bool": {"filter": [{"term": {field: value}}]}},
            "_source": _DATASET_SOURCE
            + [
                "files.size",
                "immediate_descendants.hubmap_id",
                "immediate_descendants.uuid",
                "immediate_descendants.dataset_type",
                "immediate_descendants.entity_type",
            ],
        }
        response, payload = await self._search(body)
        hits = (payload.get("hits") or {}).get("hits") or []
        if not hits:
            return ToolResult(
                source=self.name, tool="get_dataset", notes=[f"HuBMAP entity {ident} not found in the public index"],
                request_url=response.url,
            )
        src = hits[0].get("_source") or {}
        notes = []
        if src.get("entity_type") and src.get("entity_type") != "Dataset":
            notes.append(f"{ident} is a {src.get('entity_type')}, not a Dataset; use get_entity for full details")
        record = _dataset_record(src, full=True)
        records = [record]
        uuid = src.get("uuid")
        if uuid:
            pub_body = {
                "size": 20,
                "query": {"bool": {"filter": [{"term": {"entity_type.keyword": "Publication"}}, {"term": {"ancestor_ids": uuid}}]}},
                "_source": ["uuid", "hubmap_id", "title", "publication_doi", "publication_url", "publication_venue", "publication_date"],
            }
            _, pubs = await self._search(pub_body)
            pub_hits = (pubs.get("hits") or {}).get("hits") or []
            if pub_hits:
                record.attributes["publications"] = [
                    _compact(
                        {
                            "title": (h.get("_source") or {}).get("title"),
                            "doi": (h.get("_source") or {}).get("publication_doi"),
                            "venue": (h.get("_source") or {}).get("publication_venue"),
                            "date": (h.get("_source") or {}).get("publication_date"),
                            "hubmap_id": (h.get("_source") or {}).get("hubmap_id"),
                        }
                    )
                    for h in pub_hits
                ]
                records.extend(_publication_record(h.get("_source") or {}) for h in pub_hits)
                notes.append(f"{len(pub_hits)} publication record(s) that used this dataset follow the dataset record")
        if record.attributes.get("access") == "protected":
            notes.append("access=protected: raw sequence data requires dbGaP authorization; metadata/processed data are public")
        return ToolResult(
            source=self.name, tool="get_dataset", records=records, total=1, request_url=response.url, notes=notes
        )

    @tool(
        description="""
        Search HuBMAP publications (papers built on HuBMAP data) by free text over title and
        abstract. Returns article records with DOI, venue, date, authors and how many HuBMAP datasets
        each used. Use to find the papers behind atlas data, or pass '*' to list all.
        """
    )
    async def search_publications(self, params: SearchPublicationsParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        text = (
            {"match_all": {}}
            if params.query.strip() in ("", "*")
            else {"simple_query_string": {"query": params.query, "fields": ["title^3", "description", "publication_venue"], "default_operator": "and"}}
        )
        body = {
            "from": offset,
            "size": params.limit,
            "track_total_hits": True,
            "query": {"bool": {"must": [text], "filter": [{"term": {"entity_type.keyword": "Publication"}}]}},
            "_source": [
                "uuid", "hubmap_id", "title", "description", "publication_doi", "publication_url",
                "publication_venue", "publication_date", "publication_status", "contributors", "ancestor_counts",
            ],
        }
        response, payload = await self._search(body)
        hits = (payload.get("hits") or {}).get("hits") or []
        total = ((payload.get("hits") or {}).get("total") or {}).get("value")
        records = [_publication_record(h.get("_source") or {}) for h in hits]
        nxt = offset + len(records)
        return ToolResult(
            source=self.name,
            tool="search_publications",
            records=records,
            total=total,
            next_cursor=str(nxt) if records and total and nxt < total else None,
            request_url=response.url,
        )

    @tool(
        description="""
        Fetch any HuBMAP entity (Donor, Sample, Dataset, Collection, Publication) from the Entity API
        by HBM id or uuid. Donors return compact clinical metadata (age, sex, race, BMI, cause of
        death, medical history); samples return sample category, organ and parent; datasets return
        DOI, dbGaP/SRA links and status. Use get_dataset for richer dataset views (organs, donor
        summary, files, publications).
        """
    )
    async def get_entity(self, params: GetEntityParams) -> ToolResult:
        ident = params.entity_id.strip()
        if not _ID_RE.match(ident):
            return ToolResult(
                source=self.name, tool="get_entity",
                notes=[f"{ident!r} is not a HuBMAP id; expected HBM###.XXXX.### or a 32-character uuid"],
            )
        response = await self.request("GET", f"{ENTITY}/{quote(ident)}", allow_status=(400, 403, 404))
        if response.status in (400, 404):
            return ToolResult(
                source=self.name, tool="get_entity", notes=[f"HuBMAP entity {ident} not found"], request_url=response.url
            )
        if response.status == 403:
            return ToolResult(
                source=self.name, tool="get_entity",
                notes=[f"HuBMAP entity {ident} is not public (requires consortium login)"], request_url=response.url,
            )
        ent = response.json() or {}
        etype = ent.get("entity_type") or "Entity"
        uuid = ent.get("uuid") or ""
        hubmap_id = ent.get("hubmap_id")
        meta = ent.get("metadata") if isinstance(ent.get("metadata"), dict) else {}
        attrs: dict[str, Any] = {
            "entity_type": etype,
            "uuid": uuid,
            "access": ent.get("data_access_level"),
            "status": ent.get("status"),
            "lab": ent.get("group_name"),
            "protocol_url": ent.get("protocol_url"),
            "sample_category": ent.get("sample_category"),
            "organ_code": ent.get("organ"),
            "dataset_type": ent.get("dataset_type"),
            "dbgap_study": _dbgap(ent.get("dbgap_study_url")),
            "dbgap_study_url": ent.get("dbgap_study_url"),
            "sra_experiment_url": ent.get("dbgap_sra_experiment_url"),
        }
        parent = ent.get("direct_ancestor") or _first(ent.get("direct_ancestors"))
        if isinstance(parent, dict):
            attrs["parent"] = _compact(
                {"hubmap_id": parent.get("hubmap_id"), "entity_type": parent.get("entity_type"), "uuid": parent.get("uuid")}
            )
        donor_data = meta.get("organ_donor_data") or meta.get("living_donor_data") or []
        if donor_data:
            clinical: dict[str, Any] = {}
            for item in donor_data:
                if not isinstance(item, dict):
                    continue
                group = item.get("grouping_concept_preferred_term") or item.get("preferred_term")
                if not group:
                    continue
                if item.get("data_type") == "Numeric":
                    value = f"{item.get('data_value')} {item.get('units') or ''}".strip()
                else:
                    value = item.get("preferred_term") or item.get("data_value")
                key = re.sub(r"[^a-z0-9]+", "_", group.lower()).strip("_")
                if key in clinical:
                    existing = clinical[key] if isinstance(clinical[key], list) else [clinical[key]]
                    clinical[key] = existing + [value]
                else:
                    clinical[key] = value
            attrs["clinical"] = clinical
        title = ent.get("title") or ent.get("lab_donor_id") or f"HuBMAP {etype} {hubmap_id}"
        record = Record(
            source=self.name,
            id=hubmap_id or uuid,
            type={"Donor": "other", "Sample": "sample", "Dataset": "dataset", "Publication": "article"}.get(etype, "other"),
            title=title,
            url=f"{PORTAL}/{etype.lower()}/{uuid}" if uuid else None,
            identifiers={
                "hubmap": hubmap_id,
                "doi": ent.get("registered_doi") or ent.get("publication_doi"),
            },
            date=_ts(ent.get("published_timestamp")) or _ts(ent.get("created_timestamp")),
            authors=_authors(ent.get("contributors")),
            summary=strip_html(ent.get("description")),
            attributes=_compact(attrs),
        )
        return ToolResult(source=self.name, tool="get_entity", records=[record], total=1, request_url=response.url)

    @tool(
        description="""
        Count HuBMAP datasets by organ, assay type, processing level and access level (Elasticsearch
        aggregations), optionally restricted by free text. Returns tables in data, e.g. how many
        public kidney snRNA-seq datasets exist. Use to understand atlas coverage before searching.
        """
    )
    async def dataset_facets(self, params: FacetParams) -> ToolResult:
        body = {
            "size": 0,
            "track_total_hits": True,
            "query": {"bool": {"must": [_text_query(params.query)], "filter": [{"term": {"entity_type.keyword": "Dataset"}}]}},
            "aggs": {
                "organ": {"terms": {"field": "origin_samples_unique_mapped_organs.keyword", "size": params.size}},
                "assay": {"terms": {"field": "dataset_type.keyword", "size": params.size}},
                "processing": {"terms": {"field": "processing.keyword", "size": 5}},
                "access": {"terms": {"field": "data_access_level.keyword", "size": 5}},
            },
        }
        response, payload = await self._search(body)
        aggs = payload.get("aggregations") or {}
        data = {
            name: {b.get("key"): b.get("doc_count") for b in (aggs.get(name) or {}).get("buckets") or []}
            for name in ("organ", "assay", "processing", "access")
        }
        total = ((payload.get("hits") or {}).get("total") or {}).get("value")
        return ToolResult(
            source=self.name, tool="dataset_facets", total=total, data=data, request_url=response.url
        )
