"""Clarivate Web of Science (API Expanded) connector tests (respx offline + live)."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.http import SourceError
from tests.conftest import load_fixture

API = "https://wos-api.clarivate.com/api/wos"


@respx.mock
async def test_search_documents_wraps_topic_and_normalizes(make_registry):
    route = respx.get(f"{API}/").mock(return_value=httpx.Response(200, json=load_fixture("clarivate", "search.json")))
    registry = make_registry("clarivate")
    result = await registry.call(
        "clarivate", "search_documents",
        {"query": "cas9 dual-RNA endonuclease", "limit": 5, "year_from": 2012, "year_to": 2012, "sort": "times_cited"},
    )
    request = route.calls.last.request
    assert request.headers["X-ApiKey"] == "test-key"
    sent = request.url.params
    assert sent["databaseId"] == "WOS"
    assert sent["usrQuery"] == "(TS=(cas9 dual-RNA endonuclease)) AND PY=(2012-2012)"
    assert sent["count"] == "5" and sent["firstRecord"] == "1"
    assert sent["sortField"] == "TC D"
    assert "test-key" not in result.request_url

    assert result.total == 1 and result.next_cursor is None
    record = result.records[0]
    assert record.id == "WOS:000307535600036"
    assert record.identifiers == {
        "wos_uid": "WOS:000307535600036",
        "doi": "10.1126/science.1225829",
        "pmid": "22745249",
        "issn": "0036-8075",
    }
    assert record.title == "A Programmable Dual-RNA-Guided DNA Endonuclease in Adaptive Bacterial Immunity"
    assert record.url == "https://www.webofscience.com/wos/woscc/full-record/WOS:000307535600036"
    assert record.date == "2012-08-17"
    assert record.authors[0] == "Martin Jinek" and "Jennifer A. Doudna" in record.authors
    assert record.summary.startswith("Clustered regularly interspaced short palindromic repeats")
    attrs = record.attributes
    assert attrs["journal"] == "SCIENCE"
    assert attrs["times_cited"] == 12993 and attrs["times_cited_all_databases"] == 18571
    assert attrs["eissn"] == "1095-9203"
    assert attrs["citation_topic"] == "Genome Editing"
    assert attrs["wos_categories"] == ["Multidisciplinary Sciences"]
    assert "0000-0001-9161-999X" in attrs["author_orcids"]


@respx.mock
async def test_search_documents_passes_advanced_query_and_paginates(make_registry):
    route = respx.get(f"{API}/").mock(return_value=httpx.Response(200, json=load_fixture("clarivate", "citing.json")))
    registry = make_registry("clarivate")
    result = await registry.call("clarivate", "search_documents", {"query": "TI=(crispr) AND PY=2026", "limit": 2, "cursor": "10"})
    sent = route.calls.last.request.url.params
    assert sent["usrQuery"] == "TI=(crispr) AND PY=2026"
    assert sent["firstRecord"] == "11" and sent["sortField"] == "RS D"
    assert result.total == 12286 and result.next_cursor == "12"


@respx.mock
async def test_get_document_routing(make_registry):
    uid_route = respx.get(f"{API}/id/WOS:000307535600036").mock(
        return_value=httpx.Response(200, json=load_fixture("clarivate", "search.json"))
    )
    query_route = respx.get(f"{API}/").mock(return_value=httpx.Response(200, json=load_fixture("clarivate", "search.json")))
    registry = make_registry("clarivate")
    by_uid = await registry.call("clarivate", "get_document", {"identifier": "000307535600036"})
    assert uid_route.called and by_uid.records[0].id == "WOS:000307535600036"
    await registry.call("clarivate", "get_document", {"identifier": "22745249"})
    assert query_route.calls.last.request.url.params["usrQuery"] == "PMID=(22745249)"
    await registry.call("clarivate", "get_document", {"identifier": "https://doi.org/10.1126/SCIENCE.1225829"})
    assert query_route.calls.last.request.url.params["usrQuery"] == 'DO=("10.1126/science.1225829")'


@respx.mock
async def test_get_document_not_found(make_registry):
    respx.get(f"{API}/id/WOS:000000000000001").mock(
        return_value=httpx.Response(200, json=load_fixture("clarivate", "empty.json"))
    )
    registry = make_registry("clarivate")
    result = await registry.call("clarivate", "get_document", {"identifier": "WOS:000000000000001"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_citing_documents(make_registry):
    route = respx.get(f"{API}/citing").mock(return_value=httpx.Response(200, json=load_fixture("clarivate", "citing.json")))
    registry = make_registry("clarivate")
    result = await registry.call("clarivate", "citing_documents", {"uid": "WOS:000307535600036", "limit": 2})
    sent = route.calls.last.request.url.params
    assert sent["uniqueId"] == "WOS:000307535600036" and sent["count"] == "2" and sent["sortField"] == "PY D"
    assert [r.id for r in result.records] == ["WOS:001903724100001", "WOS:001902145400001"]
    assert result.total == 12286 and result.next_cursor == "2"
    assert all(r.identifiers.get("wos_uid") for r in result.records)


@respx.mock
async def test_cited_references(make_registry):
    route = respx.get(f"{API}/references").mock(
        return_value=httpx.Response(200, json=load_fixture("clarivate", "references.json"))
    )
    registry = make_registry("clarivate")
    result = await registry.call("clarivate", "cited_references", {"uid": "WOS:000307535600036", "limit": 2})
    assert route.calls.last.request.url.params["uniqueId"] == "WOS:000307535600036"
    first = result.records[0]
    assert first.id == "WOS:000305095400010"
    assert first.identifiers == {"wos_uid": "WOS:000305095400010", "doi": "10.1016/j.molcel.2012.03.020"}
    assert first.title == "Mechanism of Foreign DNA Selection in a Bacterial Adaptive Immune System"
    assert first.authors == ["Dipali G. Sashital"] and first.date == "2012"
    assert first.attributes["times_cited"] == 302 and first.attributes["cited_work"] == "MOLECULAR CELL"
    assert result.total == 38 and result.next_cursor == "2"


@respx.mock
async def test_related_documents(make_registry):
    route = respx.get(f"{API}/related").mock(return_value=httpx.Response(200, json=load_fixture("clarivate", "citing.json")))
    registry = make_registry("clarivate")
    result = await registry.call("clarivate", "related_documents", {"uid": "WOS:000307535600036", "limit": 2, "cursor": "2"})
    sent = route.calls.last.request.url.params
    assert sent["uniqueId"] == "WOS:000307535600036" and sent["firstRecord"] == "3"
    assert len(result.records) == 2 and result.next_cursor == "4"


@respx.mock
async def test_citation_counts(make_registry):
    route = respx.get(f"{API}/").mock(return_value=httpx.Response(200, json=load_fixture("clarivate", "search.json")))
    registry = make_registry("clarivate")
    result = await registry.call(
        "clarivate", "citation_counts", {"dois": ["10.1126/science.1225829", "doi:10.9999/NONE"]}
    )
    assert route.calls.last.request.url.params["usrQuery"] == 'DO=("10.1126/science.1225829" OR "10.9999/none")'
    assert result.data["counts"] == {
        "10.1126/science.1225829": {
            "wos_uid": "WOS:000307535600036", "times_cited": 12993, "times_cited_all_databases": 18571,
        }
    }
    assert result.data["not_found"] == ["10.9999/none"]


@respx.mock
async def test_entitlement_error_is_explained(make_registry):
    respx.get(f"{API}/").mock(return_value=httpx.Response(403, json=load_fixture("clarivate", "error_403.json")))
    registry = make_registry("clarivate")
    with pytest.raises(SourceError) as info:
        await registry.call("clarivate", "search_documents", {"query": "x"})
    assert info.value.status == 403
    assert "You cannot consume this service" in info.value.message
    assert "API Expanded" in info.value.message


@respx.mock
async def test_bad_query_400_propagates(make_registry):
    respx.get(f"{API}/").mock(
        return_value=httpx.Response(400, json={"code": "Bad Request", "message": "Invalid sort field:X"})
    )
    registry = make_registry("clarivate")
    with pytest.raises(SourceError, match="Invalid sort field"):
        await registry.call("clarivate", "search_documents", {"query": "TS=(x)"})


# ---------------------------------------------------------------- live
@pytest.mark.live
async def test_live_search_and_document(live_registry):
    registry = live_registry("clarivate")
    result = await registry.call(
        "clarivate", "search_documents", {"query": "tirzepatide obesity", "limit": 3, "sort": "times_cited"}
    )
    assert result.records and result.total
    assert all(r.identifiers["wos_uid"].startswith("WOS:") for r in result.records)
    doc = await registry.call("clarivate", "get_document", {"identifier": "10.1126/science.1225829"})
    assert doc.records[0].id == "WOS:000307535600036"
    assert doc.records[0].identifiers["pmid"] == "22745249"
    assert doc.records[0].attributes["times_cited"] > 10000


@pytest.mark.live
async def test_live_citation_network(live_registry):
    registry = live_registry("clarivate")
    uid = "WOS:000307535600036"
    citing = await registry.call("clarivate", "citing_documents", {"uid": uid, "limit": 2})
    assert citing.records and citing.total > 10000
    refs = await registry.call("clarivate", "cited_references", {"uid": uid, "limit": 5})
    assert refs.records and refs.total >= 30
    related = await registry.call("clarivate", "related_documents", {"uid": uid, "limit": 2})
    assert related.records
    counts = await registry.call(
        "clarivate", "citation_counts", {"dois": ["10.1126/science.1225829", "10.1038/s41586-019-1711-4"]}
    )
    assert set(counts.data["counts"]) == {"10.1126/science.1225829", "10.1038/s41586-019-1711-4"}
