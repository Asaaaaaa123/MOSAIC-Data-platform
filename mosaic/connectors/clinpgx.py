"""ClinPGx (formerly PharmGKB) pharmacogenomics knowledgebase connector.

Docs: https://api.clinpgx.org/swagger/ (OpenAPI 3 at https://api.clinpgx.org/openapi.json).
Host: https://api.clinpgx.org/v1 (the old api.pharmgkb.org host serves the same data).
Auth: none. Rate limit: ClinPGx asks for at most 2 requests/second; we stay at 2 with no burst.

Quirks:
- The documented ``/data/{type}`` query endpoints match EXACTLY and case-sensitively
  (``/data/chemical?name=Warfarin`` 404s; ``warfarin`` works; no wildcards). "No results"
  is HTTP 404 with ``{"status": "fail", ...}``.
- There is no documented free-text search. The ClinPGx website's own search uses
  ``POST /v1/site/ftSearch`` (``{"query", "size", "from", "objCls"}``; returns ``hasMore`` but
  no total) and ``POST /v1/site/searchExact`` (``{"objCls", "query", "field"}``; field
  ``name`` is case-insensitive, field ``synonyms`` matches trade names and ``RxNorm:``/``ATC:``/
  ``MeSH:``/``HGNC:`` cross-references). Both are undocumented site endpoints on the same
  fixed host; the federated search falls back to ``POST /v1/site/autocomplete`` (max 5 hits)
  if ftSearch disappears. ftSearch hits carry very large ``properties`` (connected entities,
  curator history incl. emails); only names, counts, synonyms-derived IDs and the body text
  are kept.
- ``/data/summaryAnnotation`` (clinical annotations) list queries have no paging and
  ``view=base`` is huge (multi-MB for CYP2D6), so lists use ``view=min`` and the requested
  page is enriched one-by-one with ``view=max`` (genotype phenotypes, diseases, score and the
  supporting variant annotations' PubMed IDs). ``location.fingerprint`` accepts rsIDs only.
- Gene ``crossReferences`` list secondary UniProtKB accessions; the ModBase cross-reference
  carries the canonical accession, so it is preferred for ``uniprot``.
- Non-canonical identifier keys used: ``chebi`` (CHEBI:#####), ``kegg_compound`` (C#####).
- Supporting papers of clinical annotations (``attributes['pmids']``) and dosing guidelines
  (``attributes['publications']``) are related entities, not identifiers: one CPIC guideline paper
  backs several guideline annotations, and as a ``pmid`` / ``doi`` it would merge them together.
"""

from __future__ import annotations

import re
from typing import Any, Literal, get_args
from urllib.parse import quote

from pydantic import Field

from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, strip_html, tool

API = "https://api.clinpgx.org/v1"
SITE = "https://www.clinpgx.org"

_PA = re.compile(r"^PA\d+$", re.IGNORECASE)
_RSID = re.compile(r"^rs\d+$", re.IGNORECASE)
_XREF_QUERY = re.compile(r"^(RxNorm|ATC|MeSH|HGNC)\s*:\s*(\S+)$", re.IGNORECASE)
_MESH = re.compile(r"^(?:MeSH:)?(D\d{6}|C\d{6,9})$")
_LEVEL_RANK = {"1A": 0, "1B": 1, "2A": 2, "2B": 3, "3": 4, "4": 5}
_GUIDELINE_RANK = {"CPIC": 0, "DPWG": 1, "CPNDS": 2, "RNPGx": 3}
_TESTING_RANK = {
    "Testing Required": 0,
    "Testing Recommended": 1,
    "Actionable PGx": 2,
    "Informative PGx": 3,
    "No Clinical PGx": 4,
}
SearchType = Literal[
    "Gene",
    "Chemical",
    "Variant",
    "Haplotype",
    "Phenotype",
    "GuidelineAnnotation",
    "LabelAnnotation",
    "SummaryAnnotation",
    "Pathway",
    "Literature",
]
_SEARCH_TYPES = get_args(SearchType)
_RECORD_TYPES = {
    "Gene": "gene",
    "Chemical": "drug",
    "Variant": "variant",
    "Haplotype": "variant",
    "StarAllele": "variant",
    "SNV": "variant",
    "Phenotype": "disease",
    "Disease": "disease",
    "SummaryAnnotation": "association",
    "VariantAnnotation": "association",
    "VariantDrugAnnotation": "association",
    "LabelAnnotation": "drug_label",
    "Pathway": "pathway",
    "Literature": "article",
}


# ---------------------------------------------------------------- params


class ClinPGxSearchParams(SearchParams):
    types: list[SearchType] | None = Field(
        None,
        description="Restrict to ClinPGx object classes, e.g. ['Gene'], ['Chemical'], ['Variant'], "
        "['GuidelineAnnotation','LabelAnnotation']. Default: all classes.",
    )


class GetGeneParams(ToolParams):
    gene: str = Field(description="HGNC gene symbol (CYP2C19), ClinPGx accession (PA124) or HGNC ID (HGNC:2621)")


class GetChemicalParams(ToolParams):
    chemical: str = Field(
        description="Drug/chemical name (warfarin, Coumadin), ClinPGx accession (PA451906), or a cross-reference "
        "written as RxNorm:<rxcui>, ATC:<code> or MeSH:<id> (e.g. RxNorm:11289, ATC:B01AA03)"
    )


class ClinicalAnnotationsParams(ToolParams):
    gene: str | None = Field(None, description="Gene symbol (CYP2D6), ClinPGx gene accession (PA128) or HGNC ID")
    drug: str | None = Field(
        None, description="Drug name (codeine), ClinPGx chemical accession (PA449088) or RxNorm:/ATC:/MeSH: reference"
    )
    variant: str | None = Field(
        None,
        description="dbSNP rsID (rs9923231), or a star allele / haplotype (CYP2D6*4) which is matched against "
        "the annotation's haplotypes (combine with gene)",
    )
    levels: list[Literal["1A", "1B", "2A", "2B", "3", "4"]] | None = Field(
        None, description="Only these levels of evidence (1A strongest ... 4 weakest), e.g. ['1A','1B']"
    )
    phenotype_category: Literal["Dosage", "Efficacy", "Toxicity", "Metabolism/PK", "PD", "Other"] | None = Field(
        None, description="Only annotations with this phenotype category"
    )
    include_evidence: bool = Field(
        True,
        description="Fetch each returned annotation in full: genotype-specific phenotype text, diseases, score and "
        "supporting PubMed IDs (one extra request per record; set false for fast overviews)",
    )
    limit: int = Field(10, ge=1, le=25, description="Maximum number of annotations to return")
    cursor: str | None = Field(None, description="Offset from a previous result's next_cursor")


class GuidelineParams(ToolParams):
    drug: str | None = Field(None, description="Drug name, ClinPGx chemical accession (PA...) or RxNorm:/ATC:/MeSH: reference")
    gene: str | None = Field(None, description="Gene symbol, ClinPGx gene accession or HGNC ID")
    source: str | None = Field(
        None, description="Guideline body: CPIC, DPWG, CPNDS, RNPGx (also e.g. ACR, AusNZ, SEFF, CFF); default all"
    )
    limit: int = Field(20, ge=1, le=50, description="Maximum number of guideline annotations to return")
    cursor: str | None = Field(None, description="Offset from a previous result's next_cursor")


class DrugLabelParams(ToolParams):
    drug: str | None = Field(None, description="Drug name, ClinPGx chemical accession (PA...) or RxNorm:/ATC:/MeSH: reference")
    gene: str | None = Field(None, description="Gene symbol, ClinPGx gene accession or HGNC ID")
    source: Literal["FDA", "EMA", "HCSC", "PMDA", "Swissmedic"] | None = Field(
        None, description="Regulatory agency whose label was annotated; default all"
    )
    testing_level: Literal[
        "Testing Required", "Testing Recommended", "Actionable PGx", "Informative PGx", "No Clinical PGx"
    ] | None = Field(None, description="Only labels with this PGx testing level")
    limit: int = Field(20, ge=1, le=50, description="Maximum number of label annotations to return")
    cursor: str | None = Field(None, description="Offset from a previous result's next_cursor")


# ---------------------------------------------------------------- helpers


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v not in (None, "", [], {})}


def _truncate(text: str | None, chars: int) -> str | None:
    if text and len(text) > chars:
        return text[:chars].rstrip() + " ..."
    return text


def _html(obj: Any) -> str | None:
    if isinstance(obj, dict):
        return strip_html(obj.get("html") or obj.get("markdown"))
    if isinstance(obj, str):
        return strip_html(obj)
    return None


def _last_date(item: dict[str, Any]) -> str | None:
    dates = [h.get("date") for h in item.get("history") or [] if isinstance(h, dict) and h.get("date")]
    return max(dates)[:10] if dates else None


def _names(items: Any, key: str = "name") -> list[str]:
    return [i.get(key) or i.get("name") for i in items or [] if isinstance(i, dict) and (i.get(key) or i.get("name"))]


def _literature(lit: dict[str, Any]) -> dict[str, Any]:
    """Compact publication: title, pmid, pmcid, doi, url (+ FDA application / DailyMed set id)."""
    out: dict[str, Any] = {"title": lit.get("title")}
    for x in lit.get("crossReferences") or []:
        resource, rid = x.get("resource"), x.get("resourceId")
        if not rid:
            continue
        if resource == "PubMed":
            out.setdefault("pmid", str(rid))
        elif resource == "PubMed Central":
            out.setdefault("pmcid", rid)
        elif resource == "DOI":
            out.setdefault("doi", rid)
        elif resource == "FDA Application":
            out.setdefault("fda_application", rid)
        elif resource == "FDA Drug Label at DailyMed":
            out.setdefault("spl_set_id", rid)
        elif resource == "URL":
            out.setdefault("url", rid)
    out.setdefault("url", lit.get("_sameAs"))
    if not out.get("url") and out.get("pmid"):
        out["url"] = f"https://pubmed.ncbi.nlm.nih.gov/{out['pmid']}/"
    if lit.get("year"):
        out["year"] = lit["year"]
    return _clean(out)


def _gene_record(item: dict[str, Any]) -> Record:
    pa = item.get("id", "")
    xrefs: dict[str, list[str]] = {}
    for x in item.get("crossReferences") or []:
        if x.get("resource") and x.get("resourceId"):
            xrefs.setdefault(x["resource"], []).append(str(x["resourceId"]))
    uniprots = xrefs.get("UniProtKB", [])
    modbase = xrefs.get("ModBase", [])
    uniprot = next((u for u in modbase if u in uniprots), None) or (modbase or uniprots or [None])[0]
    chrom = (item.get("chr") or {}).get("name")
    citation = item.get("vipCitation") or {}
    vip_pmid = next(
        (x.get("resourceId") for x in citation.get("crossReferences") or [] if x.get("resource") == "PubMed"), None
    )
    go_terms = [t.get("termId") for t in item.get("terms") or [] if str(t.get("termId", "")).startswith("GO:")]
    alt = item.get("altNames") or {}
    return Record(
        source="clinpgx",
        id=pa,
        type="gene",
        title=f"{item.get('symbol')} ({item.get('name')})" if item.get("name") else item.get("symbol"),
        url=f"{SITE}/gene/{pa}",
        identifiers={
            "pharmgkb": pa,
            "gene_symbol": item.get("symbol"),
            "hgnc": (xrefs.get("HGNC") or [None])[0],
            "entrez_gene": (xrefs.get("NCBI Gene") or [None])[0],
            "ensembl_gene": (xrefs.get("Ensembl") or [None])[0],
            "uniprot": uniprot,
            "omim": (xrefs.get("OMIM") or [None])[0],
            "refseq": (xrefs.get("RefSeq RNA") or [None])[0],
        },
        summary=_html(item.get("vipSummary")),
        attributes=_clean(
            {
                "name": item.get("name"),
                "chromosome": chrom,
                "cytoband": "-".join(x for x in (item.get("cbStart"), item.get("cbStop")) if x) or None,
                "start_grch38": item.get("chrStartPosB38"),
                "end_grch38": item.get("chrStopPosB38"),
                "strand": item.get("strand"),
                "cpic_gene": item.get("cpicGene"),
                "pharmvar_gene": item.get("pharmVarGene"),
                "vip_tier": item.get("vipTier"),
                "uses_star_alleles": item.get("usesStarAlleles"),
                "allele_function_source": item.get("alleleFunctionSource"),
                "amp_tier1_gene": item.get("amp"),
                "synonyms": (alt.get("symbol") or [])[:10],
                "uniprot_all": sorted(set(uniprots + modbase)) if len(set(uniprots + modbase)) > 1 else None,
                "omim_all": xrefs.get("OMIM") if len(xrefs.get("OMIM", [])) > 1 else None,
                "refseq_protein": xrefs.get("RefSeq Protein"),
                "go_terms": go_terms[:20],
                "vip_review_pmid": vip_pmid,
                "ctd_gene": (xrefs.get("Comparative Toxicogenomics Database") or [None])[0],
            }
        ),
    )


def _chemical_record(item: dict[str, Any]) -> Record:
    pa = item.get("id", "")
    links: dict[str, list[str]] = {}
    for x in item.get("linkOuts") or item.get("crossReferences") or []:
        if x.get("resource") and x.get("resourceId"):
            links.setdefault(x["resource"], []).append(str(x["resourceId"]))

    def one(resource: str) -> str | None:
        return (links.get(resource) or [None])[0]

    types = item.get("types") or []
    alt = item.get("altNames") or {}
    return Record(
        source="clinpgx",
        id=pa,
        type="drug" if "Drug" in types or "Prodrug" in types else "compound",
        title=item.get("name"),
        url=f"{SITE}/chemical/{pa}",
        identifiers={
            "pharmgkb": pa,
            "rxcui": one("RxNorm"),
            "atc": one("ATC"),
            "drugbank": one("DrugBank"),
            "pubchem_cid": one("PubChem Compound"),
            "pubchem_sid": one("PubChem Substance"),
            "mesh": one("MeSH"),
            "umls": one("UMLS"),
            "chebi": one("ChEBI"),
            "kegg_compound": one("KEGG Compound"),
            "ndc": one("NDC"),
        },
        summary=_html(item.get("description")),
        attributes=_clean(
            {
                "chemical_types": types,
                "generic_names": (alt.get("generic") or [])[:10],
                "trade_names": (alt.get("trade") or [])[:15],
                "atc_all": links.get("ATC") if len(links.get("ATC", [])) > 1 else None,
                "smiles": item.get("smiles"),
                "inchi": item.get("inChi"),
                "components": _names(item.get("components"))[:10],
                "metabolites": _names(item.get("metabolites"))[:15],
                "clinical_trials": (links.get("ClinicalTrials.gov") or [])[:20],
                "pediatric": item.get("pediatric"),
            }
        ),
    )


def _search_record(hit: dict[str, Any]) -> Record:
    cls = hit.get("objCls") or "other"
    hid = str(hit.get("id") or "")
    name = hit.get("name")
    props = hit.get("properties") or {}
    synonyms = [str(s) for s in props.get("synonyms") or []]
    identifiers: dict[str, Any] = {"pharmgkb": hid if _PA.match(hid) else None}
    for syn in synonyms:
        prefix, _, value = syn.partition(":")
        if prefix == "RxNorm":
            identifiers.setdefault("rxcui", value)
        elif prefix == "ATC":
            identifiers.setdefault("atc", value)
        elif prefix == "MeSH":
            identifiers.setdefault("mesh", value)
        elif prefix == "HGNC":
            identifiers.setdefault("hgnc", syn)
    if cls == "Gene":
        identifiers["gene_symbol"] = name
    elif cls == "Variant" and name and _RSID.match(name):
        identifiers["rsid"] = name
    elif cls in ("Phenotype", "Disease") and "mesh" not in identifiers:
        identifiers["mesh"] = next((s for s in synonyms if _MESH.match(s)), None)
    url = hit.get("url")
    counts = {
        key[3:][:1].lower() + key[4:]: value
        for key, value in hit.items()
        if key.startswith("num") and "Pediatric" not in key and isinstance(value, int) and value
    }
    rtype = _RECORD_TYPES.get(cls, "other")
    if cls == "Chemical" and "Drug" not in (hit.get("categories") or []):
        rtype = "compound"
    return Record(
        source="clinpgx",
        id=hid,
        type=rtype,
        title=name,
        url=f"{SITE}{url}" if isinstance(url, str) and url.startswith("/") else f"{SITE}/{cls[:1].lower()}{cls[1:]}/{hid}",
        identifiers=identifiers,
        summary=_truncate(strip_html(props.get("body") or hit.get("description")), 1200),
        attributes=_clean(
            {
                "object_class": cls,
                "categories": hit.get("categories"),
                "guideline_source": hit.get("source"),
                "tags": hit.get("tags"),
                "counts": counts,
                "match_score": props.get("matchScore") or hit.get("score"),
                "has_data": hit.get("hasData"),
            }
        ),
    )


def _annotation_record(item: dict[str, Any]) -> Record:
    acc = item.get("accessionId") or str(item.get("id") or "")
    loc = item.get("location") or {}
    genes = _names(loc.get("genes"), "symbol")
    rsid = loc.get("rsid") or (loc.get("displayName") if _RSID.match(loc.get("displayName") or "") else None)
    pmids: list[str] = []
    for va in item.get("variantAnnotations") or []:
        pmid = _literature(va.get("literature") or {}).get("pmid")
        if pmid and pmid not in pmids:
            pmids.append(pmid)
    phenos = [
        _clean(
            {
                "genotype": p.get("allele"),
                "phenotype": _truncate(p.get("phenotype"), 280),
                "limited_evidence": p.get("limitedEvidence") or None,
            }
        )
        for p in item.get("allelePhenotypes") or []
    ]
    level = (item.get("levelOfEvidence") or {}).get("term")
    drugs = item.get("relatedChemicals") or []
    return Record(
        source="clinpgx",
        id=acc,
        type="association",
        title=item.get("name"),
        url=f"{SITE}/clinicalAnnotation/{acc}",
        identifiers={
            "pharmgkb": acc,
            "gene_symbol": genes[0] if genes else None,
            "rsid": rsid,
        },
        date=_last_date(item),
        summary=" ".join(f"{p.get('genotype')}: {p.get('phenotype')}" for p in phenos[:2]) or None,
        attributes=_clean(
            {
                "level_of_evidence": level,
                "score": item.get("score"),
                "phenotype_categories": item.get("types"),
                "genes": genes,
                "variant": loc.get("displayName"),
                "haplotypes": _names(loc.get("haplotypes"), "symbol")[:20],
                "drugs": _names(drugs),
                "drug_ids": [d.get("id") for d in drugs if d.get("id")],
                "diseases": _names(item.get("relatedDiseases")),
                "genotype_phenotypes": phenos[:6],
                "pmids": pmids[:30],
                "n_variant_annotations": len(item.get("variantAnnotations") or []) or None,
                "related_guidelines": [g.get("id") for g in item.get("relatedGuidelines") or [] if g.get("id")],
                "related_labels": [g.get("id") for g in item.get("relatedLabels") or [] if g.get("id")],
                "pediatric": item.get("pediatric") or None,
            }
        ),
    )


def _guideline_record(item: dict[str, Any]) -> Record:
    gid = item.get("id", "")
    pubs = [_literature(lit) for lit in item.get("literature") or [] if isinstance(lit, dict)]
    genes = _names(item.get("relatedGenes"), "symbol")
    return Record(
        source="clinpgx",
        id=gid,
        type="other",
        title=item.get("name"),
        url=f"{SITE}/guidelineAnnotation/{gid}",
        identifiers={"pharmgkb": gid, "gene_symbol": genes[0] if genes else None},
        date=_last_date(item),
        summary=_html(item.get("summaryMarkdown")),
        attributes=_clean(
            {
                "kind": "dosing_guideline",
                "guideline_source": item.get("source"),
                "drugs": _names(item.get("relatedChemicals")),
                "genes": genes,
                "recommendation": item.get("recommendation"),
                "dosing_information": item.get("dosingInformation"),
                "alternate_drug_available": item.get("alternateDrugAvailable"),
                "other_prescribing_guidance": item.get("otherPrescribingGuidance") or None,
                "testing_info": item.get("hasTestingInfo") or None,
                "pediatric": item.get("pediatric") or None,
                "alleles": _names(item.get("relatedAlleles"), "symbol")[:15],
                "source_url": next((p.get("url") for p in pubs if p.get("url")), None),
                "publications": pubs[:5],
                "details": _truncate(_html(item.get("textMarkdown")), 800),
            }
        ),
    )


def _label_record(item: dict[str, Any]) -> Record:
    lid = item.get("id", "")
    pubs = [_literature(lit) for lit in item.get("literature") or [] if isinstance(lit, dict)]
    genes = _names(item.get("relatedGenes"), "symbol")
    fda_apps = [p["fda_application"] for p in pubs if p.get("fda_application")]
    set_ids = [p["spl_set_id"] for p in pubs if p.get("spl_set_id")]
    return Record(
        source="clinpgx",
        id=lid,
        type="drug_label",
        title=item.get("name"),
        url=f"{SITE}/labelAnnotation/{lid}",
        identifiers={
            "pharmgkb": lid,
            "fda_application": fda_apps[0] if fda_apps else None,
            "spl_set_id": set_ids[0] if set_ids else None,
            "gene_symbol": genes[0] if genes else None,
        },
        date=_last_date(item),
        summary=_html(item.get("summaryMarkdown")),
        attributes=_clean(
            {
                "agency": item.get("source"),
                "testing_level": (item.get("testing") or {}).get("term"),
                "biomarker_status": item.get("biomarkerStatus"),
                "drugs": _names(item.get("relatedChemicals")),
                "genes": genes,
                "prescribing_genes": _names(item.get("prescribingGenes"), "symbol"),
                "prescribing_info": _truncate(_html(item.get("prescribingMarkdown")), 800),
                "dosing_information": item.get("dosingInformation"),
                "alternate_drug_available": item.get("alternateDrugAvailable"),
                "indication": item.get("indication") or None,
                "cancer_genome": item.get("cancerGenome") or None,
                "pediatric": item.get("pediatric") or None,
                "alleles": _names(item.get("relatedAlleles"), "symbol")[:15],
                "fda_applications": fda_apps[1:] or None,
                "source_url": next((p.get("url") for p in pubs if p.get("url")), None),
            }
        ),
    )


def _page(items: list[Any], cursor: str | None, limit: int) -> tuple[list[Any], str | None]:
    offset = parse_offset(cursor)
    page = items[offset : offset + limit]
    nxt = offset + len(page)
    return page, (str(nxt) if page and nxt < len(items) else None)


# ---------------------------------------------------------------- connector


class ClinPGx(Connector):
    name = "clinpgx"
    title = "ClinPGx (PharmGKB)"
    category = "drugs"
    description = (
        "Curated pharmacogenomics knowledge (PharmGKB + CPIC + PharmCAT): gene-drug clinical annotations with "
        "levels of evidence, CPIC/DPWG/CPNDS/RNPGx dosing guidelines and FDA/EMA PGx drug-label annotations "
        "for ~1,000 drugs and the pharmacogenes that affect them. Best for 'how does genotype X change response "
        "to drug Y'."
    )
    homepage = "https://www.clinpgx.org/"
    docs_url = "https://api.clinpgx.org/swagger/"
    auth = "none"
    rate_limit = 2.0
    rate_burst = 1.0
    health_query = "warfarin"
    nih_repositories = ("ClinPGx",)

    # ---- HTTP helpers ----
    async def _data(self, path: str, params: dict[str, Any]) -> tuple[Any, str]:
        """GET a /data endpoint; returns (data or None when not found, request_url)."""
        response = await self.request("GET", f"{API}/{path}", params=params, allow_status=(404,))
        if response.status == 404:
            return None, response.url
        return response.json().get("data"), response.url

    async def _search_exact(self, obj_cls: str, query: str, field: str) -> str | None:
        """Website exact search (case-insensitive name / synonym match) -> ClinPGx accession or None."""
        response = await self.request(
            "POST",
            f"{API}/site/searchExact",
            json_body={"objCls": [obj_cls], "query": query, "field": field},
            cache_ttl=86400,
            allow_status=(400, 404, 405),
        )
        if response.status >= 300:
            return None
        hits = ((response.json().get("data") or {}).get("hits")) or []
        return hits[0].get("id") if len(hits) == 1 or (hits and field == "name") else None

    async def _resolve_gene(self, value: str) -> tuple[str, str] | None:
        """-> (accession, symbol)."""
        value = value.strip()
        if _PA.match(value):
            data, _ = await self._data(f"data/gene/{quote(value.upper())}", {"view": "min"})
        elif value.upper().startswith("HGNC:"):
            acc = await self._search_exact("Gene", value.upper(), "synonyms")
            data = (await self._data(f"data/gene/{quote(acc)}", {"view": "min"}))[0] if acc else None
        else:
            data, _ = await self._data("data/gene", {"symbol": value.upper(), "view": "min"})
        item = data[0] if isinstance(data, list) and data else data
        if not isinstance(item, dict) or not item.get("id"):
            return None
        return item["id"], item.get("symbol") or value.upper()

    async def _resolve_chemical(self, value: str) -> tuple[str, str] | None:
        """-> (accession, name). Exact name, lowercase name, case-insensitive name, then synonyms."""
        value = value.strip()
        item: Any = None
        if _PA.match(value):
            item, _ = await self._data(f"data/chemical/{quote(value.upper())}", {"view": "min"})
        else:
            m = _XREF_QUERY.match(value)
            if m:
                prefix = {"rxnorm": "RxNorm", "atc": "ATC", "mesh": "MeSH", "hgnc": "HGNC"}[m.group(1).lower()]
                acc = await self._search_exact("Chemical", f"{prefix}:{m.group(2)}", "synonyms")
            else:
                for name in dict.fromkeys((value, value.lower())):
                    item, _ = await self._data("data/chemical", {"name": name, "view": "min"})
                    if item:
                        break
                acc = None
                if not item:
                    acc = await self._search_exact("Chemical", value, "name") or await self._search_exact(
                        "Chemical", value, "synonyms"
                    )
            if not item and acc:
                item, _ = await self._data(f"data/chemical/{quote(acc)}", {"view": "min"})
        item = item[0] if isinstance(item, list) and item else item
        if not isinstance(item, dict) or not item.get("id"):
            return None
        return item["id"], item.get("name") or value

    async def _resolve_filters(
        self, tool_name: str, drug: str | None, gene: str | None
    ) -> tuple[dict[str, str], list[str], ToolResult | None]:
        """Resolve drug/gene to accessions; returns (params, notes, early not-found result)."""
        params: dict[str, str] = {}
        notes: list[str] = []
        if gene:
            resolved = await self._resolve_gene(gene)
            if not resolved:
                return params, notes, ToolResult(
                    source=self.name, tool=tool_name, total=0, notes=[f"gene {gene!r} not found in ClinPGx"]
                )
            params["relatedGenes.accessionId"] = resolved[0]
            notes.append(f"gene {gene} -> {resolved[1]} ({resolved[0]})")
        if drug:
            resolved = await self._resolve_chemical(drug)
            if not resolved:
                return params, notes, ToolResult(
                    source=self.name, tool=tool_name, total=0, notes=[f"drug {drug!r} not found in ClinPGx"]
                )
            params["relatedChemicals.accessionId"] = resolved[0]
            notes.append(f"drug {drug} -> {resolved[1]} ({resolved[0]})")
        return params, notes, None

    # ---- tools ----
    @tool(
        description="""
        Full-text search of ClinPGx/PharmGKB: genes, drugs/chemicals, variants (rsIDs), haplotypes, phenotypes,
        clinical annotations, dosing-guideline and drug-label annotations, pathways. Returns ClinPGx accessions
        (PA...), names, annotation counts (guidelines, labels, clinical/variant annotations) and RxNorm/ATC/MeSH/
        HGNC IDs where ClinPGx lists them. Use to find the pharmacogenomic entities for a drug, gene or variant,
        then call get_gene / get_chemical / clinical_annotations / dosing_guidelines. Examples: 'warfarin',
        'CYP2C19', 'rs9923231', 'clopidogrel CYP2C19'. Filter with types=['Gene'|'Chemical'|'Variant'|...].
        """,
        federated=True,
    )
    async def search(self, params: ClinPGxSearchParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        body: dict[str, Any] = {"query": params.query, "size": params.limit, "from": offset}
        if params.types:
            body["objCls"] = list(params.types)
        response = await self.request(
            "POST", f"{API}/site/ftSearch", json_body=body, cache_ttl=3600, allow_status=(400, 404, 405)
        )
        notes: list[str] = []
        if response.status >= 300:
            # Undocumented site endpoint went away: fall back to autocomplete (max 5 hits, no paging).
            response = await self.request(
                "POST", f"{API}/site/autocomplete", json_body={"query": params.query, "size": 5}, cache_ttl=3600
            )
            notes.append("full-text search unavailable; showing up to 5 autocomplete matches")
            offset = 0
        data = response.json().get("data") or {}
        hits = [h for h in data.get("hits") or [] if isinstance(h, dict)]
        if params.types and notes:
            hits = [h for h in hits if h.get("objCls") in params.types]
        hits = hits[: params.limit]
        has_more = bool(data.get("hasMore")) and not notes
        if not hits:
            notes.append(f"no ClinPGx matches for {params.query!r}")
        return ToolResult(
            source=self.name,
            tool="search",
            records=[_search_record(h) for h in hits],
            total=data.get("total") if notes else None,
            next_cursor=str(offset + len(hits)) if has_more and hits else None,
            request_url=response.url,
            notes=notes,
        )

    @tool(
        description="""
        Get one pharmacogene from ClinPGx with its cross-references: HGNC ID, NCBI Gene, Ensembl, UniProt,
        OMIM, RefSeq, chromosome position (GRCh38), CPIC/PharmVar gene flags, VIP tier and the curated
        Very Important Pharmacogene summary. Accepts a gene symbol (CYP2C19), ClinPGx accession (PA124)
        or HGNC ID (HGNC:2621). For general gene biology prefer HGNC/NCBI Gene; use this for PGx context.
        """
    )
    async def get_gene(self, params: GetGeneParams) -> ToolResult:
        value = params.gene.strip()
        if _PA.match(value):
            data, url = await self._data(f"data/gene/{quote(value.upper())}", {"view": "max"})
        elif value.upper().startswith("HGNC:"):
            acc = await self._search_exact("Gene", value.upper(), "synonyms")
            data, url = await self._data(f"data/gene/{quote(acc)}", {"view": "max"}) if acc else (None, None)
        else:
            data, url = await self._data("data/gene", {"symbol": value.upper(), "view": "max"})
        item = data[0] if isinstance(data, list) and data else data
        if not isinstance(item, dict) or not item.get("id"):
            return ToolResult(
                source=self.name, tool="get_gene", total=0, request_url=url, notes=[f"gene {value!r} not found in ClinPGx"]
            )
        return ToolResult(source=self.name, tool="get_gene", records=[_gene_record(item)], total=1, request_url=url)

    @tool(
        description="""
        Get one drug/chemical from ClinPGx with cross-references: RxNorm (rxcui), ATC, DrugBank, PubChem CID/SID,
        MeSH, UMLS, ChEBI, KEGG, NDC, plus SMILES/InChI, trade/generic names, metabolites and linked
        ClinicalTrials.gov NCT numbers. Accepts a name (warfarin; trade names like Coumadin work), a ClinPGx
        accession (PA451906) or RxNorm:<rxcui> / ATC:<code> / MeSH:<id>. Use to map a drug to PGx identifiers;
        for chemistry/bioactivity prefer ChEMBL or PubChem.
        """
    )
    async def get_chemical(self, params: GetChemicalParams) -> ToolResult:
        resolved = await self._resolve_chemical(params.chemical)
        if not resolved:
            return ToolResult(
                source=self.name, tool="get_chemical", total=0, notes=[f"chemical {params.chemical!r} not found in ClinPGx"]
            )
        data, url = await self._data(f"data/chemical/{quote(resolved[0])}", {"view": "max"})
        if not isinstance(data, dict):
            return ToolResult(
                source=self.name, tool="get_chemical", total=0, request_url=url, notes=[f"chemical {resolved[0]} not found"]
            )
        return ToolResult(source=self.name, tool="get_chemical", records=[_chemical_record(data)], total=1, request_url=url)

    @tool(
        description="""
        ClinPGx clinical annotations: curated variant/haplotype-drug-phenotype associations with a level of
        evidence (1A = CPIC/DPWG guideline or FDA label backed ... 4 = unsupported/case report), phenotype
        category (Dosage, Efficacy, Toxicity, Metabolism/PK), drugs, diseases, genotype-specific phenotype text
        and supporting PubMed IDs. Give gene and/or drug and/or variant (rsID or star allele, e.g. CYP2D6*4 with
        gene=CYP2D6). Sorted strongest evidence first; filter with levels=['1A','1B']. Example: gene='CYP2C19',
        drug='clopidogrel'. For prescribing actions use dosing_guidelines.
        """
    )
    async def clinical_annotations(self, params: ClinicalAnnotationsParams) -> ToolResult:
        if not (params.gene or params.drug or params.variant):
            return ToolResult(
                source=self.name, tool="clinical_annotations", notes=["give at least one of gene, drug or variant"]
            )
        query: dict[str, Any] = {"view": "min"}
        notes: list[str] = []
        haplotype = None
        if params.gene:
            symbol = params.gene.strip().upper()
            if _PA.match(symbol) or symbol.startswith("HGNC:"):
                resolved = await self._resolve_gene(symbol)
                if not resolved:
                    return ToolResult(
                        source=self.name, tool="clinical_annotations", total=0,
                        notes=[f"gene {params.gene!r} not found in ClinPGx"],
                    )
                symbol = resolved[1]
            query["location.genes.symbol"] = symbol
        if params.drug:
            resolved = await self._resolve_chemical(params.drug)
            if not resolved:
                return ToolResult(
                    source=self.name, tool="clinical_annotations", total=0,
                    notes=[f"drug {params.drug!r} not found in ClinPGx"],
                )
            query["relatedChemicals.accessionId"] = resolved[0]
            notes.append(f"drug {params.drug} -> {resolved[1]} ({resolved[0]})")
        if params.variant:
            variant = params.variant.strip()
            if _RSID.match(variant):
                query["location.fingerprint"] = variant.lower()
            else:
                haplotype = variant.upper()
                if not params.gene and "*" in variant:
                    query["location.genes.symbol"] = variant.split("*")[0].upper()
        if len(params.levels or []) == 1:
            query["levelOfEvidence.term"] = params.levels[0]  # type: ignore[index]
        data, url = await self._data("data/summaryAnnotation", query)
        items = [i for i in data or [] if isinstance(i, dict)]
        if params.levels:
            items = [i for i in items if (i.get("levelOfEvidence") or {}).get("term") in params.levels]
        if params.phenotype_category:
            items = [i for i in items if params.phenotype_category in (i.get("types") or [])]
        if haplotype:
            items = [
                i
                for i in items
                if haplotype in [h.upper() for h in _names((i.get("location") or {}).get("haplotypes"), "symbol")]
                or haplotype in str((i.get("location") or {}).get("displayName", "")).upper()
            ]
        items.sort(key=lambda i: _LEVEL_RANK.get((i.get("levelOfEvidence") or {}).get("term"), 9))
        page, nxt = _page(items, params.cursor, params.limit)
        if params.include_evidence:
            full = []
            for item in page:
                acc = item.get("accessionId") or item.get("id")
                detail, _ = await self._data(f"data/summaryAnnotation/{quote(str(acc))}", {"view": "max"})
                full.append(detail if isinstance(detail, dict) else item)
            page = full
        if not items:
            notes.append("no clinical annotations match these filters")
        return ToolResult(
            source=self.name,
            tool="clinical_annotations",
            records=[_annotation_record(i) for i in page],
            total=len(items),
            next_cursor=nxt,
            request_url=url,
            notes=notes,
        )

    @tool(
        description="""
        Pharmacogenomic dosing guidelines annotated by ClinPGx from CPIC, DPWG (Dutch), CPNDS (Canadian), RNPGx
        (French) and other bodies, for a drug and/or gene: recommendation summary, whether dosing changes or
        alternative drugs are advised, involved genes/alleles, and the guideline publication (PMID/DOI/PDF URL).
        Example: drug='clopidogrel'; gene='CYP2D6'; drug='codeine', source='CPIC'. CPIC guidelines are listed first.
        """
    )
    async def dosing_guidelines(self, params: GuidelineParams) -> ToolResult:
        if not (params.drug or params.gene):
            return ToolResult(source=self.name, tool="dosing_guidelines", notes=["give a drug and/or a gene"])
        query, notes, early = await self._resolve_filters("dosing_guidelines", params.drug, params.gene)
        if early:
            return early
        if params.source:
            query["source"] = params.source.strip()
        data, url = await self._data("data/guidelineAnnotation", {**query, "view": "base"})
        items = [i for i in data or [] if isinstance(i, dict)]
        items.sort(key=lambda i: (_GUIDELINE_RANK.get(i.get("source"), 9), i.get("name") or ""))
        page, nxt = _page(items, params.cursor, params.limit)
        if not items:
            notes.append("no dosing guideline annotations for these filters")
        return ToolResult(
            source=self.name,
            tool="dosing_guidelines",
            records=[_guideline_record(i) for i in page],
            total=len(items),
            next_cursor=nxt,
            request_url=url,
            notes=notes,
        )

    @tool(
        description="""
        Pharmacogenomic information in regulatory drug labels (FDA, EMA, Health Canada HCSC, Japan PMDA,
        Swissmedic) as annotated by ClinPGx, for a drug and/or gene: PGx testing level (Testing Required /
        Recommended / Actionable / Informative), FDA biomarker-list status, prescribing text, genes and alleles,
        FDA application number. Example: drug='abacavir'; gene='HLA-B', source='FDA'. For full label text use openFDA.
        """
    )
    async def drug_labels(self, params: DrugLabelParams) -> ToolResult:
        if not (params.drug or params.gene):
            return ToolResult(source=self.name, tool="drug_labels", notes=["give a drug and/or a gene"])
        query, notes, early = await self._resolve_filters("drug_labels", params.drug, params.gene)
        if early:
            return early
        if params.source:
            query["source"] = params.source
        data, url = await self._data("data/label", {**query, "view": "base"})
        items = [i for i in data or [] if isinstance(i, dict)]
        if params.testing_level:
            items = [i for i in items if (i.get("testing") or {}).get("term") == params.testing_level]
        items.sort(key=lambda i: (_TESTING_RANK.get((i.get("testing") or {}).get("term"), 5), i.get("name") or ""))
        page, nxt = _page(items, params.cursor, params.limit)
        if not items:
            notes.append("no PGx drug-label annotations for these filters")
        return ToolResult(
            source=self.name,
            tool="drug_labels",
            records=[_label_record(i) for i in page],
            total=len(items),
            next_cursor=nxt,
            request_url=url,
            notes=notes,
        )
