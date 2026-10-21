"""PubChem connector (PUG-REST + PUG-View).

Docs: https://pubchem.ncbi.nlm.nih.gov/docs/pug-rest and https://pubchem.ncbi.nlm.nih.gov/docs/pug-view
Hosts: https://pubchem.ncbi.nlm.nih.gov/rest/pug (compounds, properties, synonyms, xrefs, assay summaries,
fast 2D similarity / substructure) and https://pubchem.ncbi.nlm.nih.gov/rest/pug_view (annotation
headings such as "Drug and Medication Information", "Pharmacology and Biochemistry", "Toxicity").

Auth: none. Rate limits: at most 5 requests/s and 400/min per user (plus a CPU-time budget); we run at
4 req/s. Over-limit requests get HTTP 503 "PUGREST.ServerBusy", which the shared client retries.

Quirks:
- The property formerly called IsomericSMILES / CanonicalSMILES is now ``SMILES`` (full stereo) and
  ``ConnectivitySMILES`` (no stereo); we request the new names.
- ``PatentCount`` and ``LiteratureCount`` are compound properties, cheaper than listing xrefs.
- Unknown names/InChIKeys return 404 ``PUGREST.NotFound``; malformed CIDs/SMILES return 400
  ``PUGREST.BadRequest``; a PUG-View heading absent for a compound returns 400 ``PUGVIEW.BadRequest``.
  All of these are reported as empty results with a note.
- Free-text search is a *name* lookup: exact synonym match first, then PubChem's word match
  (``name_type=word``), merged and de-duplicated; pagination slices that CID list.
- ``assaysummary`` for well-studied drugs is large (imatinib ~2 MB / 5000 rows); the connector
  aggregates it per target gene instead of returning rows.

Identifiers: pubchem_cid, inchikey, smiles, and, parsed from synonyms, cas, unii, chembl, drugbank,
plus non-canonical dtxsid (EPA CompTox), chebi and bindingdb (BDBM number, digits only).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Literal
from urllib.parse import quote

from pydantic import Field

from ..http import SourceError
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, tool

PUG = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
VIEW = "https://pubchem.ncbi.nlm.nih.gov/rest/pug_view"
WEB = "https://pubchem.ncbi.nlm.nih.gov"

PROPERTIES = (
    "Title,MolecularFormula,MolecularWeight,SMILES,ConnectivitySMILES,InChIKey,IUPACName,XLogP,TPSA,"
    "ExactMass,Charge,HBondDonorCount,HBondAcceptorCount,RotatableBondCount,HeavyAtomCount,Complexity,"
    "PatentCount,LiteratureCount"
)

_CAS = re.compile(r"^\d{2,7}-\d{2}-\d$")
_UNII = re.compile(r"^(?:UNII-)?([A-Z0-9]{10})$")
_CHEMBL = re.compile(r"^CHEMBL\d+$")
_DRUGBANK = re.compile(r"^DB\d{5}$")
_DTXSID = re.compile(r"^DTXSID\d+$")
_CHEBI = re.compile(r"^CHEBI:\d+$")
_BDBM = re.compile(r"^BDBM(\d+)$")
_INCHIKEY = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$", re.IGNORECASE)

ANNOTATION_HEADINGS = (
    "Drug and Medication Information",
    "Pharmacology and Biochemistry",
    "Mechanism of Action",
    "Drug Indication",
    "Toxicity",
    "Safety and Hazards",
    "GHS Classification",
    "Interactions and Pathways",
    "Use and Manufacturing",
    "Associated Disorders and Diseases",
)


# ---------------------------------------------------------------- params
class SearchCompoundsParams(SearchParams):
    match: Literal["auto", "exact", "word"] = Field(
        "auto",
        description="auto: exact synonym hits first, then compounds whose names contain the word(s); "
        "exact: exact name/synonym only; word: word match only",
    )


class CompoundRef(ToolParams):
    cid: int | None = Field(None, ge=1, description="PubChem Compound ID, e.g. 2244 (aspirin)")
    name: str | None = Field(None, description="Compound name or synonym (exact match), e.g. 'imatinib'")
    inchikey: str | None = Field(None, description="Standard InChIKey, e.g. BSYNRYMUTXBXSQ-UHFFFAOYSA-N")
    smiles: str | None = Field(None, description="SMILES string, e.g. CC(=O)OC1=CC=CC=C1C(=O)O")


class CompoundXrefsParams(ToolParams):
    cid: int = Field(ge=1, description="PubChem Compound ID")
    max_pmids: int = Field(25, ge=0, le=200, description="How many linked PubMed IDs to list (newest first); 0 to skip")


class BioassaySummaryParams(ToolParams):
    cid: int = Field(ge=1, description="PubChem Compound ID")
    outcome: Literal["active", "inactive", "all"] = Field(
        "active", description="Which outcomes to aggregate into per-target rows"
    )
    limit: int = Field(25, ge=1, le=100, description="Maximum number of target rows")


class SimilaritySearchParams(ToolParams):
    smiles: str | None = Field(None, description="Query structure as SMILES")
    cid: int | None = Field(None, ge=1, description="Query structure as a PubChem CID")
    mode: Literal["similarity", "substructure", "superstructure"] = Field(
        "similarity", description="2D Tanimoto similarity, substructure or superstructure search"
    )
    threshold: int = Field(90, ge=50, le=100, description="Tanimoto threshold in percent (similarity mode)")
    limit: int = Field(20, ge=1, le=100, description="Maximum number of compounds")


class CompoundAnnotationsParams(ToolParams):
    cid: int = Field(ge=1, description="PubChem Compound ID")
    heading: str = Field(
        "Pharmacology and Biochemistry",
        description="PUG-View TOC heading, e.g. " + "; ".join(f"'{h}'" for h in ANNOTATION_HEADINGS),
    )
    max_items: int = Field(30, ge=1, le=100, description="Maximum number of annotation snippets")
    max_chars: int = Field(800, ge=100, le=4000, description="Truncate each snippet to this many characters")


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


def parse_synonyms(synonyms: list[str]) -> dict[str, list[str]]:
    """Pull registry identifiers out of a PubChem synonym list (order preserved, de-duplicated)."""
    found: dict[str, list[str]] = defaultdict(list)

    def add(key: str, value: str) -> None:
        if value not in found[key]:
            found[key].append(value)

    for raw in synonyms or []:
        s = str(raw).strip()
        if _CAS.match(s):
            add("cas", s)
        elif s.startswith("UNII-") and _UNII.match(s):
            add("unii", _UNII.match(s).group(1))
        elif _CHEMBL.match(s):
            add("chembl", s)
        elif _DRUGBANK.match(s):
            add("drugbank", s)
        elif _DTXSID.match(s):
            add("dtxsid", s)
        elif _CHEBI.match(s):
            add("chebi", s)
        elif _BDBM.match(s):
            add("bindingdb", _BDBM.match(s).group(1))
    return dict(found)


def _compound_record(p: dict[str, Any], xrefs: dict[str, list[str]] | None = None, description: str | None = None,
                     description_source: str | None = None) -> Record:
    cid = str(p.get("CID") or "")
    xrefs = xrefs or {}
    identifiers: dict[str, Any] = {
        "pubchem_cid": cid,
        "inchikey": p.get("InChIKey"),
        "smiles": p.get("SMILES") or p.get("IsomericSMILES") or p.get("CanonicalSMILES"),
    }
    for key in ("cas", "unii", "chembl", "drugbank", "dtxsid", "chebi", "bindingdb"):
        if xrefs.get(key):
            identifiers[key] = xrefs[key][0]
    formula = p.get("MolecularFormula")
    summary = description or (
        f"{formula}, MW {p.get('MolecularWeight')}" + (f"; IUPAC: {p['IUPACName']}" if p.get("IUPACName") else "")
        if formula
        else None
    )
    return Record(
        source="pubchem",
        id=cid,
        type="compound",
        title=p.get("Title") or p.get("IUPACName") or f"CID {cid}",
        url=f"{WEB}/compound/{cid}" if cid else None,
        identifiers=identifiers,
        summary=summary,
        attributes=_clean(
            {
                "formula": formula,
                "mw": _num(p.get("MolecularWeight")),
                "exact_mass": _num(p.get("ExactMass")),
                "xlogp": _num(p.get("XLogP")),
                "tpsa": _num(p.get("TPSA")),
                "hbd": p.get("HBondDonorCount"),
                "hba": p.get("HBondAcceptorCount"),
                "rotatable_bonds": p.get("RotatableBondCount"),
                "heavy_atoms": p.get("HeavyAtomCount"),
                "charge": p.get("Charge"),
                "complexity": _num(p.get("Complexity")),
                "iupac_name": p.get("IUPACName"),
                "connectivity_smiles": p.get("ConnectivitySMILES")
                if p.get("ConnectivitySMILES") != identifiers.get("smiles")
                else None,
                "patent_count": p.get("PatentCount"),
                "literature_count": p.get("LiteratureCount"),
                "cas_numbers": (xrefs.get("cas") or [])[1:6],
                "description_source": description_source,
            }
        ),
    )


def _info_text(info: dict[str, Any]) -> str | None:
    value = info.get("Value") or {}
    parts: list[str] = []
    for swm in value.get("StringWithMarkup") or []:
        text = (swm.get("String") or "").strip()
        if not text:
            # icon-only entries (GHS pictograms) carry their meaning in the markup
            text = ", ".join(m.get("Extra") for m in swm.get("Markup") or [] if m.get("Type") == "Icon" and m.get("Extra"))
        if text:
            parts.append(text)
    if value.get("Number"):
        unit = value.get("Unit") or ""
        parts.append(" ".join(str(n) for n in value["Number"]) + (f" {unit}" if unit else ""))
    if value.get("Boolean"):
        parts.append(", ".join(str(b) for b in value["Boolean"]))
    if not parts and value.get("ExternalTableName"):
        return None
    return " | ".join(parts) or None


def flatten_annotations(record: dict[str, Any], max_items: int, max_chars: int) -> list[dict[str, Any]]:
    refs = {r.get("ReferenceNumber"): r for r in record.get("Reference") or []}
    out: list[dict[str, Any]] = []

    def walk(section: dict[str, Any], path: list[str]) -> None:
        here = path + [section.get("TOCHeading")] if section.get("TOCHeading") else path
        for info in section.get("Information") or []:
            if len(out) >= max_items:
                return
            text = _info_text(info)
            if not text:
                continue
            ref = refs.get(info.get("ReferenceNumber")) or {}
            out.append(
                _clean(
                    {
                        "section": " > ".join(here[1:] or here),
                        "name": info.get("Name"),
                        "text": text if len(text) <= max_chars else text[: max_chars - 3].rstrip() + "...",
                        "source": ref.get("SourceName"),
                        "url": ref.get("URL"),
                    }
                )
            )
        for sub in section.get("Section") or []:
            if len(out) < max_items:
                walk(sub, here)

    for top in record.get("Section") or []:
        walk(top, [])
    return out


class PubChem(Connector):
    name = "pubchem"
    title = "PubChem"
    category = "chemistry"
    description = (
        "NCBI's open chemistry database: 120M+ compounds with structures, computed properties, synonyms "
        "and registry IDs, 300M+ bioassay outcomes, literature/patent links and curated annotations "
        "(pharmacology, drug information, toxicity, safety). Best for resolving any chemical name or "
        "structure and for structure similarity search."
    )
    homepage = "https://pubchem.ncbi.nlm.nih.gov/"
    docs_url = "https://pubchem.ncbi.nlm.nih.gov/docs/pug-rest"
    auth = "none"
    rate_limit = 4.0
    health_query = "aspirin"
    nih_repositories = ("PubChem",)

    # ------------------------------------------------------------ internals
    async def _cids_for_name(self, name: str, word: bool) -> tuple[list[int], str]:
        response = await self.request(
            "GET",
            f"{PUG}/compound/name/{quote(name, safe='')}/cids/JSON",
            params={"name_type": "word"} if word else None,
            allow_status=(400, 404),
        )
        if response.status != 200:
            return [], response.url
        return list((response.json().get("IdentifierList") or {}).get("CID") or []), response.url

    async def _properties(self, cids: list[int]) -> tuple[list[dict[str, Any]], str]:
        response = await self.request(
            "GET", f"{PUG}/compound/cid/{','.join(str(c) for c in cids)}/property/{PROPERTIES}/JSON", allow_status=(400, 404)
        )
        if response.status != 200:
            return [], response.url
        props = (response.json().get("PropertyTable") or {}).get("Properties") or []
        order = {c: i for i, c in enumerate(cids)}
        props.sort(key=lambda p: order.get(p.get("CID"), 1 << 30))
        return props, response.url

    async def _synonyms(self, cid: int) -> list[str]:
        response = await self.request("GET", f"{PUG}/compound/cid/{cid}/synonyms/JSON", allow_status=(400, 404))
        if response.status != 200:
            return []
        info = ((response.json().get("InformationList") or {}).get("Information") or [{}])[0]
        return info.get("Synonym") or []

    async def _description(self, cid: int) -> tuple[str | None, str | None]:
        response = await self.request("GET", f"{PUG}/compound/cid/{cid}/description/JSON", allow_status=(400, 404))
        if response.status != 200:
            return None, None
        found = [
            i for i in (response.json().get("InformationList") or {}).get("Information") or [] if i.get("Description")
        ]
        if not found:
            return None, None
        preferred = ("ChEBI", "DrugBank", "LiverTox", "NCI Thesaurus (NCIt)", "Medical Subject Headings (MeSH)")
        rank = {name: i for i, name in enumerate(preferred)}
        best = min(found, key=lambda i: (rank.get(i.get("DescriptionSourceName"), len(preferred)), -len(i["Description"])))
        return best["Description"], best.get("DescriptionSourceName")

    # ------------------------------------------------------------ tools
    @tool(
        description="""
        Find PubChem compounds by name (drug names, synonyms, trade names, registry numbers such as CAS
        '50-78-2' or codes like 'STI-571'). Returns CID, title, formula, MW, SMILES, InChIKey, IUPAC name,
        XLogP, TPSA, H-bond counts and patent/literature counts. match='auto' puts exact synonym hits first,
        then names containing the word(s). This is a name lookup, not a topic search: for 'kinase
        inhibitors' style queries use ChEMBL or literature sources. For structures use similarity_search.
        """,
        federated=True,
    )
    async def search_compounds(self, params: SearchCompoundsParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        query = params.query.strip()
        cids: list[int] = []
        url = None
        if params.match in ("auto", "exact"):
            exact, url = await self._cids_for_name(query, word=False)
            cids.extend(exact)
        if params.match == "word" or (params.match == "auto" and len(cids) < offset + params.limit):
            words, url = await self._cids_for_name(query, word=True)
            cids.extend(c for c in words if c not in cids)
        total = len(cids)
        page = cids[offset: offset + params.limit]
        if not page:
            return ToolResult(
                source=self.name,
                tool="search_compounds",
                total=total,
                notes=[f"no PubChem compound name matches {query!r}"] if not total else [],
                request_url=url,
            )
        props, url = await self._properties(page)
        nxt = offset + len(page)
        return ToolResult(
            source=self.name,
            tool="search_compounds",
            records=[_compound_record(p) for p in props],
            total=total,
            next_cursor=str(nxt) if nxt < total else None,
            request_url=url,
        )

    @tool(
        description="""
        Fetch one PubChem compound by exactly one of: cid, name (exact synonym), inchikey or smiles.
        Returns computed properties (formula, MW, exact mass, XLogP, TPSA, HBD/HBA, rotatable bonds,
        complexity), SMILES/InChIKey, IUPAC name, patent and literature counts, a textual description
        (ChEBI/LiverTox/...), and registry IDs parsed from synonyms (CAS, UNII, ChEMBL, DrugBank, DTXSID,
        ChEBI). Use it to convert between chemical identifiers.
        """
    )
    async def get_compound(self, params: CompoundRef) -> ToolResult:
        given = [k for k in ("cid", "name", "inchikey", "smiles") if getattr(params, k) not in (None, "")]
        if len(given) != 1:
            raise SourceError(self.name, "get_compound needs exactly one of cid, name, inchikey, smiles")
        key = given[0]
        notes: list[str] = []
        if key == "cid":
            path, query = f"cid/{params.cid}", None
        elif key == "name":
            path, query = f"name/{quote(params.name.strip(), safe='')}", None
        elif key == "inchikey":
            if not _INCHIKEY.match(params.inchikey.strip()):
                raise SourceError(self.name, "inchikey must look like XXXXXXXXXXXXXX-XXXXXXXXXX-X")
            path, query = f"inchikey/{params.inchikey.strip().upper()}", None
        else:
            path, query = "smiles", {"smiles": params.smiles.strip()}
        response = await self.request(
            "GET", f"{PUG}/compound/{path}/property/{PROPERTIES}/JSON", params=query, allow_status=(400, 404)
        )
        if response.status != 200:
            return ToolResult(
                source=self.name,
                tool="get_compound",
                notes=[f"no PubChem compound for {key}={getattr(params, key)!r} (HTTP {response.status})"],
                request_url=response.url,
            )
        props = (response.json().get("PropertyTable") or {}).get("Properties") or []
        props = [p for p in props if p.get("CID")]
        if not props:
            return ToolResult(source=self.name, tool="get_compound", notes=["no compound returned"], request_url=response.url)
        if len(props) > 1:
            notes.append(
                f"{len(props)} compounds match; showing CID {props[0]['CID']} (others: "
                + ", ".join(str(p["CID"]) for p in props[1:10]) + ")"
            )
        best = props[0]
        cid = int(best["CID"])
        xrefs = parse_synonyms(await self._synonyms(cid))
        description, desc_source = await self._description(cid)
        return ToolResult(
            source=self.name,
            tool="get_compound",
            records=[_compound_record(best, xrefs, description, desc_source)],
            total=len(props),
            notes=notes,
            request_url=response.url,
        )

    @tool(
        description="""
        Registry cross-references and literature/patent linkage for a PubChem CID: CAS numbers, FDA UNII,
        ChEMBL, DrugBank, EPA DTXSID, ChEBI and BindingDB IDs (parsed from the synonym list), top
        synonyms, total linked PubMed articles / patents / literature, and the newest linked PMIDs.
        Use to bridge a compound to drug databases and to find literature about it.
        """
    )
    async def compound_xrefs(self, params: CompoundXrefsParams) -> ToolResult:
        cid = params.cid
        response = await self.request("GET", f"{PUG}/compound/cid/{cid}/synonyms/JSON", allow_status=(400, 404))
        if response.status != 200:
            return ToolResult(
                source=self.name, tool="compound_xrefs", notes=[f"CID {cid} not found"], request_url=response.url
            )
        info = ((response.json().get("InformationList") or {}).get("Information") or [{}])[0]
        synonyms = info.get("Synonym") or []
        xrefs = parse_synonyms(synonyms)
        props, _ = await self._properties([cid])
        prop = props[0] if props else {"CID": cid}
        pmids: list[str] = []
        pubmed_count = None
        if params.max_pmids:
            xr = await self.request("GET", f"{PUG}/compound/cid/{cid}/xrefs/PubMedID/JSON", allow_status=(400, 404))
            if xr.status == 200:
                xinfo = ((xr.json().get("InformationList") or {}).get("Information") or [{}])[0]
                all_pmids = sorted({int(p) for p in xinfo.get("PubMedID") or []}, reverse=True)
                pubmed_count = len(all_pmids)
                pmids = [str(p) for p in all_pmids[: params.max_pmids]]
        names = [s for s in synonyms if not any(k in parse_synonyms([s]) for k in ("cas", "unii", "chembl", "drugbank", "dtxsid", "chebi", "bindingdb"))]
        data = _clean(
            {
                "cid": cid,
                **{k: v for k, v in xrefs.items()},
                "synonyms": names[:20],
                "synonym_count": len(synonyms),
                "pubmed_count": pubmed_count,
                "patent_count": prop.get("PatentCount"),
                "literature_count": prop.get("LiteratureCount"),
                "recent_pmids": pmids,
            }
        )
        return ToolResult(
            source=self.name,
            tool="compound_xrefs",
            records=[_compound_record(prop, xrefs)],
            total=1,
            data=data,
            request_url=response.url,
        )

    @tool(
        description="""
        Summarize PubChem BioAssay results for a compound (CID): counts of active / inactive /
        inconclusive outcomes and, per protein target (NCBI Gene), the number of active assays, best
        (lowest) activity value in uM with its type, example assay names, AIDs and PMIDs. Records
        represent the target genes (entrez_gene, gene_symbol). Use for a quick polypharmacology /
        screening profile; use ChEMBL get_activities for curated literature potency.
        """
    )
    async def bioassay_summary(self, params: BioassaySummaryParams) -> ToolResult:
        cid = params.cid
        response = await self.request("GET", f"{PUG}/compound/cid/{cid}/assaysummary/JSON", allow_status=(400, 404))
        if response.status != 200:
            return ToolResult(
                source=self.name,
                tool="bioassay_summary",
                notes=[f"no bioassay data for CID {cid}"],
                request_url=response.url,
            )
        table = response.json().get("Table") or {}
        columns = (table.get("Columns") or {}).get("Column") or []
        idx = {c: i for i, c in enumerate(columns)}
        rows = [r.get("Cell") or [] for r in table.get("Row") or []]

        def cell(row: list[Any], col: str) -> str:
            i = idx.get(col)
            return str(row[i]).strip() if i is not None and i < len(row) and row[i] is not None else ""

        outcome_counts = Counter(cell(r, "Activity Outcome") or "Unspecified" for r in rows)
        tested_aids = {cell(r, "AID") for r in rows}
        wanted = {"active": {"Active"}, "inactive": {"Inactive"}, "all": None}[params.outcome]
        groups: dict[str, dict[str, Any]] = {}
        untargeted_active: Counter[str] = Counter()
        for r in rows:
            outcome = cell(r, "Activity Outcome")
            if wanted is not None and outcome not in wanted:
                continue
            gene = cell(r, "Target GeneID")
            if not gene:
                if outcome == "Active":
                    untargeted_active[cell(r, "Assay Name")[:120]] += 1
                continue
            g = groups.setdefault(
                gene,
                {"gene_id": gene, "aids": set(), "outcomes": Counter(), "accessions": set(), "assays": [], "pmids": set(),
                 "best": None, "best_name": None},
            )
            g["aids"].add(cell(r, "AID"))
            g["outcomes"][outcome] += 1
            if cell(r, "Target Accession"):
                g["accessions"].add(cell(r, "Target Accession"))
            if cell(r, "PubMed ID"):
                g["pmids"].add(cell(r, "PubMed ID"))
            name = cell(r, "Assay Name")
            if name and name not in g["assays"] and len(g["assays"]) < 3:
                g["assays"].append(name)
            value = _num(cell(r, "Activity Value [uM]"))
            if value is not None and (g["best"] is None or value < g["best"]):
                g["best"], g["best_name"] = value, cell(r, "Activity Name") or None
        ranked = sorted(
            groups.values(),
            key=lambda g: (g["best"] is None, g["best"] if g["best"] is not None else 0, -len(g["aids"])),
        )[: params.limit]
        genes: dict[str, dict[str, Any]] = {}
        if ranked:
            gr = await self.request(
                "GET", f"{PUG}/gene/geneid/{','.join(g['gene_id'] for g in ranked)}/summary/JSON", allow_status=(400, 404)
            )
            if gr.status == 200:
                for s in (gr.json().get("GeneSummaries") or {}).get("GeneSummary") or []:
                    genes[str(s.get("GeneID"))] = s
        records = []
        for g in ranked:
            info = genes.get(g["gene_id"]) or {}
            symbol = info.get("Symbol")
            human = info.get("TaxonomyID") == 9606
            best = f"{g['best_name'] or 'activity'} {g['best']} uM" if g["best"] is not None else None
            records.append(
                Record(
                    source="pubchem",
                    id=f"{cid}:{g['gene_id']}",
                    type="activity",
                    title=f"CID {cid} vs {symbol or 'GeneID ' + g['gene_id']}" + (f" ({best})" if best else ""),
                    url=f"{WEB}/compound/{cid}#section=Biological-Test-Results",
                    identifiers={"entrez_gene": g["gene_id"], "gene_symbol": symbol if human else None},
                    summary="; ".join(g["assays"]) or None,
                    attributes=_clean(
                        {
                            "cid": cid,
                            "gene_id": g["gene_id"],
                            "gene_symbol": symbol,
                            "gene_name": info.get("Name"),
                            "organism": info.get("Taxonomy"),
                            "protein_accessions": sorted(g["accessions"])[:5],
                            "n_assays": len(g["aids"]),
                            "outcomes": dict(g["outcomes"]),
                            "best_activity_uM": g["best"],
                            "best_activity_type": g["best_name"],
                            "aids": sorted(g["aids"], key=lambda a: int(a) if a.isdigit() else 0)[:10],
                            "pmids": sorted(g["pmids"])[:10],
                        }
                    ),
                )
            )
        data = {
            "cid": cid,
            "assays_tested": len(tested_aids),
            "outcome_counts": dict(outcome_counts),
            "targets_with_selected_outcome": len(groups),
            "top_untargeted_active_assays": [
                {"assay": name, "n": n} for name, n in untargeted_active.most_common(5)
            ]
            or None,
        }
        return ToolResult(
            source=self.name,
            tool="bioassay_summary",
            records=records,
            total=len(groups),
            data=_clean(data),
            notes=[] if records else [f"no target-annotated {params.outcome} assay results for CID {cid}"],
            request_url=response.url,
        )

    @tool(
        description="""
        Structure search in PubChem by SMILES or CID: 2D Tanimoto similarity (threshold in %, default
        90), substructure or superstructure. Returns compounds with properties, SMILES and InChIKey.
        Use for analog / scaffold searches (e.g. find compounds similar to imatinib, CID 5291).
        Results are not ranked by similarity score (PubChem does not return scores).
        """
    )
    async def similarity_search(self, params: SimilaritySearchParams) -> ToolResult:
        if bool(params.smiles) == bool(params.cid):
            raise SourceError(self.name, "similarity_search needs exactly one of smiles or cid")
        op = {"similarity": "fastsimilarity_2d", "substructure": "fastsubstructure", "superstructure": "fastsuperstructure"}[params.mode]
        query: dict[str, Any] = {"MaxRecords": params.limit}
        if params.mode == "similarity":
            query["Threshold"] = params.threshold
        if params.smiles:
            url = f"{PUG}/compound/{op}/smiles/property/{PROPERTIES}/JSON"
            query["smiles"] = params.smiles.strip()
        else:
            url = f"{PUG}/compound/{op}/cid/{params.cid}/property/{PROPERTIES}/JSON"
        response = await self.request("GET", url, params=query, allow_status=(400, 404))
        if response.status != 200:
            return ToolResult(
                source=self.name,
                tool="similarity_search",
                notes=[f"no structure matches (HTTP {response.status}); check the SMILES/CID"],
                request_url=response.url,
            )
        props = (response.json().get("PropertyTable") or {}).get("Properties") or []
        return ToolResult(
            source=self.name,
            tool="similarity_search",
            records=[_compound_record(p) for p in props],
            total=None,
            notes=[f"capped at {params.limit} hits"] if len(props) >= params.limit else [],
            request_url=response.url,
        )

    @tool(
        description="""
        Curated PubChem annotations (PUG-View) for a CID under one heading, flattened to short text
        snippets with section, source and URL. Useful headings: 'Drug and Medication Information'
        (indications, drug classes, labels), 'Pharmacology and Biochemistry' (MeSH/ATC class, ADME,
        half-life, mechanism), 'Mechanism of Action', 'Toxicity', 'Safety and Hazards' / 'GHS
        Classification', 'Interactions and Pathways'. Not every compound has every heading.
        """
    )
    async def compound_annotations(self, params: CompoundAnnotationsParams) -> ToolResult:
        response = await self.request(
            "GET",
            f"{VIEW}/data/compound/{params.cid}/JSON",
            params={"heading": params.heading},
            allow_status=(400, 404),
        )
        if response.status != 200:
            return ToolResult(
                source=self.name,
                tool="compound_annotations",
                notes=[f"heading {params.heading!r} not available for CID {params.cid}"],
                request_url=response.url,
            )
        record = response.json().get("Record") or {}
        items = flatten_annotations(record, params.max_items, params.max_chars)
        return ToolResult(
            source=self.name,
            tool="compound_annotations",
            records=[
                Record(
                    source="pubchem",
                    id=str(params.cid),
                    type="compound",
                    title=record.get("RecordTitle") or f"CID {params.cid}",
                    url=f"{WEB}/compound/{params.cid}#section={quote(params.heading.replace(' ', '-'))}",
                    identifiers={"pubchem_cid": params.cid},
                    summary=items[0]["text"] if items else None,
                    attributes={"heading": params.heading, "n_snippets": len(items)},
                )
            ],
            total=len(items),
            data={"heading": params.heading, "annotations": items},
            notes=[] if items else ["heading exists but contains no text annotations"],
            request_url=response.url,
        )
