"""Ensembl REST connector tests (lookup + Compara orthologues)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mosaic.connectors.ensembl import alignment_coverage, cigar_columns
from mosaic.http import SourceError
from tests.conftest import load_fixture

API = "https://rest.ensembl.org"
HOMOLOGY = f"{API}/homology/id/homo_sapiens/ENSG00000133703"


def test_cigar_expansion_and_coverage():
    assert cigar_columns("3MD2M") == "MMMDMM"
    assert cigar_columns("188M") == "M" * 188
    assert alignment_coverage("188M", "188M") == 100.0
    # human 15 gaps then 188 residues; ortholog 193 residues, 2 gaps, 8 residues -> 186 of 188 aligned
    assert alignment_coverage("15D188M", "193M2D8M") == 98.94
    assert alignment_coverage("10M", "5M") is None  # inconsistent CIGARs
    assert alignment_coverage(None, None) is None


@respx.mock
async def test_get_gene_by_symbol(make_registry):
    route = respx.get(f"{API}/lookup/symbol/homo_sapiens/KRAS").mock(
        return_value=httpx.Response(200, json=load_fixture("ensembl", "lookup_kras.json"))
    )
    registry = make_registry("ensembl")
    result = await registry.call("ensembl", "get_gene", {"gene": "KRAS"})
    assert route.called
    assert route.calls.last.request.headers["accept"] == "application/json"
    gene = result.records[0]
    assert gene.id == "ENSG00000133703"
    assert gene.identifiers["ensembl_gene"] == "ENSG00000133703"
    assert gene.identifiers["gene_symbol"] == "KRAS"
    assert gene.identifiers["hgnc"] == "HGNC:6407"
    assert gene.summary == "KRAS proto-oncogene, GTPase"
    assert gene.attributes["species"] == "Homo sapiens"
    assert gene.attributes["location"].startswith("12:25205246-25326473")
    assert gene.url == "https://www.ensembl.org/Homo_sapiens/Gene/Summary?g=ENSG00000133703"


@respx.mock
async def test_get_gene_not_found_is_a_note(make_registry):
    respx.get(f"{API}/lookup/symbol/homo_sapiens/NOPE1").mock(
        return_value=httpx.Response(400, json={"error": "No valid lookup found for symbol NOPE1"})
    )
    registry = make_registry("ensembl")
    result = await registry.call("ensembl", "get_gene", {"gene": "NOPE1"})
    assert result.records == []
    assert "no Ensembl gene found" in result.notes[0]


@respx.mock
async def test_orthologs_symbol_is_resolved_then_one_request_with_repeated_species(make_registry):
    respx.get(f"{API}/lookup/symbol/homo_sapiens/KRAS").mock(
        return_value=httpx.Response(200, json=load_fixture("ensembl", "lookup_kras.json"))
    )
    homology = respx.get(HOMOLOGY).mock(return_value=httpx.Response(200, json=load_fixture("ensembl", "homology_kras.json")))
    symbols = respx.post(f"{API}/lookup/id").mock(
        return_value=httpx.Response(200, json=load_fixture("ensembl", "lookup_ids.json"))
    )
    registry = make_registry("ensembl")
    result = await registry.call(
        "ensembl", "orthologs",
        {"gene": "KRAS", "target_species": ["mus_musculus", "danio_rerio", "xenopus_tropicalis"]},
    )
    sent = homology.calls.last.request.url.params
    assert sent.get_list("target_species") == ["mus_musculus", "danio_rerio", "xenopus_tropicalis"]
    assert sent["type"] == "orthologues" and sent["sequence"] == "none"
    assert "compara" not in sent
    assert json.loads(symbols.calls.last.request.content)["ids"][0] == "ENSMUSG00000030265"

    by_id = {r.id: r for r in result.records}
    mouse = by_id["ENSMUSG00000030265"]
    assert mouse.title == "Kras"
    assert mouse.attributes["species"] == "Mus musculus"
    assert mouse.attributes["relationship"] == "ortholog_one2one"
    assert mouse.attributes["perc_id"] == 97.34  # relative to the human protein
    assert mouse.attributes["perc_pos"] == 99.47
    assert mouse.attributes["coverage_pct"] == 100.0
    assert mouse.attributes["ortholog_of_symbol"] == "KRAS"
    assert mouse.identifiers == {"ensembl_gene": "ENSMUSG00000030265"}
    frog = by_id["ENSXETG00000003398"]
    assert frog.attributes["relationship"] == "ortholog_one2many"
    assert frog.attributes["coverage_pct"] == 98.94
    assert frog.attributes["target_perc_id"] == 86.07  # relative to the frog protein
    assert by_id["ENSXETG00000043145"].title == "ENSXETG00000043145"  # no symbol returned
    assert result.data["query_gene"] == "ENSG00000133703"
    assert len(result.data["orthologs"]) == 4


@respx.mock
async def test_orthologs_by_id_pan_homology_and_symbol_failure_is_a_note(make_registry):
    homology = respx.get(HOMOLOGY).mock(return_value=httpx.Response(200, json={"data": [{"id": "ENSG00000133703", "homologies": [{
        "type": "ortholog_many2many", "taxonomy_level": "cellular organisms",
        "source": {"id": "ENSG00000133703", "species": "homo_sapiens", "perc_id": 59.5745, "perc_pos": 73.9362, "cigar_line": "7D120M"},
        "target": {"id": "YNL098C", "species": "saccharomyces_cerevisiae", "perc_id": 34.78, "perc_pos": 43.17,
                   "cigar_line": "127M", "taxon_id": 559292},
    }]}]}))
    respx.post(f"{API}/lookup/id").mock(return_value=httpx.Response(503, text="busy"))
    registry = make_registry("ensembl")
    result = await registry.call("ensembl", "orthologs", {
        "gene": "ENSG00000133703.16", "target_species": ["saccharomyces_cerevisiae"], "compara": "pan_homology", "with_symbols": True,
    })
    assert homology.calls.last.request.url.params["compara"] == "pan_homology"
    yeast = result.records[0]
    assert yeast.id == "YNL098C"
    assert yeast.identifiers == {}  # not an Ensembl-style gene id
    assert yeast.attributes["species"] == "Saccharomyces cerevisiae"
    assert yeast.attributes["perc_id"] == 59.57
    assert yeast.url.startswith("https://www.ensembl.org/Saccharomyces_cerevisiae/")
    assert any("symbol lookup failed" in n for n in result.notes)


@respx.mock
async def test_orthologs_upstream_error_raises(make_registry):
    respx.get(HOMOLOGY).mock(return_value=httpx.Response(500, text="Internal Server Error"))
    registry = make_registry("ensembl")
    with pytest.raises(SourceError) as info:
        await registry.call("ensembl", "orthologs", {"gene": "ENSG00000133703"})
    assert info.value.status == 500


@respx.mock
async def test_orthologs_unknown_gene_is_a_note(make_registry):
    respx.get(f"{API}/lookup/symbol/homo_sapiens/NOPE1").mock(return_value=httpx.Response(400, json={"error": "No valid lookup"}))
    registry = make_registry("ensembl")
    result = await registry.call("ensembl", "orthologs", {"gene": "NOPE1"})
    assert result.records == [] and "no Ensembl gene found" in result.notes[0]


@pytest.mark.live
async def test_live_ensembl_kras_orthologs(live_registry):
    registry = live_registry("ensembl")
    gene = await registry.call("ensembl", "get_gene", {"gene": "KRAS"})
    assert gene.records[0].id == "ENSG00000133703"
    result = await registry.call(
        "ensembl", "orthologs", {"gene": "ENSG00000133703", "target_species": ["mus_musculus", "danio_rerio"]}, timeout=120
    )
    mouse = [r for r in result.records if r.attributes.get("ensembl_species") == "mus_musculus"]
    assert mouse and mouse[0].title == "Kras"
    assert 95 <= mouse[0].attributes["perc_id"] <= 100
