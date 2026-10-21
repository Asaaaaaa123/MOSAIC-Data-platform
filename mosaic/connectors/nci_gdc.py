"""NCI Genomic Data Commons (GDC) connector.

Docs: https://docs.gdc.cancer.gov/API/Users_Guide/
Base: https://api.gdc.cancer.gov . Auth: none for open-access metadata and open files;
controlled-access data (BAMs, germline/unmasked VCFs) needs dbGaP authorization and a GDC
token, which this connector never uses: it only performs metadata discovery.
No published rate limit; we use 3 requests/second.

Quirks:
* Filters are JSON trees (``{"op":"and","content":[{"op":"in","content":{"field":...,"value":[...]}}]}``),
  sent in a POST body. ``size=0`` breaks ``pagination.total`` (reports 0), so counts come
  from facets.
* There is no free-text search over projects: ``search_projects`` downloads the full project
  list (~90 projects, cached 24 h) and ranks it client-side.
* Mutation frequencies use the portal's GraphQL endpoint (``/v0/graphql``, explore.cases /
  explore.genes) with the portal's filters: cases with ``available_variation_data=ssm`` as the
  denominator and ``NOT cases.gene.ssm.observation.observation_id MISSING`` for "has a simple
  somatic mutation in the gene". This counts distinct cases (REST ``/ssm_occurrences`` facets
  count case x mutation pairs and overcount long genes such as TTN); a ``genes.gene_id`` case
  filter alone also matches copy-number-altered cases.
* Unknown project -> 404 ``{"message": "... not found"}``.
Identifiers: ``gdc_project`` (TCGA-BRCA) on project records, ``gdc_file`` (file UUID, non-canonical) on
file records, ``gene_symbol``, ``ensembl_gene``, ``entrez_gene``, ``hgnc``, ``uniprot`` on gene records.
Related entities stay in attributes so unrelated records do not merge: the dbGaP accession is shared by
every project of a program (phs000178 = all 33 TCGA projects), so it is ``attributes['dbgap_accession']``;
a file's project is ``attributes['projects']``; a top-mutated gene's project is ``attributes['project']``.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from pydantic import Field

from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, tool

API = "https://api.gdc.cancer.gov"
PORTAL = "https://portal.gdc.cancer.gov"

_PROJECT_FIELDS = ",".join(
    [
        "project_id", "name", "disease_type", "primary_site", "program.name", "program.dbgap_accession_number",
        "dbgap_accession_number", "released", "state", "summary.case_count", "summary.file_count",
        "summary.file_size", "summary.data_categories.data_category", "summary.data_categories.case_count",
        "summary.data_categories.file_count", "summary.experimental_strategies.experimental_strategy",
        "summary.experimental_strategies.case_count", "summary.experimental_strategies.file_count",
    ]
)
_FILE_FIELDS = ",".join(
    [
        "file_id", "file_name", "file_size", "access", "data_category", "data_type", "data_format",
        "experimental_strategy", "platform", "analysis.workflow_type", "cases.project.project_id",
        "cases.submitter_id", "created_datetime", "updated_datetime", "md5sum",
    ]
)
GRAPHQL = f"{API}/v0/graphql"

# Portal-equivalent filters: cases with somatic mutation data / cases with >=1 SSM in the gene.
_SSM_AVAILABLE = {"op": "in", "content": {"field": "cases.available_variation_data", "value": ["ssm"]}}
_HAS_SSM = {"op": "NOT", "content": {"field": "cases.gene.ssm.observation.observation_id", "value": "MISSING"}}

_CASE_COUNTS_QUERY = """
query($mutated: FiltersArgument, $profiled: FiltersArgument) {
  viewer { explore {
    mutated: cases { aggregations(filters: $mutated) { project__project_id { buckets { key doc_count } } } }
    profiled: cases { aggregations(filters: $profiled) { project__project_id { buckets { key doc_count } } } }
  } }
}
"""

_TOP_GENES_QUERY = """
query($genes: FiltersArgument, $profiled: FiltersArgument, $first: Int) {
  viewer { explore {
    genes { hits(first: $first, filters: $genes, score: "case.project.project_id") {
      total edges { node { gene_id symbol name is_cancer_gene_census score } } } }
    profiled: cases { aggregations(filters: $profiled) { project__project_id { buckets { key doc_count } } } }
  } }
}
"""


class GetProjectParams(ToolParams):
    project_id: str = Field(description="GDC project id, e.g. TCGA-BRCA, TARGET-AML, CPTAC-3, MMRF-COMMPASS")


class GeneMutationParams(ToolParams):
    gene: str = Field(description="HGNC gene symbol (TP53, KRAS) or Ensembl gene id (ENSG00000141510)")
    project_ids: list[str] | None = Field(
        None, description="Restrict to these GDC projects (e.g. ['TCGA-BRCA','TCGA-LUAD']); default all projects"
    )
    limit: int = Field(25, ge=1, le=100, description="Maximum number of projects to list (most mutated first)")


class TopGenesParams(ToolParams):
    project_id: str = Field(description="GDC project id, e.g. TCGA-LUAD")
    limit: int = Field(20, ge=1, le=100, description="Number of genes to return")


class SearchFilesParams(ToolParams):
    project_id: str | None = Field(None, description="GDC project id, e.g. TCGA-BRCA")
    data_category: str | None = Field(
        None,
        description="e.g. 'Simple Nucleotide Variation', 'Transcriptome Profiling', 'Copy Number Variation', "
        "'DNA Methylation', 'Clinical', 'Biospecimen', 'Sequencing Reads', 'Proteome Profiling'",
    )
    data_type: str | None = Field(
        None,
        description="e.g. 'Masked Somatic Mutation', 'Gene Expression Quantification', 'Gene Level Copy Number', "
        "'Methylation Beta Value', 'Clinical Supplement', 'Aligned Reads', 'Slide Image'",
    )
    experimental_strategy: str | None = Field(
        None, description="e.g. WXS, WGS, RNA-Seq, miRNA-Seq, 'Methylation Array', 'Genotyping Array', 'Tissue Slide'"
    )
    data_format: str | None = Field(None, description="e.g. MAF, TSV, VCF, BAM, SVS, BCR XML")
    access: str = Field("open", description="open (default) or controlled (controlled files need dbGaP approval)")
    limit: int = Field(20, ge=1, le=100, description="Maximum number of files")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous call)")


def _in(field: str, values: list[str] | str) -> dict[str, Any]:
    return {"op": "in", "content": {"field": field, "value": values if isinstance(values, list) else [values]}}


def _and(*clauses: dict[str, Any] | None) -> dict[str, Any]:
    return {"op": "and", "content": [c for c in clauses if c]}


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _buckets(node: dict[str, Any] | None) -> dict[str, int]:
    aggs = ((node or {}).get("aggregations") or {}).get("project__project_id") or {}
    return {str(b.get("key")): int(b.get("doc_count") or 0) for b in aggs.get("buckets") or [] if b.get("key")}


def project_record(item: dict[str, Any]) -> Record:
    pid = item.get("project_id") or item.get("id") or ""
    summary = item.get("summary") or {}
    program = item.get("program") or {}
    sites = item.get("primary_site") or []
    diseases = item.get("disease_type") or []
    cats = {c.get("data_category"): c.get("case_count") for c in summary.get("data_categories") or [] if c.get("data_category")}
    strategies = {
        s.get("experimental_strategy"): s.get("case_count")
        for s in summary.get("experimental_strategies") or []
        if s.get("experimental_strategy")
    }
    dbgap = item.get("dbgap_accession_number") or program.get("dbgap_accession_number")
    text = (
        f"{program.get('name') or pid.split('-')[0]} project {pid}: {item.get('name') or ''}. "
        f"{summary.get('case_count', 'n/a')} cases, {summary.get('file_count', 'n/a')} files. "
        f"Primary site(s): {', '.join(sites[:6]) or 'n/a'}. Disease type(s): {', '.join(diseases[:6]) or 'n/a'}."
    )
    return Record(
        source="nci_gdc",
        id=pid,
        type="dataset",
        title=f"{pid}: {item.get('name') or ''}".rstrip(": "),
        url=f"{PORTAL}/projects/{pid}",
        identifiers={"gdc_project": pid},
        summary=text,
        attributes=_compact(
            {
                "program": program.get("name"),
                "case_count": summary.get("case_count"),
                "file_count": summary.get("file_count"),
                "file_size_gb": round(summary["file_size"] / 1e9, 1) if summary.get("file_size") else None,
                "primary_site": sites,
                "disease_type": diseases,
                "data_categories_cases": cats,
                "experimental_strategies_cases": strategies,
                "dbgap_accession": dbgap,
                "released": item.get("released"),
                "state": item.get("state"),
            }
        ),
    )


def file_record(item: dict[str, Any]) -> Record:
    fid = item.get("file_id") or item.get("id") or ""
    projects = sorted({(c.get("project") or {}).get("project_id") for c in item.get("cases") or [] if (c.get("project") or {}).get("project_id")})
    cases = [c.get("submitter_id") for c in item.get("cases") or [] if c.get("submitter_id")]
    return Record(
        source="nci_gdc",
        id=fid,
        type="dataset",
        title=item.get("file_name") or fid,
        url=f"{PORTAL}/files/{fid}",
        identifiers={"gdc_file": fid},
        date=(item.get("created_datetime") or "")[:10] or None,
        summary=" | ".join(
            str(x) for x in (item.get("data_category"), item.get("data_type"), item.get("experimental_strategy"),
                             item.get("data_format"), f"{item.get('access')} access") if x
        ) or None,
        attributes=_compact(
            {
                "access": item.get("access"),
                "file_size_bytes": item.get("file_size"),
                "data_category": item.get("data_category"),
                "data_type": item.get("data_type"),
                "data_format": item.get("data_format"),
                "experimental_strategy": item.get("experimental_strategy"),
                "platform": item.get("platform"),
                "workflow_type": (item.get("analysis") or {}).get("workflow_type"),
                "projects": projects,
                "case_submitter_ids": cases[:5],
                "case_count": len(cases) if len(cases) > 5 else None,
                "md5sum": item.get("md5sum"),
                "updated": (item.get("updated_datetime") or "")[:10] or None,
            }
        ),
    )


def gene_record(item: dict[str, Any]) -> Record:
    ext = item.get("external_db_ids") or {}
    gid = item.get("gene_id") or item.get("id") or ""
    return Record(
        source="nci_gdc",
        id=gid,
        type="gene",
        title=f"{item.get('symbol')} ({item.get('name')})" if item.get("name") else item.get("symbol"),
        url=f"{PORTAL}/genes/{gid}",
        identifiers={
            "gene_symbol": item.get("symbol"),
            "ensembl_gene": gid,
            "entrez_gene": (ext.get("entrez_gene") or [None])[0],
            "hgnc": (ext.get("hgnc") or [None])[0],
            "uniprot": (ext.get("uniprotkb_swissprot") or [None])[0],
        },
        summary=item.get("description"),
        attributes=_compact(
            {
                "biotype": item.get("biotype"),
                "cytoband": item.get("cytoband"),
                "cancer_gene_census": item.get("is_cancer_gene_census"),
            }
        ),
    )


def _score(item: dict[str, Any], terms: list[str], phrase: str) -> int:
    pid = (item.get("project_id") or "").lower()
    name = (item.get("name") or "").lower()
    program = ((item.get("program") or {}).get("name") or "").lower()
    sites = " ".join(item.get("primary_site") or []).lower()
    diseases = " ".join(item.get("disease_type") or []).lower()
    if phrase in (pid, name):
        return 1000
    score = 0
    if phrase and phrase in name:
        score += 50
    for term in terms:
        if term == pid or term == program:
            score += 40
        elif term in pid:
            score += 25
        if re.search(rf"\b{re.escape(term)}", name):
            score += 20
        if term in sites:
            score += 10
        if term in diseases:
            score += 5
    return score


class NciGdc(Connector):
    name = "nci_gdc"
    title = "NCI Genomic Data Commons (GDC)"
    category = "genomics"
    description = (
        "NCI's harmonized cancer genomics repository: ~90 projects (TCGA, TARGET, CPTAC, MMRF, "
        "HCMI...) with 45k+ cases and millions of files of somatic mutation, expression, copy number, "
        "methylation, clinical and imaging data. Use for cancer dataset discovery, project case/file "
        "counts and somatic mutation frequencies across cancer projects."
    )
    homepage = "https://portal.gdc.cancer.gov/"
    docs_url = "https://docs.gdc.cancer.gov/API/Users_Guide/"
    auth = "none"
    rate_limit = 3.0
    health_query = "breast"
    nih_repositories = ("Genomic Data Commons (GDC)",)

    async def _all_projects(self) -> tuple[list[dict[str, Any]], str]:
        response = await self.request(
            "GET",
            f"{API}/projects",
            params={"size": 200, "fields": _PROJECT_FIELDS, "format": "json"},
            cache_ttl=86400,
        )
        hits = ((response.json() or {}).get("data") or {}).get("hits") or []
        return hits, response.url

    @tool(
        description="""
        Find GDC cancer genomics projects by keywords over project id, name, program, primary site and
        disease type (e.g. "breast", "TCGA lung", "pediatric leukemia", "CPTAC"). Returns projects with
        case and file counts, primary sites, disease types and per-data-category case counts. An empty
        or generic query ("all", "*") lists every project by case count. Then use get_project,
        search_files or gene_mutation_frequency.
        """,
        federated=True,
    )
    async def search_projects(self, params: SearchParams) -> ToolResult:
        projects, url = await self._all_projects()
        phrase = params.query.strip().lower()
        terms = [t for t in re.split(r"[\s,;]+", phrase) if t and t not in ("project", "projects", "cancer", "tumor", "the", "and", "of")]
        if phrase in ("", "*", "all"):
            ranked = sorted(projects, key=lambda p: -((p.get("summary") or {}).get("case_count") or 0))
        else:
            scored = [(p, _score(p, terms or [phrase], phrase)) for p in projects]
            ranked = [
                p for p, s in sorted(scored, key=lambda x: (-x[1], -((x[0].get("summary") or {}).get("case_count") or 0))) if s > 0
            ]
        offset = parse_offset(params.cursor)
        page = ranked[offset : offset + params.limit]
        nxt = offset + len(page)
        return ToolResult(
            source=self.name,
            tool="search_projects",
            records=[project_record(p) for p in page],
            total=len(ranked),
            next_cursor=str(nxt) if page and nxt < len(ranked) else None,
            request_url=url,
            notes=[] if ranked else [f"no GDC project matches {params.query!r}; try a primary site (e.g. 'lung') or program (TCGA, TARGET, CPTAC)"],
        )

    @tool(
        description="""
        Details for one GDC project (TCGA-BRCA): name, program, dbGaP accession, primary sites, disease
        types, case and file counts, total size, and case/file counts per data category (Simple
        Nucleotide Variation, Transcriptome Profiling, ...) and per experimental strategy (WXS,
        RNA-Seq, ...).
        """
    )
    async def get_project(self, params: GetProjectParams) -> ToolResult:
        pid = params.project_id.strip().upper()
        response = await self.request(
            "GET",
            f"{API}/projects/{quote(pid)}",
            params={"expand": "summary,summary.data_categories,summary.experimental_strategies,program"},
            allow_status=(404,),
        )
        if response.status == 404:
            return ToolResult(source=self.name, tool="get_project", notes=[f"GDC project {pid} not found; use search_projects"])
        item = (response.json() or {}).get("data") or {}
        record = project_record(item)
        for key in ("data_categories", "experimental_strategies"):
            rows = (item.get("summary") or {}).get(key) or []
            label = "data_category" if key == "data_categories" else "experimental_strategy"
            record.attributes[f"{key}_detail"] = [
                {label: r.get(label), "cases": r.get("case_count"), "files": r.get("file_count")} for r in rows
            ]
        return ToolResult(source=self.name, tool="get_project", records=[record], total=1, request_url=response.url)

    async def _gene(self, gene: str) -> dict[str, Any] | None:
        field = "gene_id" if gene.upper().startswith("ENSG") else "symbol"
        value = gene.upper().split(".")[0]
        payload = await self.post_json(
            f"{API}/genes",
            {
                "filters": _in(field, [value]),
                "fields": "gene_id,symbol,name,description,biotype,cytoband,is_cancer_gene_census,"
                "external_db_ids.entrez_gene,external_db_ids.hgnc,external_db_ids.uniprotkb_swissprot",
                "size": 1,
            },
            cache_ttl=86400,
        )
        hits = ((payload or {}).get("data") or {}).get("hits") or []
        return hits[0] if hits else None

    @tool(
        description="""
        How often a gene carries simple somatic mutations (SNVs/indels) in each GDC cancer project,
        from open-access masked somatic mutation calls. Input: gene symbol (TP53, KRAS, IDH1) or Ensembl
        id. Returns a gene record (Ensembl/Entrez/HGNC/UniProt ids) and data.projects = rows of
        {project_id, mutated_cases, ssm_cases, percent}, most mutated first, where mutated_cases is the
        number of distinct cases with >=1 somatic mutation in the gene and ssm_cases the number of cases
        with somatic mutation data in that project (same definition as the GDC portal). For
        study-level frequencies in non-GDC cohorts use cbioportal.
        """
    )
    async def gene_mutation_frequency(self, params: GeneMutationParams) -> ToolResult:
        gene = await self._gene(params.gene.strip())
        if not gene:
            return ToolResult(source=self.name, tool="gene_mutation_frequency", notes=[f"gene {params.gene!r} not found in GDC"])
        projects_filter = (
            _in("cases.project.project_id", [p.strip().upper() for p in params.project_ids]) if params.project_ids else None
        )
        profiled = _and(_SSM_AVAILABLE, projects_filter)
        mutated = _and(_SSM_AVAILABLE, projects_filter, _in("genes.gene_id", [gene.get("gene_id")]), _HAS_SSM)
        data = await self.graphql(GRAPHQL, _CASE_COUNTS_QUERY, {"mutated": mutated, "profiled": profiled})
        explore = (data.get("viewer") or {}).get("explore") or {}
        num = _buckets(explore.get("mutated"))
        den = _buckets(explore.get("profiled"))
        projects, _ = await self._all_projects()
        names = {p.get("project_id"): p.get("name") for p in projects}
        rows = [
            _compact(
                {
                    "project_id": pid,
                    "project_name": names.get(pid),
                    "mutated_cases": count,
                    "ssm_cases": den.get(pid),
                    "percent": round(100.0 * count / den[pid], 1) if den.get(pid) else None,
                }
            )
            for pid, count in num.items()
        ]
        rows.sort(key=lambda r: (-r.get("mutated_cases", 0), -(r.get("percent") or 0)))
        total_mut = sum(num.values())
        total_den = sum(den.values())
        return ToolResult(
            source=self.name,
            tool="gene_mutation_frequency",
            records=[gene_record(gene)],
            total=len(rows),
            data={
                "gene": gene.get("symbol"),
                "mutated_cases_all_projects": total_mut,
                "ssm_cases_all_projects": total_den,
                "percent_all_projects": round(100.0 * total_mut / total_den, 1) if total_den else None,
                "projects": rows[: params.limit],
            },
            request_url=GRAPHQL,
            notes=([] if rows else [f"no somatic mutations in {gene.get('symbol')} for the selected projects"])
            + ([f"showing {params.limit} of {len(rows)} projects"] if len(rows) > params.limit else []),
        )

    @tool(
        description="""
        Most frequently mutated genes in one GDC project, ranked by number of distinct cases with
        simple somatic mutations (open-access masked calls), e.g. TCGA-LUAD -> TTN, TP53, MUC16, RYR2,
        CSMD3, KRAS... Returns gene records (symbol, Ensembl id) with attributes {mutated_cases,
        percent} and data.ssm_cases (denominator). Long genes (TTN, MUC16) rank high partly due to
        length; interpret with care.
        """
    )
    async def top_mutated_genes(self, params: TopGenesParams) -> ToolResult:
        pid = params.project_id.strip().upper()
        project = _in("cases.project.project_id", [pid])
        data = await self.graphql(
            GRAPHQL,
            _TOP_GENES_QUERY,
            {"genes": _and(project), "profiled": _and(_SSM_AVAILABLE, project), "first": params.limit},
        )
        explore = (data.get("viewer") or {}).get("explore") or {}
        hits = (explore.get("genes") or {}).get("hits") or {}
        edges = hits.get("edges") or []
        denom = _buckets(explore.get("profiled")).get(pid)
        if not edges:
            return ToolResult(
                source=self.name, tool="top_mutated_genes", request_url=GRAPHQL,
                notes=[f"no open somatic mutation data for project {pid} (check the id with search_projects)"],
            )
        records = []
        for edge in edges:
            node = edge.get("node") or {}
            count = node.get("score") or 0
            records.append(
                Record(
                    source="nci_gdc",
                    id=node.get("gene_id") or "",
                    type="gene",
                    title=f"{node.get('symbol')} ({node.get('name')})",
                    url=f"{PORTAL}/genes/{node.get('gene_id')}",
                    identifiers={"gene_symbol": node.get("symbol"), "ensembl_gene": node.get("gene_id")},
                    attributes=_compact(
                        {
                            "project": pid,
                            "mutated_cases": count,
                            "percent": round(100.0 * count / denom, 1) if denom else None,
                            "cancer_gene_census": node.get("is_cancer_gene_census"),
                        }
                    ),
                )
            )
        return ToolResult(
            source=self.name,
            tool="top_mutated_genes",
            records=records,
            total=hits.get("total"),
            data={"project_id": pid, "ssm_cases": denom},
            request_url=GRAPHQL,
        )

    @tool(
        description="""
        Discover GDC data files by project, data category, data type, experimental strategy, format
        and access level (default open). Returns file UUIDs, names, sizes, access, data type,
        workflow and project, e.g. project_id=TCGA-BRCA, data_type='Masked Somatic Mutation'. This is
        for dataset discovery only: controlled-access files require dbGaP authorization and are
        never downloaded here.
        """
    )
    async def search_files(self, params: SearchFilesParams) -> ToolResult:
        clauses = [
            _in("cases.project.project_id", params.project_id.strip().upper()) if params.project_id else None,
            _in("data_category", params.data_category) if params.data_category else None,
            _in("data_type", params.data_type) if params.data_type else None,
            _in("experimental_strategy", params.experimental_strategy) if params.experimental_strategy else None,
            _in("data_format", params.data_format) if params.data_format else None,
            _in("access", params.access.strip().lower()) if params.access else None,
        ]
        offset = parse_offset(params.cursor)
        response = await self.request(
            "POST",
            f"{API}/files",
            json_body={"filters": _and(*clauses), "fields": _FILE_FIELDS, "size": params.limit, "from": offset},
        )
        data = (response.json() or {}).get("data") or {}
        hits = data.get("hits") or []
        total = (data.get("pagination") or {}).get("total")
        nxt = offset + len(hits)
        notes = []
        if params.access.strip().lower() == "controlled":
            notes.append("controlled-access files require dbGaP authorization (and a GDC token) to download")
        if not hits:
            notes.append("no files match these filters; check values against get_project data categories/strategies")
        return ToolResult(
            source=self.name,
            tool="search_files",
            records=[file_record(h) for h in hits],
            total=total,
            next_cursor=str(nxt) if hits and total and nxt < total else None,
            request_url=response.url,
            notes=notes,
        )
