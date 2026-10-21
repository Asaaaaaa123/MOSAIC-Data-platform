"""UniProt (UniProtKB) REST API connector.

Docs: https://www.uniprot.org/help/api (query syntax: https://www.uniprot.org/help/query-fields,
return fields: https://www.uniprot.org/help/return_fields, ID mapping:
https://www.uniprot.org/help/id_mapping).

Auth: none (keyless). Rate limits: no published hard limit; UniProt asks for reasonable use. We stay
at 10 req/s.

Quirks:
- ``/uniprotkb/search`` paginates with an opaque ``cursor`` that only appears in the ``Link`` response
  header (``rel="next"``); the total hit count is in the ``X-Total-Results`` header (not kept by the
  shared response cache, so ``total`` can be ``None`` on a cache hit).
- Invalid query syntax and malformed accessions return HTTP 400 with ``{"messages": [...]}``; a valid
  but unknown accession returns 404. Retired accessions return ``entryType: "Inactive"`` with an
  ``inactiveReason`` (DELETED / MERGED / DEMERGED).
- ID mapping is asynchronous: ``POST /idmapping/run`` returns a ``jobId``. ``/idmapping/status/{id}``
  answers ``{"jobStatus": "RUNNING"}`` and, once finished, *redirects* to the default results page
  (which our client follows), so we poll the results URL directly (404 until ready) and only consult
  the status endpoint to detect errors. Non-UniProt to non-UniProt mappings (e.g. GeneID -> PDB) are not
  allowed upstream; this connector chains them through UniProtKB-Swiss-Prot.

Identifiers emitted on protein records: uniprot, gene_symbol, hgnc, entrez_gene, ensembl_gene,
ensembl_transcript (MANE Select), refseq (MANE/canonical protein), chembl, omim (gene MIM). Structure
(PDB, best resolution first in attributes.pdb_structures), drug (DrugBank), pathway (Reactome) and
disease (Orphanet, MIM phenotype) cross-references are kept in ``attributes`` because they identify
*other* entities (a PDB complex is shared by its chains: HBA1 and HBB would merge via 2W72).
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from pydantic import Field

from ..http import SourceError
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, tool

API = "https://rest.uniprot.org"

# Compact field set used for lists (search and ID-mapping results).
_LIST_FIELDS = (
    "accession,id,reviewed,protein_name,gene_names,organism_name,organism_id,length,"
    "protein_existence,annotation_score,date_created,date_modified,ec,cc_function,"
    "xref_hgnc,xref_geneid,xref_ensembl,xref_refseq,xref_mim,xref_chembl,xref_pdb,"
    "xref_alphafolddb,xref_mane-select"
)

# Full annotation field set for get_protein (much smaller than the complete entry: no features or
# literature references).
_DETAIL_FIELDS = (
    _LIST_FIELDS
    + ",mass,cc_subcellular_location,cc_disease,cc_tissue_specificity,cc_subunit,keyword,go,"
    "xref_drugbank,xref_drugcentral,xref_guidetopharmacology,xref_reactome,xref_orphanet,xref_ccds,"
    "xref_kegg,xref_string,xref_mgi,xref_rgd,xref_opentargets"
)

_UNIPROT_TARGETS = {"UniProtKB", "UniProtKB-Swiss-Prot"}
_UNIPROT_SOURCES = {"UniProtKB_AC-ID"}

_POLL_BUDGET_S = 30.0

_ACCESSION_RE = re.compile(
    r"^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})$"
)


class SearchProteinsParams(SearchParams):
    organism_id: int | None = Field(
        None, description="Restrict to an NCBI taxonomy ID, e.g. 9606 (human), 10090 (mouse), 10116 (rat)"
    )
    reviewed: bool | None = Field(
        None, description="true = Swiss-Prot (manually reviewed) only; false = TrEMBL only; omit for both"
    )


class GetProteinParams(ToolParams):
    accession: str = Field(
        description="UniProtKB accession (P00533), isoform accession (P00533-2) or entry name (EGFR_HUMAN)"
    )
    include_sequence: bool = Field(False, description="Include the canonical amino-acid sequence in attributes")


class GetSequencesParams(ToolParams):
    accessions: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="UniProtKB accessions (P01116, P32883 ...), up to 100; isoform suffixes (-2) are dropped",
    )
    uniref50_of: str | None = Field(
        None,
        description="UniProt accession whose UniRef50 cluster (proteins >= 50% identical over 80% of the "
        "longer sequence) to search; combine with taxon_id to find a homolog in a distant species",
    )
    taxon_id: int | None = Field(None, description="NCBI taxon restricting uniref50_of members, e.g. 3702 (Arabidopsis)")
    limit: int = Field(10, ge=1, le=100, description="Maximum UniRef50 members to return (uniref50_of mode)")


class MapIdsParams(ToolParams):
    ids: list[str] = Field(
        min_length=1,
        max_length=500,
        description="Identifiers to map (all of the same type), e.g. ['P00533','P04637'] or ['1956','673']",
    )
    from_db: str = Field(
        "UniProtKB_AC-ID",
        description=(
            "Source database name exactly as UniProt spells it: UniProtKB_AC-ID (UniProt accessions or "
            "entry names), Gene_Name, GeneID (NCBI Gene/Entrez), Ensembl (ENSG/ENST), Ensembl_Protein, "
            "Ensembl_Transcript, HGNC (HGNC:####), RefSeq_Protein (NP_...), RefSeq_Nucleotide, PDB, ChEMBL, "
            "DrugBank, MIM, CCDS, EMBL-GenBank-DDBJ, KEGG, STRING, Reactome, Orphanet, OpenTargets, MGI, RGD, "
            "UniParc, UniRef90 ..."
        ),
    )
    to_db: str = Field(
        "UniProtKB",
        description=(
            "Target database: UniProtKB (all entries), UniProtKB-Swiss-Prot (reviewed only), or any database "
            "listed for from_db (PDB, GeneID, Ensembl, ChEMBL, RefSeq_Protein, HGNC ...)"
        ),
    )
    taxon_id: int | None = Field(
        None, description="NCBI taxonomy ID; only applied when from_db=Gene_Name (strongly recommended: 9606)"
    )
    limit: int = Field(100, ge=1, le=500, description="Maximum number of mappings to return per page")
    cursor: str | None = Field(None, description="Pagination cursor from a previous map_ids result")


# ---------------------------------------------------------------------------
# parsing helpers


def _value(obj: Any) -> str | None:
    if isinstance(obj, dict):
        return obj.get("value")
    return None


def _protein_name(entry: dict[str, Any]) -> str | None:
    desc = entry.get("proteinDescription") or {}
    for key in ("recommendedName",):
        name = _value((desc.get(key) or {}).get("fullName"))
        if name:
            return name
    for key in ("submissionNames", "alternativeNames"):
        for item in desc.get(key) or []:
            name = _value(item.get("fullName"))
            if name:
                return name
    return None


def _alt_names(entry: dict[str, Any]) -> list[str]:
    desc = entry.get("proteinDescription") or {}
    out = []
    for item in desc.get("alternativeNames") or []:
        name = _value(item.get("fullName"))
        if name:
            out.append(name)
    for item in (desc.get("recommendedName") or {}).get("shortNames") or []:
        if _value(item):
            out.append(_value(item))
    return out


def _ec_numbers(entry: dict[str, Any]) -> list[str]:
    desc = entry.get("proteinDescription") or {}
    out: list[str] = []
    for block in [desc.get("recommendedName") or {}] + list(desc.get("alternativeNames") or []) + list(
        desc.get("submissionNames") or []
    ):
        for ec in block.get("ecNumbers") or []:
            if ec.get("value") and ec["value"] not in out:
                out.append(ec["value"])
    return out


def _genes(entry: dict[str, Any]) -> tuple[str | None, list[str]]:
    genes = entry.get("genes") or []
    primary = None
    synonyms: list[str] = []
    for gene in genes:
        name = _value(gene.get("geneName"))
        if name and primary is None:
            primary = name
        elif name:
            synonyms.append(name)
        synonyms.extend(s.get("value") for s in gene.get("synonyms") or [] if s.get("value"))
    if primary is None:
        for gene in genes:
            for key in ("orderedLocusNames", "orfNames"):
                for item in gene.get(key) or []:
                    if item.get("value"):
                        return item["value"], synonyms
    return primary, synonyms


def _xrefs(entry: dict[str, Any], database: str) -> list[dict[str, Any]]:
    return [x for x in entry.get("uniProtKBCrossReferences") or [] if x.get("database") == database]


def _prop(xref: dict[str, Any], key: str) -> str | None:
    for prop in xref.get("properties") or []:
        if prop.get("key") == key:
            value = prop.get("value")
            return None if value in (None, "", "-") else value
    return None


_PUBMED_REFS = re.compile(r"\s*\((?:PubMed:\d+|Ref\.\d+)(?:,\s*(?:PubMed:\d+|Ref\.\d+))*\)")


def _clean(text: str) -> str:
    """Drop inline evidence lists like (PubMed:123, PubMed:456): evidence is not prose."""
    return re.sub(r"\s+", " ", _PUBMED_REFS.sub("", text)).strip()


def _comment_texts(entry: dict[str, Any], comment_type: str) -> list[str]:
    out = []
    for comment in entry.get("comments") or []:
        if comment.get("commentType") != comment_type:
            continue
        for text in comment.get("texts") or []:
            if text.get("value"):
                prefix = f"[{comment['molecule']}] " if comment.get("molecule") else ""
                out.append(prefix + _clean(text["value"]))
    return out


def _subcellular(entry: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for comment in entry.get("comments") or []:
        if comment.get("commentType") != "SUBCELLULAR LOCATION" or comment.get("molecule"):
            continue
        for loc in comment.get("subcellularLocations") or []:
            name = _value(loc.get("location"))
            if name and name not in out:
                out.append(name)
    return out


def _diseases(entry: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for comment in entry.get("comments") or []:
        if comment.get("commentType") != "DISEASE":
            continue
        disease = comment.get("disease") or {}
        name = disease.get("diseaseId")
        if not name:
            continue
        xref = disease.get("diseaseCrossReference") or {}
        item = {
            "name": name,
            "acronym": disease.get("acronym"),
            "uniprot_disease_id": disease.get("diseaseAccession"),
            "mim": xref.get("id") if xref.get("database") == "MIM" else None,
            "note": _clean(" ".join(_value(t) or "" for t in (comment.get("note") or {}).get("texts") or [])) or None,
        }
        out.append({k: v for k, v in item.items() if v})
    return out


def _canonical_ensembl(entry: dict[str, Any], accession: str) -> tuple[str | None, str | None]:
    """(gene id, transcript id) preferring the MANE Select / canonical isoform."""
    xrefs = _xrefs(entry, "Ensembl")
    mane = _xrefs(entry, "MANE-Select")
    mane_tx = mane[0].get("id") if mane else None
    ordered = sorted(
        xrefs,
        key=lambda x: (
            0 if mane_tx and x.get("id") == mane_tx else 1,
            0 if x.get("isoformId") in (None, f"{accession}-1") else 1,
        ),
    )
    for xref in ordered:
        gene = _prop(xref, "GeneId")
        if gene:
            return gene, mane_tx or xref.get("id")
    return None, mane_tx


def _canonical_refseq(entry: dict[str, Any], accession: str) -> str | None:
    mane = _xrefs(entry, "MANE-Select")
    if mane and _prop(mane[0], "RefSeqProteinId"):
        return _prop(mane[0], "RefSeqProteinId")
    refs = _xrefs(entry, "RefSeq")
    refs = sorted(refs, key=lambda x: 0 if x.get("isoformId") in (None, f"{accession}-1") else 1)
    return refs[0].get("id") if refs else None


def _gene_mim(entry: dict[str, Any]) -> str | None:
    mims = _xrefs(entry, "MIM")
    for xref in mims:
        if (_prop(xref, "Type") or "").lower() == "gene":
            return xref.get("id")
    for xref in mims:
        if (_prop(xref, "Type") or "").lower().startswith("gene"):
            return xref.get("id")
    return None


def _pdb_ids(entry: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for xref in _xrefs(entry, "PDB"):
        res = _prop(xref, "Resolution")
        resolution = None
        if res:
            match = re.match(r"([\d.]+)", res)
            resolution = float(match.group(1)) if match else None
        out.append({"id": xref.get("id"), "method": _prop(xref, "Method"), "resolution": resolution})
    return out


def _drop_empty(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v not in (None, [], {}, "")}


def _record(entry: dict[str, Any], detailed: bool = False) -> Record:
    accession = entry.get("primaryAccession") or ""
    gene, synonyms = _genes(entry)
    organism = entry.get("organism") or {}
    ensembl_gene, ensembl_tx = _canonical_ensembl(entry, accession)
    pdbs = _pdb_ids(entry)
    # Best-resolution structure first so attributes.pdb_structures leads with the most informative model.
    pdbs_sorted = sorted(pdbs, key=lambda p: (p["resolution"] is None, p["resolution"] or 0))
    hgnc = _xrefs(entry, "HGNC")
    geneid = _xrefs(entry, "GeneID")
    chembl = _xrefs(entry, "ChEMBL")
    alphafold = _xrefs(entry, "AlphaFoldDB")
    audit = entry.get("entryAudit") or {}
    entry_type = entry.get("entryType") or ""
    functions = _comment_texts(entry, "FUNCTION")
    reviewed = "unreviewed" not in entry_type.lower() and "reviewed" in entry_type.lower()
    sequence = entry.get("sequence") or {}

    attributes: dict[str, Any] = {
        "entry_name": entry.get("uniProtkbId"),
        "reviewed": reviewed if entry_type else None,
        "gene": gene,
        "gene_synonyms": synonyms[:10],
        "organism": organism.get("scientificName"),
        "taxon_id": organism.get("taxonId"),
        "length": sequence.get("length"),
        "protein_existence": entry.get("proteinExistence"),
        "annotation_score": entry.get("annotationScore"),
        "ec_numbers": _ec_numbers(entry),
        "last_updated": audit.get("lastAnnotationUpdateDate"),
        "pdb_count": len(pdbs) or None,
        "pdb_ids": [p["id"] for p in pdbs_sorted[:25]],
        "alphafold": f"https://alphafold.ebi.ac.uk/entry/{alphafold[0]['id']}" if alphafold else None,
    }
    if detailed:
        go_terms: dict[str, list[str]] = {}
        for xref in _xrefs(entry, "GO"):
            term = _prop(xref, "GoTerm") or ""
            aspect, _, label = term.partition(":")
            key = {"F": "molecular_function", "P": "biological_process", "C": "cellular_component"}.get(aspect)
            if key and label:
                go_terms.setdefault(key, []).append(f"{label} ({xref.get('id')})")
        drugs = [
            {"drugbank": x.get("id"), "name": _prop(x, "GenericName")} for x in _xrefs(entry, "DrugBank")
        ]
        attributes.update(
            {
                "alternative_names": _alt_names(entry)[:10],
                "mass_da": sequence.get("molWeight"),
                "first_public": audit.get("firstPublicDate"),
                "entry_version": audit.get("entryVersion"),
                "function_isoforms": [f for f in functions if f.startswith("[")],
                "subcellular_location": _subcellular(entry),
                "tissue_specificity": " ".join(_comment_texts(entry, "TISSUE SPECIFICITY")) or None,
                "subunit": " ".join(_comment_texts(entry, "SUBUNIT"))[:1500] or None,
                "diseases": _diseases(entry),
                "keywords": [k.get("name") for k in entry.get("keywords") or [] if k.get("name")],
                "go_terms": {k: v[:15] for k, v in go_terms.items()},
                "go_term_count": sum(len(v) for v in go_terms.values()) or None,
                "pdb_structures": pdbs_sorted[:50],
                "drugbank_drugs": drugs[:30],
                "drugbank_drug_count": len(drugs) or None,
                "drugcentral": [x.get("id") for x in _xrefs(entry, "DrugCentral")],
                "guide_to_pharmacology": [x.get("id") for x in _xrefs(entry, "GuidetoPHARMACOLOGY")],
                "reactome_pathways": [
                    {"id": x.get("id"), "name": _prop(x, "PathwayName")} for x in _xrefs(entry, "Reactome")
                ][:30],
                "orphanet_diseases": [
                    {"id": x.get("id"), "name": _prop(x, "Disease")} for x in _xrefs(entry, "Orphanet")
                ],
                "mim_phenotypes": [
                    x.get("id") for x in _xrefs(entry, "MIM") if (_prop(x, "Type") or "").lower() == "phenotype"
                ],
                "refseq_proteins": [x.get("id") for x in _xrefs(entry, "RefSeq")],
                "ensembl_transcripts": [x.get("id") for x in _xrefs(entry, "Ensembl")],
                "ccds": [x.get("id") for x in _xrefs(entry, "CCDS")],
                "kegg": [x.get("id") for x in _xrefs(entry, "KEGG")],
                "string": [x.get("id") for x in _xrefs(entry, "STRING")],
                "mgi": [x.get("id") for x in _xrefs(entry, "MGI")],
                "rgd": [x.get("id") for x in _xrefs(entry, "RGD")],
                "sequence": sequence.get("value"),
            }
        )

    return Record(
        source="uniprot",
        id=accession,
        type="protein",
        title=_protein_name(entry) or entry.get("uniProtkbId") or accession,
        url=f"https://www.uniprot.org/uniprotkb/{accession}/entry" if accession else None,
        identifiers={
            "uniprot": accession,
            "gene_symbol": gene,
            "hgnc": hgnc[0].get("id") if hgnc else None,
            "entrez_gene": geneid[0].get("id") if geneid else None,
            "ensembl_gene": ensembl_gene,
            "ensembl_transcript": ensembl_tx,
            "refseq": (_canonical_refseq(entry, accession) or "").split(".")[0] or None,  # versionless
            "chembl": chembl[0].get("id") if chembl else None,
            "omim": _gene_mim(entry),
        },
        date=audit.get("firstPublicDate"),
        summary=" ".join(f for f in functions if not f.startswith("[")) or (functions[0] if functions else None),
        attributes=_drop_empty(attributes),
    )


def _next_cursor(link_header: str | None) -> str | None:
    """Extract the ``cursor`` query param from a ``Link: <...>; rel="next"`` header."""
    if not link_header:
        return None
    # The URL itself contains commas (fields=a,b,c), so match <...>; rel="next" instead of splitting.
    for match in re.finditer(r"<([^>]+)>\s*;\s*rel=\"?next\"?", link_header):
        values = parse_qs(urlsplit(match.group(1)).query).get("cursor")
        if values:
            return values[0]
    return None


def _total(headers: dict[str, str]) -> int | None:
    value = headers.get("x-total-results")
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------


class UniProt(Connector):
    name = "uniprot"
    title = "UniProt (UniProtKB)"
    category = "proteins"
    description = (
        "UniProtKB: ~250M protein sequences (570k manually reviewed Swiss-Prot entries) with function, "
        "subcellular location, disease involvement, keywords and cross-references to genes, structures, "
        "drugs and pathways. Best hub for protein-level annotation and for mapping identifiers between "
        "databases (ID mapping service)."
    )
    homepage = "https://www.uniprot.org/"
    docs_url = "https://www.uniprot.org/help/api"
    auth = "none"
    rate_limit = 10.0
    health_query = "gene:EGFR AND organism_id:9606"
    nih_repositories = ("Universal Protein Resource (UniProt)",)

    @tool(
        description="""
        Search UniProtKB proteins. Returns one record per entry with accession, protein name, gene,
        organism, length, function summary and cross-reference IDs (HGNC, NCBI Gene, Ensembl, RefSeq,
        PDB, ChEMBL, OMIM). Use to find the UniProt accession for a protein/gene or to list proteins
        matching a function, disease or keyword. Accepts plain text ("insulin receptor") or UniProt
        query syntax: gene:EGFR AND organism_id:9606 AND reviewed:true; keyword:Kinase; cc_disease:melanoma;
        go:0004713; ec:2.7.10.1; xref:pdb-4ZAU; length:[100 TO 200]. Use organism_id (9606 = human)
        and reviewed=true to cut noise. For full annotation of one protein use get_protein.
        """,
        federated=True,
    )
    async def search_proteins(self, params: SearchProteinsParams) -> ToolResult:
        clauses = [f"({params.query.strip()})"]
        if params.organism_id:
            clauses.append(f"(organism_id:{params.organism_id})")
        if params.reviewed is not None:
            clauses.append(f"(reviewed:{'true' if params.reviewed else 'false'})")
        query = " AND ".join(clauses) if len(clauses) > 1 else params.query.strip()
        response = await self.request(
            "GET",
            f"{API}/uniprotkb/search",
            params={
                "query": query,
                "fields": _LIST_FIELDS,
                "format": "json",
                "size": params.limit,
                "cursor": params.cursor,
            },
        )
        results = response.json().get("results") or []
        records = [_record(r) for r in results if r.get("entryType") != "Inactive"]
        return ToolResult(
            source=self.name,
            tool="search_proteins",
            records=records,
            total=_total(response.headers),
            next_cursor=_next_cursor(response.headers.get("link")) if results else None,
            request_url=response.url,
        )

    @tool(
        description="""
        Fetch the full UniProtKB annotation for one protein: names, gene and synonyms, organism, length
        and mass, function, subcellular location, tissue specificity, disease involvement (with MIM
        numbers), keywords, GO terms, PDB structures (best resolution first), AlphaFold model, drugs
        (DrugBank), Reactome pathways and the canonical gene/transcript cross-references (HGNC, NCBI
        Gene, Ensembl gene + MANE transcript, RefSeq, ChEMBL target, OMIM). Accepts an accession
        (P00533), isoform (P00533-2) or entry name (EGFR_HUMAN). Use after search_proteins or when
        another source gives you a UniProt accession.
        """
    )
    async def get_protein(self, params: GetProteinParams) -> ToolResult:
        accession = params.accession.strip()
        if "_" in accession:
            # Entry names (EGFR_HUMAN) redirect upstream to the accession URL *without* our fields
            # selection (the full 400 kB entry), so resolve the name to an accession first.
            lookup = await self.request(
                "GET",
                f"{API}/uniprotkb/search",
                params={"query": f"id:{accession.upper()}", "fields": "accession", "format": "json", "size": 1},
                allow_status=(400,),
            )
            hits = (lookup.json().get("results") or []) if lookup.status == 200 else []
            if not hits:
                return ToolResult(
                    source=self.name,
                    tool="get_protein",
                    notes=[f"UniProtKB entry name {accession!r} not found"],
                    request_url=lookup.url,
                )
            accession = hits[0].get("primaryAccession") or accession
        fields = _DETAIL_FIELDS + (",sequence" if params.include_sequence else "")
        response = await self.request(
            "GET",
            f"{API}/uniprotkb/{quote(accession, safe='')}",
            params={"format": "json", "fields": fields},
            allow_status=(400, 404),
        )
        if response.status in (400, 404):
            detail = ""
            try:
                detail = "; ".join(response.json().get("messages") or [])
            except ValueError:
                pass
            return ToolResult(
                source=self.name,
                tool="get_protein",
                notes=[f"UniProtKB entry {accession!r} not found" + (f" ({detail})" if detail else "")],
                request_url=response.url,
            )
        entry = response.json()
        if entry.get("entryType") == "Inactive":
            reason = entry.get("inactiveReason") or {}
            successors = reason.get("mergeDemergeTo") or []
            note = f"UniProtKB entry {accession} is inactive ({reason.get('inactiveReasonType', 'retired')})"
            if reason.get("deletedReason"):
                note += f": {reason['deletedReason']}"
            if successors:
                note += f"; replaced by {', '.join(successors)}"
            return ToolResult(
                source=self.name,
                tool="get_protein",
                notes=[note],
                data={"inactive": True, "replaced_by": successors} if successors else None,
                request_url=response.url,
            )
        record = _record(entry, detailed=True)
        return ToolResult(
            source=self.name, tool="get_protein", records=[record], total=1, request_url=response.url
        )

    @tool(
        description="""
        Canonical amino-acid sequences for up to 100 UniProtKB accessions in one call (data.sequences:
        accession, gene, organism, taxon_id, reviewed, length, sequence), for alignments and
        cross-species comparisons. With uniref50_of (+ taxon_id) it instead finds the members of a
        protein's UniRef50 cluster in that taxon (distant homologs, e.g. a human protein's closest
        Arabidopsis relatives) and returns their sequences. Records are the proteins (no sequence).
        """
    )
    async def get_sequences(self, params: GetSequencesParams) -> ToolResult:
        notes: list[str] = []
        accessions = list(dict.fromkeys(re.sub(r"-\d+$", "", a.strip().upper()) for a in params.accessions if a.strip()))
        invalid = [a for a in accessions if not _ACCESSION_RE.match(a)]
        if invalid:
            notes.append(f"not UniProtKB accessions (skipped): {', '.join(invalid[:10])}")
        accessions = [a for a in accessions if a not in invalid]
        cluster = None
        url = None
        if params.uniref50_of:
            seed = re.sub(r"-\d+$", "", params.uniref50_of.strip().upper())
            if not _ACCESSION_RE.match(seed):
                raise SourceError(self.name, f"uniref50_of must be a UniProtKB accession, got {seed!r}")
            lookup = await self.request(
                "GET", f"{API}/uniref/search",
                params={"query": f"uniprot_id:{seed} AND identity:0.5", "fields": "id,name,count", "format": "json", "size": 1},
            )
            url = lookup.url
            hits = lookup.json().get("results") or []
            cluster = hits[0].get("id") if hits else None
            if not cluster:
                notes.append(f"{seed} is not in a UniRef50 cluster")
            else:
                query = f"uniref_cluster_50:{cluster}"
                if params.taxon_id:
                    query += f" AND organism_id:{params.taxon_id}"
                members = await self.request(
                    "GET", f"{API}/uniprotkb/search",
                    params={"query": query, "fields": "accession", "format": "json", "size": params.limit},
                )
                url = members.url
                found = [r.get("primaryAccession") for r in members.json().get("results") or [] if r.get("primaryAccession")]
                if not found:
                    notes.append(f"no member of {cluster} in taxon {params.taxon_id}" if params.taxon_id else f"{cluster} has no members")
                accessions.extend(a for a in found if a not in accessions)
        if not accessions:
            return ToolResult(source=self.name, tool="get_sequences", notes=notes or ["no accessions given"],
                              data={"uniref50_cluster": cluster, "sequences": []}, request_url=url)
        response = await self.request(
            "GET", f"{API}/uniprotkb/accessions",
            params={"accessions": ",".join(accessions[:100]), "fields": _LIST_FIELDS + ",sequence", "format": "json"},
            allow_status=(400,),
        )
        if response.status == 400:
            detail = ""
            try:
                detail = "; ".join(response.json().get("messages") or [])
            except ValueError:
                pass
            raise SourceError(self.name, f"sequence lookup rejected: {detail or 'HTTP 400'}", status=400, url=response.url)
        entries = [e for e in response.json().get("results") or [] if e.get("entryType") != "Inactive"]
        records, sequences = [], []
        for entry in entries:
            record = _record(entry)
            seq = (entry.get("sequence") or {}).get("value")
            records.append(record)
            sequences.append({
                "accession": record.id,
                "entry_name": record.attributes.get("entry_name"),
                "gene": record.attributes.get("gene"),
                "protein": record.title,
                "organism": record.attributes.get("organism"),
                "taxon_id": record.attributes.get("taxon_id"),
                "reviewed": record.attributes.get("reviewed"),
                "length": len(seq) if seq else record.attributes.get("length"),
                "sequence": seq,
            })
        missing = [a for a in accessions if a not in {r.id for r in records}]
        if missing:
            notes.append(f"not found or inactive in UniProtKB: {', '.join(missing[:20])}")
        return ToolResult(
            source=self.name,
            tool="get_sequences",
            records=records,
            total=len(records),
            data={"uniref50_cluster": cluster, "sequences": sequences},
            notes=notes,
            request_url=response.url,
        )

    @tool(
        description="""
        Map identifiers between databases with the UniProt ID mapping service: a key cross-database
        resolver. Examples: gene symbols -> UniProt (from_db=Gene_Name, taxon_id=9606); NCBI Gene IDs
        -> UniProt (from_db=GeneID); UniProt -> PDB / ChEMBL / Ensembl / RefSeq_Protein / HGNC /
        GeneID (from_db=UniProtKB_AC-ID, to_db=PDB ...); Ensembl gene -> UniProt (from_db=Ensembl).
        Non-UniProt to non-UniProt (e.g. GeneID -> PDB) is chained through reviewed UniProt entries
        automatically. When to_db is UniProtKB or UniProtKB-Swiss-Prot, returns protein records
        (with all cross-references) plus a from->to table in data; otherwise data holds the
        from->to table. Unmapped inputs are listed in data.failed_ids. Up to 500 ids per call.
        """
    )
    async def map_ids(self, params: MapIdsParams) -> ToolResult:
        ids = [i.strip() for i in params.ids if i and i.strip()]
        if not ids:
            raise SourceError(self.name, "map_ids needs at least one non-empty id")
        from_db, to_db = params.from_db.strip(), params.to_db.strip()
        to_uniprot = to_db in _UNIPROT_TARGETS
        # Upstream only maps UniProtKB_AC-ID -> anything, or anything -> UniProtKB (+ a few UniParc /
        # UniRef same-type pairs); everything else is chained through Swiss-Prot.
        chained = (
            from_db not in _UNIPROT_SOURCES and not to_uniprot and to_db != from_db and to_db != "UniParc"
        )
        notes: list[str] = []

        if chained:
            return await self._map_chained(ids, from_db, to_db, params)

        if params.cursor and ":" in params.cursor:
            job_id, _, upstream_cursor = params.cursor.partition(":")
        else:
            job_id = await self._submit(ids, from_db, to_db, params.taxon_id)
            upstream_cursor = ""
        payload, response = await self._results(job_id, to_uniprot, params.limit, upstream_cursor or None)
        results = payload.get("results") or []
        failed = list(payload.get("failedIds") or [])
        mappings, records = self._mapping_rows(results, to_uniprot)
        if payload.get("obsoleteCount"):
            notes.append(f"{payload['obsoleteCount']} mapped entries are obsolete in UniProtKB")
        if params.taxon_id and from_db != "Gene_Name":
            notes.append("taxon_id is only applied by UniProt when from_db=Gene_Name; it was ignored")
        if from_db == "Gene_Name" and not params.taxon_id:
            notes.append("Gene_Name mapping without taxon_id matches all organisms; pass taxon_id=9606 for human")
        nxt = _next_cursor(response.headers.get("link"))
        return ToolResult(
            source=self.name,
            tool="map_ids",
            records=records,
            total=_total(response.headers),
            next_cursor=f"{job_id}:{nxt}" if nxt else None,
            data=_drop_empty(
                {"from_db": from_db, "to_db": to_db, "job_id": job_id, "mappings": mappings, "failed_ids": failed}
            ),
            notes=notes,
            request_url=response.url,
        )

    # ---- ID mapping internals ----
    async def _submit(self, ids: list[str], from_db: str, to_db: str, taxon_id: int | None) -> str:
        form = {"ids": ",".join(ids), "from": from_db, "to": to_db}
        if taxon_id and from_db == "Gene_Name":
            form["taxId"] = str(taxon_id)
        submitted = (await self.request("POST", f"{API}/idmapping/run", data=form, cache_ttl=0)).json()
        job_id = submitted.get("jobId")
        if not job_id:
            raise SourceError(self.name, f"ID mapping submission failed: {submitted}")
        return job_id

    async def _results(
        self, job_id: str, to_uniprot: bool, size: int, cursor: str | None
    ) -> tuple[dict[str, Any], Any]:
        """Poll until the job's results are available (at most ~30 s), then return one page."""
        job = quote(job_id, safe="")
        url = f"{API}/idmapping/uniprotkb/results/{job}" if to_uniprot else f"{API}/idmapping/results/{job}"
        query: dict[str, Any] = {"size": size, "format": "json", "cursor": cursor}
        if to_uniprot:
            query["fields"] = _LIST_FIELDS
        deadline = time.monotonic() + _POLL_BUDGET_S
        delay = 0.5
        while True:
            response = await self.request("GET", url, params=query, cache_ttl=0, allow_status=(404,))
            if response.status != 404:
                return response.json(), response
            status = (
                await self.request("GET", f"{API}/idmapping/status/{job}", cache_ttl=0, allow_status=(404,))
            )
            info: dict[str, Any] = {}
            try:
                info = status.json() if status.content else {}
            except ValueError:
                info = {}
            job_status = (info.get("jobStatus") or "").upper()
            if status.status == 404 or job_status in ("ERROR", "FAILURE", "FAILED"):
                errors = "; ".join(str(e.get("message", e)) for e in info.get("errors") or []) or "unknown job"
                raise SourceError(self.name, f"ID mapping job {job_id} failed: {errors}")
            if "results" in info or "failedIds" in info:
                continue  # finished between our two calls; fetch the page we want
            if time.monotonic() + delay > deadline:
                raise SourceError(
                    self.name,
                    f"ID mapping job {job_id} still {job_status or 'running'} after {_POLL_BUDGET_S:.0f}s; "
                    f"retry map_ids later with cursor='{job_id}:'",
                    retryable=True,
                )
            await asyncio.sleep(delay)
            delay = min(delay * 1.6, 4.0)

    @staticmethod
    def _mapping_rows(results: list[dict[str, Any]], to_uniprot: bool) -> tuple[list[dict[str, str]], list[Record]]:
        mappings: list[dict[str, str]] = []
        by_acc: dict[str, Record] = {}
        for row in results:
            src = row.get("from")
            target = row.get("to")
            if isinstance(target, dict):
                acc = target.get("primaryAccession")
                if not acc:
                    continue
                mappings.append({"from": src, "to": acc})
                if target.get("entryType") == "Inactive":
                    continue
                if acc not in by_acc:
                    by_acc[acc] = _record(target)
                    by_acc[acc].attributes["mapped_from"] = []
                by_acc[acc].attributes["mapped_from"].append(src)
            elif target is not None:
                mappings.append({"from": src, "to": str(target)})
        return mappings, list(by_acc.values()) if to_uniprot else []

    async def _map_chained(self, ids: list[str], from_db: str, to_db: str, params: MapIdsParams) -> ToolResult:
        """from_db -> UniProtKB-Swiss-Prot -> to_db (upstream only maps through UniProt)."""
        if params.cursor:
            raise SourceError(self.name, "pagination is not supported for chained (non-UniProt to non-UniProt) mappings")
        first_job = await self._submit(ids, from_db, "UniProtKB-Swiss-Prot", params.taxon_id)
        first, _ = await self._results(first_job, True, 500, None)
        hop1, _ = self._mapping_rows(first.get("results") or [], True)
        failed = list(first.get("failedIds") or [])
        accessions = list(dict.fromkeys(m["to"] for m in hop1))
        notes = [f"{from_db} -> {to_db} is not a direct UniProt mapping; chained through UniProtKB-Swiss-Prot"]
        if not accessions:
            return ToolResult(
                source=self.name,
                tool="map_ids",
                notes=notes + ["no input mapped to a reviewed UniProt entry"],
                data={"from_db": from_db, "to_db": to_db, "mappings": [], "failed_ids": failed},
            )
        second_job = await self._submit(accessions, "UniProtKB_AC-ID", to_db, None)
        second, response = await self._results(second_job, False, 500, None)
        hop2: dict[str, list[str]] = {}
        for row in second.get("results") or []:
            if row.get("from") and row.get("to") is not None:
                hop2.setdefault(row["from"], []).append(str(row["to"]))
        mappings = [
            {"from": m["from"], "via_uniprot": m["to"], "to": target}
            for m in hop1
            for target in hop2.get(m["to"], [])
        ][: params.limit]
        total = _total(response.headers)
        if total and total > 500:
            notes.append(f"second hop has {total} mappings; only the first 500 were joined")
        return ToolResult(
            source=self.name,
            tool="map_ids",
            total=len(mappings),
            data={
                "from_db": from_db,
                "to_db": to_db,
                "job_ids": [first_job, second_job],
                "mappings": mappings,
                "failed_ids": failed,
            },
            notes=notes,
            request_url=response.url,
        )
