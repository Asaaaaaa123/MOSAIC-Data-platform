"""Cross-species comparison engine: alignment, ortholog selection, evidence mapping and orchestration.

Offline tests use a fake registry (no HTTP); the end-to-end live test runs with MOSAIC_LIVE_TESTS=1.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from mosaic.datapackage import build_package
from mosaic.http import SourceError
from mosaic.models import Record, ToolResult
from mosaic.species.align import align_proteins, clean_sequence
from mosaic.species.compare import compare_species, compound_kind, resolve_species
from mosaic.species.evidence import (
    activity_kind,
    activity_species,
    classify_paper,
    parse_dose_mg_kg,
    pubmed_query,
    search_terms,
    species_from_organism,
)
from mosaic.species.models import SPECIES_BY_KEY, OrthologSimilarity, Paper
from mosaic.species.orthology import best_ensembl_rows, choose_row, pick_alliance, pick_protein
from tests.conftest import load_fixture

HUMAN_KRAS = (
    "MTEYKLVVVGAGGVGKSALTIQLIQNHFVDEYDPTIEDSYRKQVVIDGETCLLDILDTAGQEEYSAMRDQYMRTGEGFLCVFAINNTKSFEDIHHYREQIKRVKDSEDVPMV"
    "LVGNKCDLPSRTVDTKQAQDLARSYGIPFIETSAKTRQRVEDAFYTLVREIRQYRLKKISKEEKTPGCVKIKKCIIM"
)
MOUSE_KRAS = HUMAN_KRAS.replace("QAQDLARSY", "QAQELARSY")[:-4] + "CVIM"  # 2 substitutions (D->E, I->V)
FLY_RAS = "MTEYKLVVVGAGGVGKSALTIQLIQNHFVDEYDPTIEDSYRKQVVIDGETCLLDILDTAGQEEYSAMRDQYMRTGEGFLLVFAVNSAKSFEDIGTYREQIKRVKDAEEVPMVLVGNKCDLASWNVNNEQAREVAKQYGIPYIETSAKTRMGVDDAFYTLVREIRKDKDNKGRRGRKMNKPNRRFKCKML"


# ---------------------------------------------------------------- alignment
def test_alignment_identity_similarity_coverage():
    same = align_proteins(HUMAN_KRAS, HUMAN_KRAS)
    assert (same.identity_pct, same.similarity_pct, same.coverage_pct) == (100.0, 100.0, 100.0)

    mouse = align_proteins(HUMAN_KRAS, MOUSE_KRAS)
    assert mouse.identical == 187 and mouse.human_length == 189
    assert mouse.identity_pct == 98.94  # 187 / 189
    assert mouse.similarity_pct == 100.0  # D/E and I/V are BLOSUM62-positive
    assert mouse.coverage_pct == 100.0

    fragment = align_proteins(HUMAN_KRAS, HUMAN_KRAS[:95])
    assert fragment.identity_pct == pytest.approx(95 / 189 * 100, abs=0.01)  # relative to the HUMAN length
    assert fragment.coverage_pct == fragment.identity_pct

    fly = align_proteins(HUMAN_KRAS, FLY_RAS)
    assert 60 < fly.identity_pct < 90 and fly.similarity_pct > fly.identity_pct


def test_alignment_limits_and_cleaning():
    assert align_proteins("", HUMAN_KRAS) is None
    assert align_proteins("M" * 50, "M" * 4001) is None
    assert clean_sequence("mte ykU\n12") == "MTEYKX"  # U (selenocysteine) is not in BLOSUM62; digits/space dropped


# ---------------------------------------------------------------- ortholog selection
def _protein(acc: str, gene: str, length: int, taxon: int, reviewed: bool) -> Record:
    return Record(source="uniprot", id=acc, type="protein", identifiers={"uniprot": acc},
                  attributes={"gene": gene, "length": length, "taxon_id": taxon, "reviewed": reviewed})


def test_pick_protein_prefers_symbol_then_full_length_reviewed():
    hits = [
        _protein("Q00001", "Kras", 60, 10090, True),  # reviewed fragment
        _protein("Q00002", "Kras", 189, 10090, False),
        _protein("P32883", "Kras", 189, 10090, True),
        _protein("Q00003", "Hras", 189, 10090, True),
        _protein("Q00004", "Kras", 189, 10116, True),  # other taxon
    ]
    assert pick_protein(hits, "KRAS", 189, 10090).id == "P32883"
    assert pick_protein(hits[:2], "Kras", 189, 10090).id == "Q00002"  # full-length unreviewed beats a fragment
    assert pick_protein(hits[4:], "Kras", 189, 10090) is None


def test_pick_alliance_best_scored_ortholog_per_species():
    def orth(gid: str, symbol: str, taxon: str, best: str, methods: int) -> Record:
        return Record(source="alliance", id=gid, type="gene", title=symbol,
                      attributes={"taxon": taxon, "best_score": best, "best_reverse_score": best, "methods_matched_count": methods})

    picked = pick_alliance([
        orth("SGD:S000002", "RAS1", "NCBITaxon:559292", "No", 9),
        orth("SGD:S000001", "RAS2", "NCBITaxon:559292", "Yes", 7),
        orth("MGI:96680", "Kras", "NCBITaxon:10090", "Yes", 12),
        orth("Xenbase:XB-1", "kras.L", "NCBITaxon:8355", "Yes", 5),
        orth("Xenbase:XB-2", "kras", "NCBITaxon:8364", "Yes", 5),
        orth("X:1", "foo", "NCBITaxon:9999", "Yes", 20),
    ])
    assert picked["yeast"].title == "RAS2"
    assert picked["mouse"].id == "MGI:96680"
    assert picked["frog"].id == "Xenbase:XB-2"  # X. tropicalis preferred
    assert set(picked) == {"yeast", "mouse", "frog"}


def test_best_ensembl_rows_one2many_keeps_highest_identity():
    rows = [
        Record(source="ensembl", id="ENSXETG1", type="gene", attributes={"ensembl_species": "xenopus_tropicalis", "perc_id": 90.4}),
        Record(source="ensembl", id="ENSXETG2", type="gene", attributes={"ensembl_species": "xenopus_tropicalis", "perc_id": 92.0}),
        Record(source="ensembl", id="ENSMUSG1", type="gene", attributes={"ensembl_species": "mus_musculus", "perc_id": 97.3}),
        Record(source="ensembl", id="X", type="gene", attributes={"ensembl_species": "felis_catus", "perc_id": 99.0}),
    ]
    best = best_ensembl_rows(rows, {"xenopus_tropicalis": "frog", "mus_musculus": "mouse"})
    assert best["frog"][0].id == "ENSXETG2" and best["frog"][1] == 2
    assert set(best) == {"frog", "mouse"}


def test_choose_row_prefers_ensembl_unless_alignment_clearly_higher():
    ens = OrthologSimilarity(target="KRAS", species="rat", ortholog_symbol="Kras", ortholog_id="ENSRNOG1",
                             identity_pct=90.43, method="ensembl_compara", relationship="ortholog_one2one")
    close = OrthologSimilarity(target="KRAS", species="rat", ortholog_symbol="Kras", uniprot="P08644",
                               identity_pct=91.5, method="mosaic_alignment", note="aln")
    high = close.model_copy(update={"identity_pct": 98.94})
    kept = choose_row(ens, close)
    assert kept.method == "ensembl_compara" and kept.uniprot == "P08644" and "91.5%" in kept.note
    replaced = choose_row(ens, high)
    assert replaced.method == "mosaic_alignment" and replaced.identity_pct == 98.94
    assert "90.43%" in replaced.note and replaced.relationship == "ortholog_one2one"
    assert choose_row(None, high) is high and choose_row(ens, None) is ens
    failed = close.model_copy(update={"identity_pct": None, "method": "none"})
    assert choose_row(ens, failed) is ens


# ---------------------------------------------------------------- evidence mapping
@pytest.mark.parametrize(
    ("attrs", "species", "kind"),
    [
        ({"assay_type": "B", "target_organism": "Homo sapiens", "target_chembl_id": "CHEMBL2189121",
          "assay_description": "Inhibition of human KRAS G12C nucleotide exchange"}, "human", "potency"),
        ({"assay_type": "B", "target_organism": "Mus musculus", "target_chembl_id": "CHEMBL3", "standard_type": "Ki",
          "assay_description": "Binding affinity to mouse Abl"}, "mouse", "potency"),
        ({"assay_type": "F", "target_organism": "Homo sapiens", "target_chembl_id": "CHEMBL614",
          "assay_description": "Antitumor activity against human MIAPaCa2 cells xenografted in athymic nude mouse assessed as "
                               "tumor growth inhibition at 10 mg/kg, po qd"}, "mouse", "in_vivo_efficacy"),
        ({"assay_type": "F", "target_organism": "Homo sapiens", "target_chembl_id": "CHEMBL614",
          "assay_description": "Antitumor activity against human NCI-H23 cells assessed as inhibition of cell growth "
                               "incubated for 72 hrs"}, "human", "potency"),
        ({"assay_type": "A", "assay_description": "Intrinsic clearance in mouse cryopreserved hepatocytes"}, "mouse", "pharmacokinetics"),
        ({"assay_type": "A", "standard_type": "T1/2", "assay_description": "Half life in Beagle dog at 1 mg/kg, iv"}, "dog", "pharmacokinetics"),
        ({"assay_type": "A", "standard_type": "Cmax", "assay_description": "Cmax in NSCLC patient serum at 960 mg, po"}, "human", "pharmacokinetics"),
        ({"assay_type": "T", "standard_type": "LD50", "assay_description": "Acute toxicity in rat assessed as LD50"}, "rat", "toxicity"),
        ({"assay_type": "A", "assay_description": "Cytotoxicity against human HCT116 cells"}, "human", "toxicity"),
        ({"assay_type": "F", "target_organism": "Danio rerio", "target_chembl_id": "CHEMBL9",
          "assay_description": "Developmental toxicity in zebrafish embryos"}, "zebrafish", "potency"),
        ({"assay_type": "P", "assay_description": "Aqueous solubility"}, None, "other"),
    ],
)
def test_activity_species_and_kind(attrs, species, kind):
    assert activity_species(attrs) == species
    assert activity_kind(attrs) == kind


def test_organism_names_and_dose_parsing():
    assert species_from_organism("Mus musculus") == "mouse"
    assert species_from_organism("Canis lupus familiaris") == "dog"
    assert species_from_organism("Canis familiaris") == "dog"
    assert species_from_organism("Macaca fascicularis") == "macaque"
    assert species_from_organism("Saccharomyces cerevisiae S288c") == "yeast"
    assert species_from_organism("Plasmodium falciparum") is None
    assert parse_dose_mg_kg("tumor growth inhibition at 10 mg/kg, po qd") == 10.0
    assert parse_dose_mg_kg("at 0.5 mg / kg iv") == 0.5
    assert parse_dose_mg_kg("dosed 3 to 30 mg/kg") == 3.0
    assert parse_dose_mg_kg("960 mg once daily") is None
    assert parse_dose_mg_kg(None) is None


def test_classify_paper_from_publication_types_mesh_and_text():
    def rec(pub_types=(), mesh=(), title="t", summary=None) -> Record:
        return Record(source="ncbi", id="1", type="article", title=title, summary=summary,
                      attributes={"publication_types": list(pub_types), "mesh_terms": list(mesh)})

    assert classify_paper(rec(["Clinical Trial, Phase III"])) == "clinical"
    assert classify_paper(rec(["Randomized Controlled Trial"])) == "clinical"
    assert classify_paper(rec(["Review"], ["Disease Models, Animal"])) == "none"
    assert classify_paper(rec(["Meta-Analysis", "Review"], ["Mice, Nude"])) == "in_vivo"
    assert classify_paper(rec(mesh=["Xenograft Model Antitumor Assays*"])) == "in_vivo"
    assert classify_paper(rec(summary="Mice were treated orally")) == "in_vivo"
    assert classify_paper(rec(mesh=["Cell Line, Tumor"])) == "in_vitro"
    assert classify_paper(rec(mesh=["MCF-7 Cells"])) == "in_vitro"
    assert classify_paper(rec(mesh=["Molecular Docking Simulation"])) == "computational"
    assert classify_paper(rec()) == "none"


def test_search_terms_and_pubmed_query():
    terms = search_terms("IMATINIB", ["Imatinib", "IMATINIB MESYLATE", "GLEEVEC", "GLEEVEC,STI-571", "STI-571", "STI571",
                                      "CT-AMG510AMG-510AMG510", "ab"])
    assert terms == ["IMATINIB", "GLEEVEC", "STI-571"]
    assert pubmed_query(["sotorasib", "AMG 510"], SPECIES_BY_KEY["mouse"]) == '("sotorasib"[tiab] OR "AMG 510"[tiab]) AND mice[mh]'
    assert search_terms("4,4'-(propane-2,2-diyl)diphenol") == ["4,4'- propane-2,2-diyl diphenol"]


def test_resolve_species_and_compound_kind():
    assert [s.key for s in resolve_species(["fly", "mouse"])] == ["human", "mouse", "fly"]
    assert resolve_species(None)[0].key == "human" and len(resolve_species(None)) == 13
    with pytest.raises(ValueError):
        resolve_species(["unicorn"])
    assert compound_kind(4.0, in_chembl=True, in_rxnorm=True, in_drugcentral=True, has_ids=True) == "drug"
    assert compound_kind(None, in_chembl=True, in_rxnorm=True, in_drugcentral=False, has_ids=True) == "compound"  # e.g. BPA
    assert compound_kind(None, in_chembl=False, in_rxnorm=True, in_drugcentral=False, has_ids=True) == "drug"
    assert compound_kind(None, in_chembl=False, in_rxnorm=False, in_drugcentral=False, has_ids=False) == "unknown"


# ---------------------------------------------------------------- orchestration (fake registry)
class _FakeConnector:
    def __init__(self, tools: set[str]):
        self.configured = True
        self._tool_defs = {t: None for t in tools}


class FakeRegistry:
    """Dispatches ``registry.call`` to handlers ``(source, tool) -> fn(params) -> ToolResult | Exception``."""

    def __init__(self, handlers: dict[tuple[str, str], Callable[[dict[str, Any]], Any]]):
        self.handlers = handlers
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        tools: dict[str, set[str]] = {}
        for source, tool in handlers:
            tools.setdefault(source, set()).add(tool)
        self.connectors = {s: _FakeConnector(t) for s, t in tools.items()}

    async def call(self, source, tool, params=None, *, session_id=None, timeout=None):
        params = params or {}
        self.calls.append((source, tool, params))
        handler = self.handlers.get((source, tool))
        if handler is None:
            raise SourceError(source, f"no fake for {tool}")
        out = handler(params)
        if isinstance(out, Exception):
            raise out
        return out


def _result(source: str, tool: str, records: list[Record] | None = None, **kw) -> ToolResult:
    return ToolResult(source=source, tool=tool, records=records or [], **kw)


def _activity(aid: str, **attrs) -> Record:
    return Record(source="chembl", id=aid, type="activity", title=f"act {aid}", identifiers={"chembl": attrs.get("target_chembl_id")},
                  url=f"https://www.ebi.ac.uk/chembl/explore/assay/CHEMBL_A{aid}", attributes={"molecule_chembl_id": "CHEMBL4535757", **attrs})


def _sequences(params: dict[str, Any]) -> ToolResult:
    if params.get("uniref50_of"):
        return _result("uniprot", "get_sequences", data={"uniref50_cluster": "UniRef50_P01116", "sequences": []},
                       notes=["no member"])
    table = {
        "P01116": ("KRAS", 9606, HUMAN_KRAS, {"gene_symbol": "KRAS", "hgnc": "HGNC:6407", "ensembl_gene": "ENSG00000133703"}),
        "P32883": ("Kras", 10090, MOUSE_KRAS, {}),
        "P08646": ("Ras85D", 7227, FLY_RAS, {}),
    }
    records, seqs = [], []
    for acc in params.get("accessions") or []:
        if acc not in table:
            continue
        gene, taxon, seq, ids = table[acc]
        records.append(Record(source="uniprot", id=acc, type="protein", title="GTPase KRas", identifiers={"uniprot": acc, **ids},
                              attributes={"gene": gene, "taxon_id": taxon, "length": len(seq), "reviewed": True}))
        seqs.append({"accession": acc, "gene": gene, "taxon_id": taxon, "length": len(seq), "reviewed": True, "sequence": seq})
    return _result("uniprot", "get_sequences", records, data={"sequences": seqs})


def _pubmed(params: dict[str, Any]) -> ToolResult | Exception:
    papers = load_fixture("species_engine", "pubmed_records.json")
    query = params["query"]
    if "drosophila[mh]" in query:
        return SourceError("ncbi", "HTTP 429 too many requests", status=429)
    if "humans[mh]" in query:
        return _result("ncbi", "pubmed_search", [Record(**r) for r in papers["human"]], total=452)
    if "mice[mh]" in query:
        return _result("ncbi", "pubmed_search", [Record(**r) for r in papers["mouse"]], total=79)
    return _result("ncbi", "pubmed_search", [], total=0)


def _activities(params: dict[str, Any]) -> ToolResult:
    rows = {
        "B": [_activity("1", assay_type="B", target_chembl_id="CHEMBL2189121", target_organism="Homo sapiens",
                        target_name="GTPase KRas", standard_type="IC50", standard_value=8.9, standard_units="nM",
                        pchembl_value=8.05, assay_description="Inhibition of KRAS G12C", pmid="31666701")],
        "F": [_activity("2", assay_type="F", target_chembl_id="CHEMBL614", target_organism="Homo sapiens", standard_type="TGI",
                        standard_value=86, standard_units="%",
                        assay_description="Antitumor activity against human MIAPaCa2 cells xenografted in athymic nude mouse "
                                          "assessed as tumor growth inhibition at 10 mg/kg, po qd")],
        "A": [_activity("3", assay_type="A", standard_type="CL", standard_value=36.7,
                        assay_description="Clearance in Beagle dog at 1 mg/kg, iv"),
              _activity("4", assay_type="A", standard_type="CLint", standard_value=12,
                        assay_description="Intrinsic clearance in mouse cryopreserved hepatocytes")],
        "T": [],
    }[params.get("assay_type")]
    return _result("chembl", "get_activities", rows, total=len(rows))


def _sotorasib_handlers(overrides: dict | None = None) -> dict:
    molecule = Record(source="chembl", id="CHEMBL4535757", type="drug", title="SOTORASIB",
                      url="https://www.ebi.ac.uk/chembl/explore/compound/CHEMBL4535757",
                      identifiers={"chembl": "CHEMBL4535757", "inchikey": "NXQKSXLFSAEQCZ-SFHVURJKSA-N", "pubchem_cid": "137278711"},
                      attributes={"max_phase": 4, "synonyms": ["AMG-510", "LUMAKRAS", "SOTORASIB"]})
    mechanism = Record(source="chembl", id="1", type="association", title="GTPase KRas inhibitor",
                       attributes={"action_type": "INHIBITOR", "target_chembl_id": "CHEMBL2189121", "target_type": "SINGLE PROTEIN",
                                   "target_organism": "Homo sapiens", "target_uniprot": ["P01116"]})
    alliance = [
        Record(source="alliance", id="MGI:96680", type="gene", title="Kras",
               attributes={"taxon": "NCBITaxon:10090", "best_score": "Yes", "best_reverse_score": "Yes", "methods_matched_count": 12}),
        Record(source="alliance", id="FB:FBgn0003205", type="gene", title="Ras85D",
               attributes={"taxon": "NCBITaxon:7227", "best_score": "Yes", "best_reverse_score": "Yes", "methods_matched_count": 10}),
    ]

    def search_proteins(params):
        if "organism_id" in params and params["organism_id"] == 10090:
            return _result("uniprot", "search_proteins", [
                _protein("Q00001", "Kras", 60, 10090, True), _protein("P32883", "Kras", 189, 10090, True)])
        if params.get("organism_id") == 7227:
            return _result("uniprot", "search_proteins", [_protein("P08646", "Ras85D", len(FLY_RAS), 7227, True)])
        return _result("uniprot", "search_proteins", [])

    handlers = {
        ("chembl", "get_molecule"): lambda p: _result("chembl", "get_molecule", [molecule]),
        ("pubchem", "get_compound"): lambda p: _result("pubchem", "get_compound", [Record(
            source="pubchem", id="137278711", type="compound", title="Sotorasib",
            identifiers={"pubchem_cid": "137278711", "inchikey": "NXQKSXLFSAEQCZ-SFHVURJKSA-N"})]),
        ("chembl", "get_mechanisms"): lambda p: _result("chembl", "get_mechanisms", [mechanism]),
        ("chembl", "get_activities"): _activities,
        ("uniprot", "get_sequences"): _sequences,
        ("uniprot", "search_proteins"): search_proteins,
        ("ensembl", "orthologs"): lambda p: SourceError("ensembl", "HTTP 503 Service Unavailable", status=503)
        if p.get("compara") != "pan_homology" else _result("ensembl", "orthologs", []),
        ("alliance", "orthologs"): lambda p: _result("alliance", "orthologs", alliance),
        ("ncbi", "pubmed_search"): _pubmed,
        ("clinicaltrials", "count_studies"): lambda p: _result("clinicaltrials", "count_studies", total=73),
    }
    handlers.update(overrides or {})
    return handlers


async def test_compare_species_orchestration_with_failures():
    registry = FakeRegistry(_sotorasib_handlers())
    extracted: list[list[tuple[Paper, Record]]] = []

    async def extractor(items):
        extracted.append(items)
        return [p.model_copy(update={"dose": "960 mg once daily", "effect_value": 32.2, "effect_unit": "%"}) for p, _ in items]

    comparison = await compare_species(registry, "sotorasib", species=["arabidopsis", "fly", "mouse"], max_papers=5,
                                       extractor=extractor, session_id="s1")

    assert len(comparison.comparison_id) == 32 and comparison.created_at.endswith("+00:00")
    assert comparison.compound.name == "SOTORASIB" and comparison.compound.kind == "drug" and comparison.compound.max_phase == 4
    assert comparison.compound.identifiers["pubchem_cid"] == "137278711"
    assert [s.species for s in comparison.species] == ["human", "mouse", "fly", "arabidopsis"]
    assert [t.gene_symbol for t in comparison.targets] == ["KRAS"]
    assert comparison.targets[0].action == "INHIBITOR" and comparison.targets[0].source == "chembl_mechanism"

    # failures are recorded, not raised
    assert "ensembl.orthologs:KRAS" in comparison.errors
    assert "ncbi.pubmed_search:fly" in comparison.errors
    assert any("failed" in n and "ensembl" in n for n in comparison.notes)

    sim = {r.species: r for r in comparison.similarity}
    assert [r.species for r in comparison.similarity][0] == "human" and sim["human"].identity_pct == 100
    assert sim["mouse"].method == "mosaic_alignment" and sim["mouse"].uniprot == "P32883"  # fragment Q00001 skipped
    assert sim["mouse"].identity_pct == 98.94 and sim["mouse"].relationship == "alliance_stringent"
    assert sim["mouse"].ortholog_id == "MGI:96680"
    assert sim["fly"].ortholog_symbol == "Ras85D" and 60 < sim["fly"].identity_pct < 90
    assert sim["arabidopsis"].method == "none" and sim["arabidopsis"].identity_pct is None

    by_species = {s.species: s for s in comparison.species}
    assert by_species["human"].n_papers == 452 and by_species["human"].evidence_level == "clinical"
    assert by_species["mouse"].mean_identity_pct == 98.9 and by_species["mouse"].per_target_identity == {"KRAS": 98.94}
    assert by_species["mouse"].evidence_level == "in_vivo"
    assert by_species["mouse"].measurements_by_kind == {"in_vivo_efficacy": 1, "pharmacokinetics": 1}
    assert by_species["fly"].n_papers == 0
    assert "73 ClinicalTrials.gov studies" in by_species["human"].summary
    assert by_species["mouse"].summary.startswith("Mouse: ortholog identity to human: KRAS 98.9%")

    kinds = {(m.species, m.kind): m for m in comparison.measurements}
    assert kinds[("human", "potency")].target == "KRAS" and kinds[("human", "potency")].pchembl == 8.05
    assert kinds[("mouse", "in_vivo_efficacy")].dose_mg_kg == 10.0
    assert ("dog", "pharmacokinetics") not in kinds  # dog was not compared
    assert any("ChEMBL activities were left out" in n for n in comparison.notes)

    # extractor called once with the papers that have abstracts; its output replaces them
    assert len(extracted) == 1 and all(r.summary for _, r in extracted[0])
    assert {p.pmid for p, _ in extracted[0]} == {"32955176", "31666701"}
    assert extracted[0][0][1].attributes["species"] in ("Homo sapiens", "Mus musculus")
    clinical = [p for p in comparison.papers if p.pmid == "32955176" and p.species == "human"][0]
    assert clinical.evidence == "clinical" and clinical.dose == "960 mg once daily"
    review = [p for p in comparison.papers if p.pmid == "40000001"][0]
    assert review.evidence == "none" and review.dose is None  # no abstract: not sent to the extractor

    # records carry species and feed a by-species package
    for record in comparison.records:
        if record.type not in ("drug", "compound"):
            assert record.attributes.get("species"), record
    shared = [r for r in comparison.records if r.id == "32955176"][0]
    assert shared.attributes["species"] == ["Homo sapiens", "Mus musculus"]
    activities = [r for r in comparison.records if r.type == "activity"]
    assert activities and all(r.identifiers == {} and "target_organism" not in r.attributes for r in activities)
    xenograft = [r for r in activities if r.id == "2"][0]
    assert xenograft.attributes["species"] == "Mus musculus" and xenograft.attributes["target_organism_name"] == "Homo sapiens"
    package = build_package(comparison.records, "species")
    assert {"human", "mouse", "fly"} <= set(package.stats["tables"])
    assert "mosaic_alignment" in comparison.methods["similarity"]
    assert all(call[0] != "ensembl" or call[1] == "orthologs" for call in registry.calls)
    assert comparison.model_dump(mode="json")  # serializable


async def test_compare_species_non_drug_uses_activity_targets_and_ensembl():
    molecule = Record(source="chembl", id="CHEMBL418971", type="compound", title="BISPHENOL A",
                      identifiers={"chembl": "CHEMBL418971"}, attributes={})
    potent = [
        _activity(str(i), assay_type="B", target_chembl_id="CHEMBL206", target_organism="Homo sapiens", target_name="Estrogen receptor alpha",
                  pchembl_value=6.5, standard_type="IC50", assay_description="Binding to human ESR1")
        for i in range(3)
    ] + [_activity("9", assay_type="B", target_chembl_id="CHEMBL999", target_organism="Rattus norvegicus", pchembl_value=7.0,
                   assay_description="Binding to rat receptor")]

    def target(params):
        return _result("chembl", "get_target", [Record(source="chembl", id="CHEMBL206", type="target", title="Estrogen receptor",
                                                       identifiers={"chembl": "CHEMBL206", "uniprot": "P01116"},
                                                       attributes={"target_type": "SINGLE PROTEIN"})])

    ensembl_row = Record(source="ensembl", id="ENSMUSG00000030265", type="gene", title="Kras",
                         url="https://www.ensembl.org/Mus_musculus/Gene/Summary?g=ENSMUSG00000030265",
                         attributes={"ensembl_species": "mus_musculus", "perc_id": 98.5, "perc_pos": 99.0, "coverage_pct": 100.0,
                                     "relationship": "ortholog_one2one"})
    registry = FakeRegistry(_sotorasib_handlers({
        ("chembl", "get_molecule"): lambda p: _result("chembl", "get_molecule", [molecule]),
        ("chembl", "get_mechanisms"): lambda p: _result("chembl", "get_mechanisms", []),
        ("chembl", "get_activities"): lambda p: _result("chembl", "get_activities", potent if p.get("assay_type") == "B" else []),
        ("chembl", "get_target"): target,
        ("ensembl", "orthologs"): lambda p: _result("ensembl", "orthologs", [ensembl_row]),
        ("clinicaltrials", "count_studies"): lambda p: SourceError("clinicaltrials", "timeout", retryable=True),
    }))
    comparison = await compare_species(registry, "CHEMBL418971", species=["mouse"], max_papers=0)
    assert comparison.compound.kind == "compound"  # no phase, ChEMBL id given directly (no resolver)
    assert not any(c[0] == "pubchem" for c in registry.calls)
    assert comparison.targets[0].source == "chembl_activity" and comparison.targets[0].chembl_target == "CHEMBL206"
    assert any("No curated mechanism" in n for n in comparison.notes)
    mouse = [r for r in comparison.similarity if r.species == "mouse"][0]
    assert mouse.method == "ensembl_compara" and mouse.identity_pct == 98.5  # alignment 98.94 is within the margin
    assert "clinicaltrials.count_studies" in comparison.errors
    assert comparison.papers == [] and comparison.species[1].n_papers == 79
    # potency rows map the human target ChEMBL id to the gene symbol
    assert {m.target for m in comparison.measurements if m.kind == "potency" and m.species == "human"} == {"KRAS"}


async def test_compare_species_unknown_compound_still_returns_a_comparison():
    registry = FakeRegistry({
        ("chembl", "get_molecule"): lambda p: _result("chembl", "get_molecule", []),
        ("pubchem", "get_compound"): lambda p: SourceError("pubchem", "HTTP 404"),
        ("ncbi", "pubmed_search"): lambda p: _result("ncbi", "pubmed_search", [], total=0),
        ("clinicaltrials", "count_studies"): lambda p: _result("clinicaltrials", "count_studies", total=0),
    })
    comparison = await compare_species(registry, "notacompound123", species=["rat"])
    assert comparison.compound.kind == "unknown" and comparison.targets == [] and comparison.similarity == []
    assert [s.species for s in comparison.species] == ["human", "rat"]
    assert any("not found in ChEMBL" in n for n in comparison.notes)


# ---------------------------------------------------------------- live end-to-end
@pytest.mark.live
async def test_live_compare_sotorasib(live_registry):
    registry = live_registry()
    comparison = await compare_species(registry, "sotorasib", species=["mouse", "rat", "zebrafish", "yeast"], max_papers=2)
    assert comparison.compound.identifiers.get("chembl") == "CHEMBL4535757"
    assert [t.gene_symbol for t in comparison.targets][:1] == ["KRAS"]
    sim = {r.species: r for r in comparison.similarity if r.target == "KRAS"}
    assert 95 <= sim["mouse"].identity_pct <= 100
    assert 95 <= sim["rat"].identity_pct <= 100
    assert 80 <= sim["zebrafish"].identity_pct <= 100
    assert sim["yeast"].identity_pct is None or 40 <= sim["yeast"].identity_pct <= 75
    human = comparison.species[0]
    assert human.species == "human" and human.n_papers > 100 and human.evidence_level == "clinical"
    assert any(m.species == "mouse" and m.kind == "in_vivo_efficacy" for m in comparison.measurements)


async def test_fetch_papers_retries_rate_limit_once(monkeypatch):
    from mosaic.species import evidence
    from mosaic.species.calls import SafeCaller

    monkeypatch.setattr(evidence, "RATE_LIMIT_PAUSE", 0)
    attempts: dict[str, int] = {}

    def pubmed(params):
        key = "mouse" if "mice[mh]" in params["query"] else "rat"
        attempts[key] = attempts.get(key, 0) + 1
        if key == "rat" or attempts[key] == 1:
            return SourceError("ncbi", "HTTP 429 too many requests", status=429)
        return _result("ncbi", "pubmed_search", [], total=7)

    caller = SafeCaller(FakeRegistry({("ncbi", "pubmed_search"): pubmed}))
    out = await evidence.fetch_papers(caller, ["sotorasib"], [SPECIES_BY_KEY["mouse"], SPECIES_BY_KEY["rat"]], 2)
    assert out["mouse"] == (7, []) and attempts["mouse"] == 2
    assert out["rat"] == (None, []) and attempts["rat"] == 2
    assert "ncbi.pubmed_search:mouse" not in caller.errors and "ncbi.pubmed_search:rat" in caller.errors
