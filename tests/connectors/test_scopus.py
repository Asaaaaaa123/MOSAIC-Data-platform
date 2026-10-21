"""Scopus / ScienceDirect connector tests (respx offline + live)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mosaic.http import SourceError
from tests.conftest import load_fixture

API = "https://api.elsevier.com/content"


@respx.mock
async def test_search_wraps_plain_text_and_normalizes(make_registry):
    route = respx.get(f"{API}/search/scopus").mock(
        return_value=httpx.Response(200, json=load_fixture("scopus", "search.json"))
    )
    registry = make_registry("scopus")
    result = await registry.call(
        "scopus", "search", {"query": "crispr", "limit": 2, "year_from": 2021, "open_access": True, "sort": "cited"}
    )

    request = route.calls.last.request
    assert request.headers["X-ELS-APIKey"] == "test-key"
    assert "X-ELS-Insttoken" not in request.headers
    sent = request.url.params
    assert sent["query"] == "(TITLE-ABS-KEY(crispr)) AND PUBYEAR > 2020 AND OPENACCESS(1)"
    assert sent["count"] == "2" and sent["start"] == "0" and sent["sort"] == "-citedby-count"
    assert "test-key" not in result.request_url

    assert result.total == 70968
    assert result.next_cursor == "2"
    first, second = result.records
    assert first.id == "2-s2.0-105048028293"
    assert first.identifiers == {
        "scopus_eid": "2-s2.0-105048028293",
        "doi": "10.1016/j.synbio.2026.08.006",
        "issn": "2405-805X",
    }
    assert first.url == "https://www.scopus.com/record/display.uri?eid=2-s2.0-105048028293&origin=inward"
    assert first.date == "2027-04-01"
    assert first.authors == ["Wang X."]
    assert first.attributes["journal"] == "Synthetic and Systems Biotechnology"
    assert first.attributes["citedby_count"] == 0
    assert first.attributes["open_access"] is True
    assert "Shijiazhuang University" in first.attributes["affiliations"]
    assert second.identifiers["pmid"] == "42700952"
    assert second.identifiers["issn"] == "1383-5769"
    assert second.attributes["issns"] == ["1383-5769", "1873-0329"]


@respx.mock
async def test_search_passes_advanced_syntax_and_handles_empty(make_registry):
    route = respx.get(f"{API}/search/scopus").mock(
        return_value=httpx.Response(200, json=load_fixture("scopus", "search_empty.json"))
    )
    registry = make_registry("scopus")
    result = await registry.call("scopus", "search", {"query": "DOI(10.9999/none)", "cursor": "25"})
    assert route.calls.last.request.url.params["query"] == "DOI(10.9999/none)"
    assert route.calls.last.request.url.params["start"] == "25"
    assert result.records == [] and result.total == 0 and result.next_cursor is None
    assert result.notes


async def test_search_limit_capped_at_25(make_registry):
    registry = make_registry("scopus")
    with pytest.raises(SourceError, match="invalid parameters"):
        await registry.call("scopus", "search", {"query": "x", "limit": 50})


@respx.mock
async def test_search_entitlement_error_is_explained(make_registry):
    respx.get(f"{API}/search/scopus").mock(return_value=httpx.Response(401, json=load_fixture("scopus", "error_401.json")))
    registry = make_registry("scopus")
    with pytest.raises(SourceError) as info:
        await registry.call("scopus", "search", {"query": "x"})
    assert info.value.status == 401
    assert "not available with the configured Elsevier API key" in info.value.message
    assert "AUTHORIZATION_ERROR" in info.value.message


@respx.mock
async def test_get_abstract_by_pmid(make_registry):
    route = respx.get(f"{API}/abstract/pubmed_id/23643243").mock(
        return_value=httpx.Response(200, json=load_fixture("scopus", "abstract.json"))
    )
    registry = make_registry("scopus")
    result = await registry.call("scopus", "get_abstract", {"identifier": "23643243"})
    assert route.called
    record = result.records[0]
    assert record.id == "2-s2.0-84877707375"
    assert record.identifiers["doi"] == "10.1016/j.cell.2013.04.025"
    assert record.identifiers["pmid"] == "23643243"
    assert record.identifiers["scopus_eid"] == "2-s2.0-84877707375"
    assert record.title.startswith("One-step generation of mice")
    assert record.date == "2013-05-09"
    assert record.authors == ["Haoyi Wang"]
    assert record.attributes["citedby_count"] == 3074
    assert record.attributes["scopus_author_ids"] == ["24176407700"]
    assert record.attributes["affiliations"][0] == "Massachusetts Institute of Technology, Cambridge, United States"
    assert result.notes  # no abstract with META view


@respx.mock
async def test_get_abstract_doi_and_eid_routing_and_not_found(make_registry):
    doi_route = respx.get(f"{API}/abstract/doi/10.1016/j.cell.2013.04.025").mock(
        return_value=httpx.Response(200, json=load_fixture("scopus", "abstract.json"))
    )
    eid_route = respx.get(f"{API}/abstract/eid/2-s2.0-1").mock(
        return_value=httpx.Response(404, json={"service-error": {"status": {"statusCode": "RESOURCE_NOT_FOUND"}}})
    )
    sid_route = respx.get(f"{API}/abstract/scopus_id/84877707375").mock(
        return_value=httpx.Response(200, json=load_fixture("scopus", "abstract.json"))
    )
    registry = make_registry("scopus")
    await registry.call("scopus", "get_abstract", {"identifier": "https://doi.org/10.1016/J.CELL.2013.04.025"})
    await registry.call("scopus", "get_abstract", {"identifier": "84877707375"})
    missing = await registry.call("scopus", "get_abstract", {"identifier": "2-s2.0-1"})
    assert doi_route.called and sid_route.called and eid_route.called
    assert missing.records == [] and "not found" in missing.notes[0]


@respx.mock
async def test_serial_metrics_by_issn_and_title(make_registry):
    issn_route = respx.get(f"{API}/serial/title/issn/0092-8674").mock(
        return_value=httpx.Response(200, json=load_fixture("scopus", "serial_issn.json"))
    )
    title_route = respx.get(f"{API}/serial/title").mock(
        return_value=httpx.Response(200, json=load_fixture("scopus", "serial_search.json"))
    )
    registry = make_registry("scopus")
    result = await registry.call("scopus", "serial_metrics", {"issn": "00928674"})
    assert issn_route.called
    cell = result.records[0]
    assert cell.type == "journal" and cell.title == "Cell" and cell.id == "source:18434"
    assert cell.identifiers == {"issn": "0092-8674"}
    assert cell.attributes["citescore"] == 67.8
    assert cell.attributes["snip"] == 8.498 and cell.attributes["sjr"] == 24.592
    assert cell.attributes["eissn"] == "1097-4172"

    result = await registry.call("scopus", "serial_metrics", {"title": "lancet", "limit": 2})
    assert title_route.calls.last.request.url.params["title"] == "lancet"
    assert [r.title for r in result.records] == ["Lancet Primary Care", "The Lancet"]
    assert result.records[1].identifiers["issn"] == "0140-6736"


@respx.mock
async def test_serial_metrics_not_found(make_registry):
    respx.get(f"{API}/serial/title/issn/9999-9999").mock(
        return_value=httpx.Response(404, json={"service-error": {"status": {"statusCode": "RESOURCE_NOT_FOUND"}}})
    )
    registry = make_registry("scopus")
    result = await registry.call("scopus", "serial_metrics", {"issn": "9999-9999"})
    assert result.records == [] and "no Scopus journal" in result.notes[0]


@respx.mock
async def test_get_full_text_open_access(make_registry):
    route = respx.get(f"{API}/article/pii/S2405805X26002036").mock(
        return_value=httpx.Response(200, json=load_fixture("scopus", "article_oa.json"))
    )
    registry = make_registry("scopus")
    result = await registry.call(
        "scopus", "get_full_text", {"identifier": "S2405-805X(26)00203-6", "max_chars": 1000}
    )
    assert route.called and "view" not in route.calls.last.request.url.params
    record = result.records[0]
    assert record.id == "S2405805X26002036"
    assert record.identifiers["doi"] == "10.1016/j.synbio.2026.08.006"
    assert record.identifiers["scopus_eid"] == "2-s2.0-105048028293"
    assert record.authors[:2] == ["Xiaoru Wang", "Hongmei Zhai"]
    assert record.summary.startswith("The global emergence of vancomycin-resistant")
    assert record.url == "https://www.sciencedirect.com/science/article/pii/S2405805X26002036"
    assert result.data["text"].startswith("The global emergence")  # XOCS index header skipped
    assert len(result.data["text"]) == 1000 and result.data["truncated"] is True
    assert "truncated" in result.notes[0]
    whole = await registry.call("scopus", "get_full_text", {"identifier": "S2405805X26002036", "max_chars": 100000})
    assert "1 Introduction" in whole.data["text"]
    assert " References " not in whole.data["text"]  # trailing bibliography dropped
    assert whole.data["truncated"] is False


@respx.mock
async def test_get_full_text_paywalled_and_missing(make_registry):
    respx.get(f"{API}/article/doi/10.1016/j.tig.2026.06.007").mock(
        return_value=httpx.Response(200, json=load_fixture("scopus", "article_paywalled.json"))
    )
    respx.get(f"{API}/article/pubmed_id/31634902").mock(
        return_value=httpx.Response(404, json={"service-error": {"status": {"statusCode": "RESOURCE_NOT_FOUND"}}})
    )
    registry = make_registry("scopus")
    result = await registry.call("scopus", "get_full_text", {"identifier": "10.1016/j.tig.2026.06.007"})
    assert result.data is None
    assert result.records[0].summary and "not available" in result.notes[0]
    assert result.records[0].attributes["open_access"] is False
    missing = await registry.call("scopus", "get_full_text", {"identifier": "31634902"})
    assert missing.records == [] and "not found" in missing.notes[0]


_AUTHOR_SEARCH = {  # documented response shape (key lacks the entitlement, so not live-captured)
    "search-results": {
        "opensearch:totalResults": "3",
        "entry": [
            {
                "dc:identifier": "AUTHOR_ID:7006500627",
                "orcid": "0000-0001-9161-999X",
                "preferred-name": {"surname": "Doudna", "given-name": "Jennifer A."},
                "document-count": "512",
                "affiliation-current": {
                    "affiliation-name": "University of California, Berkeley",
                    "affiliation-city": "Berkeley",
                    "affiliation-country": "United States",
                },
                "subject-area": [{"@abbrev": "BIOC", "$": "Biochemistry, Genetics and Molecular Biology"}],
            }
        ],
    }
}


@respx.mock
async def test_search_authors(make_registry):
    route = respx.get(f"{API}/search/author").mock(return_value=httpx.Response(200, json=_AUTHOR_SEARCH))
    registry = make_registry("scopus")
    result = await registry.call(
        "scopus", "search_authors", {"last_name": "Doudna", "first_name": "J", "affiliation": "Berkeley", "limit": 1}
    )
    assert route.calls.last.request.url.params["query"] == "AUTHLASTNAME(Doudna) AND AUTHFIRST(J) AND AFFIL(Berkeley)"
    author = result.records[0]
    assert author.type == "author" and author.id == "7006500627"
    assert author.identifiers == {"orcid": "0000-0001-9161-999X"}
    assert author.title == "Jennifer A. Doudna"
    assert author.attributes["document_count"] == 512
    assert result.next_cursor == "1"


@respx.mock
async def test_search_authors_entitlement(make_registry):
    respx.get(f"{API}/search/author").mock(return_value=httpx.Response(401, json=load_fixture("scopus", "error_401.json")))
    registry = make_registry("scopus")
    with pytest.raises(SourceError, match="Scopus Author Search is not available"):
        await registry.call("scopus", "search_authors", {"orcid": "0000-0001-9161-999X"})


@respx.mock
async def test_sciencedirect_search(make_registry):
    payload = {  # documented ScienceDirect Search v2 response shape
        "resultsFound": 5,
        "results": [
            {
                "authors": [{"order": 2, "name": "B. Second"}, {"order": 1, "name": "A. First"}],
                "doi": "10.1016/j.cell.2013.04.025",
                "openAccess": True,
                "pages": {"first": "910", "last": "918"},
                "pii": "S0092867413004674",
                "publicationDate": "2013-05-09",
                "sourceTitle": "Cell",
                "title": "One-Step Generation of Mice <b>CRISPR</b>",
                "uri": "https://www.sciencedirect.com/science/article/pii/S0092867413004674",
                "volumeIssue": "Volume 153, Issue 4",
            }
        ],
    }
    route = respx.put(f"{API}/search/sciencedirect").mock(return_value=httpx.Response(200, json=payload))
    registry = make_registry("scopus")
    result = await registry.call(
        "scopus", "sciencedirect_search", {"query": "crispr mice", "limit": 1, "year_from": 2010, "open_access": True}
    )
    body = json.loads(route.calls.last.request.content)
    assert body["qs"] == "crispr mice"
    assert body["display"] == {"offset": 0, "show": 1, "sortBy": "relevance"}
    assert body["date"] == "2010-2100" and body["filters"] == {"openAccess": True}
    record = result.records[0]
    assert record.id == "S0092867413004674" and record.identifiers == {"doi": "10.1016/j.cell.2013.04.025"}
    assert record.authors == ["A. First", "B. Second"]
    assert record.title == "One-Step Generation of Mice CRISPR"
    assert result.total == 5 and result.next_cursor == "1"


@respx.mock
async def test_sciencedirect_search_entitlement(make_registry):
    respx.put(f"{API}/search/sciencedirect").mock(
        return_value=httpx.Response(401, json=load_fixture("scopus", "error_401.json"))
    )
    registry = make_registry("scopus")
    with pytest.raises(SourceError, match="ScienceDirect Search is not available"):
        await registry.call("scopus", "sciencedirect_search", {"query": "x"})


# ---------------------------------------------------------------- live
@pytest.mark.live
async def test_live_search_abstract_serial_fulltext(live_registry):
    registry = live_registry("scopus")
    result = await registry.call("scopus", "search", {"query": "prime editing sickle cell", "limit": 3, "sort": "cited"})
    assert result.records and result.total
    assert all(r.identifiers.get("scopus_eid", "").startswith("2-s2.0-") for r in result.records)

    detail = await registry.call("scopus", "get_abstract", {"identifier": "10.1016/j.cell.2013.04.025"})
    assert detail.records[0].identifiers["pmid"] == "23643243"
    assert detail.records[0].attributes["citedby_count"] > 1000

    serial = await registry.call("scopus", "serial_metrics", {"issn": "0092-8674"})
    assert serial.records[0].title == "Cell" and serial.records[0].attributes.get("citescore")

    full = await registry.call("scopus", "get_full_text", {"identifier": "10.1016/j.cell.2013.04.025", "max_chars": 2000})
    assert full.records[0].identifiers["doi"] == "10.1016/j.cell.2013.04.025"
    assert full.data and full.data["chars"] > 10000


@pytest.mark.live
async def test_live_entitlement_limited_tools(live_registry):
    """Author Search and ScienceDirect Search need an institutional entitlement.

    With the probed key both return 401; the connector must explain that instead of crashing.
    If the key gains the entitlement the calls succeed and return records.
    """
    registry = live_registry("scopus")
    for tool, params in (
        ("search_authors", {"last_name": "Doudna", "first_name": "Jennifer", "limit": 2}),
        ("sciencedirect_search", {"query": "CAR-T cytokine release syndrome", "limit": 2}),
    ):
        try:
            result = await registry.call("scopus", tool, params)
        except SourceError as exc:
            assert exc.status in (401, 403) and "entitlement" in exc.message
        else:
            assert result.records
