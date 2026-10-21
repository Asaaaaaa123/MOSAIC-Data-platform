"""ChEMBL web services connector (bioactive molecules, targets, bioactivities, drug mechanisms).

Docs: https://chembl.gitbook.io/chembl-interface-documentation/web-services
API root: https://www.ebi.ac.uk/chembl/api/data/<resource>.json (Django-tastypie style filters such as
``molecule_structures__standard_inchi_key=``, ``pchembl_value__gte=``, ``target_chembl_id__in=``,
``order_by=-pchembl_value``, ``only=field1,field2`` to trim payloads).

Auth: none. Rate limit: EBI asks for moderate use; we stay at 4 req/s (documented ceiling ~5 req/s).

Quirks:
- ``page_meta`` carries ``total_count``/``offset``/``next``; ``limit`` max is 1000 (we cap at 100).
- Molecule payloads embed a full molfile in ``molecule_structures``; it is never copied into records.
- ``order_by=-pchembl_value`` sorts NULLs first, so we add ``pchembl_value__isnull=false`` when sorting
  by pChEMBL.
- Mechanisms and indications are curated against the *parent* molecule and its salts; we query
  ``parent_molecule_chembl_id`` first and fall back to ``molecule_chembl_id``.
- ChEMBL molecule ``cross_references`` rarely contain PubChem/DrugBank anymore, so ``get_molecule``
  enriches identifiers through EBI UniChem (https://www.ebi.ac.uk/unichem/api/v1/compounds, POST by
  InChIKey): pubchem_cid, drugbank, unii (FDA SRS), drugcentral, bindingdb, chebi (non-canonical key).

Record identifier policy (cross-database clustering is transitive): entity records (molecules, targets)
carry every identifier of that entity. Relationship records (activities, mechanisms, indications) carry
the identifiers of exactly ONE entity, the counterpart of the query (e.g. get_mechanisms by molecule
-> the target's ChEMBL ID/UniProt; by target -> the molecule's ChEMBL ID), and keep the other side and
literature references in ``attributes``, so a relationship never bridges two different entities.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from pydantic import Field

from ..http import SourceError
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, tool

API = "https://www.ebi.ac.uk/chembl/api/data"
UNICHEM = "https://www.ebi.ac.uk/unichem/api/v1/compounds"
WEB = "https://www.ebi.ac.uk/chembl/explore"

_CHEMBL_ID = re.compile(r"^CHEMBL\d+$", re.IGNORECASE)
_INCHIKEY = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$", re.IGNORECASE)

# UniChem source shortName -> Mosaic identifier key
_UNICHEM_KEYS = {
    "pubchem": "pubchem_cid",
    "drugbank": "drugbank",
    "fdasrs": "unii",
    "drugcentral": "drugcentral",
    "bindingdb": "bindingdb",
    "chebi": "chebi",
}


# ---------------------------------------------------------------- params
class SearchMoleculesParams(SearchParams):
    max_phase: int | None = Field(
        None, ge=0, le=4, description="Only molecules at or above this max clinical phase (4 = approved drug)"
    )


class GetMoleculeParams(ToolParams):
    identifier: str = Field(
        description="ChEMBL molecule ID (CHEMBL941), standard InChIKey (KTUFNOKKBVMGRW-UHFFFAOYSA-N), "
        "or an exact preferred name / synonym (imatinib, Gleevec)"
    )
    include_xrefs: bool = Field(
        True, description="Also resolve PubChem CID, DrugBank, UNII, DrugCentral, BindingDB, ChEBI via EBI UniChem"
    )


class SearchTargetsParams(ToolParams):
    query: str = Field(description="Target name, gene symbol or keyword, e.g. 'EGFR', 'dopamine D2 receptor'")
    organism: str | None = Field(None, description="Exact organism filter, e.g. 'Homo sapiens'")
    target_type: str | None = Field(
        None, description="SINGLE PROTEIN, PROTEIN FAMILY, PROTEIN COMPLEX, CELL-LINE, ORGANISM, ..."
    )
    limit: int = Field(10, ge=1, le=100, description="Maximum number of targets to return")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor of a previous call)")


class GetTargetParams(ToolParams):
    identifier: str = Field(
        description="ChEMBL target ID (CHEMBL203) or UniProt accession (P00533); a UniProt accession returns "
        "every ChEMBL target containing that protein (single protein first)"
    )


class GetActivitiesParams(ToolParams):
    molecule_chembl_id: str | None = Field(None, description="Molecule ChEMBL ID, e.g. CHEMBL941")
    target_chembl_id: str | None = Field(None, description="Target ChEMBL ID, e.g. CHEMBL203 (use get_target to map UniProt)")
    standard_type: str | None = Field(None, description="Activity type: IC50, Ki, Kd, EC50, GI50, Potency, Inhibition ...")
    pchembl_min: float | None = Field(
        None, ge=0, le=15, description="Minimum pChEMBL (-log10 molar; 6 = 1 uM, 7 = 100 nM, 9 = 1 nM)"
    )
    assay_type: str | None = Field(None, description="B (binding), F (functional), A (ADME), T (toxicity), P (physchem)")
    sort: str = Field("pchembl", description="'pchembl' (most potent first, rows without pChEMBL dropped) or 'none'")
    limit: int = Field(20, ge=1, le=100, description="Maximum number of activity rows")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor of a previous call)")


class GetMechanismsParams(ToolParams):
    molecule_chembl_id: str | None = Field(None, description="Molecule (parent or salt) ChEMBL ID, e.g. CHEMBL941")
    target_chembl_id: str | None = Field(None, description="Target ChEMBL ID, e.g. CHEMBL1862 (ABL1)")
    limit: int = Field(25, ge=1, le=100, description="Maximum number of mechanisms")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor of a previous call)")


class GetDrugIndicationsParams(ToolParams):
    molecule_chembl_id: str = Field(description="Molecule (parent or salt) ChEMBL ID, e.g. CHEMBL941")
    min_phase: float | None = Field(None, ge=0, le=4, description="Only indications at or above this max phase")
    limit: int = Field(50, ge=1, le=100, description="Maximum number of indications")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor of a previous call)")


# ---------------------------------------------------------------- helpers
def _num(value: Any) -> float | int | None:
    if value in (None, ""):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _norm_chembl(value: str) -> str:
    return value.strip().upper()


def _disease_ids(value: str | None) -> dict[str, str]:
    """ChEMBL ``efo_id`` holds EFO:/MONDO:/HP:/Orphanet: CURIEs; map to canonical keys (EFO_#######, MONDO:#######)."""
    if not value:
        return {}
    v = value.strip()
    prefix = v.split(":", 1)[0].split("_", 1)[0].upper()
    if prefix == "EFO":
        return {"efo": v.replace(":", "_")}
    if prefix == "MONDO":
        return {"mondo": v.replace("_", ":")}
    if prefix == "HP":
        return {"hp": v.replace("_", ":")}
    if prefix == "ORPHANET":
        return {"orphanet": v.split(":", 1)[-1].split("_", 1)[-1]}
    if prefix == "DOID":
        return {"doid": v.replace("_", ":")}
    return {}


def _page(meta: dict[str, Any], offset: int, returned: int) -> tuple[int | None, str | None]:
    total = meta.get("total_count")
    nxt = offset + returned
    more = returned > 0 and (meta.get("next") or (total is not None and nxt < total))
    return total, (str(nxt) if more else None)


def _synonyms(m: dict[str, Any], cap: int = 15) -> list[str]:
    seen: list[str] = []
    for s in m.get("molecule_synonyms") or []:
        name = (s.get("molecule_synonym") or "").strip()
        if name and len(name) <= 60 and name.lower() not in {x.lower() for x in seen}:
            seen.append(name)
    return seen[:cap]


def _molecule_record(m: dict[str, Any], xrefs: dict[str, str] | None = None) -> Record:
    cid = m.get("molecule_chembl_id") or ""
    props = m.get("molecule_properties") or {}
    structs = m.get("molecule_structures") or {}
    phase = _num(m.get("max_phase"))
    hierarchy = m.get("molecule_hierarchy") or {}
    parent = hierarchy.get("parent_chembl_id")
    identifiers: dict[str, Any] = {
        "chembl": cid,
        "inchikey": structs.get("standard_inchi_key"),
        "smiles": structs.get("canonical_smiles"),
        "atc": (m.get("atc_classifications") or [None])[0],
    }
    for key, value in (xrefs or {}).items():
        identifiers.setdefault(key, value)
    other_xrefs = [
        f"{x.get('xref_src')}:{x.get('xref_id')}"
        for x in m.get("cross_references") or []
        if x.get("xref_src") and x.get("xref_id")
    ]
    summary_bits = [m.get("molecule_type")]
    if phase is not None:
        summary_bits.append(f"max phase {phase}" + (f" (first approval {m['first_approval']})" if m.get("first_approval") else ""))
    if m.get("atc_classifications"):
        summary_bits.append("ATC " + ", ".join(m["atc_classifications"][:5]))
    if m.get("usan_stem_definition"):
        summary_bits.append(f"stem {m.get('usan_stem')}: {m['usan_stem_definition']}")
    if props.get("full_molformula"):
        summary_bits.append(f"{props['full_molformula']}, MW {props.get('full_mwt')}")
    return Record(
        source="chembl",
        id=cid,
        type="drug" if phase is not None and phase >= 4 else "compound",
        title=m.get("pref_name") or cid,
        url=f"{WEB}/compound/{cid}" if cid else None,
        identifiers=identifiers,
        date=str(m["first_approval"]) if m.get("first_approval") else None,
        summary="; ".join(str(b) for b in summary_bits if b) or None,
        attributes=_clean(
            {
                "max_phase": phase,
                "first_approval": m.get("first_approval"),
                "molecule_type": m.get("molecule_type"),
                "atc_classifications": m.get("atc_classifications"),
                "parent_chembl_id": parent if parent and parent != cid else None,
                "oral": m.get("oral"),
                "parenteral": m.get("parenteral"),
                "topical": m.get("topical"),
                "black_box_warning": bool(m.get("black_box_warning")) or None,
                "withdrawn": m.get("withdrawn_flag") or None,
                "natural_product": bool(m.get("natural_product")) or None,
                "formula": props.get("full_molformula"),
                "mw": _num(props.get("full_mwt")),
                "mw_freebase": _num(props.get("mw_freebase")),
                "alogp": _num(props.get("alogp")),
                "psa": _num(props.get("psa")),
                "hba": props.get("hba"),
                "hbd": props.get("hbd"),
                "rotatable_bonds": props.get("rtb"),
                "ro5_violations": props.get("num_ro5_violations"),
                "qed": _num(props.get("qed_weighted")),
                "usan_stem": m.get("usan_stem"),
                "synonyms": _synonyms(m),
                "cross_references": other_xrefs[:15],
                "search_score": m.get("score"),
            }
        ),
    )


def _target_record(t: dict[str, Any]) -> Record:
    tid = t.get("target_chembl_id") or ""
    components = t.get("target_components") or []
    accessions = [c.get("accession") for c in components if c.get("accession")]
    single = (t.get("target_type") or "").upper() == "SINGLE PROTEIN" or len(accessions) == 1
    gene_symbol = hgnc = pharmgkb = None
    if components:
        c0 = components[0]
        gene_symbol = next(
            (s.get("component_synonym") for s in c0.get("target_component_synonyms") or [] if s.get("syn_type") == "GENE_SYMBOL"),
            None,
        )
        for x in c0.get("target_component_xrefs") or []:
            if x.get("xref_src_db") == "HGNC" and not hgnc:
                hgnc = x.get("xref_id")
            elif x.get("xref_src_db") == "PharmGKB" and not pharmgkb:
                pharmgkb = x.get("xref_id")
    identifiers: dict[str, Any] = {"chembl": tid}
    if single and accessions:
        # Only single-protein targets are the same entity as a UniProt entry.
        identifiers["uniprot"] = accessions[0]
        if t.get("organism") == "Homo sapiens":
            identifiers.update({"gene_symbol": gene_symbol, "hgnc": hgnc, "pharmgkb": pharmgkb})
    return Record(
        source="chembl",
        id=tid,
        type="target",
        title=t.get("pref_name") or tid,
        url=f"{WEB}/target/{tid}" if tid else None,
        identifiers=identifiers,
        summary=f"{t.get('target_type') or 'target'} in {t.get('organism') or 'unknown organism'}"
        + (f"; components: {', '.join(accessions[:8])}" if accessions else ""),
        attributes=_clean(
            {
                "target_type": t.get("target_type"),
                "organism": t.get("organism"),
                "tax_id": t.get("tax_id"),
                "gene_symbol": gene_symbol,
                "uniprot_accessions": accessions,
                "components": [
                    _clean(
                        {
                            "accession": c.get("accession"),
                            "description": c.get("component_description"),
                            "relationship": c.get("relationship"),
                        }
                    )
                    for c in components[:20]
                ],
                "search_score": t.get("score"),
            }
        ),
    )


class ChEMBL(Connector):
    name = "chembl"
    title = "ChEMBL"
    category = "chemistry"
    description = (
        "EMBL-EBI's curated database of ~2.4M bioactive molecules, 15K+ targets and 20M+ bioactivity "
        "measurements (IC50/Ki/EC50...) from the medicinal-chemistry literature, plus drug mechanisms of "
        "action and indications. Best for compound potency, target pharmacology and drug-target links."
    )
    homepage = "https://www.ebi.ac.uk/chembl/"
    docs_url = "https://chembl.gitbook.io/chembl-interface-documentation/web-services"
    auth = "none"
    rate_limit = 4.0
    health_query = "imatinib"

    # ------------------------------------------------------------ internals
    async def _list(self, resource: str, params: dict[str, Any], allow_404: bool = False):
        response = await self.request(
            "GET", f"{API}/{resource}.json", params=params, allow_status=(404,) if allow_404 else ()
        )
        payload = response.json() if response.status != 404 else {}
        return response, payload

    async def _unichem(self, inchikey: str) -> dict[str, str]:
        payload = await self.post_json(
            UNICHEM,
            {"type": "inchikey", "compound": inchikey},
            cache_ttl=self.http.cache.default_ttl if self.http.cache else 0,
            allow_status=(404,),
        )
        out: dict[str, str] = {}
        for compound in (payload or {}).get("compounds") or []:
            for src in compound.get("sources") or []:
                key = _UNICHEM_KEYS.get(src.get("shortName"))
                if key and key not in out and src.get("compoundId"):
                    out[key] = str(src["compoundId"])
        return out

    async def _targets_by_id(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return {}
        _, payload = await self._list(
            "target",
            {
                "target_chembl_id__in": ",".join(ids[:100]),
                "limit": min(len(ids), 100),
                "only": "target_chembl_id,pref_name,target_type,organism,target_components",
            },
        )
        return {t.get("target_chembl_id"): t for t in payload.get("targets") or []}

    async def _documents(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return {}
        _, payload = await self._list(
            "document",
            {
                "document_chembl_id__in": ",".join(ids[:100]),
                "limit": min(len(ids), 100),
                "only": "document_chembl_id,pubmed_id,doi,title,year,journal",
            },
        )
        return {d.get("document_chembl_id"): d for d in payload.get("documents") or []}

    # ------------------------------------------------------------ tools
    @tool(
        description="""
        Free-text search of ChEMBL molecules (drugs, clinical candidates, research compounds) by name,
        synonym, trade name or code (e.g. 'imatinib', 'Gleevec', 'STI-571', 'osimertinib'). Returns ChEMBL
        IDs, max clinical phase, first approval year, molecule type, ATC codes, key properties (MW, ALogP,
        Ro5) and InChIKey/SMILES for cross-database linking. Use get_molecule for full detail with
        PubChem/DrugBank/UNII cross-references; use PubChem for structure/similarity search of arbitrary
        chemicals. Optional max_phase filter (4 = approved drugs only).
        """,
        federated=True,
    )
    async def search_molecules(self, params: SearchMoleculesParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        query: dict[str, Any] = {"q": params.query, "limit": params.limit, "offset": offset}
        if params.max_phase is not None:
            query["max_phase__gte"] = params.max_phase
        response, payload = await self._list("molecule/search", query)
        molecules = payload.get("molecules") or []
        total, nxt = _page(payload.get("page_meta") or {}, offset, len(molecules))
        return ToolResult(
            source=self.name,
            tool="search_molecules",
            records=[_molecule_record(m) for m in molecules],
            total=total,
            next_cursor=nxt,
            request_url=response.url,
        )

    @tool(
        description="""
        Fetch one ChEMBL molecule by ChEMBL ID (CHEMBL941), standard InChIKey, or exact preferred
        name/synonym (case-insensitive: 'imatinib', 'Gleevec'). Returns max phase, first approval, molecule
        type, ATC codes, physicochemical properties (MW, ALogP, PSA, HBA/HBD, Ro5 violations, QED),
        canonical SMILES, InChIKey, synonyms and cross-references, and (include_xrefs=true) the PubChem CID,
        DrugBank, UNII, DrugCentral, BindingDB and ChEBI IDs via EBI UniChem. Use this to map a drug to
        identifiers used by other databases. A name can match a parent and its salts; all are returned.
        """
    )
    async def get_molecule(self, params: GetMoleculeParams) -> ToolResult:
        ident = params.identifier.strip()
        notes: list[str] = []
        if _CHEMBL_ID.match(ident):
            cid = _norm_chembl(ident)
            response = await self.request("GET", f"{API}/molecule/{quote(cid)}.json", allow_status=(404,))
            molecules = [response.json()] if response.status != 404 else []
        elif _INCHIKEY.match(ident):
            response, payload = await self._list(
                "molecule", {"molecule_structures__standard_inchi_key": ident.upper(), "limit": 5}
            )
            molecules = payload.get("molecules") or []
        else:
            response, payload = await self._list("molecule", {"pref_name__iexact": ident, "limit": 5})
            molecules = payload.get("molecules") or []
            if not molecules:
                response, payload = await self._list(
                    "molecule", {"molecule_synonyms__molecule_synonym__iexact": ident, "limit": 5}
                )
                molecules = payload.get("molecules") or []
        if not molecules:
            return ToolResult(
                source=self.name,
                tool="get_molecule",
                notes=[f"no ChEMBL molecule matches {ident!r}; try search_molecules for fuzzy matching"],
                request_url=response.url,
            )
        records = []
        for m in molecules:
            xrefs: dict[str, str] = {}
            key = (m.get("molecule_structures") or {}).get("standard_inchi_key")
            if params.include_xrefs and key:
                try:
                    xrefs = await self._unichem(key)
                except SourceError as exc:
                    notes.append(f"UniChem cross-reference lookup failed: {exc.message[:120]}")
            records.append(_molecule_record(m, xrefs))
        return ToolResult(
            source=self.name,
            tool="get_molecule",
            records=records,
            total=len(records),
            notes=notes,
            request_url=response.url,
        )

    @tool(
        description="""
        Search ChEMBL drug targets (proteins, complexes, families, cell lines, organisms) by name, gene
        symbol or keyword ('EGFR', 'dopamine receptor', 'BACE1'). Returns target ChEMBL IDs, target type,
        organism, component UniProt accessions and gene symbol. Filter with organism='Homo sapiens' and
        target_type='SINGLE PROTEIN'. Use the returned target ChEMBL ID with get_activities or
        get_mechanisms; use get_target to go from a UniProt accession to ChEMBL targets.
        """
    )
    async def search_targets(self, params: SearchTargetsParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        response, payload = await self._list(
            "target/search",
            {
                "q": params.query,
                "organism": params.organism,
                "target_type": params.target_type.upper() if params.target_type else None,
                "limit": params.limit,
                "offset": offset,
            },
        )
        targets = payload.get("targets") or []
        total, nxt = _page(payload.get("page_meta") or {}, offset, len(targets))
        return ToolResult(
            source=self.name,
            tool="search_targets",
            records=[_target_record(t) for t in targets],
            total=total,
            next_cursor=nxt,
            request_url=response.url,
        )

    @tool(
        description="""
        Fetch a ChEMBL target by target ChEMBL ID (CHEMBL203) or look targets up by UniProt accession
        (P00533 -> CHEMBL203 single protein plus any families/complexes containing it). Returns target
        type, organism, components with UniProt accessions, gene symbol, HGNC and PharmGKB IDs. Use it to
        translate a protein into the ChEMBL target ID needed by get_activities / get_mechanisms.
        """
    )
    async def get_target(self, params: GetTargetParams) -> ToolResult:
        ident = params.identifier.strip()
        if _CHEMBL_ID.match(ident):
            response = await self.request(
                "GET", f"{API}/target/{quote(_norm_chembl(ident))}.json", allow_status=(404,)
            )
            targets = [response.json()] if response.status != 404 else []
            total = len(targets)
        else:
            response, payload = await self._list(
                "target", {"target_components__accession": ident.upper(), "limit": 50}
            )
            targets = payload.get("targets") or []
            targets.sort(key=lambda t: (t.get("target_type") != "SINGLE PROTEIN", t.get("target_chembl_id") or ""))
            total = (payload.get("page_meta") or {}).get("total_count")
        if not targets:
            return ToolResult(
                source=self.name,
                tool="get_target",
                notes=[f"no ChEMBL target found for {ident!r} (expects CHEMBL target ID or UniProt accession)"],
                request_url=response.url,
            )
        return ToolResult(
            source=self.name,
            tool="get_target",
            records=[_target_record(t) for t in targets],
            total=total,
            request_url=response.url,
        )

    @tool(
        description="""
        Bioactivity measurements from ChEMBL for a molecule and/or target: IC50, Ki, Kd, EC50 ... with
        standardized value/units, pChEMBL (-log10 M), assay description/type, target and source publication
        (PMID/DOI). Give molecule_chembl_id and/or target_chembl_id (map UniProt -> target with get_target).
        Filter by standard_type and pchembl_min (7 = 100 nM or better). Default sort: most potent first.
        Rows are also summarized in `data` as a compact table. For binding data outside ChEMBL see BindingDB.
        """
    )
    async def get_activities(self, params: GetActivitiesParams) -> ToolResult:
        if not params.molecule_chembl_id and not params.target_chembl_id:
            raise SourceError(self.name, "get_activities needs molecule_chembl_id and/or target_chembl_id")
        offset = parse_offset(params.cursor)
        query: dict[str, Any] = {
            "molecule_chembl_id": _norm_chembl(params.molecule_chembl_id) if params.molecule_chembl_id else None,
            "target_chembl_id": _norm_chembl(params.target_chembl_id) if params.target_chembl_id else None,
            "standard_type": params.standard_type,
            "pchembl_value__gte": params.pchembl_min,
            "assay_type": params.assay_type.upper() if params.assay_type else None,
            "limit": params.limit,
            "offset": offset,
        }
        if params.sort == "pchembl":
            query["order_by"] = "-pchembl_value"
            query["pchembl_value__isnull"] = "false"
        response, payload = await self._list("activity", query)
        rows = payload.get("activities") or []
        total, nxt = _page(payload.get("page_meta") or {}, offset, len(rows))
        docs: dict[str, dict[str, Any]] = {}
        notes: list[str] = []
        try:
            docs = await self._documents([r.get("document_chembl_id") for r in rows])
        except SourceError as exc:
            notes.append(f"document lookup failed (no PMIDs/DOIs): {exc.message[:120]}")
        by_target = bool(params.target_chembl_id) and not params.molecule_chembl_id
        records, table = [], []
        for r in rows:
            doc = docs.get(r.get("document_chembl_id")) or {}
            pmid = doc.get("pubmed_id")
            value = f"{r.get('standard_relation') or ''}{r.get('standard_value') or r.get('value') or '?'} {r.get('standard_units') or r.get('units') or ''}".strip()
            mol = r.get("molecule_pref_name") or r.get("molecule_chembl_id")
            title = f"{mol} {r.get('standard_type') or r.get('type')} {value} vs {r.get('target_pref_name') or r.get('target_chembl_id')}"
            # counterpart entity only (see module docstring): by target -> molecule; else -> target
            if by_target:
                identifiers = {"chembl": r.get("molecule_chembl_id"), "smiles": r.get("canonical_smiles")}
            else:
                identifiers = {"chembl": r.get("target_chembl_id")}
            attrs = _clean(
                {
                    "molecule_chembl_id": r.get("molecule_chembl_id"),
                    "molecule_name": r.get("molecule_pref_name"),
                    "target_chembl_id": r.get("target_chembl_id"),
                    "target_name": r.get("target_pref_name"),
                    "target_organism": r.get("target_organism"),
                    "standard_type": r.get("standard_type"),
                    "standard_relation": r.get("standard_relation"),
                    "standard_value": _num(r.get("standard_value")),
                    "standard_units": r.get("standard_units"),
                    "pchembl_value": _num(r.get("pchembl_value")),
                    "assay_chembl_id": r.get("assay_chembl_id"),
                    "assay_type": r.get("assay_type"),
                    "assay_description": r.get("assay_description"),
                    "activity_comment": r.get("activity_comment"),
                    "data_validity_comment": r.get("data_validity_comment"),
                    "document_chembl_id": r.get("document_chembl_id"),
                    "journal": r.get("document_journal") or doc.get("journal"),
                    "pmid": str(pmid) if pmid else None,
                    "doi": doc.get("doi"),
                    "ligand_efficiency": (r.get("ligand_efficiency") or {}).get("le"),
                }
            )
            records.append(
                Record(
                    source="chembl",
                    id=str(r.get("activity_id")),
                    type="activity",
                    title=title,
                    url=f"{WEB}/assay/{r['assay_chembl_id']}" if r.get("assay_chembl_id") else None,
                    identifiers=identifiers,
                    date=str(r.get("document_year") or doc.get("year") or "") or None,
                    summary=r.get("assay_description"),
                    attributes=attrs,
                )
            )
            table.append(
                _clean(
                    {
                        "activity_id": r.get("activity_id"),
                        "molecule": r.get("molecule_chembl_id"),
                        "target": r.get("target_chembl_id"),
                        "type": r.get("standard_type"),
                        "value": value,
                        "pchembl": _num(r.get("pchembl_value")),
                        "assay": r.get("assay_chembl_id"),
                        "pmid": str(pmid) if pmid else None,
                    }
                )
            )
        return ToolResult(
            source=self.name,
            tool="get_activities",
            records=records,
            total=total,
            next_cursor=nxt,
            data={"rows": table} if table else None,
            notes=notes,
            request_url=response.url,
        )

    @tool(
        description="""
        Curated drug mechanisms of action from ChEMBL: for a molecule (parent or salt ChEMBL ID, e.g.
        CHEMBL941 imatinib) returns each target with action type (INHIBITOR, AGONIST, ...), mechanism text,
        target ChEMBL ID + UniProt accessions and supporting references; for a target (target ChEMBL ID)
        returns the drugs acting on it. Covers approved drugs and clinical candidates (not raw bioactivity;
        use get_activities for potency data).
        """
    )
    async def get_mechanisms(self, params: GetMechanismsParams) -> ToolResult:
        if not params.molecule_chembl_id and not params.target_chembl_id:
            raise SourceError(self.name, "get_mechanisms needs molecule_chembl_id or target_chembl_id")
        offset = parse_offset(params.cursor)
        base: dict[str, Any] = {
            "target_chembl_id": _norm_chembl(params.target_chembl_id) if params.target_chembl_id else None,
            "limit": params.limit,
            "offset": offset,
        }
        if params.molecule_chembl_id:
            mol = _norm_chembl(params.molecule_chembl_id)
            response, payload = await self._list("mechanism", {**base, "parent_molecule_chembl_id": mol})
            if not (payload.get("mechanisms") or []) and offset == 0:
                response, payload = await self._list("mechanism", {**base, "molecule_chembl_id": mol})
        else:
            response, payload = await self._list("mechanism", base)
        rows = payload.get("mechanisms") or []
        total, nxt = _page(payload.get("page_meta") or {}, offset, len(rows))
        targets = await self._targets_by_id([r.get("target_chembl_id") for r in rows])
        by_target = not params.molecule_chembl_id
        records = []
        for r in rows:
            t = targets.get(r.get("target_chembl_id")) or {}
            accessions = [c.get("accession") for c in t.get("target_components") or [] if c.get("accession")]
            if by_target:
                identifiers = {"chembl": r.get("parent_molecule_chembl_id") or r.get("molecule_chembl_id")}
            else:
                identifiers = {"chembl": r.get("target_chembl_id")}
                if len(accessions) == 1:
                    identifiers["uniprot"] = accessions[0]
            refs = [
                f"{x.get('ref_type')}: {x.get('ref_id')}"
                for x in r.get("mechanism_refs") or []
                if x.get("ref_type") and x.get("ref_id")
            ]
            records.append(
                Record(
                    source="chembl",
                    id=str(r.get("mec_id")),
                    type="association",
                    title=r.get("mechanism_of_action") or f"{r.get('action_type')} of {t.get('pref_name')}",
                    url=f"{WEB}/compound/{r.get('molecule_chembl_id')}" if r.get("molecule_chembl_id") else None,
                    identifiers=identifiers,
                    summary=r.get("mechanism_comment"),
                    attributes=_clean(
                        {
                            "action_type": r.get("action_type"),
                            "molecule_chembl_id": r.get("molecule_chembl_id"),
                            "parent_molecule_chembl_id": r.get("parent_molecule_chembl_id"),
                            "target_chembl_id": r.get("target_chembl_id"),
                            "target_name": t.get("pref_name"),
                            "target_type": t.get("target_type"),
                            "target_organism": t.get("organism"),
                            "target_uniprot": accessions,
                            "max_phase": _num(r.get("max_phase")),
                            "direct_interaction": bool(r.get("direct_interaction")),
                            "disease_efficacy": bool(r.get("disease_efficacy")),
                            "selectivity_comment": r.get("selectivity_comment"),
                            "binding_site_comment": r.get("binding_site_comment"),
                            "references": refs[:6],
                        }
                    ),
                )
            )
        notes = [] if records else ["no curated mechanism of action found (only drugs/clinical candidates have one)"]
        return ToolResult(
            source=self.name,
            tool="get_mechanisms",
            records=records,
            total=total,
            next_cursor=nxt,
            notes=notes,
            request_url=response.url,
        )

    @tool(
        description="""
        Drug indications curated by ChEMBL for a molecule (parent or salt ChEMBL ID): each disease with
        MeSH ID + heading, EFO ID + term, the maximum clinical phase reached for that indication
        (4 = approved) and the supporting references (ClinicalTrials.gov NCT IDs, FDA/EMA labels).
        Records represent the diseases (identifiers mesh/efo). Filter with min_phase. For disease-centric
        drug lists use Open Targets.
        """
    )
    async def get_drug_indications(self, params: GetDrugIndicationsParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        mol = _norm_chembl(params.molecule_chembl_id)
        base: dict[str, Any] = {
            "max_phase_for_ind__gte": params.min_phase,
            "order_by": "-max_phase_for_ind",
            "limit": params.limit,
            "offset": offset,
        }
        response, payload = await self._list("drug_indication", {**base, "parent_molecule_chembl_id": mol})
        if not (payload.get("drug_indications") or []) and offset == 0:
            response, payload = await self._list("drug_indication", {**base, "molecule_chembl_id": mol})
        rows = payload.get("drug_indications") or []
        total, nxt = _page(payload.get("page_meta") or {}, offset, len(rows))
        records = []
        for r in rows:
            nct_ids: list[str] = []
            refs: list[str] = []
            for ref in r.get("indication_refs") or []:
                if ref.get("ref_type") == "ClinicalTrials":
                    nct_ids.extend(x.strip() for x in (ref.get("ref_id") or "").split(",") if x.strip())
                elif ref.get("ref_type"):
                    refs.append(f"{ref.get('ref_type')}: {ref.get('ref_id')}")
            phase = _num(r.get("max_phase_for_ind"))
            records.append(
                Record(
                    source="chembl",
                    id=str(r.get("drugind_id")),
                    type="disease",
                    title=r.get("mesh_heading") or r.get("efo_term"),
                    url=f"{WEB}/compound/{r.get('molecule_chembl_id')}" if r.get("molecule_chembl_id") else None,
                    identifiers={"mesh": r.get("mesh_id"), **_disease_ids(r.get("efo_id"))},
                    summary=f"Indication of {r.get('parent_molecule_chembl_id') or mol}: {r.get('efo_term') or r.get('mesh_heading')} (max phase {phase})",
                    attributes=_clean(
                        {
                            "max_phase_for_indication": phase,
                            "efo_term": r.get("efo_term"),
                            "mesh_heading": r.get("mesh_heading"),
                            "molecule_chembl_id": r.get("molecule_chembl_id"),
                            "parent_molecule_chembl_id": r.get("parent_molecule_chembl_id"),
                            "nct_ids": nct_ids[:20],
                            "n_trials": len(nct_ids) or None,
                            "references": refs[:6],
                        }
                    ),
                )
            )
        return ToolResult(
            source=self.name,
            tool="get_drug_indications",
            records=records,
            total=total,
            next_cursor=nxt,
            notes=[] if records else [f"no indications recorded for {mol}"],
            request_url=response.url,
        )
