"""EMBL-EBI Ontology Lookup Service (OLS4) connector.

Docs: https://www.ebi.ac.uk/ols4/help (REST v1 under /ols4/api, richer v2 under /ols4/api/v2).
Auth: none. Rate limits: none published; EBI asks for reasonable use, we stay at 5 req/s.

The general ontology resolver for ~270 ontologies (MONDO, EFO, DOID, HP, GO, NCIT, ChEBI,
UBERON, CL, ORDO (Orphanet terms are ``ORDO:<n>`` in OLS; ``Orphanet:<n>`` input is accepted), ...): term search, term detail with cross-references, hierarchy navigation
and cross-ontology mappings.

Quirks:
- Term IRIs must be *double* URL-encoded in v1/v2 path segments
  (``/ontologies/mondo/terms/http%253A%252F%252F...``).
- The same term appears in many ontologies (imports). ``local=true`` on search and
  ``/terms/findByIdAndIsDefiningOntology`` restrict to the defining ontology.
- v1 term JSON exposes ``obo_xref`` (database/id/description) and ``annotation`` (incl.
  ``database_cross_reference`` and "has exact match"); v2 class JSON adds ``linkedEntities``
  (labels of referenced terms) and skos ``exactMatch``/``closeMatch`` URIs, used by ``map_term``.
- Only cross-references and exact matches name the same concept, so only they become identifiers.
  skos close / narrow / broad matches (and v1 ``closeMatch`` annotations) are other concepts: they are
  listed in ``attributes['loose_matches']`` (and as their own records in ``map_term``), never as
  identifiers, so a MONDO disease does not merge with a broader or narrower term.
- Unknown terms/ontologies return HTTP 404.

Identifier keys: canonical keys from mosaic.ids where they exist (mondo, efo, hp, doid, go,
mesh, omim, orphanet, umls, hgnc, entrez_gene, ensembl_gene, uniprot, reactome), plus
non-canonical keys ``ncit``, ``chebi``, ``uberon``, ``cl``, ``icd10``, ``icd9``, ``icd11``,
``snomedct``, ``medgen``, ``gard``, ``nord``, ``so``, ``eco``, ``mp``, ``zp``, ``ma``, ``hmdb``,
``kegg_compound``, ``kegg_drug``, ``meddra``; ChEBI terms also get ``inchikey`` (and cas/drugbank xrefs).
``map_curie`` below is reused by the Monarch and Alliance connectors.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Literal
from urllib.parse import quote

from pydantic import Field

from ..ids import normalize_identifiers
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, first, parse_offset, strip_html, tool

API = "https://www.ebi.ac.uk/ols4/api"
SITE = "https://www.ebi.ac.uk/ols4"

DEFAULT_ONTOLOGIES = "mondo,efo,doid,hp,go,ncit,chebi,uberon,cl"

# CURIE prefix (upper-cased) -> (identifier key, value style)
#   "curie": keep PREFIX:LOCAL with canonical prefix casing; "local": local part only;
#   "efo": EFO_####### form.
_PREFIX_MAP: dict[str, tuple[str, str, str]] = {
    "MONDO": ("mondo", "curie", "MONDO"),
    "EFO": ("efo", "efo", "EFO"),
    "HP": ("hp", "curie", "HP"),
    "HPO": ("hp", "curie", "HP"),
    "DOID": ("doid", "curie", "DOID"),
    "GO": ("go", "curie", "GO"),
    "MESH": ("mesh", "local", ""),
    "MSH": ("mesh", "local", ""),
    "OMIM": ("omim", "local", ""),
    "MIM": ("omim", "local", ""),
    "ORPHANET": ("orphanet", "curie", "Orphanet"),
    "ORPHA": ("orphanet", "curie", "Orphanet"),
    "ORDO": ("orphanet", "curie", "Orphanet"),
    "ORDO_": ("orphanet", "curie", "Orphanet"),
    "UMLS": ("umls", "local", ""),
    "UMLS_CUI": ("umls", "local", ""),
    "HGNC": ("hgnc", "curie", "HGNC"),
    "NCBIGENE": ("entrez_gene", "local", ""),
    "NCBI_GENE": ("entrez_gene", "local", ""),
    "ENTREZ": ("entrez_gene", "local", ""),
    "ENSEMBL": ("ensembl_gene", "local", ""),
    "UNIPROTKB": ("uniprot", "local", ""),
    "UNIPROT": ("uniprot", "local", ""),
    "REACTOME": ("reactome", "local", ""),
    "PMID": ("pmid", "local", ""),
    "NCIT": ("ncit", "local", ""),
    "NCI": ("ncit", "local", ""),
    "CHEBI": ("chebi", "curie", "CHEBI"),
    "UBERON": ("uberon", "curie", "UBERON"),
    "CL": ("cl", "curie", "CL"),
    "ICD10": ("icd10", "local", ""),
    "ICD10CM": ("icd10", "local", ""),
    "ICD10WHO": ("icd10", "local", ""),
    "ICD9": ("icd9", "local", ""),
    "ICD9CM": ("icd9", "local", ""),
    "ICD11": ("icd11", "local", ""),
    "ICD11.FOUNDATION": ("icd11", "local", ""),
    "SCTID": ("snomedct", "local", ""),
    "SNOMEDCT": ("snomedct", "local", ""),
    "SNOMEDCT_US": ("snomedct", "local", ""),
    "MEDGEN": ("medgen", "local", ""),
    "GARD": ("gard", "local", ""),
    "NORD": ("nord", "local", ""),
    "SO": ("so", "curie", "SO"),
    "ECO": ("eco", "curie", "ECO"),
    "MP": ("mp", "curie", "MP"),
    "ZP": ("zp", "curie", "ZP"),
    "MA": ("ma", "curie", "MA"),
    "ICD-10": ("icd10", "local", ""),
    "ICD-11": ("icd11", "local", ""),
    "ICD-9": ("icd9", "local", ""),
    "CAS": ("cas", "local", ""),
    "CASRN": ("cas", "local", ""),
    "DRUGBANK": ("drugbank", "local", ""),
    "DRUGCENTRAL": ("drugcentral", "local", ""),
    "PUBCHEM.COMPOUND": ("pubchem_cid", "local", ""),
    "PUBCHEM": ("pubchem_cid", "local", ""),
    "CHEMBL.COMPOUND": ("chembl", "local", ""),
    "CHEMBL": ("chembl", "local", ""),
    "HMDB": ("hmdb", "local", ""),
    "KEGG.COMPOUND": ("kegg_compound", "local", ""),
    "KEGG.DRUG": ("kegg_drug", "local", ""),
    "RXNORM": ("rxcui", "local", ""),
    "UNII": ("unii", "local", ""),
    "MEDDRA": ("meddra", "local", ""),
}

# OLS ontology id for CURIE prefixes whose OLS id differs from the lowercased prefix.
_PREFIX_TO_ONTOLOGY = {"ORDO": "ordo", "ORPHANET": "ordo", "ORPHA": "ordo", "NCBITAXON": "ncbitaxon", "MESH": "mesh"}


def split_curie(value: str) -> tuple[str, str] | None:
    """Split ``PREFIX:LOCAL`` (or an OBO/identifiers.org/Orphanet IRI) into (prefix, local)."""
    text = (value or "").strip()
    if not text:
        return None
    if text.startswith("http"):
        m = re.match(r"^https?://purl\.obolibrary\.org/obo/([A-Za-z]+)_(\w+)$", text)
        if m:
            return m.group(1), m.group(2)
        m = re.match(r"^https?://(?:www\.)?orpha\.net/ORDO/Orphanet_(\d+)$", text)
        if m:
            return "Orphanet", m.group(1)
        m = re.match(r"^https?://www\.ebi\.ac\.uk/efo/EFO_(\d+)$", text)
        if m:
            return "EFO", m.group(1)
        m = re.match(r"^https?://identifiers\.org/([^/]+)/(.+)$", text)
        if m:
            return m.group(1), m.group(2).split(":")[-1]
        m = re.match(r"^https?://id\.who\.int/icd/entity/(\d+)$", text)
        if m:
            return "ICD11", m.group(1)
        return None
    if ":" not in text:
        m = re.match(r"^([A-Za-z]+)_(\d+)$", text)
        return (m.group(1), m.group(2)) if m else None
    prefix, local = text.split(":", 1)
    return prefix.strip(), local.strip()


def map_curie(value: str) -> tuple[str, str] | None:
    """Map a CURIE / IRI to (canonical identifier key, normalized value), or None if unknown."""
    parts = split_curie(value)
    if not parts:
        return None
    prefix, local = parts
    spec = _PREFIX_MAP.get(prefix.upper())
    if not spec or not local:
        return None
    key, style, canonical_prefix = spec
    if style == "local":
        return key, local
    if style == "efo":
        return key, f"EFO_{local}"
    return key, f"{canonical_prefix}:{local}"


def identifiers_from_curies(curies: Iterable[str]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Return (first value per canonical key, all values per key) for a list of CURIEs/IRIs."""
    ids: dict[str, str] = {}
    multi: dict[str, list[str]] = {}
    for curie in curies:
        if not isinstance(curie, str):
            continue
        mapped = map_curie(curie)
        if not mapped:
            continue
        key, val = mapped
        ids.setdefault(key, val)
        bucket = multi.setdefault(key, [])
        if val not in bucket:
            bucket.append(val)
    return ids, multi


def _double_encode(iri: str) -> str:
    return quote(quote(iri, safe=""), safe="")


def _record_type(ontology: str | None, prefix: str | None) -> str:
    o = (ontology or prefix or "").lower()
    if o in ("mondo", "doid", "ordo", "orphanet"):
        return "disease"
    if o in ("hp", "mp", "zp"):
        return "phenotype"
    return "ontology_term"


def _term_url(ontology: str | None, iri: str | None) -> str | None:
    if not ontology or not iri:
        return None
    return f"{SITE}/ontologies/{ontology}/classes/{_double_encode(iri)}"


def _xrefs_from_v1(term: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for x in term.get("obo_xref") or []:
        if isinstance(x, dict) and x.get("database") and x.get("id"):
            out.append(f"{x['database']}:{x['id']}")
    ann = term.get("annotation") or {}
    for key in ("database_cross_reference", "has exact match", "exactMatch", "has_dbxref"):
        for v in ann.get(key) or []:
            if isinstance(v, str):
                out.append(v)
    seen: list[str] = []
    for v in out:
        if v not in seen:
            seen.append(v)
    return seen


def _close_matches_v1(term: dict[str, Any]) -> list[str]:
    values = (term.get("annotation") or {}).get("closeMatch") or []
    return list(dict.fromkeys(v for v in values if isinstance(v, str)))


def _term_record(term: dict[str, Any], *, full: bool = False) -> Record:
    obo_id = term.get("obo_id") or term.get("curie")
    iri = term.get("iri")
    ontology = term.get("ontology_name") or term.get("ontologyId")
    prefix = term.get("ontology_prefix") or (obo_id.split(":")[0] if obo_id and ":" in obo_id else None)
    xrefs = _xrefs_from_v1(term) if full else []
    loose = [f"{v} (closeMatch)" for v in _close_matches_v1(term)] if full else []
    ids, multi = identifiers_from_curies([obo_id or iri or "", *xrefs])
    ann = term.get("annotation") or {}
    inchikey = first(ann.get("inchi_key_string") or ann.get("inchikey"))
    if inchikey and isinstance(inchikey, str):
        ids.setdefault("inchikey", inchikey)
    smiles = first(ann.get("smiles_string"))
    synonyms: list[str] = []
    for key in ("exact_synonyms", "synonyms", "related_synonyms", "narrow_synonyms", "broad_synonyms", "synonym"):
        for s in term.get(key) or []:
            if isinstance(s, str) and s not in synonyms:
                synonyms.append(s)
    description = None
    for cit in term.get("obo_definition_citation") or []:
        if isinstance(cit, dict) and cit.get("definition"):
            description = cit["definition"]
            break
    description = description or term.get("description")
    if isinstance(description, list):
        description = " ".join(d for d in description if isinstance(d, str))
    label = term.get("label")
    if isinstance(label, list):
        label = first(label)
    extra_ids = {k: v[1:] for k, v in multi.items() if len(v) > 1}
    attrs: dict[str, Any] = {
        "ontology": ontology,
        "obo_id": obo_id,
        "iri": iri,
        "synonyms": synonyms[:25] or None,
        "is_obsolete": term.get("is_obsolete"),
        "replaced_by": term.get("term_replaced_by"),
        "has_children": term.get("has_children"),
        "is_defining_ontology": term.get("is_defining_ontology"),
        "subsets": term.get("in_subset") if full else None,
        "formula": first(ann.get("generalized_empirical_formula")) if full else None,
        "smiles": smiles if full else None,
        "xrefs": xrefs[:60] or None,
        "loose_matches": loose[:60] or None,
        "additional_ids": extra_ids or None,
    }
    return Record(
        source="ols",
        id=obo_id or term.get("short_form") or iri or "",
        type=_record_type(ontology, prefix),  # type: ignore[arg-type]
        title=label,
        url=_term_url(ontology, iri),
        identifiers=ids,
        summary=strip_html(description) if isinstance(description, str) else None,
        attributes={k: v for k, v in attrs.items() if v not in (None, "", [], {})},
    )


class OlsSearchParams(SearchParams):
    ontology: str | None = Field(
        DEFAULT_ONTOLOGIES,
        description="Comma-separated OLS ontology ids to search (e.g. 'mondo,efo', 'hp', 'chebi'); "
        "empty string searches all ~270 ontologies",
    )
    exact: bool = Field(False, description="Exact label/synonym match only")
    include_obsolete: bool = Field(False, description="Include obsolete terms")
    defining_only: bool = Field(
        True, description="Only return terms from their defining ontology (drops imported copies)"
    )


class TermParams(ToolParams):
    term_id: str = Field(
        description="Term CURIE (MONDO:0004979, HP:0002099, EFO:0000270, GO:0006915, CHEBI:15365, Orphanet:558), "
        "short form (MONDO_0004979) or full IRI"
    )
    ontology: str | None = Field(
        None, description="OLS ontology id (mondo, efo, hp, ...); default: the term's defining ontology"
    )


class HierarchyParams(TermParams):
    direction: Literal["parents", "children", "ancestors", "descendants"] = Field(
        "parents", description="Which related terms to return"
    )
    hierarchical: bool = Field(
        True,
        description="Use hierarchical relations (is_a plus part_of/develops_from where the ontology defines them); "
        "false = strict is_a only",
    )
    limit: int = Field(50, ge=1, le=100, description="Maximum number of terms to return")
    cursor: str | None = Field(None, description="Page cursor from a previous result's next_cursor")


class Ols(Connector):
    name = "ols"
    title = "Ontology Lookup Service (OLS4)"
    category = "biology"
    description = (
        "EMBL-EBI's resolver for ~270 biomedical ontologies (MONDO, EFO, DOID, HP, GO, NCIT, ChEBI, UBERON, "
        "CL, ORDO...): term search, definitions, synonyms, cross-references (MeSH, OMIM, UMLS, ICD10, "
        "Orphanet, DOID, EFO) and hierarchies. Best for normalizing disease/phenotype/anatomy terms and "
        "mapping IDs across ontologies."
    )
    homepage = "https://www.ebi.ac.uk/ols4/"
    docs_url = "https://www.ebi.ac.uk/ols4/help"
    auth = "none"
    rate_limit = 5.0
    health_query = "asthma"
    nih_repositories = ("Disease Ontology Knowledgebase (DO-KB)",)

    @tool(
        description="""
        Search ontology terms by label/synonym/definition across OLS ontologies. Default ontologies:
        mondo, efo, doid, hp, go, ncit, chebi, uberon, cl (set ontology='' for all, or e.g. 'hp' for
        phenotypes only). Returns CURIEs (MONDO:0004979, HP:0002099, ...), labels, definitions,
        synonyms. Use to normalize free-text disease/phenotype/anatomy/cell/chemical names to ontology
        IDs, then get_term for cross-references (MeSH/OMIM/UMLS/ICD10...) or map_term for equivalents.
        Set exact=true for exact label/synonym matches. Example: query='type 2 diabetes', ontology='mondo'.
        """,
        federated=True,
    )
    async def search_terms(self, params: OlsSearchParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        query = {
            "q": params.query,
            "ontology": params.ontology or None,
            "rows": params.limit,
            "start": offset,
            "exact": "true" if params.exact else None,
            "obsoletes": "true" if params.include_obsolete else None,
            "local": "true" if params.defining_only else None,
            "type": "class",
        }
        response = await self.request("GET", f"{API}/search", params=query)
        body = (response.json() or {}).get("response") or {}
        docs = [d for d in body.get("docs") or [] if isinstance(d, dict)]
        total = body.get("numFound")
        nxt = offset + len(docs)
        return ToolResult(
            source=self.name,
            tool="search_terms",
            records=[_term_record(d) for d in docs],
            total=total,
            next_cursor=str(nxt) if docs and total and nxt < total else None,
            request_url=response.url,
        )

    async def _resolve(self, term_id: str, ontology: str | None) -> tuple[dict[str, Any] | None, str]:
        """Fetch the v1 term JSON. Returns (term or None, request_url)."""
        raw = term_id.strip()
        is_iri = raw.startswith("http")
        if not is_iri:
            parts = split_curie(raw)
            if parts:
                prefix = parts[0]
                if prefix.upper() in ("ORPHANET", "ORPHA"):
                    prefix = "ORDO"  # OLS stores Orphanet terms as ORDO:<n>
                raw = f"{prefix}:{parts[1]}"
        key = "iri" if is_iri else "obo_id"
        if ontology:
            response = await self.request(
                "GET", f"{API}/ontologies/{quote(ontology.lower(), safe='')}/terms", params={key: raw},
                allow_status=(404,),
            )
        else:
            response = await self.request(
                "GET", f"{API}/terms/findByIdAndIsDefiningOntology", params={key: raw}, allow_status=(404,)
            )
            terms = ((response.json() or {}).get("_embedded") or {}).get("terms") if response.status != 404 else None
            if not terms:
                # not defined anywhere as "defining": fall back to any ontology that has it
                response = await self.request(
                    "GET", f"{API}/terms", params={key: raw, "size": 20}, allow_status=(404,)
                )
        if response.status == 404:
            return None, response.url
        terms = [t for t in ((response.json() or {}).get("_embedded") or {}).get("terms") or [] if isinstance(t, dict)]
        if not terms:
            return None, response.url
        prefix = raw.split(":")[0].upper() if ":" in raw and not is_iri else ""
        preferred = _PREFIX_TO_ONTOLOGY.get(prefix, prefix.lower())
        best = next((t for t in terms if t.get("is_defining_ontology")), None) or next(
            (t for t in terms if t.get("ontology_name") == preferred), None
        )
        return best or terms[0], response.url

    @tool(
        description="""
        Get one ontology term by CURIE or IRI (MONDO:0004979, HP:0001250, EFO:0000270, DOID:2841,
        GO:0006915, CHEBI:15365, Orphanet:558): label, definition, synonyms, obsolete/replaced-by
        status, subsets and all database cross-references mapped to canonical identifier keys
        (mesh, omim, umls, orphanet, doid, efo, mondo, hp, icd10, ncit, snomedct...). Uses the term's
        defining ontology unless ontology is given.
        """
    )
    async def get_term(self, params: TermParams) -> ToolResult:
        term, url = await self._resolve(params.term_id, params.ontology)
        if term is None:
            return ToolResult(
                source=self.name,
                tool="get_term",
                request_url=url,
                notes=[f"term {params.term_id!r} not found in OLS" + (f" ontology {params.ontology}" if params.ontology else "")],
            )
        return ToolResult(
            source=self.name, tool="get_term", records=[_term_record(term, full=True)], total=1, request_url=url
        )

    @tool(
        description="""
        Navigate an ontology hierarchy from one term: direct parents, direct children, all ancestors
        or all descendants (paged, max 100 per call). hierarchical=true (default) also follows part_of
        style relations; false = strict is_a. Use to broaden/narrow a disease or phenotype query
        (e.g. children of MONDO:0004979 asthma) or to get the subtypes to search other databases with.
        """
    )
    async def term_hierarchy(self, params: HierarchyParams) -> ToolResult:
        term, url = await self._resolve(params.term_id, params.ontology)
        if term is None:
            return ToolResult(
                source=self.name, tool="term_hierarchy", request_url=url,
                notes=[f"term {params.term_id!r} not found in OLS"],
            )
        ontology = term.get("ontology_name")
        iri = term.get("iri") or ""
        rel = params.direction
        if params.hierarchical:
            rel = "hierarchical" + rel[0].upper() + rel[1:]
        page = parse_offset(params.cursor)
        response = await self.request(
            "GET",
            f"{API}/ontologies/{quote(ontology or '', safe='')}/terms/{_double_encode(iri)}/{rel}",
            params={"size": params.limit, "page": page},
            allow_status=(404,),
        )
        if response.status == 404:
            return ToolResult(
                source=self.name, tool="term_hierarchy", request_url=response.url,
                notes=[f"no {params.direction} for {term.get('obo_id')}"],
            )
        payload = response.json() or {}
        terms = [t for t in (payload.get("_embedded") or {}).get("terms") or [] if isinstance(t, dict)]
        info = payload.get("page") or {}
        total = info.get("totalElements")
        pages = info.get("totalPages") or 0
        records = [_term_record(t) for t in terms]
        for r in records:
            r.attributes["relation"] = params.direction
            r.attributes["relative_of"] = term.get("obo_id")
        notes = [] if records else [f"{term.get('obo_id')} has no {params.direction} in {ontology}"]
        return ToolResult(
            source=self.name,
            tool="term_hierarchy",
            records=records,
            total=total if total is not None else len(records),
            next_cursor=str(page + 1) if page + 1 < pages else None,
            request_url=response.url,
            notes=notes,
            data={"term": term.get("obo_id"), "label": term.get("label"), "ontology": ontology, "relation": rel},
        )

    @tool(
        description="""
        Map one ontology term to equivalent / cross-referenced terms in other ontologies and
        vocabularies (e.g. MONDO:0004979 -> DOID:2841, EFO:0000270, HP:0002099, MeSH D001249,
        UMLS C0004096, ICD10 J45, NCIT C28397, Orphanet). Returns the source term (with every mapped
        canonical identifier) followed by one record per mapped term, with labels where OLS knows them
        and the mapping relation (xref, exactMatch, closeMatch) plus its curation source. Use to hop
        between disease vocabularies before querying other databases.
        """
    )
    async def map_term(self, params: TermParams) -> ToolResult:
        term, url = await self._resolve(params.term_id, params.ontology)
        if term is None:
            return ToolResult(
                source=self.name, tool="map_term", request_url=url,
                notes=[f"term {params.term_id!r} not found in OLS"],
            )
        ontology = term.get("ontology_name") or ""
        iri = term.get("iri") or ""
        response = await self.request(
            "GET",
            f"{API}/v2/ontologies/{quote(ontology, safe='')}/classes/{_double_encode(iri)}",
            allow_status=(404,),
        )
        v2 = response.json() if response.status != 404 else {}
        v2 = v2 or {}
        linked = v2.get("linkedEntities") or {}
        mappings: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(value: str, relation: str, sources: Any = None) -> None:
            parts = split_curie(value)
            curie = f"{parts[0]}:{parts[1]}" if parts else value
            if curie in seen or curie == term.get("obo_id"):
                return
            seen.add(curie)
            info = linked.get(curie) or linked.get(value) or {}
            mapped = map_curie(curie)
            mappings.append(
                {
                    "curie": curie,
                    "relation": relation,
                    "label": first(info.get("label")),
                    "defined_by": first(info.get("definedBy")),
                    "identifier_key": mapped[0] if mapped else None,
                    "identifier": mapped[1] if mapped else None,
                    "source": sources,
                }
            )

        for key, relation in (
            ("http://www.geneontology.org/formats/oboInOwl#hasDbXref", "xref"),
            ("http://www.w3.org/2004/02/skos/core#exactMatch", "exactMatch"),
            ("http://www.w3.org/2004/02/skos/core#closeMatch", "closeMatch"),
            ("http://www.w3.org/2004/02/skos/core#narrowMatch", "narrowMatch"),
            ("http://www.w3.org/2004/02/skos/core#broadMatch", "broadMatch"),
        ):
            values = v2.get(key)
            if values is None:
                continue
            for v in values if isinstance(values, list) else [values]:
                if isinstance(v, str):
                    add(v, relation)
                elif isinstance(v, dict) and isinstance(v.get("value"), str):
                    srcs: list[str] = []
                    for ax in v.get("axioms") or []:
                        src = (ax or {}).get("http://www.geneontology.org/formats/oboInOwl#source")
                        for one in src if isinstance(src, list) else [src] if src else []:
                            if isinstance(one, str) and not one.startswith("http") and one not in srcs:
                                srcs.append(one)
                    add(v["value"], relation, srcs[:5] or None)
        if not v2:  # v2 unavailable: fall back to v1 xrefs
            for x in _xrefs_from_v1(term):
                add(x, "xref")
            for x in _close_matches_v1(term):
                add(x, "closeMatch")

        source_record = _term_record(term, full=True)
        exact = [m["curie"] for m in mappings if m["relation"] in ("xref", "exactMatch")]
        all_ids, _ = identifiers_from_curies([source_record.id] + exact)
        # set after the Record was built, so normalized here (Record normalizes only on construction)
        source_record.identifiers = normalize_identifiers({**all_ids, **source_record.identifiers})
        loose = [f"{m['curie']} ({m['relation']})" for m in mappings if m["relation"] not in ("xref", "exactMatch")]
        if loose:
            source_record.attributes["loose_matches"] = list(
                dict.fromkeys([*source_record.attributes.get("loose_matches", []), *loose])
            )
        records = [source_record]
        for m in mappings:
            if not m["identifier_key"] and not m["label"]:
                continue
            prefix = m["curie"].split(":")[0] if ":" in m["curie"] else None
            ont = m["defined_by"]
            ids = {m["identifier_key"]: m["identifier"]} if m["identifier_key"] else {}
            records.append(
                Record(
                    source=self.name,
                    id=m["curie"],
                    type=_record_type(ont, prefix),  # type: ignore[arg-type]
                    title=m["label"],
                    url=_term_url(ont, (linked.get(m["curie"]) or {}).get("iri")) if ont else None,
                    identifiers=ids,
                    attributes={
                        k: v
                        for k, v in {
                            "relation": m["relation"],
                            "mapped_from": term.get("obo_id"),
                            "ontology": ont,
                            "mapping_source": m["source"],
                        }.items()
                        if v
                    },
                )
            )
        return ToolResult(
            source=self.name,
            tool="map_term",
            records=records,
            total=len(mappings),
            request_url=response.url,
            data={
                "term": term.get("obo_id"),
                "mappings": [
                    {"curie": m["curie"], "relation": m["relation"], "label": m["label"]} for m in mappings[:100]
                ],
            },
            notes=[] if mappings else [f"OLS lists no cross-references for {term.get('obo_id')}"],
        )
