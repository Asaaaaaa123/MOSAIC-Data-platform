"""RxNorm / RxNav connector tests (offline respx mocks + live)."""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import load_fixture

API = "https://rxnav.nlm.nih.gov/REST"


def _fx(name: str) -> httpx.Response:
    return httpx.Response(200, json=load_fixture("rxnorm", name))


def _mock_properties() -> respx.Route:
    def responder(request: httpx.Request) -> httpx.Response:
        rxcui = request.url.path.split("/")[-2]
        try:
            return _fx(f"properties_{rxcui}.json")
        except FileNotFoundError:
            return httpx.Response(200, json={})

    return respx.get(url__regex=rf"{API}/rxcui/\d+/properties\.json").mock(side_effect=responder)


@respx.mock
async def test_search_merges_approximate_and_products(make_registry):
    approx = respx.get(f"{API}/approximateTerm.json").mock(return_value=_fx("approximate_metformin.json"))
    drugs = respx.get(f"{API}/drugs.json").mock(return_value=_fx("drugs_metformin.json"))
    _mock_properties()
    registry = make_registry("rxnorm")
    result = await registry.call("rxnorm", "search", {"query": "metformin", "limit": 4})

    assert approx.calls.last.request.url.params["term"] == "metformin"
    assert approx.calls.last.request.url.params["option"] == "1"
    assert drugs.calls.last.request.url.params["name"] == "metformin"
    first = result.records[0]
    assert first.id == "6809" and first.title == "metformin"
    assert first.identifiers == {"rxcui": "6809"}
    assert first.attributes["tty"] == "IN" and first.attributes["match"] == "approximate"
    assert first.url.endswith("searchTerm=6809")
    assert result.total == 9  # 3 approximate concepts + 6 products (SCD before SBD)
    assert result.next_cursor == "4"
    assert result.records[3].attributes["tty"] == "SCD"

    page2 = await registry.call("rxnorm", "search", {"query": "metformin", "limit": 4, "cursor": "8"})
    assert len(page2.records) == 1 and page2.next_cursor is None


@respx.mock
async def test_search_tty_filter_and_no_match(make_registry):
    respx.get(f"{API}/approximateTerm.json").mock(return_value=_fx("approximate_metformin.json"))
    respx.get(f"{API}/drugs.json").mock(return_value=_fx("drugs_metformin.json"))
    _mock_properties()
    registry = make_registry("rxnorm")
    result = await registry.call("rxnorm", "search", {"query": "metformin", "tty": ["IN"]})
    assert [r.id for r in result.records] == ["6809"]

    respx.get(f"{API}/approximateTerm.json").mock(return_value=httpx.Response(200, json={"approximateGroup": {}}))
    respx.get(f"{API}/drugs.json").mock(return_value=httpx.Response(200, json={"drugGroup": {}}))
    sugg = respx.get(f"{API}/spellingsuggestions.json").mock(
        return_value=httpx.Response(
            200, json={"suggestionGroup": {"suggestionList": {"suggestion": ["metformin"]}}}
        )
    )
    empty = await registry.call("rxnorm", "search", {"query": "metfromin xyz"})
    assert sugg.called and empty.records == [] and "metformin" in empty.notes[0]


@respx.mock
async def test_get_concept_codes_and_related(make_registry):
    respx.get(f"{API}/rxcui/6809/properties.json").mock(return_value=_fx("properties_6809.json"))
    allprops = respx.get(f"{API}/rxcui/6809/allProperties.json").mock(return_value=_fx("allproperties_6809.json"))
    respx.get(f"{API}/rxcui/6809/allrelated.json").mock(return_value=_fx("allrelated_6809.json"))
    registry = make_registry("rxnorm")
    result = await registry.call("rxnorm", "get_concept", {"rxcui": "RXCUI:6809", "max_per_group": 2})

    assert allprops.calls.last.request.url.params["prop"] == "codes attributes"
    rec = result.records[0]
    assert rec.identifiers == {"rxcui": "6809", "unii": "9100L32L2N", "drugbank": "DB00331", "atc": "A10BA02"}
    related = rec.attributes["related"]
    assert set(related) >= {"PIN", "BN", "SCD", "SBD", "MIN", "DF"}
    assert "IN" not in related  # the concept itself is not listed as related
    assert len(related["BN"]) == 2 and rec.attributes["related_counts"]["BN"] == 4
    assert any("truncated" in n for n in result.notes)
    assert rec.attributes["codes"]["SNOMEDCT"]


@respx.mock
async def test_get_concept_retired_and_not_found(make_registry):
    respx.get(f"{API}/rxcui/1547554/properties.json").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{API}/rxcui/1547554/historystatus.json").mock(return_value=_fx("historystatus_1547554.json"))
    respx.get(f"{API}/rxcui/99999999/properties.json").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{API}/rxcui/99999999/historystatus.json").mock(return_value=httpx.Response(404, text="error"))
    registry = make_registry("rxnorm")
    retired = await registry.call("rxnorm", "get_concept", {"rxcui": "1547554"})
    assert retired.records == [] and "remapped" in retired.notes[0] and "1657749" in retired.notes[0]
    missing = await registry.call("rxnorm", "get_concept", {"rxcui": "99999999"})
    assert missing.records == [] and "not found" in missing.notes[0]


@respx.mock
async def test_drug_classes(make_registry):
    route = respx.get(f"{API}/rxclass/class/byRxcui.json").mock(return_value=_fx("rxclass_6809.json"))
    registry = make_registry("rxnorm")
    result = await registry.call("rxnorm", "drug_classes", {"rxcui": "6809"})
    sent = route.calls.last.request.url.params
    assert sent["rxcui"] == "6809" and "relaSource" not in sent
    by_id = {(r.id, r.attributes.get("relation")): r for r in result.records}
    atc = by_id[("A10BA", None)]
    assert atc.identifiers == {"atc": "A10BA"} and atc.attributes["rela_source"] == "ATC"
    assert ("A10BD", None) not in by_id  # combination / product-level ATC omitted by default
    epc = by_id[("N0000175565", "has_epc")]
    assert epc.attributes["rela_source"] == "DAILYMED" and epc.attributes["also_in"] == ["FDASPL"]
    treat = by_id[("D003924", "may_treat")]
    assert treat.type == "disease" and treat.identifiers == {"mesh": "D003924"}
    assert len(treat.attributes["drug_concepts"]) == 2  # IN + PIN merged
    assert result.records[0].attributes["rela_source"] == "ATC"
    assert any("omitted" in n for n in result.notes)

    combos = await registry.call("rxnorm", "drug_classes", {"rxcui": "6809", "include_combinations": True})
    assert any(r.id == "A10BD" for r in combos.records)


@respx.mock
async def test_drug_classes_by_name_with_type(make_registry):
    route = respx.get(f"{API}/rxclass/class/byDrugName.json").mock(
        return_value=httpx.Response(200, json={})
    )
    registry = make_registry("rxnorm")
    result = await registry.call("rxnorm", "drug_classes", {"drug_name": "metformin", "class_type": "may_treat"})
    sent = route.calls.last.request.url.params
    assert sent["drugName"] == "metformin" and sent["relaSource"] == "MEDRT" and sent["relas"] == "may_treat"
    assert result.records == [] and result.notes


@respx.mock
async def test_find_by_id_ndc_and_unii(make_registry):
    ndc = respx.get(f"{API}/ndcstatus.json").mock(return_value=_fx("ndcstatus_keytruda.json"))
    unii = respx.get(f"{API}/rxcui.json").mock(return_value=_fx("rxcui_unii.json"))
    _mock_properties()
    registry = make_registry("rxnorm")

    result = await registry.call("rxnorm", "find_by_id", {"id_type": "NDC", "id": "0006-3026-02"})
    assert ndc.calls.last.request.url.params["ndc"] == "00006302602"
    rec = result.records[0]
    assert rec.id == "1657751" and rec.attributes["tty"] == "SBD"
    assert rec.identifiers == {"rxcui": "1657751", "ndc": "00006302602"}
    assert rec.attributes["ndc_status"] == "ACTIVE"

    result = await registry.call("rxnorm", "find_by_id", {"id_type": "UNII", "id": "9100l32l2n"})
    sent = unii.calls.last.request.url.params
    assert sent["idtype"] == "UNII_CODE" and sent["id"] == "9100L32L2N"
    assert result.records[0].identifiers == {"rxcui": "6809", "unii": "9100L32L2N"}

    respx.get(f"{API}/rxcui.json").mock(return_value=httpx.Response(200, json={"idGroup": {}}))
    none = await registry.call("rxnorm", "find_by_id", {"id_type": "DRUGBANK", "id": "DB99999"})
    assert none.records == [] and "no RxNorm concept" in none.notes[0]


@respx.mock
async def test_ndcs_for_rxcui_active_and_history(make_registry):
    route = respx.get(f"{API}/ndcproperties.json").mock(return_value=_fx("ndcproperties_1657751.json"))
    respx.get(f"{API}/rxcui/860975/allhistoricalndcs.json").mock(return_value=_fx("historicalndcs_860975.json"))
    registry = make_registry("rxnorm")
    result = await registry.call("rxnorm", "ndcs_for_rxcui", {"rxcui": "1657751"})
    sent = route.calls.last.request.url.params
    assert sent["id"] == "1657751" and sent["ndcstatus"] == "active"
    row = result.data[0]
    assert row["ndc11"] == "00006302601" and row["application"] == "BLA125514"
    assert row["spl_set_id"] == "9333c79b-d487-4538-a9f0-71b91a02b287"
    assert result.total == 3

    hist = await registry.call("rxnorm", "ndcs_for_rxcui", {"rxcui": "860975", "status": "all", "limit": 2})
    assert hist.total == 4 and len(hist.data) == 2 and hist.data[0]["start"]
    assert any("showing 2 of 4" in n for n in hist.notes)


@respx.mock
async def test_ndcs_for_ingredient_explains(make_registry):
    respx.get(f"{API}/ndcproperties.json").mock(return_value=httpx.Response(200, json={}))
    _mock_properties()
    registry = make_registry("rxnorm")
    result = await registry.call("rxnorm", "ndcs_for_rxcui", {"rxcui": "6809"})
    assert result.data == [] and "term type IN" in result.notes[0]


# ------------------------------------------------------------------ live
@pytest.mark.live
async def test_live_all_tools(live_registry):
    registry = live_registry("rxnorm")
    found = await registry.call("rxnorm", "search", {"query": "metformin", "limit": 5})
    assert found.records[0].identifiers["rxcui"] == "6809"

    concept = await registry.call("rxnorm", "get_concept", {"rxcui": "6809"})
    assert concept.records[0].identifiers["unii"] == "9100L32L2N"
    assert concept.records[0].attributes["related"]["BN"]

    classes = await registry.call("rxnorm", "drug_classes", {"drug_name": "metformin", "class_type": "atc"})
    assert any(r.identifiers.get("atc") == "A10BA" for r in classes.records)

    by_ndc = await registry.call("rxnorm", "find_by_id", {"id_type": "NDC", "id": "00006302602"})
    assert by_ndc.records and by_ndc.records[0].id == "1657751"
    by_atc = await registry.call("rxnorm", "find_by_id", {"id_type": "ATC", "id": "A10BA02"})
    assert any(r.id == "6809" for r in by_atc.records)

    ndcs = await registry.call("rxnorm", "ndcs_for_rxcui", {"rxcui": "1657751"})
    assert ndcs.data and ndcs.data[0]["ndc11"].startswith("00006")
