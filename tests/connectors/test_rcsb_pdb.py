"""RCSB PDB connector tests (offline respx mocks + live)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mosaic.http import SourceError
from tests.conftest import load_fixture

SEARCH = "https://search.rcsb.org/rcsbsearch/v2/query"
GRAPHQL = "https://data.rcsb.org/graphql"


def _body(call) -> dict:
    return json.loads(call.request.content)


@respx.mock
async def test_search_structures_normalizes_records(make_registry):
    search = respx.post(SEARCH).mock(return_value=httpx.Response(200, json=load_fixture("rcsb_pdb", "search.json")))
    graphql = respx.post(GRAPHQL).mock(
        return_value=httpx.Response(200, json=load_fixture("rcsb_pdb", "entries_search.json"))
    )
    registry = make_registry("rcsb_pdb")
    result = await registry.call("rcsb_pdb", "search_structures", {"query": "EGFR kinase osimertinib", "limit": 2})

    body = _body(search.calls.last)
    assert body["query"] == {"type": "terminal", "service": "full_text", "parameters": {"value": "EGFR kinase osimertinib"}}
    assert body["return_type"] == "entry"
    assert body["request_options"]["paginate"] == {"start": 0, "rows": 2}
    assert body["request_options"]["results_content_type"] == ["experimental"]
    assert "sort" not in body["request_options"]  # relevance by default
    assert _body(graphql.calls.last)["variables"] == {"ids": ["6LUD", "7JXP"]}

    assert result.total == 31
    assert result.next_cursor == "2"
    first = result.records[0]
    assert first.id == "6LUD" and first.type == "structure"
    assert first.title.startswith("Crystal Structure of EGFR(L858R/T790M/C797S)")
    assert first.url == "https://www.rcsb.org/structure/6LUD"
    assert first.date == "2020-10-07"
    assert first.identifiers == {
        "pdb": "6LUD",
        "uniprot": "P00533",
        "pmid": "32943545",
        "doi": "10.1158/1535-7163.mct-20-0229",
    }
    assert first.authors[0] == "Kawauchi, H."
    assert "<i>" not in first.summary
    attrs = first.attributes
    assert attrs["method"] == "X-ray" and attrs["resolution_angstrom"] == 2.05
    assert attrs["ligands"][0]["id"] == "YY3"
    assert attrs["ligands"][0]["inchikey"] == "DUYJMQONPNNFPI-UHFFFAOYSA-N"
    assert attrs["ligands"][0]["drugbank"] == "DB09330"
    assert attrs["polymer_entities"][0]["uniprot"] == ["P00533"]
    second = result.records[1]
    assert "pmid" not in second.identifiers and second.identifiers["doi"] == "10.1038/s41467-022-30258-y"


@respx.mock
async def test_search_structures_filters_sort_and_empty_204(make_registry):
    search = respx.post(SEARCH).mock(return_value=httpx.Response(204))
    graphql = respx.post(GRAPHQL)
    registry = make_registry("rcsb_pdb")
    result = await registry.call(
        "rcsb_pdb",
        "search_structures",
        {"query": "zzz", "experimental_method": "EM", "max_resolution": 3.0, "sort": "release_date", "cursor": "20"},
    )
    body = _body(search.calls.last)
    nodes = body["query"]["nodes"]
    assert body["query"]["logical_operator"] == "and"
    assert nodes[1]["parameters"] == {"attribute": "rcsb_entry_info.experimental_method", "operator": "exact_match", "value": "EM"}
    assert nodes[2]["parameters"]["operator"] == "less_or_equal" and nodes[2]["parameters"]["value"] == 3.0
    assert body["request_options"]["paginate"]["start"] == 20
    assert body["request_options"]["sort"] == [{"sort_by": "rcsb_accession_info.initial_release_date", "direction": "desc"}]
    assert result.records == [] and result.total == 0 and result.next_cursor is None
    assert not graphql.called


@respx.mock
async def test_get_entry(make_registry):
    graphql = respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=load_fixture("rcsb_pdb", "entry_6M0J.json")))
    registry = make_registry("rcsb_pdb")
    result = await registry.call("rcsb_pdb", "get_entry", {"pdb_id": "pdb_00006m0j"})
    assert _body(graphql.calls.last)["variables"] == {"ids": ["6M0J"]}
    record = result.records[0]
    assert record.id == "6M0J"
    assert record.identifiers["pmid"] == "32225176"
    assert record.identifiers["doi"] == "10.1038/s41586-020-2180-5"
    # entities are ordered by entity number: ACE2 (entity 1) before the spike RBD (entity 2)
    assert [e["entity"] for e in record.attributes["polymer_entities"]] == ["6M0J_1", "6M0J_2"]
    assert record.identifiers["uniprot"] == "Q9BYF1"
    assert set(record.attributes["uniprot_ids"]) == {"Q9BYF1", "P0DTC2"}
    assert "Homo sapiens" in record.attributes["organisms"]
    # trivial ions sort after real ligands
    assert record.attributes["ligands"][0]["id"] == "NAG"


@respx.mock
async def test_get_entry_not_found_and_invalid(make_registry):
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json={"data": {"entries": []}}))
    registry = make_registry("rcsb_pdb")
    missing = await registry.call("rcsb_pdb", "get_entry", {"pdb_id": "0QQQ"})
    assert missing.records == [] and "not found" in missing.notes[0]
    invalid = await registry.call("rcsb_pdb", "get_entry", {"pdb_id": "EGFR kinase"})
    assert invalid.records == [] and "not a valid PDB ID" in invalid.notes[0]


@respx.mock
async def test_structures_for_uniprot(make_registry):
    search = respx.post(SEARCH).mock(
        return_value=httpx.Response(200, json=load_fixture("rcsb_pdb", "uniprot_search.json"))
    )
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=load_fixture("rcsb_pdb", "entries_uniprot.json")))
    registry = make_registry("rcsb_pdb")
    result = await registry.call("rcsb_pdb", "structures_for_uniprot", {"uniprot": "q9byf1", "limit": 2})
    body = _body(search.calls.last)
    accession_node, db_node = body["query"]["nodes"]
    assert accession_node["parameters"]["value"] == "Q9BYF1"
    assert accession_node["parameters"]["attribute"].endswith("reference_sequence_identifiers.database_accession")
    assert db_node["parameters"]["value"] == "UniProt"
    assert body["request_options"]["sort"] == [{"sort_by": "rcsb_entry_info.resolution_combined", "direction": "asc"}]
    assert result.total == 359 and result.next_cursor == "2"
    assert [r.id for r in result.records] == ["9SPA", "28KD"]
    # the queried accession becomes the record's uniprot identifier (links structure -> protein)
    assert all(r.identifiers["uniprot"] == "Q9BYF1" for r in result.records)


@respx.mock
async def test_structures_for_ligand_resolves_inchikey(make_registry):
    search = respx.post(SEARCH).mock(
        side_effect=[
            httpx.Response(200, json=load_fixture("rcsb_pdb", "chem_search.json")),
            httpx.Response(200, json=load_fixture("rcsb_pdb", "ligand_search.json")),
        ]
    )
    respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=load_fixture("rcsb_pdb", "entries_ligand.json")))
    registry = make_registry("rcsb_pdb")
    result = await registry.call(
        "rcsb_pdb", "structures_for_ligand", {"ligand": "duyjmqonpnnfpi-uhfffaoysa-n", "limit": 2}
    )
    chem = _body(search.calls[0])
    assert chem["query"]["service"] == "text_chem"
    assert chem["query"]["parameters"]["value"] == "DUYJMQONPNNFPI-UHFFFAOYSA-N"
    assert chem["return_type"] == "mol_definition"
    entry_query = _body(search.calls[1])
    assert entry_query["query"]["parameters"] == {
        "attribute": "rcsb_nonpolymer_entity_container_identifiers.nonpolymer_comp_id",
        "operator": "exact_match",
        "value": "YY3",
    }
    assert result.data == {"chem_comp_ids": ["YY3"]}
    assert result.total == 10 and [r.id for r in result.records] == ["7K1H", "7JXM"]
    assert "resolved" in result.notes[0]


@respx.mock
async def test_structures_for_ligand_comp_id_and_unknown(make_registry):
    search = respx.post(SEARCH).mock(return_value=httpx.Response(204))
    registry = make_registry("rcsb_pdb")
    result = await registry.call("rcsb_pdb", "structures_for_ligand", {"ligand": "sti"})
    assert _body(search.calls.last)["query"]["parameters"]["value"] == "STI"
    assert result.records == [] and result.total == 0
    unknown = await registry.call("rcsb_pdb", "structures_for_ligand", {"ligand": "DB99999"})
    assert unknown.records == [] and "no PDB chemical component" in unknown.notes[0]
    with pytest.raises(SourceError, match="chemical component ID"):
        await registry.call("rcsb_pdb", "structures_for_ligand", {"ligand": "imatinib mesylate"})


@pytest.mark.live
async def test_live_rcsb_all_tools(live_registry):
    registry = live_registry("rcsb_pdb")
    search = await registry.call("rcsb_pdb", "search_structures", {"query": "BRAF V600E inhibitor", "limit": 3})
    assert search.records and search.total
    pdb_id = search.records[0].id

    entry = await registry.call("rcsb_pdb", "get_entry", {"pdb_id": pdb_id})
    assert entry.records[0].identifiers["pdb"] == pdb_id

    by_protein = await registry.call("rcsb_pdb", "structures_for_uniprot", {"uniprot": "P15056", "limit": 3})
    assert by_protein.total > 50 and by_protein.records[0].identifiers["uniprot"] == "P15056"

    by_ligand = await registry.call("rcsb_pdb", "structures_for_ligand", {"ligand": "DB00530", "limit": 3})
    assert by_ligand.data["chem_comp_ids"] == ["AQ4"] and by_ligand.records
