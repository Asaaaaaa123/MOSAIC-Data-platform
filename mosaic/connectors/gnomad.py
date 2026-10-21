"""gnomAD (Genome Aggregation Database) GraphQL connector.

API: POST https://gnomad.broadinstitute.org/api (GraphQL; schema discoverable by introspection; query
examples in the browser source: https://github.com/broadinstitute/gnomad-browser, e.g.
browser/src/VariantPage/VariantPage.tsx).

Auth: none (keyless). Rate limits: the API counts requests and query cost per IP per clock minute
(graphql-api/src/config.ts defaults: 30 requests/min, cost 100/min, 25 per query) and a front proxy
answers bursts with an HTML ``429``. We therefore pace at ~9 requests/minute, cache every response,
send one GraphQL document per tool call where possible, and handle 429 ourselves (one 20 s back-off,
no blind retries).

Quirks:
- A GraphQL error with ``data: null`` comes back as HTTP 500 (e.g. ``variant_search`` with a
  non-variant query: "Unrecognized query. Search by variant ID, rsID, or ClinVar variation ID."), so
  the free-text search routes variant-looking queries to ``variant_search`` and everything else to
  ``gene_search`` (symbol prefix search; multi-word text is reduced to gene-like tokens).
- Not-found entities return HTTP 200 with ``errors: [{"message": "Variant not found"}]`` and a null field.
- ``gene.variants`` returns every variant in the gene in one payload (PCSK9: ~4.5k variants, 2 MB);
  filtering, sorting and paging happen locally. Very large genes (TTN) are slow.
- Datasets: gnomad_r4 (default; GRCh38, 807k exomes+genomes, ``joint`` frequencies), gnomad_r3
  (GRCh38 genomes) and gnomad_r2_1 (GRCh37). Gene constraint on GRCh38 is gnomAD v4.

Identifiers: variant records -> gnomad_variant (chrom-pos-ref-alt), rsid, clinvar (variation ID),
gene_symbol, hgvs; gene records -> ensembl_gene, gene_symbol, hgnc, entrez_gene, omim,
ensembl_transcript (MANE/canonical), refseq (MANE). The gene of a variant is kept in attributes
(gene_id) rather than as ensembl_gene so variants are not merged into their gene when records are
linked across databases.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Literal

from pydantic import Field

from ..http import SourceError
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, tool

API = "https://gnomad.broadinstitute.org/api"
BROWSER = "https://gnomad.broadinstitute.org"

Dataset = Literal["gnomad_r4", "gnomad_r3", "gnomad_r2_1"]
_REFERENCE = {"gnomad_r4": "GRCh38", "gnomad_r3": "GRCh38", "gnomad_r2_1": "GRCh37"}

_RSID = re.compile(r"^rs\d+$", re.IGNORECASE)
_VARIANT = re.compile(
    r"^(?:chr)?([0-9]{1,2}|X|Y|MT?)[\s:_-]+(\d+)[\s:_-]+([ACGTN]+)[\s:_>/-]+([ACGTN]+)$", re.IGNORECASE
)
_ENSG = re.compile(r"^ENSG\d{11}(?:\.\d+)?$", re.IGNORECASE)

_POPULATIONS = {
    "afr": "African/African American",
    "amr": "Admixed American",
    "asj": "Ashkenazi Jewish",
    "eas": "East Asian",
    "fin": "European (Finnish)",
    "mid": "Middle Eastern",
    "nfe": "European (non-Finnish)",
    "sas": "South Asian",
    "ami": "Amish",
    "oth": "Remaining",
    "remaining": "Remaining",
}

_PLOF = {
    "transcript_ablation", "splice_acceptor_variant", "splice_donor_variant", "stop_gained",
    "frameshift_variant", "start_lost",
}
_MISSENSE = {"missense_variant", "inframe_insertion", "inframe_deletion", "stop_lost", "protein_altering_variant"}
_SPLICE = {"splice_acceptor_variant", "splice_donor_variant", "splice_region_variant",
           "splice_donor_5th_base_variant", "splice_donor_region_variant", "splice_polypyrimidine_tract_variant"}
_CODING = _PLOF | _MISSENSE | {"synonymous_variant", "coding_sequence_variant", "incomplete_terminal_codon_variant"}

_FREQ = "ac an homozygote_count hemizygote_count filters faf95 { popmax popmax_population } populations { id ac an homozygote_count }"
_VARIANT_FIELDS = f"""
    variant_id reference_genome chrom pos ref alt caid rsids flags
    exome {{ {_FREQ} }}
    genome {{ {_FREQ} }}
    joint {{ {_FREQ} }}
    transcript_consequences {{
      gene_id gene_symbol transcript_id transcript_version hgvsc hgvsp is_canonical is_mane_select
      major_consequence lof lof_filter lof_flags polyphen_prediction sift_prediction
    }}
    in_silico_predictors {{ id value }}
"""
_VARIANT_FIELDS_R2 = _VARIANT_FIELDS.replace(f"joint {{ {_FREQ} }}", "")
_CLINVAR_FIELDS = "clinvar_variation_id clinical_significance review_status gold_stars last_evaluated"

_GENE_FIELDS = """
    gene_id gene_version symbol name hgnc_id ncbi_id omim_id chrom start stop strand canonical_transcript_id flags
    mane_select_transcript { ensembl_id ensembl_version refseq_id refseq_version }
    gnomad_constraint {
      exp_lof obs_lof oe_lof oe_lof_lower oe_lof_upper exp_mis obs_mis oe_mis oe_mis_lower oe_mis_upper
      exp_syn obs_syn oe_syn oe_syn_lower oe_syn_upper lof_z mis_z syn_z pLI flags
    }
"""

_MAX_BATCH = 5  # aliased entities per GraphQL document (stay under the per-query cost limit)


class SearchParamsGnomad(SearchParams):
    limit: int = Field(5, ge=1, le=10, description="Maximum number of genes/variants to return (max 10)")
    dataset: Dataset = Field("gnomad_r4", description="gnomAD dataset for variant hits (gnomad_r4 = v4, GRCh38)")


class GeneConstraintParams(ToolParams):
    gene: str = Field(description="Gene symbol (PCSK9) or Ensembl gene ID (ENSG00000169174)")
    reference_genome: Literal["GRCh38", "GRCh37"] = Field(
        "GRCh38", description="GRCh38 = gnomAD v4 constraint (default); GRCh37 = gnomAD v2.1.1 constraint"
    )


class GetVariantParams(ToolParams):
    variant: str = Field(
        description="gnomAD variant ID chrom-pos-ref-alt (1-55051215-G-GA; chr1:55051215 G>GA also accepted) or an rsID (rs429358)"
    )
    dataset: Dataset = Field("gnomad_r4", description="gnomad_r4 (GRCh38, default), gnomad_r3 (GRCh38 genomes), gnomad_r2_1 (GRCh37)")


class GeneVariantsParams(ToolParams):
    gene: str = Field(description="Gene symbol (PCSK9) or Ensembl gene ID")
    consequence: Literal["all", "plof", "plof_hc", "missense", "synonymous", "splice", "coding", "clinvar_pathogenic"] = Field(
        "plof",
        description=(
            "Filter: plof = predicted loss-of-function (stop_gained, frameshift, splice donor/acceptor, start_lost); "
            "plof_hc = LOFTEE high-confidence pLoF only; missense (incl. in-frame indels); synonymous; splice; "
            "coding; clinvar_pathogenic = ClinVar pathogenic/likely pathogenic; all"
        ),
    )
    max_af: float | None = Field(None, ge=0, le=1, description="Only variants with allele frequency <= this (e.g. 0.001 for rare)")
    sort: Literal["af", "ac", "position"] = Field("af", description="af / ac = most common first; position = genomic order")
    dataset: Dataset = Field("gnomad_r4", description="gnomad_r4 (default), gnomad_r3, gnomad_r2_1 (GRCh37)")
    limit: int = Field(25, ge=1, le=100, description="Maximum number of variants to return")
    cursor: str | None = Field(None, description="Pagination cursor (offset) from a previous result")


# ---------------------------------------------------------------------------


def normalize_variant_id(text: str) -> str | None:
    match = _VARIANT.match(text.strip())
    if not match:
        return None
    chrom, pos, ref, alt = match.groups()
    chrom = chrom.upper()
    chrom = "M" if chrom in ("M", "MT") else chrom
    return f"{chrom}-{pos}-{ref.upper()}-{alt.upper()}"


def _af(block: dict[str, Any] | None) -> float | None:
    if not block or not block.get("an"):
        return None
    return (block.get("ac") or 0) / block["an"]


def _combined(variant: dict[str, Any]) -> dict[str, Any]:
    """Joint (exome + genome) counts: v4 ``joint`` when present, otherwise summed."""
    if variant.get("joint"):
        return variant["joint"]
    out: dict[str, Any] = {"ac": 0, "an": 0, "homozygote_count": 0, "hemizygote_count": 0, "filters": []}
    pops: dict[str, dict[str, int]] = {}
    for key in ("exome", "genome"):
        block = variant.get(key) or {}
        if not block:
            continue
        for field in ("ac", "an", "homozygote_count", "hemizygote_count"):
            out[field] += block.get(field) or 0
        out["filters"] += [f"{key}:{f}" for f in block.get("filters") or []]
        for pop in block.get("populations") or []:
            agg = pops.setdefault(pop["id"], {"id": pop["id"], "ac": 0, "an": 0, "homozygote_count": 0})
            agg["ac"] += pop.get("ac") or 0
            agg["an"] += pop.get("an") or 0
            agg["homozygote_count"] += pop.get("homozygote_count") or 0
    out["populations"] = list(pops.values())
    return out


def _top_populations(block: dict[str, Any], n: int = 4) -> list[dict[str, Any]]:
    rows = []
    for pop in block.get("populations") or []:
        pid = pop.get("id") or ""
        if not pid or "_" in pid or ":" in pid or pid in ("XX", "XY") or not pop.get("an"):
            continue
        rows.append(
            {
                "population": _POPULATIONS.get(pid, pid),
                "af": round(pop["ac"] / pop["an"], 6),
                "ac": pop.get("ac"),
                "an": pop.get("an"),
                "homozygotes": pop.get("homozygote_count"),
            }
        )
    rows.sort(key=lambda r: r["af"], reverse=True)
    return rows[:n]


def _main_consequence(consequences: list[dict[str, Any]]) -> dict[str, Any]:
    if not consequences:
        return {}
    return sorted(
        consequences,
        key=lambda c: (not c.get("is_mane_select"), not c.get("is_canonical")),
    )[0]


def _variant_record(variant: dict[str, Any], dataset: str, clinvar: dict[str, Any] | None = None) -> Record:
    vid = variant.get("variant_id") or ""
    csq = _main_consequence(variant.get("transcript_consequences") or [])
    joint = _combined(variant)
    af = _af(joint)
    rsids = variant.get("rsids") or []
    gene = csq.get("gene_symbol")
    change = csq.get("hgvsp") or csq.get("hgvsc")
    consequence = csq.get("major_consequence")
    title = vid
    if gene or change or consequence:
        title += " (" + ", ".join(x for x in (" ".join(y for y in (gene, change) if y), consequence) if x) + ")"
    faf = (joint.get("faf95") or {}) if isinstance(joint, dict) else {}
    predictors = {p.get("id"): p.get("value") for p in variant.get("in_silico_predictors") or [] if p.get("id")}
    clin = clinvar or {}
    summary_bits = []
    if af is not None:
        summary_bits.append(f"Allele frequency {af:.3g} ({joint.get('ac')}/{joint.get('an')} alleles, {joint.get('homozygote_count') or 0} homozygotes) in {dataset}")
    else:
        summary_bits.append(f"Not observed with allele-count data in {dataset}")
    if consequence:
        summary_bits.append(f"{consequence.replace('_', ' ')} in {gene or csq.get('gene_id')}" + (f" ({change})" if change else ""))
    if clin.get("clinical_significance"):
        summary_bits.append(f"ClinVar: {clin['clinical_significance']} ({clin.get('gold_stars', 0)} stars)")
    attributes = {
        "dataset": dataset,
        "reference_genome": variant.get("reference_genome"),
        "chrom": variant.get("chrom"),
        "pos": variant.get("pos"),
        "ref": variant.get("ref"),
        "alt": variant.get("alt"),
        "af": round(af, 8) if af is not None else None,
        "ac": joint.get("ac"),
        "an": joint.get("an"),
        "homozygotes": joint.get("homozygote_count"),
        "hemizygotes": joint.get("hemizygote_count") or None,
        "exome_af": round(_af(variant.get("exome")), 8) if _af(variant.get("exome")) is not None else None,
        "genome_af": round(_af(variant.get("genome")), 8) if _af(variant.get("genome")) is not None else None,
        "faf95_popmax": faf.get("popmax"),
        "faf95_popmax_population": _POPULATIONS.get(faf.get("popmax_population") or "", faf.get("popmax_population")),
        "top_populations": _top_populations(joint),
        "filters": joint.get("filters") or None,
        "flags": variant.get("flags") or None,
        "gene_symbol": gene,
        "gene_id": csq.get("gene_id"),
        "transcript": f"{csq['transcript_id']}.{csq['transcript_version']}" if csq.get("transcript_id") and csq.get("transcript_version") else csq.get("transcript_id"),
        "mane_select": csq.get("is_mane_select"),
        "consequence": consequence,
        "hgvsc": csq.get("hgvsc"),
        "hgvsp": csq.get("hgvsp"),
        "lof": csq.get("lof"),
        "lof_filter": csq.get("lof_filter"),
        "polyphen": csq.get("polyphen_prediction"),
        "sift": csq.get("sift_prediction"),
        "in_silico": predictors or None,
        "clinvar_significance": clin.get("clinical_significance"),
        "clinvar_review_status": clin.get("review_status"),
        "clinvar_gold_stars": clin.get("gold_stars"),
        "clinvar_last_evaluated": clin.get("last_evaluated"),
        "caid": variant.get("caid"),
        "rsids": rsids if len(rsids) > 1 else None,
    }
    transcript = attributes["transcript"]
    return Record(
        source="gnomad",
        id=vid,
        type="variant",
        title=title,
        url=f"{BROWSER}/variant/{vid}?dataset={dataset}" if vid else None,
        identifiers={
            "gnomad_variant": vid,
            "rsid": rsids,
            "clinvar": clin.get("clinvar_variation_id"),
            "gene_symbol": gene,
            "hgvs": f"{transcript}:{csq['hgvsc']}" if transcript and csq.get("hgvsc") else None,
        },
        summary=". ".join(summary_bits) + ".",
        attributes={k: v for k, v in attributes.items() if v not in (None, [], {}, "")},
    )


def _gene_variant_record(variant: dict[str, Any], dataset: str, clinvar: dict[str, Any] | None) -> Record:
    """Record for one row of ``gene.variants`` (flat consequence fields)."""
    shaped = dict(variant)
    shaped["transcript_consequences"] = [
        {
            "gene_symbol": variant.get("gene_symbol"),
            "gene_id": variant.get("gene_id"),
            "transcript_id": variant.get("transcript_id"),
            "transcript_version": variant.get("transcript_version"),
            "hgvsc": variant.get("hgvsc"),
            "hgvsp": variant.get("hgvsp"),
            "major_consequence": variant.get("consequence"),
            "lof": variant.get("lof"),
            "lof_filter": variant.get("lof_filter"),
            "is_canonical": True,
        }
    ]
    return _variant_record(shaped, dataset, clinvar)


def _gene_record(gene: dict[str, Any], reference: str) -> Record:
    gid = gene.get("gene_id") or ""
    c = gene.get("gnomad_constraint") or {}
    mane = gene.get("mane_select_transcript") or {}
    version = "v4" if reference == "GRCh38" else "v2.1.1"
    summary = None
    if c:
        parts = []
        if c.get("pLI") is not None:
            parts.append(f"pLI={c['pLI']:.3g}")
        if c.get("oe_lof_upper") is not None:
            parts.append(f"LOEUF={c['oe_lof_upper']:.3g}")
        if c.get("oe_lof") is not None:
            parts.append(f"oe_lof={c['oe_lof']:.3g} ({c.get('obs_lof')}/{c.get('exp_lof', 0):.1f} obs/exp pLoF)")
        if c.get("mis_z") is not None:
            parts.append(f"missense Z={c['mis_z']:.3g} (oe_mis={c.get('oe_mis', 0):.3g})")
        if c.get("syn_z") is not None:
            parts.append(f"synonymous Z={c['syn_z']:.3g}")
        verdict = ""
        loeuf = c.get("oe_lof_upper")
        if loeuf is not None:
            verdict = (
                " Strongly LoF-intolerant (LOEUF < 0.6)."
                if loeuf < 0.6
                else " Not LoF-intolerant (LOEUF >= 0.6)."
            )
        summary = f"gnomAD {version} constraint for {gene.get('symbol')}: " + ", ".join(parts) + "." + verdict
    attributes = {
        "name": gene.get("name"),
        "reference_genome": reference,
        "constraint_version": version if c else None,
        "location": f"{gene.get('chrom')}:{gene.get('start')}-{gene.get('stop')} ({gene.get('strand')})" if gene.get("start") else None,
        "canonical_transcript": gene.get("canonical_transcript_id"),
        "mane_select": f"{mane.get('ensembl_id')}.{mane.get('ensembl_version')} / {mane.get('refseq_id')}.{mane.get('refseq_version')}" if mane else None,
        "pLI": c.get("pLI"),
        "loeuf": c.get("oe_lof_upper"),
        "oe_lof": c.get("oe_lof"),
        "oe_lof_ci": [c.get("oe_lof_lower"), c.get("oe_lof_upper")] if c.get("oe_lof_lower") is not None else None,
        "obs_lof": c.get("obs_lof"),
        "exp_lof": round(c["exp_lof"], 2) if c.get("exp_lof") is not None else None,
        "lof_z": c.get("lof_z"),
        "mis_z": c.get("mis_z"),
        "oe_mis": c.get("oe_mis"),
        "oe_mis_ci": [c.get("oe_mis_lower"), c.get("oe_mis_upper")] if c.get("oe_mis_lower") is not None else None,
        "obs_mis": c.get("obs_mis"),
        "exp_mis": round(c["exp_mis"], 2) if c.get("exp_mis") is not None else None,
        "syn_z": c.get("syn_z"),
        "oe_syn": c.get("oe_syn"),
        "constraint_flags": c.get("flags") or None,
        "gene_flags": gene.get("flags") or None,
    }
    return Record(
        source="gnomad",
        id=gid,
        type="gene",
        title=f"{gene.get('symbol')}: {gene.get('name')}" if gene.get("name") else (gene.get("symbol") or gid),
        url=f"{BROWSER}/gene/{gid}?dataset={'gnomad_r4' if reference == 'GRCh38' else 'gnomad_r2_1'}" if gid else None,
        identifiers={
            "ensembl_gene": gid,
            "gene_symbol": gene.get("symbol"),
            "hgnc": gene.get("hgnc_id"),
            "entrez_gene": gene.get("ncbi_id"),
            "omim": gene.get("omim_id"),
            "ensembl_transcript": mane.get("ensembl_id") or gene.get("canonical_transcript_id"),
            "refseq": mane.get("refseq_id"),
        },
        summary=summary,
        attributes={k: v for k, v in attributes.items() if v not in (None, [], {}, "")},
    )


def _gene_arg(gene: str) -> tuple[str, str]:
    """GraphQL argument name and value for a gene symbol or Ensembl ID."""
    text = gene.strip()
    if _ENSG.match(text):
        return "gene_id", text.upper().split(".")[0]
    return "gene_symbol", text


def _clinvar_pathogenic(significance: str | None) -> bool:
    text = (significance or "").lower()
    return "pathogenic" in text and "conflicting" not in text and "benign" not in text


class Gnomad(Connector):
    name = "gnomad"
    title = "gnomAD (Genome Aggregation Database)"
    category = "genomics"
    description = (
        "gnomAD v4: allele frequencies from 807k human exomes and genomes across ancestry groups, variant "
        "consequences and ClinVar status, plus gene-level loss-of-function and missense constraint (pLI, "
        "LOEUF). Best for 'how common is this variant?', 'is this gene LoF-intolerant?' and listing "
        "pLoF/missense variants in a gene."
    )
    homepage = "https://gnomad.broadinstitute.org/"
    docs_url = "https://gnomad.broadinstitute.org/help"
    auth = "none"
    rate_limit = 0.15  # ~9 requests/minute: stay under the API's per-minute counters and proxy limit
    rate_burst = 1.0
    health_query = "PCSK9"
    nih_repositories = ("gnomAD Genome Aggregation Database",)

    async def _query(self, query: str, variables: dict[str, Any] | None = None, timeout: float | None = None) -> tuple[dict[str, Any], list[str]]:
        """POST one GraphQL document. Returns (data, error messages).

        Handles gnomAD specifics: HTTP 500 for errors with null data (no blind retries), and 429 with a
        single 20 s back-off. Responses are cached like GETs.
        """
        ttl = self.http.cache.default_ttl if self.http.cache else 0
        body = {"query": query, "variables": variables or {}}
        for attempt in range(2):
            response = await self.request(
                "POST",
                API,
                json_body=body,
                cache_ttl=ttl,
                retries=0,
                timeout=timeout,
                allow_status=(400, 429, 500, 502, 503),
            )
            if response.status == 429 and attempt == 0:
                await asyncio.sleep(20)
                continue
            if response.status == 429:
                raise SourceError(
                    self.name, "gnomAD rate limit exceeded; wait a minute and retry", status=429, retryable=True,
                    url=response.url,
                )
            try:
                payload = response.json()
            except ValueError:
                raise SourceError(
                    self.name, f"gnomAD returned HTTP {response.status}: {response.text[:200]}",
                    status=response.status, retryable=response.status >= 500, url=response.url,
                ) from None
            errors = [str(e.get("message", e)) for e in payload.get("errors") or [] if isinstance(e, dict)]
            data = payload.get("data")
            if data is None:
                message = "; ".join(errors) or f"HTTP {response.status}"
                if "rate limit" in message.lower() and attempt == 0:
                    await asyncio.sleep(20)
                    continue
                raise SourceError(self.name, f"gnomAD GraphQL error: {message}", status=response.status, url=response.url)
            return data, errors
        raise SourceError(self.name, "gnomAD request failed")  # pragma: no cover

    async def _variants_by_id(self, ids: list[str], dataset: str) -> tuple[list[Record], list[str]]:
        """Fetch details (+ ClinVar) for up to _MAX_BATCH variant IDs in one aliased GraphQL document."""
        ids = ids[:_MAX_BATCH]
        reference = _REFERENCE[dataset]
        fields = _VARIANT_FIELDS if dataset == "gnomad_r4" else _VARIANT_FIELDS_R2
        parts = []
        for i, vid in enumerate(ids):
            literal = json.dumps(vid)
            parts.append(f"v{i}: variant(variantId: {literal}, dataset: {dataset}) {{ {fields} }}")
            parts.append(f"c{i}: clinvar_variant(variant_id: {literal}, reference_genome: {reference}) {{ {_CLINVAR_FIELDS} }}")
        data, errors = await self._query("{ " + "\n".join(parts) + " }")
        records = []
        for i, _ in enumerate(ids):
            variant = data.get(f"v{i}")
            if variant:
                records.append(_variant_record(variant, dataset, data.get(f"c{i}")))
        notes = sorted({e for e in errors if "not found" not in e.lower()})
        return records, notes

    # ---- tools ----
    @tool(
        description="""
        Search gnomAD by gene symbol (prefix match, e.g. "PCSK9", "BRCA") or by variant: a variant
        ID (1-55051215-G-GA), an rsID (rs429358) or a ClinVar variation ID (digits). Gene hits come
        back as gene records with LoF/missense constraint (pLI, LOEUF); variant hits as variant
        records with allele frequency, top populations, consequence and ClinVar status. Multi-word
        text is reduced to its gene-like tokens (gnomAD has no free-text search). Max 10 results;
        calls are rate-limited (~9/min), so prefer the specific tools when you know what you need.
        """,
        federated=True,
    )
    async def search(self, params: SearchParamsGnomad) -> ToolResult:
        query = params.query.strip()
        dataset = params.dataset
        variant_id = normalize_variant_id(query)
        if variant_id or _RSID.match(query) or query.isdigit():
            if variant_id:
                ids = [variant_id]
            else:
                data, _ = await self._query(
                    "query($q: String!) { variant_search(query: $q, dataset: %s) { variant_id } }" % dataset,
                    {"q": query.lower() if _RSID.match(query) else query},
                )
                ids = [v.get("variant_id") for v in data.get("variant_search") or [] if v.get("variant_id")]
            if not ids:
                return ToolResult(source=self.name, tool="search", total=0, notes=[f"no gnomAD variant matches {query!r} in {dataset}"])
            records, notes = await self._variants_by_id(ids[: params.limit], dataset)
            if len(ids) > len(records) and not records:
                notes.append(f"{query} is not present in {dataset}")
            return ToolResult(
                source=self.name, tool="search", records=records, total=len(ids), notes=notes, request_url=API
            )

        reference = "GRCh37" if dataset == "gnomad_r2_1" else "GRCh38"
        candidates = [query]
        tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-.]*", query)
        if len(tokens) > 1:
            candidates = [t for t in tokens if re.search(r"[A-Za-z]", t) and (t.isupper() or re.search(r"\d", t))][:2]
        hits: list[dict[str, Any]] = []
        for candidate in candidates:
            if _ENSG.match(candidate):
                hits.append({"ensembl_id": candidate.upper().split(".")[0], "symbol": None})
                continue
            data, _ = await self._query(
                "query($q: String!) { gene_search(query: $q, reference_genome: %s) { ensembl_id symbol } }" % reference,
                {"q": candidate},
            )
            found = data.get("gene_search") or []
            exact = [g for g in found if (g.get("symbol") or "").upper() == candidate.upper()]
            hits.extend(exact + [g for g in found if g not in exact])
            if hits:
                break
        seen: set[str] = set()
        hits = [h for h in hits if h.get("ensembl_id") and not (h["ensembl_id"] in seen or seen.add(h["ensembl_id"]))]
        notes = [] if candidates == [query] else [f"gnomAD has no free-text search; searched gene token(s): {', '.join(candidates) or 'none'}"]
        if not hits:
            return ToolResult(source=self.name, tool="search", total=0, notes=notes + [f"no gnomAD gene matches {query!r}"])
        page = hits[: min(params.limit, _MAX_BATCH)]
        parts = [
            f"g{i}: gene(gene_id: {json.dumps(h['ensembl_id'])}, reference_genome: {reference}) {{ {_GENE_FIELDS} }}"
            for i, h in enumerate(page)
        ]
        data, _ = await self._query("{ " + "\n".join(parts) + " }")
        records = [_gene_record(data[f"g{i}"], reference) for i in range(len(page)) if data.get(f"g{i}")]
        return ToolResult(
            source=self.name, tool="search", records=records, total=len(hits), notes=notes, request_url=API
        )

    @tool(
        description="""
        gnomAD gene constraint for one gene: pLI, LOEUF (oe_lof_upper; < 0.6 = LoF-intolerant),
        observed/expected pLoF, missense Z and oe_mis, synonymous Z, plus HGNC/NCBI/OMIM/MANE
        cross-references. Accepts a symbol or Ensembl gene ID. GRCh38 = gnomAD v4 constraint
        (default); GRCh37 = v2.1.1. Use to judge haploinsufficiency / target safety (e.g. is a
        drug target tolerant of loss of function?).
        """
    )
    async def gene_constraint(self, params: GeneConstraintParams) -> ToolResult:
        arg, value = _gene_arg(params.gene)
        data, errors = await self._query(
            "query($v: String!) { gene(%s: $v, reference_genome: %s) { %s } }" % (arg, params.reference_genome, _GENE_FIELDS),
            {"v": value},
        )
        gene = data.get("gene")
        if not gene:
            return ToolResult(
                source=self.name,
                tool="gene_constraint",
                notes=[f"gene {params.gene!r} not found in gnomAD ({params.reference_genome})"] + [e for e in errors if "not found" not in e.lower()],
                request_url=API,
            )
        record = _gene_record(gene, params.reference_genome)
        notes = [] if gene.get("gnomad_constraint") else ["gnomAD reports no constraint metrics for this gene (e.g. too few expected variants)"]
        return ToolResult(source=self.name, tool="gene_constraint", records=[record], total=1, notes=notes, request_url=API)

    @tool(
        description="""
        Look up one variant in gnomAD: exome, genome and combined allele counts/frequencies,
        homozygote counts, filtering allele frequency (faf95 popmax), the top ancestry groups by
        frequency, VEP consequence on the MANE/canonical transcript (HGVSc/HGVSp, LOFTEE),
        in-silico scores (CADD, SpliceAI, REVEL, ...) and ClinVar significance/review status.
        Accepts chrom-pos-ref-alt (GRCh38 for gnomad_r4/r3, e.g. 19-44908684-T-C) or an rsID
        (multi-allelic rsIDs return each allele). Absent from gnomAD = empty result with a note.
        """
    )
    async def get_variant(self, params: GetVariantParams) -> ToolResult:
        text = params.variant.strip()
        variant_id = normalize_variant_id(text)
        notes: list[str] = []
        if variant_id:
            ids = [variant_id]
        elif _RSID.match(text):
            data, _ = await self._query(
                "query($q: String!) { variant_search(query: $q, dataset: %s) { variant_id } }" % params.dataset,
                {"q": text.lower()},
            )
            ids = [v.get("variant_id") for v in data.get("variant_search") or [] if v.get("variant_id")]
            if len(ids) > _MAX_BATCH:
                notes.append(f"{text} matches {len(ids)} variants; showing the first {_MAX_BATCH}")
        else:
            raise SourceError(self.name, f"{text!r} is not a variant ID (chrom-pos-ref-alt) or rsID")
        if not ids:
            return ToolResult(source=self.name, tool="get_variant", notes=[f"{text} not found in gnomAD {params.dataset}"])
        records, extra = await self._variants_by_id(ids, params.dataset)
        notes += extra
        if not records:
            notes.append(f"{text} not found in gnomAD {params.dataset} (never observed or not called)")
        return ToolResult(source=self.name, tool="get_variant", records=records, total=len(records), notes=notes, request_url=API)

    @tool(
        description="""
        List variants observed in gnomAD within one gene, filtered by consequence (default plof:
        stop-gained, frameshift, essential splice, start-lost; also plof_hc, missense, synonymous,
        splice, coding, clinvar_pathogenic, all) and optional max_af, sorted by allele frequency
        (default) or position. Each variant record has HGVS, consequence, LOFTEE call, allele
        count/frequency, homozygotes and ClinVar significance; data.counts summarizes the gene's
        variants by class. Accepts a symbol or Ensembl gene ID. Only variants with AC > 0 are shown.
        """
    )
    async def gene_variants(self, params: GeneVariantsParams) -> ToolResult:
        arg, value = _gene_arg(params.gene)
        reference = _REFERENCE[params.dataset]
        joint = "joint { ac an homozygote_count hemizygote_count filters }" if params.dataset == "gnomad_r4" else ""
        query = """query($v: String!) { gene(%s: $v, reference_genome: %s) {
            gene_id symbol
            variants(dataset: %s) {
              variant_id pos rsids consequence gene_id gene_symbol transcript_id transcript_version hgvsc hgvsp
              lof lof_filter flags
              exome { ac an homozygote_count hemizygote_count filters }
              genome { ac an homozygote_count hemizygote_count filters }
              %s
            }
            clinvar_variants { variant_id clinvar_variation_id clinical_significance review_status gold_stars }
        } }""" % (arg, reference, params.dataset, joint)
        data, errors = await self._query(query, {"v": value}, timeout=120)
        gene = data.get("gene")
        if not gene:
            return ToolResult(
                source=self.name,
                tool="gene_variants",
                notes=[f"gene {params.gene!r} not found in gnomAD ({reference})"] + [e for e in errors if "not found" not in e.lower()],
                request_url=API,
            )
        clinvar = {c.get("variant_id"): c for c in gene.get("clinvar_variants") or [] if c.get("variant_id")}
        observed = []
        counts = {"observed": 0, "plof": 0, "plof_hc": 0, "missense": 0, "synonymous": 0, "clinvar_pathogenic": 0}
        for v in gene.get("variants") or []:
            total = _combined(v)
            if not total.get("ac"):
                continue
            v["_af"] = _af(total) or 0.0
            v["_ac"] = total.get("ac") or 0
            csq = v.get("consequence")
            cv = clinvar.get(v.get("variant_id"))
            v["_classes"] = {
                "plof": csq in _PLOF,
                "plof_hc": v.get("lof") == "HC",
                "missense": csq in _MISSENSE,
                "synonymous": csq == "synonymous_variant",
                "splice": csq in _SPLICE,
                "coding": csq in _CODING,
                "clinvar_pathogenic": bool(cv and _clinvar_pathogenic(cv.get("clinical_significance"))),
                "all": True,
            }
            counts["observed"] += 1
            for key in ("plof", "plof_hc", "missense", "synonymous", "clinvar_pathogenic"):
                counts[key] += int(v["_classes"][key])
            observed.append(v)
        selected = [v for v in observed if v["_classes"][params.consequence]]
        if params.max_af is not None:
            selected = [v for v in selected if v["_af"] <= params.max_af]
        if params.sort == "position":
            selected.sort(key=lambda v: v.get("pos") or 0)
        else:
            key = "_af" if params.sort == "af" else "_ac"
            selected.sort(key=lambda v: v[key], reverse=True)
        offset = parse_offset(params.cursor)
        page = selected[offset : offset + params.limit]
        records = []
        for v in page:
            clean = {k: val for k, val in v.items() if not k.startswith("_")}
            records.append(_gene_variant_record(clean, params.dataset, clinvar.get(v.get("variant_id"))))
        nxt = offset + len(page)
        notes = [] if selected else [f"no {params.consequence} variants with AC > 0 in {gene.get('symbol')} ({params.dataset})"]
        return ToolResult(
            source=self.name,
            tool="gene_variants",
            records=records,
            total=len(selected),
            next_cursor=str(nxt) if page and nxt < len(selected) else None,
            data={
                "gene": gene.get("symbol"),
                "gene_id": gene.get("gene_id"),
                "dataset": params.dataset,
                "filter": params.consequence,
                "counts": counts,
                "clinvar_variants_in_gene": len(clinvar),
            },
            notes=notes,
            request_url=API,
        )
