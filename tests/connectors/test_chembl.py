"""ChEMBL connector tests (offline respx mocks + live checks)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mosaic.http import SourceError
from tests.conftest import load_fixture

API = "https://www.ebi.ac.uk/chembl/api/data"
UNICHEM = "https://www.ebi.ac.uk/unichem/api/v1/compounds"


def fx(name: str):
    return load_fixture("chembl", name)


@respx.mock
async def test_search_molecules_normalizes_and_paginates(make_registry):
    route = respx.get(f"{API}/molecule/search.json").mock(return_value=httpx.Response(200, json=fx("molecule_search.json")))
    registry = make_registry("chembl")
    result = await registry.call("chembl", "search_molecules", {"query": "imatinib", "limit": 2, "max_phase": 4})

    sent = route.calls.last.request.url.params
    assert sent["q"] == "imatinib" and sent["limit"] == "2" and sent["offset"] == "0"
    assert sent["max_phase__gte"] == "4"
    assert result.total == 5 and result.next_cursor == "2"
    rec = result.records[0]
    assert rec.id == "CHEMBL941" and rec.type == "drug" and rec.title == "IMATINIB"
    assert rec.identifiers["chembl"] == "CHEMBL941"
    assert rec.identifiers["inchikey"] == "KTUFNOKKBVMGRW-UHFFFAOYSA-N"
    assert rec.identifiers["smiles"].startswith("Cc1ccc")
    assert rec.date == "2001"
    assert rec.attributes["max_phase"] == 4 and rec.attributes["atc_classifications"] == ["L01EA01"]
    assert rec.attributes["mw"] == 493.62 and rec.attributes["ro5_violations"] == 0
    assert "molfile" not in json.dumps(rec.model_dump())
    salt = result.records[1]
    assert salt.attributes["parent_chembl_id"] == "CHEMBL941"


@respx.mock
async def test_get_molecule_by_inchikey_adds_unichem_xrefs(make_registry):
    route = respx.get(f"{API}/molecule.json").mock(return_value=httpx.Response(200, json=fx("molecule_by_inchikey.json")))
    uni = respx.post(UNICHEM).mock(return_value=httpx.Response(200, json=fx("unichem.json")))
    registry = make_registry("chembl")
    result = await registry.call("chembl", "get_molecule", {"identifier": "ktufnokkbvmgrw-uhfffaoysa-n"})

    assert route.calls.last.request.url.params["molecule_structures__standard_inchi_key"] == "KTUFNOKKBVMGRW-UHFFFAOYSA-N"
    assert json.loads(uni.calls.last.request.content) == {"type": "inchikey", "compound": "KTUFNOKKBVMGRW-UHFFFAOYSA-N"}
    ids = result.records[0].identifiers
    assert ids["chembl"] == "CHEMBL941"
    assert ids["pubchem_cid"] == "5291"
    assert ids["drugbank"] == "DB00619"
    assert ids["unii"] == "BKJ8M8G5HI"
    assert ids["chebi"] == "CHEBI:45783"


@respx.mock
async def test_get_molecule_by_name_falls_back_to_synonym(make_registry):
    data = fx("molecule_by_inchikey.json")
    empty = {"molecules": [], "page_meta": {"total_count": 0, "next": None}}
    route = respx.get(f"{API}/molecule.json").mock(
        side_effect=[httpx.Response(200, json=empty), httpx.Response(200, json=data)]
    )
    registry = make_registry("chembl")
    result = await registry.call("chembl", "get_molecule", {"identifier": "Gleevec", "include_xrefs": False})
    assert route.calls[0].request.url.params["pref_name__iexact"] == "Gleevec"
    assert route.calls[1].request.url.params["molecule_synonyms__molecule_synonym__iexact"] == "Gleevec"
    assert result.records[0].id == "CHEMBL941"


@respx.mock
async def test_get_molecule_not_found_is_note(make_registry):
    respx.get(f"{API}/molecule/CHEMBL99999999.json").mock(return_value=httpx.Response(404, text="Not found"))
    registry = make_registry("chembl")
    result = await registry.call("chembl", "get_molecule", {"identifier": "chembl99999999"})
    assert result.records == [] and "no ChEMBL molecule" in result.notes[0]


@respx.mock
async def test_unichem_failure_is_a_note(make_registry):
    respx.get(f"{API}/molecule.json").mock(return_value=httpx.Response(200, json=fx("molecule_by_inchikey.json")))
    respx.post(UNICHEM).mock(return_value=httpx.Response(400, json={"error": "bad"}))
    registry = make_registry("chembl")
    result = await registry.call("chembl", "get_molecule", {"identifier": "KTUFNOKKBVMGRW-UHFFFAOYSA-N"})
    assert result.records[0].identifiers["chembl"] == "CHEMBL941"
    assert "UniChem" in result.notes[0]


@respx.mock
async def test_search_targets(make_registry):
    route = respx.get(f"{API}/target/search.json").mock(return_value=httpx.Response(200, json=fx("target_search.json")))
    registry = make_registry("chembl")
    result = await registry.call(
        "chembl", "search_targets", {"query": "EGFR", "organism": "Homo sapiens", "target_type": "protein-protein interaction", "limit": 2}
    )
    sent = route.calls.last.request.url.params
    assert sent["q"] == "EGFR" and sent["organism"] == "Homo sapiens"
    assert sent["target_type"] == "PROTEIN-PROTEIN INTERACTION"
    assert result.total == 21 and result.next_cursor == "2"
    rec = result.records[0]
    assert rec.type == "target" and rec.id.startswith("CHEMBL")
    # multi-component targets must not claim a single UniProt identity
    assert "uniprot" not in rec.identifiers
    assert "P00533" in rec.attributes["uniprot_accessions"]


@respx.mock
async def test_get_target_by_uniprot(make_registry):
    route = respx.get(f"{API}/target.json").mock(return_value=httpx.Response(200, json=fx("target_by_accession.json")))
    registry = make_registry("chembl")
    result = await registry.call("chembl", "get_target", {"identifier": "p00533"})
    assert route.calls.last.request.url.params["target_components__accession"] == "P00533"
    first = result.records[0]
    assert first.id == "CHEMBL203"
    assert first.identifiers == {
        "chembl": "CHEMBL203",
        "uniprot": "P00533",
        "gene_symbol": "EGFR",
        "hgnc": "HGNC:3236",
        "pharmgkb": "PA7360",
    }


@respx.mock
async def test_get_target_not_found(make_registry):
    respx.get(f"{API}/target/CHEMBL0.json").mock(return_value=httpx.Response(404))
    registry = make_registry("chembl")
    result = await registry.call("chembl", "get_target", {"identifier": "CHEMBL0"})
    assert result.records == [] and result.notes


@respx.mock
async def test_get_activities_with_documents(make_registry):
    act = respx.get(f"{API}/activity.json").mock(return_value=httpx.Response(200, json=fx("activities.json")))
    docs = respx.get(f"{API}/document.json").mock(return_value=httpx.Response(200, json=fx("documents.json")))
    registry = make_registry("chembl")
    result = await registry.call(
        "chembl", "get_activities", {"target_chembl_id": "chembl203", "standard_type": "IC50", "pchembl_min": 9, "limit": 2}
    )
    sent = act.calls.last.request.url.params
    assert sent["target_chembl_id"] == "CHEMBL203"
    assert sent["standard_type"] == "IC50" and sent["pchembl_value__gte"] == "9.0"
    assert sent["order_by"] == "-pchembl_value" and sent["pchembl_value__isnull"] == "false"
    assert docs.calls.last.request.url.params["document_chembl_id__in"] == "CHEMBL1129035,CHEMBL6087440"
    assert result.total == 1463 and result.next_cursor == "2"
    rec = result.records[0]
    assert rec.type == "activity" and rec.id == "1043415"
    # queried by target -> record identifies the molecule only (no bridging to the paper/target)
    assert rec.identifiers == {"chembl": "CHEMBL176582", "smiles": "Cn1cnc2cc3ncnc(Nc4cccc(Br)c4)c3cc21"}
    assert rec.attributes["pmid"] == "8632415" and rec.attributes["doi"] == "10.1021/jm950692f"
    assert rec.attributes["pchembl_value"] == 11 and rec.date == "1996"
    assert result.data["rows"][0]["pmid"] == "8632415"


async def test_get_activities_requires_a_filter(make_registry):
    registry = make_registry("chembl")
    with pytest.raises(SourceError, match="needs molecule_chembl_id"):
        await registry.call("chembl", "get_activities", {})


@respx.mock
async def test_get_mechanisms_by_molecule(make_registry):
    mech = respx.get(f"{API}/mechanism.json").mock(return_value=httpx.Response(200, json=fx("mechanisms.json")))
    tgt = respx.get(f"{API}/target.json").mock(return_value=httpx.Response(200, json=fx("targets_in.json")))
    registry = make_registry("chembl")
    result = await registry.call("chembl", "get_mechanisms", {"molecule_chembl_id": "CHEMBL941", "limit": 2})
    assert mech.calls.last.request.url.params["parent_molecule_chembl_id"] == "CHEMBL941"
    assert tgt.calls.last.request.url.params["target_chembl_id__in"] == "CHEMBL1862,CHEMBL1913"
    assert result.total == 4 and result.next_cursor == "2"
    rec = result.records[0]
    assert rec.title == "Tyrosine-protein kinase ABL inhibitor"
    assert rec.identifiers == {"chembl": "CHEMBL1862", "uniprot": "P00519"}
    assert rec.attributes["action_type"] == "INHIBITOR"
    assert rec.attributes["target_name"] == "Tyrosine-protein kinase ABL1"


@respx.mock
async def test_get_mechanisms_falls_back_to_molecule_id(make_registry):
    empty = {"mechanisms": [], "page_meta": {"total_count": 0, "next": None}}
    mech = respx.get(f"{API}/mechanism.json").mock(
        side_effect=[httpx.Response(200, json=empty), httpx.Response(200, json=empty)]
    )
    registry = make_registry("chembl")
    result = await registry.call("chembl", "get_mechanisms", {"molecule_chembl_id": "CHEMBL25"})
    assert mech.calls[1].request.url.params["molecule_chembl_id"] == "CHEMBL25"
    assert result.records == [] and result.notes


@respx.mock
async def test_get_drug_indications(make_registry):
    route = respx.get(f"{API}/drug_indication.json").mock(return_value=httpx.Response(200, json=fx("indications.json")))
    registry = make_registry("chembl")
    result = await registry.call("chembl", "get_drug_indications", {"molecule_chembl_id": "CHEMBL941", "min_phase": 3, "limit": 2})
    sent = route.calls.last.request.url.params
    assert sent["parent_molecule_chembl_id"] == "CHEMBL941" and sent["max_phase_for_ind__gte"] == "3.0"
    assert result.total == 134 and result.next_cursor == "2"
    first, second = result.records
    assert first.type == "disease"
    assert first.identifiers == {"mesh": "D054198", "mondo": "MONDO:0000873"}
    assert second.identifiers == {"mesh": "D015464", "efo": "EFO_1000131"}
    assert first.attributes["max_phase_for_indication"] == 4


# ------------------------------------------------------------------ live
@pytest.mark.live
async def test_live_molecule_tools(live_registry):
    registry = live_registry("chembl")
    found = await registry.call("chembl", "search_molecules", {"query": "imatinib", "limit": 3})
    assert any(r.id == "CHEMBL941" for r in found.records)
    detail = await registry.call("chembl", "get_molecule", {"identifier": "CHEMBL941"})
    ids = detail.records[0].identifiers
    assert ids["inchikey"] == "KTUFNOKKBVMGRW-UHFFFAOYSA-N" and ids.get("pubchem_cid") == "5291"
    by_name = await registry.call("chembl", "get_molecule", {"identifier": "imatinib", "include_xrefs": False})
    assert by_name.records[0].id == "CHEMBL941"


@pytest.mark.live
async def test_live_target_and_activity_tools(live_registry):
    registry = live_registry("chembl")
    targets = await registry.call("chembl", "search_targets", {"query": "EGFR", "organism": "Homo sapiens", "limit": 5})
    assert targets.records
    target = await registry.call("chembl", "get_target", {"identifier": "P00533"})
    assert target.records[0].id == "CHEMBL203"
    acts = await registry.call("chembl", "get_activities", {"target_chembl_id": "CHEMBL203", "standard_type": "IC50", "pchembl_min": 9, "limit": 3})
    assert acts.records and acts.records[0].attributes["pchembl_value"] >= 9
    mechs = await registry.call("chembl", "get_mechanisms", {"molecule_chembl_id": "CHEMBL941"})
    assert any(r.identifiers.get("uniprot") == "P00519" for r in mechs.records)
    inds = await registry.call("chembl", "get_drug_indications", {"molecule_chembl_id": "CHEMBL941", "min_phase": 4})
    assert inds.records and all(r.attributes["max_phase_for_indication"] >= 4 for r in inds.records)
