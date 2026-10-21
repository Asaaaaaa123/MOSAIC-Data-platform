"""PubChem connector tests (offline respx mocks + live checks)."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.connectors.pubchem import PROPERTIES, parse_synonyms
from mosaic.http import SourceError
from tests.conftest import load_fixture

PUG = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
VIEW = "https://pubchem.ncbi.nlm.nih.gov/rest/pug_view"


def fx(name: str):
    return load_fixture("pubchem", name)


def test_parse_synonyms_extracts_registry_ids():
    ids = parse_synonyms(["Imatinib", "152459-95-5", "UNII-BKJ8M8G5HI", "BKJ8M8G5HI", "CHEMBL941", "DB00619",
                          "DTXSID3037125", "CHEBI:45783", "BDBM13530", "NSC-743414"])
    assert ids == {
        "cas": ["152459-95-5"],
        "unii": ["BKJ8M8G5HI"],
        "chembl": ["CHEMBL941"],
        "drugbank": ["DB00619"],
        "dtxsid": ["DTXSID3037125"],
        "chebi": ["CHEBI:45783"],
        "bindingdb": ["13530"],
    }


@respx.mock
async def test_search_compounds_merges_exact_then_word(make_registry):
    route = respx.get(f"{PUG}/compound/name/imatinib/cids/JSON").mock(
        side_effect=[httpx.Response(200, json=fx("cids_exact.json")), httpx.Response(200, json=fx("cids_word.json"))]
    )
    props = respx.get(f"{PUG}/compound/cid/5291,123596/property/{PROPERTIES}/JSON").mock(
        return_value=httpx.Response(200, json=fx("properties.json"))
    )
    registry = make_registry("pubchem")
    result = await registry.call("pubchem", "search_compounds", {"query": "imatinib", "limit": 2})

    assert "name_type" not in route.calls[0].request.url.params
    assert route.calls[1].request.url.params["name_type"] == "word"
    assert props.called
    assert result.total == 5 and result.next_cursor == "2"
    first = result.records[0]
    assert first.id == "5291" and first.title == "Imatinib" and first.type == "compound"
    assert first.identifiers["pubchem_cid"] == "5291"
    assert first.identifiers["inchikey"] == "KTUFNOKKBVMGRW-UHFFFAOYSA-N"
    assert first.identifiers["smiles"].startswith("CC1=C")
    assert first.attributes["formula"] == "C29H31N7O" and first.attributes["mw"] == 493.6
    assert first.attributes["patent_count"] > 1000
    assert result.records[1].id == "123596"


@respx.mock
async def test_search_compounds_no_match(make_registry):
    respx.get(f"{PUG}/compound/name/zzzz/cids/JSON").mock(
        return_value=httpx.Response(404, json={"Fault": {"Code": "PUGREST.NotFound"}})
    )
    registry = make_registry("pubchem")
    result = await registry.call("pubchem", "search_compounds", {"query": "zzzz", "match": "exact"})
    assert result.records == [] and result.total == 0 and "no PubChem compound" in result.notes[0]


@respx.mock
async def test_get_compound_by_name_with_synonym_ids(make_registry):
    prop = {"PropertyTable": {"Properties": [fx("properties.json")["PropertyTable"]["Properties"][0]]}}
    respx.get(f"{PUG}/compound/name/imatinib/property/{PROPERTIES}/JSON").mock(return_value=httpx.Response(200, json=prop))
    respx.get(f"{PUG}/compound/cid/5291/synonyms/JSON").mock(return_value=httpx.Response(200, json=fx("synonyms.json")))
    respx.get(f"{PUG}/compound/cid/5291/description/JSON").mock(return_value=httpx.Response(200, json=fx("description.json")))
    registry = make_registry("pubchem")
    result = await registry.call("pubchem", "get_compound", {"name": "imatinib"})
    rec = result.records[0]
    assert rec.identifiers["cas"] == "152459-95-5"
    assert rec.identifiers["unii"] == "BKJ8M8G5HI"
    assert rec.identifiers["chembl"] == "CHEMBL941"
    assert rec.identifiers["drugbank"] == "DB00619"
    assert rec.summary.startswith("Imatinib is a benzamide")
    assert rec.attributes["description_source"] == "ChEBI"


@respx.mock
async def test_get_compound_by_smiles_sends_query_param(make_registry):
    prop = {"PropertyTable": {"Properties": [fx("properties.json")["PropertyTable"]["Properties"][0]]}}
    route = respx.get(f"{PUG}/compound/smiles/property/{PROPERTIES}/JSON").mock(return_value=httpx.Response(200, json=prop))
    respx.get(f"{PUG}/compound/cid/5291/synonyms/JSON").mock(return_value=httpx.Response(404))
    respx.get(f"{PUG}/compound/cid/5291/description/JSON").mock(return_value=httpx.Response(404))
    registry = make_registry("pubchem")
    result = await registry.call("pubchem", "get_compound", {"smiles": "Cc1ccc(NC(=O)c2ccc(CN3CCN(C)CC3)cc2)cc1Nc1nccc(-c2cccnc2)n1"})
    assert route.calls.last.request.url.params["smiles"].startswith("Cc1ccc")
    assert result.records[0].id == "5291"


@respx.mock
async def test_get_compound_not_found_and_validation(make_registry):
    respx.get(f"{PUG}/compound/inchikey/AAAAAAAAAAAAAA-BBBBBBBBBB-C/property/{PROPERTIES}/JSON").mock(
        return_value=httpx.Response(404, json={"Fault": {"Code": "PUGREST.NotFound"}})
    )
    registry = make_registry("pubchem")
    result = await registry.call("pubchem", "get_compound", {"inchikey": "AAAAAAAAAAAAAA-BBBBBBBBBB-C"})
    assert result.records == [] and "HTTP 404" in result.notes[0]
    with pytest.raises(SourceError, match="exactly one"):
        await registry.call("pubchem", "get_compound", {"cid": 1, "name": "x"})


@respx.mock
async def test_compound_xrefs(make_registry):
    respx.get(f"{PUG}/compound/cid/5291/synonyms/JSON").mock(return_value=httpx.Response(200, json=fx("synonyms.json")))
    prop = {"PropertyTable": {"Properties": [fx("properties.json")["PropertyTable"]["Properties"][0]]}}
    respx.get(f"{PUG}/compound/cid/5291/property/{PROPERTIES}/JSON").mock(return_value=httpx.Response(200, json=prop))
    respx.get(f"{PUG}/compound/cid/5291/xrefs/PubMedID/JSON").mock(return_value=httpx.Response(200, json=fx("xrefs_pubmed.json")))
    registry = make_registry("pubchem")
    result = await registry.call("pubchem", "compound_xrefs", {"cid": 5291, "max_pmids": 3})
    data = result.data
    assert data["chembl"] == ["CHEMBL941"] and data["drugbank"] == ["DB00619"] and data["cas"][0] == "152459-95-5"
    assert data["pubmed_count"] == 8 and len(data["recent_pmids"]) == 3
    assert data["recent_pmids"] == sorted(data["recent_pmids"], key=int, reverse=True)
    assert result.records[0].identifiers["unii"] == "BKJ8M8G5HI"


@respx.mock
async def test_bioassay_summary_aggregates_by_gene(make_registry):
    respx.get(f"{PUG}/compound/cid/5291/assaysummary/JSON").mock(return_value=httpx.Response(200, json=fx("assaysummary.json")))
    genes = respx.get(f"{PUG}/gene/geneid/780,25,5159/summary/JSON").mock(
        return_value=httpx.Response(200, json=fx("gene_summary.json"))
    )
    registry = make_registry("pubchem")
    result = await registry.call("pubchem", "bioassay_summary", {"cid": 5291})
    assert genes.called
    assert result.total == 3
    first = result.records[0]
    assert first.id == "5291:780" and first.type == "activity"
    assert first.identifiers == {"entrez_gene": "780", "gene_symbol": "DDR1"}
    assert first.attributes["best_activity_uM"] == 0.0007
    assert [r.attributes["gene_symbol"] for r in result.records] == ["DDR1", "ABL1", "PDGFRB"]
    assert result.data["outcome_counts"]["Active"] == 11
    assert result.data["top_untargeted_active_assays"]


@respx.mock
async def test_bioassay_summary_not_found(make_registry):
    respx.get(f"{PUG}/compound/cid/1/assaysummary/JSON").mock(return_value=httpx.Response(404))
    registry = make_registry("pubchem")
    result = await registry.call("pubchem", "bioassay_summary", {"cid": 1})
    assert result.records == [] and result.notes


@respx.mock
async def test_similarity_search_by_cid(make_registry):
    route = respx.get(f"{PUG}/compound/fastsimilarity_2d/cid/5291/property/{PROPERTIES}/JSON").mock(
        return_value=httpx.Response(200, json=fx("similarity.json"))
    )
    registry = make_registry("pubchem")
    result = await registry.call("pubchem", "similarity_search", {"cid": 5291, "threshold": 95, "limit": 3})
    sent = route.calls.last.request.url.params
    assert sent["Threshold"] == "95" and sent["MaxRecords"] == "3"
    assert len(result.records) == 3 and result.records[0].identifiers["pubchem_cid"] == "5291"


@respx.mock
async def test_substructure_by_smiles(make_registry):
    route = respx.get(f"{PUG}/compound/fastsubstructure/smiles/property/{PROPERTIES}/JSON").mock(
        return_value=httpx.Response(200, json=fx("similarity.json"))
    )
    registry = make_registry("pubchem")
    await registry.call("pubchem", "similarity_search", {"smiles": "c1ccc2ncncc2c1", "mode": "substructure"})
    sent = route.calls.last.request.url.params
    assert sent["smiles"] == "c1ccc2ncncc2c1" and "Threshold" not in sent


@respx.mock
async def test_compound_annotations(make_registry):
    route = respx.get(f"{VIEW}/data/compound/5291/JSON").mock(return_value=httpx.Response(200, json=fx("annotations_moa.json")))
    registry = make_registry("pubchem")
    result = await registry.call("pubchem", "compound_annotations", {"cid": 5291, "heading": "Mechanism of Action"})
    assert route.calls.last.request.url.params["heading"] == "Mechanism of Action"
    items = result.data["annotations"]
    assert items and items[0]["section"] == "Mechanism of Action"
    assert "BCR-ABL" in items[0]["text"] and items[0]["source"]
    assert result.records[0].identifiers == {"pubchem_cid": "5291"}


@respx.mock
async def test_compound_annotations_missing_heading(make_registry):
    respx.get(f"{VIEW}/data/compound/5291/JSON").mock(
        return_value=httpx.Response(400, json={"Fault": {"Code": "PUGVIEW.BadRequest"}})
    )
    registry = make_registry("pubchem")
    result = await registry.call("pubchem", "compound_annotations", {"cid": 5291, "heading": "Nope"})
    assert result.records == [] and "not available" in result.notes[0]


# ------------------------------------------------------------------ live
@pytest.mark.live
async def test_live_search_get_xrefs(live_registry):
    registry = live_registry("pubchem")
    found = await registry.call("pubchem", "search_compounds", {"query": "aspirin", "limit": 3})
    assert found.records[0].id == "2244"
    detail = await registry.call("pubchem", "get_compound", {"inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"})
    assert detail.records[0].identifiers["cas"] == "50-78-2"
    xrefs = await registry.call("pubchem", "compound_xrefs", {"cid": 2244, "max_pmids": 3})
    assert xrefs.data["chembl"] == ["CHEMBL25"] and xrefs.data["pubmed_count"]


@pytest.mark.live
async def test_live_assays_similarity_annotations(live_registry):
    registry = live_registry("pubchem")
    assays = await registry.call("pubchem", "bioassay_summary", {"cid": 5291, "limit": 5})
    assert assays.records and assays.data["assays_tested"] > 100
    sim = await registry.call("pubchem", "similarity_search", {"cid": 5291, "threshold": 95, "limit": 5})
    assert any(r.id == "5291" for r in sim.records)
    notes = await registry.call("pubchem", "compound_annotations", {"cid": 5291, "heading": "Drug and Medication Information", "max_items": 5})
    assert notes.data["annotations"]
