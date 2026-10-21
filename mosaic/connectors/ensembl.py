"""Ensembl REST API connector (genes and Compara orthologues with % identity).

Docs: https://rest.ensembl.org/ (lookup: /lookup/symbol/:species/:symbol, /lookup/id/:id, POST
/lookup/id; homology: /homology/id/:species/:id). Auth: none. Ensembl allows ~15 requests/s per
client; we stay at 3/s because the homology endpoint is slow (seconds to tens of seconds for many
target species) and intermittently answers HTTP 500/503 or times out.

Quirks:
- ``/homology/symbol/...`` is much slower than ``/homology/id/...`` (often > 60 s), so symbols are
  first resolved to the stable gene id with ``/lookup/symbol`` (fast) and homology is asked by id.
- ``target_species`` is repeated once per species in ONE request; ``sequence=none`` keeps the
  payload small. Homology rows carry ``perc_id`` / ``perc_pos`` for BOTH proteins:
  ``source.perc_id`` = % of the *query* (e.g. human) protein's residues that are identical in the
  alignment, ``target.perc_id`` = the same relative to the ortholog. The CIGAR lines of both proteins
  describe the alignment (M = residue, D = gap) and give the aligned coverage.
- Homology rows hold no gene symbols; they are filled with one ``POST /lookup/id`` request.
- ``compara=pan_homology`` (Ensembl Genomes pan-taxonomic Compara) covers distant species missing from
  the default vertebrate Compara (e.g. Arabidopsis thaliana); species names are the same
  production names (``arabidopsis_thaliana``).
- Unknown ids/symbols answer HTTP 400 (``{"error": "No valid lookup found ..."}``) or 404.

Identifier keys: ``ensembl_gene`` (ENS...G ids; non-ENS stable ids such as yeast ``YNL098C`` are not
Ensembl-style gene ids and stay in ``id`` / attributes), ``gene_symbol`` and ``hgnc`` for human genes.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import quote

from pydantic import Field

from ..http import SourceError
from ..models import HealthResult, Record, ToolResult
from .base import Connector, ToolParams, tool

API = "https://rest.ensembl.org"
_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
_CACHE_TTL = 30 * 24 * 3600  # Ensembl releases are quarterly; orthology rarely changes
_HOMOLOGY_TIMEOUT = 45.0

# Stable ids: ENSG00000133703, ENSMUSG00000030265, FBgn0003205, WBGene00002335, YNL098C, AT1G01010
_STABLE_ID = re.compile(
    r"^(ENS[A-Z]*G\d{6,}|FBgn\d{7}|WBGene\d{8}|Y[A-P][LR]\d{3}[WC](-[A-Z])?|AT[1-5CM]G\d{5})(\.\d+)?$",
    re.IGNORECASE,
)
_HGNC_ACC = re.compile(r"Acc:(HGNC:\d+)")
_SOURCE_TAG = re.compile(r"\s*\[Source:[^\]]*\]\s*$")

# Ensembl Genomes divisions host these species' gene pages (others: www.ensembl.org)
_SITES = {
    "arabidopsis_thaliana": "https://plants.ensembl.org",
}


def species_label(name: str | None) -> str | None:
    """``mus_musculus`` -> ``Mus musculus``; ``canis_lupus_familiaris`` -> ``Canis lupus familiaris``."""
    if not name:
        return None
    text = name.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def gene_url(species: str | None, gene_id: str) -> str:
    site = _SITES.get(species or "", "https://www.ensembl.org")
    sp = (species or "homo_sapiens")
    return f"{site}/{sp[:1].upper() + sp[1:]}/Gene/Summary?g={quote(gene_id, safe='')}"


def cigar_columns(cigar: str | None) -> str:
    """Expand an Ensembl protein CIGAR line (``7D120MD46M``) into one M/D character per column."""
    out: list[str] = []
    for count, op in re.findall(r"(\d*)([MDI])", cigar or ""):
        out.append(("M" if op == "M" else "D") * (int(count) if count else 1))
    return "".join(out)


def alignment_coverage(source_cigar: str | None, target_cigar: str | None) -> float | None:
    """% of the source protein's residues aligned to a residue of the target (not to a gap)."""
    src, tgt = cigar_columns(source_cigar), cigar_columns(target_cigar)
    if not src or len(src) != len(tgt):
        return None
    residues = src.count("M")
    if not residues:
        return None
    both = sum(1 for a, b in zip(src, tgt) if a == "M" and b == "M")
    return round(both / residues * 100, 2)


def _num(value: Any) -> float | None:
    try:
        return round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


class GetGeneParams(ToolParams):
    gene: str = Field(description="Gene symbol (KRAS) or Ensembl stable gene id (ENSG00000133703, ENSMUSG00000030265)")
    species: str = Field(
        "homo_sapiens",
        description="Ensembl species production name used to resolve a symbol, e.g. homo_sapiens, mus_musculus, danio_rerio",
    )


class OrthologsParams(ToolParams):
    gene: str = Field(description="Gene symbol of the query species (KRAS) or its Ensembl gene id (ENSG00000133703)")
    species: str = Field("homo_sapiens", description="Ensembl production name of the query gene's species")
    target_species: list[str] = Field(
        default_factory=list,
        max_length=30,
        description="Ensembl production names to restrict to, e.g. ['mus_musculus', 'rattus_norvegicus', 'danio_rerio']; "
        "empty = every species in the Compara database",
    )
    compara: Literal["vertebrates", "pan_homology"] = Field(
        "vertebrates",
        description="vertebrates = default Ensembl Compara (vertebrates plus fly, worm, yeast outgroups); "
        "pan_homology = Ensembl Genomes pan-taxonomic Compara (adds plants such as arabidopsis_thaliana)",
    )
    with_symbols: bool = Field(True, description="Look up the orthologous genes' symbols (one extra request)")


class Ensembl(Connector):
    name = "ensembl"
    title = "Ensembl"
    category = "genomics"
    description = (
        "Ensembl genome annotation for vertebrates and model organisms: gene lookup (stable ids, location, "
        "biotype) and Compara orthologues between species with the % sequence identity / similarity of the "
        "aligned proteins. Best for 'how conserved is gene X in mouse / zebrafish / chicken ...'."
    )
    homepage = "https://www.ensembl.org/"
    docs_url = "https://rest.ensembl.org/"
    auth = "none"
    rate_limit = 3.0
    health_query = "KRAS"

    async def _get(self, path: str, params: Any = None, *, timeout: float = 30.0, retries: int = 2):
        return await self.request(
            "GET", f"{API}{path}", params=params, headers=_HEADERS, allow_status=(400, 404),
            cache_ttl=_CACHE_TTL, timeout=timeout, retries=retries,
        )

    async def _lookup(self, gene: str, species: str) -> tuple[dict[str, Any] | None, str]:
        ident = gene.strip()
        if _STABLE_ID.match(ident):
            response = await self._get(f"/lookup/id/{quote(ident.split('.')[0], safe='')}")
        else:
            response = await self._get(f"/lookup/symbol/{quote(species, safe='')}/{quote(ident, safe='')}")
        if response.status in (400, 404):
            return None, response.url
        return response.json(), response.url

    async def _symbols(self, ids: list[str]) -> dict[str, str]:
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return {}
        response = await self.request(
            "POST", f"{API}/lookup/id", json_body={"ids": ids[:1000]}, headers=_HEADERS,
            cache_ttl=_CACHE_TTL, timeout=30.0, retries=1,
        )
        payload = response.json() or {}
        return {k: v.get("display_name") for k, v in payload.items() if isinstance(v, dict) and v.get("display_name")}

    def _gene_record(self, gene: dict[str, Any]) -> Record:
        gid = gene.get("id") or ""
        species = gene.get("species")
        description = gene.get("description") or ""
        hgnc = _HGNC_ACC.search(description)
        human = species == "homo_sapiens"
        location = None
        if gene.get("seq_region_name") and gene.get("start"):
            location = f"{gene['seq_region_name']}:{gene['start']}-{gene.get('end')}:{gene.get('strand')}"
        return Record(
            source=self.name,
            id=gid,
            type="gene",
            title=gene.get("display_name") or gid,
            url=gene_url(species, gid) if gid else None,
            identifiers={
                "ensembl_gene": gid,
                "gene_symbol": gene.get("display_name") if human else None,
                "hgnc": hgnc.group(1) if hgnc and human else None,
            },
            summary=_SOURCE_TAG.sub("", description) or None,
            attributes={
                k: v
                for k, v in {
                    "species": species_label(species),
                    "ensembl_species": species,
                    "biotype": gene.get("biotype"),
                    "location": location,
                    "assembly": gene.get("assembly_name"),
                    "canonical_transcript": gene.get("canonical_transcript"),
                    "version": gene.get("version"),
                }.items()
                if v not in (None, "")
            },
        )

    @tool(
        description="""
        Look up one gene in Ensembl by symbol (KRAS; species=homo_sapiens by default, or mus_musculus,
        danio_rerio ...) or by stable id (ENSG00000133703). Returns the Ensembl gene id, chromosome
        location, biotype, description and HGNC id (human). Use it to get the Ensembl id other tools need.
        """
    )
    async def get_gene(self, params: GetGeneParams) -> ToolResult:
        gene, url = await self._lookup(params.gene, params.species)
        if not gene:
            return ToolResult(source=self.name, tool="get_gene", request_url=url,
                              notes=[f"no Ensembl gene found for {params.gene!r} in {params.species}"])
        return ToolResult(source=self.name, tool="get_gene", records=[self._gene_record(gene)], total=1, request_url=url)

    @tool(
        description="""
        Ensembl Compara orthologues of one gene (default: a human gene) in other species, with the %
        identity and % positives (similar residues) of the aligned proteins and the orthology type
        (ortholog_one2one, ortholog_one2many ...). Give target_species as Ensembl production names
        (mus_musculus, rattus_norvegicus, macaca_mulatta, canis_lupus_familiaris, sus_scrofa,
        gallus_gallus, xenopus_tropicalis, danio_rerio, drosophila_melanogaster, caenorhabditis_elegans,
        saccharomyces_cerevisiae) in one call. perc_id / perc_pos are relative to the query protein;
        target_perc_id / target_perc_pos relative to the ortholog; coverage_pct = query residues aligned.
        compara=pan_homology adds distant species such as arabidopsis_thaliana. Slow upstream (5-30 s).
        """
    )
    async def orthologs(self, params: OrthologsParams) -> ToolResult:
        notes: list[str] = []
        ident = params.gene.strip()
        gene_id, symbol = ident.split(".")[0], None
        if not _STABLE_ID.match(ident):
            gene, url = await self._lookup(ident, params.species)
            if not gene:
                return ToolResult(source=self.name, tool="orthologs", request_url=url,
                                  notes=[f"no Ensembl gene found for {ident!r} in {params.species}"])
            gene_id, symbol = gene.get("id"), gene.get("display_name")
        query: list[tuple[str, str]] = [("type", "orthologues"), ("sequence", "none")]
        if params.compara == "pan_homology":
            query.append(("compara", "pan_homology"))
        query.extend(("target_species", s) for s in dict.fromkeys(params.target_species) if s)
        response = await self._get(
            f"/homology/id/{quote(params.species, safe='')}/{quote(gene_id, safe='')}",
            query, timeout=_HOMOLOGY_TIMEOUT, retries=1,
        )
        if response.status in (400, 404):
            detail = ""
            try:
                detail = str((response.json() or {}).get("error") or "")[:200]
            except ValueError:
                pass
            return ToolResult(source=self.name, tool="orthologs", request_url=response.url,
                              notes=[f"Ensembl has no homology data for {gene_id}" + (f": {detail}" if detail else "")])
        homologies = [h for block in (response.json() or {}).get("data") or [] for h in block.get("homologies") or []]
        symbols: dict[str, str] = {}
        if params.with_symbols and homologies:
            try:
                symbols = await self._symbols([(h.get("target") or {}).get("id") for h in homologies])
            except (SourceError, ValueError) as exc:
                notes.append(f"ortholog symbol lookup failed: {str(exc)[:150]}")
        records, rows = [], []
        for h in homologies:
            src, tgt = h.get("source") or {}, h.get("target") or {}
            tid = tgt.get("id")
            if not tid:
                continue
            tsym = symbols.get(tid)
            attrs = {
                "species": species_label(tgt.get("species")),
                "ensembl_species": tgt.get("species"),
                "taxon_id": tgt.get("taxon_id"),
                "ortholog_of": src.get("id") or gene_id,
                "ortholog_of_symbol": symbol,
                "relationship": h.get("type"),
                "perc_id": _num(src.get("perc_id")),
                "perc_pos": _num(src.get("perc_pos")),
                "target_perc_id": _num(tgt.get("perc_id")),
                "target_perc_pos": _num(tgt.get("perc_pos")),
                "coverage_pct": alignment_coverage(src.get("cigar_line"), tgt.get("cigar_line")),
                "protein_id": tgt.get("protein_id"),
                "query_protein_id": src.get("protein_id"),
                "taxonomy_level": h.get("taxonomy_level"),
                "dn_ds": h.get("dn_ds"),
                "compara": params.compara,
            }
            attrs = {k: v for k, v in attrs.items() if v not in (None, "")}
            records.append(
                Record(
                    source=self.name,
                    id=tid,
                    type="gene",
                    title=tsym or tid,
                    url=gene_url(tgt.get("species"), tid),
                    identifiers={"ensembl_gene": tid} if tid.upper().startswith("ENS") else {},
                    summary=(
                        f"{h.get('type')} of {symbol or gene_id} in {species_label(tgt.get('species'))}: "
                        f"{attrs.get('perc_id')}% identical, {attrs.get('perc_pos')}% similar (relative to the query protein)"
                    ),
                    attributes=attrs,
                )
            )
            rows.append({k: attrs.get(k) for k in ("ensembl_species", "relationship", "perc_id", "perc_pos", "coverage_pct")}
                        | {"gene_id": tid, "symbol": tsym})
        if not records:
            notes.append(f"no orthologues of {symbol or gene_id} in the requested species ({params.compara} Compara)")
        return ToolResult(
            source=self.name,
            tool="orthologs",
            records=records,
            total=len(records),
            data={"query_gene": gene_id, "query_symbol": symbol, "orthologs": rows},
            notes=notes,
            request_url=response.url,
        )

    async def healthcheck(self) -> HealthResult:
        import time

        started = time.perf_counter()
        try:
            result = await self.call("get_gene", {"gene": self.health_query})
        except Exception as exc:  # report, never raise
            return HealthResult(source=self.name, ok=False, latency_ms=(time.perf_counter() - started) * 1000,
                                detail=str(exc)[:300])
        return HealthResult(source=self.name, ok=bool(result.records), latency_ms=(time.perf_counter() - started) * 1000,
                            detail=f"get_gene: {len(result.records)} record(s)")
