"""Alliance of Genome Resources connector tests."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mosaic.connectors.alliance import gene_identifiers
from tests.conftest import load_fixture

API = "https://www.alliancegenome.org/api"


def _ok(name: str) -> httpx.Response:
    return httpx.Response(200, json=load_fixture("alliance", name))


EMPTY = {"total": 0, "returnedRecords": 0}


def test_gene_identifiers_drop_foreign_mod_and_omim():
    ids = gene_identifiers(
        "MGI:97490", ["RGD:3258", "OMIM:607108", "NCBI_Gene:18508", "ENSEMBL:ENSMUSG00000027168"], symbol="Pax6"
    )
    assert ids == {"mgi": "MGI:97490", "entrez_gene": "18508", "ensembl_gene": "ENSMUSG00000027168"}
    human = gene_identifiers("HGNC:8620", ["OMIM:607108", "RGD:737459"], symbol="PAX6", uniprot="UniProtKB:P26367")
    assert human == {"hgnc": "HGNC:8620", "omim": "607108", "uniprot": "P26367", "gene_symbol": "PAX6"}


@respx.mock
async def test_search_genes(make_registry):
    route = respx.get(f"{API}/search").mock(return_value=_ok("search_gene.json"))
    registry = make_registry("alliance")
    result = await registry.call("alliance", "search", {"query": "pax6", "limit": 2, "species": "Homo sapiens"})
    sent = route.calls.last.request.url.params
    assert sent["q"] == "pax6" and sent["category"] == "gene_search_result"
    assert sent["limit"] == "2" and sent["offset"] == "0" and sent["species"] == "Homo sapiens"
    assert result.total == 22 and result.next_cursor == "2"
    rat, human = result.records
    assert rat.id == "RGD:3258" and rat.type == "gene" and rat.title == "Pax6"
    assert rat.identifiers == {
        "rgd": "RGD:3258",
        "entrez_gene": "25509",
        "ensembl_gene": "ENSRNOG00000004410",
        "panther": "PTHR45636",
    }
    assert "gene_symbol" not in rat.identifiers  # only human symbols are canonical gene_symbol
    assert rat.attributes["species"] == "Rattus norvegicus"
    assert human.identifiers["hgnc"] == "HGNC:8620" and human.identifiers["gene_symbol"] == "PAX6"
    assert "uniprot" not in human.identifiers  # unordered TrEMBL xrefs are kept out of identifiers
    assert human.url == "https://www.alliancegenome.org/gene/HGNC:8620"
    assert human.summary.startswith("Enables DNA-binding")
    assert "genes" not in human.attributes


@respx.mock
async def test_search_diseases_maps_xrefs(make_registry):
    route = respx.get(f"{API}/search").mock(return_value=_ok("search_disease.json"))
    registry = make_registry("alliance")
    result = await registry.call("alliance", "search", {"query": "aniridia", "category": "disease", "limit": 1})
    assert route.calls.last.request.url.params["category"] == "disease_search_result"
    assert result.total == 4 and result.next_cursor == "1"
    disease = result.records[0]
    assert disease.id == "DOID:12271" and disease.type == "disease" and disease.title == "aniridia"
    ids = disease.identifiers
    assert ids["doid"] == "DOID:12271" and ids["mesh"] == "D015783" and ids["umls"] == "C0003076"
    assert ids["orphanet"] == "250923"
    assert "omim" not in ids  # MIM:PS106210 is a phenotypic series, not an OMIM entry
    assert "ELP4 (Hsa)" in disease.attributes["genes"]


@respx.mock
async def test_get_gene(make_registry):
    route = respx.get(f"{API}/gene/HGNC:8620").mock(return_value=_ok("gene.json"))
    registry = make_registry("alliance")
    result = await registry.call("alliance", "get_gene", {"gene_id": " HGNC:8620 "})
    assert route.called
    gene = result.records[0]
    assert gene.id == "HGNC:8620" and gene.title == "PAX6"
    assert gene.identifiers == {
        "hgnc": "HGNC:8620",
        "ensembl_gene": "ENSG00000007372",
        "entrez_gene": "5080",
        "omim": "607108",
        "panther": "PTHR45636",
        "uniprot": "P26367",
        "gene_symbol": "PAX6",
    }
    attrs = gene.attributes
    assert attrs["location"] == {
        "assembly": "GRCh38", "chromosome": "11", "start": 31784779, "end": 31817967, "strand": "-"
    }
    assert attrs["canonical_protein"] == "UniProtKB:P26367"
    assert "P26367" not in attrs["other_uniprot"]
    assert attrs["species"] == "Homo sapiens" and attrs["biotype"] == "protein_coding_gene"
    assert gene.summary.startswith("This gene encodes paired box protein Pax-6")


@respx.mock
async def test_get_gene_not_found(make_registry):
    respx.get(f"{API}/gene/HGNC:0000000").mock(return_value=httpx.Response(400, json={"errors": ["No gene found"]}))
    registry = make_registry("alliance")
    result = await registry.call("alliance", "get_gene", {"gene_id": "HGNC:0000000"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_orthologs(make_registry):
    route = respx.get(f"{API}/gene/HGNC:8620/orthologs").mock(return_value=_ok("orthologs.json"))
    registry = make_registry("alliance")
    result = await registry.call("alliance", "orthologs", {"gene_id": "HGNC:8620", "limit": 2, "stringency": "all"})
    sent = route.calls.last.request.url.params
    assert sent["filter.stringency"] == "all" and sent["limit"] == "2" and sent["page"] == "1"
    assert result.total == 9 and result.next_cursor == "2"
    frog = result.records[0]
    assert frog.id == "Xenbase:XB-GENE-865046" and frog.title == "pax6.L"
    assert frog.identifiers == {"xenbase": "Xenbase:XB-GENE-865046"}
    assert frog.attributes["ortholog_of"] == "HGNC:8620" and frog.attributes["ortholog_of_symbol"] == "PAX6"
    assert frog.attributes["best_score"] == "Yes" and frog.attributes["methods_matched_count"] >= 1


@respx.mock
async def test_orthologs_empty(make_registry):
    respx.get(f"{API}/gene/HGNC:0000000/orthologs").mock(return_value=httpx.Response(200, json=EMPTY))
    registry = make_registry("alliance")
    result = await registry.call("alliance", "orthologs", {"gene_id": "HGNC:0000000"})
    assert result.records == [] and result.next_cursor is None and "no stringent orthologs" in result.notes[0]


@respx.mock
async def test_gene_diseases_for_gene(make_registry):
    route = respx.post(f"{API}/disease").mock(return_value=_ok("gene_diseases.json"))
    registry = make_registry("alliance")
    result = await registry.call("alliance", "gene_diseases", {"entity_id": "HGNC:8620", "limit": 2, "cursor": "1"})
    request = route.calls.last.request
    assert json.loads(request.content) == ["HGNC:8620"]
    assert request.url.params["limit"] == "2" and request.url.params["page"] == "1"
    assert result.total == 17 and result.next_cursor == "2"
    assoc = result.records[0]
    assert assoc.type == "association"
    assert assoc.title == "PAX6 is implicated in aniridia"
    assert assoc.identifiers == {"doid": "DOID:12271"}  # the listed partner; the paper is only related
    assert "25366758" in assoc.attributes["pmids"]
    assert assoc.url == "https://www.alliancegenome.org/disease/DOID:12271"
    assert assoc.attributes["relation"] == "is_implicated_in"
    assert assoc.attributes["evidence_codes"] == ["IAGP"]
    assert "25366758" in assoc.attributes["pmids"]
    assert "providers" not in assoc.attributes  # empty provider objects are dropped, not [None]


@respx.mock
async def test_gene_diseases_for_disease(make_registry):
    route = respx.get(f"{API}/disease/DOID:12271/genes").mock(return_value=_ok("disease_genes.json"))
    registry = make_registry("alliance")
    result = await registry.call("alliance", "gene_diseases", {"entity_id": "doid:12271", "limit": 2})
    assert route.calls.last.request.url.params["limit"] == "2"
    assert result.total == 50 and result.next_cursor == "2"
    elp4 = result.records[0]
    assert elp4.identifiers == {"hgnc": "HGNC:1171", "gene_symbol": "ELP4"}
    assert elp4.url == "https://www.alliancegenome.org/gene/HGNC:1171"
    assert elp4.attributes["disease"] == "DOID:12271" and elp4.attributes["species"] == "Homo sapiens"


@respx.mock
async def test_gene_diseases_empty(make_registry):
    respx.get(f"{API}/disease/DOID:99999999/genes").mock(return_value=httpx.Response(200, json=EMPTY))
    registry = make_registry("alliance")
    result = await registry.call("alliance", "gene_diseases", {"entity_id": "DOID:99999999"})
    assert result.records == [] and "no disease annotations" in result.notes[0]


@respx.mock
async def test_gene_models(make_registry):
    route = respx.get(f"{API}/gene/MGI:97490/models").mock(return_value=_ok("models.json"))
    registry = make_registry("alliance")
    result = await registry.call("alliance", "gene_models", {"gene_id": "MGI:97490", "limit": 2})
    assert route.calls.last.request.url.params["page"] == "1"
    assert result.total == 183 and result.next_cursor == "2"
    model = result.records[0]
    assert model.id == "MGI:2680573"
    assert model.title.startswith("Pax6<1Jrt>/Pax6<+>")  # <sup> markup becomes MGI angle brackets
    assert model.identifiers == {"mgi": "MGI:2680573"}  # the modelled disease is a related entity
    assert model.url == "https://www.alliancegenome.org/model/MGI:2680573"
    assert model.attributes["disease_models"][0]["disease"] == "Peters anomaly"
    assert "microphthalmia" in model.attributes["phenotypes"]


@respx.mock
async def test_gene_phenotypes(make_registry):
    route = respx.get(f"{API}/gene/MGI:97490/phenotypes").mock(return_value=_ok("phenotypes.json"))
    registry = make_registry("alliance")
    result = await registry.call("alliance", "gene_phenotypes", {"gene_id": "MGI:97490", "limit": 2, "cursor": "3"})
    assert route.calls.last.request.url.params["page"] == "3"
    assert result.total == 184 and result.next_cursor == "4"
    pheno = result.records[0]
    assert pheno.type == "phenotype"
    assert pheno.title == "abnormal anterior eye segment morphology"
    assert "pmid" not in pheno.identifiers  # supporting papers are related entities
    assert pheno.attributes["pmids"] == ["11481423", "19563798"]
    assert pheno.attributes["species"] == "Mus musculus"


@pytest.mark.live
async def test_live_alliance_all_tools(live_registry):
    registry = live_registry("alliance")
    search = await registry.call("alliance", "search", {"query": "pax6", "limit": 5})
    assert search.records and search.total
    assert any(r.identifiers.get("hgnc") == "HGNC:8620" for r in search.records)
    disease = await registry.call("alliance", "search", {"query": "aniridia", "category": "disease", "limit": 3})
    assert any(r.identifiers.get("doid") == "DOID:12271" for r in disease.records)
    gene = await registry.call("alliance", "get_gene", {"gene_id": "HGNC:8620"})
    assert gene.records[0].identifiers["entrez_gene"] == "5080"
    assert gene.records[0].identifiers["ensembl_gene"] == "ENSG00000007372"
    orth = await registry.call("alliance", "orthologs", {"gene_id": "HGNC:8620", "limit": 20})
    assert any(r.identifiers.get("mgi") == "MGI:97490" for r in orth.records)
    by_gene = await registry.call("alliance", "gene_diseases", {"entity_id": "HGNC:8620", "limit": 5})
    assert by_gene.records and all(r.attributes.get("disease", "").startswith("DOID:") for r in by_gene.records)
    by_disease = await registry.call("alliance", "gene_diseases", {"entity_id": "DOID:12271", "limit": 5})
    assert by_disease.records and by_disease.total
    models = await registry.call("alliance", "gene_models", {"gene_id": "MGI:97490", "limit": 3})
    assert models.records and models.records[0].id.startswith("MGI:")
    phenos = await registry.call("alliance", "gene_phenotypes", {"gene_id": "MGI:97490", "limit": 3})
    assert phenos.records and phenos.total
