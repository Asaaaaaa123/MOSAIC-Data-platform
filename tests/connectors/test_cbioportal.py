"""cBioPortal connector tests."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mosaic.curation import curate
from mosaic.models import Record
from tests.conftest import load_fixture

API = "https://www.cbioportal.org/api"


def fx(name):
    return httpx.Response(200, json=load_fixture("cbioportal", name))


@respx.mock
async def test_search_studies(make_registry):
    def studies(request):
        if request.url.params.get("projection") == "META":
            return httpx.Response(200, json=[], headers={"total-count": "18"})
        return fx("studies.json")

    route = respx.get(f"{API}/studies").mock(side_effect=studies)
    registry = make_registry("cbioportal")
    result = await registry.call("cbioportal", "search_studies", {"query": "lung adenocarcinoma", "limit": 2, "cursor": "2"})
    summary_call = route.calls[0].request.url.params
    assert summary_call["keyword"] == "lung adenocarcinoma"
    assert summary_call["projection"] == "SUMMARY"
    assert summary_call["pageSize"] == "2" and summary_call["pageNumber"] == "1"
    assert result.total == 18 and result.next_cursor == "4"
    rec = result.records[0]
    assert rec.id == "luad_broad" and rec.type == "study"
    assert rec.identifiers == {"cbioportal": "luad_broad"}  # the paper is a related entity
    assert rec.attributes["pmids"] == ["22980975"]
    assert rec.url == "https://www.cbioportal.org/study/summary?id=luad_broad"
    assert rec.attributes["samples"] == 183


@respx.mock
async def test_get_study(make_registry):
    respx.get(f"{API}/studies/luad_tcga_pan_can_atlas_2018").mock(return_value=fx("study.json"))
    respx.get(f"{API}/studies/luad_tcga_pan_can_atlas_2018/molecular-profiles").mock(return_value=fx("molecular_profiles.json"))
    registry = make_registry("cbioportal")
    result = await registry.call("cbioportal", "get_study", {"study_id": "luad_tcga_pan_can_atlas_2018"})
    rec = result.records[0]
    assert rec.title == "Lung Adenocarcinoma (TCGA, PanCancer Atlas)"
    assert "pmid" not in rec.identifiers  # shared by all PanCancer Atlas studies
    assert len(rec.attributes["pmids"]) > 1 and rec.attributes["pmids"][0].isdigit()
    assert rec.attributes["sample_counts"]["sequenced"] == 566
    assert rec.summary and "<" not in rec.summary
    types = {p["type"] for p in result.data["molecular_profiles"]}
    assert "MUTATION_EXTENDED" in types


@respx.mock
async def test_get_study_not_found(make_registry):
    respx.get(f"{API}/studies/nope_x").mock(return_value=httpx.Response(404, json={"message": "Study not found: nope_x"}))
    registry = make_registry("cbioportal")
    result = await registry.call("cbioportal", "get_study", {"study_id": "nope_x"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_gene_alteration_frequency(make_registry):
    respx.get(f"{API}/genes/KRAS").mock(return_value=fx("gene_kras.json"))
    fetch = respx.post(f"{API}/studies/fetch").mock(return_value=fx("studies_fetch.json"))
    respx.post(f"{API}/molecular-profiles/fetch").mock(return_value=fx("molecular_profiles_fetch.json"))
    muts = respx.post(f"{API}/mutations/fetch").mock(return_value=fx("mutations.json"))
    registry = make_registry("cbioportal")
    result = await registry.call(
        "cbioportal", "gene_alteration_frequency",
        {"gene": "kras", "study_ids": ["luad_tcga_pan_can_atlas_2018", "coadread_tcga_pan_can_atlas_2018", "nope_x"]},
    )
    assert json.loads(fetch.calls.last.request.content)[2] == "nope_x"
    body = json.loads(muts.calls.last.request.content)
    assert body["entrezGeneIds"] == [3845]
    assert sorted(body["molecularProfileIds"]) == [
        "coadread_tcga_pan_can_atlas_2018_mutations", "luad_tcga_pan_can_atlas_2018_mutations",
    ]
    assert result.records[0].identifiers == {"entrez_gene": "3845", "gene_symbol": "KRAS"}
    rows = {r["study_id"]: r for r in result.data["studies"]}
    assert rows["luad_tcga_pan_can_atlas_2018"]["mutated_samples"] == 4
    assert rows["luad_tcga_pan_can_atlas_2018"]["profiled_samples"] == 566
    assert rows["luad_tcga_pan_can_atlas_2018"]["percent"] == 0.7
    assert rows["luad_tcga_pan_can_atlas_2018"]["top_protein_changes"][0] == "G12C (3)"
    assert "study nope_x not found" in result.notes


async def test_gene_alteration_frequency_caps_studies(make_registry):
    from mosaic.http import SourceError

    registry = make_registry("cbioportal")
    with pytest.raises(SourceError, match="invalid parameters"):
        await registry.call("cbioportal", "gene_alteration_frequency", {"gene": "KRAS", "study_ids": [f"s{i}" for i in range(11)]})


@respx.mock
async def test_mutations_in_gene(make_registry):
    respx.get(f"{API}/genes/KRAS").mock(return_value=fx("gene_kras.json"))
    respx.get(f"{API}/studies/coadread_tcga_pan_can_atlas_2018").mock(return_value=httpx.Response(200, json={"studyId": "coadread_tcga_pan_can_atlas_2018", "sequencedSampleCount": 534}))
    respx.post(f"{API}/molecular-profiles/fetch").mock(return_value=fx("molecular_profiles_fetch.json"))
    coad = [m for m in load_fixture("cbioportal", "mutations.json") if m["studyId"].startswith("coadread")]
    respx.post(f"{API}/mutations/fetch").mock(return_value=httpx.Response(200, json=coad))
    registry = make_registry("cbioportal")
    result = await registry.call("cbioportal", "mutations_in_gene", {"study_id": "coadread_tcga_pan_can_atlas_2018", "gene": "KRAS"})
    top = result.records[0]
    assert top.id == "KRAS G12D" and top.type == "variant"
    assert top.attributes["samples"] == 3
    assert top.attributes["mutation_type"] == "Missense_Mutation"
    assert top.identifiers == {"gene_symbol": "KRAS"} and top.attributes["entrez_gene"] == "3845"
    assert result.total == 2
    assert result.data["mutated_samples"] == 4 and result.data["profiled_samples"] == 534

    # each protein change stays its own entity, apart from the gene record
    gene = Record(source="hgnc", id="HGNC:6407", type="gene", title="KRAS",
                  identifiers={"hgnc": "HGNC:6407", "entrez_gene": "3845", "gene_symbol": "KRAS"})
    curated = curate([*result.records, gene])
    assert curated.stats["entities_out"] == len(result.records) + 1
    assert [e.type for e in curated.entities].count("gene") == 1


@respx.mock
async def test_mutations_in_unknown_gene(make_registry):
    respx.get(f"{API}/genes/NOTAGENE").mock(return_value=httpx.Response(404, json={"message": "Gene not found"}))
    registry = make_registry("cbioportal")
    result = await registry.call("cbioportal", "mutations_in_gene", {"study_id": "x", "gene": "notagene"})
    assert result.records == [] and "not found" in result.notes[0]


@pytest.mark.live
async def test_live_all_tools(live_registry):
    registry = live_registry("cbioportal")
    search = await registry.call("cbioportal", "search_studies", {"query": "breast", "limit": 3})
    assert search.records and search.total
    study = await registry.call("cbioportal", "get_study", {"study_id": "brca_tcga_pan_can_atlas_2018"})
    assert study.records[0].attributes["pmids"]
    freq = await registry.call(
        "cbioportal", "gene_alteration_frequency",
        {"gene": "PIK3CA", "study_ids": ["brca_tcga_pan_can_atlas_2018"]},
    )
    assert 20 < freq.data["studies"][0]["percent"] < 50
    muts = await registry.call("cbioportal", "mutations_in_gene", {"study_id": "brca_tcga_pan_can_atlas_2018", "gene": "PIK3CA", "limit": 3})
    assert muts.records[0].title.startswith("PIK3CA H1047")


def test_tcga_studies_link_to_gdc_projects():
    from mosaic.connectors.cbioportal import _gdc_project

    assert _gdc_project("skcm_tcga_gdc") == "TCGA-SKCM"
    assert _gdc_project("brca_tcga_pan_can_atlas_2018") == "TCGA-BRCA"
    assert _gdc_project("luad_tcga") == "TCGA-LUAD"
    assert _gdc_project("luad_broad") is None


def test_only_the_gdc_import_is_the_same_entity_as_its_gdc_project():
    from mosaic.connectors.cbioportal import study_record

    cohorts = [study_record({"studyId": sid, "name": sid}) for sid in (
        "brca_tcga_pan_can_atlas_2018", "brca_tcga_pub2015", "brca_tcga_firehose_legacy", "brca_tcga_gdc")]
    assert cohorts[0].identifiers == {"cbioportal": "brca_tcga_pan_can_atlas_2018"}
    assert cohorts[0].attributes["gdc_project"] == "TCGA-BRCA"  # related, not the same data
    assert cohorts[3].identifiers == {"cbioportal": "brca_tcga_gdc", "gdc_project": "TCGA-BRCA"}
    gdc = Record(source="nci_gdc", id="TCGA-BRCA", type="study", title="Breast Invasive Carcinoma",
                 identifiers={"gdc_project": "TCGA-BRCA"})
    curated = curate([*cohorts, gdc])
    assert curated.stats["entities_out"] == 4  # three cohorts apart; the GDC import merges with its project
    merged = next(e for e in curated.entities if len(e.sources) > 1)
    assert sorted(s["id"] for s in merged.sources) == ["TCGA-BRCA", "brca_tcga_gdc"]
