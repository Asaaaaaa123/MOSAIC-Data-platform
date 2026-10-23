"""Cross-species comparison: the shared data model (contract between engine, API, agent and UI).

``compare_species(registry, query, ...)`` (``mosaic/species/compare.py``) returns a
``SpeciesComparison``: for a drug or chemical compound, its human targets, how similar each target's
ortholog is in every species (sequence identity / similarity %), and the evidence per species
(papers, bioactivity / pharmacokinetic / in vivo measurements), all with references. The web
dashboard and the chat card render this JSON; ``records`` feed a by-species data package.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field

from ..models import Record

SpeciesGroup = Literal["human", "primate", "mammal", "bird", "amphibian", "fish", "insect", "nematode", "fungus", "plant"]


class SpeciesInfo(BaseModel):
    key: str
    label: str
    label_zh: str = ""  # kept so older saved comparisons still load
    scientific_name: str
    taxon_id: int
    group: SpeciesGroup
    ensembl_name: str | None = None  # Ensembl Compara species name (None: not in Ensembl vertebrates/model set)
    pubmed_filter: str  # PubMed query clause selecting studies in this species


SPECIES: list[SpeciesInfo] = [
    SpeciesInfo(key="human", label="Human", scientific_name="Homo sapiens", taxon_id=9606, group="human",
                ensembl_name="homo_sapiens", pubmed_filter="humans[mh]"),
    SpeciesInfo(key="macaque", label="Macaque", scientific_name="Macaca mulatta", taxon_id=9544, group="primate",
                ensembl_name="macaca_mulatta", pubmed_filter="(macaca[mh] OR macaque[tiab] OR monkey[tiab])"),
    SpeciesInfo(key="mouse", label="Mouse", scientific_name="Mus musculus", taxon_id=10090, group="mammal",
                ensembl_name="mus_musculus", pubmed_filter="mice[mh]"),
    SpeciesInfo(key="rat", label="Rat", scientific_name="Rattus norvegicus", taxon_id=10116, group="mammal",
                ensembl_name="rattus_norvegicus", pubmed_filter="rats[mh]"),
    SpeciesInfo(key="dog", label="Dog", scientific_name="Canis lupus familiaris", taxon_id=9615, group="mammal",
                ensembl_name="canis_lupus_familiaris", pubmed_filter="dogs[mh]"),
    SpeciesInfo(key="pig", label="Pig", scientific_name="Sus scrofa", taxon_id=9823, group="mammal",
                ensembl_name="sus_scrofa", pubmed_filter="swine[mh]"),
    SpeciesInfo(key="chicken", label="Chicken", scientific_name="Gallus gallus", taxon_id=9031, group="bird",
                ensembl_name="gallus_gallus", pubmed_filter="(chickens[mh] OR birds[mh])"),
    SpeciesInfo(key="frog", label="Frog (Xenopus)", scientific_name="Xenopus tropicalis", taxon_id=8364,
                group="amphibian", ensembl_name="xenopus_tropicalis", pubmed_filter="xenopus[mh]"),
    SpeciesInfo(key="zebrafish", label="Zebrafish", scientific_name="Danio rerio", taxon_id=7955, group="fish",
                ensembl_name="danio_rerio", pubmed_filter="zebrafish[mh]"),
    SpeciesInfo(key="fly", label="Fruit fly", scientific_name="Drosophila melanogaster", taxon_id=7227,
                group="insect", ensembl_name="drosophila_melanogaster", pubmed_filter="drosophila[mh]"),
    SpeciesInfo(key="worm", label="Worm (C. elegans)", scientific_name="Caenorhabditis elegans", taxon_id=6239,
                group="nematode", ensembl_name="caenorhabditis_elegans", pubmed_filter="caenorhabditis elegans[mh]"),
    SpeciesInfo(key="yeast", label="Yeast", scientific_name="Saccharomyces cerevisiae", taxon_id=559292,
                group="fungus", ensembl_name="saccharomyces_cerevisiae", pubmed_filter="saccharomyces cerevisiae[mh]"),
    SpeciesInfo(key="arabidopsis", label="Plant (Arabidopsis)", scientific_name="Arabidopsis thaliana",
                taxon_id=3702, group="plant", ensembl_name=None, pubmed_filter="(arabidopsis[mh] OR plants[mh])"),
]
SPECIES_BY_KEY: dict[str, SpeciesInfo] = {s.key: s for s in SPECIES}
DEFAULT_SPECIES = [s.key for s in SPECIES]

EvidenceLevel = Literal["clinical", "in_vivo", "in_vitro", "computational", "none"]
MeasurementKind = Literal["potency", "in_vivo_efficacy", "pharmacokinetics", "toxicity", "other"]
SimilarityMethod = Literal["ensembl_compara", "mosaic_alignment", "none"]


class CompoundInfo(BaseModel):
    query: str
    name: str | None = None
    kind: Literal["drug", "compound", "unknown"] = "unknown"
    max_phase: float | None = None  # ChEMBL max phase (4 = approved)
    identifiers: dict[str, str] = Field(default_factory=dict)  # chembl, pubchem_cid, inchikey, unii, drugbank ...
    url: str | None = None


class TargetInfo(BaseModel):
    gene_symbol: str  # human HGNC symbol
    name: str | None = None
    uniprot: str | None = None
    ensembl_gene: str | None = None
    chembl_target: str | None = None
    action: str | None = None  # INHIBITOR, AGONIST ... (from mechanisms)
    source: str  # chembl_mechanism | chembl_activity | opentargets | drugcentral
    url: str | None = None


class OrthologSimilarity(BaseModel):
    target: str  # human gene symbol
    species: str  # SpeciesInfo.key
    ortholog_symbol: str | None = None
    ortholog_id: str | None = None  # MGI:..., ZFIN:..., ENSMUSG..., UniProt ...
    uniprot: str | None = None
    identity_pct: float | None = None  # % identical residues (relative to the human protein)
    similarity_pct: float | None = None  # % similar residues (BLOSUM62 > 0)
    coverage_pct: float | None = None
    relationship: str | None = None  # ortholog_one2one, ortholog_one2many, best_hit ...
    method: SimilarityMethod = "none"
    note: str | None = None
    url: str | None = None


class Paper(BaseModel):
    species: str
    pmid: str | None = None
    doi: str | None = None
    title: str
    journal: str | None = None
    year: str | None = None
    url: str | None = None
    evidence: EvidenceLevel = "none"
    finding: str | None = None  # one-sentence result (extracted from the abstract when available)
    model: str | None = None  # e.g. "KRAS G12C NCI-H358 xenograft", "healthy volunteers"
    dose: str | None = None  # as reported, e.g. "100 mg/kg/day p.o."
    dose_mg_kg: float | None = None  # normalized when the dose is per body weight
    effect: str | None = None  # as reported, e.g. "tumor regression 85%"
    effect_value: float | None = None
    effect_unit: str | None = None


class Measurement(BaseModel):
    species: str
    kind: MeasurementKind
    target: str | None = None  # human gene symbol of the target (potency)
    standard_type: str | None = None  # IC50, Ki, T1/2, Cmax, AUC, TGI, LD50 ...
    relation: str | None = None
    value: float | None = None
    units: str | None = None
    pchembl: float | None = None
    dose_mg_kg: float | None = None
    description: str | None = None  # assay description
    source: str = "chembl"  # chembl | paper
    source_id: str | None = None  # activity / assay id
    pmid: str | None = None
    doi: str | None = None
    url: str | None = None


# Optional enrichment step injected into compare_species: receives (paper, its source record with the
# abstract in ``summary``) pairs and returns the papers with model / dose / effect / finding filled in.
PaperExtractor = Callable[[list[tuple[Paper, Record]]], Awaitable[list[Paper]]]


class SpeciesSummary(BaseModel):
    species: str
    label: str
    label_zh: str = ""  # kept so older saved comparisons still load
    group: SpeciesGroup
    scientific_name: str
    mean_identity_pct: float | None = None  # mean over targets with an ortholog
    per_target_identity: dict[str, float | None] = Field(default_factory=dict)
    n_papers: int = 0  # PubMed hits for compound + species
    n_measurements: int = 0
    measurements_by_kind: dict[str, int] = Field(default_factory=dict)
    median_pchembl: float | None = None
    evidence_level: EvidenceLevel = "none"
    summary: str | None = None


class SpeciesComparison(BaseModel):
    comparison_id: str
    created_at: str
    query: str
    compound: CompoundInfo
    targets: list[TargetInfo] = Field(default_factory=list)
    species: list[SpeciesSummary] = Field(default_factory=list)  # human first, then SPECIES order
    similarity: list[OrthologSimilarity] = Field(default_factory=list)
    measurements: list[Measurement] = Field(default_factory=list)
    papers: list[Paper] = Field(default_factory=list)
    methods: dict[str, str] = Field(default_factory=dict)  # how each part was computed, sources
    notes: list[str] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)  # failed sources
    package: dict[str, Any] | None = None  # data package written for this comparison (export_id, files, urls)
    records: list[Record] = Field(default_factory=list, exclude=True)  # raw records behind the comparison

    def to_llm(self, max_chars: int = 30_000) -> str:
        import json

        data = self.model_dump(mode="json", exclude={"records"})
        data["papers"] = data["papers"][:40]
        data["measurements"] = data["measurements"][:60]
        text = json.dumps(data, ensure_ascii=False, default=str)
        if len(text) > max_chars:
            data["measurements"] = data["measurements"][:20]
            data["papers"] = [{k: p.get(k) for k in ("species", "pmid", "title", "year", "finding")} for p in data["papers"][:25]]
            text = json.dumps(data, ensure_ascii=False, default=str)
        return text[:max_chars] if len(text) > max_chars else text
