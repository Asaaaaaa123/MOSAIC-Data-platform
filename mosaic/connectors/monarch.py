"""Monarch Initiative knowledge graph connector (API v3).

Docs: https://api-v3.monarchinitiative.org/v3/docs (OpenAPI at /openapi.json).
Auth: none. Rate limits: none published; the service is a single Solr-backed deployment that
returns transient 502s under load (the http layer retries), so we stay at 3 req/s.

Integrates MONDO diseases, HPO phenotypes (incl. HPO annotations), HGNC/MGI/ZFIN/... genes,
OMIM/Orphanet/ClinVar/Alliance associations and cross-species phenotype similarity.

Quirks:
- Native ids are MONDO:, HP:, HGNC:, MGI:, ZFIN:, RGD:, ... ``/entity/{id}`` 404s for xref-only ids
  (OMIM:, Orphanet:, MESH:); get_entity then resolves through ``/search`` (which matches xrefs).
  NCBIGene: ids are not resolvable (genes are keyed by HGNC).
- ``/entity/{id}/{association category}`` returns associations where the entity is subject *or*
  object; ``direct=false`` also includes descendants of the entity (e.g. subtypes of a disease).
- ``limit`` max 500 per page; offset pagination.
- Entity ``has_phenotype`` and ``has_phenotype_label`` lists are not index-aligned, so only the ids
  are exposed; use ``associations`` (disease_phenotype) for labeled phenotypes.
- ``/semsim/search/{termset}/{group}`` ranks diseases/genes by phenotype-profile similarity.

Identifiers use ols.map_curie (mondo, hp, hgnc, omim, orphanet, doid, mesh, umls, efo, ncit,
icd10, medgen, gard, ensembl_gene, ...); non-human genes use lowercase model-organism keys
(``mgi``, ``rgd``, ``zfin``, ``flybase``, ``wormbase``, ``sgd``, ``xenbase``).
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

from pydantic import Field

from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, first, parse_offset, strip_html, tool
from .ols import identifiers_from_curies, map_curie

API = "https://api-v3.monarchinitiative.org/v3/api"
SITE = "https://monarchinitiative.org"

_MOD_KEYS = {
    "MGI": "mgi",
    "RGD": "rgd",
    "ZFIN": "zfin",
    "FB": "flybase",
    "WB": "wormbase",
    "SGD": "sgd",
    "XENBASE": "xenbase",
    "DICTYBASE": "dictybase",
    "POMBASE": "pombase",
}

ASSOCIATION_CATEGORIES = (
    "biolink:DiseaseToPhenotypicFeatureAssociation",
    "biolink:GeneToPhenotypicFeatureAssociation",
    "biolink:CausalGeneToDiseaseAssociation",
    "biolink:CorrelatedGeneToDiseaseAssociation",
    "biolink:VariantToDiseaseAssociation",
    "biolink:VariantToGeneAssociation",
    "biolink:GenotypeToDiseaseAssociation",
    "biolink:GenotypeToPhenotypicFeatureAssociation",
    "biolink:GeneToGeneHomologyAssociation",
    "biolink:PairwiseGeneToGeneInteraction",
    "biolink:GeneToPathwayAssociation",
    "biolink:MacromolecularMachineToBiologicalProcessAssociation",
    "biolink:MacromolecularMachineToMolecularActivityAssociation",
    "biolink:MacromolecularMachineToCellularComponentAssociation",
    "biolink:GeneToExpressionSiteAssociation",
    "biolink:ChemicalToPathwayAssociation",
    "biolink:CaseToDiseaseAssociation",
    "biolink:CaseToPhenotypicFeatureAssociation",
)

AssociationCategory = Literal[
    "disease_phenotype",
    "gene_phenotype",
    "causal_gene_disease",
    "correlated_gene_disease",
    "variant_disease",
    "variant_gene",
    "genotype_disease",
    "genotype_phenotype",
    "gene_orthology",
    "gene_interaction",
    "gene_pathway",
    "gene_process",
    "gene_function",
    "gene_location",
    "gene_expression",
    "chemical_pathway",
    "case_disease",
    "case_phenotype",
]

_CATEGORY_ALIASES: dict[str, str] = dict(zip(AssociationCategory.__args__, ASSOCIATION_CATEGORIES))  # type: ignore[attr-defined]


def entity_identifiers(curie: str | None, xrefs: list[str] | None = None, *, symbol: str | None = None,
                       taxon: str | None = None) -> dict[str, str]:
    """Canonical identifiers for a Monarch node id + xrefs."""
    ids, _ = identifiers_from_curies([curie or "", *(xrefs or [])])
    if curie and ":" in curie:
        prefix, local = curie.split(":", 1)
        key = _MOD_KEYS.get(prefix.upper())
        if key:
            ids.setdefault(key, curie)
    if symbol and (taxon in (None, "NCBITaxon:9606") or (curie or "").startswith("HGNC:")):
        ids.setdefault("gene_symbol", symbol)
    return ids


def _record_type(category: str | None) -> str:
    c = (category or "").replace("biolink:", "")
    return {
        "Disease": "disease",
        "PhenotypicFeature": "phenotype",
        "Gene": "gene",
        "Protein": "protein",
        "SequenceVariant": "variant",
        "Pathway": "pathway",
        "ChemicalEntity": "compound",
        "SmallMolecule": "compound",
        "Drug": "drug",
        "BiologicalProcess": "ontology_term",
        "MolecularActivity": "ontology_term",
        "CellularComponent": "ontology_term",
        "AnatomicalEntity": "ontology_term",
        "Cell": "ontology_term",
    }.get(c, "other")


def _node_record(node: dict[str, Any], *, full: bool = False) -> Record:
    curie = node.get("id") or ""
    synonyms = node.get("exact_synonym") or node.get("synonym") or []
    attrs: dict[str, Any] = {
        "category": node.get("category"),
        "symbol": node.get("symbol"),
        "full_name": node.get("full_name"),
        "taxon": node.get("in_taxon_label"),
        "taxon_id": node.get("in_taxon"),
        "synonyms": synonyms[:20] or None,
        "xrefs": (node.get("xref") or [])[:40] or None,
        "phenotype_count": node.get("has_phenotype_count"),
        "deprecated": node.get("deprecated"),
    }
    if full:
        inheritance = node.get("inheritance") or {}
        hierarchy = node.get("node_hierarchy") or {}
        attrs.update(
            {
                "subsets": node.get("subsets"),
                "inheritance": {"id": inheritance.get("id"), "name": inheritance.get("name")} if inheritance else None,
                "causal_genes": [
                    {"id": g.get("id"), "symbol": g.get("name") or g.get("symbol")}
                    for g in node.get("causal_gene") or []
                    if isinstance(g, dict)
                ]
                or None,
                "causes_disease": [
                    {"id": d.get("id"), "name": d.get("name")} for d in (node.get("causes_disease") or [])[:30] if isinstance(d, dict)
                ]
                or None,
                # has_phenotype and has_phenotype_label are NOT index-aligned upstream: never zip them
                "phenotype_ids_sample": (node.get("has_phenotype") or [])[:30] or None,
                "super_classes": [
                    {"id": x.get("id"), "name": x.get("name")} for x in (hierarchy.get("super_classes") or [])[:10] if isinstance(x, dict)
                ]
                or None,
                "sub_classes": [
                    {"id": x.get("id"), "name": x.get("name")} for x in (hierarchy.get("sub_classes") or [])[:25] if isinstance(x, dict)
                ]
                or None,
                "association_counts": {
                    (a.get("category") or a.get("label") or "").replace("biolink:", ""): a.get("count")
                    for a in node.get("association_counts") or []
                    if isinstance(a, dict)
                }
                or None,
                "provided_by": node.get("provided_by"),
            }
        )
    ids = entity_identifiers(curie, node.get("xref"), symbol=node.get("symbol"), taxon=node.get("in_taxon"))
    return Record(
        source="monarch",
        id=curie,
        type=_record_type(node.get("category")),  # type: ignore[arg-type]
        title=node.get("name") or node.get("symbol"),
        url=f"{SITE}/{curie}" if curie else None,
        identifiers=ids,
        summary=strip_html(node.get("description")),
        attributes={k: v for k, v in attrs.items() if v not in (None, "", [], {})},
    )


def _association_record(a: dict[str, Any], focus: str | None) -> Record:
    subject, obj = a.get("subject"), a.get("object")
    # the "other" side of the association relative to the queried entity
    focus_is_subject = focus is not None and (subject == focus or focus in (a.get("subject_closure") or []))
    other = obj if focus_is_subject or focus is None else subject
    other_label = a.get("object_label") if other == obj else a.get("subject_label")
    other_cat = a.get("object_category") if other == obj else a.get("subject_category")
    other_taxon = a.get("object_taxon") if other == obj else a.get("subject_taxon")
    pubs = [p for p in a.get("publications") or [] if isinstance(p, str)]
    pmids = [p.split(":", 1)[1] for p in pubs if p.upper().startswith("PMID:")]
    ids = entity_identifiers(other, None, symbol=other_label if (other_cat or "").endswith("Gene") else None, taxon=other_taxon)
    original = a.get("original_object") if other == obj else a.get("original_subject")
    if original and original != other:
        for key, value in entity_identifiers(original, None).items():
            ids.setdefault(key, value)
    if pmids:
        ids["pmid"] = pmids[0]
    predicate = (a.get("predicate") or "").replace("biolink:", "")
    attrs = {
        "subject": subject,
        "subject_label": a.get("subject_label"),
        "predicate": predicate,
        "object": obj,
        "object_label": a.get("object_label"),
        "category": (a.get("category") or "").replace("biolink:", ""),
        "negated": a.get("negated") or None,
        "evidence": a.get("has_evidence"),
        "evidence_count": a.get("evidence_count"),
        "publications": pubs[:15] or None,
        "frequency": a.get("frequency_qualifier_label") or a.get("frequency_qualifier"),
        "onset": a.get("onset_qualifier_label"),
        "sex": a.get("sex_qualifier_label"),
        "has_percentage": a.get("has_percentage"),
        "primary_source": (a.get("primary_knowledge_source") or "").replace("infores:", "") or None,
        "subject_taxon": a.get("subject_taxon_label"),
        "object_taxon": a.get("object_taxon_label"),
        "original_subject": a.get("original_subject"),
        "other_category": (other_cat or "").replace("biolink:", "") or None,
    }
    return Record(
        source="monarch",
        id=a.get("id") or f"{subject}|{predicate}|{obj}",
        type="association",
        title=f"{a.get('subject_label') or subject} {predicate.replace('_', ' ')} {a.get('object_label') or obj}",
        url=f"{SITE}/{other}" if other else None,
        identifiers=ids,
        attributes={k: v for k, v in attrs.items() if v not in (None, "", [], {})},
    )


class MonarchSearchParams(SearchParams):
    category: Literal["any", "disease", "gene", "phenotype", "disease_gene_phenotype"] = Field(
        "disease_gene_phenotype",
        description="Entity category filter: disease (biolink:Disease), gene, phenotype (biolink:PhenotypicFeature), "
        "disease_gene_phenotype (all three, default) or any",
    )
    taxon: str | None = Field(None, description="Restrict genes to a taxon, e.g. NCBITaxon:9606 (human), NCBITaxon:10090")


class EntityParams(ToolParams):
    entity_id: str = Field(
        description="Monarch node CURIE: MONDO:0007947, HP:0001166, HGNC:3603, MGI:95489, ZFIN:ZDB-GENE-...; "
        "xref ids (OMIM:154700, Orphanet:558, MESH:D008382) are resolved to the matching MONDO disease"
    )


class AssociationParams(ToolParams):
    entity_id: str = Field(description="Node CURIE the associations are about (subject or object), e.g. MONDO:0007947, HGNC:3603, HP:0001166")
    category: AssociationCategory = Field(
        description="Association type: disease_phenotype (disease->HPO phenotypes), gene_phenotype "
        "(gene<->phenotype, incl. phenotype->genes), causal_gene_disease (Mendelian gene<->disease), "
        "correlated_gene_disease (GWAS/other), variant_disease, genotype_disease, gene_orthology, "
        "gene_interaction, gene_pathway, gene_process/gene_function/gene_location (GO), gene_expression, case_phenotype ..."
    )
    direct: bool = Field(
        True, description="Only associations of this exact entity; false also includes its descendants (e.g. disease subtypes)"
    )
    traverse_orthologs: bool = Field(False, description="Also include associations inferred via orthologous genes")
    limit: int = Field(25, ge=1, le=100, description="Maximum associations to return")
    cursor: str | None = Field(None, description="Offset cursor from a previous result's next_cursor")


class SemsimParams(ToolParams):
    phenotypes: list[str] = Field(
        min_length=1, max_length=100, description="HPO term ids describing a patient/model, e.g. ['HP:0001166','HP:0000545','HP:0002616']"
    )
    target: Literal["Human Diseases", "Human Genes", "Mouse Genes", "Rat Genes", "Zebrafish Genes", "C. Elegans Genes"] = Field(
        "Human Diseases", description="What to rank by phenotype-profile similarity"
    )
    limit: int = Field(10, ge=1, le=50, description="Number of ranked matches to return")


class Monarch(Connector):
    name = "monarch"
    title = "Monarch Initiative"
    category = "biology"
    description = (
        "Integrated knowledge graph of diseases (MONDO), phenotypes (HPO), genes across species and their "
        "associations, built from OMIM, Orphanet, HPO annotations, ClinVar, Alliance and more. Best for "
        "disease<->phenotype<->gene relationships, disease cross-references and phenotype-driven differential diagnosis."
    )
    homepage = "https://monarchinitiative.org/"
    docs_url = "https://api-v3.monarchinitiative.org/v3/docs"
    auth = "none"
    rate_limit = 3.0
    health_query = "Marfan syndrome"
    nih_repositories = ("Monarch Initiative", "Human Phenotype Ontology (HPO)")

    @tool(
        description="""
        Search Monarch entities by name/synonym/xref: diseases (MONDO ids), genes (HGNC for human, MGI,
        ZFIN, RGD, ... for model organisms) and HPO phenotypes (HP ids). Default searches all three
        categories; set category='disease'|'gene'|'phenotype' to narrow, taxon='NCBITaxon:9606' for human
        genes. Also resolves xref ids typed as the query (e.g. 'OMIM:154700'). Follow with get_entity
        for cross-references or associations for linked phenotypes/genes/diseases.
        """,
        federated=True,
    )
    async def search(self, params: MonarchSearchParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        cats = {
            "any": [],
            "disease": ["biolink:Disease"],
            "gene": ["biolink:Gene"],
            "phenotype": ["biolink:PhenotypicFeature"],
            "disease_gene_phenotype": ["biolink:Disease", "biolink:Gene", "biolink:PhenotypicFeature"],
        }[params.category]
        query: list[tuple[str, Any]] = [("q", params.query)]
        query += [("category", c) for c in cats]
        if params.taxon:
            query.append(("in_taxon", params.taxon))
        query += [("limit", params.limit), ("offset", offset)]
        response = await self.request("GET", f"{API}/search", params=query)
        payload = response.json() or {}
        items = [i for i in payload.get("items") or [] if isinstance(i, dict)]
        total = payload.get("total")
        nxt = offset + len(items)
        return ToolResult(
            source=self.name,
            tool="search",
            records=[_node_record(i) for i in items],
            total=total,
            next_cursor=str(nxt) if items and total and nxt < total else None,
            request_url=response.url,
        )

    @tool(
        description="""
        Get one Monarch entity: a disease (MONDO:0007947), phenotype (HP:0001166) or gene (HGNC:3603,
        MGI:95489, ...). Returns name, description, synonyms, all cross-references mapped to canonical ids
        (mondo, omim, orphanet, doid, mesh, umls, hp, hgnc, ensembl_gene, icd10, ncit...), inheritance,
        causal genes, sample phenotype ids, parent/child classes and association counts per type. OMIM:,
        Orphanet: and MESH: ids are resolved to the corresponding MONDO disease.
        """
    )
    async def get_entity(self, params: EntityParams) -> ToolResult:
        entity_id = params.entity_id.strip()
        response = await self.request("GET", f"{API}/entity/{quote(entity_id, safe=':')}", allow_status=(404,))
        notes: list[str] = []
        if response.status == 404:
            search = await self.request("GET", f"{API}/search", params={"q": entity_id, "limit": 5})
            items = [i for i in (search.json() or {}).get("items") or [] if isinstance(i, dict)]
            match = next(
                (i for i in items if entity_id.lower() in {x.lower() for x in i.get("xref") or []}), None
            )
            if not match:
                return ToolResult(
                    source=self.name,
                    tool="get_entity",
                    request_url=search.url,
                    notes=[f"entity {entity_id!r} not found in Monarch (native ids: MONDO, HP, HGNC, MGI, ZFIN, ...)"],
                )
            notes.append(f"{entity_id} resolved via cross-reference to {match.get('id')}")
            response = await self.request(
                "GET", f"{API}/entity/{quote(match['id'], safe=':')}", allow_status=(404,)
            )
            if response.status == 404:
                return ToolResult(source=self.name, tool="get_entity", records=[_node_record(match)], total=1,
                                  request_url=response.url, notes=notes)
        node = response.json() or {}
        return ToolResult(
            source=self.name,
            tool="get_entity",
            records=[_node_record(node, full=True)],
            total=1,
            request_url=response.url,
            notes=notes,
        )

    @tool(
        description="""
        List associations of one entity from the Monarch graph, with evidence codes, publications (PMIDs),
        frequency/onset qualifiers and the primary knowledge source (OMIM, Orphanet, HPO, ClinGen, Alliance...).
        Examples: entity_id='MONDO:0007947', category='disease_phenotype' (phenotypes of Marfan);
        'HGNC:3603' + 'causal_gene_disease' (diseases caused by FBN1); 'HP:0001166' + 'gene_phenotype'
        (genes with arachnodactyly); 'HGNC:3603' + 'gene_orthology'. Each record's identifiers describe the
        entity on the other side of the association. direct=false includes descendant terms.
        """
    )
    async def associations(self, params: AssociationParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        category = _CATEGORY_ALIASES[params.category]
        entity_id = params.entity_id.strip()
        response = await self.request(
            "GET",
            f"{API}/entity/{quote(entity_id, safe=':')}/{quote(category, safe=':')}",
            params={
                "limit": params.limit,
                "offset": offset,
                "direct": params.direct,
                "traverse_orthologs": "true" if params.traverse_orthologs else None,
            },
            allow_status=(404, 422),
        )
        if response.status in (404, 422):
            return ToolResult(
                source=self.name, tool="associations", request_url=response.url,
                notes=[f"no {params.category} associations for {entity_id} (entity unknown or category not applicable)"],
            )
        payload = response.json() or {}
        items = [i for i in payload.get("items") or [] if isinstance(i, dict)]
        total = payload.get("total")
        nxt = offset + len(items)
        notes = [] if items else [f"no {params.category} associations for {entity_id}"]
        return ToolResult(
            source=self.name,
            tool="associations",
            records=[_association_record(i, entity_id) for i in items],
            total=total,
            next_cursor=str(nxt) if items and total and nxt < total else None,
            request_url=response.url,
            notes=notes,
        )

    @tool(
        description="""
        Phenotype-driven ranking (semantic similarity, Monarch semsim): given a set of HPO terms (a patient
        or model phenotype profile), return the most similar human diseases (default) or genes of human /
        mouse / rat / zebrafish / C. elegans, scored by ancestor information content. Use for differential
        diagnosis or finding model organisms with a matching phenotype. Example:
        phenotypes=['HP:0001166','HP:0000545','HP:0002616'] (arachnodactyly, myopia, aortic root aneurysm).
        """
    )
    async def phenotype_similarity_search(self, params: SemsimParams) -> ToolResult:
        terms = [t.strip().upper() for t in params.phenotypes if t and t.strip()]
        termset = ",".join(dict.fromkeys(terms))
        response = await self.request(
            "GET",
            f"{API}/semsim/search/{quote(termset, safe=':,')}/{quote(params.target)}",
            params={"limit": params.limit},
            timeout=90,
            allow_status=(404, 422),
        )
        if response.status in (404, 422):
            return ToolResult(
                source=self.name, tool="phenotype_similarity_search", request_url=response.url,
                notes=[f"Monarch could not score these phenotypes: {response.text[:200]}"],
            )
        matches = [m for m in response.json() or [] if isinstance(m, dict)]
        records = []
        for rank, m in enumerate(matches, start=1):
            rec = _node_record(m.get("subject") or {})
            sim = m.get("similarity") or {}
            rec.attributes.update(
                {
                    k: v
                    for k, v in {
                        "rank": rank,
                        "score": m.get("score"),
                        "average_score": sim.get("average_score"),
                        "best_score": sim.get("best_score"),
                        "metric": sim.get("metric"),
                    }.items()
                    if v is not None
                }
            )
            records.append(rec)
        return ToolResult(
            source=self.name,
            tool="phenotype_similarity_search",
            records=records,
            request_url=response.url,
            notes=[] if records else ["no matches"],
            data={"query_phenotypes": terms, "target": params.target},
        )
