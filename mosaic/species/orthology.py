"""Ortholog similarity of each human target protein in every species.

1. Ensembl Compara (connector ``ensembl``, tool ``orthologs``): one request per target for all
   Ensembl species; % identity / % positives are relative to the human protein (``source.perc_id``).
   Species still missing (fly, worm, yeast, Arabidopsis) are asked again with ``compara=pan_homology``.
2. Fallback (Ensembl failed or has no ortholog): find the ortholog protein and align it ourselves.
   - model organisms: Alliance of Genome Resources stringent orthologs (mouse Kras, fly Ras85D ...),
     then the UniProt protein of that gene symbol in that taxon;
   - macaque / dog / pig / chicken (and any species the Alliance lacks): UniProt protein with the
     human gene symbol in that taxon (reviewed first, then the length closest to the human protein);
   - Arabidopsis / distant species: members of the human protein's UniRef50 cluster in that taxon;
   - both canonical sequences from UniProt, global alignment (``align.align_proteins``).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from ..models import Record
from .align import MAX_PROTEIN_LENGTH, align_proteins
from .calls import SafeCaller
from .models import OrthologSimilarity, SpeciesInfo, TargetInfo

# Ensembl Genomes / pan-taxonomic Compara production names for species outside the vertebrate set
PAN_NAMES: dict[str, str] = {
    "fly": "drosophila_melanogaster",
    "worm": "caenorhabditis_elegans",
    "yeast": "saccharomyces_cerevisiae",
    "arabidopsis": "arabidopsis_thaliana",
}
# Alliance taxon -> species key (frog: X. tropicalis preferred over X. laevis)
ALLIANCE_TAXA: dict[int, str] = {
    10090: "mouse", 10116: "rat", 7955: "zebrafish", 8364: "frog", 8355: "frog", 7227: "fly", 6239: "worm",
    559292: "yeast", 4932: "yeast",
}
# Species where a UniProt gene-symbol search is pointless (unrelated gene nomenclature)
_NO_SYMBOL_SEARCH = {"arabidopsis"}
ENSEMBL_TIMEOUT = 75.0  # per call (connector: 45 s homology timeout, 1 retry); bounds a slow Ensembl


@dataclass
class TargetContext:
    info: TargetInfo
    sequence: str | None = None
    hgnc: str | None = None


@dataclass
class _Candidate:
    species: str
    accession: str
    symbol: str | None = None
    gene_id: str | None = None
    relationship: str | None = None
    sequence: str | None = None
    note: str | None = None
    url: str | None = None


@dataclass
class SimilarityResult:
    rows: list[OrthologSimilarity] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)
    methods: set[str] = field(default_factory=set)


def _species_by_ensembl(species: list[SpeciesInfo]) -> dict[str, str]:
    out = {s.ensembl_name: s.key for s in species if s.ensembl_name}
    out.update({v: k for k, v in PAN_NAMES.items() if k in {s.key for s in species}})
    return out


def best_ensembl_rows(records: list[Record], by_ensembl: dict[str, str]) -> dict[str, tuple[Record, int]]:
    """Best Ensembl ortholog per species key (highest identity to the human protein) and the count."""
    grouped: dict[str, list[Record]] = {}
    for record in records:
        key = by_ensembl.get(record.attributes.get("ensembl_species") or "")
        if key and record.attributes.get("perc_id") is not None:
            grouped.setdefault(key, []).append(record)
    return {
        key: (max(rows, key=lambda r: (r.attributes.get("perc_id") or 0, r.attributes.get("perc_pos") or 0)), len(rows))
        for key, rows in grouped.items()
    }


def pick_alliance(records: list[Record]) -> dict[str, Record]:
    """Best Alliance ortholog per species key: best score both ways, then most methods agreeing."""
    best: dict[str, tuple[tuple, Record]] = {}
    for record in records:
        a = record.attributes
        match = re.search(r"(\d+)$", str(a.get("taxon") or ""))
        key = ALLIANCE_TAXA.get(int(match.group(1))) if match else None
        if not key:
            continue
        rank = (
            a.get("best_score") == "Yes",
            a.get("best_reverse_score") == "Yes",
            int(a.get("methods_matched_count") or 0),
            str(a.get("taxon") or "").endswith(":8364"),
        )
        if key not in best or rank > best[key][0]:
            best[key] = (rank, record)
    return {k: v[1] for k, v in best.items()}


def pick_protein(records: list[Record], symbol: str | None, human_length: int | None, taxon_id: int | None) -> Record | None:
    """UniProt search hit most likely to be the ortholog: same taxon, exact gene symbol, reviewed,
    then the length closest to the human protein."""
    def taxon_ok(r: Record) -> bool:
        tid = r.attributes.get("taxon_id")
        return taxon_id is None or tid is None or int(tid) == int(taxon_id)

    pool = [r for r in records if taxon_ok(r)]
    if not pool:
        return None
    sym = (symbol or "").lower()

    def rank(r: Record) -> tuple:
        gene = str(r.attributes.get("gene") or r.identifiers.get("gene_symbol") or "").lower()
        length = int(r.attributes.get("length") or 0)
        # fragments (< 70% of the human length) lose even when reviewed: their identity would be meaningless
        fragment = bool(human_length and length < 0.7 * human_length)
        return (
            gene != sym if sym else False,
            fragment,
            not r.attributes.get("reviewed"),
            abs(length - int(human_length)) if human_length and length else 10**6,
        )

    return min(pool, key=rank)


def _human_row(target: TargetContext) -> OrthologSimilarity:
    t = target.info
    return OrthologSimilarity(
        target=t.gene_symbol, species="human", ortholog_symbol=t.gene_symbol, ortholog_id=t.ensembl_gene or t.uniprot,
        uniprot=t.uniprot, identity_pct=100.0, similarity_pct=100.0, coverage_pct=100.0, relationship="reference",
        method="none", note="human reference protein (100% by definition)", url=t.url,
    )


def _ensembl_row(target: TargetInfo, key: str, record: Record, n: int) -> OrthologSimilarity:
    a = record.attributes
    note = f"best of {n} Ensembl orthologs (highest identity)" if n > 1 else None
    if a.get("compara") == "pan_homology":
        note = "; ".join(filter(None, [note, "Ensembl pan-taxonomic Compara"]))
    return OrthologSimilarity(
        target=target.gene_symbol, species=key,
        ortholog_symbol=record.title if record.title != record.id else None,
        ortholog_id=record.id, identity_pct=a.get("perc_id"), similarity_pct=a.get("perc_pos"),
        coverage_pct=a.get("coverage_pct"), relationship=a.get("relationship"), method="ensembl_compara",
        note=note, url=record.url,
    )


async def _ensembl(caller: SafeCaller, target: TargetContext, species: list[SpeciesInfo]) -> dict[str, OrthologSimilarity]:
    t = target.info
    gene = t.ensembl_gene or t.gene_symbol
    by_ensembl = _species_by_ensembl(species)
    rows: dict[str, OrthologSimilarity] = {}
    vertebrate = [s.ensembl_name for s in species if s.ensembl_name and s.key != "human"]
    failed = False
    if vertebrate:
        result = await caller.call("ensembl", "orthologs", {"gene": gene, "target_species": vertebrate},
                                   label=f"ensembl.orthologs:{t.gene_symbol}", timeout=ENSEMBL_TIMEOUT)
        failed = result is None
        if result is not None:
            for key, (record, n) in best_ensembl_rows(result.records, by_ensembl).items():
                rows[key] = _ensembl_row(t, key, record, n)
    pan = [PAN_NAMES[s.key] for s in species if s.key in PAN_NAMES and s.key not in rows]
    # Ensembl just failed (5xx after retries or timeout): do not wait a second time on the same server
    if pan and not failed:
        result = await caller.call("ensembl", "orthologs", {"gene": gene, "target_species": pan, "compara": "pan_homology"},
                                   label=f"ensembl.orthologs_pan:{t.gene_symbol}", timeout=ENSEMBL_TIMEOUT)
        if result is not None:
            for key, (record, n) in best_ensembl_rows(result.records, by_ensembl).items():
                rows.setdefault(key, _ensembl_row(t, key, record, n))
    return rows


ALIGNMENT_MARGIN = 2.0  # identity points by which the UniProt alignment must beat Ensembl to replace it


async def _symbol_candidates(
    caller: SafeCaller, target: TargetContext, species: list[SpeciesInfo]
) -> dict[str, _Candidate]:
    """UniProt protein of the ortholog in each species: the Alliance stringent ortholog's symbol for model
    organisms, else the human gene symbol (best reviewed hit with the length closest to the human protein)."""
    t = target.info
    human_len = len(target.sequence) if target.sequence else None
    alliance: dict[str, Record] = {}
    if target.hgnc and any(s.key in ALLIANCE_TAXA.values() for s in species):
        result = await caller.call("alliance", "orthologs", {"gene_id": target.hgnc, "stringency": "stringent", "limit": 50},
                                   label=f"alliance.orthologs:{t.gene_symbol}", timeout=60)
        if result is not None:
            alliance = pick_alliance(result.records)

    async def find(info: SpeciesInfo) -> _Candidate | None:
        if info.key in _NO_SYMBOL_SEARCH:
            return None
        orth = alliance.get(info.key)
        if orth is None and info.key in ALLIANCE_TAXA.values() and alliance:
            return None  # the Alliance answered and has no stringent ortholog in this species
        symbol = orth.title if orth is not None else t.gene_symbol
        if not symbol:
            return None
        term = symbol if re.fullmatch(r"[A-Za-z0-9_.\-]+", symbol) else '"' + symbol.replace('"', "") + '"'
        result = await caller.call(
            "uniprot", "search_proteins", {"query": f"gene_exact:{term}", "organism_id": info.taxon_id, "limit": 10},
            label=f"uniprot.search_proteins:{t.gene_symbol}:{info.key}", timeout=60,
        )
        hit = pick_protein(result.records, symbol, human_len, info.taxon_id) if result is not None else None
        if hit is None:
            return None
        if orth is not None:
            note = f"Alliance stringent ortholog {orth.id}, UniProt {hit.id}"
        else:
            status = "reviewed" if hit.attributes.get("reviewed") else "unreviewed"
            note = f"UniProt {hit.id} ({status}) with the same gene symbol"
        return _Candidate(
            info.key, hit.id, symbol=str(hit.attributes.get("gene") or symbol),
            gene_id=orth.id if orth is not None else hit.id,
            relationship="alliance_stringent" if orth is not None else "same_gene_symbol",
            note=note, url=hit.url,
        )

    found = await asyncio.gather(*(find(s) for s in species))
    return {c.species: c for c in found if c is not None}


async def _uniref_candidates(caller: SafeCaller, target: TargetContext, species: list[SpeciesInfo]) -> dict[str, _Candidate]:
    """Distant species: the member of the human protein's UniRef50 cluster in that taxon."""
    t = target.info
    if not t.uniprot or not species:
        return {}
    human_len = len(target.sequence) if target.sequence else 0

    async def find(info: SpeciesInfo) -> _Candidate | None:
        result = await caller.call(
            "uniprot", "get_sequences", {"uniref50_of": t.uniprot, "taxon_id": info.taxon_id, "limit": 10},
            label=f"uniprot.get_sequences:uniref50:{t.gene_symbol}:{info.key}", timeout=60,
        )
        data = (result.data or {}) if result is not None else {}
        seqs = [s for s in data.get("sequences") or [] if s.get("sequence")]
        if not seqs:
            return None
        best = min(seqs, key=lambda s: (not s.get("reviewed"), abs((s.get("length") or 0) - human_len)))
        return _Candidate(
            info.key, best["accession"], symbol=best.get("gene"), gene_id=best["accession"], relationship="uniref50_member",
            sequence=best.get("sequence"),
            note=f"member of the human protein's UniRef50 cluster {data.get('uniref50_cluster')} (>= 50% identity)",
            url=f"https://www.uniprot.org/uniprotkb/{best['accession']}/entry",
        )

    found = await asyncio.gather(*(find(s) for s in species))
    return {c.species: c for c in found if c is not None}


async def _align_candidates(
    caller: SafeCaller, target: TargetContext, candidates: dict[str, _Candidate]
) -> dict[str, OrthologSimilarity]:
    """Fetch missing sequences in one UniProt call and align every candidate to the human protein."""
    t = target.info
    need = [c.accession for c in candidates.values() if not c.sequence]
    if not target.sequence and t.uniprot:
        need.append(t.uniprot)
    sequences: dict[str, str] = {}
    if need:
        result = await caller.call("uniprot", "get_sequences", {"accessions": list(dict.fromkeys(need))[:100]},
                                   label=f"uniprot.get_sequences:{t.gene_symbol}", timeout=60)
        items = ((result.data or {}).get("sequences") or []) if result is not None else []
        for item in items:
            if item.get("accession") and item.get("sequence"):
                sequences[item["accession"]] = item["sequence"]
    target.sequence = target.sequence or sequences.get(t.uniprot or "")
    human_seq = target.sequence

    async def align(c: _Candidate) -> OrthologSimilarity:
        other = c.sequence or sequences.get(c.accession)
        base = dict(target=t.gene_symbol, species=c.species, ortholog_symbol=c.symbol, ortholog_id=c.gene_id,
                    uniprot=c.accession, relationship=c.relationship, url=c.url)
        if not human_seq or not other:
            return OrthologSimilarity(**base, method="none", note=f"{c.note}; sequence could not be fetched")
        stats = await asyncio.to_thread(align_proteins, human_seq, other)
        if stats is None:
            return OrthologSimilarity(**base, method="none",
                                      note=f"{c.note}; not aligned (protein longer than {MAX_PROTEIN_LENGTH} aa)")
        return OrthologSimilarity(
            **base, identity_pct=stats.identity_pct, similarity_pct=stats.similarity_pct, coverage_pct=stats.coverage_pct,
            method="mosaic_alignment",
            note=(f"{c.note}; global BLOSUM62 alignment of UniProt canonical sequences, "
                  f"{stats.human_length} vs {stats.other_length} aa"),
        )

    rows = await asyncio.gather(*(align(c) for c in candidates.values()))
    return {r.species: r for r in rows}


def choose_row(ensembl: OrthologSimilarity | None, aligned: OrthologSimilarity | None) -> OrthologSimilarity | None:
    """Ensembl Compara by default. Its canonical proteins can be different isoforms (KRAS4A vs 4B) or
    partial gene models, so the alignment of the UniProt canonical sequences wins when it is clearly
    (> ALIGNMENT_MARGIN points) more identical; the other value is kept in the note."""
    usable = aligned if aligned is not None and aligned.identity_pct is not None else None
    if ensembl is None:
        return aligned
    if usable is None:
        return ensembl
    same_gene = (usable.ortholog_symbol or "").lower() == (ensembl.ortholog_symbol or "").lower()
    if usable.identity_pct > (ensembl.identity_pct or 0) + ALIGNMENT_MARGIN:
        note = (f"{usable.note}; Ensembl Compara {ensembl.ortholog_id} gave {ensembl.identity_pct}% for its canonical "
                "proteins (likely a different isoform or gene model)")
        return usable.model_copy(update={"note": note, "relationship": ensembl.relationship if same_gene else usable.relationship})
    extra = f"UniProt canonical-sequence alignment: {usable.identity_pct}% identity ({usable.uniprot})"
    return ensembl.model_copy(update={
        "note": "; ".join(filter(None, [ensembl.note, extra])),
        "uniprot": ensembl.uniprot or (usable.uniprot if same_gene else None),
    })


async def compute_similarity(
    caller: SafeCaller, targets: list[TargetContext], species: list[SpeciesInfo]
) -> SimilarityResult:
    """One OrthologSimilarity per target x species (human first), plus a Record per row for the package."""
    others = [s for s in species if s.key != "human"]

    async def one(target: TargetContext) -> list[OrthologSimilarity]:
        ensembl_rows, candidates = await asyncio.gather(
            _ensembl(caller, target, species), _symbol_candidates(caller, target, others)
        )
        missing = [s for s in others if s.key not in ensembl_rows and s.key not in candidates]
        candidates.update(await _uniref_candidates(caller, target, missing))
        aligned = await _align_candidates(caller, target, candidates)
        ordered = [_human_row(target)] if len(others) < len(species) else []
        for info in others:
            row = choose_row(ensembl_rows.get(info.key), aligned.get(info.key))
            ordered.append(row or OrthologSimilarity(
                target=target.info.gene_symbol, species=info.key, method="none",
                note="no ortholog found (Ensembl Compara, Alliance, UniProt gene symbol and UniRef50 searched)",
            ))
        return ordered

    out = SimilarityResult()
    for rows in await asyncio.gather(*(one(t) for t in targets)):
        out.rows.extend(rows)
    out.methods = {r.method for r in out.rows if r.method != "none"}
    by_key = {s.key: s for s in species}
    out.records = [similarity_record(r, by_key.get(r.species)) for r in out.rows
                   if r.species != "human" and r.method != "none"]
    return out


def similarity_record(row: OrthologSimilarity, info: SpeciesInfo | None) -> Record:
    """Package row for one ortholog (one per species file)."""
    ident: dict[str, Any] = {}
    oid = row.ortholog_id or ""
    if oid.upper().startswith("ENS"):
        ident["ensembl_gene"] = oid
    for prefix, key in (("MGI:", "mgi"), ("RGD:", "rgd"), ("ZFIN:", "zfin"), ("FB:", "flybase"), ("WB:", "wormbase"),
                        ("SGD:", "sgd"), ("XENBASE:", "xenbase")):
        if oid.upper().startswith(prefix):
            ident[key] = oid
    if row.uniprot:
        ident["uniprot"] = row.uniprot
    return Record(
        source="ensembl" if row.method == "ensembl_compara" else "uniprot",
        id=oid or row.uniprot or f"{row.target}:{row.species}",
        type="gene",
        title=f"{row.ortholog_symbol or oid} ({info.label if info else row.species} ortholog of human {row.target})",
        url=row.url,
        identifiers=ident,
        summary=(f"{row.identity_pct}% identical, {row.similarity_pct}% similar to human {row.target} "
                 f"({row.method}; {row.relationship or 'ortholog'})"),
        attributes={k: v for k, v in {
            "species": info.scientific_name if info else row.species,
            "human_target": row.target,
            "ortholog_symbol": row.ortholog_symbol,
            "identity_pct": row.identity_pct,
            "similarity_pct": row.similarity_pct,
            "coverage_pct": row.coverage_pct,
            "relationship": row.relationship,
            "method": row.method,
            "note": row.note,
        }.items() if v not in (None, "")},
    )
