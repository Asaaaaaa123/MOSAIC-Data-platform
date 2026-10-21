"""NCI GDC connector tests."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from tests.conftest import load_fixture

API = "https://api.gdc.cancer.gov"


def fx(name):
    return httpx.Response(200, json=load_fixture("nci_gdc", name))


@respx.mock
async def test_search_projects_ranks_client_side(make_registry):
    route = respx.get(f"{API}/projects").mock(return_value=fx("projects.json"))
    registry = make_registry("nci_gdc")
    result = await registry.call("nci_gdc", "search_projects", {"query": "TCGA lung", "limit": 2})
    sent = route.calls.last.request.url.params
    assert sent["size"] == "200" and "summary.case_count" in sent["fields"]
    ids = [r.id for r in result.records]
    assert set(ids) == {"TCGA-LUAD", "TCGA-LUSC"}
    assert result.next_cursor == "2"  # other TCGA projects still match "tcga"
    rec = result.records[0]
    assert rec.type == "dataset"
    assert rec.identifiers["gdc_project"] == rec.id
    assert rec.identifiers["dbgap"] == "phs000178"
    assert rec.url == f"https://portal.gdc.cancer.gov/projects/{rec.id}"
    assert rec.attributes["case_count"] > 400


@respx.mock
async def test_search_projects_no_match(make_registry):
    respx.get(f"{API}/projects").mock(return_value=fx("projects.json"))
    registry = make_registry("nci_gdc")
    result = await registry.call("nci_gdc", "search_projects", {"query": "zebrafish"})
    assert result.records == [] and result.total == 0 and result.notes


@respx.mock
async def test_get_project(make_registry):
    route = respx.get(f"{API}/projects/TCGA-BRCA").mock(return_value=fx("project_brca.json"))
    registry = make_registry("nci_gdc")
    result = await registry.call("nci_gdc", "get_project", {"project_id": "tcga-brca"})
    assert "summary.data_categories" in route.calls.last.request.url.params["expand"]
    rec = result.records[0]
    assert rec.id == "TCGA-BRCA" and rec.title == "TCGA-BRCA: Breast Invasive Carcinoma"
    assert rec.attributes["primary_site"] == ["Breast"]
    assert rec.attributes["data_categories_cases"]["Simple Nucleotide Variation"] == 1098
    detail = {d["data_category"]: d for d in rec.attributes["data_categories_detail"]}
    assert detail["Clinical"]["files"] > 0


@respx.mock
async def test_get_project_not_found(make_registry):
    respx.get(f"{API}/projects/NOPE-X").mock(return_value=httpx.Response(404, json={"message": "NOPE-X not found"}))
    registry = make_registry("nci_gdc")
    result = await registry.call("nci_gdc", "get_project", {"project_id": "NOPE-X"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_gene_mutation_frequency(make_registry):
    genes = respx.post(f"{API}/genes").mock(return_value=fx("gene_tp53.json"))
    gql = respx.post(f"{API}/v0/graphql").mock(return_value=fx("graphql_gene_counts.json"))
    respx.get(f"{API}/projects").mock(return_value=fx("projects.json"))
    registry = make_registry("nci_gdc")
    result = await registry.call(
        "nci_gdc", "gene_mutation_frequency", {"gene": "tp53", "project_ids": ["TCGA-BRCA", "TCGA-LUAD", "TCGA-OV"]}
    )
    gene_body = json.loads(genes.calls.last.request.content)
    assert gene_body["filters"]["content"]["field"] == "symbol"
    assert gene_body["filters"]["content"]["value"] == ["TP53"]
    body = json.loads(gql.calls.last.request.content)
    mutated = json.dumps(body["variables"]["mutated"])
    assert "ENSG00000141510" in mutated and "MISSING" in mutated and "TCGA-OV" in mutated
    assert "genes.gene_id" not in json.dumps(body["variables"]["profiled"])

    gene = result.records[0]
    assert gene.type == "gene"
    assert gene.identifiers == {
        "gene_symbol": "TP53",
        "ensembl_gene": "ENSG00000141510",
        "entrez_gene": "7157",
        "hgnc": "HGNC:11998",
        "uniprot": "P04637",
    }
    rows = {r["project_id"]: r for r in result.data["projects"]}
    assert rows["TCGA-BRCA"]["mutated_cases"] == 331
    assert rows["TCGA-BRCA"]["ssm_cases"] == 969
    assert rows["TCGA-BRCA"]["percent"] == 34.2
    assert rows["TCGA-OV"]["percent"] == 87.6
    assert result.data["projects"][0]["project_id"] == "TCGA-OV"


@respx.mock
async def test_gene_mutation_frequency_unknown_gene(make_registry):
    respx.post(f"{API}/genes").mock(return_value=httpx.Response(200, json={"data": {"hits": []}}))
    registry = make_registry("nci_gdc")
    result = await registry.call("nci_gdc", "gene_mutation_frequency", {"gene": "NOTAGENE"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_top_mutated_genes(make_registry):
    gql = respx.post(f"{API}/v0/graphql").mock(return_value=fx("graphql_top_genes.json"))
    registry = make_registry("nci_gdc")
    result = await registry.call("nci_gdc", "top_mutated_genes", {"project_id": "TCGA-LUAD", "limit": 3})
    body = json.loads(gql.calls.last.request.content)
    assert body["variables"]["first"] == 3
    assert "TCGA-LUAD" in json.dumps(body["variables"]["genes"])
    assert [r.identifiers["gene_symbol"] for r in result.records] == ["TTN", "TP53", "MUC16"]
    tp53 = result.records[1]
    assert tp53.identifiers["ensembl_gene"] == "ENSG00000141510"
    assert tp53.attributes["mutated_cases"] == 281
    assert tp53.attributes["percent"] == 50.3
    assert result.data["ssm_cases"] == 559


@respx.mock
async def test_search_files_filters_and_pagination(make_registry):
    route = respx.post(f"{API}/files").mock(return_value=fx("files.json"))
    registry = make_registry("nci_gdc")
    result = await registry.call(
        "nci_gdc", "search_files",
        {"project_id": "TCGA-BRCA", "data_type": "Masked Somatic Mutation", "limit": 2, "cursor": "0"},
    )
    body = json.loads(route.calls.last.request.content)
    fields = {c["content"]["field"]: c["content"]["value"] for c in body["filters"]["content"]}
    assert fields == {
        "cases.project.project_id": ["TCGA-BRCA"],
        "data_type": ["Masked Somatic Mutation"],
        "access": ["open"],
    }
    assert body["size"] == 2 and body["from"] == 0
    assert result.total == 992 and result.next_cursor == "2"
    rec = result.records[0]
    assert rec.id == "081c1afc-ea50-4773-9402-dfc686603a16"
    assert rec.identifiers == {"gdc_project": "TCGA-BRCA"}
    assert rec.attributes["access"] == "open" and rec.attributes["data_format"] == "MAF"
    assert rec.date == "2022-08-01"


@pytest.mark.live
async def test_live_all_tools(live_registry):
    registry = live_registry("nci_gdc")
    projects = await registry.call("nci_gdc", "search_projects", {"query": "breast", "limit": 3})
    assert "TCGA-BRCA" in [r.id for r in projects.records]
    project = await registry.call("nci_gdc", "get_project", {"project_id": "TCGA-BRCA"})
    assert project.records[0].attributes["case_count"] > 1000
    freq = await registry.call("nci_gdc", "gene_mutation_frequency", {"gene": "KRAS", "limit": 5})
    assert freq.records[0].identifiers["entrez_gene"] == "3845" and freq.data["projects"]
    top = await registry.call("nci_gdc", "top_mutated_genes", {"project_id": "TCGA-LUAD", "limit": 5})
    assert "TP53" in [r.identifiers["gene_symbol"] for r in top.records]
    files = await registry.call("nci_gdc", "search_files", {"project_id": "TCGA-BRCA", "data_category": "Clinical", "limit": 2})
    assert files.records and files.total
