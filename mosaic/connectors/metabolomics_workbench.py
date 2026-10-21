"""Metabolomics Workbench (NIH Common Fund National Metabolomics Data Repository) connector.

Docs: https://www.metabolomicsworkbench.org/tools/MWRestAPIv1.0.pdf
REST pattern: https://www.metabolomicsworkbench.org/rest/{context}/{input item}/{input value}/{output item}
Auth: none. Rate limits: none documented; we stay at 3 req/s.

Quirks (verified live)
- Responses are JSON but shaped by hit count: ``[]`` for none, a flat object for one hit, and an
  object keyed "1","2",... (or "Row1"...) for several. ``_rows`` normalizes all three.
- ``study/study_id/{x}`` is a *prefix* match (ST0000 returns ST000001..ST000099), so get_study
  filters to the exact id. ``study/study_title/{term}`` is a case-insensitive substring match on
  titles only, newest first; multi-word queries that miss are retried per word and ranked by the
  number of words matched.
- The REST study summary has no abstract or DOI. Those (Project DOI, Study Summary, project title,
  publications, sample type) come from the public study landing page on the same host
  (DRCCMetadata.php), parsed defensively; failures only add a note.
- ``compound`` cannot be queried by name; names are resolved through RefMet (``refmet/match``,
  ``refmet/name``) first. Unknown inputs return ``[]`` (refmet/match returns "-" fields).
- ``study/refmet_name/{RefMet name}/summary`` lists study ids containing a metabolite (exact RefMet name).
- Never request ``mwtab``/``data`` outputs here: they can be several MB.

Identifier keys: ``metabolomics_workbench`` (ST######), ``pubchem_cid``, ``inchikey``, ``smiles``, plus non-canonical ``kegg``, ``hmdb``,
``chebi``, ``lipidmaps``, ``refmet`` (RM#######), ``mw_regno`` (Workbench structure registry number).
Publication DOIs / PMIDs of a study are kept in attributes (they identify papers, not the study), and so
is the project DOI (``attributes['project_doi']``, 10.21228/M8...): all studies of a project share it, so
as a ``doi`` identifier it would merge sibling studies into one entity.
"""

from __future__ import annotations

import asyncio
import html
import re
from collections import Counter
from typing import Any
from urllib.parse import quote

from pydantic import Field

from ..http import SourceError
from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, strip_html, tool

REST = "https://www.metabolomicsworkbench.org/rest"
STUDY_PAGE = "https://www.metabolomicsworkbench.org/data/DRCCMetadata.php"
STRUCTURE_PAGE = "https://www.metabolomicsworkbench.org/data/StructureData.php"

_STUDY_RE = re.compile(r"^ST\d{6}$", re.IGNORECASE)
_INCHIKEY_RE = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$", re.IGNORECASE)
_FORMULA_RE = re.compile(r"^(?:[A-Z][a-z]?\d*){2,}$")
_STOP = {
    "and", "the", "of", "in", "for", "with", "from", "to", "on", "by", "a", "an", "study", "studies", "analysis",
    "metabolomics", "metabolomic", "metabolome", "metabolite", "metabolites", "profiling", "profile", "untargeted",
    "targeted", "data", "lipidomics", "samples", "sample", "human", "effect", "effects",
}

_ID_TYPES = ("auto", "name", "pubchem_cid", "inchi_key", "regno", "hmdb_id", "kegg_id", "lm_id", "formula")


class SearchStudiesParams(SearchParams):
    limit: int = Field(10, ge=1, le=100, description="Maximum number of studies to return")


class GetStudyParams(ToolParams):
    study_id: str = Field(description="Metabolomics Workbench study id, e.g. 'ST001000'")
    include_page: bool = Field(
        True, description="Also parse the public study page for abstract, project DOI and publications"
    )


class CompoundLookupParams(ToolParams):
    identifier: str = Field(
        description="Compound name, PubChem CID, InChIKey, Workbench regno, HMDB id (HMDB0000122), "
        "KEGG id (C00031), LIPID MAPS id (LMFA...) or molecular formula (C6H12O6)"
    )
    id_type: str = Field(
        "auto",
        description="How to interpret identifier: " + ", ".join(_ID_TYPES) + ". 'auto' detects InChIKey, "
        "HMDB, KEGG, LIPID MAPS, formula and digits (=PubChem CID); anything else is treated as a name.",
    )


class RefmetLookupParams(ToolParams):
    names: list[str] = Field(
        min_length=1,
        max_length=20,
        description="Metabolite names as written in papers/data files (e.g. ['d-glucose', 'Tyr', 'C16:0', "
        "'PC 34:1']); each is mapped to its standardized RefMet name and classification",
    )


class StudiesForCompoundParams(ToolParams):
    compound: str = Field(description="Metabolite name (any synonym; resolved to its RefMet name), e.g. 'cholesterol'")
    limit: int = Field(10, ge=1, le=25, description="Maximum number of studies to return (each needs a summary call)")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous result)")


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Normalize MW's [], {flat}, {"1": {...}, "2": {...}} response shapes to a list of dicts."""
    if not payload:
        return []
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        values = list(payload.values())
        if values and all(isinstance(v, dict) for v in values):
            return values
        return [payload]
    return []


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return None if value in ("", "-", "NA", "N/A", "None") else value
    return value


def _compact(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v not in (None, [], "", {}, "-")}


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def study_url(study_id: str) -> str:
    return f"{STUDY_PAGE}?Mode=Study&StudyID={study_id}"


def _study_record(row: dict[str, Any]) -> Record:
    sid = str(row.get("study_id") or "").upper()
    return Record(
        source="metabolomics_workbench",
        id=sid,
        type="study",
        title=_clean(row.get("study_title")),
        url=study_url(sid),
        identifiers={"metabolomics_workbench": sid},
        date=_clean(row.get("release_date")) or _clean(row.get("submission_date")),
        attributes=_compact(
            {
                "species": _clean(row.get("species")),
                "institute": _clean(row.get("institute")),
                "analysis_type": _clean(row.get("analysis_type")),
                "number_of_samples": _int(row.get("number_of_samples")),
                "submission_date": _clean(row.get("submission_date")),
                "release_date": _clean(row.get("release_date")),
                "version": _clean(row.get("version")),
                "license": _clean(row.get("license")),
            }
        ),
    )


def parse_study_page(page: str) -> dict[str, str]:
    """Map row labels ('Project DOI', 'Study Summary', ...) to cleaned cell text (first occurrence wins)."""
    fields: dict[str, str] = {}
    for m in re.finditer(r"<b>([^<]{1,60}?):?</b>\s*</td>\s*<td[^>]*>(.*?)</td>", page, re.IGNORECASE | re.DOTALL):
        label = html.unescape(m.group(1)).strip().rstrip(":").strip()
        raw = m.group(2)
        if label == "Publications":
            fields.setdefault("Publications_raw", raw)
        value = strip_html(raw)
        if label and value and label not in fields:
            fields[label] = value
    return fields


def _publication_links(raw: str | None) -> tuple[list[str], list[str]]:
    dois: list[str] = []
    pmids: list[str] = []
    for m in re.finditer(r"10\.\d{4,9}/[^\s<>\"';,]+", raw or ""):
        doi = normalize_doi(m.group(0).rstrip(".)"))
        if doi not in dois and not doi.startswith("10.21228/"):
            dois.append(doi)
    for m in re.finditer(r"pubmed(?:\.ncbi\.nlm\.nih\.gov)?/(\d{5,9})|PMID:?\s*(\d{5,9})", raw or "", re.IGNORECASE):
        pmid = m.group(1) or m.group(2)
        if pmid not in pmids:
            pmids.append(pmid)
    return dois, pmids


def _factor_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter] = {}
    for row in rows:
        for part in str(row.get("factors") or "").split("|"):
            if ":" not in part:
                continue
            name, value = part.split(":", 1)
            name, value = name.strip(), value.strip()
            if name:
                counts.setdefault(name, Counter())[value] += 1
    return {name: dict(c.most_common(12)) for name, c in list(counts.items())[:10]}


def _compound_record(row: dict[str, Any], *, refmet: dict[str, Any] | None = None) -> Record:
    refmet = refmet or {}
    regno = _clean(row.get("regno")) or _clean(refmet.get("regno"))
    if regno is not None and (_int(regno) or 0) <= 0:
        regno = None
    name = _clean(row.get("name")) or _clean(refmet.get("name")) or _clean(refmet.get("refmet_name"))
    lm_id = _clean(row.get("lm_id"))
    chebi = _clean(row.get("chebi_id"))
    return Record(
        source="metabolomics_workbench",
        id=str(regno or refmet.get("refmet_id") or name),
        type="compound",
        title=name,
        url=f"{STRUCTURE_PAGE}?RegNo={regno}" if regno else None,
        identifiers={
            "pubchem_cid": _clean(row.get("pubchem_cid")) or _clean(refmet.get("pubchem_cid")),
            "inchikey": _clean(row.get("inchi_key")) or _clean(refmet.get("inchi_key")),
            "smiles": _clean(row.get("smiles")) or _clean(refmet.get("smiles")),
            "kegg": _clean(row.get("kegg_id")),
            "hmdb": _clean(row.get("hmdb_id")),
            "chebi": f"CHEBI:{chebi}" if chebi and str(chebi).isdigit() else chebi,
            "lipidmaps": lm_id,
            "refmet": _clean(refmet.get("refmet_id")),
            "mw_regno": regno,
        },
        attributes=_compact(
            {
                "formula": _clean(row.get("formula")) or _clean(refmet.get("formula")),
                "exact_mass": _clean(row.get("exactmass")) or _clean(refmet.get("exactmass")),
                "systematic_name": _clean(row.get("sys_name")),
                "refmet_name": _clean(refmet.get("name")) or _clean(refmet.get("refmet_name")),
                "super_class": _clean(refmet.get("super_class")),
                "main_class": _clean(refmet.get("main_class")),
                "sub_class": _clean(refmet.get("sub_class")),
                "metacyc": _clean(row.get("metacyc_id")),
            }
        ),
    )


class MetabolomicsWorkbench(Connector):
    name = "metabolomics_workbench"
    title = "Metabolomics Workbench"
    category = "datasets"
    description = (
        "NIH National Metabolomics Data Repository: ~4,000 MS/NMR metabolomics studies (species, sample "
        "types, design factors, analyses, project DOIs) plus the RefMet standardized metabolite nomenclature "
        "and a structure database with PubChem/InChIKey/KEGG/HMDB cross-references."
    )
    homepage = "https://www.metabolomicsworkbench.org/"
    docs_url = "https://www.metabolomicsworkbench.org/tools/MWRestAPIv1.0.pdf"
    auth = "none"
    rate_limit = 3.0
    health_query = "diabetes"
    nih_repositories = ("Metabolomics Workbench - National Metabolomics Data Repository",)

    async def _get(self, path: str) -> tuple[Any, str]:
        response = await self.request("GET", f"{REST}/{path}")
        text = response.text.strip()
        if not text or text.startswith(("This ", "<")):
            # MW answers unknown inputs/outputs with an HTML/plain-text message and HTTP 200
            return [], response.url
        try:
            return response.json(), response.url
        except ValueError:
            return [], response.url

    async def _title_search(self, term: str) -> tuple[list[dict[str, Any]], str]:
        payload, url = await self._get(f"study/study_title/{quote(term, safe='')}/summary")
        return _rows(payload), url

    @tool(
        description="""
        Search Metabolomics Workbench studies by words in the study title (case-insensitive substring;
        multi-word queries that match nothing as a phrase are retried word by word and ranked by how
        many words match). Returns study records (ST######) with title, species, institute, analysis
        type (LC-MS / GC-MS / NMR), number of samples and release date, newest first.
        Use for metabolomics dataset discovery by disease, tissue, organism or exposure, e.g.
        'breast cancer', 'serum diabetes'. Use get_study for abstract, design factors and DOI, and
        studies_for_compound to find studies that measured a given metabolite.
        """,
        federated=True,
    )
    async def search_studies(self, params: SearchStudiesParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        query = re.sub(r"\s+", " ", params.query.replace("/", " ")).strip()
        rows, url = await self._title_search(query)
        notes: list[str] = []
        if not rows:
            words = [w for w in re.findall(r"[\w\-]+", query) if len(w) >= 3 and w.lower() not in _STOP]
            words = sorted(dict.fromkeys(words), key=len, reverse=True)[:4]
            if len(words) > 1:
                matches: dict[str, dict[str, Any]] = {}
                score: Counter = Counter()
                for word in words:
                    hits, _ = await self._title_search(word)
                    for row in hits:
                        sid = row.get("study_id")
                        if sid:
                            matches[sid] = row
                            score[sid] += 1
                # most words matched first; within a score, newest (highest ST id) first
                rows = list(matches.values())
                rows.sort(key=lambda r: (score[r["study_id"]], r["study_id"]), reverse=True)
                if rows:
                    best = max(score.values())
                    if best > 1:
                        rows = [r for r in rows if score[r["study_id"]] == best]
                    notes.append(
                        f"no title contains the exact phrase; ranked studies by words matched "
                        f"(best: {best}/{len(words)} of {', '.join(words)})"
                    )
        rows = [r for r in rows if r.get("study_id")]
        page = rows[offset : offset + params.limit]
        nxt = offset + len(page)
        return ToolResult(
            source=self.name,
            tool="search_studies",
            records=[_study_record(r) for r in page],
            total=len(rows),
            next_cursor=str(nxt) if page and nxt < len(rows) else None,
            request_url=url,
            notes=notes,
        )

    @tool(
        description="""
        Get one Metabolomics Workbench study (ST######): title, species, institute, analysis type,
        sample count, submit/release dates, license; experimental design factors with sample counts
        per level; analyses (MS/NMR instrument, ion mode, chromatography) with number of metabolites;
        and from the study page: abstract (study summary), project title, project DOI, sample type,
        subject type and publications (paper DOIs / PMIDs).
        """
    )
    async def get_study(self, params: GetStudyParams) -> ToolResult:
        sid = params.study_id.strip().upper()
        if not _STUDY_RE.match(sid):
            return ToolResult(
                source=self.name, tool="get_study", notes=[f"{params.study_id!r} is not a study id; expected ST followed by 6 digits"]
            )
        payload, url = await self._get(f"study/study_id/{sid}/summary")
        rows = [r for r in _rows(payload) if str(r.get("study_id", "")).upper() == sid]
        if not rows:
            return ToolResult(source=self.name, tool="get_study", notes=[f"study {sid} not found"], request_url=url)
        record = _study_record(rows[0])
        notes: list[str] = []

        factors_payload, analysis_payload, counts_payload = await asyncio.gather(
            self._get(f"study/study_id/{sid}/factors"),
            self._get(f"study/study_id/{sid}/analysis"),
            self._get(f"study/study_id/{sid}/number_of_metabolites"),
        )
        factor_rows = _rows(factors_payload[0])
        if factor_rows:
            record.attributes["design_factors"] = _factor_summary(factor_rows)
            record.attributes["sample_sources"] = sorted(
                {str(r.get("sample_source")) for r in factor_rows if _clean(r.get("sample_source"))}
            )
        counts = {r.get("analysis_id"): r for r in _rows(counts_payload[0])}
        analyses = []
        for a in _rows(analysis_payload[0]):
            aid = a.get("analysis_id")
            analyses.append(
                _compact(
                    {
                        "analysis_id": aid,
                        "summary": _clean(a.get("analysis_summary")),
                        "type": _clean(a.get("analysis_type")),
                        "chromatography": _clean(a.get("chromatography_type")),
                        "instrument": _clean(a.get("ms_instrument_name")) or _clean(a.get("nmr_instrument_type")),
                        "instrument_type": _clean(a.get("ms_instrument_type")),
                        "ion_mode": _clean(a.get("ion_mode")),
                        "units": _clean(a.get("units")),
                        "num_metabolites": _int((counts.get(aid) or {}).get("num_metabolites")),
                    }
                )
            )
        if analyses:
            record.attributes["analyses"] = analyses[:20]

        if params.include_page:
            try:
                response = await self.request(
                    "GET",
                    STUDY_PAGE,
                    params={"Mode": "Study", "StudyID": sid},
                    headers={"Accept": "text/html"},
                )
                fields = parse_study_page(response.text)
            except SourceError as exc:
                fields = {}
                notes.append(f"study page unavailable ({exc.status or 'error'}); abstract/DOI omitted")
            doi_text = fields.get("Project DOI") or ""
            doi_match = re.search(r"10\.\d{4,9}/\S+", doi_text)
            if doi_match:
                record.attributes["project_doi"] = normalize_doi(doi_match.group(0))
            record.summary = fields.get("Study Summary") or record.summary
            pub_dois, pmids = _publication_links(fields.get("Publications_raw"))
            authors = [a.strip() for a in re.split(r",\s*", fields.get("Contributors") or "") if a.strip()]
            if not authors and fields.get("Last Name"):
                authors = [" ".join(x for x in (fields.get("First Name"), fields.get("Last Name")) if x)]
            record.authors = authors[:50]
            record.attributes.update(
                _compact(
                    {
                        "study_type": fields.get("Study Type"),
                        "project_id": fields.get("Project ID"),
                        "project_title": fields.get("Project Title"),
                        "project_summary": (fields.get("Project Summary") or "")[:1500] or None,
                        "subject_type": fields.get("Subject Type"),
                        "taxonomy_id": fields.get("Taxonomy ID"),
                        "sample_type": fields.get("Sample Type"),
                        "num_groups": _int(fields.get("Num Groups")),
                        "total_subjects": _int(fields.get("Total Subjects")),
                        "raw_data_available": fields.get("Raw Data Available"),
                        "raw_data_file_types": fields.get("Raw Data File Type(s)"),
                        "publications": (fields.get("Publications") or "")[:800] or None,
                        "publication_dois": pub_dois,
                        "publication_pmids": pmids,
                    }
                )
            )
        return ToolResult(source=self.name, tool="get_study", records=[record], total=1, request_url=url, notes=notes)

    async def _refmet(self, name: str) -> dict[str, Any] | None:
        """Resolve a free-form metabolite name to its RefMet entry (with ids), or None."""
        payload, _ = await self._get(f"refmet/name/{quote(name, safe='')}/all")
        rows = _rows(payload)
        if not rows:
            match, _ = await self._get(f"refmet/match/{quote(name, safe='')}")
            matched = (_rows(match) or [{}])[0]
            refmet_name = _clean(matched.get("refmet_name"))
            if not refmet_name:
                return None
            payload, _ = await self._get(f"refmet/name/{quote(refmet_name, safe='')}/all")
            rows = _rows(payload) or [{**matched, "name": refmet_name}]
        return rows[0]

    @tool(
        description="""
        Look up a metabolite in the Metabolomics Workbench structure database and return its
        cross-references: PubChem CID, InChIKey, SMILES, KEGG, HMDB, ChEBI, LIPID MAPS, formula, exact
        mass, systematic name, and RefMet classification. Input may be a name (resolved via RefMet),
        PubChem CID, InChIKey, HMDB id, KEGG id, LIPID MAPS id, Workbench regno or a formula (returns
        all isomers). Use to map metabolite names/ids across databases (then search PubChem/ChEBI).
        """
    )
    async def compound_lookup(self, params: CompoundLookupParams) -> ToolResult:
        ident = params.identifier.strip()
        kind = params.id_type if params.id_type in _ID_TYPES else "auto"
        if kind == "auto":
            if _INCHIKEY_RE.match(ident):
                kind = "inchi_key"
            elif re.fullmatch(r"HMDB\d+", ident, re.IGNORECASE):
                kind = "hmdb_id"
            elif re.fullmatch(r"C\d{5}", ident):
                kind = "kegg_id"
            elif re.fullmatch(r"LM[A-Z]{2}\d+", ident, re.IGNORECASE):
                kind = "lm_id"
            elif ident.isdigit():
                kind = "pubchem_cid"
            elif _FORMULA_RE.match(ident) and any(c.isdigit() for c in ident):
                kind = "formula"
            else:
                kind = "name"
        notes: list[str] = []
        refmet = None
        if kind == "name":
            refmet = await self._refmet(ident)
            if not refmet:
                return ToolResult(
                    source=self.name, tool="compound_lookup", notes=[f"no RefMet/Workbench compound matches {ident!r}"]
                )
            regno = _clean(refmet.get("regno"))
            if not regno:
                return ToolResult(
                    source=self.name,
                    tool="compound_lookup",
                    records=[_compound_record({}, refmet=refmet)],
                    total=1,
                    notes=["RefMet entry has no structure registry number; showing RefMet data only"],
                )
            kind, value = "regno", regno
            notes.append(f"name {ident!r} resolved via RefMet to {refmet.get('name') or refmet.get('refmet_name')!r}")
        else:
            value = ident.upper() if kind in ("inchi_key", "hmdb_id", "lm_id") else ident
        payload, url = await self._get(f"compound/{kind}/{quote(value, safe='')}/all")
        rows = _rows(payload)
        if not rows:
            return ToolResult(
                source=self.name,
                tool="compound_lookup",
                notes=notes + [f"no Workbench compound with {kind} {value!r}"],
                request_url=url,
            )
        return ToolResult(
            source=self.name,
            tool="compound_lookup",
            records=[_compound_record(r, refmet=refmet) for r in rows[:100]],
            total=len(rows),
            request_url=url,
            notes=notes,
        )

    @tool(
        description="""
        Standardize metabolite names with RefMet (the Metabolomics Workbench reference nomenclature):
        maps each input name (synonyms, abbreviations, lipid shorthand such as 'Tyr', 'd-glucose',
        'PC 34:1', 'C16:0') to its RefMet name, RefMet id, formula, exact mass, super/main/sub class,
        and PubChem CID / InChIKey when available. Use before comparing metabolite lists across studies
        or databases. Unmatched names are reported in notes.
        """
    )
    async def refmet_lookup(self, params: RefmetLookupParams) -> ToolResult:
        records: list[Record] = []
        unmatched: list[str] = []
        for name in params.names:
            name = name.strip()
            if not name:
                continue
            match, _ = await self._get(f"refmet/match/{quote(name, safe='')}")
            matched = (_rows(match) or [{}])[0]
            refmet_name = _clean(matched.get("refmet_name"))
            if not refmet_name:
                unmatched.append(name)
                continue
            payload, _ = await self._get(f"refmet/name/{quote(refmet_name, safe='')}/all")
            full = (_rows(payload) or [{}])[0]
            entry = {**matched, **full, "name": refmet_name}
            rec = _compound_record({}, refmet=entry)
            rec.id = _clean(entry.get("refmet_id")) or refmet_name
            rec.url = (
                f"https://www.metabolomicsworkbench.org/databases/refmet/refmet_details.php?REFMET_NAME={quote(refmet_name)}"
            )
            rec.attributes["input_name"] = name
            records.append(rec)
        notes = [f"no RefMet match for: {', '.join(unmatched)}"] if unmatched else []
        return ToolResult(
            source=self.name,
            tool="refmet_lookup",
            records=records,
            total=len(records),
            request_url=f"{REST}/refmet/match/",
            notes=notes,
            data={"mapping": {r.attributes.get("input_name"): r.title for r in records}} if records else None,
        )

    @tool(
        description="""
        Find Metabolomics Workbench studies whose results include a given metabolite (matched by its
        RefMet name; synonyms are resolved first). Returns study records (newest first) with title,
        species, institute, analysis type and sample count. Use for 'which datasets measured
        TMAO / kynurenine / cholesterol?' questions.
        """
    )
    async def studies_for_compound(self, params: StudiesForCompoundParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        match, _ = await self._get(f"refmet/match/{quote(params.compound.strip(), safe='')}")
        matched = (_rows(match) or [{}])[0]
        refmet_name = _clean(matched.get("refmet_name"))
        if not refmet_name:
            return ToolResult(
                source=self.name, tool="studies_for_compound", notes=[f"no RefMet match for {params.compound!r}"]
            )
        payload, url = await self._get(f"study/refmet_name/{quote(refmet_name, safe='')}/summary")
        ids = sorted({str(r.get("study_id")).upper() for r in _rows(payload) if r.get("study_id")}, reverse=True)
        page_ids = ids[offset : offset + params.limit]

        async def summary(sid: str) -> Record:
            try:
                data, _ = await self._get(f"study/study_id/{sid}/summary")
                rows = [r for r in _rows(data) if str(r.get("study_id", "")).upper() == sid]
            except SourceError:
                rows = []
            if rows:
                return _study_record(rows[0])
            return Record(source=self.name, id=sid, type="study", url=study_url(sid), identifiers={"metabolomics_workbench": sid})

        records = list(await asyncio.gather(*(summary(s) for s in page_ids)))
        for rec in records:
            rec.attributes["refmet_name"] = refmet_name
        nxt = offset + len(records)
        notes = []
        if refmet_name.lower() != params.compound.strip().lower():
            notes.append(f"{params.compound!r} resolved via RefMet to {refmet_name!r}")
        return ToolResult(
            source=self.name,
            tool="studies_for_compound",
            records=records,
            total=len(ids),
            next_cursor=str(nxt) if records and nxt < len(ids) else None,
            request_url=url,
            notes=notes,
        )
