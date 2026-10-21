"""EMBL-EBI QuickGO connector (Gene Ontology terms and GO annotations).

Docs: https://www.ebi.ac.uk/QuickGO/api/index.html (REST under /QuickGO/services).
Auth: none. Rate limits: none published; we stay at 5 req/s.

Quirks:
- Term search ``/ontology/go/search`` pages with ``limit`` + 1-based ``page``; relevance order
  is approximate (child terms can rank above the exact term).
- ``/ontology/go/terms/{ids}/complete`` takes a comma-separated id list; unknown-but-valid ids
  return an empty list, malformed ids return HTTP 400.
- ``/annotation/search`` returns one row per annotation (max ``limit`` 200 per page) and leaves
  ``goName``/``name``/``synonyms`` null unless requested via ``includeFields``.
  ``geneProductSubset=Swiss-Prot`` requires ``geneProductType=protein``. ``geneProductId`` must be
  a database-prefixed id or accession (``UniProtKB:P04637`` or ``P04637``); gene symbols are not
  accepted (HTTP 400).
- ``/annotation/stats`` gives distinct gene-product counts for a filter (used for totals).
- Evidence filtering: ``evidenceCode`` takes ECO ids (a ``goEvidence`` param is silently ignored).
  ``exclude_iea`` sends ECO:0000352 (manual assertion) + ``evidenceCodeUsage=descendants``;
  electronic IEA annotations are ECO:0000501 descendants.
- Annotation rows (and hence genes_for_term results) are ordered by gene product accession,
  not by relevance.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field

from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, strip_html, tool

API = "https://www.ebi.ac.uk/QuickGO/services"
SITE = "https://www.ebi.ac.uk/QuickGO"

_GO = re.compile(r"^(?:GO:)?(\d{7})$", re.IGNORECASE)
# ECO "evidence used in manual assertion": with evidenceCodeUsage=descendants this keeps every
# curated annotation (EXP/IDA/IMP/IGI/IPI/ISS/IBA/TAS/...) and drops electronic IEA ones.
_MANUAL_ECO = "ECO:0000352"
ASPECTS = ("biological_process", "molecular_function", "cellular_component")


def _norm_go(value: str) -> str | None:
    m = _GO.match(value.strip())
    return f"GO:{m.group(1)}" if m else None


def _term_url(go_id: str) -> str:
    return f"{SITE}/term/{go_id}"


def _term_record(t: dict[str, Any], *, full: bool = False) -> Record:
    go_id = t.get("id") or ""
    definition = (t.get("definition") or {}).get("text") if isinstance(t.get("definition"), dict) else t.get("definition")
    attrs: dict[str, Any] = {"aspect": t.get("aspect"), "is_obsolete": t.get("isObsolete")}
    identifiers: dict[str, Any] = {"go": go_id}
    if full:
        syn = [s.get("name") for s in t.get("synonyms") or [] if isinstance(s, dict) and s.get("name")]
        xrefs = [
            f"{x.get('dbCode')}:{x.get('dbId')}"
            for x in t.get("xRefs") or []
            if isinstance(x, dict) and x.get("dbCode") and x.get("dbId")
        ]
        reactome = [x.split(":", 1)[1] for x in xrefs if x.startswith("Reactome:")]
        if reactome:
            identifiers["reactome"] = reactome[0]
        replacements = [
            {"id": r.get("id"), "type": r.get("type")}
            for r in t.get("replacements") or []
            if isinstance(r, dict)
        ]
        attrs.update(
            {
                "synonyms": syn[:25] or None,
                "synonym_types": sorted({s.get("type") for s in t.get("synonyms") or [] if isinstance(s, dict) and s.get("type")}) or None,
                "comment": strip_html(t.get("comment")) if t.get("comment") else None,
                "secondary_ids": t.get("secondaryIds"),
                "replaced_by": [r["id"] for r in replacements if r.get("type") == "replace"] or None,
                "consider": [r["id"] for r in replacements if r.get("type") == "consider"] or None,
                "children": [
                    {"id": c.get("id"), "relation": c.get("relation")}
                    for c in (t.get("children") or [])[:30]
                    if isinstance(c, dict)
                ]
                or None,
                "child_count": len(t.get("children") or []) or None,
                "xrefs": xrefs[:30] or None,
                "usage": t.get("usage"),
                "taxon_constraints": [
                    f"{c.get('relationship')} {c.get('taxName')}"
                    for c in (t.get("taxonConstraints") or [])[:10]
                    if isinstance(c, dict)
                ]
                or None,
            }
        )
    return Record(
        source="quickgo",
        id=go_id,
        type="ontology_term",
        title=t.get("name"),
        url=_term_url(go_id) if go_id else None,
        identifiers=identifiers,
        summary=strip_html(definition) if definition else None,
        attributes={k: v for k, v in attrs.items() if v not in (None, "", [])},
    )


def _accession(gene_product_id: str | None) -> str:
    gp = gene_product_id or ""
    return gp.split(":", 1)[1] if ":" in gp else gp


def _symbol(row: dict[str, Any]) -> str | None:
    sym = row.get("symbol")
    return sym if isinstance(sym, str) and sym else None


class QuickGOSearchParams(SearchParams):
    limit: int = Field(10, ge=1, le=100, description="Maximum number of GO terms to return")


class GetTermsParams(ToolParams):
    go_ids: list[str] = Field(
        min_length=1, max_length=50, description="GO term IDs, e.g. ['GO:0006915', 'GO:0005634'] (GO: prefix optional)"
    )


class GeneAnnotationsParams(ToolParams):
    gene_product_id: str = Field(
        description="UniProtKB accession (P04637) or prefixed id (UniProtKB:P04637). Gene symbols are not accepted: "
        "resolve symbols to UniProt first (e.g. with the UniProt connector)."
    )
    aspect: Literal["biological_process", "molecular_function", "cellular_component"] | None = Field(
        None, description="Restrict to one GO aspect"
    )
    exclude_iea: bool = Field(False, description="Drop electronic (IEA) annotations, keeping manual/experimental only")
    limit: int = Field(50, ge=1, le=100, description="Maximum number of distinct GO terms to return")


class GenesForTermParams(ToolParams):
    go_id: str = Field(description="GO term ID, e.g. GO:0006915 (apoptotic process)")
    taxon_id: int | None = Field(9606, description="NCBI taxon to restrict to (9606 human, 10090 mouse); null = all")
    include_descendants: bool = Field(
        True, description="Include annotations to descendant terms (is_a/part_of/occurs_in), as GO tools usually do"
    )
    reviewed_only: bool = Field(True, description="Only Swiss-Prot (reviewed) proteins")
    exclude_iea: bool = Field(False, description="Drop electronic (IEA) annotations")
    limit: int = Field(50, ge=1, le=100, description="Maximum number of gene products to return")
    cursor: str | None = Field(None, description="Annotation page cursor from a previous result's next_cursor")


class QuickGO(Connector):
    name = "quickgo"
    title = "QuickGO (Gene Ontology)"
    category = "biology"
    description = (
        "EMBL-EBI browser for the Gene Ontology (~40k terms) and ~1 billion GO annotations from the "
        "GO Consortium/UniProt-GOA. Best for GO term lookup, a protein's GO annotations with evidence codes, "
        "and listing genes annotated to a biological process/function/component."
    )
    homepage = "https://www.ebi.ac.uk/QuickGO/"
    docs_url = "https://www.ebi.ac.uk/QuickGO/api/index.html"
    auth = "none"
    rate_limit = 5.0
    health_query = "apoptosis"
    nih_repositories = ("Gene Ontology Consortium and Knowledgebase",)

    @tool(
        description="""
        Search Gene Ontology terms by name/synonym text (e.g. 'mitochondrial fission', 'kinase activity',
        'synapse'). Returns GO IDs, names, aspect (biological_process / molecular_function /
        cellular_component), definitions and obsolete flag. Use get_terms for synonyms/xrefs/children,
        genes_for_term to list annotated genes, gene_annotations for one protein's GO profile.
        """,
        federated=True,
    )
    async def search_terms(self, params: QuickGOSearchParams) -> ToolResult:
        page = max(1, parse_offset(params.cursor) or 1)
        response = await self.request(
            "GET", f"{API}/ontology/go/search", params={"query": params.query, "limit": params.limit, "page": page}
        )
        payload = response.json() or {}
        results = [r for r in payload.get("results") or [] if isinstance(r, dict)]
        total = payload.get("numberOfHits")
        pages = (payload.get("pageInfo") or {}).get("total") or 0
        return ToolResult(
            source=self.name,
            tool="search_terms",
            records=[_term_record(r) for r in results],
            total=total,
            next_cursor=str(page + 1) if results and page < pages else None,
            request_url=response.url,
        )

    @tool(
        description="""
        Fetch full details for up to 50 GO terms by ID: name, aspect, definition, synonyms, obsolete flag
        with replacement/consider terms, secondary IDs, child terms with relations, cross-references
        (Reactome, UniProt keywords, Wikipedia) and taxon constraints. IDs like GO:0006915 or 0006915.
        """
    )
    async def get_terms(self, params: GetTermsParams) -> ToolResult:
        ids = [g for g in (_norm_go(x) for x in params.go_ids) if g]
        bad = [x for x in params.go_ids if not _norm_go(x)]
        notes = [f"ignored malformed GO ids: {', '.join(bad)}"] if bad else []
        if not ids:
            return ToolResult(source=self.name, tool="get_terms", notes=notes or ["no valid GO ids given"])
        response = await self.request(
            "GET", f"{API}/ontology/go/terms/{','.join(dict.fromkeys(ids))}/complete", allow_status=(400,)
        )
        if response.status == 400:
            return ToolResult(
                source=self.name, tool="get_terms", request_url=response.url,
                notes=notes + [f"QuickGO rejected the ids: {response.text[:200]}"],
            )
        results = [r for r in (response.json() or {}).get("results") or [] if isinstance(r, dict)]
        found = {r.get("id") for r in results}
        missing = [i for i in ids if i not in found]
        if missing:
            notes.append(f"not found: {', '.join(missing)}")
        for r in results:
            if r.get("isObsolete"):
                notes.append(f"{r.get('id')} is obsolete; see replaced_by/consider")
        return ToolResult(
            source=self.name,
            tool="get_terms",
            records=[_term_record(r, full=True) for r in results],
            total=len(results),
            request_url=response.url,
            notes=notes,
        )

    @tool(
        description="""
        Get the GO annotations of one protein (UniProtKB accession, e.g. P04637 for TP53), aggregated
        to one record per GO term with qualifiers (enables, involved_in, located_in, ...), evidence
        codes (IDA, IMP, IPI, ISS, IBA, IEA, ...), supporting PMIDs and assigning databases;
        data.by_aspect groups the terms by biological_process / molecular_function / cellular_component.
        Set exclude_iea=true for manually curated annotations only. Reads up to 600 annotation rows.
        """
    )
    async def gene_annotations(self, params: GeneAnnotationsParams) -> ToolResult:
        acc = params.gene_product_id.strip()
        if ":" in acc:
            db, local = acc.split(":", 1)
            gp = f"{'UniProtKB' if db.lower() in ('uniprotkb', 'uniprot') else db}:{local.strip().upper() if db.lower() in ('uniprotkb', 'uniprot') else local.strip()}"
        else:
            gp = f"UniProtKB:{acc.upper()}"
        base: dict[str, Any] = {
            "geneProductId": gp,
            "limit": 200,
            "includeFields": "goName,name,synonyms",
            "aspect": params.aspect,
            "evidenceCode": _MANUAL_ECO if params.exclude_iea else None,
            "evidenceCodeUsage": "descendants" if params.exclude_iea else None,
        }
        rows: list[dict[str, Any]] = []
        total_rows = None
        first_url = None
        for page in (1, 2, 3):
            response = await self.request(
                "GET", f"{API}/annotation/search", params={**base, "page": page}, allow_status=(400,)
            )
            first_url = first_url or response.url
            if response.status == 400:
                return ToolResult(
                    source=self.name, tool="gene_annotations", request_url=response.url,
                    notes=[f"QuickGO rejected gene product id {params.gene_product_id!r}: use a UniProtKB accession like P04637"],
                )
            payload = response.json() or {}
            total_rows = payload.get("numberOfHits")
            batch = [r for r in payload.get("results") or [] if isinstance(r, dict)]
            rows.extend(batch)
            pages = (payload.get("pageInfo") or {}).get("total") or 0
            if page >= pages or not batch:
                break
        if not rows:
            return ToolResult(
                source=self.name, tool="gene_annotations", total=0, request_url=first_url,
                notes=[f"no GO annotations found for {gp}"],
            )
        terms: dict[str, dict[str, Any]] = {}
        for r in rows:
            go_id = r.get("goId")
            if not go_id:
                continue
            t = terms.setdefault(
                go_id,
                {"name": r.get("goName"), "aspect": r.get("goAspect"), "qualifiers": [], "evidence": [], "refs": [], "assigned_by": [], "n": 0},
            )
            t["n"] += 1
            for key, value in (("qualifiers", r.get("qualifier")), ("evidence", r.get("goEvidence")), ("assigned_by", r.get("assignedBy"))):
                if value and value not in t[key]:
                    t[key].append(value)
            ref = r.get("reference")
            if ref and ref not in t["refs"]:
                t["refs"].append(ref)
        first_row = rows[0]
        symbol = _symbol(first_row)
        protein_name = first_row.get("name")
        ordered = sorted(terms.items(), key=lambda kv: (ASPECTS.index(kv[1]["aspect"]) if kv[1]["aspect"] in ASPECTS else 9, -kv[1]["n"]))
        records = []
        by_aspect: dict[str, list[dict[str, Any]]] = {}
        for go_id, t in ordered:
            pmids = [x.split(":", 1)[1] for x in t["refs"] if x.startswith("PMID:")]
            by_aspect.setdefault(t["aspect"] or "unknown", []).append(
                f"{go_id} {t['name'] or ''} [{','.join(t['evidence'])}]".strip()
            )
            if len(records) >= params.limit:
                continue
            records.append(
                Record(
                    source=self.name,
                    id=go_id,
                    type="ontology_term",
                    title=t["name"],
                    url=_term_url(go_id),
                    identifiers={"go": go_id},
                    attributes={
                        k: v
                        for k, v in {
                            "aspect": t["aspect"],
                            "gene_product": gp,
                            "gene_symbol": symbol,
                            "qualifiers": t["qualifiers"],
                            "evidence_codes": t["evidence"],
                            "annotation_count": t["n"],
                            "pmids": pmids[:20] or None,
                            "other_references": [x for x in t["refs"] if not x.startswith("PMID:")][:10] or None,
                            "assigned_by": t["assigned_by"],
                        }.items()
                        if v not in (None, [], "")
                    },
                )
            )
        notes = []
        if total_rows and total_rows > len(rows):
            notes.append(f"aggregated the first {len(rows)} of {total_rows} annotation rows")
        if len(terms) > len(records):
            notes.append(f"showing {len(records)} of {len(terms)} GO terms (all listed in data.by_aspect)")
        return ToolResult(
            source=self.name,
            tool="gene_annotations",
            records=records,
            total=len(terms),
            request_url=first_url,
            notes=notes,
            data={
                "gene_product": {"id": gp, "uniprot": _accession(gp), "symbol": symbol, "name": protein_name},
                "annotation_rows": total_rows,
                "by_aspect": {k: v[:80] for k, v in by_aspect.items()},
            },
        )

    @tool(
        description="""
        List gene products (proteins) annotated to a GO term, by default human (taxon_id 9606),
        reviewed Swiss-Prot entries, including annotations to descendant terms. Returns one record per
        protein with UniProt accession, gene symbol, protein name, the specific GO terms and evidence
        codes; total = number of distinct gene products (from QuickGO stats). Example: go_id='GO:0006915'.
        Use exclude_iea=true for curated evidence only. Paged via next_cursor.
        """
    )
    async def genes_for_term(self, params: GenesForTermParams) -> ToolResult:
        go_id = _norm_go(params.go_id)
        if not go_id:
            return ToolResult(source=self.name, tool="genes_for_term", notes=[f"malformed GO id {params.go_id!r}"])
        base: dict[str, Any] = {
            "goId": go_id,
            "taxonId": params.taxon_id,
            "goUsage": "descendants" if params.include_descendants else "exact",
            "geneProductType": "protein" if params.reviewed_only else None,
            "geneProductSubset": "Swiss-Prot" if params.reviewed_only else None,
            "evidenceCode": _MANUAL_ECO if params.exclude_iea else None,
            "evidenceCodeUsage": "descendants" if params.exclude_iea else None,
        }
        page, skip = 1, 0
        if params.cursor:
            head, _, tail = params.cursor.partition(".")
            page = max(1, parse_offset(head) or 1)
            skip = parse_offset(tail)
        genes: dict[str, dict[str, Any]] = {}
        first_url = None
        pages = 0
        current = page
        next_cursor: str | None = None
        while current < page + 3:
            response = await self.request(
                "GET",
                f"{API}/annotation/search",
                params={**base, "limit": 200, "page": current, "includeFields": "goName,name,synonyms"},
            )
            first_url = first_url or response.url
            payload = response.json() or {}
            pages = (payload.get("pageInfo") or {}).get("total") or 0
            rows = [r for r in payload.get("results") or [] if isinstance(r, dict)]
            start = skip if current == page else 0
            stopped = False
            for index in range(start, len(rows)):
                r = rows[index]
                gp = r.get("geneProductId")
                if not gp:
                    continue
                if gp not in genes and len(genes) >= params.limit:
                    next_cursor = f"{current}.{index}"  # resume at this row (rows are ordered by gene product)
                    stopped = True
                    break
                g = genes.setdefault(gp, {"row": r, "terms": {}, "evidence": [], "n": 0})
                g["n"] += 1
                if r.get("goId"):
                    g["terms"].setdefault(r["goId"], r.get("goName"))
                if r.get("goEvidence") and r["goEvidence"] not in g["evidence"]:
                    g["evidence"].append(r["goEvidence"])
            if stopped or current >= pages or not rows:
                break
            current += 1
        else:
            if current <= pages:
                next_cursor = f"{current}.0"
        total = None
        if not params.cursor:
            stats_resp = await self.request(
                "GET", f"{API}/annotation/stats", params=base, allow_status=(400,)
            )
            if stats_resp.status == 200:
                for group in (stats_resp.json() or {}).get("results") or []:
                    if group.get("groupName") == "geneProduct":
                        total = group.get("totalHits")
        records = []
        for gp, g in genes.items():
            row = g["row"]
            acc = _accession(gp)
            is_uniprot = gp.startswith("UniProtKB:")
            records.append(
                Record(
                    source=self.name,
                    id=gp,
                    type="protein" if is_uniprot else "gene",
                    title=_symbol(row) or acc,
                    url=f"https://www.uniprot.org/uniprotkb/{acc}/entry" if is_uniprot else None,
                    identifiers={"uniprot": acc if is_uniprot else None, "gene_symbol": _symbol(row)},
                    summary=row.get("name"),
                    attributes={
                        k: v
                        for k, v in {
                            "taxon_id": row.get("taxonId"),
                            "query_go_id": go_id,
                            "go_terms": [{"go": k2, "name": v2} for k2, v2 in list(g["terms"].items())[:10]],
                            "evidence_codes": g["evidence"],
                            "annotation_count": g["n"],
                            "synonyms": (row.get("synonyms") or "").split(",")[:10] if row.get("synonyms") else None,
                        }.items()
                        if v not in (None, [], "")
                    },
                )
            )
        notes = [] if records else [f"no gene products annotated to {go_id} with these filters"]
        if records and next_cursor is None and current < pages:
            next_cursor = f"{current + 1}.0"
        return ToolResult(
            source=self.name,
            tool="genes_for_term",
            records=records,
            total=total,
            next_cursor=next_cursor if records else None,
            request_url=first_url,
            notes=notes,
        )
