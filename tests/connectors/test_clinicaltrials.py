"""ClinicalTrials.gov API v2 connector tests (offline respx mocks + live)."""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import load_fixture

API = "https://clinicaltrials.gov/api/v2"


@respx.mock
async def test_search_studies_params_and_normalization(make_registry):
    route = respx.get(f"{API}/studies").mock(
        return_value=httpx.Response(200, json=load_fixture("clinicaltrials", "search_studies.json"))
    )
    registry = make_registry("clinicaltrials")
    result = await registry.call(
        "clinicaltrials",
        "search_studies",
        {
            "query": "",
            "condition": "melanoma",
            "intervention": "pembrolizumab",
            "phase": ["PHASE3"],
            "status": ["RECRUITING", "COMPLETED"],
            "start_date_from": "2020",
            "limit": 2,
            "sort": "start_date",
        },
    )
    sent = route.calls.last.request.url.params
    assert "query.term" not in sent
    assert sent["query.cond"] == "melanoma"
    assert sent["query.intr"] == "pembrolizumab"
    assert sent["filter.overallStatus"] == "RECRUITING,COMPLETED"
    assert sent["filter.advanced"] == "AREA[Phase]PHASE3 AND AREA[StartDate]RANGE[2020,MAX]"
    assert sent["pageSize"] == "2"
    assert sent["countTotal"] == "true"
    assert sent["sort"] == "StartDate:desc"
    assert "NCTId" in sent["fields"]

    assert result.total == 29
    assert result.next_cursor == "ZVNj7o2Elu8o3lpzW96v4q2tmpOQJJxuYPKh2fUW"
    rec = result.records[0]
    assert rec.id == "NCT05665595"
    assert rec.type == "trial"
    assert rec.url == "https://clinicaltrials.gov/study/NCT05665595"
    assert rec.identifiers["nct"] == "NCT05665595"
    assert rec.identifiers["eu_ct"] == "2022-501417-31-00"
    assert rec.date == "2023-01-19"
    assert rec.title.startswith("A Study of Adjuvant Pembrolizumab/Vibostolimab")
    assert rec.attributes["phases"] == ["PHASE3"]
    assert rec.attributes["status"] == "TERMINATED"
    assert "Melanoma [D008545]" in rec.attributes["condition_mesh"]
    assert rec.attributes["enrollment"] == 1594
    assert "39230120" in rec.attributes["reference_pmids"]
    # markdown escapes removed from summaries
    assert all("\\[" not in (r.summary or "") for r in result.records)


@respx.mock
async def test_search_pagination_cursor_is_sent(make_registry):
    route = respx.get(f"{API}/studies").mock(return_value=httpx.Response(200, json={"studies": []}))
    registry = make_registry("clinicaltrials")
    result = await registry.call("clinicaltrials", "search_studies", {"query": "asthma", "cursor": "TOKEN123"})
    sent = route.calls.last.request.url.params
    assert sent["pageToken"] == "TOKEN123"
    assert sent["query.term"] == "asthma"
    assert result.records == [] and result.next_cursor is None and result.notes


@respx.mock
async def test_get_study_detail(make_registry):
    route = respx.get(f"{API}/studies/NCT02362594").mock(
        return_value=httpx.Response(200, json=load_fixture("clinicaltrials", "study_NCT02362594.json"))
    )
    registry = make_registry("clinicaltrials")
    result = await registry.call("clinicaltrials", "get_study", {"nct": " nct02362594 "})
    assert route.called
    rec = result.records[0]
    assert rec.identifiers == {"nct": "NCT02362594", "euctr": "2014-004944-37", "eu_ct": "2023-509136-25-00"}
    assert rec.date == "2015-07-16"
    attrs = rec.attributes
    assert attrs["lead_sponsor"] == "Merck Sharp & Dohme LLC"
    assert attrs["has_results"] is True
    assert "nih_grants" not in attrs  # upstream bogus NIH ids ("2014") are dropped
    assert attrs["primary_outcomes"]
    assert attrs["eligibility"]["sex"]
    assert attrs["references"][0]["pmid"]
    assert attrs["location_count"] == 0  # this EORTC trial lists no sites
    assert "\\[" not in rec.summary
    assert any("get_study_results" in n for n in result.notes)


@respx.mock
async def test_get_study_not_found_and_invalid(make_registry):
    respx.get(f"{API}/studies/NCT99999999").mock(return_value=httpx.Response(404, text="Study not found"))
    registry = make_registry("clinicaltrials")
    result = await registry.call("clinicaltrials", "get_study", {"nct": "NCT99999999"})
    assert result.records == [] and "not found" in result.notes[0]
    bad = await registry.call("clinicaltrials", "get_study", {"nct": "12345"})
    assert bad.records == [] and "not a valid NCT" in bad.notes[0]


@respx.mock
async def test_get_study_results_summary(make_registry):
    respx.get(f"{API}/studies/NCT02362594").mock(
        return_value=httpx.Response(200, json=load_fixture("clinicaltrials", "study_NCT02362594.json"))
    )
    registry = make_registry("clinicaltrials")
    result = await registry.call(
        "clinicaltrials", "get_study_results", {"nct": "NCT02362594", "max_outcomes": 1, "top_adverse_events": 3}
    )
    data = result.data
    started = data["participant_flow"]["periods"][0]["milestones"][0]
    assert started["milestone"] == "STARTED"
    assert started["counts"] == {"Pembrolizumab": 514, "Placebo": 505}
    assert data["baseline"]["participants"]["Total"] == 1019
    assert len(data["outcome_measures"]) == 1 and data["outcome_measures_omitted"] == 1
    assert data["outcome_measures"][0]["title"]
    ae = data["adverse_events"]
    assert len(ae["top_serious_events"]) == 3
    counts = [e["affected_total"] for e in ae["top_other_events"]]
    assert counts == sorted(counts, reverse=True)
    assert result.records[0].identifiers["nct"] == "NCT02362594"


@respx.mock
async def test_get_study_results_without_results(make_registry):
    study = load_fixture("clinicaltrials", "study_NCT02362594.json")
    study.pop("resultsSection")
    study["hasResults"] = False
    respx.get(f"{API}/studies/NCT02362594").mock(return_value=httpx.Response(200, json=study))
    registry = make_registry("clinicaltrials")
    result = await registry.call("clinicaltrials", "get_study_results", {"nct": "NCT02362594"})
    assert result.data is None and "no posted results" in result.notes[0]


@respx.mock
async def test_studies_by_identifier_pmid_and_nct(make_registry):
    route = respx.get(f"{API}/studies").mock(
        return_value=httpx.Response(200, json=load_fixture("clinicaltrials", "by_pmid.json"))
    )
    registry = make_registry("clinicaltrials")
    result = await registry.call("clinicaltrials", "studies_by_identifier", {"id_type": "pmid", "ids": ["PMID:39230120"]})
    assert route.calls.last.request.url.params["query.term"] == "AREA[ReferencePMID]39230120"
    assert [r.id for r in result.records] == ["NCT05665595", "NCT05226598", "NCT05224141"]

    result = await registry.call(
        "clinicaltrials", "studies_by_identifier", {"id_type": "nct", "ids": ["nct05665595", "NCT00000001", "bogus"]}
    )
    sent = route.calls.last.request.url.params
    assert sent["filter.ids"] == "NCT05665595,NCT00000001"
    assert any("bogus" in n for n in result.notes)
    assert any("not found: NCT00000001" in n for n in result.notes)


@respx.mock
async def test_count_studies_breakdown(make_registry):
    def responder(request: httpx.Request) -> httpx.Response:
        adv = request.url.params.get("filter.advanced", "")
        total = 40 if adv.endswith("AND AREA[Phase]PHASE2") else 25 if adv.endswith("AND AREA[Phase]PHASE3") else 60
        return httpx.Response(200, json={"totalCount": total, "studies": [{}]})

    route = respx.get(f"{API}/studies").mock(side_effect=responder)
    registry = make_registry("clinicaltrials")
    result = await registry.call(
        "clinicaltrials",
        "count_studies",
        {"condition": "obesity", "intervention": "semaglutide", "phase": ["PHASE2", "PHASE3"], "group_by": "phase"},
    )
    assert route.call_count == 3
    first_sent = route.calls[0].request.url.params
    assert first_sent["pageSize"] == "1" and first_sent["query.cond"] == "obesity"
    assert first_sent["filter.advanced"] == "AREA[Phase](PHASE2 OR PHASE3)"
    assert result.total == 60
    assert result.data["breakdown"] == [{"value": "PHASE2", "count": 40}, {"value": "PHASE3", "count": 25}]


@respx.mock
async def test_field_values(make_registry):
    route = respx.get(f"{API}/stats/field/values").mock(
        return_value=httpx.Response(200, json=load_fixture("clinicaltrials", "field_values.json"))
    )
    registry = make_registry("clinicaltrials")
    result = await registry.call("clinicaltrials", "field_values", {"fields": ["Phase"], "top": 3})
    assert route.calls.last.request.url.params["fields"] == "Phase"
    entry = result.data[0]
    assert entry["field"] == "Phase" and len(entry["top_values"]) == 3
    assert entry["top_values"][0]["studies"] > 0


# ------------------------------------------------------------------ live
@pytest.mark.live
async def test_live_all_tools(live_registry):
    registry = live_registry("clinicaltrials")
    found = await registry.call(
        "clinicaltrials",
        "search_studies",
        {"query": "", "condition": "melanoma", "intervention": "pembrolizumab", "phase": ["PHASE3"], "limit": 3},
    )
    assert found.records and found.total and found.next_cursor
    nct = found.records[0].identifiers["nct"]
    page2 = await registry.call("clinicaltrials", "search_studies", {"query": "melanoma pembrolizumab", "cursor": None})
    assert page2.records

    detail = await registry.call("clinicaltrials", "get_study", {"nct": nct})
    assert detail.records[0].id == nct and detail.records[0].attributes.get("primary_outcomes")

    results = await registry.call("clinicaltrials", "get_study_results", {"nct": "NCT02362594"})
    assert results.data and results.data.get("outcome_measures")

    by_pmid = await registry.call("clinicaltrials", "studies_by_identifier", {"id_type": "pmid", "ids": ["39230120"]})
    assert any(r.id == "NCT05665595" for r in by_pmid.records)

    counts = await registry.call(
        "clinicaltrials", "count_studies", {"condition": "obesity", "intervention": "semaglutide", "group_by": "none"}
    )
    assert counts.total and counts.total > 50

    stats = await registry.call("clinicaltrials", "field_values", {"fields": ["Phase"]})
    assert stats.data and stats.data[0]["top_values"]
