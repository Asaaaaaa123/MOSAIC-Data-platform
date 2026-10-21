"""ClinPGx (PharmGKB) connector tests: offline respx mocks + live."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from tests.conftest import load_fixture

API = "https://api.clinpgx.org/v1"
NOT_FOUND = {"status": "fail", "data": {"errors": [{"message": "No results matching criteria."}]}}


def _chem_min(pa: str, name: str) -> dict:
    return {"data": [{"objCls": "Chemical", "id": pa, "name": name}], "status": "success"}


@respx.mock
async def test_search_normalizes_and_paginates(make_registry):
    route = respx.post(f"{API}/site/ftSearch").mock(
        return_value=httpx.Response(200, json=load_fixture("clinpgx", "search.json"))
    )
    registry = make_registry("clinpgx")
    result = await registry.call("clinpgx", "search", {"query": "warfarin", "limit": 3, "cursor": "6"})

    body = json.loads(route.calls.last.request.content)
    assert body == {"query": "warfarin", "size": 3, "from": 6}
    assert result.next_cursor == "9"
    chem, guideline, variant = result.records
    assert chem.id == "PA451906" and chem.type == "drug" and chem.title == "warfarin"
    assert chem.url == "https://www.clinpgx.org/chemical/PA451906"
    assert chem.identifiers == {"pharmgkb": "PA451906", "rxcui": "11289", "atc": "B01AA03", "mesh": "D014859"}
    assert chem.attributes["counts"]["guidelineAnnotations"] == 5
    assert "connectedEntities" not in json.dumps(chem.attributes)
    assert guideline.attributes["object_class"] == "GuidelineAnnotation"
    assert guideline.summary and "warfarin" in guideline.summary
    assert variant.type == "variant" and variant.identifiers["rsid"] == "rs9923231"

    await registry.call("clinpgx", "search", {"query": "CYP2C19", "types": ["Gene"]})
    assert json.loads(route.calls.last.request.content)["objCls"] == ["Gene"]


@respx.mock
async def test_search_falls_back_to_autocomplete(make_registry):
    respx.post(f"{API}/site/ftSearch").mock(return_value=httpx.Response(404, json=NOT_FOUND))
    auto = respx.post(f"{API}/site/autocomplete").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "hits": [{"id": "PA126", "name": "CYP2C9", "objCls": "Gene", "score": 1.0, "directMatch": True}],
                    "total": 1,
                },
                "status": "success",
            },
        )
    )
    registry = make_registry("clinpgx")
    result = await registry.call("clinpgx", "search", {"query": "CYP2C9"})
    assert auto.called
    assert result.records[0].identifiers == {"pharmgkb": "PA126", "gene_symbol": "CYP2C9"}
    assert result.next_cursor is None and "autocomplete" in result.notes[0]


@respx.mock
async def test_get_gene_by_symbol_maps_cross_references(make_registry):
    route = respx.get(f"{API}/data/gene").mock(
        return_value=httpx.Response(200, json=load_fixture("clinpgx", "gene.json"))
    )
    registry = make_registry("clinpgx")
    result = await registry.call("clinpgx", "get_gene", {"gene": "cyp2d6"})
    assert route.calls.last.request.url.params["symbol"] == "CYP2D6"
    assert route.calls.last.request.url.params["view"] == "max"
    gene = result.records[0]
    assert gene.id == "PA128" and gene.type == "gene"
    assert gene.identifiers == {
        "pharmgkb": "PA128",
        "gene_symbol": "CYP2D6",
        "hgnc": "HGNC:2625",
        "entrez_gene": "1565",
        "ensembl_gene": "ENSG00000100197",
        "uniprot": "P10635",
        "omim": "124030",
        "refseq": "NM_000106",
    }
    assert gene.attributes["vip_tier"] == "Tier 1" and gene.attributes["cpic_gene"] is True
    assert gene.summary.startswith("CYP2D6 is one of the key pharmacogenes") and "<" not in gene.summary


@respx.mock
async def test_get_gene_not_found(make_registry):
    respx.get(f"{API}/data/gene/PA999999").mock(return_value=httpx.Response(404, json=NOT_FOUND))
    registry = make_registry("clinpgx")
    result = await registry.call("clinpgx", "get_gene", {"gene": "PA999999"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_get_chemical_by_trade_name_resolves_and_maps_ids(make_registry):
    by_name = respx.get(f"{API}/data/chemical").mock(return_value=httpx.Response(404, json=NOT_FOUND))
    exact = respx.post(f"{API}/site/searchExact").mock(
        return_value=httpx.Response(200, json={"data": {"hits": [{"id": "PA451906", "objCls": "Chemical"}], "total": 1}})
    )
    detail = respx.get(f"{API}/data/chemical/PA451906").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={"data": {"objCls": "Chemical", "id": "PA451906", "name": "warfarin"}}
            if request.url.params["view"] == "min"
            else load_fixture("clinpgx", "chemical.json"),
        )
    )
    registry = make_registry("clinpgx")
    result = await registry.call("clinpgx", "get_chemical", {"chemical": "Coumadin"})

    assert [c.request.url.params["name"] for c in by_name.calls] == ["Coumadin", "coumadin"]
    assert json.loads(exact.calls[0].request.content) == {"objCls": ["Chemical"], "query": "Coumadin", "field": "name"}
    assert detail.calls.last.request.url.params["view"] == "max"
    chem = result.records[0]
    assert chem.id == "PA451906" and chem.type == "drug"
    assert chem.identifiers["rxcui"] == "11289"
    assert chem.identifiers["atc"] == "B01AA03"
    assert chem.identifiers["drugbank"] == "DB00682"
    assert chem.identifiers["pubchem_cid"] == "54678486"
    assert chem.identifiers["mesh"] == "D014859"
    assert chem.identifiers["chebi"] == "CHEBI:10033"
    assert chem.attributes["clinical_trials"] == ["NCT00247702", "NCT00334464"]
    assert "smiles" in chem.attributes


@respx.mock
async def test_get_chemical_by_rxnorm_reference(make_registry):
    exact = respx.post(f"{API}/site/searchExact").mock(
        return_value=httpx.Response(200, json={"data": {"hits": [{"id": "PA451906", "objCls": "Chemical"}], "total": 1}})
    )
    respx.get(f"{API}/data/chemical/PA451906").mock(
        return_value=httpx.Response(200, json=load_fixture("clinpgx", "chemical.json"))
    )
    registry = make_registry("clinpgx")
    result = await registry.call("clinpgx", "get_chemical", {"chemical": "rxnorm:11289"})
    assert json.loads(exact.calls[0].request.content)["query"] == "RxNorm:11289"
    assert json.loads(exact.calls[0].request.content)["field"] == "synonyms"
    assert result.records[0].identifiers["pharmgkb"] == "PA451906"


@respx.mock
async def test_get_chemical_not_found(make_registry):
    respx.get(f"{API}/data/chemical").mock(return_value=httpx.Response(404, json=NOT_FOUND))
    respx.post(f"{API}/site/searchExact").mock(
        return_value=httpx.Response(200, json={"data": {"hits": [], "total": 0}})
    )
    registry = make_registry("clinpgx")
    result = await registry.call("clinpgx", "get_chemical", {"chemical": "notadrugxyz"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_clinical_annotations_sorted_paged_and_enriched(make_registry):
    respx.get(f"{API}/data/chemical").mock(return_value=httpx.Response(200, json=_chem_min("PA451735", "tramadol")))
    listing = respx.get(f"{API}/data/summaryAnnotation").mock(
        return_value=httpx.Response(200, json=load_fixture("clinpgx", "annotations_min.json"))
    )
    detail = respx.get(url__regex=rf"{API}/data/summaryAnnotation/PA\d+").mock(
        return_value=httpx.Response(200, json=load_fixture("clinpgx", "annotation_max.json"))
    )
    registry = make_registry("clinpgx")
    result = await registry.call(
        "clinpgx", "clinical_annotations", {"gene": "cyp2d6", "drug": "tramadol", "limit": 1}
    )
    sent = listing.calls.last.request.url.params
    assert sent["location.genes.symbol"] == "CYP2D6"
    assert sent["relatedChemicals.accessionId"] == "PA451735"
    assert sent["view"] == "min"
    assert result.total == 3 and result.next_cursor == "1"
    # strongest level (2A) first, then fetched in full
    assert detail.calls.last.request.url.path.endswith("/PA166135161")
    rec = result.records[0]
    assert rec.type == "association"
    assert rec.identifiers["pharmgkb"] == "PA166134805"
    assert rec.identifiers["rsid"] == "rs7089580"
    assert rec.identifiers["gene_symbol"] == "CYP2C9"
    assert "pmid" not in rec.identifiers and rec.attributes["pmids"]  # supporting papers are related
    assert rec.attributes["level_of_evidence"] == "3"
    assert rec.attributes["phenotype_categories"] == ["Dosage"]
    assert rec.attributes["genotype_phenotypes"][0]["genotype"] == "AA"
    assert rec.date and rec.date[:4].isdigit()


@respx.mock
async def test_clinical_annotations_level_filter_and_no_hits(make_registry):
    listing = respx.get(f"{API}/data/summaryAnnotation").mock(
        return_value=httpx.Response(200, json=load_fixture("clinpgx", "annotations_min.json"))
    )
    registry = make_registry("clinpgx")
    result = await registry.call(
        "clinpgx", "clinical_annotations", {"gene": "CYP2D6", "levels": ["3", "4"], "include_evidence": False}
    )
    assert "levelOfEvidence.term" not in listing.calls.last.request.url.params
    assert [r.attributes["level_of_evidence"] for r in result.records] == ["3", "4"]

    listing.mock(return_value=httpx.Response(404, json=NOT_FOUND))
    empty = await registry.call("clinpgx", "clinical_annotations", {"variant": "rs1", "levels": ["1A"]})
    params = listing.calls.last.request.url.params
    assert params["location.fingerprint"] == "rs1" and params["levelOfEvidence.term"] == "1A"
    assert empty.records == [] and empty.total == 0 and empty.notes

    nothing = await registry.call("clinpgx", "clinical_annotations", {})
    assert "at least one" in nothing.notes[0]


@respx.mock
async def test_dosing_guidelines(make_registry):
    respx.get(f"{API}/data/chemical").mock(return_value=httpx.Response(200, json=_chem_min("PA451906", "warfarin")))
    route = respx.get(f"{API}/data/guidelineAnnotation").mock(
        return_value=httpx.Response(200, json=load_fixture("clinpgx", "guidelines.json"))
    )
    registry = make_registry("clinpgx")
    result = await registry.call("clinpgx", "dosing_guidelines", {"drug": "warfarin", "source": "DPWG", "limit": 1})
    sent = route.calls.last.request.url.params
    assert sent["relatedChemicals.accessionId"] == "PA451906"
    assert sent["source"] == "DPWG" and sent["view"] == "base"
    assert result.total == 2 and result.next_cursor == "1"
    rec = result.records[0]
    assert rec.id.startswith("PA") and rec.url.startswith("https://www.clinpgx.org/guidelineAnnotation/")
    assert rec.attributes["guideline_source"] == "DPWG"
    assert rec.attributes["drugs"] == ["warfarin"]
    assert rec.summary and "<p>" not in rec.summary
    assert rec.attributes["source_url"].endswith(".pdf")
    # the guideline's publication is a related entity (one paper covers several guidelines)
    assert "pmid" not in rec.identifiers and "doi" not in rec.identifiers


@respx.mock
async def test_dosing_guidelines_unknown_gene(make_registry):
    respx.get(f"{API}/data/gene").mock(return_value=httpx.Response(404, json=NOT_FOUND))
    registry = make_registry("clinpgx")
    result = await registry.call("clinpgx", "dosing_guidelines", {"gene": "NOTAGENE"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_drug_labels(make_registry):
    respx.get(f"{API}/data/chemical").mock(return_value=httpx.Response(200, json=_chem_min("PA451906", "warfarin")))
    route = respx.get(f"{API}/data/label").mock(
        return_value=httpx.Response(200, json=load_fixture("clinpgx", "labels.json"))
    )
    registry = make_registry("clinpgx")
    result = await registry.call("clinpgx", "drug_labels", {"drug": "warfarin", "source": "FDA"})
    sent = route.calls.last.request.url.params
    assert sent["relatedChemicals.accessionId"] == "PA451906" and sent["source"] == "FDA"
    first = result.records[0]
    assert first.type == "drug_label"
    assert first.identifiers["fda_application"] == "NDA009218"
    assert first.attributes["agency"] == "FDA"
    assert first.attributes["testing_level"] == "Actionable PGx"
    assert "CYP2C9" in first.attributes["genes"]


@pytest.mark.live
async def test_live_all_tools(live_registry):
    registry = live_registry("clinpgx")
    search = await registry.call("clinpgx", "search", {"query": "clopidogrel", "limit": 3})
    assert any(r.id == "PA449053" for r in search.records)
    gene = await registry.call("clinpgx", "get_gene", {"gene": "CYP2C19"})
    assert gene.records[0].identifiers["hgnc"] == "HGNC:2621"
    chem = await registry.call("clinpgx", "get_chemical", {"chemical": "clopidogrel"})
    assert chem.records[0].identifiers["rxcui"] == "32968"
    ann = await registry.call(
        "clinpgx", "clinical_annotations", {"gene": "CYP2C19", "drug": "clopidogrel", "levels": ["1A"], "limit": 2}
    )
    assert ann.records and ann.records[0].attributes["level_of_evidence"] == "1A"
    assert ann.records[0].attributes.get("pmids")
    guide = await registry.call("clinpgx", "dosing_guidelines", {"drug": "clopidogrel", "gene": "CYP2C19"})
    assert any(r.attributes.get("guideline_source") == "CPIC" for r in guide.records)
    labels = await registry.call("clinpgx", "drug_labels", {"gene": "HLA-B", "source": "FDA", "limit": 3})
    assert labels.records and labels.records[0].type == "drug_label"
