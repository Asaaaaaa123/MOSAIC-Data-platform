"""Metabolomics Workbench connector tests."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.connectors.metabolomics_workbench import _rows, parse_study_page
from tests.conftest import load_fixture

REST = "https://www.metabolomicsworkbench.org/rest"
PAGE = "https://www.metabolomicsworkbench.org/data/DRCCMetadata.php"
SRC = "metabolomics_workbench"


def _ok(name: str) -> httpx.Response:
    return httpx.Response(200, json=load_fixture(SRC, name))


EMPTY = httpx.Response(200, json=[])


def test_rows_normalizes_response_shapes():
    assert _rows([]) == []
    assert _rows({"study_id": "ST000001"}) == [{"study_id": "ST000001"}]
    assert _rows({"1": {"a": 1}, "2": {"a": 2}}) == [{"a": 1}, {"a": 2}]


def test_parse_study_page_fixture():
    fields = parse_study_page(load_fixture(SRC, "page_ST000001.html"))
    assert fields["Project DOI"] == "doi: 10.21228/M8159B"
    assert fields["Study Summary"].startswith("This experiment tests the consequence")
    assert fields["Institute"] == "University of California, Davis"  # first occurrence wins
    assert "Email" not in fields


@respx.mock
async def test_search_studies_phrase(make_registry):
    route = respx.get(f"{REST}/study/study_title/breast%20cancer/summary").mock(
        return_value=_ok("search_breast_cancer.json")
    )
    registry = make_registry(SRC)
    result = await registry.call(SRC, "search_studies", {"query": "breast  cancer", "limit": 2})
    assert route.called
    assert result.total == 3 and result.next_cursor == "2"
    first = result.records[0]
    assert first.id == "ST005026" and first.type == "study"
    assert first.identifiers == {"metabolomics_workbench": "ST005026"}
    assert first.title.startswith("A non-canonical function of the tyrosine degradation enzyme FAH")
    assert first.date == "2026-08-10"
    assert first.url == f"{PAGE}?Mode=Study&StudyID=ST005026"
    assert first.attributes["species"] == "Homo sapiens"
    assert first.attributes["number_of_samples"] == 15
    assert first.attributes["analysis_type"] == "LC-MS"
    page2 = await registry.call(SRC, "search_studies", {"query": "breast cancer", "limit": 2, "cursor": "2"})
    assert [r.id for r in page2.records] == ["ST004904"] and page2.next_cursor is None


@respx.mock
async def test_search_studies_word_fallback(make_registry):
    breast = load_fixture(SRC, "search_breast_cancer.json")
    respx.get(f"{REST}/study/study_title/breast%20tumor%20lipidomics/summary").mock(return_value=EMPTY)
    respx.get(f"{REST}/study/study_title/breast/summary").mock(return_value=httpx.Response(200, json=breast))
    # a single hit comes back as a flat object, not keyed "1"
    respx.get(f"{REST}/study/study_title/tumor/summary").mock(return_value=httpx.Response(200, json=breast["2"]))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "search_studies", {"query": "breast tumor lipidomics"})
    assert [r.id for r in result.records] == ["ST004922"]  # the only study matching both words
    assert "ranked studies by words matched" in result.notes[0] and "2/2" in result.notes[0]


@respx.mock
async def test_search_studies_no_hits(make_registry):
    respx.get(f"{REST}/study/study_title/zzqqxx/summary").mock(return_value=EMPTY)
    registry = make_registry(SRC)
    result = await registry.call(SRC, "search_studies", {"query": "zzqqxx"})
    assert result.records == [] and result.total == 0 and result.next_cursor is None


@respx.mock
async def test_get_study_full(make_registry):
    base = f"{REST}/study/study_id/ST000001"
    respx.get(f"{base}/summary").mock(return_value=_ok("sum_ST000001.json"))
    respx.get(f"{base}/factors").mock(return_value=_ok("fac_ST000001.json"))
    respx.get(f"{base}/analysis").mock(return_value=_ok("ana_ST000001.json"))
    respx.get(f"{base}/number_of_metabolites").mock(return_value=_ok("nm_ST000001.json"))
    page = respx.get(PAGE).mock(return_value=httpx.Response(200, text=load_fixture(SRC, "page_ST000001.html")))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "get_study", {"study_id": "st000001"})
    params = page.calls.last.request.url.params
    assert params["Mode"] == "Study" and params["StudyID"] == "ST000001"
    record = result.records[0]
    assert record.id == "ST000001"
    assert record.title == "Fatb Induction Experiment (FatBIE)"
    assert record.identifiers == {"metabolomics_workbench": "ST000001", "doi": "10.21228/m8159b"}
    assert record.date == "2013-02-14"
    assert record.authors == ["Tobias Kind"]
    assert record.summary.startswith("This experiment tests the consequence of a mutation at the FatB gene")
    attrs = record.attributes
    assert attrs["license"] == "CC BY-NC-ND"
    assert attrs["design_factors"]["Arabidopsis Genotype"] == {"fatb-ko KD; At1g08510": 6}
    assert attrs["sample_sources"] == ["Plant"]
    analysis = attrs["analyses"][0]
    assert analysis["analysis_id"] == "AN000001" and analysis["num_metabolites"] == 102
    assert analysis["chromatography"] == "GC" and analysis["ion_mode"] == "POSITIVE"
    assert attrs["project_id"] == "PR000001" and attrs["project_title"] == "FatB Gene Project"
    assert attrs["publication_dois"] == ["10.1111/j.1365-313x.2007.03387.x"]
    assert attrs["publication_pmids"] == ["18269577"]
    assert attrs["taxonomy_id"] == "3702" and attrs["total_subjects"] == 24
    assert result.notes == []


@respx.mock
async def test_get_study_page_failure_is_a_note(make_registry):
    base = f"{REST}/study/study_id/ST000001"
    respx.get(f"{base}/summary").mock(return_value=_ok("sum_ST000001.json"))
    for output in ("factors", "analysis", "number_of_metabolites"):
        respx.get(f"{base}/{output}").mock(return_value=EMPTY)
    respx.get(PAGE).mock(return_value=httpx.Response(403, text="Forbidden"))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "get_study", {"study_id": "ST000001"})
    assert result.records[0].identifiers == {"metabolomics_workbench": "ST000001"}
    assert "study page unavailable (403)" in result.notes[0]


@respx.mock
async def test_get_study_not_found_and_invalid(make_registry):
    respx.get(f"{REST}/study/study_id/ST999999/summary").mock(return_value=EMPTY)
    # study_id is a prefix match upstream: a different study coming back must not be accepted
    respx.get(f"{REST}/study/study_id/ST000020/summary").mock(return_value=_ok("sum_ST000002.json"))
    registry = make_registry(SRC)
    missing = await registry.call(SRC, "get_study", {"study_id": "ST999999"})
    assert missing.records == [] and "not found" in missing.notes[0]
    prefix = await registry.call(SRC, "get_study", {"study_id": "ST000020", "include_page": False})
    assert prefix.records == []
    invalid = await registry.call(SRC, "get_study", {"study_id": "glucose"})
    assert invalid.records == [] and "not a study id" in invalid.notes[0]


@respx.mock
async def test_compound_lookup_by_name_and_cid(make_registry):
    respx.get(f"{REST}/refmet/name/glucose/all").mock(return_value=_ok("refname_glucose.json"))
    by_regno = respx.get(f"{REST}/compound/regno/37084/all").mock(return_value=_ok("cid_5793.json"))
    by_cid = respx.get(f"{REST}/compound/pubchem_cid/5793/all").mock(return_value=_ok("cid_5793.json"))
    registry = make_registry(SRC)
    named = await registry.call(SRC, "compound_lookup", {"identifier": "glucose"})
    assert by_regno.called
    rec = named.records[0]
    assert rec.id == "37084" and rec.type == "compound" and rec.title == "D-Glucose"
    assert rec.identifiers == {
        "pubchem_cid": "5793",
        "inchikey": "WQZGKKKJIJFFOK-GASJEMHNSA-N",
        "smiles": "C([C@@H]1[C@H]([C@@H]([C@H](C(O)O1)O)O)O)O",
        "kegg": "C00031",
        "hmdb": "HMDB0304632",
        "chebi": "CHEBI:4167",
        "refmet": "RM0135901",
        "mw_regno": "37084",
    }
    assert rec.attributes["formula"] == "C6H12O6"
    assert rec.attributes["super_class"] == "Carbohydrates" and rec.attributes["sub_class"] == "Hexoses"
    assert rec.url == "https://www.metabolomicsworkbench.org/data/StructureData.php?RegNo=37084"
    assert "resolved via RefMet" in named.notes[0]

    cid = await registry.call(SRC, "compound_lookup", {"identifier": "5793"})
    assert by_cid.called
    assert cid.records[0].identifiers["inchikey"] == "WQZGKKKJIJFFOK-GASJEMHNSA-N"
    assert "refmet" not in cid.records[0].identifiers and cid.notes == []


@respx.mock
async def test_compound_lookup_not_found(make_registry):
    respx.get(f"{REST}/refmet/name/zzqqxx/all").mock(return_value=EMPTY)
    respx.get(f"{REST}/refmet/match/zzqqxx").mock(return_value=_ok("match_none.json"))
    respx.get(f"{REST}/compound/inchi_key/AAAAAAAAAAAAAA-BBBBBBBBBB-C/all").mock(return_value=EMPTY)
    registry = make_registry(SRC)
    by_name = await registry.call(SRC, "compound_lookup", {"identifier": "zzqqxx"})
    assert by_name.records == [] and "no RefMet/Workbench compound" in by_name.notes[0]
    by_key = await registry.call(SRC, "compound_lookup", {"identifier": "aaaaaaaaaaaaaa-bbbbbbbbbb-c"})
    assert by_key.records == [] and "inchi_key" in by_key.notes[0]


@respx.mock
async def test_refmet_lookup(make_registry):
    respx.get(f"{REST}/refmet/match/Tyr").mock(return_value=_ok("match_tyr.json"))
    respx.get(f"{REST}/refmet/name/Tyrosine/all").mock(return_value=_ok("refname_tyrosine.json"))
    respx.get(f"{REST}/refmet/match/zzqqxx").mock(return_value=_ok("match_none.json"))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "refmet_lookup", {"names": ["Tyr", "zzqqxx"]})
    assert len(result.records) == 1 and result.total == 1
    tyr = result.records[0]
    assert tyr.id == "RM0135896" and tyr.title == "Tyrosine"
    assert tyr.identifiers["pubchem_cid"] == "6057"
    assert tyr.identifiers["inchikey"] == "OUYCCCASQSFEME-QMMMGPOBSA-N"
    assert tyr.identifiers["refmet"] == "RM0135896"
    assert tyr.attributes["input_name"] == "Tyr"
    assert tyr.attributes["main_class"] == "Amino acids and peptides"
    assert tyr.url.endswith("REFMET_NAME=Tyrosine")
    assert result.data == {"mapping": {"Tyr": "Tyrosine"}}
    assert result.notes == ["no RefMet match for: zzqqxx"]


@respx.mock
async def test_studies_for_compound(make_registry):
    respx.get(f"{REST}/refmet/match/cholesterol").mock(return_value=_ok("match_chol.json"))
    route = respx.get(f"{REST}/study/refmet_name/Cholesterol/summary").mock(return_value=_ok("refsum_cholesterol.json"))
    for sid in ("ST000002", "ST000004", "ST000005"):
        respx.get(f"{REST}/study/study_id/{sid}/summary").mock(return_value=_ok(f"sum_{sid}.json"))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "studies_for_compound", {"compound": "cholesterol", "limit": 2})
    assert route.called
    assert result.total == 3 and result.next_cursor == "2"
    assert [r.id for r in result.records] == ["ST000005", "ST000004"]  # newest first
    assert result.records[0].title.startswith("Timecourse on RAW 264.7 cells")
    assert all(r.attributes["refmet_name"] == "Cholesterol" for r in result.records)
    assert result.notes == []  # same name, case-insensitively
    rest = await registry.call(SRC, "studies_for_compound", {"compound": "cholesterol", "limit": 2, "cursor": "2"})
    assert [r.id for r in rest.records] == ["ST000002"] and rest.next_cursor is None


@respx.mock
async def test_studies_for_compound_unknown(make_registry):
    respx.get(f"{REST}/refmet/match/zzqqxx").mock(return_value=_ok("match_none.json"))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "studies_for_compound", {"compound": "zzqqxx"})
    assert result.records == [] and "no RefMet match" in result.notes[0]


@pytest.mark.live
async def test_live_metabolomics_workbench_all_tools(live_registry):
    registry = live_registry(SRC)
    search = await registry.call(SRC, "search_studies", {"query": "breast cancer", "limit": 3})
    assert search.records and search.total
    assert all(r.identifiers["metabolomics_workbench"].startswith("ST") for r in search.records)
    study = await registry.call(SRC, "get_study", {"study_id": "ST000001"})
    record = study.records[0]
    assert record.identifiers.get("doi", "").startswith("10.21228/")
    assert record.attributes["analyses"] and record.summary
    compound = await registry.call(SRC, "compound_lookup", {"identifier": "glucose"})
    assert compound.records[0].identifiers["pubchem_cid"] == "5793"
    refmet = await registry.call(SRC, "refmet_lookup", {"names": ["Tyr"]})
    assert refmet.records and refmet.records[0].title == "Tyrosine"
    studies = await registry.call(SRC, "studies_for_compound", {"compound": "cholesterol", "limit": 2})
    assert len(studies.records) == 2 and studies.total > 10
