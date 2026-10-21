"""Reactome pathway knowledgebase connector (Content Service + Analysis Service).

Docs: https://reactome.org/ContentService (OpenAPI at /ContentService/v3/api-docs) and
https://reactome.org/AnalysisService (OpenAPI at /AnalysisService/v3/api-docs).
Auth: none. Rate limits: not formally documented; Reactome asks for reasonable use, so we
stay at 5 req/s.

Quirks:
- ``/search/query`` ignores ``start``; ``/search/query/paginated`` (``rowCount`` + 1-based
  ``page``) paginates. A query with no hits returns HTTP 404 with a JSON "No entries found" body.
- Search names/summations contain ``<span class="highlighting">`` markup; stripped here.
- ``/data/mapping/{resource}/{id}/pathways`` returns lowest-level pathways for UniProt, ENSEMBL,
  ChEBI (bare digits) and "NCBI Gene" ids; it 404s when nothing maps. Gene symbols are not
  supported there, so symbols go through the Analysis Service ``/identifier/{id}/projection``.
- ``/identifiers/projection`` takes a text/plain body (one identifier per line) and returns a
  ``token`` that can be reused for ``/token/{token}/...`` follow-ups (we fetch ``notFound``).
- Non-canonical identifier key used: ``chebi`` (CHEBI:#####) for small-molecule search hits.
- A pathway's GO biological process and its diseases are related entities, kept in
  ``attributes['go_biological_process']`` / ``attributes['diseases']``: as ``go`` / ``doid``
  identifiers every cancer pathway would merge via DOID:162, and pathways via shared GO terms.
- ``/data/participants/{id}/referenceEntities`` can be several MB for large pathways, so
  ``get_pathway`` only calls it when ``include_participants`` is true.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import quote

from pydantic import Field

from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, first, parse_offset, strip_html, tool

CONTENT = "https://reactome.org/ContentService"
ANALYSIS = "https://reactome.org/AnalysisService"
SITE = "https://reactome.org"

_UNIPROT = re.compile(r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})(?:-\d+)?$")
_STID = re.compile(r"^R-[A-Z]{3}-\d+(?:\.\d+)?$", re.IGNORECASE)


class ReactomeSearchParams(SearchParams):
    species: str | None = Field(
        "Homo sapiens",
        description="Species name filter, e.g. 'Homo sapiens', 'Mus musculus'; empty string for all species",
    )
    types: str = Field(
        "Pathway",
        description="Comma-separated Reactome result types: Pathway, Reaction, Protein, Complex, "
        "'Small molecule', Set, Gene, 'DNA Sequence', 'RNA Sequence'",
    )


class GetPathwayParams(ToolParams):
    pathway_id: str = Field(description="Reactome stable ID (e.g. R-HSA-109581, version suffix optional) or numeric dbId")
    include_participants: bool = Field(
        False,
        description="Also fetch the pathway's molecular participants (UniProt/ChEBI/Ensembl reference "
        "entities): counts by database plus up to 100 UniProt accessions. Slower for large pathways.",
    )


class PathwaysForEntityParams(ToolParams):
    identifier: str = Field(
        description="Molecule identifier: UniProt accession (P04637), Ensembl gene (ENSG00000141510), "
        "ChEBI (CHEBI:15377 or 15377 with id_type=chebi), NCBI Gene ID (7157) or HGNC gene symbol (TP53)"
    )
    id_type: Literal["auto", "uniprot", "ensembl", "chebi", "ncbi_gene", "gene_symbol"] = Field(
        "auto", description="Identifier type; 'auto' detects UniProt/Ensembl/ChEBI/NCBI Gene/symbol from the format"
    )
    species: str = Field("9606", description="NCBI taxon ID or species name for the pathways (default human 9606)")
    limit: int = Field(50, ge=1, le=100, description="Maximum number of pathways to return")


class EnrichmentParams(ToolParams):
    identifiers: list[str] = Field(
        min_length=1,
        max_length=3000,
        description="Gene/protein/compound identifiers to test: HGNC symbols, UniProt, Ensembl, NCBI Gene, ChEBI. "
        "Mixed types are allowed.",
    )
    projection: bool = Field(
        True, description="Project non-human identifiers onto human pathways (recommended for gene lists)"
    )
    include_disease: bool = Field(True, description="Include disease pathways in the results")
    p_value: float | None = Field(None, gt=0, le=1, description="Only return pathways with entities p-value <= this")
    limit: int = Field(20, ge=1, le=100, description="Number of top pathways to return (sorted by p-value)")
    cursor: str | None = Field(None, description="Page number from a previous result's next_cursor")


_HIGHLIGHT = re.compile(r"<span class=\"highlighting\"\s*>|</span>", re.IGNORECASE)


def _clean(text: str | None) -> str | None:
    """Drop search-highlight spans without inserting spaces, then strip remaining HTML."""
    if not text:
        return text
    return strip_html(_HIGHLIGHT.sub("", text))


def _go_id(value: Any) -> str | None:
    if isinstance(value, dict):
        acc = value.get("accession")
        if acc:
            return acc if str(acc).startswith("GO:") else f"GO:{acc}"
    return None


def _pathway_url(st_id: str) -> str:
    return f"{SITE}/content/detail/{st_id}"


def _event_record(item: dict[str, Any], **attributes: Any) -> Record:
    """Record from a Content Service event/pathway object (mapping endpoint, sub-events)."""
    st_id = item.get("stId") or str(item.get("dbId") or "")
    attrs = {
        "species": item.get("speciesName"),
        "schema_class": item.get("schemaClass") or item.get("className"),
        "is_in_disease": item.get("isInDisease"),
        "is_inferred": item.get("isInferred"),
        "has_diagram": item.get("hasDiagram"),
        "pathway_browser_url": f"{SITE}/PathwayBrowser/#/{st_id}" if st_id else None,
        **attributes,
    }
    return Record(
        source="reactome",
        id=st_id,
        type="pathway" if (item.get("schemaClass") or item.get("className") or "Pathway") in ("Pathway", "TopLevelPathway") else "other",
        title=strip_html(item.get("displayName") or first(item.get("name"))),
        url=_pathway_url(st_id) if st_id else None,
        identifiers={"reactome": st_id},
        date=item.get("releaseDate"),
        attributes={k: v for k, v in attrs.items() if v not in (None, "", [])},
    )


def _search_record(entry: dict[str, Any]) -> Record:
    st_id = entry.get("stId") or entry.get("id") or entry.get("dbId")
    etype = entry.get("type") or entry.get("exactType")
    identifiers: dict[str, Any] = {"reactome": st_id}
    db = (entry.get("databaseName") or "").lower()
    ref = entry.get("referenceIdentifier")
    if ref and db == "uniprot":
        identifiers["uniprot"] = ref
        identifiers["gene_symbol"] = _clean(entry.get("referenceName"))
    elif ref and db == "chebi":
        identifiers["chebi"] = f"CHEBI:{ref}" if not str(ref).upper().startswith("CHEBI:") else ref
    elif ref and db == "ensembl":
        identifiers["ensembl_gene"] = ref
    rtype = {
        "Pathway": "pathway",
        "Reaction": "other",
        "Protein": "protein",
        "Small molecule": "compound",
        "Gene": "gene",
    }.get(etype or "", "other")
    return Record(
        source="reactome",
        id=st_id,
        type=rtype,  # type: ignore[arg-type]
        title=_clean(entry.get("name")),
        url=_pathway_url(st_id),
        identifiers=identifiers,
        summary=_clean(entry.get("summation")),
        attributes={
            k: v
            for k, v in {
                "reactome_type": entry.get("exactType") or etype,
                "species": first(entry.get("species")),
                "is_disease": entry.get("isDisease"),
                "compartments": entry.get("compartmentNames"),
                "has_ehld": entry.get("hasEHLD"),
                "reference_db": entry.get("databaseName"),
            }.items()
            if v not in (None, "", [])
        },
    )


def _analysis_pathway_record(p: dict[str, Any]) -> Record:
    st_id = p.get("stId") or str(p.get("dbId") or "")
    ent = p.get("entities") or {}
    rx = p.get("reactions") or {}
    species = p.get("species") or {}
    return Record(
        source="reactome",
        id=st_id,
        type="pathway",
        title=p.get("name"),
        url=_pathway_url(st_id),
        identifiers={"reactome": st_id},
        attributes={
            k: v
            for k, v in {
                "species": species.get("name"),
                "p_value": ent.get("pValue"),
                "fdr": ent.get("fdr"),
                "entities_found": ent.get("found"),
                "entities_total": ent.get("total"),
                "entities_ratio": ent.get("ratio"),
                "reactions_found": rx.get("found"),
                "reactions_total": rx.get("total"),
                "lowest_level_pathway": p.get("llp"),
                "in_disease": p.get("inDisease"),
            }.items()
            if v is not None
        },
    )


class Reactome(Connector):
    name = "reactome"
    title = "Reactome"
    category = "biology"
    description = (
        "Expert-curated, peer-reviewed knowledgebase of human biological pathways and reactions "
        "(~2,700 human pathways, ~11k proteins) with orthology-inferred pathways for 15+ species. "
        "Best for pathway lookup, gene/protein-to-pathway mapping and gene-list over-representation analysis."
    )
    homepage = "https://reactome.org/"
    docs_url = "https://reactome.org/dev/content-service"
    auth = "none"
    rate_limit = 5.0
    health_query = "apoptosis"
    nih_repositories = ("Reactome",)

    @tool(
        description="""
        Search Reactome by free text (pathway names, summaries, gene/protein/compound names).
        Default returns human Pathways with stable IDs (R-HSA-...), names and curated summaries.
        Set types='Protein' or 'Small molecule' to find Reactome entities (with UniProt / ChEBI
        cross-references), or species='' for all species. Example: query='DNA double-strand break repair'.
        Use get_pathway for details of one pathway, pathways_for_entity to map a specific gene/protein,
        and enrichment_analysis for gene lists.
        """,
        federated=True,
    )
    async def search(self, params: ReactomeSearchParams) -> ToolResult:
        page = max(1, parse_offset(params.cursor) or 1)
        query: dict[str, Any] = {
            "query": params.query,
            "types": params.types or None,
            "species": params.species or None,
            "cluster": "true",
            "rowCount": params.limit,
            "page": page,
        }
        response = await self.request(
            "GET", f"{CONTENT}/search/query/paginated", params=query, allow_status=(404,)
        )
        if response.status == 404:
            return ToolResult(
                source=self.name,
                tool="search",
                total=0,
                request_url=response.url,
                notes=[f"no Reactome entries match {params.query!r} for the given species/types"],
            )
        payload = response.json() or {}
        records: list[Record] = []
        for group in payload.get("results") or []:
            for entry in group.get("entries") or []:
                records.append(_search_record(entry))
        records = records[: params.limit]
        groups = payload.get("results") or []
        total = payload.get("numberOfMatches")
        if len(groups) == 1 and groups[0].get("entriesCount") is not None:
            total = groups[0].get("entriesCount")
        more = total is not None and page * params.limit < total and len(records) > 0
        return ToolResult(
            source=self.name,
            tool="search",
            records=records,
            total=total,
            next_cursor=str(page + 1) if more else None,
            request_url=response.url,
        )

    @tool(
        description="""
        Get one Reactome pathway (or reaction) by stable ID, e.g. R-HSA-109581 (Apoptosis):
        name, species, curated summary, release date, GO biological process, parent pathways,
        sub-pathways/reactions, disease flag, diagram link and literature references (PMIDs).
        The first record is the pathway; following records are its literature references
        (type article, with PMIDs). Set include_participants=true for molecular participants
        (UniProt/ChEBI counts and accessions).
        """
    )
    async def get_pathway(self, params: GetPathwayParams) -> ToolResult:
        pid = params.pathway_id.strip()
        if _STID.match(pid):
            pid = pid.upper().split(".")[0]
        response = await self.request(
            "GET", f"{CONTENT}/data/query/enhanced/{quote(pid, safe='')}", allow_status=(404, 400)
        )
        if response.status in (400, 404):
            return ToolResult(
                source=self.name,
                tool="get_pathway",
                request_url=response.url,
                notes=[f"Reactome pathway {params.pathway_id!r} not found (use a stable ID like R-HSA-109581)"],
            )
        item = response.json() or {}
        st_id = item.get("stId") or str(item.get("dbId") or pid)
        refs = [r for r in item.get("literatureReference") or [] if isinstance(r, dict)]
        pmids = [str(r["pubMedIdentifier"]) for r in refs if r.get("pubMedIdentifier")]
        summation = " ".join(
            strip_html(s.get("text")) or "" for s in item.get("summation") or [] if isinstance(s, dict)
        ).strip()
        go = _go_id(item.get("goBiologicalProcess"))
        diseases = [d for d in item.get("disease") or [] if isinstance(d, dict)]
        notes: list[str] = []
        attributes: dict[str, Any] = {
            "species": item.get("speciesName") or first([s.get("displayName") for s in item.get("species") or [] if isinstance(s, dict)]),
            "schema_class": item.get("schemaClass"),
            "version": item.get("stIdVersion"),
            "is_in_disease": item.get("isInDisease"),
            "is_inferred": item.get("isInferred"),
            "has_diagram": item.get("hasDiagram"),
            "last_updated": item.get("lastUpdatedDate"),
            "review_status": (item.get("reviewStatus") or {}).get("displayName"),
            "go_biological_process": (
                {"id": go, "name": (item.get("goBiologicalProcess") or {}).get("displayName")} if go else None
            ),
            "diseases": [
                {"name": d.get("displayName"), "id": f"DOID:{d['identifier']}" if d.get("identifier") else None}
                for d in diseases
            ]
            or None,
            "parent_pathways": [
                {"id": e.get("stId"), "name": e.get("displayName")}
                for e in item.get("eventOf") or []
                if isinstance(e, dict)
            ]
            or None,
            "sub_events": [
                {"id": e.get("stId"), "name": e.get("displayName"), "class": e.get("schemaClass")}
                for e in (item.get("hasEvent") or [])[:50]
                if isinstance(e, dict)
            ]
            or None,
            "sub_event_count": len(item.get("hasEvent") or []) or None,
            "literature_count": len(refs) or None,
            "pmids": pmids[:50] or None,
            "pathway_browser_url": f"{SITE}/PathwayBrowser/#/{st_id}",
            "diagram_png": f"{CONTENT}/exporter/diagram/{st_id}.png" if item.get("hasDiagram") else None,
        }
        if params.include_participants:
            presp = await self.request(
                "GET", f"{CONTENT}/data/participants/{quote(st_id, safe='')}/referenceEntities", allow_status=(404,)
            )
            if presp.status == 404:
                notes.append("no participants reported for this event")
            else:
                ents = [e for e in presp.json() or [] if isinstance(e, dict)]
                by_db: dict[str, int] = {}
                uniprot: list[str] = []
                symbols: list[str] = []
                for e in ents:
                    db = e.get("databaseName") or "other"
                    by_db[db] = by_db.get(db, 0) + 1
                    if db == "UniProt" and e.get("identifier") and str(e["identifier"]) not in uniprot:
                        uniprot.append(str(e["identifier"]))
                        sym = first(e.get("geneName")) or first(e.get("name"))
                        if sym:
                            symbols.append(sym)
                attributes["participants_count"] = len(ents)
                attributes["participants_by_database"] = by_db
                attributes["participant_uniprot"] = uniprot[:100] or None
                attributes["participant_genes"] = sorted(set(symbols))[:100] or None
                if len(uniprot) > 100:
                    notes.append(f"participant UniProt list truncated to 100 of {len(uniprot)}")
        record = Record(
            source=self.name,
            id=st_id,
            type="pathway" if item.get("schemaClass") in ("Pathway", "TopLevelPathway", None) else "other",
            title=item.get("displayName") or first(item.get("name")),
            url=_pathway_url(st_id),
            identifiers={"reactome": st_id},
            date=item.get("releaseDate"),
            summary=summation or None,
            attributes={k: v for k, v in attributes.items() if v not in (None, "", [], {})},
        )
        lit = [
            Record(
                source=self.name,
                id=f"PMID:{r['pubMedIdentifier']}" if r.get("pubMedIdentifier") else str(r.get("dbId")),
                type="article",
                title=r.get("title") or r.get("displayName"),
                url=f"https://pubmed.ncbi.nlm.nih.gov/{r['pubMedIdentifier']}/" if r.get("pubMedIdentifier") else r.get("url"),
                identifiers={"pmid": r.get("pubMedIdentifier")},
                date=str(r["year"]) if r.get("year") else None,
                attributes={
                    k: v
                    for k, v in {"journal": r.get("journal"), "volume": r.get("volume"), "pages": r.get("pages"), "cited_by_pathway": st_id}.items()
                    if v not in (None, "")
                },
            )
            for r in refs[:25]
        ]
        if len(refs) > 25:
            notes.append(f"literature records truncated to 25 of {len(refs)} (all PMIDs up to 50 are in attributes.pmids)")
        return ToolResult(
            source=self.name,
            tool="get_pathway",
            records=[record, *lit],
            total=1,
            request_url=response.url,
            notes=notes,
        )

    @tool(
        description="""
        Map one gene, protein or small molecule to the Reactome pathways it participates in.
        Accepts UniProt accession (P04637), Ensembl gene ID (ENSG00000141510), ChEBI ID (CHEBI:15377),
        NCBI Gene ID (7157) or gene symbol (TP53). UniProt/Ensembl/ChEBI/NCBI IDs return the
        lowest-level pathways containing the entity; gene symbols are resolved through the Analysis
        Service and return all pathway levels with entity counts. Use enrichment_analysis for many genes.
        """
    )
    async def pathways_for_entity(self, params: PathwaysForEntityParams) -> ToolResult:
        ident = params.identifier.strip()
        id_type = params.id_type
        if id_type == "auto":
            upper = ident.upper()
            if upper.startswith("CHEBI:"):
                id_type = "chebi"
            elif re.match(r"^ENS[A-Z]*[GTP]\d+", upper):
                id_type = "ensembl"
            elif ident.isdigit():
                id_type = "ncbi_gene"
            elif _UNIPROT.match(upper):
                id_type = "uniprot"
            else:
                id_type = "gene_symbol"
        if id_type == "gene_symbol":
            return await self._pathways_via_analysis(ident, params)
        resource, value = {
            "uniprot": ("UniProt", ident.upper()),
            "ensembl": ("ENSEMBL", ident.upper().split(".")[0]),
            "chebi": ("ChEBI", re.sub(r"(?i)^chebi:", "", ident)),
            "ncbi_gene": ("NCBI Gene", ident),
        }[id_type]
        response = await self.request(
            "GET",
            f"{CONTENT}/data/mapping/{quote(resource)}/{quote(value, safe='')}/pathways",
            params={"species": params.species},
            allow_status=(404,),
        )
        if response.status == 404:
            return ToolResult(
                source=self.name,
                tool="pathways_for_entity",
                total=0,
                request_url=response.url,
                notes=[f"no Reactome pathways found for {resource}:{value} (species {params.species})"],
            )
        items = [i for i in response.json() or [] if isinstance(i, dict)]
        records = [
            _event_record(i, lowest_level_pathway=True, query_identifier=f"{resource}:{value}")
            for i in items[: params.limit]
        ]
        notes = []
        if len(items) > params.limit:
            notes.append(f"showing {params.limit} of {len(items)} lowest-level pathways")
        return ToolResult(
            source=self.name,
            tool="pathways_for_entity",
            records=records,
            total=len(items),
            request_url=response.url,
            notes=notes,
            data={"query": {"resource": resource, "identifier": value}},
        )

    async def _pathways_via_analysis(self, ident: str, params: PathwaysForEntityParams) -> ToolResult:
        query = {
            "pageSize": params.limit,
            "page": 1,
            "sortBy": "ENTITIES_PVALUE",
            "order": "ASC",
            "resource": "TOTAL",
            "includeDisease": "true",
        }
        species = params.species.strip()
        if species and species not in ("9606", "Homo sapiens"):
            query["species"] = species
        response = await self.request(
            "GET", f"{ANALYSIS}/identifier/{quote(ident, safe='')}/projection", params=query, allow_status=(404,)
        )
        payload = response.json() if response.status != 404 else {}
        pathways = [p for p in (payload or {}).get("pathways") or [] if isinstance(p, dict)]
        if not pathways:
            return ToolResult(
                source=self.name,
                tool="pathways_for_entity",
                total=0,
                request_url=response.url,
                notes=[f"identifier {ident!r} was not found in any Reactome pathway"],
            )
        records = [_analysis_pathway_record(p) for p in pathways]
        return ToolResult(
            source=self.name,
            tool="pathways_for_entity",
            records=records,
            total=payload.get("pathwaysFound"),
            request_url=response.url,
            notes=["gene symbol resolved via Reactome Analysis Service (projected to human); includes all pathway levels"],
            data={"token": (payload.get("summary") or {}).get("token")},
        )

    @tool(
        description="""
        Pathway over-representation (enrichment) analysis of a gene/protein/compound list against
        Reactome (hypergeometric test, Benjamini-Hochberg FDR). Returns the top pathways sorted by
        p-value with FDR, entities found/total and reactions found/total, plus a compact table in
        data.rows and the identifiers Reactome could not map. Accepts symbols (TP53), UniProt,
        Ensembl, NCBI Gene and ChEBI IDs. Example: identifiers=['TP53','BRCA1','ATM','CHEK2'].
        """
    )
    async def enrichment_analysis(self, params: EnrichmentParams) -> ToolResult:
        page = max(1, parse_offset(params.cursor) or 1)
        ids = [i.strip() for i in params.identifiers if i and i.strip()]
        query: dict[str, Any] = {
            "pageSize": params.limit,
            "page": page,
            "sortBy": "ENTITIES_PVALUE",
            "order": "ASC",
            "resource": "TOTAL",
            "includeDisease": params.include_disease,
            "pValue": params.p_value,
        }
        path = "/identifiers/projection" if params.projection else "/identifiers/"
        response = await self.request(
            "POST",
            f"{ANALYSIS}{path}",
            params=query,
            data="\n".join(ids),
            headers={"Content-Type": "text/plain"},
            allow_status=(404,),
        )
        payload = response.json() if response.status != 404 else {}
        payload = payload or {}
        pathways = [p for p in payload.get("pathways") or [] if isinstance(p, dict)]
        records = [_analysis_pathway_record(p) for p in pathways]
        token = (payload.get("summary") or {}).get("token")
        not_found_count = payload.get("identifiersNotFound") or 0
        notes: list[str] = []
        not_found: list[str] = []
        if response.status == 404 or not pathways:
            notes.append("none of the identifiers mapped to Reactome pathways")
        if token and not_found_count:
            nresp = await self.request("GET", f"{ANALYSIS}/token/{token}/notFound", allow_status=(404,))
            if nresp.status != 404:
                not_found = [str(x.get("id")) for x in nresp.json() or [] if isinstance(x, dict) and x.get("id")]
            notes.append(f"{not_found_count} of {len(ids)} identifiers were not found in Reactome")
        total = payload.get("pathwaysFound")
        rows = [
            {
                "pathway": r.id,
                "name": r.title,
                "p_value": r.attributes.get("p_value"),
                "fdr": r.attributes.get("fdr"),
                "found": r.attributes.get("entities_found"),
                "total": r.attributes.get("entities_total"),
            }
            for r in records
        ]
        more = total is not None and page * params.limit < total and bool(records)
        return ToolResult(
            source=self.name,
            tool="enrichment_analysis",
            records=records,
            total=total,
            next_cursor=str(page + 1) if more else None,
            request_url=response.url,
            notes=notes,
            data={
                "rows": rows,
                "identifiers_submitted": len(ids),
                "identifiers_not_found": not_found[:100] or None,
                "token": token,
                "analysis_browser_url": f"{SITE}/PathwayBrowser/#/DTAB=AN&ANALYSIS={token}" if token else None,
            },
        )
