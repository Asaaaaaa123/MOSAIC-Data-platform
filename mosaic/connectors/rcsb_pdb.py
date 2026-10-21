"""RCSB Protein Data Bank connector (Search API v2 + Data API GraphQL).

Docs: https://search.rcsb.org/ (search query language, attribute names:
https://search.rcsb.org/structure-search-attributes.html and .../chemical-search-attributes.html),
https://data.rcsb.org/ (Data API; GraphQL endpoint https://data.rcsb.org/graphql).

Auth: none (keyless). Rate limits: not published; RCSB asks for moderate use. We stay at 5 req/s.

Quirks:
- The Search API answers *HTTP 204 No Content* (empty body) when nothing matches.
- Search returns only identifiers; details are fetched in one batch with GraphQL
  ``entries(entry_ids: [...])`` (unknown IDs are simply omitted).
- Structure attributes (``rcsb_*``) use the ``text`` service; chemical-component attributes
  (InChIKey, DrugBank id) need the separate ``text_chem`` service, otherwise the search silently maps
  through entries and returns unrelated components.
- ``rows`` is capped at 10000 upstream; we cap at 100. Computed (AlphaFold/ModelArchive) models are
  excluded: only experimental entries are returned.

Identifiers on structure records: pdb only. The proteins in the structure (attributes.uniprot = the
queried or first polymer entity's accession, all accessions in attributes.uniprot_ids), the primary
citation (attributes.citation.pmid / .doi) and the ligands (InChIKeys, DrugBank IDs in
attributes.ligands) are related entities: as identifiers they would merge every structure of a protein,
or of a paper, into one cluster with the protein / article record.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field

from ..http import SourceError
from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, strip_html, tool

SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
GRAPHQL_URL = "https://data.rcsb.org/graphql"

_PDB_ID = re.compile(r"^(?:pdb_0000)?([0-9][A-Za-z0-9]{3})$", re.IGNORECASE)
_INCHIKEY = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
_DRUGBANK = re.compile(r"^DB\d{5}$", re.IGNORECASE)
_CHEM_COMP = re.compile(r"^[A-Za-z0-9]{1,5}$")

# Components that are almost never the ligand of interest (solvent, buffer, common ions).
_TRIVIAL_LIGANDS = {
    "HOH", "DOD", "SO4", "PO4", "GOL", "EDO", "PEG", "PGE", "ACT", "CL", "NA", "K", "MG", "CA", "ZN",
    "DMS", "FMT", "IOD", "BR", "NO3", "MPD", "TRS", "EPE", "MES", "BME", "IMD", "ACY", "1PE", "P6G",
}

Sort = Literal["score", "release_date", "resolution"]
_SORT_ATTR = {
    "release_date": ("rcsb_accession_info.initial_release_date", "desc"),
    "resolution": ("rcsb_entry_info.resolution_combined", "asc"),
}

_ENTRY_FIELDS = """
  rcsb_id
  struct { title }
  struct_keywords { pdbx_keywords text }
  exptl { method }
  rcsb_entry_info {
    resolution_combined experimental_method polymer_entity_count nonpolymer_entity_count
    deposited_atom_count molecular_weight
  }
  rcsb_accession_info { initial_release_date deposit_date revision_date }
  rcsb_primary_citation {
    title pdbx_database_id_PubMed pdbx_database_id_DOI rcsb_journal_abbrev journal_abbrev year rcsb_authors
  }
  audit_author { name }
  pubmed { rcsb_pubmed_abstract_text }
  polymer_entities {
    rcsb_id
    rcsb_polymer_entity { pdbx_description }
    rcsb_polymer_entity_container_identifiers {
      uniprot_ids
      reference_sequence_identifiers { database_accession database_name }
    }
    rcsb_entity_source_organism { ncbi_scientific_name ncbi_taxonomy_id }
    entity_poly { rcsb_entity_polymer_type pdbx_strand_id rcsb_sample_sequence_length }
  }
  nonpolymer_entities {
    nonpolymer_comp {
      chem_comp { id name formula }
      rcsb_chem_comp_descriptor { InChIKey }
      rcsb_chem_comp_container_identifiers { drugbank_id }
    }
  }
"""

_ENTRIES_QUERY = "query($ids: [String!]!) { entries(entry_ids: $ids) {" + _ENTRY_FIELDS + "} }"


class SearchStructuresParams(SearchParams):
    experimental_method: Literal["X-ray", "EM", "NMR", "Neutron", "Multiple methods"] | None = Field(
        None, description="Only entries solved by this method (X-ray, EM = cryo-EM, NMR, ...)"
    )
    max_resolution: float | None = Field(
        None, gt=0, description="Only entries with resolution at or better than this many Angstroms, e.g. 2.5"
    )
    sort: Sort = Field("score", description="score (relevance, default), release_date (newest first), resolution (best first)")


class GetEntryParams(ToolParams):
    pdb_id: str = Field(description="4-character PDB ID, e.g. 4ZAU (case-insensitive; pdb_00004zau also accepted)")


class StructuresForUniprotParams(ToolParams):
    uniprot: str = Field(description="UniProtKB accession, e.g. P00533 (EGFR) or P15056 (BRAF)")
    limit: int = Field(10, ge=1, le=100, description="Maximum number of structures to return")
    cursor: str | None = Field(None, description="Pagination cursor (offset) from a previous result")
    max_resolution: float | None = Field(None, gt=0, description="Only structures at or better than this resolution (A)")
    experimental_method: Literal["X-ray", "EM", "NMR", "Neutron", "Multiple methods"] | None = Field(
        None, description="Only entries solved by this method"
    )
    sort: Sort = Field("resolution", description="resolution (best first, default), release_date (newest first) or score")


class StructuresForLigandParams(ToolParams):
    ligand: str = Field(
        description=(
            "Ligand as a PDB chemical component ID (e.g. YY3 = osimertinib, STI = imatinib, ATP, HEM), a "
            "standard InChIKey (from PubChem/ChEMBL), or a DrugBank ID (DB09330)"
        )
    )
    limit: int = Field(10, ge=1, le=100, description="Maximum number of structures to return")
    cursor: str | None = Field(None, description="Pagination cursor (offset) from a previous result")
    sort: Sort = Field("release_date", description="release_date (newest first, default), resolution or score")


# ---------------------------------------------------------------------------


def _terminal(attribute: str, operator: str, value: Any, service: str = "text") -> dict[str, Any]:
    return {
        "type": "terminal",
        "service": service,
        "parameters": {"attribute": attribute, "operator": operator, "value": value},
    }


def _filters(method: str | None, max_resolution: float | None) -> list[dict[str, Any]]:
    nodes = []
    if method:
        nodes.append(_terminal("rcsb_entry_info.experimental_method", "exact_match", method))
    if max_resolution:
        nodes.append(_terminal("rcsb_entry_info.resolution_combined", "less_or_equal", max_resolution))
    return nodes


def _query_body(nodes: list[dict[str, Any]], start: int, rows: int, sort: str, return_type: str = "entry") -> dict:
    query = nodes[0] if len(nodes) == 1 else {"type": "group", "logical_operator": "and", "nodes": nodes}
    options: dict[str, Any] = {"paginate": {"start": start, "rows": rows}, "results_content_type": ["experimental"]}
    if sort in _SORT_ATTR:
        attr, direction = _SORT_ATTR[sort]
        options["sort"] = [{"sort_by": attr, "direction": direction}]
    return {"query": query, "return_type": return_type, "request_options": options}


def _date(value: str | None) -> str | None:
    return value[:10] if value else None


def _entity_order(entity: dict[str, Any]) -> tuple[int, str]:
    eid = (entity or {}).get("rcsb_id") or ""
    tail = eid.rsplit("_", 1)[-1]
    return (int(tail) if tail.isdigit() else 10**6, eid)


def _record(entry: dict[str, Any], prefer_uniprot: str | None = None) -> Record:
    """Normalize one GraphQL entry. ``prefer_uniprot`` (the accession a caller searched for) becomes
    ``attributes['uniprot']`` when the entry contains it, rather than an arbitrary partner chain."""
    pdb_id = entry.get("rcsb_id") or ""
    info = entry.get("rcsb_entry_info") or {}
    accession = entry.get("rcsb_accession_info") or {}
    citation = entry.get("rcsb_primary_citation") or {}
    resolution = info.get("resolution_combined") or []
    entities = []
    uniprot_ids: list[str] = []
    organisms: list[str] = []
    for entity in sorted((e for e in entry.get("polymer_entities") or [] if e), key=_entity_order):
        ids = entity.get("rcsb_polymer_entity_container_identifiers") or {}
        accs = list(ids.get("uniprot_ids") or [])
        for ref in ids.get("reference_sequence_identifiers") or []:
            if (ref.get("database_name") or "").lower() == "uniprot" and ref.get("database_accession") not in accs:
                accs.append(ref["database_accession"])
        orgs = [o.get("ncbi_scientific_name") for o in entity.get("rcsb_entity_source_organism") or [] if o]
        poly = entity.get("entity_poly") or {}
        for acc in accs:
            if acc and acc not in uniprot_ids:
                uniprot_ids.append(acc)
        for org in orgs:
            if org and org not in organisms:
                organisms.append(org)
        entities.append(
            {
                k: v
                for k, v in {
                    "entity": entity.get("rcsb_id"),
                    "description": (entity.get("rcsb_polymer_entity") or {}).get("pdbx_description"),
                    "type": poly.get("rcsb_entity_polymer_type"),
                    "chains": poly.get("pdbx_strand_id"),
                    "length": poly.get("rcsb_sample_sequence_length"),
                    "uniprot": accs or None,
                    "organism": orgs[0] if orgs else None,
                    "taxon_id": next(
                        (o.get("ncbi_taxonomy_id") for o in entity.get("rcsb_entity_source_organism") or [] if o),
                        None,
                    ),
                }.items()
                if v not in (None, [], "")
            }
        )
    ligands = []
    for item in entry.get("nonpolymer_entities") or []:
        comp = (item or {}).get("nonpolymer_comp") or {}
        chem = comp.get("chem_comp") or {}
        if not chem.get("id"):
            continue
        ligands.append(
            {
                k: v
                for k, v in {
                    "id": chem.get("id"),
                    "name": chem.get("name"),
                    "formula": chem.get("formula"),
                    "inchikey": (comp.get("rcsb_chem_comp_descriptor") or {}).get("InChIKey"),
                    "drugbank": (comp.get("rcsb_chem_comp_container_identifiers") or {}).get("drugbank_id"),
                }.items()
                if v
            }
        )
    ligands.sort(key=lambda lig: lig["id"] in _TRIVIAL_LIGANDS)
    pmid = citation.get("pdbx_database_id_PubMed")
    keywords = entry.get("struct_keywords") or {}
    abstract = (entry.get("pubmed") or {}).get("rcsb_pubmed_abstract_text")
    attributes = {
        "method": info.get("experimental_method") or ", ".join(e.get("method") for e in entry.get("exptl") or [] if e.get("method")),
        "resolution_angstrom": resolution[0] if resolution else None,
        "deposit_date": _date(accession.get("deposit_date")),
        "revision_date": _date(accession.get("revision_date")),
        "keywords": keywords.get("pdbx_keywords"),
        "organisms": organisms,
        "uniprot": prefer_uniprot
        if prefer_uniprot and prefer_uniprot in uniprot_ids
        else (uniprot_ids[0] if uniprot_ids else None),
        "uniprot_ids": uniprot_ids,
        "polymer_entity_count": info.get("polymer_entity_count"),
        "molecular_weight_kda": info.get("molecular_weight"),
        "atom_count": info.get("deposited_atom_count"),
        "polymer_entities": entities[:12],
        "ligands": ligands[:15],
        "citation": {
            k: v
            for k, v in {
                "title": citation.get("title"),
                "journal": citation.get("rcsb_journal_abbrev") or citation.get("journal_abbrev"),
                "year": citation.get("year"),
                "pmid": str(pmid) if pmid else None,
                "doi": normalize_doi(citation["pdbx_database_id_DOI"]) if citation.get("pdbx_database_id_DOI") else None,
            }.items()
            if v
        }
        or None,
    }
    return Record(
        source="rcsb_pdb",
        id=pdb_id,
        type="structure",
        title=strip_html((entry.get("struct") or {}).get("title")) or pdb_id,
        url=f"https://www.rcsb.org/structure/{pdb_id}" if pdb_id else None,
        identifiers={"pdb": pdb_id},
        date=_date(accession.get("initial_release_date")),
        authors=[a.get("name") for a in entry.get("audit_author") or [] if a.get("name")],
        summary=strip_html(abstract or citation.get("title")),
        attributes={k: v for k, v in attributes.items() if v not in (None, [], {}, "")},
    )


class RcsbPdb(Connector):
    name = "rcsb_pdb"
    title = "RCSB Protein Data Bank"
    category = "proteins"
    description = (
        "RCSB PDB: ~230k experimentally determined 3D structures of proteins, nucleic acids and complexes "
        "(X-ray, cryo-EM, NMR) with bound ligands, source organisms, UniProt mappings and primary "
        "citations. Best for finding structures of a target, structures with a given drug/ligand bound, "
        "and structure metadata."
    )
    homepage = "https://www.rcsb.org/"
    docs_url = "https://search.rcsb.org/"
    auth = "none"
    rate_limit = 5.0
    health_query = "EGFR kinase"
    nih_repositories = ("Protein Data Bank",)

    async def _search_ids(self, body: dict[str, Any]) -> tuple[list[str], int, str]:
        # Search is a read-only POST: cache it like a GET.
        ttl = self.http.cache.default_ttl if self.http.cache else 0
        response = await self.request("POST", SEARCH_URL, json_body=body, cache_ttl=ttl)
        if response.status == 204 or not response.content.strip():
            return [], 0, response.url
        payload = response.json()
        ids = [r.get("identifier") for r in payload.get("result_set") or [] if r.get("identifier")]
        return ids, int(payload.get("total_count") or 0), response.url

    async def _details(self, ids: list[str], prefer_uniprot: str | None = None) -> list[Record]:
        if not ids:
            return []
        data = await self.graphql(GRAPHQL_URL, _ENTRIES_QUERY, {"ids": ids})
        by_id = {e.get("rcsb_id"): e for e in data.get("entries") or [] if e}
        return [_record(by_id[i], prefer_uniprot) for i in ids if i in by_id]

    async def _page(
        self,
        tool_name: str,
        nodes: list[dict[str, Any]],
        limit: int,
        cursor: str | None,
        sort: str,
        notes: list[str] | None = None,
        prefer_uniprot: str | None = None,
    ) -> ToolResult:
        offset = parse_offset(cursor)
        ids, total, url = await self._search_ids(_query_body(nodes, offset, limit, sort))
        records = await self._details(ids, prefer_uniprot)
        nxt = offset + len(ids)
        return ToolResult(
            source=self.name,
            tool=tool_name,
            records=records,
            total=total,
            next_cursor=str(nxt) if ids and nxt < total else None,
            notes=list(notes or []) + ([] if ids else ["no matching PDB entries"]),
            request_url=url,
        )

    @tool(
        description="""
        Full-text search of PDB entries (titles, descriptions, keywords, authors, citations,
        macromolecule and ligand names). Returns structure records with PDB ID, title, method,
        resolution, release date, primary citation (PMID/DOI), polymer entities with UniProt
        accessions and organisms, and bound ligands. Example queries: "EGFR kinase osimertinib",
        "SARS-CoV-2 spike ACE2", "KRAS G12C". Optional filters: experimental_method, max_resolution;
        sort by score, release_date or resolution. For all structures of a known protein use
        structures_for_uniprot; for a known drug/ligand use structures_for_ligand.
        """,
        federated=True,
    )
    async def search_structures(self, params: SearchStructuresParams) -> ToolResult:
        nodes = [{"type": "terminal", "service": "full_text", "parameters": {"value": params.query}}]
        nodes += _filters(params.experimental_method, params.max_resolution)
        return await self._page("search_structures", nodes, params.limit, params.cursor, params.sort)

    @tool(
        description="""
        Fetch one PDB entry by ID (e.g. 4ZAU): title, method, resolution, deposit/release dates,
        authors, primary citation (PMID, DOI, abstract), every polymer entity (description, chains,
        UniProt accession, organism) and bound ligands (chemical component ID, name, formula,
        InChIKey, DrugBank ID). Use when you already have a PDB ID from UniProt, a paper or another tool.
        """
    )
    async def get_entry(self, params: GetEntryParams) -> ToolResult:
        match = _PDB_ID.match(params.pdb_id.strip())
        if not match:
            return ToolResult(
                source=self.name,
                tool="get_entry",
                notes=[f"{params.pdb_id!r} is not a valid PDB ID (expected 4 characters such as 4ZAU)"],
            )
        pdb_id = match.group(1).upper()
        records = await self._details([pdb_id])
        if not records:
            return ToolResult(source=self.name, tool="get_entry", notes=[f"PDB entry {pdb_id} not found"])
        return ToolResult(
            source=self.name,
            tool="get_entry",
            records=records,
            total=1,
            request_url=GRAPHQL_URL,
        )

    @tool(
        description="""
        List experimental PDB structures containing a given UniProt protein (any chain mapped to the
        accession), best resolution first by default. Use for "what structures exist for EGFR
        (P00533)?" or to pick a high-resolution model of a target. Get the accession from
        uniprot.search_proteins or hgnc.fetch_gene first if you only have a gene symbol. Filters:
        max_resolution, experimental_method; sort: resolution, release_date, score.
        """
    )
    async def structures_for_uniprot(self, params: StructuresForUniprotParams) -> ToolResult:
        accession = params.uniprot.strip().upper()
        nodes = [
            _terminal(
                "rcsb_polymer_entity_container_identifiers.reference_sequence_identifiers.database_accession",
                "exact_match",
                accession,
            ),
            _terminal(
                "rcsb_polymer_entity_container_identifiers.reference_sequence_identifiers.database_name",
                "exact_match",
                "UniProt",
            ),
        ] + _filters(params.experimental_method, params.max_resolution)
        return await self._page(
            "structures_for_uniprot", nodes, params.limit, params.cursor, params.sort, prefer_uniprot=accession
        )

    @tool(
        description="""
        List PDB structures in which a given small molecule is bound. Accepts a PDB chemical
        component ID (YY3, STI, ATP, HEM), a standard InChIKey (e.g. from PubChem or ChEMBL) or a
        DrugBank ID; InChIKey/DrugBank inputs are first resolved to component IDs. Newest first by
        default. Use to find co-crystal structures of a drug with its targets.
        """
    )
    async def structures_for_ligand(self, params: StructuresForLigandParams) -> ToolResult:
        ligand = params.ligand.strip()
        notes: list[str] = []
        if _INCHIKEY.match(ligand.upper()) or _DRUGBANK.match(ligand):
            attribute = (
                "rcsb_chem_comp_descriptor.InChIKey"
                if _INCHIKEY.match(ligand.upper())
                else "rcsb_chem_comp_container_identifiers.drugbank_id"
            )
            body = {
                "query": _terminal(attribute, "exact_match", ligand.upper(), service="text_chem"),
                "return_type": "mol_definition",
                "request_options": {"paginate": {"start": 0, "rows": 25}},
            }
            comp_ids, _, url = await self._search_ids(body)
            if not comp_ids:
                return ToolResult(
                    source=self.name,
                    tool="structures_for_ligand",
                    notes=[f"no PDB chemical component matches {ligand}"],
                    request_url=url,
                )
            notes.append(f"{ligand} resolved to PDB chemical component(s): {', '.join(comp_ids)}")
        elif _CHEM_COMP.match(ligand):
            comp_ids = [ligand.upper()]
        else:
            raise SourceError(
                self.name,
                f"ligand {ligand!r} must be a PDB chemical component ID (1-5 letters/digits), an InChIKey or a DrugBank ID",
            )
        nodes = [
            _terminal(
                "rcsb_nonpolymer_entity_container_identifiers.nonpolymer_comp_id",
                "in" if len(comp_ids) > 1 else "exact_match",
                comp_ids if len(comp_ids) > 1 else comp_ids[0],
            )
        ]
        result = await self._page("structures_for_ligand", nodes, params.limit, params.cursor, params.sort, notes)
        result.data = {"chem_comp_ids": comp_ids}
        return result
