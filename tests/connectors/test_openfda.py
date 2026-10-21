"""openFDA connector tests (offline respx mocks + live)."""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import load_fixture

API = "https://api.fda.gov"
NOT_FOUND = {"error": {"code": "NOT_FOUND", "message": "No matches found!"}}


def _json(name: str) -> httpx.Response:
    return httpx.Response(200, json=load_fixture("openfda", name))


@respx.mock
async def test_search_drug_labels_by_name(make_registry):
    route = respx.get(f"{API}/drug/label.json").mock(return_value=_json("labels_keytruda.json"))
    registry = make_registry("openfda")
    result = await registry.call("openfda", "search_drug_labels", {"query": "Keytruda", "limit": 2})
    sent = route.calls.last.request.url.params
    assert sent["search"] == (
        'openfda.generic_name:"Keytruda" OR openfda.brand_name:"Keytruda" OR openfda.substance_name:"Keytruda"'
    )
    assert sent["limit"] == "2"
    assert sent["api_key"] == "test-key"
    assert "test-key" not in result.request_url and "api_key=***" in result.request_url
    assert result.total == 2 and result.next_cursor is None

    combo, mono = result.records
    assert mono.id == "9333c79b-d487-4538-a9f0-71b91a02b287"
    assert mono.type == "drug_label"
    assert mono.title == "KEYTRUDA (pembrolizumab)"
    assert mono.url.endswith("setid=9333c79b-d487-4538-a9f0-71b91a02b287")
    assert mono.date == "2026-07-10"
    assert mono.identifiers["unii"] == "DPT0O3T46P"
    assert mono.identifiers["fda_application"] == "BLA125514"
    assert mono.identifiers["spl_set_id"] == mono.id
    assert mono.identifiers["rxcui"].isdigit()
    assert not mono.summary.upper().startswith("1 INDICATIONS")  # section heading stripped
    assert mono.attributes["manufacturer"] == "Merck Sharp & Dohme LLC"
    # combination product: no single UNII in identifiers, all UNIIs in attributes
    assert "unii" not in combo.identifiers and len(combo.attributes["uniis"]) == 2


@respx.mock
async def test_search_drug_labels_full_text_fallback_and_cursor(make_registry):
    calls: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        search = request.url.params["search"]
        calls.append(search)
        if search.startswith("openfda.generic_name"):
            return httpx.Response(404, json=NOT_FOUND)
        payload = load_fixture("openfda", "labels_keytruda.json")
        payload["meta"]["results"]["total"] = 50
        return httpx.Response(200, json=payload)

    respx.get(f"{API}/drug/label.json").mock(side_effect=responder)
    registry = make_registry("openfda")
    result = await registry.call("openfda", "search_drug_labels", {"query": "PD-1 melanoma", "limit": 2})
    assert calls[-1] == "(PD-1 AND melanoma)"
    assert result.next_cursor == "text:2"
    assert any("full-text" in n for n in result.notes)

    page2 = await registry.call(
        "openfda", "search_drug_labels", {"query": "PD-1 melanoma", "limit": 2, "cursor": result.next_cursor}
    )
    assert calls[-1] == "(PD-1 AND melanoma)" and page2.next_cursor == "text:4"


@respx.mock
async def test_search_drug_labels_raw_syntax_and_bad_query(make_registry):
    route = respx.get(f"{API}/drug/label.json").mock(
        return_value=httpx.Response(400, json={"error": {"code": "BAD_REQUEST", "message": "Invalid search"}})
    )
    registry = make_registry("openfda")
    result = await registry.call(
        "openfda", "search_drug_labels", {"query": "boxed_warning:suicidal", "product_type": "prescription"}
    )
    assert route.calls.last.request.url.params["search"] == (
        'boxed_warning:suicidal AND openfda.product_type:"HUMAN PRESCRIPTION DRUG"'
    )
    assert result.records == [] and "rejected the query: Invalid search" in result.notes[0]


@respx.mock
async def test_get_drug_label_by_rxcui(make_registry):
    payload = load_fixture("openfda", "labels_keytruda.json")
    payload["results"] = payload["results"][1:]
    route = respx.get(f"{API}/drug/label.json").mock(return_value=httpx.Response(200, json=payload))
    registry = make_registry("openfda")
    result = await registry.call(
        "openfda", "get_drug_label", {"id_type": "rxcui", "value": "1657746", "section_chars": 300}
    )
    assert route.calls.last.request.url.params["search"] == 'openfda.rxcui:"1657746"'
    rec = result.records[0]
    assert rec.title == "KEYTRUDA (pembrolizumab)"
    assert rec.attributes["mechanism_of_action"]
    assert len(rec.attributes["indications_and_usage"]) <= 305
    assert rec.attributes["dosage_and_administration"]


@respx.mock
async def test_get_drug_label_not_found(make_registry):
    respx.get(f"{API}/drug/label.json").mock(return_value=httpx.Response(404, json=NOT_FOUND))
    registry = make_registry("openfda")
    result = await registry.call("openfda", "get_drug_label", {"value": "00000000-0000-0000-0000-000000000000"})
    assert result.records == [] and "no FDA label found" in result.notes[0]


@respx.mock
async def test_adverse_event_counts(make_registry):
    def responder(request: httpx.Request) -> httpx.Response:
        if "count" in request.url.params:
            return _json("event_counts.json")
        return _json("event_meta.json")

    route = respx.get(f"{API}/drug/event.json").mock(side_effect=responder)
    registry = make_registry("openfda")
    result = await registry.call(
        "openfda",
        "adverse_event_counts",
        {"drug": "semaglutide", "limit": 5, "date_from": "2020-01-01", "serious_only": True},
    )
    first = route.calls[0].request.url.params
    assert first["count"] == "patient.reaction.reactionmeddrapt.exact"
    assert first["limit"] == "5"
    assert first["search"] == (
        '(patient.drug.openfda.generic_name:"semaglutide" OR patient.drug.openfda.brand_name:"semaglutide" '
        'OR patient.drug.medicinalproduct:"semaglutide") AND serious:1 AND receivedate:[20200101 TO 29991231]'
    )
    rows = result.data["rows"]
    assert rows[0] == {"term": "NAUSEA", "count": rows[0]["count"]} and len(rows) == 5
    assert result.data["total_reports"] == result.total and result.total > 0
    assert any("causation" in n for n in result.notes)


@respx.mock
async def test_adverse_event_counts_by_year_and_codes(make_registry):
    route = respx.get(f"{API}/drug/event.json").mock(side_effect=lambda r: (
        _json("event_counts_year.json") if r.url.params.get("count") == "receivedate"
        else httpx.Response(200, json={"results": [{"term": 1, "count": 7}, {"term": 2, "count": 3}]})
        if "count" in r.url.params else _json("event_meta.json")
    ))
    registry = make_registry("openfda")
    years = await registry.call("openfda", "adverse_event_counts", {"drug": "semaglutide", "count_by": "year"})
    assert "limit" not in route.calls[0].request.url.params
    expected = sum(b["count"] for b in load_fixture("openfda", "event_counts_year.json")["results"])
    assert years.data["rows"] == [{"term": "2018", "count": expected}]  # daily buckets summed per year
    sex = await registry.call("openfda", "adverse_event_counts", {"drug": "semaglutide", "count_by": "sex"})
    assert sex.data["rows"] == [{"term": "male", "count": 7}, {"term": "female", "count": 3}]


async def test_invalid_date_rejected(make_registry):
    from mosaic.http import SourceError

    registry = make_registry("openfda")
    with pytest.raises(SourceError, match="invalid parameters"):
        await registry.call("openfda", "adverse_event_counts", {"drug": "x", "date_from": "last year"})


@respx.mock
async def test_search_adverse_events(make_registry):
    route = respx.get(f"{API}/drug/event.json").mock(return_value=_json("events_pembro_myocarditis.json"))
    registry = make_registry("openfda")
    result = await registry.call(
        "openfda",
        "search_adverse_events",
        {"drug": "pembrolizumab", "reaction": "myocarditis", "fatal_only": True, "limit": 2},
    )
    sent = route.calls.last.request.url.params
    assert 'patient.reaction.reactionmeddrapt:"myocarditis"' in sent["search"]
    assert "seriousnessdeath:1" in sent["search"]
    assert result.total == 2144 and result.next_cursor == "2"
    rec = result.records[0]
    assert rec.id == "10617396" and rec.type == "adverse_event"
    assert rec.date == "2014-12-01"
    assert rec.title.startswith("pembrolizumab:") and "Myocarditis" in rec.title
    assert rec.identifiers == {}
    assert rec.attributes["drugs"][0]["role"] == "suspect"  # suspects listed first
    assert rec.attributes["reactions"][0]["term"] == "Myocarditis"


@respx.mock
async def test_drug_approvals(make_registry):
    route = respx.get(f"{API}/drug/drugsfda.json").mock(return_value=_json("drugsfda_semaglutide.json"))
    registry = make_registry("openfda")
    result = await registry.call("openfda", "drug_approvals", {"drug": "semaglutide", "limit": 2, "max_submissions": 2})
    assert 'products.active_ingredients.name:"semaglutide"' in route.calls.last.request.url.params["search"]
    rec = result.records[0]
    assert rec.id == "NDA215256"
    assert rec.identifiers["fda_application"] == "NDA215256"
    assert rec.date == "2021-06-04"  # ORIG approval
    assert rec.url.endswith("ApplNo=215256")
    assert rec.attributes["sponsor"] == "NOVO"
    assert len(rec.attributes["recent_submissions"]) == 2
    assert rec.attributes["recent_submissions"][0]["date"] == "2025-10-14"  # newest first
    assert result.next_cursor == "2"

    empty = await registry.call("openfda", "drug_approvals", {})
    assert empty.records == [] and "give drug" in empty.notes[0]


@respx.mock
async def test_drug_recalls(make_registry):
    route = respx.get(f"{API}/drug/enforcement.json").mock(return_value=_json("enforcement_nitrosamine.json"))
    registry = make_registry("openfda")
    result = await registry.call(
        "openfda", "drug_recalls", {"reason": "nitrosamine", "classification": "Class II", "limit": 2}
    )
    sent = route.calls.last.request.url.params
    assert sent["search"] == 'reason_for_recall:nitrosamine AND classification:"Class II"'
    assert sent["sort"] == "recall_initiation_date:desc"
    rec = result.records[0]
    assert rec.id == "D-0555-2026" and rec.type == "recall"
    assert rec.date == "2026-05-14"
    assert "Nitrosamine" in rec.summary
    assert "rxcui" not in rec.identifiers and "unii" not in rec.identifiers


@respx.mock
async def test_ndc_lookup_eleven_digit(make_registry):
    route = respx.get(f"{API}/drug/ndc.json").mock(return_value=_json("ndc_keytruda.json"))
    registry = make_registry("openfda")
    result = await registry.call("openfda", "ndc_lookup", {"ndc": "00006302602"})
    search = route.calls.last.request.url.params["search"]
    assert 'packaging.package_ndc:"00006-3026-02"' in search and 'packaging.package_ndc:"0006-3026-02"' in search
    rec = result.records[0]
    assert rec.id == "0006-3026" and rec.identifiers["ndc"] == "0006-3026"
    assert rec.identifiers["unii"] == "DPT0O3T46P"
    assert rec.attributes["labeler"]


@respx.mock
async def test_ndc_lookup_not_found(make_registry):
    respx.get(f"{API}/drug/ndc.json").mock(return_value=httpx.Response(404, json=NOT_FOUND))
    registry = make_registry("openfda")
    result = await registry.call("openfda", "ndc_lookup", {"ndc": "9999-9999"})
    assert result.records == [] and "no NDC" in result.notes[0]


# ------------------------------------------------------------------ live
@pytest.mark.live
async def test_live_all_tools(live_registry):
    registry = live_registry("openfda")
    labels = await registry.call("openfda", "search_drug_labels", {"query": "pembrolizumab", "limit": 3})
    assert labels.records and labels.records[0].identifiers.get("spl_set_id")
    assert "api_key=" not in (labels.request_url or "") or "api_key=***" in labels.request_url

    label = await registry.call("openfda", "get_drug_label", {"id_type": "unii", "value": "DPT0O3T46P"})
    assert label.records and label.records[0].attributes.get("indications_and_usage")

    counts = await registry.call("openfda", "adverse_event_counts", {"drug": "semaglutide", "limit": 5})
    assert counts.data["rows"] and counts.total

    events = await registry.call("openfda", "search_adverse_events", {"drug": "pembrolizumab", "limit": 2})
    assert events.records and events.next_cursor

    approvals = await registry.call("openfda", "drug_approvals", {"application_number": "NDA209637"})
    assert approvals.records[0].attributes["original_approval_date"] == "2017-12-05"

    recalls = await registry.call("openfda", "drug_recalls", {"reason": "nitrosamine", "limit": 2})
    assert recalls.records

    ndc = await registry.call("openfda", "ndc_lookup", {"ndc": "0006-3026"})
    assert ndc.records and ndc.records[0].identifiers["ndc"] == "0006-3026"
