"""GTEx Portal API v2 connector (Genotype-Tissue Expression).

Docs: https://gtexportal.org/api/v2/redoc (OpenAPI: https://gtexportal.org/api/v2/openapi.json).

Auth: none (keyless). Rate limits: not published; we stay at 5 req/s and rely on the cache.

Datasets: gtex_v8 (GENCODE v26), gtex_v10 (GENCODE v39; the API's default and our default) and
gtex_v11 (GENCODE v47). Expression/eQTL endpoints need the *versioned* GENCODE ID of the dataset's
annotation (ENSG00000157764.14 in v10, .12 in v8), so genes are first resolved with
``/reference/gene?geneId=...&gencodeVersion=...``.

Quirks:
- Page-number pagination (``page``, ``itemsPerPage``); ``paging_info.totalNumberOfItems`` is the total.
- Unknown enum values (tissue, dataset) return HTTP 422 with the list of valid values; tissues are
  resolved leniently here ("liver", "whole blood", "Brain - Cortex" -> Brain_Cortex).
- ``/association/singleTissueEqtl`` is unsorted; we fetch up to 10,000 rows, sort by p-value and page
  locally (noted when a gene has more significant eQTLs than that).
- GTEx variant IDs look like ``chr7_140753336_A_T_b38``; they are converted to gnomAD-style
  ``7-140753336-A-T`` for the ``gnomad_variant`` identifier.

Identifiers: gene records carry ensembl_gene (version stripped; versioned ID in attributes),
gene_symbol, entrez_gene; eQTL association records carry rsid and gnomad_variant (the variant), with
the gene in attributes (an eQTL is not the same entity as its gene).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field

from ..http import SourceError
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, tool

API = "https://gtexportal.org/api/v2"
PORTAL = "https://gtexportal.org/home"

Dataset = Literal["gtex_v10", "gtex_v11", "gtex_v8"]
_GENCODE = {"gtex_v8": "v26", "gtex_v10": "v39", "gtex_v11": "v47"}
_EQTL_FETCH = 10000

_GTEX_VARIANT = re.compile(r"^chr([0-9XYM]+)_(\d+)_([ACGTN]+)_([ACGTN]+)_b38$", re.IGNORECASE)
_GNOMAD_VARIANT = re.compile(r"^(?:chr)?([0-9XYM]+)[-:_](\d+)[-:_]([ACGTN]+)[-:_>]([ACGTN]+)$", re.IGNORECASE)
_RSID = re.compile(r"^rs\d+$", re.IGNORECASE)


class SearchGenesParams(SearchParams):
    dataset: Dataset = Field("gtex_v10", description="GTEx release whose GENCODE annotation to search")


class MedianExpressionParams(ToolParams):
    gene: str = Field(description="Gene symbol (BRAF), Ensembl gene ID (ENSG00000157764) or versioned GENCODE ID")
    dataset: Dataset = Field("gtex_v10", description="GTEx release: gtex_v10 (default), gtex_v11 (newest), gtex_v8")
    tissue: str | None = Field(
        None, description="Optional single tissue (e.g. Liver, Whole_Blood, 'Brain - Cortex'); omit for all tissues"
    )


class EqtlParams(ToolParams):
    gene: str | None = Field(None, description="Gene symbol or Ensembl/GENCODE ID; give gene and/or variant")
    variant: str | None = Field(
        None,
        description="Variant as rsID (rs38719), GTEx ID (chr7_140994094_G_T_b38) or GRCh38 chrom-pos-ref-alt (7-140994094-G-T)",
    )
    tissue: str | None = Field(None, description="Optional tissue filter (e.g. Whole_Blood, Liver, 'Brain - Cortex')")
    dataset: Dataset = Field("gtex_v10", description="GTEx release: gtex_v10 (default), gtex_v11, gtex_v8")
    limit: int = Field(20, ge=1, le=100, description="Maximum number of eQTLs to return (most significant first)")
    cursor: str | None = Field(None, description="Pagination cursor (offset) from a previous result")


class TissuesParams(ToolParams):
    dataset: Dataset = Field("gtex_v10", description="GTEx release")


class TopExpressedParams(ToolParams):
    tissue: str = Field(description="Tissue, e.g. Liver, Whole_Blood, Heart_Left_Ventricle, 'Brain - Cortex'")
    dataset: Dataset = Field("gtex_v10", description="GTEx release")
    include_mitochondrial: bool = Field(False, description="Include mitochondrial genes (MT-*) in the ranking")
    limit: int = Field(25, ge=1, le=100, description="Number of top genes to return")
    cursor: str | None = Field(None, description="Pagination cursor (page number) from a previous result")


# ---------------------------------------------------------------------------


def _strip_version(gencode_id: str | None) -> str | None:
    return gencode_id.split(".")[0] if gencode_id else None


def gtex_to_gnomad(variant_id: str | None) -> str | None:
    match = _GTEX_VARIANT.match(variant_id or "")
    if not match:
        return None
    chrom, pos, ref, alt = match.groups()
    return f"{chrom.upper()}-{pos}-{ref.upper()}-{alt.upper()}"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _gene_record(gene: dict[str, Any], extra: dict[str, Any] | None = None) -> Record:
    gencode_id = gene.get("gencodeId") or ""
    symbol = gene.get("geneSymbol")
    description = re.sub(r"\s*\[Source:.*?\]\s*$", "", gene.get("description") or "").strip() or None
    hgnc = re.search(r"Acc:(HGNC:\d+)", gene.get("description") or "")
    start, end = gene.get("start"), gene.get("end")
    attributes = {
        "gencode_id": gencode_id,
        "gencode_version": gene.get("gencodeVersion"),
        "gene_type": gene.get("geneType"),
        "location": f"{gene.get('chromosome')}:{start}-{end} ({gene.get('strand')})" if start and end else None,
        "genome_build": gene.get("genomeBuild"),
        "tss": gene.get("tss"),
        **(extra or {}),
    }
    return Record(
        source="gtex",
        id=gencode_id or (symbol or ""),
        type="gene",
        title=f"{symbol}: {description}" if symbol and description else (symbol or gencode_id),
        url=f"{PORTAL}/gene/{symbol or _strip_version(gencode_id)}",
        identifiers={
            "ensembl_gene": gencode_id,
            "gene_symbol": symbol,
            "entrez_gene": gene.get("entrezGeneId"),
            "hgnc": hgnc.group(1) if hgnc else None,
        },
        summary=description,
        attributes={k: v for k, v in attributes.items() if v not in (None, "", [], {})},
    )


def _eqtl_record(row: dict[str, Any]) -> Record:
    variant_id = row.get("variantId") or ""
    gene = row.get("geneSymbol") or row.get("gencodeId")
    tissue = row.get("tissueSiteDetailId")
    snp = row.get("snpId")
    nes = row.get("nes")
    pval = row.get("pValue")
    label = snp or variant_id
    title = f"{label} eQTL for {gene} in {tissue}"
    if nes is not None and pval is not None:
        title += f" (NES {nes:+.3f}, p={pval:.2e})"
    return Record(
        source="gtex",
        id=f"{variant_id}|{row.get('gencodeId')}|{tissue}",
        type="association",
        title=title,
        url=f"{PORTAL}/snp/{snp}" if snp else f"{PORTAL}/gene/{row.get('geneSymbol')}",
        identifiers={"rsid": snp, "gnomad_variant": gtex_to_gnomad(variant_id)},  # gene: attributes (module doc)
        attributes={
            k: v
            for k, v in {
                "variant_id": variant_id,
                "gene_symbol": row.get("geneSymbol"),
                "ensembl_gene": _strip_version(row.get("gencodeId")),
                "gencode_id": row.get("gencodeId"),
                "tissue": tissue,
                "tissue_uberon": row.get("ontologyId"),
                "nes": nes,
                "p_value": pval,
                "chromosome": row.get("chromosome"),
                "position": row.get("pos"),
                "dataset": row.get("datasetId"),
            }.items()
            if v not in (None, "")
        },
    )


class Gtex(Connector):
    name = "gtex"
    title = "GTEx Portal (Genotype-Tissue Expression)"
    category = "genomics"
    description = (
        "GTEx: RNA-seq expression across 54 non-diseased human tissues from ~950 donors, plus "
        "expression QTLs (eQTLs) linking genetic variants to gene expression per tissue. Best for "
        "'where is this gene expressed?' and 'is this variant an eQTL, for which gene and tissue?'."
    )
    homepage = "https://gtexportal.org/"
    docs_url = "https://gtexportal.org/api/v2/redoc"
    auth = "none"
    rate_limit = 5.0
    health_query = "BRAF"
    nih_repositories = ("Genotype-Tissue Expression (GTEx)",)

    def __init__(self, settings, http):
        super().__init__(settings, http)
        self._tissue_cache: dict[str, list[dict[str, Any]]] = {}

    # ---- helpers ----
    async def _get(self, path: str, params: dict[str, Any], allow_status: tuple[int, ...] = (422,)) -> Any:
        response = await self.request("GET", f"{API}/{path}", params=params, allow_status=allow_status)
        if response.status == 422:
            detail = ""
            try:
                errors = response.json().get("detail") or []
                detail = "; ".join(f"{'.'.join(map(str, e.get('loc', [])[1:2]))}: {e.get('msg', '')[:200]}" for e in errors)
            except (ValueError, AttributeError):
                detail = response.text[:200]
            raise SourceError(self.name, f"GTEx rejected the request: {detail}", status=422, url=response.url)
        return response

    async def _tissue_list(self, dataset: str) -> list[dict[str, Any]]:
        if dataset not in self._tissue_cache:
            response = await self._get("dataset/tissueSiteDetail", {"datasetId": dataset, "itemsPerPage": 250})
            self._tissue_cache[dataset] = response.json().get("data") or []
        return self._tissue_cache[dataset]

    async def _resolve_tissue(self, text: str | None, dataset: str) -> str | None:
        if not text:
            return None
        tissues = await self._tissue_list(dataset)
        wanted = _norm(text)
        for t in tissues:
            if wanted in (_norm(t.get("tissueSiteDetailId") or ""), _norm(t.get("tissueSiteDetail") or "")):
                return t["tissueSiteDetailId"]
        partial = [
            t["tissueSiteDetailId"]
            for t in tissues
            if wanted
            and (wanted in _norm(t.get("tissueSiteDetailId") or "") or wanted in _norm(t.get("tissueSiteDetail") or ""))
        ]
        if len(partial) == 1:
            return partial[0]
        if not tissues:  # tissue list unavailable: pass through and let the API validate
            return text
        hint = ", ".join(partial[:12]) if partial else "see the tissues tool"
        raise SourceError(self.name, f"unknown or ambiguous GTEx tissue {text!r}; candidates: {hint}")

    async def _resolve_gene(self, gene: str, dataset: str) -> tuple[dict[str, Any] | None, str]:
        query = gene.strip()
        lookup = query.split(".")[0] if query.upper().startswith("ENSG") else query
        response = await self._get(
            "reference/gene",
            {"geneId": lookup, "gencodeVersion": _GENCODE[dataset], "genomeBuild": "GRCh38/hg38", "itemsPerPage": 25},
        )
        genes = response.json().get("data") or []
        key = lookup.upper()
        exact = [
            g
            for g in genes
            if (g.get("geneSymbolUpper") or "").upper() == key or (_strip_version(g.get("gencodeId")) or "").upper() == key
        ]
        return (exact or genes or [None])[0], response.url

    async def _variant_id(self, variant: str, dataset: str) -> tuple[str | None, list[str]]:
        text = variant.strip()
        if _GTEX_VARIANT.match(text):
            return text, []
        match = _GNOMAD_VARIANT.match(text)
        if match:
            chrom, pos, ref, alt = match.groups()
            return f"chr{chrom.upper()}_{pos}_{ref.upper()}_{alt.upper()}_b38", []
        if _RSID.match(text):
            response = await self._get("dataset/variant", {"snpId": text.lower(), "datasetId": dataset})
            rows = response.json().get("data") or []
            if not rows:
                return None, [f"{text} is not among the variants genotyped in {dataset}"]
            notes = [f"{text} has {len(rows)} GTEx variant IDs; using {rows[0].get('variantId')}"] if len(rows) > 1 else []
            return rows[0].get("variantId"), notes
        raise SourceError(
            self.name, f"variant {variant!r} must be an rsID, a GTEx ID (chr1_123_A_G_b38) or chrom-pos-ref-alt"
        )

    # ---- tools ----
    @tool(
        description="""
        Search genes in the GTEx reference annotation by symbol, Ensembl ID or symbol prefix (e.g.
        "BRAF", "ENSG00000157764", "HLA-"). Returns gene records with the versioned GENCODE ID used by
        GTEx, symbol, description, gene type, location and NCBI Gene ID. Free text is not supported:
        multi-word queries are reduced to their gene-like tokens. Use median_expression or
        single_tissue_eqtls next for data about a gene.
        """,
        federated=True,
    )
    async def search_genes(self, params: SearchGenesParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        query = params.query.strip()
        candidates = [query]
        tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-.]*", query)
        if len(tokens) > 1:
            candidates = [t for t in tokens if re.search(r"[A-Z]", t) and (t.isupper() or re.search(r"\d", t))][:3] or tokens[:3]
        records: list[Record] = []
        total = 0
        url = None
        notes: list[str] = []
        for candidate in candidates:
            term = candidate.split(".")[0] if candidate.upper().startswith("ENSG") else candidate
            page = offset // params.limit if candidates == [query] else 0
            response = await self._get(
                "reference/geneSearch",
                {
                    "geneId": term,
                    "gencodeVersion": _GENCODE[params.dataset],
                    "genomeBuild": "GRCh38/hg38",
                    "page": page,
                    "itemsPerPage": params.limit,
                },
            )
            url = url or response.url
            payload = response.json()
            records.extend(_gene_record(g) for g in payload.get("data") or [])
            total += int((payload.get("paging_info") or {}).get("totalNumberOfItems") or 0)
        if candidates != [query]:
            notes.append(f"GTEx gene search is symbol/ID based; searched tokens: {', '.join(candidates)}")
        seen: set[str] = set()
        unique = [r for r in records if not (r.id in seen or seen.add(r.id))][: params.limit]
        nxt = offset + len(unique)
        return ToolResult(
            source=self.name,
            tool="search_genes",
            records=unique,
            total=total,
            next_cursor=str(nxt) if candidates == [query] and unique and nxt < total else None,
            notes=notes + ([] if unique else [f"no GTEx genes match {query!r}"]),
            request_url=url,
        )

    @tool(
        description="""
        Median gene expression (TPM) of one gene in every GTEx tissue, sorted from highest to lowest.
        Returns a gene record (top tissues in attributes) and data.tissues = [{tissue, median_tpm,
        uberon}]. Accepts a symbol (BRAF) or Ensembl ID. Use for tissue specificity / where a target
        is expressed in healthy humans (e.g. on-target toxicity questions). Dataset gtex_v10 by default.
        """
    )
    async def median_expression(self, params: MedianExpressionParams) -> ToolResult:
        gene, url = await self._resolve_gene(params.gene, params.dataset)
        if gene is None:
            return ToolResult(
                source=self.name,
                tool="median_expression",
                notes=[f"gene {params.gene!r} not found in GTEx ({params.dataset}, GENCODE {_GENCODE[params.dataset]})"],
                request_url=url,
            )
        tissue = await self._resolve_tissue(params.tissue, params.dataset)
        response = await self._get(
            "expression/medianGeneExpression",
            {
                "gencodeId": gene["gencodeId"],
                "datasetId": params.dataset,
                "tissueSiteDetailId": tissue,
                "itemsPerPage": 250,
            },
        )
        rows = response.json().get("data") or []
        rows.sort(key=lambda r: r.get("median") or 0, reverse=True)
        table = [
            {"tissue": r.get("tissueSiteDetailId"), "median_tpm": r.get("median"), "uberon": r.get("ontologyId")}
            for r in rows
        ]
        notes = [] if rows else [f"no expression data for {gene.get('geneSymbol')} in {params.dataset}"]
        expressed = [t for t in table if (t["median_tpm"] or 0) >= 1]
        record = _gene_record(
            gene,
            {
                "dataset": params.dataset,
                "unit": "TPM (median across samples)",
                "top_tissues": [f"{t['tissue']}: {t['median_tpm']}" for t in table[:5]],
                "tissues_with_tpm_ge_1": len(expressed),
                "tissue_count": len(table),
            },
        )
        record.url = f"{PORTAL}/gene/{gene.get('geneSymbol') or _strip_version(gene.get('gencodeId'))}"
        return ToolResult(
            source=self.name,
            tool="median_expression",
            records=[record],
            total=len(table),
            data={"dataset": params.dataset, "gencode_id": gene["gencodeId"], "unit": "TPM", "tissues": table},
            notes=notes,
            request_url=response.url,
        )

    @tool(
        description="""
        Significant single-tissue cis-eQTLs from GTEx for a gene and/or a variant, most significant
        first. Each record is one variant-gene-tissue association with normalized effect size (NES:
        sign = direction of the alt allele's effect on expression) and p-value; identifiers carry the
        variant's rsID and gnomAD-style ID. data.by_tissue summarizes eQTL counts and best p-value
        per tissue. Gene: symbol or Ensembl ID. Variant: rsID, GTEx ID (chr7_140994094_G_T_b38) or
        GRCh38 chrom-pos-ref-alt. Optional tissue filter. Use to connect GWAS/clinical variants to
        target genes and tissues.
        """
    )
    async def single_tissue_eqtls(self, params: EqtlParams) -> ToolResult:
        if not (params.gene or params.variant):
            raise SourceError(self.name, "single_tissue_eqtls needs gene and/or variant")
        notes: list[str] = []
        query: dict[str, Any] = {"datasetId": params.dataset, "itemsPerPage": _EQTL_FETCH, "page": 0}
        if params.gene:
            gene, url = await self._resolve_gene(params.gene, params.dataset)
            if gene is None:
                return ToolResult(
                    source=self.name,
                    tool="single_tissue_eqtls",
                    notes=[f"gene {params.gene!r} not found in GTEx ({params.dataset})"],
                    request_url=url,
                )
            query["gencodeId"] = gene["gencodeId"]
        if params.variant:
            variant_id, vnotes = await self._variant_id(params.variant, params.dataset)
            notes += vnotes
            if variant_id is None:
                return ToolResult(source=self.name, tool="single_tissue_eqtls", notes=notes)
            query["variantId"] = variant_id
        tissue = await self._resolve_tissue(params.tissue, params.dataset)
        if tissue:
            query["tissueSiteDetailId"] = tissue
        response = await self._get("association/singleTissueEqtl", query)
        payload = response.json()
        rows = payload.get("data") or []
        total = int((payload.get("paging_info") or {}).get("totalNumberOfItems") or len(rows))
        if total > len(rows):
            notes.append(
                f"{total} significant eQTLs; only the first {len(rows)} upstream rows were ranked. Add a tissue filter for a complete ranking."
            )
        rows.sort(key=lambda r: (r.get("pValue") is None, r.get("pValue") or 1.0))
        by_tissue: dict[str, dict[str, Any]] = {}
        for r in rows:
            t = r.get("tissueSiteDetailId") or "?"
            entry = by_tissue.setdefault(t, {"tissue": t, "eqtls": 0, "best_p": None, "best_variant": None})
            entry["eqtls"] += 1
            if r.get("pValue") is not None and (entry["best_p"] is None or r["pValue"] < entry["best_p"]):
                entry["best_p"] = r["pValue"]
                entry["best_variant"] = r.get("snpId") or r.get("variantId")
                entry["gene"] = r.get("geneSymbol")
        summary = sorted(by_tissue.values(), key=lambda e: (e["best_p"] is None, e["best_p"] or 1.0))
        offset = parse_offset(params.cursor)
        page = rows[offset : offset + params.limit]
        nxt = offset + len(page)
        if not rows:
            notes.append("no significant eQTLs for this query in " + params.dataset)
        return ToolResult(
            source=self.name,
            tool="single_tissue_eqtls",
            records=[_eqtl_record(r) for r in page],
            total=total,
            next_cursor=str(nxt) if page and nxt < len(rows) else None,
            data={"dataset": params.dataset, "by_tissue": summary[:60]},
            notes=notes,
            request_url=response.url,
        )

    @tool(
        description="""
        List GTEx tissues (tissueSiteDetailId values accepted by the other GTEx tools) with display
        name, UBERON ID, RNA-seq and eQTL sample counts, eGene counts. Returned in data.tissues.
        """
    )
    async def tissues(self, params: TissuesParams) -> ToolResult:
        response = await self._get("dataset/tissueSiteDetail", {"datasetId": params.dataset, "itemsPerPage": 250})
        rows = response.json().get("data") or []
        self._tissue_cache[params.dataset] = rows
        table = [
            {
                k: v
                for k, v in {
                    "tissue": r.get("tissueSiteDetailId"),
                    "name": r.get("tissueSiteDetail"),
                    "site": r.get("tissueSite"),
                    "uberon": r.get("ontologyId"),
                    "rnaseq_samples": (r.get("rnaSeqSampleSummary") or {}).get("totalCount"),
                    "eqtl_samples": (r.get("eqtlSampleSummary") or {}).get("totalCount"),
                    "egenes": r.get("eGeneCount"),
                    "sgenes": r.get("sGeneCount"),
                    "expressed_genes": r.get("expressedGeneCount"),
                }.items()
                if v is not None
            }
            for r in rows
        ]
        return ToolResult(
            source=self.name,
            tool="tissues",
            total=len(table),
            data={"dataset": params.dataset, "tissues": table},
            request_url=response.url,
        )

    @tool(
        description="""
        Top expressed genes in one GTEx tissue, ranked by median TPM (mitochondrial genes excluded
        by default). Returns gene records with median TPM in attributes. Use for tissue marker /
        background-expression questions (e.g. "most highly expressed genes in liver").
        """
    )
    async def top_expressed_genes(self, params: TopExpressedParams) -> ToolResult:
        tissue = await self._resolve_tissue(params.tissue, params.dataset)
        page = parse_offset(params.cursor)
        response = await self._get(
            "expression/topExpressedGene",
            {
                "tissueSiteDetailId": tissue,
                "datasetId": params.dataset,
                "filterMtGene": not params.include_mitochondrial,
                "page": page,
                "itemsPerPage": params.limit,
            },
        )
        payload = response.json()
        rows = payload.get("data") or []
        paging = payload.get("paging_info") or {}
        records = []
        for rank, r in enumerate(rows, start=page * params.limit + 1):
            gencode_id = r.get("gencodeId") or ""
            records.append(
                Record(
                    source="gtex",
                    id=gencode_id,
                    type="gene",
                    title=f"{r.get('geneSymbol')}: {r.get('median')} TPM in {r.get('tissueSiteDetailId')}",
                    url=f"{PORTAL}/gene/{r.get('geneSymbol') or _strip_version(gencode_id)}",
                    identifiers={"ensembl_gene": gencode_id, "gene_symbol": r.get("geneSymbol")},
                    attributes={
                        "rank": rank,
                        "median_tpm": r.get("median"),
                        "tissue": r.get("tissueSiteDetailId"),
                        "tissue_uberon": r.get("ontologyId"),
                        "gencode_id": gencode_id,
                        "dataset": r.get("datasetId"),
                    },
                )
            )
        pages = int(paging.get("numberOfPages") or 0)
        return ToolResult(
            source=self.name,
            tool="top_expressed_genes",
            records=records,
            total=paging.get("totalNumberOfItems"),
            next_cursor=str(page + 1) if rows and page + 1 < pages else None,
            request_url=response.url,
        )
