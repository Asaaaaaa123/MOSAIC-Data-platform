"""Cross-species comparison engine: ``compare_species(registry, query, ...) -> SpeciesComparison``.

Steps (every data access goes through ``registry.call``; a failing source is recorded in
``errors`` / ``notes`` and never aborts the comparison):

1. compound: EntityResolver("drug") (PubChem, ChEMBL, RxNorm, DrugCentral, Open Targets) or ChEMBL
   directly for a ChEMBL ID; name, identifiers, max phase, kind (drug / compound).
2. human targets (max 5): ChEMBL curated mechanisms; for compounds without one, the human targets
   with the most potent (pChEMBL >= 6) ChEMBL activities. UniProt canonical sequences + gene symbols.
3. ortholog similarity per target x species (``orthology.compute_similarity``).
4. evidence per species: PubMed (hits + top papers), ClinicalTrials.gov (human), ChEMBL measurements.
5. optional paper extractor (dose / effect from abstracts), 6. per-species summaries, 7. records.
"""

from __future__ import annotations

import asyncio
import re
import statistics
from collections import Counter
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..models import Record
from .calls import SafeCaller
from .evidence import (
    EVIDENCE_RANK,
    count_trials,
    fetch_activities,
    fetch_papers,
    measurement_from_activity,
    measurement_in_vivo,
    paper_from_record,
    search_terms,
)
from .models import (
    DEFAULT_SPECIES,
    SPECIES,
    SPECIES_BY_KEY,
    CompoundInfo,
    EvidenceLevel,
    Measurement,
    OrthologSimilarity,
    Paper,
    PaperExtractor,
    SpeciesComparison,
    SpeciesInfo,
    SpeciesSummary,
    TargetInfo,
)
from .orthology import ALIGNMENT_MARGIN, TargetContext, compute_similarity

MAX_TARGETS = 5
_CHEMBL_ID = re.compile(r"^CHEMBL\d+$", re.I)
_INCHIKEY = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$", re.I)
_COMPOUND_ID_KEYS = ("chembl", "pubchem_cid", "inchikey", "unii", "drugbank", "rxcui", "drugcentral", "chebi", "cas", "atc")
_KIND_LABELS = {"potency": "potency", "in_vivo_efficacy": "in vivo efficacy", "pharmacokinetics": "pharmacokinetics",
                "toxicity": "toxicity", "other": "other"}


def resolve_species(species: list[str] | None) -> list[SpeciesInfo]:
    """Requested SpeciesInfo keys in SPECIES order, human always included and first."""
    wanted = {s.strip().lower() for s in (species or DEFAULT_SPECIES) if s and s.strip()}
    unknown = wanted - set(SPECIES_BY_KEY)
    if unknown:
        raise ValueError(f"unknown species: {', '.join(sorted(unknown))}; choose from {', '.join(DEFAULT_SPECIES)}")
    wanted.add("human")
    return [s for s in SPECIES if s.key in wanted]


# ---------------------------------------------------------------- 1. compound
def compound_kind(max_phase: float | None, *, in_chembl: bool, in_rxnorm: bool, in_drugcentral: bool, has_ids: bool) -> str:
    """drug: ChEMBL reports a clinical phase, or DrugCentral (approved drugs) knows it, or - only when ChEMBL
    does not know the molecule - RxNorm does. RxNorm alone is not enough when ChEMBL has the molecule without a
    phase: RxNorm also lists allergens, excipients and patch-test chemicals (e.g. bisphenol A)."""
    if max_phase is not None or in_drugcentral or (in_rxnorm and not in_chembl):
        return "drug"
    return "compound" if has_ids else "unknown"


async def _compound(caller: SafeCaller, query: str) -> tuple[CompoundInfo, Record | None, list[str], list[Record]]:
    """CompoundInfo, the ChEMBL molecule record (if any), synonyms, and the source records."""
    q = query.strip()
    identifiers: dict[str, str] = {}
    records: list[Record] = []
    molecule: Record | None = None
    in_rxnorm = in_drugcentral = False
    name: str | None = None
    url: str | None = None
    if _CHEMBL_ID.match(q):
        result = await caller.call("chembl", "get_molecule", {"identifier": q.upper()}, timeout=60)
        molecule = result.records[0] if result and result.records else None
    else:
        from ..crosslink.resolver import EntityResolver

        try:
            outcome = await EntityResolver(caller.registry, per_source_timeout=45).resolve("drug", q, session_id=caller.session_id)
        except Exception as exc:  # noqa: BLE001
            caller.errors["resolver"] = str(exc)[:300]
            outcome = None
        if outcome is not None:
            for source, message in outcome.errors.items():
                caller.errors[f"resolve:{source}"] = message
            identifiers.update({k: v for k, v in outcome.identifiers.items() if k in _COMPOUND_ID_KEYS})
            for cand in outcome.candidates:
                caller.sources_used.add(cand.source.split(":")[0])
                if cand.source == "rxnorm" and cand.exact_match:
                    in_rxnorm = True
                if cand.source == "drugcentral" and cand.exact_match:
                    in_drugcentral = True
            for record in outcome.records:
                if record.source == "chembl" and molecule is None:
                    molecule = record
                elif record.source == "pubchem" and not name:
                    name, url = record.title, record.url
                    records.append(record)
        if molecule is None and identifiers.get("chembl"):
            result = await caller.call("chembl", "get_molecule", {"identifier": identifiers["chembl"]}, timeout=60)
            molecule = result.records[0] if result and result.records else None
        elif molecule is None and (identifiers.get("inchikey") or _INCHIKEY.match(q)):
            result = await caller.call("chembl", "get_molecule", {"identifier": identifiers.get("inchikey") or q.upper()}, timeout=60)
            molecule = result.records[0] if result and result.records else None
    synonyms: list[str] = []
    max_phase = None
    if molecule is not None:
        records.insert(0, molecule)
        for key, value in molecule.identifiers.items():
            if key in _COMPOUND_ID_KEYS and value:
                identifiers.setdefault(key, value)
        identifiers["chembl"] = molecule.id
        name = molecule.title or name
        url = molecule.url
        synonyms = [str(s) for s in molecule.attributes.get("synonyms") or []]
        phase = molecule.attributes.get("max_phase")
        max_phase = float(phase) if phase is not None else None
    kind = compound_kind(max_phase, in_chembl=molecule is not None, in_rxnorm=in_rxnorm, in_drugcentral=in_drugcentral,
                         has_ids=bool(identifiers))
    info = CompoundInfo(query=query, name=name or q, kind=kind, max_phase=max_phase, identifiers=identifiers, url=url)
    return info, molecule, synonyms, records


# ---------------------------------------------------------------- 2. targets
async def _targets_from_mechanisms(caller: SafeCaller, chembl_id: str) -> list[tuple[str, dict[str, Any]]]:
    """[(uniprot accession, {chembl_target, action, source})] from curated mechanisms (human only)."""
    result = await caller.call("chembl", "get_mechanisms", {"molecule_chembl_id": chembl_id, "limit": 50}, timeout=60)
    if result is None:
        return []
    singles: list[tuple[str, dict[str, Any]]] = []
    others: list[tuple[str, dict[str, Any]]] = []
    chimeric: list[tuple[str, dict[str, Any]]] = []
    for record in result.records:
        a = record.attributes
        if a.get("target_organism") not in (None, "Homo sapiens"):
            continue
        meta = {"chembl_target": a.get("target_chembl_id"), "action": a.get("action_type"), "source": "chembl_mechanism"}
        accessions = a.get("target_uniprot") or []
        target_type = (a.get("target_type") or "").upper()
        if target_type == "SINGLE PROTEIN" or len(accessions) == 1:
            singles.extend((acc, meta) for acc in accessions)
        elif target_type == "CHIMERIC PROTEIN":
            chimeric.extend((acc, meta) for acc in accessions)
        else:  # complexes, families, selectivity groups: every component is a target
            others.extend((acc, meta) for acc in accessions)
    out: dict[str, dict[str, Any]] = {}
    for acc, meta in singles + others:
        out.setdefault(acc, meta)
    if not out:  # fusion proteins (BCR-ABL): the partner is only used when nothing else names a target
        for acc, meta in chimeric:
            out.setdefault(acc, meta)
    return list(out.items())


async def _targets_from_activities(
    caller: SafeCaller, activities: list[Record], chembl_id: str | None = None
) -> list[tuple[str, dict[str, Any]]]:
    """Human single-protein targets with the most pChEMBL >= 6 activities (non-drug chemicals)."""
    counts: Counter[str] = Counter()
    best: dict[str, float] = {}
    for record in activities:
        a = record.attributes
        p = a.get("pchembl_value")
        tid = a.get("target_chembl_id")
        if tid and a.get("target_organism") == "Homo sapiens" and p is not None and float(p) >= 6:
            counts[tid] += 1
            best[tid] = max(best.get(tid, 0.0), float(p))
    if not counts:
        molecule = chembl_id or (activities[0].attributes.get("molecule_chembl_id") if activities else None)
        result = await caller.call("chembl", "get_activities", {"molecule_chembl_id": molecule, "pchembl_min": 6, "limit": 100},
                                   label="chembl.get_activities:pchembl6", timeout=60) if molecule else None
        for record in result.records if result else []:
            a = record.attributes
            if a.get("target_organism") == "Homo sapiens" and a.get("target_chembl_id"):
                counts[a["target_chembl_id"]] += 1
                best[a["target_chembl_id"]] = max(best.get(a["target_chembl_id"], 0.0), float(a.get("pchembl_value") or 0))
    ranked = sorted(counts, key=lambda t: (-counts[t], -best.get(t, 0)))[: MAX_TARGETS * 2]
    results = await asyncio.gather(*(caller.call("chembl", "get_target", {"identifier": t}, label=f"chembl.get_target:{t}") for t in ranked))
    out: list[tuple[str, dict[str, Any]]] = []
    for tid, result in zip(ranked, results):
        record = result.records[0] if result and result.records else None
        if record is None or (record.attributes.get("target_type") or "").upper() != "SINGLE PROTEIN":
            continue
        acc = record.identifiers.get("uniprot")
        if acc and acc not in {a for a, _ in out}:
            out.append((acc, {"chembl_target": tid, "action": None, "source": "chembl_activity",
                              "n_activities": counts[tid], "best_pchembl": best.get(tid)}))
    return out


async def _target_details(caller: SafeCaller, picked: list[tuple[str, dict[str, Any]]]) -> tuple[list[TargetContext], list[Record]]:
    accessions = [acc for acc, _ in picked][:MAX_TARGETS]
    if not accessions:
        return [], []
    result = await caller.call("uniprot", "get_sequences", {"accessions": accessions}, label="uniprot.get_sequences:targets")
    proteins = {r.id: r for r in (result.records if result else [])}
    seqs = {s["accession"]: s.get("sequence") for s in ((result.data or {}).get("sequences") or [])} if result else {}
    contexts: list[TargetContext] = []
    records: list[Record] = []
    for acc, meta in picked[:MAX_TARGETS]:
        protein = proteins.get(acc)
        symbol = protein.identifiers.get("gene_symbol") if protein else None
        hgnc = protein.identifiers.get("hgnc") if protein else None
        ensembl = protein.identifiers.get("ensembl_gene") if protein else None
        if not symbol or not hgnc:
            gene = await caller.call("hgnc", "fetch_gene", {"value": symbol or acc}, label=f"hgnc.fetch_gene:{acc}")
            hit = gene.records[0] if gene and gene.records else None
            if hit is not None:
                symbol = symbol or hit.identifiers.get("gene_symbol") or hit.title
                hgnc = hgnc or hit.identifiers.get("hgnc")
                ensembl = ensembl or hit.identifiers.get("ensembl_gene")
        if not symbol:
            symbol = acc
        if any(c.info.gene_symbol == symbol for c in contexts):
            continue
        info = TargetInfo(
            gene_symbol=symbol, name=protein.title if protein else None, uniprot=acc, ensembl_gene=ensembl,
            chembl_target=meta.get("chembl_target"), action=meta.get("action"), source=meta["source"],
            url=f"https://www.uniprot.org/uniprotkb/{acc}/entry",
        )
        contexts.append(TargetContext(info=info, sequence=seqs.get(acc), hgnc=hgnc))
        if protein is not None:
            records.append(protein.model_copy(update={"attributes": {
                **protein.attributes, "species": "Homo sapiens", "role": f"human target ({meta['source']})",
                "action": meta.get("action"),
            }}))
    return contexts, records


# ---------------------------------------------------------------- 6. summaries
def _evidence_level(papers: list[Paper], measurements: list[Measurement], extra: EvidenceLevel = "none") -> EvidenceLevel:
    levels: list[str] = [extra] + [p.evidence for p in papers]
    for m in measurements:
        levels.append("in_vivo" if measurement_in_vivo(m) else "in_vitro")
    return max(levels, key=lambda lv: EVIDENCE_RANK.get(lv, 0))  # type: ignore[return-value]


def _fmt_count(n: int) -> str:
    return f"{n:,}"


def summarize(
    info: SpeciesInfo, targets: list[TargetInfo], similarity: list[OrthologSimilarity], n_papers: int,
    papers: list[Paper], measurements: list[Measurement], *, n_trials: int | None = None, max_phase: float | None = None,
    pubmed_failed: bool = False,
) -> SpeciesSummary:
    rows = {r.target: r for r in similarity if r.species == info.key}
    per_target = {t.gene_symbol: (rows[t.gene_symbol].identity_pct if t.gene_symbol in rows else None) for t in targets}
    values = [v for v in per_target.values() if v is not None]
    mean = round(sum(values) / len(values), 1) if values else None
    by_kind = Counter(m.kind for m in measurements)
    pchembl = [m.pchembl for m in measurements if m.pchembl is not None]
    median = round(statistics.median(pchembl), 2) if pchembl else None
    extra: EvidenceLevel = "clinical" if info.key == "human" and ((n_trials or 0) > 0 or (max_phase or 0) >= 1) else "none"
    level = _evidence_level(papers, measurements, extra)

    bits: list[str] = []
    if info.key == "human":
        bits.append("reference species (target identity 100%)" if targets else "reference species")
    elif values:
        parts = [f"{sym} {v:.1f}%" for sym, v in per_target.items() if v is not None]
        missing = [sym for sym, v in per_target.items() if v is None]
        bits.append("ortholog identity to human: " + ", ".join(parts) + (f" (no ortholog: {', '.join(missing)})" if missing else ""))
    elif targets:
        bits.append("no ortholog found for " + ", ".join(t.gene_symbol for t in targets))
    bits.append("PubMed search failed (paper count unknown)" if pubmed_failed else f"{_fmt_count(n_papers)} PubMed papers")
    if info.key == "human" and n_trials is not None:
        bits.append(f"{_fmt_count(n_trials)} ClinicalTrials.gov studies")
    if measurements:
        kinds = ", ".join(f"{n} {_KIND_LABELS[k]}" for k, n in by_kind.most_common())
        bits.append(f"{len(measurements)} ChEMBL measurements ({kinds})" + (f", median pChEMBL {median}" if median else ""))
    bits.append(f"evidence: {level.replace('_', ' ')}")
    return SpeciesSummary(
        species=info.key, label=info.label, label_zh=info.label_zh, group=info.group, scientific_name=info.scientific_name,
        mean_identity_pct=mean, per_target_identity=per_target, n_papers=n_papers, n_measurements=len(measurements),
        measurements_by_kind=dict(by_kind), median_pchembl=median, evidence_level=level,
        summary=f"{info.label}: " + "; ".join(bits) + ".",
    )


# ---------------------------------------------------------------- 7. records
def _paper_records(paper_hits: dict[str, tuple[int | None, list[Record]]]) -> list[Record]:
    """One record per PubMed article; ``species`` lists every species search that returned it."""
    by_id: dict[str, Record] = {}
    species: dict[str, list[str]] = {}
    for key, (_, records) in paper_hits.items():
        for record in records:
            rid = record.identifiers.get("pmid") or record.id
            by_id.setdefault(rid, record)
            name = SPECIES_BY_KEY[key].scientific_name
            if name not in species.setdefault(rid, []):
                species[rid].append(name)
    out = []
    for rid, record in by_id.items():
        names = species[rid]
        out.append(record.model_copy(update={"attributes": {**record.attributes, "species": names if len(names) > 1 else names[0]}}))
    return out


def _activity_record(record: Record, m: Measurement) -> Record:
    """Activity as a package row: identifiers dropped (they name the target / molecule, and would merge
    every activity of one target into a single row), species + measurement kind added. ``target_organism``
    is renamed so the package files the row under the species it was measured in only (a human cell line
    xenografted in mice is a mouse measurement)."""
    attributes = {k: v for k, v in record.attributes.items() if k != "target_organism"}
    if record.attributes.get("target_organism"):
        attributes["target_organism_name"] = record.attributes["target_organism"]
    return record.model_copy(update={
        "identifiers": {},
        "attributes": {**attributes, "species": SPECIES_BY_KEY[m.species].scientific_name,
                       "measurement_kind": m.kind, "human_target": m.target, "dose_mg_kg": m.dose_mg_kg},
    })


# ---------------------------------------------------------------- main
async def compare_species(
    registry: Any,
    query: str,
    *,
    species: list[str] | None = None,
    max_papers: int = 5,
    extractor: PaperExtractor | None = None,
    session_id: str | None = None,
) -> SpeciesComparison:
    infos = resolve_species(species)
    keys = {s.key for s in infos}
    caller = SafeCaller(registry, session_id)
    notes: list[str] = []

    compound, molecule, synonyms, compound_records = await _compound(caller, query)
    chembl_id = compound.identifiers.get("chembl")
    terms = search_terms(compound.name or query, synonyms)

    papers_task = asyncio.create_task(fetch_papers(caller, terms, infos, max_papers))
    trials_task = asyncio.create_task(count_trials(caller, compound.name or query)) if "human" in keys else None
    activities_task = asyncio.create_task(fetch_activities(caller, chembl_id)) if chembl_id else None

    picked: list[tuple[str, dict[str, Any]]] = []
    if chembl_id:
        picked = await _targets_from_mechanisms(caller, chembl_id)
        if not picked and activities_task is not None:
            picked = await _targets_from_activities(caller, await activities_task, chembl_id)
            if picked:
                notes.append("No curated mechanism of action: targets are the human proteins with the most potent "
                             "ChEMBL activities (pChEMBL >= 6), which may be off-targets or assay artefacts.")
    else:
        notes.append(f"{query!r} was not found in ChEMBL: no targets, ortholog similarity or ChEMBL measurements.")
    targets_ctx, target_records = await _target_details(caller, picked)
    if chembl_id and not targets_ctx:
        notes.append("No human protein target was found, so no ortholog similarity can be computed.")
    targets = [c.info for c in targets_ctx]

    similarity_result = await compute_similarity(caller, targets_ctx, infos)

    paper_hits = await papers_task
    n_trials = await trials_task if trials_task is not None else None
    activities = await activities_task if activities_task is not None else []

    # measurements
    symbols = {t.chembl_target: t.gene_symbol for t in targets if t.chembl_target}
    names = {(t.name or "").lower(): t.gene_symbol for t in targets if t.name}
    measurements: list[Measurement] = []
    activity_records: list[Record] = []
    skipped = 0
    for record in activities:
        m = measurement_from_activity(record, symbols, names)
        if m is None or m.species not in keys:
            skipped += 1
            continue
        measurements.append(m)
        activity_records.append(_activity_record(record, m))
    if skipped:
        notes.append(f"{skipped} of {len(activities)} ChEMBL activities were left out: species not stated in the assay "
                     "or not among the compared species.")

    # papers (+ optional extraction)
    papers: list[Paper] = []
    paper_source: list[tuple[Paper, Record]] = []
    for info in infos:
        _, records = paper_hits.get(info.key, (0, []))
        for record in records:
            paper = paper_from_record(record, info.key)
            papers.append(paper)
            paper_source.append((paper, record.model_copy(update={"attributes": {**record.attributes, "species": info.scientific_name}})))
    if extractor is not None:
        items = [(p, r) for p, r in paper_source if r.summary]
        if items:
            try:
                extracted = await extractor(items)
                if len(extracted) == len(items):
                    # the extractor fills model / dose / effect; species and references stay the engine's
                    replace = {id(p): e.model_copy(update={"species": p.species, "pmid": p.pmid, "doi": p.doi, "url": p.url})
                               for (p, _), e in zip(items, extracted)}
                    papers = [replace.get(id(p), p) for p in papers]
                else:
                    notes.append("paper extraction returned an unexpected number of papers; kept the unextracted list")
            except Exception as exc:  # noqa: BLE001
                caller.errors["paper_extractor"] = str(exc)[:300]

    # summaries
    summaries = []
    for info in infos:
        n_papers = paper_hits.get(info.key, (0, []))[0]
        summaries.append(summarize(
            info, targets, similarity_result.rows, n_papers or 0,
            [p for p in papers if p.species == info.key], [m for m in measurements if m.species == info.key],
            n_trials=n_trials if info.key == "human" else None, max_phase=compound.max_phase,
            pubmed_failed=n_papers is None,
        ))

    # methods / notes
    methods = {
        "compound": "Identifiers from the Mosaic entity resolver (PubChem, ChEMBL, RxNorm, DrugCentral, Open Targets); "
                    "max phase and synonyms from ChEMBL. kind = drug when ChEMBL reports a clinical phase, DrugCentral "
                    "knows the name, or (for molecules ChEMBL lacks) RxNorm does; otherwise compound.",
        "targets": "Human targets from ChEMBL curated mechanisms of action (chembl_mechanism); without one, the human "
                   "single-protein targets with the most pChEMBL >= 6 activities (chembl_activity). Max 5. Gene symbols "
                   "and canonical sequences from UniProt (HGNC when missing).",
        "similarity": "Similarity = protein sequence identity of each target's ortholog relative to the human protein. "
                      "ensembl_compara: Ensembl Compara orthologues (perc_id / perc_pos of the human protein, coverage from "
                      "the alignment CIGAR; best ortholog when one-to-many; pan-taxonomic Compara for fly / worm / yeast / "
                      "plant when the vertebrate set has none). mosaic_alignment: ortholog from the Alliance of Genome "
                      "Resources (stringent), a UniProt protein with the same gene symbol in that taxon (non-fragment, "
                      "reviewed first), or a member of the human protein's UniRef50 cluster; global Needleman-Wunsch "
                      "alignment of the UniProt canonical sequences (BLOSUM62, gap open -11, extend -1); identity = "
                      "identical residues / human length, similarity = BLOSUM62-positive pairs / human length, coverage = "
                      "aligned human residues / human length. Ensembl Compara is used unless it has no ortholog or the "
                      "UniProt alignment is more than 2 identity points higher (Ensembl compares each gene's canonical "
                      "protein, which can be a different isoform or a partial gene model); the other value is kept in the "
                      "row's note. Species value = mean over targets with an ortholog.",
        "papers": f"PubMed (NCBI E-utilities): ({' OR '.join(repr(t) for t in terms)} in title/abstract) AND the species' "
                  "MeSH filter; n_papers = total hits, top papers by PubMed relevance. Evidence level from publication "
                  "types, MeSH headings and the abstract (Clinical Trial -> clinical; animal models, xenografts, in vivo "
                  "-> in_vivo; cell lines, in vitro -> in_vitro).",
        "measurements": "ChEMBL activities of the molecule (binding/functional, ADME, toxicity; up to ~550 rows). Species "
                        "from the target organism, or from the assay description for in vivo / ADME / toxicity assays "
                        "(e.g. human cells xenografted in mice -> mouse). Kind: in vivo efficacy (xenograft, tumour growth), "
                        "pharmacokinetics (ADME assays), toxicity, potency (binding / functional on a target). dose_mg_kg "
                        "parsed from 'N mg/kg' in the description.",
        "evidence_level": "Highest of clinical > in_vivo > in_vitro > computational over the species' papers and "
                          "measurements; human is clinical when ClinicalTrials.gov lists studies or ChEMBL a clinical phase.",
        "similarity_rows": "; ".join(f"{m}: {n} rows" for m, n in Counter(
            "reference (human)" if r.species == "human" else r.method if r.identity_pct is not None else "no ortholog"
            for r in similarity_result.rows).most_common()) or "no targets",
        "sources": ", ".join(sorted(caller.sources_used)) or "none",
    }
    if n_trials is not None:
        notes.append(f"ClinicalTrials.gov lists {n_trials:,} studies with {compound.name or query} as an intervention.")
    notes.append("PubMed counts include reviews and papers that only mention the compound; MeSH species filters miss "
                 "recent, not yet indexed papers.")
    if any(r.method == "mosaic_alignment" for r in similarity_result.rows):
        notes.append("Rows with method mosaic_alignment were computed by Mosaic (global alignment of UniProt canonical "
                     "sequences) because Ensembl Compara had no ortholog for that species or its value was more than "
                     f"{ALIGNMENT_MARGIN:g} identity points lower (likely a different isoform or gene model).")
    if caller.errors:
        failed = sorted({k.split(":")[0] for k in caller.errors})
        notes.append(f"Some source calls failed and were skipped ({', '.join(failed)}); see errors.")

    records = compound_records + target_records + similarity_result.records + _paper_records(paper_hits) + activity_records
    return SpeciesComparison(
        comparison_id=uuid4().hex,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        query=query,
        compound=compound,
        targets=targets,
        species=summaries,
        similarity=similarity_result.rows,
        measurements=measurements,
        papers=papers,
        methods=methods,
        notes=notes,
        errors=dict(caller.errors),
        records=records,
    )
