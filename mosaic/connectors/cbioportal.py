"""cBioPortal for Cancer Genomics connector (public instance).

Docs: https://docs.cbioportal.org/web-api-and-clients/ ; OpenAPI: https://www.cbioportal.org/api/v3/api-docs
Base: https://www.cbioportal.org/api . Auth: none for the public portal.
No published rate limit; we use 3 requests/second (mutation fetches can be large).

Quirks:
* List endpoints are paged with ``pageSize`` / ``pageNumber`` (0-based); totals are only
  available from ``projection=META`` via the ``total-count`` response header.
* ``pmid`` on a study is a comma-separated list, kept in ``attributes.pmids``. It is not an
  identifier: the papers are related entities, and one paper often backs many studies (all 32
  TCGA PanCancer Atlas studies cite the same PMIDs), so it would merge unrelated studies.
* TCGA studies come in several cohorts per project (``brca_tcga``, ``_pub2015``, ``_firehose_legacy``,
  ``_pan_can_atlas_2018``, ``_gdc``) with different samples. Only the GDC import (``<x>_tcga_gdc``)
  carries ``gdc_project`` as an identifier (it is that project's data); for the others the GDC project
  is a related entity in ``attributes``, so sibling cohorts do not merge.
* Mutation (variant) records are one protein change in one study; the gene's ``entrez_gene`` stays in
  ``attributes`` (only the non-linking ``gene_symbol`` is an identifier), so the changes of a gene do
  not merge with each other or with the gene record.
* Mutation frequency = distinct samples with >=1 mutation in the gene / the study's
  ``sequencedSampleCount`` (samples profiled for mutations), as on the portal (mutations only;
  copy-number alterations and fusions are not counted).
* Unknown study / gene -> HTTP 404 ``{"message": "... not found"}``.
"""

from __future__ import annotations

import re

from collections import Counter, defaultdict
from typing import Any
from urllib.parse import quote

from pydantic import Field

from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, strip_html, tool

API = "https://www.cbioportal.org/api"
WEB = "https://www.cbioportal.org"


class GetStudyParams(ToolParams):
    study_id: str = Field(description="cBioPortal study id, e.g. brca_tcga_pan_can_atlas_2018, msk_impact_2017")


class AlterationFrequencyParams(ToolParams):
    gene: str = Field(description="HGNC gene symbol (TP53, PIK3CA) or NCBI Entrez Gene id (7157)")
    study_ids: list[str] = Field(
        min_length=1,
        max_length=10,
        description="1-10 study ids (find them with search_studies), e.g. ['brca_tcga_pan_can_atlas_2018', "
        "'luad_tcga_pan_can_atlas_2018']",
    )


class MutationsInGeneParams(ToolParams):
    study_id: str = Field(description="cBioPortal study id, e.g. coadread_tcga_pan_can_atlas_2018")
    gene: str = Field(description="HGNC gene symbol (KRAS) or NCBI Entrez Gene id (3845)")
    limit: int = Field(25, ge=1, le=100, description="Number of distinct protein changes to return (most frequent first)")


def _pmids(value: Any) -> list[str]:
    out: list[str] = []
    for part in str(value or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit() and part not in out:
            out.append(part)
    return out


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


_COUNT_FIELDS = (
    "sequencedSampleCount", "cnaSampleCount", "mrnaRnaSeqV2SampleCount", "mrnaRnaSeqSampleCount",
    "mrnaMicroarraySampleCount", "miRnaSampleCount", "methylationHm27SampleCount", "rppaSampleCount",
    "massSpectrometrySampleCount", "completeSampleCount", "structuralVariantCount", "treatmentCount",
)


_TCGA_STUDY = re.compile(r"^([a-z]+)_tcga(?:_pan_can_atlas_2018|_gdc|_firehose_legacy|_pub\w*)?$")


def _gdc_project(study_id: str) -> str | None:
    """TCGA studies mirror an NCI GDC project: ``skcm_tcga_gdc`` -> ``TCGA-SKCM`` (links to nci_gdc)."""
    match = _TCGA_STUDY.match(study_id)
    return f"TCGA-{match.group(1).upper()}" if match else None


def study_record(item: dict[str, Any]) -> Record:
    sid = item.get("studyId") or ""
    pmids = _pmids(item.get("pmid"))
    cancer = item.get("cancerType") or {}
    counts = {k.replace("SampleCount", "").replace("Count", ""): item[k] for k in _COUNT_FIELDS if item.get(k)}
    gdc = _gdc_project(sid)
    gdc_import = sid.endswith("_tcga_gdc")  # the same data as the GDC project; other cohorts are only related
    return Record(
        source="cbioportal",
        id=sid,
        type="study",
        title=item.get("name") or sid,
        url=f"{WEB}/study/summary?id={quote(sid)}",
        identifiers={"cbioportal": sid, "gdc_project": gdc if gdc_import else None},
        summary=strip_html(item.get("description")),
        attributes=_compact(
            {
                "gdc_project": None if gdc_import else gdc,
                "cancer_type_id": item.get("cancerTypeId"),
                "cancer_type": cancer.get("name"),
                "citation": item.get("citation"),
                "pmids": pmids,
                "samples": item.get("allSampleCount") if (item.get("allSampleCount") or 0) > 1 else None,
                "sample_counts": counts,
                "reference_genome": item.get("referenceGenome"),
                "groups": item.get("groups"),
                "import_date": item.get("importDate"),
            }
        ),
    )


class CBioPortal(Connector):
    name = "cbioportal"
    title = "cBioPortal for Cancer Genomics"
    category = "genomics"
    description = (
        "Public cBioPortal: 400+ curated cancer genomics studies (TCGA PanCancer Atlas, MSK-IMPACT, "
        "AACR GENIE, published cohorts) with sample-level mutations, copy number, expression and "
        "clinical data. Use to find cancer studies and to compute gene mutation frequencies and "
        "hotspot protein changes per study."
    )
    homepage = "https://www.cbioportal.org/"
    docs_url = "https://www.cbioportal.org/api/swagger-ui/index.html"
    auth = "none"
    rate_limit = 3.0
    health_query = "breast"
    nih_repositories = ("cBioPortal",)

    async def _gene(self, gene: str) -> dict[str, Any] | None:
        response = await self.request("GET", f"{API}/genes/{quote(gene.strip().upper())}", allow_status=(404, 400))
        return response.json() if response.status == 200 else None

    @tool(
        description="""
        Search cBioPortal cancer genomics studies by keyword over study name, description, cancer
        type and citation (e.g. "breast", "lung adenocarcinoma", "pan_can_atlas", "MSK"). Returns
        study ids (needed by the other cBioPortal tools), name, cancer type, sample count, citation
        and PMID. For harmonized TCGA/TARGET/CPTAC data files use nci_gdc.
        """,
        federated=True,
    )
    async def search_studies(self, params: SearchParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        query = {"keyword": params.query.strip(), "projection": "SUMMARY", "pageSize": params.limit,
                 "pageNumber": offset // params.limit, "sortBy": "name", "direction": "ASC"}
        response = await self.request("GET", f"{API}/studies", params=query)
        items = response.json() or []
        meta = await self.request(
            "GET", f"{API}/studies", params={"keyword": params.query.strip(), "projection": "META"}
        )
        try:
            total = int(meta.headers.get("total-count") or 0)
        except ValueError:
            total = None
        nxt = offset + len(items)
        return ToolResult(
            source=self.name,
            tool="search_studies",
            records=[study_record(i) for i in items],
            total=total,
            next_cursor=str(nxt) if items and total and nxt < total else None,
            request_url=response.url,
            notes=[] if items else [f"no cBioPortal study matches {params.query!r}"],
        )

    @tool(
        description="""
        Details for one cBioPortal study: name, description, cancer type, citation/PMIDs, reference
        genome, sample counts per data type (sequenced, CNA, RNA-seq, RPPA...) and its molecular
        profiles (data.molecular_profiles: id, alteration type, name).
        """
    )
    async def get_study(self, params: GetStudyParams) -> ToolResult:
        sid = params.study_id.strip()
        response = await self.request("GET", f"{API}/studies/{quote(sid)}", allow_status=(404,))
        if response.status == 404:
            return ToolResult(source=self.name, tool="get_study", notes=[f"study {sid} not found; use search_studies"])
        record = study_record(response.json() or {})
        profiles = await self.get_json(f"{API}/studies/{quote(sid)}/molecular-profiles", params={"projection": "SUMMARY"})
        return ToolResult(
            source=self.name,
            tool="get_study",
            records=[record],
            total=1,
            data={
                "molecular_profiles": [
                    {"id": p.get("molecularProfileId"), "type": p.get("molecularAlterationType"), "name": p.get("name")}
                    for p in profiles or []
                ]
            },
            request_url=response.url,
        )

    async def _mutations(self, profile_ids: list[str], entrez: int) -> tuple[list[dict[str, Any]], str]:
        response = await self.request(
            "POST",
            f"{API}/mutations/fetch",
            params={"projection": "SUMMARY", "pageSize": 100000},
            json_body={"molecularProfileIds": profile_ids, "entrezGeneIds": [entrez]},
        )
        return response.json() or [], response.url

    async def _mutation_profiles(self, study_ids: list[str]) -> dict[str, str]:
        profiles = await self.post_json(
            f"{API}/molecular-profiles/fetch", {"studyIds": study_ids}, params={"projection": "SUMMARY"}
        )
        out: dict[str, str] = {}
        for p in profiles or []:
            if p.get("molecularAlterationType") == "MUTATION_EXTENDED" and p.get("studyId") not in out:
                out[p["studyId"]] = p["molecularProfileId"]
        return out

    @tool(
        description="""
        Percentage of profiled samples with >=1 mutation in a gene, per cBioPortal study (max 10
        studies per call). Input gene symbol or Entrez id plus study ids from search_studies.
        Returns a gene record (Entrez id) and data.studies = rows of {study_id, study_name,
        mutated_samples, profiled_samples, percent, top_protein_changes}. Mutations only (no CNA /
        fusions). For pan-cancer GDC projects use nci_gdc gene_mutation_frequency.
        """
    )
    async def gene_alteration_frequency(self, params: AlterationFrequencyParams) -> ToolResult:
        gene = await self._gene(params.gene)
        if not gene:
            return ToolResult(source=self.name, tool="gene_alteration_frequency", notes=[f"gene {params.gene!r} not found in cBioPortal"])
        study_ids = list(dict.fromkeys(s.strip() for s in params.study_ids if s.strip()))
        studies = await self.post_json(f"{API}/studies/fetch", study_ids, params={"projection": "DETAILED"})
        by_id = {s.get("studyId"): s for s in studies or []}
        notes = [f"study {s} not found" for s in study_ids if s not in by_id]
        profiles = await self._mutation_profiles([s for s in study_ids if s in by_id])
        notes += [f"study {s} has no mutation profile" for s in study_ids if s in by_id and s not in profiles]
        rows = []
        url = None
        if profiles:
            mutations, url = await self._mutations(list(profiles.values()), int(gene["entrezGeneId"]))
            samples: dict[str, set[str]] = defaultdict(set)
            changes: dict[str, Counter] = defaultdict(Counter)
            for m in mutations:
                sid = m.get("studyId")
                samples[sid].add(m.get("sampleId"))
                if m.get("proteinChange"):
                    changes[sid][m["proteinChange"]] += 1
            for sid in profiles:
                study = by_id.get(sid) or {}
                profiled = study.get("sequencedSampleCount")
                mutated = len(samples.get(sid, ()))
                rows.append(
                    _compact(
                        {
                            "study_id": sid,
                            "study_name": study.get("name"),
                            "mutated_samples": mutated,
                            "profiled_samples": profiled,
                            "percent": round(100.0 * mutated / profiled, 1) if profiled else None,
                            "top_protein_changes": [f"{k} ({v})" for k, v in changes[sid].most_common(5)],
                        }
                    )
                )
        rows.sort(key=lambda r: -(r.get("percent") or 0))
        symbol = gene.get("hugoGeneSymbol")
        return ToolResult(
            source=self.name,
            tool="gene_alteration_frequency",
            records=[
                Record(
                    source="cbioportal",
                    id=str(gene.get("entrezGeneId")),
                    type="gene",
                    title=symbol,
                    url=f"{WEB}/results/mutations?cancer_study_list={','.join(profiles)}&gene_list={quote(symbol or '')}",
                    identifiers={"entrez_gene": gene.get("entrezGeneId"), "gene_symbol": symbol},
                    attributes=_compact({"gene_type": gene.get("type")}),
                )
            ],
            total=len(rows),
            data={"gene": symbol, "studies": rows},
            request_url=url,
            notes=notes,
        )

    @tool(
        description="""
        Most frequent protein changes (e.g. KRAS G12D, G12V) for a gene in one cBioPortal study:
        one variant record per distinct protein change with sample count, mutation type (Missense,
        Nonsense, Frame_Shift...), genomic position and alleles, plus data.mutated_samples /
        profiled_samples. Use to identify hotspots in a specific cohort.
        """
    )
    async def mutations_in_gene(self, params: MutationsInGeneParams) -> ToolResult:
        sid = params.study_id.strip()
        gene = await self._gene(params.gene)
        if not gene:
            return ToolResult(source=self.name, tool="mutations_in_gene", notes=[f"gene {params.gene!r} not found in cBioPortal"])
        study_resp = await self.request("GET", f"{API}/studies/{quote(sid)}", allow_status=(404,))
        if study_resp.status == 404:
            return ToolResult(source=self.name, tool="mutations_in_gene", notes=[f"study {sid} not found; use search_studies"])
        study = study_resp.json() or {}
        profiles = await self._mutation_profiles([sid])
        if sid not in profiles:
            return ToolResult(source=self.name, tool="mutations_in_gene", notes=[f"study {sid} has no mutation data"])
        mutations, url = await self._mutations([profiles[sid]], int(gene["entrezGeneId"]))
        symbol = gene.get("hugoGeneSymbol") or params.gene.upper()
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for m in mutations:
            groups[m.get("proteinChange") or "(no protein change)"].append(m)
        ranked = sorted(groups.items(), key=lambda kv: -len({m.get("sampleId") for m in kv[1]}))
        records = []
        for change, items in ranked[: params.limit]:
            first_m = items[0]
            types = Counter(m.get("mutationType") for m in items if m.get("mutationType"))
            positions = sorted(
                {
                    f"{m.get('chr')}:{m.get('startPosition')} {m.get('referenceAllele')}>{m.get('variantAllele')}"
                    for m in items
                    if m.get("chr") and m.get("startPosition")
                }
            )
            records.append(
                Record(
                    source="cbioportal",
                    id=f"{symbol} {change}",
                    type="variant",
                    title=f"{symbol} {change}",
                    url=f"{WEB}/results/mutations?cancer_study_list={quote(sid)}&gene_list={quote(symbol)}",
                    identifiers={"gene_symbol": symbol},  # the gene is a related entity (module docstring)
                    attributes=_compact(
                        {
                            "entrez_gene": str(gene["entrezGeneId"]) if gene.get("entrezGeneId") else None,
                            "samples": len({m.get("sampleId") for m in items}),
                            "mutation_type": types.most_common(1)[0][0] if types else None,
                            "protein_position": first_m.get("proteinPosStart"),
                            "genomic_changes": positions[:5],
                            "ncbi_build": first_m.get("ncbiBuild"),
                            "variant_type": first_m.get("variantType"),
                            "study_id": sid,
                        }
                    ),
                )
            )
        mutated = len({m.get("sampleId") for m in mutations})
        profiled = study.get("sequencedSampleCount")
        return ToolResult(
            source=self.name,
            tool="mutations_in_gene",
            records=records,
            total=len(groups),
            data=_compact(
                {
                    "study_id": sid,
                    "gene": symbol,
                    "mutated_samples": mutated,
                    "profiled_samples": profiled,
                    "percent": round(100.0 * mutated / profiled, 1) if profiled else None,
                    "mutations": len(mutations),
                }
            ),
            request_url=url,
            notes=[] if mutations else [f"no {symbol} mutations in {sid}"],
        )
