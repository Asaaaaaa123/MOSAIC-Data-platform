"""Evidence per species: PubMed papers, ClinicalTrials.gov counts and ChEMBL measurements.

Pure helpers (query building, evidence classification, organism / assay-text -> species, assay ->
measurement kind, dose parsing) are separate from the async gatherers so they can be unit-tested.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Iterable

from ..models import Record
from .calls import SafeCaller
from .models import SPECIES_BY_KEY, EvidenceLevel, Measurement, MeasurementKind, Paper, SpeciesInfo

EVIDENCE_RANK: dict[str, int] = {"none": 0, "computational": 1, "in_vitro": 2, "in_vivo": 3, "clinical": 4}

# ---------------------------------------------------------------- organisms and species words
# Organism names as ChEMBL / UniProt / NCBI write them -> SpeciesInfo.key
_ORGANISMS: dict[str, str] = {
    "homo sapiens": "human",
    "macaca mulatta": "macaque", "macaca fascicularis": "macaque", "macaca nemestrina": "macaque", "macaca": "macaque",
    "mus musculus": "mouse", "mus": "mouse",
    "rattus norvegicus": "rat", "rattus rattus": "rat", "rattus": "rat",
    "canis lupus familiaris": "dog", "canis familiaris": "dog", "canis lupus": "dog",
    "sus scrofa": "pig", "sus scrofa domesticus": "pig",
    "gallus gallus": "chicken", "gallus gallus domesticus": "chicken",
    "xenopus tropicalis": "frog", "xenopus laevis": "frog", "xenopus": "frog",
    "danio rerio": "zebrafish",
    "drosophila melanogaster": "fly",
    "caenorhabditis elegans": "worm",
    "saccharomyces cerevisiae": "yeast", "saccharomyces cerevisiae s288c": "yeast",
    "arabidopsis thaliana": "arabidopsis",
}

# Words in assay descriptions / abstracts naming the animal (or plant) a study used.
_SPECIES_WORDS: list[tuple[str, re.Pattern[str]]] = [
    ("macaque", re.compile(r"\b(monkeys?|macaques?|cynomolgus|rhesus|macaca|primates?)\b", re.I)),
    ("mouse", re.compile(r"\b(mouse|mice|murine)\b", re.I)),
    ("rat", re.compile(r"\b(rats?|rattus)\b", re.I)),
    ("dog", re.compile(r"\b(dogs?|beagles?|canine)\b", re.I)),
    ("pig", re.compile(r"\b(pigs?|piglets?|minipigs?|swine|porcine)\b", re.I)),
    ("chicken", re.compile(r"\b(chickens?|chicks?|avian|birds?|hens?|broilers?)\b", re.I)),
    ("frog", re.compile(r"\b(xenopus|frogs?|tadpoles?)\b", re.I)),
    ("zebrafish", re.compile(r"\b(zebrafish|danio)\b", re.I)),
    ("fly", re.compile(r"\b(drosophila|fruit ?fl(?:y|ies))\b", re.I)),
    ("worm", re.compile(r"\b(c\.\s?elegans|caenorhabditis|nematodes?)\b", re.I)),
    ("yeast", re.compile(r"\b(yeasts?|saccharomyces|cerevisiae)\b", re.I)),
    ("arabidopsis", re.compile(r"\b(arabidopsis|plants?|seedlings?)\b", re.I)),
]
_HUMAN_WORDS = re.compile(r"\b(humans?|patients?|volunteers?|subjects|clinical)\b", re.I)

_IN_VIVO_TEXT = re.compile(
    r"\b(xenograft\w*|in vivo|tumou?r (growth|volume|regression|weight)|antitumou?r activity in|"
    r"efficacy in|survival time|life ?span|bearing|orthotopic|allograft\w*|p\.?o\.?|i\.?p\.?|i\.?v\.?|s\.?c\.?|oral(ly)? administ\w*|"
    r"administered|dosed|mg/kg)\b",
    re.I,
)
_EFFICACY_TEXT = re.compile(
    r"\b(xenograft\w*|tumou?r (growth|volume|regression|weight)|in vivo|orthotopic|allograft\w*|"
    r"tumou?r[- ]bearing|survival time|life ?span)\b",
    re.I,
)
# 'antitumor activity' / 'efficacy' count as in vivo only when an animal is named (else: cell assays)
_EFFICACY_WITH_ANIMAL = re.compile(r"\b(antitumou?r activity|anti-?tumou?r efficacy|efficacy|tumou?r)\b", re.I)
_IN_VITRO_ASSAY = re.compile(r"\b(in vitro|incubated|cell lines?|cells? growth|cells? viability)\b", re.I)
_TOX_TEXT = re.compile(r"\b(toxicity|cytotoxic\w*|ld50|mtd|maximum tolerated|lethal|hepatotox\w*|cardiotox\w*)\b", re.I)
_PK_TYPES = {
    "T1/2", "CMAX", "TMAX", "AUC", "AUC0-24", "AUCINF", "CL", "CLEARANCE", "VD", "VDSS", "F", "BIOAVAILABILITY",
    "MRT", "CLINT", "CL/F", "PPB", "FU", "CSS",
}
_TOX_TYPES = {"LD50", "MTD", "TD50", "NOAEL", "LOAEL", "TC50", "CC50"}
_DOSE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|to)?\s*(?:\d+(?:\.\d+)?\s*)?mg\s*/\s*kg", re.I)


def species_from_organism(organism: str | None) -> str | None:
    """'Mus musculus' -> 'mouse'; unknown or missing -> None."""
    if not organism:
        return None
    text = re.sub(r"\s+", " ", str(organism).strip().lower())
    if text in _ORGANISMS:
        return _ORGANISMS[text]
    words = text.split(" ")
    for n in (3, 2, 1):
        key = " ".join(words[:n])
        if key in _ORGANISMS:
            return _ORGANISMS[key]
    return None


def species_from_text(text: str | None) -> str | None:
    """First non-human species named in free text (assay description), else None."""
    if not text:
        return None
    hits = [(m.start(), key) for key, pattern in _SPECIES_WORDS if (m := pattern.search(text))]
    return min(hits)[1] if hits else None


def parse_dose_mg_kg(text: str | None) -> float | None:
    """'... at 10 mg/kg, po qd' -> 10.0 (first per-body-weight dose; '3 to 30 mg/kg' -> 3.0)."""
    if not text:
        return None
    match = _DOSE.search(text)
    return float(match.group(1)) if match else None


# ---------------------------------------------------------------- ChEMBL activities
def activity_species(attributes: dict[str, Any]) -> str | None:
    """Species a ChEMBL activity was measured in.

    The target organism is authoritative for binding / cell assays on a protein, but in vivo and
    ADME / toxicity assays (xenografts in mice, rat plasma ...) name the animal only in the assay
    description, often with the human cell line as the 'target'."""
    organism_key = species_from_organism(attributes.get("target_organism"))
    description = attributes.get("assay_description") or ""
    animal = species_from_text(description)
    assay_type = (attributes.get("assay_type") or "").upper()
    in_vivo = bool(_IN_VIVO_TEXT.search(description))
    if animal and (in_vivo or assay_type in ("A", "T") or organism_key is None):
        return animal
    if organism_key:
        return organism_key
    if _HUMAN_WORDS.search(description):
        return "human"
    return None


def activity_kind(attributes: dict[str, Any]) -> MeasurementKind:
    assay_type = (attributes.get("assay_type") or "").upper()
    description = attributes.get("assay_description") or ""
    standard_type = (attributes.get("standard_type") or "").upper()
    if assay_type != "A" and (
        _EFFICACY_TEXT.search(description)
        or (_EFFICACY_WITH_ANIMAL.search(description) and not _IN_VITRO_ASSAY.search(description)
            and species_from_text(description) not in (None, "arabidopsis", "yeast"))
    ):
        return "in_vivo_efficacy"
    if assay_type == "T" or standard_type in _TOX_TYPES:
        return "toxicity"
    if assay_type == "A":
        return "toxicity" if _TOX_TEXT.search(description) and standard_type not in _PK_TYPES else "pharmacokinetics"
    if standard_type in _PK_TYPES:
        return "pharmacokinetics"
    if assay_type in ("B", "F") and attributes.get("target_chembl_id"):
        return "potency"
    return "other"


def measurement_in_vivo(m: Measurement) -> bool:
    if m.kind == "in_vivo_efficacy":
        return True
    if m.kind in ("pharmacokinetics", "toxicity"):
        text = m.description or ""
        return bool(m.dose_mg_kg is not None or re.search(r"\b(plasma|oral(ly)?|bioavailability|p\.?o\.?|i\.?v\.?|in vivo|administ\w*)\b", text, re.I))
    return False


def measurement_from_activity(
    record: Record, target_symbols: dict[str, str], target_names: dict[str, str]
) -> Measurement | None:
    """ChEMBL activity record -> Measurement (None when the species cannot be determined)."""
    a = record.attributes
    species = activity_species(a)
    if species is None:
        return None
    kind = activity_kind(a)
    target = None
    if kind == "potency":
        target = target_symbols.get(a.get("target_chembl_id") or "") or target_names.get(
            (a.get("target_name") or "").strip().lower()
        ) or a.get("target_name")
    description = a.get("assay_description")
    return Measurement(
        species=species,
        kind=kind,
        target=target,
        standard_type=a.get("standard_type"),
        relation=a.get("standard_relation"),
        value=_float(a.get("standard_value")),
        units=a.get("standard_units"),
        pchembl=_float(a.get("pchembl_value")),
        dose_mg_kg=parse_dose_mg_kg(description),
        description=description,
        source="chembl",
        source_id=record.id,
        pmid=a.get("pmid"),
        doi=a.get("doi"),
        url=record.url,
    )


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


# activity pages: (assay_type, sort, pages of 100) -> about 500 rows per molecule
ACTIVITY_PLAN: list[tuple[str, str, int]] = [("B", "pchembl", 2), ("F", "none", 2), ("A", "none", 1), ("T", "none", 1)]


async def fetch_activities(caller: SafeCaller, chembl_id: str) -> list[Record]:
    async def plan(assay_type: str, sort: str, pages: int) -> list[Record]:
        out: list[Record] = []
        cursor = None
        for _ in range(pages):
            params: dict[str, Any] = {"molecule_chembl_id": chembl_id, "assay_type": assay_type, "sort": sort, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            result = await caller.call("chembl", "get_activities", params, label=f"chembl.get_activities:{assay_type}", timeout=90)
            if result is None:
                break
            out.extend(result.records)
            cursor = result.next_cursor
            if not cursor:
                break
        return out

    batches = await asyncio.gather(*(plan(*p) for p in ACTIVITY_PLAN))
    seen: set[str] = set()
    records: list[Record] = []
    for batch in batches:
        for record in batch:
            if record.id not in seen:
                seen.add(record.id)
                records.append(record)
    return records


# ---------------------------------------------------------------- papers
_CLINICAL_TYPES = {
    "randomized controlled trial", "controlled clinical trial", "pragmatic clinical trial", "clinical study",
    "case reports", "observational study", "equivalence trial", "adaptive clinical trial", "multicenter study",
}
_REVIEW_TYPES = {"review", "systematic review", "scoping review", "editorial", "comment", "news", "letter"}
_IN_VIVO_MESH = {
    "disease models, animal", "xenograft model antitumor assays", "heterografts", "transplantation, heterologous",
    "drug evaluation, preclinical", "neoplasm transplantation",
}
_IN_VITRO_MESH = {"cell line", "cell line, tumor", "in vitro techniques", "cells, cultured", "cell proliferation",
                  "cell survival", "apoptosis"}
_COMPUTATIONAL_MESH = {"molecular docking simulation", "molecular dynamics simulation", "computer simulation",
                       "computational biology", "quantitative structure-activity relationship"}
_IN_VIVO_PAPER_TEXT = re.compile(
    r"\b(xenograft\w*|in vivo|mice were|rats were|animals were|tumou?r[- ]bearing|orally administered|"
    r"intraperitoneal\w*|mg/kg|knockout mice|transgenic)\b", re.I)
_IN_VITRO_PAPER_TEXT = re.compile(r"\b(in vitro|cell lines?|cultured cells|cells were (treated|exposed|incubated))\b", re.I)
_COMPUTATIONAL_TEXT = re.compile(r"\b(molecular docking|in silico|molecular dynamics)\b", re.I)


def classify_paper(record: Record) -> EvidenceLevel:
    """Evidence level of a PubMed record from its publication types, MeSH headings and abstract."""
    attrs = record.attributes or {}
    pub_types = {str(t).lower() for t in attrs.get("publication_types") or []}
    mesh = {str(t).rstrip("*").lower() for t in attrs.get("mesh_terms") or []}
    text = " ".join(filter(None, [record.title, record.summary]))
    if any(t.startswith("clinical trial") for t in pub_types) or pub_types & _CLINICAL_TYPES:
        return "clinical"
    if pub_types & _REVIEW_TYPES and "meta-analysis" not in pub_types:
        return "none"
    if mesh & _IN_VIVO_MESH or any(m.startswith(("mice, ", "rats, ")) for m in mesh) or _IN_VIVO_PAPER_TEXT.search(text):
        return "in_vivo"
    if mesh & _IN_VITRO_MESH or any(m.endswith(" cells") for m in mesh) or _IN_VITRO_PAPER_TEXT.search(text):
        return "in_vitro"
    if mesh & _COMPUTATIONAL_MESH or _COMPUTATIONAL_TEXT.search(text):
        return "computational"
    return "none"


def paper_from_record(record: Record, species: str) -> Paper:
    attrs = record.attributes or {}
    text = " ".join(filter(None, [record.title, record.summary]))
    evidence = classify_paper(record)
    mesh = {str(t).rstrip("*").lower() for t in attrs.get("mesh_terms") or []}
    if species != "human" and evidence == "clinical" and "humans" in mesh:
        # a human trial that also reports data in this animal: for the animal it is in vivo evidence
        # (veterinary trials have no 'Humans' MeSH tag and stay clinical)
        evidence = "in_vivo"
    return Paper(
        species=species,
        pmid=record.identifiers.get("pmid") or (record.id if record.id.isdigit() else None),
        doi=record.identifiers.get("doi"),
        title=record.title or record.id,
        journal=attrs.get("journal_abbrev") or attrs.get("journal"),
        year=(record.date or "")[:4] or None,
        url=record.url,
        evidence=evidence,
        dose_mg_kg=parse_dose_mg_kg(text) if species != "human" else None,
    )


_SAFE_TERM = re.compile(r"^[\w\s\-+.']{3,40}$")


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def search_terms(name: str, synonyms: Iterable[str] = (), max_synonyms: int = 5) -> list[str]:
    """The compound name plus a few short, PubMed-safe synonyms.

    Synonyms must be 4-40 characters without commas / brackets and must not contain the name (a
    phrase search for the name already matches 'imatinib mesylate'). Names without digits (brand
    names: Gleevec, Lumakras) come before research codes (STI-571, AMG 510)."""
    base = re.sub(r"\s+", " ", str(name or "")).strip().strip('"')
    if base and not _SAFE_TERM.match(base):  # e.g. "4,4'-isopropylidenediphenol": drop PubMed syntax characters
        base = re.sub(r"\s+", " ", re.sub(r'["\[\]()]', " ", base)).strip()[:80]
    terms: list[str] = [base] if base else []
    lowered = {_key(t) for t in terms}
    candidates = []
    for value in synonyms:
        text = re.sub(r"\s+", " ", str(value or "")).strip().strip('"')
        key = _key(text)
        if len(text) < 4 or not _SAFE_TERM.match(text) or key in lowered or (base and _key(base) in key):
            continue
        lowered.add(key)
        candidates.append(text)
    # brand names first, then the shortest research codes (long codes are usually concatenation noise)
    candidates = [t for t in candidates if not re.search(r"\d", t) or len(t) <= 12]
    candidates.sort(key=lambda t: (bool(re.search(r"\d", t)), len(t) if re.search(r"\d", t) else 0))
    return terms + candidates[:max_synonyms]


def pubmed_query(terms: list[str], info: SpeciesInfo) -> str:
    clause = " OR ".join(f'"{t}"[tiab]' for t in terms)
    return f"({clause}) AND {info.pubmed_filter}"


RATE_LIMIT_PAUSE = 2.0


async def fetch_papers(
    caller: SafeCaller, terms: list[str], species: list[SpeciesInfo], max_papers: int
) -> dict[str, tuple[int | None, list[Record]]]:
    """Per species key: (PubMed hit count, top PubMed records); the count is None when the search failed.

    13 species searches start together, so NCBI can answer HTTP 429 (rate limit): such a search is
    retried once after a short pause before it is recorded as failed."""

    async def one(info: SpeciesInfo) -> tuple[str, int | None, list[Record]]:
        label = f"ncbi.pubmed_search:{info.key}"
        params = {"query": pubmed_query(terms, info), "limit": max(1, max_papers)}
        result = await caller.call("ncbi", "pubmed_search", params, label=label, timeout=90)
        if result is None and re.search(r"\b429\b|rate limit", caller.errors.get(label, ""), re.I):
            await asyncio.sleep(RATE_LIMIT_PAUSE)
            result = await caller.call("ncbi", "pubmed_search", params, label=label, timeout=90)
            if result is not None:
                caller.errors.pop(label, None)
        if result is None:
            return info.key, None, []
        records = result.records[:max_papers] if max_papers else []
        return info.key, int(result.total or 0), records

    out: dict[str, tuple[int | None, list[Record]]] = {}
    for key, total, records in await asyncio.gather(*(one(s) for s in species)):
        out[key] = (total, records)
    return out


async def count_trials(caller: SafeCaller, name: str) -> int | None:
    result = await caller.call("clinicaltrials", "count_studies", {"intervention": name, "group_by": "none"},
                               label="clinicaltrials.count_studies", timeout=60)
    if result is None:
        return None
    return int(result.total or 0)


def species_name(key: str) -> str:
    info = SPECIES_BY_KEY.get(key)
    return info.scientific_name if info else key
