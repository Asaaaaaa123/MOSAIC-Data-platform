"""Comparative Toxicogenomics Database (CTD) connector (Batch Query tool).

Docs: https://ctdbase.org/help/batchQueryHelp.jsp ; endpoint https://ctdbase.org/tools/batchQuery.go
(``inputType`` = chem | gene | disease | go, ``inputTerms`` = names / MeSH / CAS / NCBI Gene / OMIM IDs
separated by ``|``, ``report`` = cgixns | diseases_curated | diseases_inferred | chems_curated |
chems_inferred | genes_curated | ..., ``format`` = json | tsv | csv | xml).
Auth: none. CTD asks for modest use: 1 request/second and long-lived cached responses (7 days).

Quirks:
- CTD has no free-text search API, so this connector has no federated tool; every tool is a
  batch query for one entity.
- Since 2025 CTD fronts the site with an ALTCHA proof-of-work CAPTCHA: automated clients get an
  HTTP 302 (no Location header) or 200 with an HTML "verify you are a human" page instead of data.
  That is detected (status / content-type / body sniff) and raised as a clear SourceError; CTD says
  a person must complete the verification in a browser from the same machine/IP. The CAPTCHA is
  never solved programmatically.
- Report rows are whole-database dumps for the input term (bisphenol A has >30k interactions), so
  results are filtered and capped client-side; the full download is cached.
- Column names differ in case between formats/versions (``ChemicalId`` vs ``ChemicalID``,
  ``PubMedIds`` vs ``PubMedIDs``), so fields are read case-insensitively; TSV/CSV bodies (header
  line starting with ``#``) are parsed too in case ``format=json`` is ignored.
- Chemical IDs are MeSH IDs (C###### / D######); disease IDs are ``MESH:D######`` or ``OMIM:######``.
  An association record carries the MeSH ID of the *answer* entity under ``mesh``: the disease for
  chemical->disease and gene->disease, the chemical for disease->chemical and chemical-gene
  interactions; the other side is in ``attributes``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
from typing import Any, Literal

from pydantic import Field

from ..http import HttpResponse, SourceError
from ..models import HealthResult, Record, ToolResult
from .base import Connector, ToolParams, parse_offset, tool

BATCH = "https://ctdbase.org/tools/batchQuery.go"
SITE = "https://ctdbase.org"
CACHE_TTL = 7 * 86400

_BLOCK_HINT = (
    "CTD blocked this automated request with a human-verification (CAPTCHA) page instead of data. "
    "CTD requires a person to open https://ctdbase.org in a browser from this machine/network and "
    "complete the verification before batch queries work; alternatively use CTD bulk downloads "
    "(https://ctdbase.org/downloads/) or another source (e.g. Open Targets, PubChem, ChEMBL)."
)

Evidence = Literal["direct", "inferred", "both"]


# ---------------------------------------------------------------- params


class InteractionParams(ToolParams):
    chemical: str | None = Field(
        None,
        description="Chemical name (bisphenol A), MeSH ID (D019344 / C006780) or CAS RN (80-05-7). "
        "Give chemical OR gene.",
    )
    gene: str | None = Field(
        None, description="Gene symbol (ESR1) or NCBI Gene ID (2099) to get the chemicals that interact with it"
    )
    partner: str | None = Field(
        None,
        description="Keep only interactions with this partner: a gene symbol when querying a chemical, a "
        "chemical name/MeSH ID when querying a gene",
    )
    organism: str | None = Field(
        None, description="Keep only this organism (e.g. 'Homo sapiens', 'Rattus norvegicus', 'Mus musculus')"
    )
    action: str | None = Field(
        None,
        description="Keep only interactions whose action contains this text, e.g. 'increases^expression', "
        "'decreases^activity', 'binding', 'methylation'",
    )
    limit: int = Field(25, ge=1, le=100, description="Maximum number of interactions to return")
    cursor: str | None = Field(None, description="Offset from a previous result's next_cursor")


class ChemicalDiseaseParams(ToolParams):
    chemical: str = Field(description="Chemical name (arsenic), MeSH ID (D001151) or CAS RN (7440-38-2)")
    evidence: Evidence = Field(
        "direct",
        description="direct = curated marker/mechanism or therapeutic links; inferred = inferred via curated "
        "genes, ranked by inference score; both = combine",
    )
    disease: str | None = Field(None, description="Keep only diseases whose name or ID contains this text")
    limit: int = Field(25, ge=1, le=100, description="Maximum number of associations to return")
    cursor: str | None = Field(None, description="Offset from a previous result's next_cursor")


class GeneDiseaseParams(ToolParams):
    gene: str = Field(description="Gene symbol (TP53) or NCBI Gene ID (7157)")
    evidence: Evidence = Field(
        "direct",
        description="direct = curated marker/mechanism or therapeutic links; inferred = inferred via curated "
        "chemicals, ranked by inference score; both = combine",
    )
    disease: str | None = Field(None, description="Keep only diseases whose name or ID contains this text")
    limit: int = Field(25, ge=1, le=100, description="Maximum number of associations to return")
    cursor: str | None = Field(None, description="Offset from a previous result's next_cursor")


class DiseaseChemicalsParams(ToolParams):
    disease: str = Field(
        description="Disease name (asthma), MeSH ID (MESH:D001249 or D001249) or OMIM ID (OMIM:600807)"
    )
    evidence: Evidence = Field(
        "direct",
        description="direct = curated marker/mechanism or therapeutic chemicals; inferred = inferred via curated "
        "genes, ranked by inference score; both = combine",
    )
    direct_evidence: Literal["marker/mechanism", "therapeutic"] | None = Field(
        None, description="Only chemicals with this direct-evidence type (therapeutic = treatment links)"
    )
    limit: int = Field(25, ge=1, le=100, description="Maximum number of chemicals to return")
    cursor: str | None = Field(None, description="Offset from a previous result's next_cursor")


# ---------------------------------------------------------------- parsing helpers


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z]", "", key.lower())


def _row(raw: dict[str, Any]) -> dict[str, str]:
    """Row with case/punctuation-insensitive keys ('# Input' -> 'input', 'ChemicalID' -> 'chemicalid')."""
    return {_norm_key(str(k)): ("" if v is None else str(v).strip()) for k, v in raw.items()}


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in re.split(r"[|;]", value or "") if v.strip()]


def _mesh(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.upper().startswith("MESH:"):
        value = value[5:]
    return value if re.match(r"^[CD]\d{6,9}$", value) else None


def _omim(value: str | None) -> str | None:
    if value and value.upper().startswith("OMIM:"):
        return value[5:]
    return None


def _score(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _pmids(row: dict[str, str]) -> list[str]:
    return _split(row.get("pubmedids"))


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v not in (None, "", [], {})}


def _short_hash(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]


def _chem_url(chem_id: str) -> str:
    return f"{SITE}/detail.go?type=chem&acc={chem_id}"


def _gene_url(gene_id: str) -> str:
    return f"{SITE}/detail.go?type=gene&acc={gene_id}"


def _disease_url(disease_id: str) -> str:
    return f"{SITE}/detail.go?type=disease&acc={disease_id}"


def _interaction_record(row: dict[str, str]) -> Record:
    chem_id, gene_id = row.get("chemicalid", ""), row.get("geneid", "")
    pmids = _pmids(row)
    text = row.get("interaction") or f"{row.get('chemicalname')} interacts with {row.get('genesymbol')}"
    return Record(
        source="ctd",
        id=f"{chem_id}:{gene_id}:{_short_hash(text, row.get('organismid', ''))}",
        type="association",
        title=text,
        url=f"{_chem_url(chem_id)}&view=ixn" if chem_id else None,
        identifiers={
            "mesh": _mesh(chem_id),
            "cas": row.get("casrn"),
            "entrez_gene": gene_id,
            "gene_symbol": row.get("genesymbol"),
            "pmid": pmids[0] if pmids else None,
        },
        attributes=_clean(
            {
                "chemical": row.get("chemicalname"),
                "chemical_mesh": chem_id,
                "gene": row.get("genesymbol"),
                "gene_id": gene_id,
                "gene_forms": _split(row.get("geneforms")),
                "organism": row.get("organism"),
                "taxon_id": row.get("organismid"),
                "actions": _split(row.get("interactionactions")),
                "pmids": pmids[:20],
                "n_pmids": len(pmids),
            }
        ),
    )


def _disease_assoc_record(row: dict[str, str], subject: Literal["chemical", "gene"]) -> Record:
    disease_id = row.get("diseaseid", "")
    pmids = _pmids(row)
    omims = _split(row.get("omimids"))
    direct = row.get("directevidence") or None
    if subject == "chemical":
        left_id, left_name = row.get("chemicalid", ""), row.get("chemicalname")
        identifiers = {"cas": row.get("casrn")}
        left_attrs = {"chemical": left_name, "chemical_mesh": left_id}
        inferred_via = {"inference_gene": row.get("inferencegenesymbol")}
    else:
        left_id, left_name = row.get("geneid", ""), row.get("genesymbol")
        identifiers = {"entrez_gene": left_id, "gene_symbol": left_name}
        left_attrs = {"gene": left_name, "gene_id": left_id}
        inferred_via = {"inference_chemical": row.get("inferencechemicalname")}
    via_key, via_value = next(iter(inferred_via.items()))
    return Record(
        source="ctd",
        id=f"{left_id}:{disease_id}:{'direct' if direct else 'inferred'}:{_short_hash(via_value or '')}",
        type="association",
        title=f"{left_name} - {row.get('diseasename')}"
        + (f" ({direct})" if direct else f" (inferred via {via_value})" if via_value else " (inferred)"),
        url=_disease_url(disease_id) if disease_id else None,
        identifiers={
            **identifiers,
            "mesh": _mesh(disease_id),
            "omim": _omim(disease_id) or (omims[0] if omims else None),
            "pmid": pmids[0] if pmids else None,
        },
        attributes=_clean(
            {
                **left_attrs,
                "disease": row.get("diseasename"),
                "disease_id": disease_id,
                "direct_evidence": direct,
                via_key: _split(via_value)[:20] if via_value and "|" in via_value else via_value,
                "inference_score": _score(row.get("inferencescore")),
                "omim_ids": omims[:10],
                "pmids": pmids[:20],
                "n_pmids": len(pmids),
            }
        ),
    )


def _disease_chemical_record(row: dict[str, str]) -> Record:
    chem_id, disease_id = row.get("chemicalid", ""), row.get("diseaseid", "")
    pmids = _pmids(row)
    direct = row.get("directevidence") or None
    via = row.get("inferencegenesymbol") or None
    return Record(
        source="ctd",
        id=f"{chem_id}:{disease_id}:{'direct' if direct else 'inferred'}:{_short_hash(via or '')}",
        type="association",
        title=f"{row.get('chemicalname')} - {row.get('diseasename')}"
        + (f" ({direct})" if direct else f" (inferred via {via})" if via else " (inferred)"),
        url=_chem_url(chem_id) if chem_id else None,
        identifiers={
            "mesh": _mesh(chem_id),
            "cas": row.get("casrn"),
            "omim": _omim(disease_id),
            "pmid": pmids[0] if pmids else None,
        },
        attributes=_clean(
            {
                "chemical": row.get("chemicalname"),
                "chemical_mesh": chem_id,
                "disease": row.get("diseasename"),
                "disease_id": disease_id,
                "direct_evidence": direct,
                "inference_gene": via,
                "inference_score": _score(row.get("inferencescore")),
                "pmids": pmids[:20],
                "n_pmids": len(pmids),
            }
        ),
    )


def _parse_rows(text: str) -> list[dict[str, str]]:
    """Parse a batch-query body: JSON array/object, or TSV/CSV with a '#'-prefixed header line."""
    stripped = text.lstrip("﻿ \r\n\t")
    if stripped[:1] in ("[", "{"):
        payload = json.loads(stripped)
        if isinstance(payload, dict):
            payload = next((v for v in payload.values() if isinstance(v, list)), [payload])
        return [_row(r) for r in payload if isinstance(r, dict)]
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    header_idx = next((i for i, ln in enumerate(lines) if ln.startswith("#") and ("\t" in ln or "," in ln)), None)
    if header_idx is None:
        return []
    header_line = lines[header_idx].lstrip("#").strip()
    delimiter = "\t" if "\t" in header_line else ","
    reader = csv.reader(io.StringIO("\n".join([header_line] + [ln for ln in lines[header_idx + 1 :] if not ln.startswith("#")])), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        return []
    header = rows[0]
    return [_row(dict(zip(header, r))) for r in rows[1:] if r]


# ---------------------------------------------------------------- connector


class CTD(Connector):
    name = "ctd"
    title = "Comparative Toxicogenomics Database (CTD)"
    category = "biology"
    description = (
        "Literature-curated chemical-gene interactions, chemical-disease and gene-disease associations "
        "(direct evidence plus gene/chemical-inferred links with inference scores) across species from the "
        "Comparative Toxicogenomics Database. Best for environmental exposure / toxicology mechanisms."
    )
    homepage = "https://ctdbase.org/"
    docs_url = "https://ctdbase.org/help/batchQueryHelp.jsp"
    auth = "none"
    rate_limit = 1.0
    rate_burst = 1.0
    health_query = "bisphenol A"
    nih_repositories = ("Comparative Toxicogenomics Database (CTD)",)

    async def _batch(self, input_type: str, term: str, report: str) -> tuple[list[dict[str, str]], str]:
        """Run one batch query; returns (rows, request_url). Raises SourceError when CTD serves a CAPTCHA."""
        term = term.strip()
        if not term:
            raise SourceError(self.name, "empty input term")
        query = {"inputType": input_type, "inputTerms": term, "report": report, "format": "json"}
        response = await self.request(
            "GET",
            BATCH,
            params=query,
            cache_ttl=CACHE_TTL,
            timeout=120,
            allow_status=(301, 302, 303, 307, 403),
            validate=self._raise_if_blocked,  # a 200 CAPTCHA page must never be cached
        )
        self._raise_if_blocked(response)
        head = response.content[:4000].decode("utf-8", errors="replace")
        try:
            rows = _parse_rows(response.text)
        except (ValueError, csv.Error) as exc:
            raise SourceError(self.name, f"unparseable CTD batch response: {head[:150]!r}", url=response.url) from exc
        return rows, response.url

    def _raise_if_blocked(self, response: HttpResponse) -> None:
        """Raise when CTD answers with its bot-protection (ALTCHA CAPTCHA) page instead of data."""
        ctype = response.headers.get("content-type", "").lower()
        head = response.content[:4000].decode("utf-8", errors="replace")
        lowered = head.lower()
        if (
            response.status >= 300
            or "text/html" in ctype
            or lowered.lstrip().startswith(("<!doctype", "<html"))
            or "altcha" in lowered
            or "verify you are a human" in lowered
        ):
            status = response.status if response.status >= 300 else None
            raise SourceError(self.name, _BLOCK_HINT, status=status, retryable=True, url=response.url)

    async def _disease_reports(
        self, input_type: str, term: str, prefix: str, evidence: Evidence
    ) -> tuple[list[dict[str, str]], str]:
        reports = {"direct": [f"{prefix}_curated"], "inferred": [f"{prefix}_inferred"]}.get(
            evidence, [f"{prefix}_curated", f"{prefix}_inferred"]
        )
        rows: list[dict[str, str]] = []
        url = ""
        for report in reports:
            got, url_i = await self._batch(input_type, term, report)
            url = url or url_i
            if report.endswith("_curated"):
                got = [r for r in got if r.get("directevidence")] or got
            rows.extend(got)
        return rows, url

    @staticmethod
    def _paginate(rows: list[Any], cursor: str | None, limit: int) -> tuple[list[Any], str | None]:
        offset = parse_offset(cursor)
        page = rows[offset : offset + limit]
        nxt = offset + len(page)
        return page, (str(nxt) if page and nxt < len(rows) else None)

    @staticmethod
    def _rank(rows: list[dict[str, str]]) -> list[dict[str, str]]:
        """Direct evidence first (most PMIDs), then inferred by inference score."""
        return sorted(
            rows,
            key=lambda r: (
                0 if r.get("directevidence") else 1,
                -len(_pmids(r)) if r.get("directevidence") else -(_score(r.get("inferencescore")) or 0.0),
            ),
        )

    # ---- tools ----
    @tool(
        description="""
        CTD curated chemical-gene/protein interactions from the literature: for a chemical (name, MeSH ID or
        CAS RN) the genes it affects, or for a gene (symbol / NCBI Gene ID) the chemicals that affect it, with
        the interaction statement ('bisphenol A results in increased expression of ESR1 mRNA'), action codes
        (increases^expression, decreases^activity, affects^binding ...), organism and PubMed IDs. Results are
        ranked by number of supporting papers; filter with partner, organism ('Homo sapiens') and action.
        Use for toxicology / exposure mechanisms; for drug-target pharmacology prefer ChEMBL.
        """
    )
    async def chemical_gene_interactions(self, params: InteractionParams) -> ToolResult:
        if bool(params.chemical) == bool(params.gene):
            return ToolResult(source=self.name, tool="chemical_gene_interactions", notes=["give exactly one of chemical or gene"])
        input_type, term = ("chem", params.chemical) if params.chemical else ("gene", params.gene)
        rows, url = await self._batch(input_type, term or "", "cgixns")
        rows = [r for r in rows if r.get("interaction") or (r.get("chemicalid") and r.get("geneid"))]
        found = len(rows)
        if params.partner:
            p = params.partner.strip().lower()
            if input_type == "chem":
                rows = [r for r in rows if r.get("genesymbol", "").lower() == p or r.get("geneid") == p]
            else:
                rows = [r for r in rows if p in (r.get("chemicalname", "").lower(), r.get("chemicalid", "").lower())]
        if params.organism:
            o = params.organism.strip().lower()
            rows = [r for r in rows if r.get("organism", "").lower() == o or r.get("organismid") == o]
        if params.action:
            a = params.action.strip().lower()
            rows = [r for r in rows if a in r.get("interactionactions", "").lower()]
        rows.sort(key=lambda r: -len(_pmids(r)))
        page, nxt = self._paginate(rows, params.cursor, params.limit)
        notes = [] if found else [f"no CTD chemical-gene interactions for {term!r} (check the name / MeSH ID)"]
        if found and not rows:
            notes.append(f"{found} interactions found but none match the filters")
        return ToolResult(
            source=self.name,
            tool="chemical_gene_interactions",
            records=[_interaction_record(r) for r in page],
            total=len(rows),
            next_cursor=nxt,
            request_url=url,
            notes=notes,
        )

    @tool(
        description="""
        CTD chemical-disease associations for one chemical (name, MeSH ID or CAS RN): diseases with direct
        literature evidence ('marker/mechanism' or 'therapeutic', with PubMed IDs) and/or associations
        inferred through curated genes (inference gene, inference score). Disease IDs are MeSH or OMIM.
        evidence='direct' (default) | 'inferred' | 'both'. Example: chemical='arsenic', evidence='direct'.
        """
    )
    async def chemical_disease_associations(self, params: ChemicalDiseaseParams) -> ToolResult:
        rows, url = await self._disease_reports("chem", params.chemical, "diseases", params.evidence)
        rows = [r for r in rows if r.get("diseaseid") or r.get("diseasename")]
        if params.disease:
            d = params.disease.strip().lower()
            rows = [r for r in rows if d in r.get("diseasename", "").lower() or d in r.get("diseaseid", "").lower()]
        rows = self._rank(rows)
        page, nxt = self._paginate(rows, params.cursor, params.limit)
        return ToolResult(
            source=self.name,
            tool="chemical_disease_associations",
            records=[_disease_assoc_record(r, "chemical") for r in page],
            total=len(rows),
            next_cursor=nxt,
            request_url=url,
            notes=[] if rows else [f"no CTD disease associations for chemical {params.chemical!r} with these filters"],
        )

    @tool(
        description="""
        CTD gene-disease associations for one gene (symbol or NCBI Gene ID): diseases with direct curated
        evidence (marker/mechanism, therapeutic; PubMed IDs, OMIM IDs) and/or associations inferred via
        chemicals (inference chemical, inference score). evidence='direct' (default) | 'inferred' | 'both'.
        For human genetic association evidence prefer Open Targets / GWAS Catalog; CTD adds toxicogenomic context.
        """
    )
    async def gene_disease_associations(self, params: GeneDiseaseParams) -> ToolResult:
        rows, url = await self._disease_reports("gene", params.gene, "diseases", params.evidence)
        rows = [r for r in rows if r.get("diseaseid") or r.get("diseasename")]
        if params.disease:
            d = params.disease.strip().lower()
            rows = [r for r in rows if d in r.get("diseasename", "").lower() or d in r.get("diseaseid", "").lower()]
        rows = self._rank(rows)
        page, nxt = self._paginate(rows, params.cursor, params.limit)
        return ToolResult(
            source=self.name,
            tool="gene_disease_associations",
            records=[_disease_assoc_record(r, "gene") for r in page],
            total=len(rows),
            next_cursor=nxt,
            request_url=url,
            notes=[] if rows else [f"no CTD disease associations for gene {params.gene!r} with these filters"],
        )

    @tool(
        description="""
        Chemicals associated with a disease in CTD (disease name, MeSH ID like MESH:D001249, or OMIM ID):
        chemicals with direct evidence (marker/mechanism = exposure/biomarker, therapeutic = treatment) and/or
        inferred via curated genes with inference scores, each with CAS RN, MeSH ID and PubMed IDs.
        Example: disease='asthma', direct_evidence='marker/mechanism' for environmental exposures.
        """
    )
    async def disease_chemicals(self, params: DiseaseChemicalsParams) -> ToolResult:
        evidence: Evidence = "direct" if params.direct_evidence else params.evidence
        rows, url = await self._disease_reports("disease", params.disease, "chems", evidence)
        rows = [r for r in rows if r.get("chemicalid") or r.get("chemicalname")]
        if params.direct_evidence:
            rows = [r for r in rows if params.direct_evidence in _split(r.get("directevidence"))]
        rows = self._rank(rows)
        page, nxt = self._paginate(rows, params.cursor, params.limit)
        return ToolResult(
            source=self.name,
            tool="disease_chemicals",
            records=[_disease_chemical_record(r) for r in page],
            total=len(rows),
            next_cursor=nxt,
            request_url=url,
            notes=[] if rows else [f"no CTD chemicals for disease {params.disease!r} with these filters"],
        )

    async def healthcheck(self) -> HealthResult:
        """No federated search: probe chemical_gene_interactions for one chemical."""
        started = time.perf_counter()
        try:
            result = await self.call(
                "chemical_gene_interactions", {"chemical": self.health_query, "limit": 1}
            )
        except Exception as exc:  # report, never raise
            return HealthResult(
                source=self.name, ok=False, latency_ms=(time.perf_counter() - started) * 1000, detail=str(exc)[:300]
            )
        return HealthResult(
            source=self.name,
            ok=True,
            latency_ms=(time.perf_counter() - started) * 1000,
            detail=f"chemical_gene_interactions: {len(result.records)} record(s), total={result.total}",
        )
