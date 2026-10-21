"""Open Targets Platform connector (GraphQL).

Docs: https://platform-docs.opentargets.org/data-access/graphql-api
Endpoint: https://api.platform.opentargets.org/api/v4/graphql (POST {query, variables}); schema browser at
https://api.platform.opentargets.org/api/v4/graphql/browser.

Auth: none. No published hard rate limit; we stay at 5 req/s. GraphQL POSTs are cached like GETs by the
shared client (``HttpClient.graphql``).

Schema notes (verified against the live API v26.9, data release 26.09; field names change between
releases, so every query here is exercised by the live tests):
- ``knownDrugs`` was replaced by ``drugAndClinicalCandidates`` (rows with ``maxClinicalStage`` such as
  APPROVAL / PHASE_3 / PHASE_2_3 / WITHDRAWAL) on Target and Disease; Drug has ``indications`` with the
  same stage strings and ``maximumClinicalStage``.
- The ChEMBL evidence datasource is now ``clinical_precedence`` (datatype ``clinical``).
- Pagination: ``page: {index, size}`` (page number, not offset) for search and associations; evidence
  uses an opaque ``cursor``.
- Disease IDs use underscores (EFO_0000311, MONDO_0005233, Orphanet_558, HP_0001250); we accept
  ``MONDO:0005233`` style and convert.

Identifiers: targets -> ensembl_gene, gene_symbol, uniprot (Swiss-Prot first), hgnc, chembl (target);
diseases -> efo / mondo / hp / orphanet from the ID plus mesh, omim, doid, umls, orphanet, efo from
dbXRefs; drugs -> chembl, drugbank. Association rows are emitted as records of the *counterpart* entity
(disease_associated_targets -> target records, target_associated_diseases -> disease records) with
the scores in ``attributes``; evidence rows carry only the supporting publication / trial identifier,
so relationship records never bridge two entities during cross-database clustering.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field

from ..http import SourceError
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, strip_html, tool

API = "https://api.platform.opentargets.org/api/v4/graphql"
WEB = "https://platform.opentargets.org"

_ENSG = re.compile(r"^ENSG\d{11}(\.\d+)?$", re.IGNORECASE)
_CHEMBL = re.compile(r"^CHEMBL\d+$", re.IGNORECASE)
_DISEASE_ID = re.compile(r"^[A-Za-z]+[_:][A-Za-z0-9]+$")

STAGE_RANK = {
    "APPROVAL": 9,
    "WITHDRAWAL": 8,
    "PHASE_4": 7,
    "PHASE_3": 6,
    "PHASE_2_3": 5,
    "PHASE_2": 4,
    "PHASE_1_2": 3,
    "PHASE_1": 2,
    "EARLY_PHASE_1": 1,
}


# ---------------------------------------------------------------- params
class OTSearchParams(SearchParams):
    entities: list[Literal["target", "disease", "drug"]] = Field(
        default_factory=lambda: ["target", "disease", "drug"],
        description="Entity types to search (default all three)",
    )


class TargetParams(ToolParams):
    target: str = Field(description="Ensembl gene ID (ENSG00000146648) or human gene symbol (EGFR)")
    max_drugs: int = Field(25, ge=0, le=100, description="How many drugs / clinical candidates to list (most advanced first)")


class DiseaseParams(ToolParams):
    disease: str = Field(
        description="Disease ID as used by Open Targets: EFO_0000311, MONDO_0005233 (MONDO:0005233 accepted), "
        "Orphanet_558, HP_...; a plain name is resolved via search"
    )
    max_drugs: int = Field(20, ge=0, le=100, description="How many drugs / clinical candidates to list")


class DiseaseTargetsParams(ToolParams):
    disease: str = Field(description="Disease ID (EFO_/MONDO_/Orphanet_...) or a disease name to resolve")
    limit: int = Field(25, ge=1, le=100, description="Targets per page (ranked by overall association score)")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor of a previous call)")


class TargetDiseasesParams(ToolParams):
    target: str = Field(description="Ensembl gene ID or human gene symbol")
    limit: int = Field(25, ge=1, le=100, description="Diseases per page (ranked by overall association score)")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor of a previous call)")


class DrugParams(ToolParams):
    chembl_id: str = Field(description="ChEMBL molecule ID, e.g. CHEMBL941 (imatinib); a drug name is resolved via search")
    max_indications: int = Field(30, ge=0, le=200, description="How many indications to list (most advanced first)")


class EvidenceParams(ToolParams):
    target: str = Field(description="Ensembl gene ID or human gene symbol")
    disease: str = Field(description="Disease ID (EFO_/MONDO_/...) or name")
    datasources: list[str] | None = Field(
        None,
        description="Restrict to datasource IDs, e.g. ['clinical_precedence'] (drugs/trials), "
        "['gwas_credible_sets'], ['eva'] (ClinVar), ['cancer_gene_census','intogen'], ['europepmc'] (text mining), "
        "['impc'] (mouse), ['reactome']",
    )
    limit: int = Field(20, ge=1, le=100, description="Evidence rows per page")
    cursor: str | None = Field(None, description="Opaque evidence cursor (next_cursor of a previous call)")


# ---------------------------------------------------------------- queries
_SEARCH_Q = """
query Search($q: String!, $entities: [String!], $page: Pagination) {
  search(queryString: $q, entityNames: $entities, page: $page) {
    total
    hits {
      id entity name description category score
      object {
        __typename
        ... on Target { approvedSymbol biotype proteinIds { id source } }
        ... on Disease { dbXRefs therapeuticAreas { name } }
        ... on Drug { drugType maximumClinicalStage crossReferences { source ids } }
      }
    }
  }
}"""

_TARGET_Q = """
query Target($id: String!) {
  target(ensemblId: $id) {
    id approvedSymbol approvedName biotype functionDescriptions
    genomicLocation { chromosome start end strand }
    proteinIds { id source }
    dbXrefs { id source }
    symbolSynonyms { label }
    targetClass { label level }
    subcellularLocations { location }
    tractability { label modality value }
    safetyLiabilities { event eventId datasource literature effects { direction } biosamples { tissueLabel } }
    pathways { pathwayId pathway topLevelTerm }
    drugAndClinicalCandidates { count rows { maxClinicalStage drug { id name drugType } diseases { disease { id name } } } }
  }
}"""

_DISEASE_FIELDS = """
    id name description dbXRefs
    synonyms { relation terms }
    therapeuticAreas { id name }
    parents { id name }
"""

_DISEASE_Q = (
    """
query Disease($id: String!) {
  disease(efoId: $id) {"""
    + _DISEASE_FIELDS
    + """
    drugAndClinicalCandidates { count rows { maxClinicalStage drug { id name drugType } } }
  }
}"""
)

_DISEASE_TARGETS_Q = """
query DiseaseTargets($id: String!, $page: Pagination) {
  disease(efoId: $id) {
    id name
    associatedTargets(page: $page) {
      count
      rows {
        score
        datatypeScores { id score }
        target { id approvedSymbol approvedName biotype proteinIds { id source } }
      }
    }
  }
}"""

_TARGET_DISEASES_Q = """
query TargetDiseases($id: String!, $page: Pagination) {
  target(ensemblId: $id) {
    id approvedSymbol
    associatedDiseases(page: $page) {
      count
      rows {
        score
        datatypeScores { id score }
        disease { id name dbXRefs therapeuticAreas { name } }
      }
    }
  }
}"""

_DRUG_Q = """
query Drug($id: String!) {
  drug(chemblId: $id) {
    id name drugType maximumClinicalStage description
    synonyms { label source }
    tradeNames { label }
    crossReferences { source ids }
    parentMolecule { id name }
    childMolecules { id name }
    drugWarnings { warningType toxicityClass country year description }
    mechanismsOfAction {
      rows { mechanismOfAction actionType targetName targets { id approvedSymbol } references { source ids } }
    }
    indications { count rows { maxClinicalStage disease { id name } } }
    adverseEvents(page: {index: 0, size: 10}) { count criticalValue rows { name meddraCode count logLR } }
  }
}"""

_EVIDENCE_Q = """
query Evidence($ensg: String!, $efo: String!, $ds: [String!], $size: Int, $cursor: String) {
  target(ensemblId: $ensg) {
    id approvedSymbol
    evidences(efoIds: [$efo], datasourceIds: $ds, size: $size, cursor: $cursor) {
      count cursor
      rows {
        id datasourceId datatypeId score
        disease { id name }
        literature pubMedCentralIds publicationYear
        variantRsId variantFunctionalConsequence { label }
        drug { id name }
        clinicalStage clinicalReportId
        studyId confidence diseaseFromSource
        beta oddsRatio pValueMantissa pValueExponent studySampleSize
        textMiningSentences { text section }
      }
    }
  }
  disease(efoId: $efo) {
    id name
    associatedTargets(Bs: [$ensg]) { rows { score datasourceScores { id score } datatypeScores { id score } } }
  }
}"""


# ---------------------------------------------------------------- helpers
def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _disease_key(disease_id: str) -> str:
    """Normalize user input to the Open Targets ID style (MONDO:0005233 -> MONDO_0005233)."""
    v = disease_id.strip()
    if ":" in v and not v.upper().startswith("HTTP"):
        prefix, rest = v.split(":", 1)
        prefix = {"ORPHANET": "Orphanet", "ORPHA": "Orphanet"}.get(prefix.upper(), prefix.upper())
        return f"{prefix}_{rest}"
    return v


def disease_identifiers(disease_id: str | None, xrefs: list[str] | None = None) -> dict[str, str]:
    ids: dict[str, str] = {}

    def put(key: str, value: str) -> None:
        if value and key not in ids:
            ids[key] = value

    if disease_id:
        prefix, _, rest = disease_id.partition("_")
        p = prefix.upper()
        if p == "EFO":
            put("efo", disease_id)
        elif p == "MONDO":
            put("mondo", f"MONDO:{rest}")
        elif p == "HP":
            put("hp", f"HP:{rest}")
        elif p == "ORPHANET":
            put("orphanet", rest)
        elif p == "DOID":
            put("doid", f"DOID:{rest}")
    for x in xrefs or []:
        prefix, _, rest = str(x).partition(":")
        p = prefix.upper()
        if not rest:
            continue
        if p in ("MESH", "MSH") and re.match(r"^[DC]\d+$", rest):
            put("mesh", rest)
        elif p == "OMIM":
            put("omim", rest)
        elif p == "DOID":
            put("doid", f"DOID:{rest}")
        elif p in ("ORPHANET", "ORPHA"):
            put("orphanet", rest)
        elif p == "UMLS":
            put("umls", rest)
        elif p == "EFO":
            put("efo", f"EFO_{rest}")
        elif p == "MONDO":
            put("mondo", f"MONDO:{rest}")
        elif p == "HP":
            put("hp", f"HP:{rest}")
    return ids


def _uniprot(protein_ids: list[dict[str, Any]] | None) -> str | None:
    ids = protein_ids or []
    for source in ("uniprot_swissprot", "uniprot_trembl", "uniprot_obsolete"):
        for p in ids:
            if p.get("source") == source and p.get("id"):
                return p["id"]
    return None


def _target_identifiers(t: dict[str, Any]) -> dict[str, Any]:
    ids: dict[str, Any] = {
        "ensembl_gene": t.get("id"),
        "gene_symbol": t.get("approvedSymbol"),
        "uniprot": _uniprot(t.get("proteinIds")),
    }
    for x in t.get("dbXrefs") or []:
        if x.get("source") == "HGNC" and "hgnc" not in ids:
            ids["hgnc"] = x.get("id")
        elif x.get("source") == "ChEMBL" and "chembl" not in ids:
            ids["chembl"] = x.get("id")
    return ids


def _stage_sorted(rows: list[dict[str, Any]], key: str = "maxClinicalStage") -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: -STAGE_RANK.get(r.get(key) or "", 0))


def _scores(items: list[dict[str, Any]] | None) -> dict[str, float]:
    return {s["id"]: round(float(s["score"]), 4) for s in items or [] if s.get("id") and s.get("score") is not None}


def _readable(text: str | None) -> bool:
    if not text:
        return False
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return ascii_chars / max(len(text), 1) > 0.9


class OpenTargets(Connector):
    name = "opentargets"
    title = "Open Targets Platform"
    category = "proteins"
    description = (
        "Integrated target-disease association platform (EMBL-EBI/Wellcome Sanger): ~63K targets, ~30K "
        "diseases/phenotypes and drugs, with association scores from genetics (GWAS, ClinVar, gene "
        "burden), somatic mutations, drugs/clinical trials, pathways, expression, mouse models and text "
        "mining. Best for target prioritization, disease-gene evidence and drug-target-indication links."
    )
    homepage = "https://platform.opentargets.org/"
    docs_url = "https://platform-docs.opentargets.org/data-access/graphql-api"
    auth = "none"
    rate_limit = 5.0
    health_query = "EGFR"

    # ------------------------------------------------------------ internals
    async def _q(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return await self.graphql(API, query, variables)

    async def _search(self, text: str, entities: list[str], size: int, index: int = 0) -> dict[str, Any]:
        data = await self._q(_SEARCH_Q, {"q": text, "entities": entities, "page": {"index": index, "size": size}})
        return data.get("search") or {}

    async def _resolve_target(self, value: str) -> tuple[str | None, str | None]:
        v = value.strip()
        if _ENSG.match(v):
            return v.upper().split(".")[0], None
        hits = (await self._search(v, ["target"], 5)).get("hits") or []
        exact = next((h for h in hits if (h.get("name") or "").upper() == v.upper()), None)
        hit = exact or (hits[0] if hits else None)
        if not hit:
            return None, f"no Open Targets target matches {v!r}"
        note = None if exact else f"{v!r} resolved to {hit.get('name')} ({hit.get('id')}) by search"
        return hit["id"], note

    async def _resolve_disease(self, value: str) -> tuple[str | None, str | None]:
        v = value.strip()
        if _DISEASE_ID.match(v) and any(ch.isdigit() for ch in v):
            return _disease_key(v), None
        hits = (await self._search(v, ["disease"], 5)).get("hits") or []
        if not hits:
            return None, f"no Open Targets disease matches {v!r}"
        hit = hits[0]
        return hit["id"], f"{v!r} resolved to {hit.get('name')} ({hit.get('id')}) by search"

    async def _resolve_drug(self, value: str) -> tuple[str | None, str | None]:
        v = value.strip()
        if _CHEMBL.match(v):
            return v.upper(), None
        hits = (await self._search(v, ["drug"], 3)).get("hits") or []
        if not hits:
            return None, f"no Open Targets drug matches {v!r}"
        return hits[0]["id"], f"{v!r} resolved to {hits[0].get('name')} ({hits[0].get('id')}) by search"

    def _hit_record(self, h: dict[str, Any]) -> Record:
        entity = h.get("entity")
        obj = h.get("object") or {}
        hid = h.get("id") or ""
        if entity == "target":
            identifiers = {
                "ensembl_gene": hid,
                "gene_symbol": obj.get("approvedSymbol") or h.get("name"),
                "uniprot": _uniprot(obj.get("proteinIds")),
            }
            rtype, url = "target", f"{WEB}/target/{hid}"
        elif entity == "disease":
            identifiers = disease_identifiers(hid, obj.get("dbXRefs"))
            rtype, url = "disease", f"{WEB}/disease/{hid}"
        elif entity == "drug":
            identifiers = {"chembl": hid}
            for x in obj.get("crossReferences") or []:
                if (x.get("source") or "").lower() == "drugbank" and x.get("ids"):
                    identifiers["drugbank"] = x["ids"][0]
            rtype, url = "drug", f"{WEB}/drug/{hid}"
        else:
            identifiers, rtype, url = {}, "other", None
        return Record(
            source="opentargets",
            id=hid,
            type=rtype,
            title=h.get("name"),
            url=url,
            identifiers=identifiers,
            summary=strip_html(h.get("description")),
            attributes=_clean(
                {
                    "entity": entity,
                    "category": h.get("category"),
                    "biotype": obj.get("biotype"),
                    "drug_type": obj.get("drugType"),
                    "max_clinical_stage": obj.get("maximumClinicalStage"),
                    "therapeutic_areas": [t.get("name") for t in obj.get("therapeuticAreas") or []][:5],
                    "search_score": round(h["score"], 2) if isinstance(h.get("score"), (int, float)) else None,
                }
            ),
        )

    # ------------------------------------------------------------ tools
    @tool(
        description="""
        Search the Open Targets Platform for targets (genes), diseases/phenotypes and drugs by name,
        symbol, synonym or ID ('EGFR', 'non-small cell lung carcinoma', 'imatinib', 'MONDO_0005233').
        Returns Ensembl gene IDs + UniProt for targets, EFO/MONDO IDs + MeSH/OMIM xrefs for diseases and
        ChEMBL IDs for drugs, ready for get_target / get_disease / get_drug and the association tools.
        Restrict with entities=['target'] etc.
        """,
        federated=True,
    )
    async def search(self, params: OTSearchParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        index = offset // params.limit
        result = await self._search(params.query, list(params.entities), params.limit, index)
        hits = result.get("hits") or []
        total = result.get("total")
        nxt = (index + 1) * params.limit
        return ToolResult(
            source=self.name,
            tool="search",
            records=[self._hit_record(h) for h in hits],
            total=total,
            next_cursor=str(nxt) if hits and total and nxt < total else None,
            request_url=API,
        )

    @tool(
        description="""
        Target (gene) profile from Open Targets by Ensembl gene ID or human gene symbol: approved
        symbol/name, biotype, function, genomic location, UniProt/HGNC/ChEMBL IDs, target class,
        subcellular location, tractability (small molecule / antibody / PROTAC buckets), safety
        liabilities, Reactome pathways and the drugs / clinical candidates acting on it with their most
        advanced stage. Use for target assessment; use target_associated_diseases for disease links.
        """
    )
    async def get_target(self, params: TargetParams) -> ToolResult:
        ensg, note = await self._resolve_target(params.target)
        notes = [note] if note else []
        if not ensg:
            return ToolResult(source=self.name, tool="get_target", notes=notes, request_url=API)
        t = (await self._q(_TARGET_Q, {"id": ensg})).get("target")
        if not t:
            return ToolResult(source=self.name, tool="get_target", notes=notes + [f"target {ensg} not found"], request_url=API)
        cands = t.get("drugAndClinicalCandidates") or {}
        drugs = []
        for row in _stage_sorted(cands.get("rows") or [])[: params.max_drugs]:
            drug = row.get("drug") or {}
            drugs.append(
                _clean(
                    {
                        "chembl_id": drug.get("id"),
                        "name": drug.get("name"),
                        "type": drug.get("drugType"),
                        "max_stage": row.get("maxClinicalStage"),
                        "diseases": list(
                            dict.fromkeys(d["disease"]["name"] for d in row.get("diseases") or [] if d.get("disease"))
                        )[:5],
                    }
                )
            )
        tract = {}
        for item in t.get("tractability") or []:
            if item.get("value"):
                tract.setdefault(item.get("modality"), []).append(item.get("label"))
        loc = t.get("genomicLocation") or {}
        safety = [
            _clean(
                {
                    "event": s.get("event"),
                    "effects": sorted({e.get("direction") for e in s.get("effects") or [] if e.get("direction")}),
                    "tissues": sorted({b.get("tissueLabel") for b in s.get("biosamples") or [] if b.get("tissueLabel")})[:5],
                    "source": s.get("datasource"),
                    "pmid": s.get("literature"),
                }
            )
            for s in (t.get("safetyLiabilities") or [])[:20]
        ]
        record = Record(
            source=self.name,
            id=t["id"],
            type="target",
            title=f"{t.get('approvedSymbol')} - {t.get('approvedName')}",
            url=f"{WEB}/target/{t['id']}",
            identifiers=_target_identifiers(t),
            summary=" ".join(t.get("functionDescriptions") or []) or None,
            attributes=_clean(
                {
                    "approved_symbol": t.get("approvedSymbol"),
                    "approved_name": t.get("approvedName"),
                    "biotype": t.get("biotype"),
                    "location": f"chr{loc.get('chromosome')}:{loc.get('start')}-{loc.get('end')}" if loc.get("chromosome") else None,
                    "synonyms": [
                        s for s in dict.fromkeys(x.get("label") for x in t.get("symbolSynonyms") or [])
                        if s and s != t.get("approvedSymbol")
                    ][:10],
                    "target_class": sorted({c.get("label") for c in t.get("targetClass") or [] if c.get("label")}),
                    "subcellular_locations": sorted({s.get("location") for s in t.get("subcellularLocations") or [] if s.get("location")})[:8],
                    "tractability": tract,
                    "safety_liabilities": safety,
                    "pathways": [
                        {"id": p.get("pathwayId"), "name": p.get("pathway"), "top_level": p.get("topLevelTerm")}
                        for p in (t.get("pathways") or [])[:15]
                    ],
                    "n_pathways": len(t.get("pathways") or []) or None,
                    "n_drugs_and_candidates": cands.get("count"),
                    "drugs_and_candidates": drugs,
                }
            ),
        )
        return ToolResult(source=self.name, tool="get_target", records=[record], total=1, notes=notes, request_url=API)

    @tool(
        description="""
        Disease/phenotype profile from Open Targets by ID (EFO_0000311, MONDO_0005233, Orphanet_558,
        HP_...; names are resolved via search): name, description, synonyms, cross-references (MeSH,
        OMIM, DOID, UMLS, Orphanet, NCIt...), therapeutic areas, parent terms and the drugs / clinical
        candidates studied for it (most advanced first). Use disease_associated_targets for genes.
        """
    )
    async def get_disease(self, params: DiseaseParams) -> ToolResult:
        efo, note = await self._resolve_disease(params.disease)
        notes = [note] if note else []
        if not efo:
            return ToolResult(source=self.name, tool="get_disease", notes=notes, request_url=API)
        d = (await self._q(_DISEASE_Q, {"id": efo})).get("disease")
        if not d:
            return ToolResult(source=self.name, tool="get_disease", notes=notes + [f"disease {efo} not found"], request_url=API)
        cands = d.get("drugAndClinicalCandidates") or {}
        drugs = [
            _clean({"chembl_id": (r.get("drug") or {}).get("id"), "name": (r.get("drug") or {}).get("name"),
                    "type": (r.get("drug") or {}).get("drugType"), "max_stage": r.get("maxClinicalStage")})
            for r in _stage_sorted(cands.get("rows") or [])[: params.max_drugs]
        ]
        synonyms = []
        for s in d.get("synonyms") or []:
            if s.get("relation") == "hasExactSynonym":
                synonyms.extend(s.get("terms") or [])
        record = Record(
            source=self.name,
            id=d["id"],
            type="disease",
            title=d.get("name"),
            url=f"{WEB}/disease/{d['id']}",
            identifiers=disease_identifiers(d["id"], d.get("dbXRefs")),
            summary=strip_html(d.get("description")),
            attributes=_clean(
                {
                    "synonyms": synonyms[:15],
                    "db_xrefs": (d.get("dbXRefs") or [])[:25],
                    "therapeutic_areas": [t.get("name") for t in d.get("therapeuticAreas") or []],
                    "parents": [{"id": p.get("id"), "name": p.get("name")} for p in d.get("parents") or []][:10],
                    "n_drugs_and_candidates": cands.get("count"),
                    "drugs_and_candidates": drugs,
                }
            ),
        )
        return ToolResult(source=self.name, tool="get_disease", records=[record], total=1, notes=notes, request_url=API)

    @tool(
        description="""
        Targets associated with a disease in Open Targets, ranked by overall association score (0-1), with
        per-datatype scores (genetic_association, somatic_mutation, clinical (known drugs), literature,
        affected_pathway, rna_expression, animal_model). Records are the target genes (Ensembl, symbol,
        UniProt). Give an EFO/MONDO ID (or a name to resolve). Paginate with cursor. Use
        association_evidence to see the evidence behind one target-disease pair.
        """
    )
    async def disease_associated_targets(self, params: DiseaseTargetsParams) -> ToolResult:
        efo, note = await self._resolve_disease(params.disease)
        notes = [note] if note else []
        if not efo:
            return ToolResult(source=self.name, tool="disease_associated_targets", notes=notes, request_url=API)
        offset = parse_offset(params.cursor)
        index = offset // params.limit
        d = (await self._q(_DISEASE_TARGETS_Q, {"id": efo, "page": {"index": index, "size": params.limit}})).get("disease")
        if not d:
            return ToolResult(
                source=self.name, tool="disease_associated_targets", notes=notes + [f"disease {efo} not found"], request_url=API
            )
        assoc = d.get("associatedTargets") or {}
        rows = assoc.get("rows") or []
        records = []
        for rank, row in enumerate(rows, start=index * params.limit + 1):
            t = row.get("target") or {}
            records.append(
                Record(
                    source=self.name,
                    id=t.get("id") or "",
                    type="target",
                    title=f"{t.get('approvedSymbol')} - {t.get('approvedName')}",
                    url=f"{WEB}/evidence/{t.get('id')}/{d['id']}",
                    identifiers={"ensembl_gene": t.get("id"), "gene_symbol": t.get("approvedSymbol"), "uniprot": _uniprot(t.get("proteinIds"))},
                    summary=f"Association with {d.get('name')} ({d['id']}): overall score {row.get('score'):.3f}",
                    attributes=_clean(
                        {
                            "rank": rank,
                            "disease_id": d["id"],
                            "disease_name": d.get("name"),
                            "overall_score": round(float(row.get("score") or 0), 4),
                            "datatype_scores": _scores(row.get("datatypeScores")),
                            "biotype": t.get("biotype"),
                        }
                    ),
                )
            )
        total = assoc.get("count")
        nxt = (index + 1) * params.limit
        return ToolResult(
            source=self.name,
            tool="disease_associated_targets",
            records=records,
            total=total,
            next_cursor=str(nxt) if rows and total and nxt < total else None,
            notes=notes,
            request_url=API,
        )

    @tool(
        description="""
        Diseases associated with a target (Ensembl gene ID or symbol) in Open Targets, ranked by overall
        association score with per-datatype scores. Records are the diseases (EFO/MONDO + MeSH/OMIM
        xrefs) with therapeutic areas. Paginate with cursor. Use for indication discovery / target
        safety context.
        """
    )
    async def target_associated_diseases(self, params: TargetDiseasesParams) -> ToolResult:
        ensg, note = await self._resolve_target(params.target)
        notes = [note] if note else []
        if not ensg:
            return ToolResult(source=self.name, tool="target_associated_diseases", notes=notes, request_url=API)
        offset = parse_offset(params.cursor)
        index = offset // params.limit
        t = (await self._q(_TARGET_DISEASES_Q, {"id": ensg, "page": {"index": index, "size": params.limit}})).get("target")
        if not t:
            return ToolResult(
                source=self.name, tool="target_associated_diseases", notes=notes + [f"target {ensg} not found"], request_url=API
            )
        assoc = t.get("associatedDiseases") or {}
        rows = assoc.get("rows") or []
        records = []
        for rank, row in enumerate(rows, start=index * params.limit + 1):
            dz = row.get("disease") or {}
            records.append(
                Record(
                    source=self.name,
                    id=dz.get("id") or "",
                    type="disease",
                    title=dz.get("name"),
                    url=f"{WEB}/evidence/{t['id']}/{dz.get('id')}",
                    identifiers=disease_identifiers(dz.get("id"), dz.get("dbXRefs")),
                    summary=f"Association with {t.get('approvedSymbol')}: overall score {row.get('score'):.3f}",
                    attributes=_clean(
                        {
                            "rank": rank,
                            "target_id": t["id"],
                            "target_symbol": t.get("approvedSymbol"),
                            "overall_score": round(float(row.get("score") or 0), 4),
                            "datatype_scores": _scores(row.get("datatypeScores")),
                            "therapeutic_areas": [a.get("name") for a in dz.get("therapeuticAreas") or []][:5],
                        }
                    ),
                )
            )
        total = assoc.get("count")
        nxt = (index + 1) * params.limit
        return ToolResult(
            source=self.name,
            tool="target_associated_diseases",
            records=records,
            total=total,
            next_cursor=str(nxt) if rows and total and nxt < total else None,
            notes=notes,
            request_url=API,
        )

    @tool(
        description="""
        Drug profile from Open Targets by ChEMBL ID (CHEMBL941; names resolved via search): drug type,
        maximum clinical stage, description, synonyms/trade names, DrugBank xref, parent/child (salt)
        molecules, mechanisms of action with target Ensembl IDs and symbols, indications with their most
        advanced stage (APPROVAL, PHASE_3, ...), drug warnings (withdrawals, black box) and top FAERS
        adverse events (log-likelihood ratio). Use for drug -> target -> indication reasoning.
        """
    )
    async def get_drug(self, params: DrugParams) -> ToolResult:
        chembl, note = await self._resolve_drug(params.chembl_id)
        notes = [note] if note else []
        if not chembl:
            return ToolResult(source=self.name, tool="get_drug", notes=notes, request_url=API)
        d = (await self._q(_DRUG_Q, {"id": chembl})).get("drug")
        if not d:
            return ToolResult(source=self.name, tool="get_drug", notes=notes + [f"drug {chembl} not found"], request_url=API)
        identifiers: dict[str, Any] = {"chembl": d["id"]}
        xrefs: dict[str, list[str]] = {}
        for x in d.get("crossReferences") or []:
            if x.get("source") and x.get("ids"):
                xrefs[x["source"]] = x["ids"][:5]
                if x["source"].lower() == "drugbank":
                    identifiers["drugbank"] = x["ids"][0]
        mechanisms = []
        for m in (d.get("mechanismsOfAction") or {}).get("rows") or []:
            mechanisms.append(
                _clean(
                    {
                        "mechanism": m.get("mechanismOfAction"),
                        "action_type": m.get("actionType"),
                        "target_name": m.get("targetName"),
                        "targets": [{"ensembl_gene": t.get("id"), "symbol": t.get("approvedSymbol")} for t in m.get("targets") or []],
                    }
                )
            )
        ind = d.get("indications") or {}
        indications = [
            _clean({"disease_id": (r.get("disease") or {}).get("id"), "disease": (r.get("disease") or {}).get("name"), "max_stage": r.get("maxClinicalStage")})
            for r in _stage_sorted(ind.get("rows") or [])[: params.max_indications]
        ]
        ae = d.get("adverseEvents") or {}
        record = Record(
            source=self.name,
            id=d["id"],
            type="drug",
            title=d.get("name"),
            url=f"{WEB}/drug/{d['id']}",
            identifiers=identifiers,
            summary=d.get("description"),
            attributes=_clean(
                {
                    "drug_type": d.get("drugType"),
                    "max_clinical_stage": d.get("maximumClinicalStage"),
                    "synonyms": sorted({s.get("label") for s in d.get("synonyms") or [] if s.get("label") and len(s["label"]) < 40})[:15],
                    "trade_names": sorted({s.get("label") for s in d.get("tradeNames") or [] if s.get("label")})[:10],
                    "cross_references": xrefs,
                    "parent_molecule": (d.get("parentMolecule") or {}).get("id"),
                    "child_molecules": [c.get("id") for c in d.get("childMolecules") or []][:10],
                    "mechanisms_of_action": mechanisms,
                    "linked_targets": sorted({t["symbol"] for m in mechanisms for t in m.get("targets", []) if t.get("symbol")}),
                    "n_indications": ind.get("count"),
                    "indications": indications,
                    "warnings": [
                        _clean({"type": w.get("warningType"), "class": w.get("toxicityClass"), "country": w.get("country"),
                                "year": w.get("year"), "description": w.get("description")})
                        for w in d.get("drugWarnings") or []
                    ][:10],
                    "adverse_events": [
                        {"event": r.get("name"), "reports": r.get("count"), "llr": round(float(r.get("logLR") or 0), 1)}
                        for r in ae.get("rows") or []
                    ],
                    "adverse_events_significant": ae.get("count"),
                }
            ),
        )
        return ToolResult(source=self.name, tool="get_drug", records=[record], total=1, notes=notes, request_url=API)

    @tool(
        description="""
        Evidence behind one target-disease association in Open Targets: the overall and per-datasource
        association scores (in `data`) plus evidence rows (datasource, datatype, score, supporting PMIDs /
        PMCIDs, drug + clinical stage + trial ID for clinical precedence, rsID / consequence / p-value for
        genetics, text-mined sentences for literature). Filter with datasources (e.g.
        ['clinical_precedence'], ['gwas_credible_sets'], ['eva'], ['europepmc']). Records carry the
        supporting publication (pmid) or trial (nct) identifier. Paginate with the opaque cursor.
        """
    )
    async def association_evidence(self, params: EvidenceParams) -> ToolResult:
        ensg, n1 = await self._resolve_target(params.target)
        efo, n2 = await self._resolve_disease(params.disease)
        notes = [n for n in (n1, n2) if n]
        if not ensg or not efo:
            return ToolResult(source=self.name, tool="association_evidence", notes=notes, request_url=API)
        data = await self._q(
            _EVIDENCE_Q,
            {"ensg": ensg, "efo": efo, "ds": params.datasources, "size": params.limit, "cursor": params.cursor},
        )
        t = data.get("target")
        dz = data.get("disease")
        if not t or not dz:
            return ToolResult(
                source=self.name,
                tool="association_evidence",
                notes=notes + [f"target {ensg} or disease {efo} not found"],
                request_url=API,
            )
        ev = t.get("evidences") or {}
        rows = ev.get("rows") or []
        assoc_rows = (dz.get("associatedTargets") or {}).get("rows") or []
        assoc = assoc_rows[0] if assoc_rows else {}
        records = []
        for r in rows:
            pmids = [p for p in r.get("literature") or [] if str(p).isdigit()]
            pmcids = r.get("pubMedCentralIds") or []
            report = r.get("clinicalReportId") or ""
            nct = report.upper() if report.lower().startswith("nct") else None
            if pmids:
                identifiers = {"pmid": pmids[0], "pmcid": pmcids[0] if pmcids else None}
            elif nct:
                identifiers = {"nct": nct}
            else:
                identifiers = {}
            sentence = next((s.get("text") for s in r.get("textMiningSentences") or [] if _readable(s.get("text"))), None)
            drug = r.get("drug") or {}
            pval = (
                f"{r['pValueMantissa']}e{r['pValueExponent']}"
                if r.get("pValueMantissa") is not None and r.get("pValueExponent") is not None
                else None
            )
            label = r.get("datasourceId")
            what = drug.get("name") or r.get("variantRsId") or r.get("diseaseFromSource") or r.get("studyId") or ""
            records.append(
                Record(
                    source=self.name,
                    id=r.get("id") or "",
                    type="association",
                    title=f"{t.get('approvedSymbol')} - {(r.get('disease') or {}).get('name') or dz.get('name')}: {label}" + (f" ({what})" if what else ""),
                    url=f"{WEB}/evidence/{ensg}/{efo}",
                    identifiers=identifiers,
                    date=str(r["publicationYear"]) if r.get("publicationYear") else None,
                    summary=sentence,
                    attributes=_clean(
                        {
                            "datasource": label,
                            "datatype": r.get("datatypeId"),
                            "score": round(float(r.get("score") or 0), 4),
                            "target_id": ensg,
                            "target_symbol": t.get("approvedSymbol"),
                            "disease_id": (r.get("disease") or {}).get("id"),
                            "disease_from_source": r.get("diseaseFromSource"),
                            "pmids": pmids[:10],
                            "pmcids": pmcids[:5],
                            "drug_chembl_id": drug.get("id"),
                            "drug_name": drug.get("name"),
                            "clinical_stage": r.get("clinicalStage"),
                            "clinical_report_id": report or None,
                            "rsid": r.get("variantRsId"),
                            "variant_consequence": (r.get("variantFunctionalConsequence") or {}).get("label"),
                            "study_id": r.get("studyId"),
                            "p_value": pval,
                            "beta": r.get("beta"),
                            "odds_ratio": r.get("oddsRatio"),
                            "sample_size": r.get("studySampleSize"),
                            "confidence": r.get("confidence"),
                        }
                    ),
                )
            )
        summary = _clean(
            {
                "target": {"id": ensg, "symbol": t.get("approvedSymbol")},
                "disease": {"id": efo, "name": dz.get("name")},
                "overall_score": round(float(assoc["score"]), 4) if assoc.get("score") is not None else None,
                "datatype_scores": _scores(assoc.get("datatypeScores")),
                "datasource_scores": _scores(assoc.get("datasourceScores")),
            }
        )
        if not assoc:
            notes.append("no association between this target and disease in the current release")
        return ToolResult(
            source=self.name,
            tool="association_evidence",
            records=records,
            total=ev.get("count"),
            next_cursor=ev.get("cursor") if rows and ev.get("cursor") else None,
            data=summary,
            notes=notes,
            request_url=API,
        )
