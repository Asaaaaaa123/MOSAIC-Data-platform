"""NHGRI-EBI GWAS Catalog connector (REST API v2).

Docs: https://www.ebi.ac.uk/gwas/rest/api/v2/docs (reference: .../v2/docs/reference)
Base: https://www.ebi.ac.uk/gwas/rest/api/v2 . The legacy v1 API (``/gwas/rest/api``)
was retired in 2025 and now answers HTTP 410, so only v2 is used here.

Auth: none. Rate limit: 15 requests/second documented; we use 5/s.

Quirks:
* HAL-style JSON: lists live under ``_embedded.<collection>`` and paging under
  ``page`` (``size``, ``totalElements``, ``number``). Pagination is page-based
  (``page`` + ``size``); our cursor is an offset, converted to ``page = offset // limit``
  (pass the same ``limit`` when paging).
* ``p_value`` underflows to 0.0 for very small values; ``pvalue_mantissa`` and
  ``pvalue_exponent`` carry the real value, so we render p-values as ``"3e-1315"``.
* Associations carry ``or_value`` (odds ratio) or a free-text ``beta`` ("0.28 unit
  decrease"), plus ``range`` (95% CI text) and ``ci_lower``/``ci_upper``.
* Trait ids are mixed ontologies: EFO_, MONDO_, HP_, OBA_, GO_ ... They are mapped to
  the canonical keys ``efo`` (EFO_#######), ``mondo`` (MONDO:#######), ``hp``, ``go``;
  the full trait list is kept in ``attributes.efo_traits``.
* Unknown study accession -> 404; empty searches -> 200 with no ``_embedded``.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import quote

from pydantic import Field

from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, tool

API = "https://www.ebi.ac.uk/gwas/rest/api/v2"
WEB = "https://www.ebi.ac.uk/gwas"

_GCST = re.compile(r"^GCST\d+$", re.IGNORECASE)
_RSID = re.compile(r"^rs\d+$", re.IGNORECASE)
_TRAIT_ID = re.compile(r"^([A-Za-z]+)[_:](\d+)$")


class GwasSearchParams(SearchParams):
    kind: Literal["studies", "traits"] = Field(
        "studies",
        description="studies (default): GWAS studies whose mapped/reported trait matches the query; "
        "traits: ontology trait terms (EFO/MONDO/HP...) whose label matches, to get ids for associations_for_trait",
    )


class VariantAssocParams(ToolParams):
    rsid: str = Field(description="dbSNP rsID, e.g. rs7903146")
    limit: int = Field(20, ge=1, le=100, description="Maximum number of associations (sorted by p-value)")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous call)")


class GeneAssocParams(ToolParams):
    gene: str = Field(description="HGNC gene symbol, e.g. TCF7L2, APOE")
    extended_geneset: bool = Field(
        False,
        description="false (default): genes mapped by Ensembl overlap/nearest gene (as on the website); "
        "true: also genes within the extended upstream/downstream window",
    )
    limit: int = Field(20, ge=1, le=100, description="Maximum number of associations (sorted by p-value)")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous call)")


class TraitAssocParams(ToolParams):
    trait: str = Field(
        description="Trait ontology id (EFO_0001360, MONDO_0005148, MONDO:0005148, HP_0000822) or an exact "
        "trait label (e.g. 'type 2 diabetes mellitus'). Ids are more precise; labels that do not match "
        "exactly are resolved to the best matching trait term."
    )
    show_child_traits: bool = Field(
        True, description="Include associations annotated to more specific child traits (default true)"
    )
    limit: int = Field(20, ge=1, le=100, description="Maximum number of associations (strongest p-values first)")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous call)")


class GetStudyParams(ToolParams):
    accession: str = Field(description="GWAS Catalog study accession, e.g. GCST000392 or GCST90132222")


def trait_identifiers(trait_id: str | None) -> dict[str, str]:
    """Map a GWAS Catalog trait id (EFO_..., MONDO_..., HP_...) to canonical identifier keys."""
    if not trait_id:
        return {}
    match = _TRAIT_ID.match(trait_id.strip())
    if not match:
        return {}
    prefix, num = match.group(1).upper(), match.group(2)
    if prefix == "EFO":
        return {"efo": f"EFO_{num}"}
    if prefix in ("MONDO", "HP", "GO", "DOID"):
        return {prefix.lower(): f"{prefix}:{num}"}
    if prefix == "ORPHANET":
        return {"orphanet": num}
    return {}


def _trait_list(items: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    return [
        {"id": t.get("efo_id"), "label": t.get("efo_trait")}
        for t in items or []
        if t.get("efo_id") or t.get("efo_trait")
    ]


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {}, "-", "NR", "NA")}


def _pvalue(item: dict[str, Any]) -> str | None:
    mant, exp = item.get("pvalue_mantissa"), item.get("pvalue_exponent")
    if mant is not None and exp is not None:
        return f"{mant}e{exp}"
    value = item.get("p_value")
    return None if value is None else str(value)


def _embedded(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return ((payload or {}).get("_embedded") or {}).get(key) or []


def _page(payload: dict[str, Any], offset: int, returned: int) -> tuple[int | None, str | None]:
    total = ((payload or {}).get("page") or {}).get("totalElements")
    nxt = offset + returned
    return total, (str(nxt) if returned and total is not None and nxt < total else None)


def study_record(item: dict[str, Any], publication: dict[str, Any] | None = None) -> Record:
    acc = item.get("accession_id") or ""
    traits = _trait_list(item.get("efo_traits"))
    identifiers: dict[str, Any] = {"gwas_study": acc, "pmid": item.get("pubmed_id")}
    for t in traits:
        for key, value in trait_identifiers(t["id"]).items():
            identifiers.setdefault(key, value)
    parts = []
    if item.get("initial_sample_size"):
        parts.append(f"Discovery sample: {item['initial_sample_size']}.")
    if item.get("replication_sample_size") and item["replication_sample_size"] != "NA":
        parts.append(f"Replication sample: {item['replication_sample_size']}.")
    if traits:
        parts.append("Mapped traits: " + "; ".join(f"{t['label']} ({t['id']})" for t in traits) + ".")
    pub = publication or {}
    authors = [a.get("full_name") for a in pub.get("authors") or [] if a.get("full_name")]
    return Record(
        source="gwas_catalog",
        id=acc,
        type="study",
        title=item.get("disease_trait") or (traits[0]["label"] if traits else acc),
        url=f"{WEB}/studies/{acc}" if acc else None,
        identifiers=identifiers,
        date=pub.get("publication_date"),
        authors=authors,
        summary=" ".join(parts) or None,
        attributes=_compact(
            {
                "reported_trait": item.get("disease_trait"),
                "efo_traits": traits,
                "background_traits": _trait_list(item.get("bg_efo_traits")),
                "initial_sample_size": item.get("initial_sample_size"),
                "replication_sample_size": item.get("replication_sample_size"),
                "discovery_ancestry": item.get("discovery_ancestry"),
                "replication_ancestry": item.get("replication_ancestry"),
                "cohort": item.get("cohort"),
                "snp_count": item.get("snp_count"),
                "imputed": item.get("imputed"),
                "platforms": item.get("platforms"),
                "genotyping_technologies": item.get("genotyping_technologies"),
                "full_summary_stats_available": item.get("full_summary_stats_available"),
                "summary_stats_url": item.get("full_summary_stats"),
                "publication_title": pub.get("title"),
                "journal": pub.get("journal"),
            }
        ),
    )


def association_record(item: dict[str, Any]) -> Record:
    alleles = item.get("snp_allele") or []
    rsids = [a.get("rs_id") for a in alleles if a.get("rs_id")]
    risk = item.get("snp_effect_allele") or []
    traits = _trait_list(item.get("efo_traits"))
    genes = [g for g in item.get("mapped_genes") or [] if g and g != "-"]
    identifiers: dict[str, Any] = {
        "rsid": rsids[0] if rsids and _RSID.match(rsids[0]) else None,
        "gwas_study": item.get("accession_id"),
        "pmid": item.get("pubmed_id"),
        "gene_symbol": genes[0] if len(genes) == 1 else None,
    }
    for t in traits:
        for key, value in trait_identifiers(t["id"]).items():
            identifiers.setdefault(key, value)
    pval = _pvalue(item)
    trait_label = ", ".join(t["label"] for t in traits if t["label"]) or ", ".join(item.get("reported_trait") or [])
    effect = None
    if item.get("or_value") not in (None, "", "-"):
        effect = f"OR={item['or_value']}"
    elif item.get("beta") not in (None, "", "-"):
        effect = f"beta={item['beta']}"
    title = f"{', '.join(risk) or ', '.join(rsids) or 'association'} - {trait_label}"
    summary_bits = [f"p={pval}" if pval else None, effect, f"95% CI {item['range']}" if item.get("range") not in (None, "", "-") else None]
    if genes:
        summary_bits.append("mapped gene(s): " + ", ".join(genes))
    if item.get("accession_id"):
        summary_bits.append(f"study {item['accession_id']} ({item.get('first_author') or 'n/a'}, PMID {item.get('pubmed_id')})")
    return Record(
        source="gwas_catalog",
        id=str(item.get("association_id") or ""),
        type="association",
        title=title,
        url=(f"{WEB}/variants/{rsids[0]}" if rsids else f"{WEB}/studies/{item.get('accession_id')}"),
        identifiers=identifiers,
        summary="; ".join(b for b in summary_bits if b) or None,
        attributes=_compact(
            {
                "p_value": pval,
                "pvalue_description": item.get("pvalue_description"),
                "or_value": item.get("or_value"),
                "beta": item.get("beta"),
                "ci": item.get("range"),
                "ci_lower": item.get("ci_lower"),
                "ci_upper": item.get("ci_upper"),
                "risk_allele": risk,
                "risk_allele_frequency": item.get("risk_frequency"),
                "rsids": rsids if len(rsids) > 1 else None,
                "mapped_genes": genes,
                "reported_trait": item.get("reported_trait"),
                "efo_traits": traits,
                "location": item.get("locations"),
                "study_accession": item.get("accession_id"),
                "pubmed_id": item.get("pubmed_id"),
                "first_author": item.get("first_author"),
                "multi_snp_haplotype": item.get("multi_snp_haplotype") or None,
                "snp_interaction": item.get("snp_interaction") or None,
            }
        ),
    )


def trait_record(item: dict[str, Any]) -> Record:
    tid = item.get("efo_id") or ""
    return Record(
        source="gwas_catalog",
        id=tid,
        type="ontology_term",
        title=item.get("efo_trait"),
        url=f"{WEB}/efotraits/{tid}" if tid else None,
        identifiers=trait_identifiers(tid),
        attributes=_compact({"ontology_uri": item.get("uri")}),
    )


class GwasCatalog(Connector):
    name = "gwas_catalog"
    title = "GWAS Catalog (NHGRI-EBI)"
    category = "genomics"
    description = (
        "NHGRI-EBI catalog of published genome-wide association studies: ~1M curated variant-trait "
        "associations (p-values, odds ratios/betas, risk alleles, mapped genes) from 7,000+ publications, "
        "with study sample sizes, ancestry and summary-statistics availability. Best source for "
        "'which variants/genes are associated with trait X' and 'what is rsID Y associated with'."
    )
    homepage = "https://www.ebi.ac.uk/gwas/"
    docs_url = "https://www.ebi.ac.uk/gwas/rest/api/v2/docs"
    auth = "none"
    rate_limit = 5.0
    health_query = "asthma"
    nih_repositories = ("GWAS Catalog (NHGRI-EBI Catalog)",)

    async def _get(self, path: str, params: dict[str, Any] | None = None, **kwargs: Any):
        return await self.request("GET", f"{API}{path}", params=params, **kwargs)

    @tool(
        description="""
        Search the GWAS Catalog by trait/disease free text. kind=studies (default) returns GWAS
        studies (GCST accession, reported trait, mapped EFO/MONDO traits, discovery/replication sample
        sizes and ancestry, PMID, summary-statistics availability); kind=traits returns matching trait
        ontology terms (EFO_/MONDO_/HP_ ids) to feed into associations_for_trait. Also accepts a GCST
        accession or a PubMed ID as the query. Example: query="type 2 diabetes". For variant-level
        hits use associations_for_variant / associations_for_gene / associations_for_trait.
        """,
        federated=True,
    )
    async def search(self, params: GwasSearchParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        query = params.query.strip()
        page = {"page": offset // params.limit, "size": params.limit}
        notes: list[str] = []
        if params.kind == "traits":
            # The upstream orders trait matches arbitrarily: fetch one large page and rank
            # exact > prefix > substring matches, shortest label first.
            response = await self._get("/efo-traits", {"efo_trait": query, "page": 0, "size": 200})
            payload = response.json()
            ranked = rank_traits(_embedded(payload, "efo_traits"), query)
            items = ranked[offset : offset + params.limit]
            total = ((payload.get("page") or {}).get("totalElements")) or len(ranked)
            nxt = offset + len(items)
            notes = ["only the first 200 matching traits are ranked; refine the query"] if total > 200 else []
            return ToolResult(
                source=self.name,
                tool="search",
                records=[trait_record(i) for i in items],
                total=total,
                next_cursor=str(nxt) if items and nxt < len(ranked) else None,
                request_url=response.url,
                notes=notes,
            )
        if _GCST.match(query):
            filters = {"accession_id": query.upper()}
        elif query.isdigit():
            filters = {"pubmed_id": query}
        else:
            filters = {"efo_trait": query}
        response = await self._get("/studies", {**filters, **page})
        payload = response.json()
        items = _embedded(payload, "studies")
        if not items and "efo_trait" in filters and offset == 0:
            # No mapped trait with that label: fall back to the free-text reported trait.
            response = await self._get("/studies", {"disease_trait": query, **page})
            payload = response.json()
            items = _embedded(payload, "studies")
            if items:
                notes.append("no trait ontology label equals the query exactly; results matched on the reported-trait text instead")
        total, nxt = _page(payload, offset, len(items))
        if not items:
            notes.append(f"no GWAS Catalog studies found for {query!r}; try kind=traits to find trait ids")
        return ToolResult(
            source=self.name,
            tool="search",
            records=[study_record(i) for i in items],
            total=total,
            next_cursor=nxt,
            request_url=response.url,
            notes=notes,
        )

    async def _associations(
        self, tool_name: str, filters: dict[str, Any], limit: int, cursor: str | None, notes: list[str] | None = None
    ) -> ToolResult:
        offset = parse_offset(cursor)
        response = await self._get(
            "/associations",
            {**filters, "sort": "p_value", "direction": "asc", "page": offset // limit, "size": limit},
        )
        payload = response.json()
        items = _embedded(payload, "associations")
        total, nxt = _page(payload, offset, len(items))
        return ToolResult(
            source=self.name,
            tool=tool_name,
            records=[association_record(i) for i in items],
            total=total,
            next_cursor=nxt,
            request_url=response.url,
            notes=list(notes or []),
        )

    @tool(
        description="""
        All GWAS Catalog associations reported for one variant (dbSNP rsID, e.g. rs7903146), strongest
        p-value first: trait (EFO/MONDO id), p-value, odds ratio or beta with 95% CI, risk allele and its
        frequency, mapped genes, study accession (GCST) and PMID. data.variant holds the variant's
        location (GRCh38), functional class and mapped genes. Use for 'what is this SNP associated with'.
        """
    )
    async def associations_for_variant(self, params: VariantAssocParams) -> ToolResult:
        rsid = params.rsid.strip().lower()
        if not _RSID.match(rsid):
            return ToolResult(source=self.name, tool="associations_for_variant", notes=[f"{params.rsid!r} is not an rsID (rs + digits)"])
        result = await self._associations("associations_for_variant", {"rs_id": rsid}, params.limit, params.cursor)
        snp = await self._get(f"/single-nucleotide-polymorphisms/{quote(rsid)}", allow_status=(404,))
        if snp.status == 404:
            result.notes.append(f"variant {rsid} is not in the GWAS Catalog")
        else:
            info = snp.json() or {}
            loc = first_location(info.get("locations"))
            result.data = {
                "variant": _compact(
                    {
                        "rsid": info.get("rs_id"),
                        "location_grch38": loc,
                        "functional_class": info.get("functional_class"),
                        "most_severe_consequence": info.get("most_severe_consequence"),
                        "alleles": info.get("alleles"),
                        "mapped_genes": info.get("mapped_genes"),
                        "merged": info.get("merged") or None,
                    }
                )
            }
        if not result.records:
            result.notes.append(f"no associations reported for {rsid}")
        return result

    @tool(
        description="""
        GWAS Catalog associations whose variant maps to a gene (HGNC symbol, e.g. APOE, TCF7L2),
        strongest p-value first: rsID, risk allele, trait (EFO/MONDO), p-value, OR/beta, study GCST, PMID.
        data.gene gives the gene's Ensembl/Entrez ids and location. extended_geneset=true widens the
        mapping window. Use to see which traits a gene's locus is associated with.
        """
    )
    async def associations_for_gene(self, params: GeneAssocParams) -> ToolResult:
        gene = params.gene.strip()
        filters: dict[str, Any] = {"mapped_gene": gene}
        if params.extended_geneset:
            filters["extended_geneset"] = True
        result = await self._associations("associations_for_gene", filters, params.limit, params.cursor)
        info_resp = await self._get(f"/genes/{quote(gene)}", allow_status=(404,))
        if info_resp.status != 404:
            info = info_resp.json() or {}
            result.data = {
                "gene": _compact(
                    {
                        "symbol": info.get("gene_name"),
                        "description": info.get("gene_description"),
                        "location": info.get("location"),
                        "cytogenetic_region": info.get("cytogenic_region"),
                        "biotype": info.get("biotype"),
                        "ensembl_gene": info.get("ensembl_gene_ids"),
                        "entrez_gene": info.get("entrez_gene_ids"),
                    }
                )
            }
        if not result.records:
            result.notes.append(f"no GWAS associations mapped to gene {gene!r} (check the HGNC symbol)")
        return result

    @tool(
        description="""
        Top GWAS Catalog associations for a trait, strongest p-value first (capped by limit, paginate
        with cursor): rsID, risk allele, p-value, OR/beta with CI, mapped genes, study GCST, PMID. Pass a
        trait ontology id (EFO_0001360, MONDO_0005148; find ids with search kind=traits) or an exact
        trait label. show_child_traits (default true) includes more specific sub-traits.
        """
    )
    async def associations_for_trait(self, params: TraitAssocParams) -> ToolResult:
        trait = params.trait.strip()
        child = {"show_child_traits": params.show_child_traits}
        match = _TRAIT_ID.match(trait)
        if match:
            trait_id = f"{match.group(1).upper()}_{match.group(2)}"
            return await self._associations(
                "associations_for_trait", {"efo_id": trait_id, **child}, params.limit, params.cursor
            )
        result = await self._associations(
            "associations_for_trait", {"efo_trait": trait, **child}, params.limit, params.cursor
        )
        if result.records or params.cursor:
            return result
        lookup = await self._get("/efo-traits", {"efo_trait": trait, "page": 0, "size": 200})
        candidates = rank_traits(_embedded(lookup.json(), "efo_traits"), trait)[:5]
        if not candidates:
            result.notes.append(f"no trait term matches {trait!r}")
            return result
        exact = candidates[0]
        notes = [
            f"resolved trait {trait!r} to {exact.get('efo_trait')} ({exact.get('efo_id')}); other candidates: "
            + ", ".join(f"{c.get('efo_trait')} ({c.get('efo_id')})" for c in candidates if c is not exact)
        ]
        return await self._associations(
            "associations_for_trait", {"efo_id": exact.get("efo_id"), **child}, params.limit, params.cursor, notes
        )

    @tool(
        description="""
        Full metadata for one GWAS study by accession (GCST000392): reported and mapped traits,
        discovery/replication sample sizes, ancestry breakdown (individuals per ancestral group and
        country), genotyping platform, SNP count, publication (title, journal, date, authors, PMID) and
        whether full summary statistics are available (with the FTP URL).
        """
    )
    async def get_study(self, params: GetStudyParams) -> ToolResult:
        acc = params.accession.strip().upper()
        if not _GCST.match(acc):
            return ToolResult(source=self.name, tool="get_study", notes=[f"{params.accession!r} is not a GCST accession"])
        response = await self._get(f"/studies/{quote(acc)}", allow_status=(404,))
        if response.status == 404:
            return ToolResult(source=self.name, tool="get_study", notes=[f"study {acc} not found in the GWAS Catalog"])
        item = response.json() or {}
        publication = None
        if item.get("pubmed_id"):
            pub = await self._get(f"/publications/{quote(str(item['pubmed_id']))}", allow_status=(404,))
            if pub.status != 404:
                publication = pub.json()
        record = study_record(item, publication)
        anc = await self._get(f"/studies/{quote(acc)}/ancestries", allow_status=(404,))
        if anc.status != 404:
            groups = []
            for a in _embedded(anc.json(), "ancestries"):
                groups.append(
                    _compact(
                        {
                            "stage": a.get("type"),
                            "individuals": a.get("number_of_individuals"),
                            "ancestral_groups": [g.get("ancestral_group") for g in a.get("ancestral_groups") or []],
                            "countries_of_recruitment": [
                                c.get("country_name") for c in a.get("country_of_recruitment") or []
                            ],
                        }
                    )
                )
            if groups:
                record.attributes["ancestries"] = groups
        return ToolResult(source=self.name, tool="get_study", records=[record], total=1, request_url=response.url)


def rank_traits(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Order trait matches: exact label, then prefix, then substring; shorter labels first."""
    q = query.strip().lower()

    def key(item: dict[str, Any]) -> tuple[int, int, str]:
        label = (item.get("efo_trait") or "").lower()
        tier = 0 if label == q else 1 if label.startswith(q) else 2 if q in label else 3
        return tier, len(label), label

    return sorted(items, key=key)


def first_location(locations: list[dict[str, Any]] | None) -> str | None:
    for loc in locations or []:
        if loc.get("chromosome_name") and loc.get("chromosome_position"):
            return f"{loc['chromosome_name']}:{loc['chromosome_position']}"
    return None
