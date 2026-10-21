"""BindingDB connector tests (offline respx mocks + live checks)."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.connectors.bindingdb import parse_affinity
from mosaic.http import SourceError
from tests.conftest import load_fixture

API = "https://bindingdb.org/rest"


def fx(name: str):
    return load_fixture("bindingdb", name)


def test_parse_affinity():
    assert parse_affinity("0.006") == ("=", 0.006)
    assert parse_affinity(">10000") == (">", 10000.0)
    assert parse_affinity(" 6.80") == ("=", 6.8)
    assert parse_affinity("<=1") == ("<", 1.0)
    assert parse_affinity("n/a") == ("=", None)


@respx.mock
async def test_ligands_for_target_groups_by_monomer(make_registry):
    route = respx.get(f"{API}/getLigandsByUniprots").mock(return_value=httpx.Response(200, json=fx("ligands_uniprot.json")))
    registry = make_registry("bindingdb")
    result = await registry.call("bindingdb", "ligands_for_target", {"uniprot": "p00533", "cutoff_nm": 0.01, "limit": 2})

    sent = route.calls.last.request.url.params
    assert sent["uniprot"] == "P00533" and sent["cutoff"] == "0.01" and sent["response"] == "application/json"
    # 7 measurement rows -> 5 distinct ligands, most potent first
    assert result.total == 5 and result.next_cursor == "2"
    assert result.data == {"measurements": 7, "ligands": 5}
    first = result.records[0]
    assert first.id == "3032" and first.type == "compound"
    assert first.identifiers == {"bindingdb": "3032", "smiles": "COc1cc2ncnc(Nc3cccc(Br)c3)c2cc1OC"}
    assert first.attributes["best_affinity_nM"] == 0.006 and first.attributes["best_affinity_type"] == "Ki"
    assert first.attributes["pmids"] == ["18077363"] and first.attributes["dois"]
    assert first.attributes["query_uniprot"] == ["P00533"]
    multi = result.records[1]
    assert multi.id == "717507" and multi.attributes["n_measurements"] == 3
    assert multi.attributes["best_affinity_nM"] == 0.007


@respx.mock
async def test_ligands_for_target_paging_and_filter(make_registry):
    respx.get(f"{API}/getLigandsByUniprots").mock(return_value=httpx.Response(200, json=fx("ligands_uniprot.json")))
    registry = make_registry("bindingdb")
    page2 = await registry.call("bindingdb", "ligands_for_target", {"uniprot": "P00533", "limit": 2, "cursor": "4"})
    assert len(page2.records) == 1 and page2.next_cursor is None
    ki = await registry.call("bindingdb", "ligands_for_target", {"uniprot": "P00533", "affinity_type": "Ki"})
    assert [r.id for r in ki.records] == ["3032"]


@respx.mock
async def test_ligands_for_target_empty(make_registry):
    respx.get(f"{API}/getLigandsByUniprots").mock(return_value=httpx.Response(200, json=fx("empty_uniprot.json")))
    registry = make_registry("bindingdb")
    result = await registry.call("bindingdb", "ligands_for_target", {"uniprot": "A0A000"})
    assert result.records == [] and result.total == 0 and "no BindingDB measurements" in result.notes[0]


@respx.mock
async def test_ligands_for_target_blank_body(make_registry):
    respx.get(f"{API}/getLigandsByUniprots").mock(return_value=httpx.Response(200, text=""))
    registry = make_registry("bindingdb")
    result = await registry.call("bindingdb", "ligands_for_target", {"uniprot": "P00533"})
    assert result.records == [] and result.notes


async def test_invalid_accession_rejected(make_registry):
    registry = make_registry("bindingdb")
    with pytest.raises(SourceError, match="UniProt accessions"):
        await registry.call("bindingdb", "ligands_for_target", {"uniprot": "EGFR"})
    with pytest.raises(SourceError, match="PDB IDs"):
        await registry.call("bindingdb", "ligands_for_pdb", {"pdb": "1M17; DROP TABLE"})


@respx.mock
async def test_ligands_for_pdb(make_registry):
    route = respx.get(f"{API}/getLigandsByPDBs").mock(return_value=httpx.Response(200, json=fx("ligands_pdb.json")))
    registry = make_registry("bindingdb")
    result = await registry.call("bindingdb", "ligands_for_pdb", {"pdb": "1m17", "cutoff_nm": 0.01, "identity": 90})
    sent = route.calls.last.request.url.params
    assert sent["pdb"] == "1M17" and sent["identity"] == "90" and sent["cutoff"] == "0.01"
    assert result.total == 4
    assert result.records[0].id == "3032" and result.records[0].attributes["query_pdb"] == ["1M17"]


@respx.mock
async def test_ligands_for_pdb_server_error_is_note(make_registry):
    respx.get(f"{API}/getLigandsByPDBs").mock(return_value=httpx.Response(500, text="<html>bad SQL grammar</html>"))
    registry = make_registry("bindingdb")
    result = await registry.call("bindingdb", "ligands_for_pdb", {"pdb": "9ZZZ"})
    assert result.records == [] and "HTTP 500" in result.notes[0] and len(result.notes) == 1


@respx.mock
async def test_targets_for_compound(make_registry):
    route = respx.get(f"{API}/getTargetByCompound").mock(return_value=httpx.Response(200, json=fx("targets_compound.json")))
    registry = make_registry("bindingdb")
    smiles = "Cc1ccc(NC(=O)c2ccc(CN3CCN(C)CC3)cc2)cc1Nc1nccc(-c2cccnc2)n1"
    result = await registry.call("bindingdb", "targets_for_compound", {"smiles": smiles, "similarity": 0.9})
    sent = route.calls.last.request.url.params
    assert sent["smiles"] == smiles and sent["cutoff"] == "0.9"
    first = result.records[0]
    assert first.type == "target" and first.attributes["target_name"].startswith("Tyrosine-protein kinase ABL1")
    assert first.attributes["best_affinity_nM"] == 2.0 and first.attributes["species"] == "Human"
    assert first.attributes["best_compound_bindingdb"] == "13530"
    assert first.identifiers == {}
    # censored (>10000) targets sort last
    assert result.records[-1].attributes["best_affinity_relation"] == ">"
    potent = await registry.call("bindingdb", "targets_for_compound", {"smiles": smiles, "max_affinity_nm": 10})
    assert all(r.attributes["best_affinity_nM"] <= 10 for r in potent.records)
    assert potent.total < result.total


# ------------------------------------------------------------------ live
@pytest.mark.live
async def test_live_ligands(live_registry):
    registry = live_registry("bindingdb")
    by_uniprot = await registry.call("bindingdb", "ligands_for_target", {"uniprot": "P00533", "cutoff_nm": 0.1, "limit": 5})
    assert by_uniprot.records and by_uniprot.records[0].attributes["best_affinity_nM"] <= 0.1
    by_pdb = await registry.call("bindingdb", "ligands_for_pdb", {"pdb": "1M17", "cutoff_nm": 0.1, "limit": 5})
    assert by_pdb.records


@pytest.mark.live
async def test_live_targets_for_compound(live_registry):
    registry = live_registry("bindingdb")
    result = await registry.call(
        "bindingdb", "targets_for_compound",
        {"smiles": "Cc1ccc(NC(=O)c2ccc(CN3CCN(C)CC3)cc2)cc1Nc1nccc(-c2cccnc2)n1", "similarity": 0.9, "max_affinity_nm": 100},
    )
    assert any("ABL" in (r.title or "") for r in result.records)
