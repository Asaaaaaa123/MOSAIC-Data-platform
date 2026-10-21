"""The Cancer Imaging Archive (TCIA) connector: cancer imaging collections, studies and series.

Docs: https://wiki.cancerimagingarchive.net/display/Public/TCIA+REST+API+Guide (NBIA v1)
      https://github.com/kirbyju/tcia_utils (reference client for both APIs)
Two anonymous, keyless APIs:

1. TCIA website REST API (WordPress, ``https://www.cancerimagingarchive.net/api/v1/``):
   ``collections`` (~240; collection_short_title, collection_title, collection_doi, cancer_types,
   cancer_locations, data_types (modalities), supporting_data, species, subjects (string),
   collection_status, collection_page_accessibility (Public | Limited | ""), program,
   collection_summary HTML, version_number, date_updated, and ids of citations / downloads /
   related analysis results), ``downloads`` (per-package data_license, download_access,
   subjects/study/series/image counts, size, file types), ``citations`` (data citation text +
   DOI), ``analysis-results`` (derived datasets: segmentations, annotations, with result_doi).
   WordPress paging: ``per_page`` <= 100, ``page`` beyond the last page returns HTTP 400;
   ``_fields`` trims payloads, ``include=1,2,3`` fetches by id, ``slug=`` by slug.
   We cache the full collection list for 24 h (HTTP cache + in-process copy) and rank it
   client-side, because the WordPress ``search`` only matches titles/body text.
2. NBIA search API v1 (``https://services.cancerimagingarchive.net/nbia-api/services/v1/``):
   ``getSeries`` / ``getPatientStudy`` with ``Collection``, ``PatientID``, ``StudyInstanceUID``,
   ``Modality``, ``BodyPartExamined``. Anonymous calls see PUBLIC data only: limited-access
   collections and unknown collection names both return HTTP 200 with an empty body. No server-side
   paging, so results are paged client-side; very large collections (e.g. NLST) return many MB,
   so filter by patient or modality. ``getCollectionDescriptions`` and v2 endpoints need a token.

Rate limits: none documented; we stay at 3 req/s.

Identifier keys: ``doi`` (collection DOI, 10.7937/...), ``tcia`` (non-canonical: the collection
short name used by NBIA, e.g. ``TCGA-BRCA``), ``gdc_project`` for TCGA-* collections (same id as
the NCI GDC project). Series carry ``dicom_series_uid`` (non-canonical; shared with IDC).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from typing import Any, Literal

from pydantic import Field

from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, strip_html, tool

WP = "https://www.cancerimagingarchive.net/api/v1"
NBIA = "https://services.cancerimagingarchive.net/nbia-api/services/v1"
VIEWER = "https://nbia.cancerimagingarchive.net/viewer/?series="
LIST_TTL = 24 * 3600
NBIA_TTL = 6 * 3600

_LIST_FIELDS = (
    "id,slug,link,collection_doi,collection_short_title,collection_title,cancer_types,cancer_locations,"
    "data_types,supporting_data,species,subjects,collection_status,collection_page_accessibility,"
    "date_updated,version_number,program,collection_summary"
)
_DETAIL_FIELDS = (
    _LIST_FIELDS + ",citations,collection_downloads,related_analysis_results,version_change_log,"
    "additional_resources,usage_notes,detailed_description,collection_funding"
)
_DOWNLOAD_FIELDS = (
    "id,download_title,download_type,data_license,download_access,subjects,study_count,series_count,"
    "image_count,download_size,download_size_unit,file_type,data_type,search_url,download_requirements,download_url"
)

# query term -> extra spellings to look for (modality codes, common synonyms)
_ALIASES: dict[str, tuple[str, ...]] = {
    "mri": ("mr",),
    "mr": ("mri",),
    "pet": ("pt",),
    "pt": ("pet",),
    "mammography": ("mg",),
    "mammogram": ("mg",),
    "xray": ("cr", "dx"),
    "x-ray": ("cr", "dx"),
    "radiograph": ("cr", "dx"),
    "ultrasound": ("us",),
    "pathology": ("histopathology",),
    "histology": ("histopathology",),
    "wsi": ("histopathology",),
    "segmentation": ("seg", "rtstruct"),
    "radiotherapy": ("rtstruct", "rtdose", "rtplan"),
    "glioblastoma": ("gbm",),
    "gbm": ("glioblastoma",),
}

_FIELD_WEIGHTS = (
    ("short", 6.0),
    ("title", 5.0),
    ("cancer", 5.0),
    ("modality", 4.0),
    ("program", 3.0),
    ("other", 2.0),
    ("summary", 1.0),
)


class SearchCollectionsParams(SearchParams):
    modality: str | None = Field(
        None, description="Only collections with this modality/data type, e.g. CT, MR, PT, MG, Histopathology, SEG"
    )
    cancer_type: str | None = Field(
        None, description="Only collections whose cancer types/locations contain this text, e.g. 'lung', 'breast'"
    )
    access: Literal["public", "limited"] | None = Field(
        None, description="public = downloadable by anyone; limited = requires a TCIA/dbGaP data access request"
    )


class GetCollectionParams(ToolParams):
    collection: str = Field(
        description="Collection short name as used by NBIA ('TCGA-BRCA', 'LIDC-IDRI', 'NSCLC-Radiomics'), its "
        "website slug or URL (https://www.cancerimagingarchive.net/collection/tcga-brca/) or its DOI (10.7937/...)"
    )


class SeriesForCollectionParams(ToolParams):
    collection: str = Field(description="Collection short name, e.g. 'TCGA-BRCA' (see search_collections)")
    modality: str | None = Field(None, description="DICOM modality code: CT, MR, PT, MG, CR, DX, US, SEG, RTSTRUCT, SR...")
    patient_id: str | None = Field(None, description="Only this subject, e.g. 'TCGA-AR-A1AQ' (strongly advised for big collections)")
    study_uid: str | None = Field(None, description="Only series of this StudyInstanceUID")
    body_part: str | None = Field(None, description="BodyPartExamined, e.g. 'BREAST', 'CHEST', 'BRAIN'")
    limit: int = Field(25, ge=1, le=100, description="Maximum number of series to return")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous result)")


class StudiesForCollectionParams(ToolParams):
    collection: str = Field(description="Collection short name, e.g. 'TCGA-BRCA'")
    patient_id: str | None = Field(None, description="Only this subject, e.g. 'TCGA-AR-A1AQ'")
    limit: int = Field(25, ge=1, le=100, description="Maximum number of studies to return")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous result)")


def _compact(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v is not None and v != [] and v != "" and v != {}}


def _int(value: Any) -> int | None:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v not in (None, "")]
    return []


def _text(value: Any) -> str | None:
    """WordPress custom fields are a string, a list of strings, or ``false``."""
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value if v not in (None, "", False))
    if value in (None, False):
        return None
    text = str(value).strip()
    return text or None


def _date(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:10] if re.match(r"\d{4}-\d{2}-\d{2}", text) else None


def _title(item: dict[str, Any]) -> str:
    rendered = item.get("title") if isinstance(item.get("title"), dict) else {}
    return strip_html(rendered.get("rendered")) or ""


def _access(item: dict[str, Any]) -> str | None:
    value = (item.get("collection_page_accessibility") or "").strip()
    return value or None


def _short(item: dict[str, Any]) -> str:
    return (item.get("collection_short_title") or _title(item) or item.get("slug") or "").strip()


def _collection_record(item: dict[str, Any]) -> Record:
    short = _short(item)
    doi = item.get("collection_doi")
    gdc = short.upper() if re.fullmatch(r"TCGA-[A-Z]{2,5}", short.upper()) else None
    access = _access(item)
    attrs = {
        "short_name": short,
        "access": access or "unspecified",
        "status": item.get("collection_status"),
        "cancer_types": _list(item.get("cancer_types")),
        "cancer_locations": _list(item.get("cancer_locations")),
        "modalities": _list(item.get("data_types")),
        "supporting_data": _list(item.get("supporting_data")),
        "species": _list(item.get("species")),
        "subjects": _int(item.get("subjects")),
        "program": _list(item.get("program")),
        "version": item.get("version_number"),
        "wordpress_id": item.get("id"),
    }
    return Record(
        source="tcia",
        id=short,
        type="dataset",
        title=strip_html(item.get("collection_title")) or short,
        url=item.get("link") or None,
        identifiers={"doi": doi, "tcia": short, "gdc_project": gdc},
        date=_date(item.get("date_updated")),
        summary=strip_html(item.get("collection_summary")),
        attributes=_compact(attrs),
    )


def _haystacks(item: dict[str, Any]) -> dict[str, str]:
    return {
        "short": _short(item).lower(),
        "title": (item.get("collection_title") or "").lower(),
        "cancer": " | ".join(_list(item.get("cancer_types")) + _list(item.get("cancer_locations"))).lower(),
        "modality": " | ".join(_list(item.get("data_types"))).lower(),
        "program": " | ".join(_list(item.get("program"))).lower(),
        "other": " | ".join(_list(item.get("supporting_data")) + _list(item.get("species"))).lower(),
        "summary": (strip_html(item.get("collection_summary")) or "").lower(),
    }


def _score(item: dict[str, Any], terms: list[str], phrase: str) -> tuple[int, float]:
    hay = _haystacks(item)
    matched, score = 0, 0.0
    for term in terms:
        variants = (term, *_ALIASES.get(term, ()))
        hit = False
        for field, weight in _FIELD_WEIGHTS:
            for variant in variants:
                # short codes (CT, MR, PT) must match whole words; longer terms may be prefixes
                tail = r"(?![a-z0-9])" if len(variant) <= 3 else ""
                if re.search(r"(?<![a-z0-9])" + re.escape(variant) + tail, hay[field]):
                    score += weight
                    hit = True
                    break
        matched += hit
    if phrase:
        squashed = re.sub(r"[^a-z0-9]", "", phrase)
        if squashed and re.sub(r"[^a-z0-9]", "", hay["short"]) == squashed:
            score += 20
        if len(terms) > 1 and phrase in hay["title"]:
            score += 8
    return matched, score


def _series_record(s: dict[str, Any]) -> Record:
    uid = s.get("SeriesInstanceUID") or ""
    desc = (s.get("SeriesDescription") or "").strip()
    modality = s.get("Modality") or "?"
    patient = s.get("PatientID") or ""
    title = f"{modality} series" + (f" '{desc}'" if desc else "") + (f" of {patient}" if patient else "")
    size = s.get("FileSize")
    return Record(
        source="tcia",
        id=uid,
        type="other",
        title=title,
        url=f"{VIEWER}{uid}" if uid else None,
        identifiers={"dicom_series_uid": uid},
        date=_date(s.get("SeriesDate")),
        attributes=_compact(
            {
                "collection": s.get("Collection"),
                "patient_id": patient,
                "study_uid": s.get("StudyInstanceUID"),
                "modality": s.get("Modality"),
                "body_part": s.get("BodyPartExamined"),
                "series_description": desc,
                "protocol": (s.get("ProtocolName") or "").strip(),
                "series_number": s.get("SeriesNumber"),
                "image_count": s.get("ImageCount"),
                "size_mb": round(size / 1e6, 2) if isinstance(size, (int, float)) else None,
                "manufacturer": s.get("Manufacturer"),
                "model": s.get("ManufacturerModelName"),
                "license": s.get("LicenseName"),
                "collection_doi": normalize_doi(s["CollectionURI"]) if s.get("CollectionURI") else None,
                "released": _date(s.get("DateReleased")),
            }
        ),
    )


def _study_record(s: dict[str, Any]) -> Record:
    uid = s.get("StudyInstanceUID") or ""
    desc = (s.get("StudyDescription") or "").strip()
    desc = "" if desc.upper() in ("NA", "N/A") else desc
    patient = s.get("PatientID") or ""
    return Record(
        source="tcia",
        id=uid,
        type="study",
        title=(desc or "Imaging study") + (f" of {patient}" if patient else ""),
        identifiers={"dicom_study_uid": uid},
        date=_date(s.get("StudyDate")),
        attributes=_compact(
            {
                "collection": s.get("Collection"),
                "patient_id": patient,
                "patient_sex": s.get("PatientSex"),
                "patient_age": s.get("PatientAge"),
                "series_count": s.get("SeriesCount"),
                "study_description": desc,
            }
        ),
    )


def _download_entry(d: dict[str, Any]) -> dict[str, Any]:
    size = _text(d.get("download_size"))
    unit = (_text(d.get("download_size_unit")) or "").upper()
    return _compact(
        {
            "title": _text(d.get("download_title")),
            "type": _text(d.get("download_type")),
            "access": _text(d.get("download_access")),
            "license": _text(d.get("data_license")),
            "size": f"{size} {unit}".strip() if size else None,
            "file_types": _list(d.get("file_type")),
            "modalities": _list(d.get("data_type")),
            "subjects": _int(d.get("subjects")),
            "studies": _int(d.get("study_count")),
            "series": _int(d.get("series_count")),
            "images": _int(d.get("image_count")),
            "requirements": _text(d.get("download_requirements")),
            "url": next((u for u in (_text(d.get("search_url")), _text(d.get("download_url"))) if u and len(u) <= 250), None),
        }
    )


class Tcia(Connector):
    name = "tcia"
    title = "The Cancer Imaging Archive (TCIA)"
    category = "datasets"
    description = (
        "NCI archive of ~240 de-identified cancer imaging collections (CT, MR, PET, mammography, digital "
        "pathology; TCGA/CPTAC/clinical-trial cohorts) with DOIs, cancer types, modalities, subject counts and "
        "access level, plus public DICOM study/series metadata. Best for finding imaging cohorts for a cancer type."
    )
    homepage = "https://www.cancerimagingarchive.net/"
    docs_url = "https://wiki.cancerimagingarchive.net/display/Public/TCIA+REST+API+Guide"
    auth = "none"
    rate_limit = 3.0
    health_query = "lung"
    nih_repositories = ("The Cancer Imaging Archive (TCIA)",)

    def __init__(self, settings, http):
        super().__init__(settings, http)
        self._collections: list[dict[str, Any]] | None = None
        self._collections_at = 0.0

    # ---- collection list (cached) ----
    async def _page(self, page: int) -> list[dict[str, Any]]:
        response = await self.request(
            "GET",
            f"{WP}/collections/",
            params={"per_page": 100, "page": page, "_fields": _LIST_FIELDS},
            cache_ttl=LIST_TTL,
            allow_status=(400,),
            timeout=60,
        )
        if response.status == 400:  # past the last page
            return []
        payload = response.json()
        return [c for c in payload or [] if isinstance(c, dict)] if isinstance(payload, list) else []

    async def _all_collections(self) -> list[dict[str, Any]]:
        if self._collections is not None and time.monotonic() - self._collections_at < LIST_TTL:
            return self._collections
        pages = list(await asyncio.gather(*(self._page(n) for n in (1, 2, 3))))
        n = 3
        while pages[-1] and len(pages[-1]) >= 100 and n < 20:
            n += 1
            pages.append(await self._page(n))
        items = [c for page in pages for c in page if _short(c)]
        self._collections, self._collections_at = items, time.monotonic()
        return items

    async def _resolve(self, ref: str) -> dict[str, Any] | None:
        text = ref.strip()
        items = await self._all_collections()
        m = re.search(r"10\.7937/[^\s?#]+", text, re.IGNORECASE)
        if m:
            doi = normalize_doi(m.group(0)).rstrip("/")
            return next((c for c in items if normalize_doi(c.get("collection_doi") or "") == doi), None)
        m = re.search(r"cancerimagingarchive\.net/collection/([^/?#]+)", text, re.IGNORECASE)
        key = (m.group(1) if m else text).strip().lower()
        squashed = re.sub(r"[^a-z0-9]", "", key)
        for c in items:
            if _short(c).lower() == key or (c.get("slug") or "").lower() == key:
                return c
        return next((c for c in items if re.sub(r"[^a-z0-9]", "", _short(c).lower()) == squashed), None)

    @tool(
        description="""
        Search TCIA cancer imaging collections (radiology CT/MR/PET/mammography and digital
        pathology cohorts: TCGA-*, CPTAC-*, LIDC-IDRI, NLST, clinical trial imaging, radiomics,
        segmentation challenges). Ranked keyword match over collection name, title, cancer types and
        locations, modalities (MRI~MR, PET~PT, mammography~MG, pathology~Histopathology), program,
        supporting data and summary. Each record has the collection DOI, cancer types, modalities,
        subject count, status and ACCESS (Public vs Limited). Optional filters: modality, cancer_type,
        access. Use '*' as query to list all (most recently updated first). Example: 'lung CT
        radiomics' or 'glioblastoma MRI'. Then use get_collection for license/counts/citations and
        series_for_collection / studies_for_collection for DICOM-level metadata.
        """,
        federated=True,
    )
    async def search_collections(self, params: SearchCollectionsParams) -> ToolResult:
        items = await self._all_collections()
        if params.modality:
            want = params.modality.strip().lower()
            variants = {want, *_ALIASES.get(want, ())}
            items = [c for c in items if variants & {d.lower() for d in _list(c.get("data_types"))}]
        if params.cancer_type:
            want = params.cancer_type.strip().lower()
            items = [
                c
                for c in items
                if want in " | ".join(_list(c.get("cancer_types")) + _list(c.get("cancer_locations"))).lower()
            ]
        if params.access:
            items = [c for c in items if (_access(c) or "").lower() == params.access]

        query = params.query.strip().lower()
        notes: list[str] = []
        if query in ("", "*"):
            ranked = sorted(items, key=lambda c: (c.get("date_updated") or "", _short(c)), reverse=True)
        else:
            terms = [t for t in re.findall(r"[a-z0-9][a-z0-9\-+.]*", query) if len(t) > 1 or t.isdigit()]
            terms = list(dict.fromkeys(terms)) or [query]
            phrase = " ".join(terms)
            scored = []
            for c in items:
                matched, score = _score(c, terms, phrase)
                if matched:
                    scored.append((matched, score, _int(c.get("subjects")) or 0, c))
            full = sum(1 for s in scored if s[0] == len(terms))
            if len(terms) > 1 and full < len(scored):
                notes.append(
                    f"{full} collection(s) match all query terms and are ranked first; the other "
                    f"{len(scored) - full} match only some terms"
                )
            scored.sort(key=lambda s: s[:3], reverse=True)
            ranked = [s[3] for s in scored]

        offset = parse_offset(params.cursor)
        page = ranked[offset : offset + params.limit]
        records = [_collection_record(c) for c in page]
        limited = [r.id for r in records if r.attributes.get("access") == "Limited"]
        if limited:
            notes.append(
                f"limited-access collection(s) {', '.join(limited)}: images require an approved TCIA data access "
                "request; NBIA series/studies tools only see public data"
            )
        nxt = offset + len(page)
        return ToolResult(
            source=self.name,
            tool="search_collections",
            records=records,
            total=len(ranked),
            next_cursor=str(nxt) if page and nxt < len(ranked) else None,
            request_url=f"{WP}/collections/?per_page=100&page=1",
            notes=notes,
        )

    @tool(
        description="""
        Get one TCIA collection in full: title, summary, DOI, cancer types/locations, modalities,
        supporting data (clinical, genomics, pathology), species, subject count, status, ACCESS level
        (Public / Limited), version, and every download package with its license (e.g. CC BY 4.0),
        access, size, file types and subject/study/series/image counts; plus the data citation, citation
        DOIs and related analysis results (derived segmentations/annotations with their own DOIs).
        Accepts the collection short name ('TCGA-BRCA', 'LIDC-IDRI'), website slug/URL or DOI.
        """
    )
    async def get_collection(self, params: GetCollectionParams) -> ToolResult:
        item = await self._resolve(params.collection)
        if item is None:
            return ToolResult(
                source=self.name,
                tool="get_collection",
                notes=[f"no TCIA collection matches {params.collection!r}; try search_collections"],
            )
        response = await self.request(
            "GET", f"{WP}/collections/{int(item['id'])}", params={"_fields": _DETAIL_FIELDS}, allow_status=(404,)
        )
        detail = response.json() if response.status == 200 else None
        full = {**item, **(detail if isinstance(detail, dict) else {})}

        async def fetch(endpoint: str, ids: Any, fields: str | None) -> list[dict[str, Any]]:
            ids = [str(i) for i in ids if str(i).isdigit()] if isinstance(ids, list) else []
            if not ids:
                return []
            q = {"include": ",".join(ids[:50]), "per_page": 100}
            if fields:
                q["_fields"] = fields
            r = await self.request("GET", f"{WP}/{endpoint}/", params=q, allow_status=(400, 404))
            data = r.json() if r.status == 200 else []
            return [d for d in data or [] if isinstance(d, dict)] if isinstance(data, list) else []

        downloads, citations, analyses = await asyncio.gather(
            fetch("downloads", full.get("collection_downloads"), _DOWNLOAD_FIELDS),
            fetch("citations", full.get("citations"), "id,tcia_citation_type,tcia_citation_text,tcia_citation_doi"),
            fetch(
                "analysis-results",
                full.get("related_analysis_results"),
                "id,link,result_short_title,result_title,result_doi,result_page_accessibility,subjects",
            ),
        )
        record = _collection_record(full)
        dl = [_download_entry(d) for d in downloads]
        coll_doi = record.identifiers.get("doi")
        cite_dois = []
        data_citation = None
        for c in citations:
            doi = normalize_doi(_text(c.get("tcia_citation_doi")) or "") or None
            if doi and doi != coll_doi and doi not in cite_dois:
                cite_dois.append(doi)
            if _text(c.get("tcia_citation_type")) == "Data Citation" and not data_citation:
                data_citation = strip_html(c.get("tcia_citation_text"))
        related = [
            _compact(
                {
                    "name": a.get("result_short_title"),
                    "title": strip_html(a.get("result_title")),
                    "doi": normalize_doi(a["result_doi"]) if a.get("result_doi") else None,
                    "access": a.get("result_page_accessibility"),
                    "subjects": _int(a.get("subjects")),
                    "url": a.get("link"),
                }
            )
            for a in analyses
        ]
        licenses = sorted({d["license"] for d in dl if d.get("license")})
        radiology = [d for d in dl if (d.get("type") or "").lower().startswith("radiology")]
        record.attributes.update(
            _compact(
                {
                    "licenses": licenses,
                    "downloads": dl[:20],
                    "image_series": sum(d.get("series") or 0 for d in radiology) or None,
                    "image_studies": sum(d.get("studies") or 0 for d in radiology) or None,
                    "images": sum(d.get("images") or 0 for d in radiology) or None,
                    "data_citation": data_citation,
                    "citation_dois": cite_dois,
                    "related_analysis_results": related[:25],
                    "related_dois": [r["doi"] for r in related if r.get("doi")],
                    "version_change_log": strip_html(full.get("version_change_log")),
                    "usage_notes": (strip_html(full.get("usage_notes")) or "")[:800] or None,
                    "funding": strip_html(full.get("collection_funding")),
                }
            )
        )
        notes = []
        access = record.attributes.get("access")
        gated = [d for d in dl if (d.get("access") or "").lower() not in ("", "public")]
        if access == "Limited" or gated:
            notes.append(
                "LIMITED ACCESS: some or all data in this collection require an approved TCIA data access request "
                "(often with a signed data use agreement); NBIA series/studies tools only list public data"
            )
        if not dl:
            notes.append("no download packages listed for this collection")
        return ToolResult(
            source=self.name,
            tool="get_collection",
            records=[record],
            total=1,
            request_url=response.url,
            notes=notes,
        )

    async def _nbia(self, endpoint: str, query: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        response = await self.request("GET", f"{NBIA}/{endpoint}", params=query, cache_ttl=NBIA_TTL, timeout=90)
        if not response.content.strip():  # NBIA answers "nothing visible" with an empty 200
            return [], response.url
        payload = response.json()
        return [x for x in payload or [] if isinstance(x, dict)] if isinstance(payload, list) else [], response.url

    async def _empty_note(self, collection: str) -> str:
        item = await self._resolve(collection)
        if item is None:
            return f"no public NBIA data for {collection!r}: collection name not recognised (use the short name from search_collections)"
        if (_access(item) or "") == "Limited":
            return f"{_short(item)} is a limited-access collection: anonymous NBIA queries return no series/studies"
        return f"no matching public DICOM data in {_short(item)} for these filters"

    @tool(
        description="""
        List DICOM image series of a TCIA collection from the NBIA API (public data only): one record per
        series with SeriesInstanceUID, modality, body part, series description, patient ID, study UID,
        image count, size, scanner manufacturer/model, license, series date and an OHIF viewer URL.
        ToolResult.data summarises the WHOLE filtered set (series/patient/study/image counts, modality
        and body-part breakdown). Filter by modality ('MR', 'CT', 'PT', 'SEG'), patient_id, study_uid,
        body_part; always filter large collections (NLST, CBIS-DDSM) by patient_id or modality.
        """
    )
    async def series_for_collection(self, params: SeriesForCollectionParams) -> ToolResult:
        query = {
            "Collection": params.collection.strip(),
            "Modality": params.modality,
            "PatientID": params.patient_id,
            "StudyInstanceUID": params.study_uid,
            "BodyPartExamined": params.body_part,
        }
        rows, url = await self._nbia("getSeries", query)
        rows.sort(key=lambda s: (s.get("PatientID") or "", s.get("StudyInstanceUID") or "", s.get("SeriesNumber") or 0))
        offset = parse_offset(params.cursor)
        page = rows[offset : offset + params.limit]
        notes = [] if rows else [await self._empty_note(params.collection)]
        summary = None
        if rows:
            summary = {
                "series": len(rows),
                "patients": len({s.get("PatientID") for s in rows}),
                "studies": len({s.get("StudyInstanceUID") for s in rows}),
                "images": sum(s.get("ImageCount") or 0 for s in rows if isinstance(s.get("ImageCount"), int)),
                "size_gb": round(sum(s.get("FileSize") or 0 for s in rows if isinstance(s.get("FileSize"), (int, float))) / 1e9, 2),
                "modalities": dict(Counter(s.get("Modality") or "?" for s in rows).most_common()),
                "body_parts": dict(Counter(s.get("BodyPartExamined") or "unspecified" for s in rows).most_common(10)),
            }
        nxt = offset + len(page)
        return ToolResult(
            source=self.name,
            tool="series_for_collection",
            records=[_series_record(s) for s in page],
            total=len(rows),
            next_cursor=str(nxt) if page and nxt < len(rows) else None,
            data=summary,
            request_url=url,
            notes=notes,
        )

    @tool(
        description="""
        List imaging studies (DICOM StudyInstanceUID) of a TCIA collection from the NBIA API (public data
        only): study description, date, patient ID, sex, age and series count per study. ToolResult.data
        summarises the whole set (patients, studies, series, sex breakdown). Use to see cohort size and
        per-subject imaging timepoints; filter by patient_id for one subject. For series-level detail use
        series_for_collection.
        """
    )
    async def studies_for_collection(self, params: StudiesForCollectionParams) -> ToolResult:
        rows, url = await self._nbia(
            "getPatientStudy", {"Collection": params.collection.strip(), "PatientID": params.patient_id}
        )
        rows.sort(key=lambda s: (s.get("PatientID") or "", s.get("StudyDate") or ""))
        offset = parse_offset(params.cursor)
        page = rows[offset : offset + params.limit]
        notes = [] if rows else [await self._empty_note(params.collection)]
        summary = None
        if rows:
            per_patient: dict[str, str] = {}
            for s in rows:
                per_patient.setdefault(s.get("PatientID") or "?", s.get("PatientSex") or "unknown")
            summary = {
                "patients": len(per_patient),
                "studies": len(rows),
                "series": sum(s.get("SeriesCount") or 0 for s in rows if isinstance(s.get("SeriesCount"), int)),
                "patient_sex": dict(Counter(per_patient.values()).most_common()),
            }
        nxt = offset + len(page)
        return ToolResult(
            source=self.name,
            tool="studies_for_collection",
            records=[_study_record(s) for s in page],
            total=len(rows),
            next_cursor=str(nxt) if page and nxt < len(rows) else None,
            data=summary,
            request_url=url,
            notes=notes,
        )
