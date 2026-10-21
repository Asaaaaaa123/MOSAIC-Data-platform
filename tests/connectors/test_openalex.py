"""OpenAlex connector tests (respx offline + live)."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.connectors.openalex import reconstruct_abstract
from tests.conftest import load_fixture

API = "https://api.openalex.org"
SRC = "openalex"


def fx(name):
    return load_fixture(SRC, name)


def test_reconstruct_abstract():
    assert reconstruct_abstract({"editing": [1], "Prime": [0], "works.": [2]}) == "Prime editing works."
    assert reconstruct_abstract(None) is None
    assert reconstruct_abstract({}) is None


@respx.mock
async def test_search_works_params_and_records(make_registry):
    route = respx.get(f"{API}/works").mock(return_value=httpx.Response(200, json=fx("search_works.json")))
    registry = make_registry(SRC)
    result = await registry.call(
        SRC, "search_works", {"query": "prime editing", "limit": 2, "filter": "publication_year:2019-2024"}
    )
    sent = route.calls.last.request.url.params
    assert sent["filter"] == "publication_year:2019-2024,title_and_abstract.search:prime editing"
    assert "search" not in sent
    assert sent["sort"] == "relevance_score:desc"
    assert sent["per-page"] == "2"
    assert sent["cursor"] == "*"
    assert sent["api_key"] == "test-key"
    assert sent["mailto"] == "tests@mosaic.invalid"
    assert "abstract_inverted_index" in sent["select"]
    assert "test-key" not in result.request_url and "api_key=***" in result.request_url
    assert "tests@mosaic.invalid" not in result.request_url
    assert result.total == 2125
    assert result.next_cursor == fx("search_works.json")["meta"]["next_cursor"]
    rec = result.records[0]
    assert rec.id == "W3202931255"
    assert rec.url == "https://openalex.org/W3202931255"
    assert rec.identifiers == {
        "openalex": "W3202931255",
        "doi": "10.1038/s41587-021-01039-7",
        "pmid": "34608327",
        "pmcid": "PMC8930418",
        "issn": "0733-222X",
    }
    assert rec.date == "2021-10-04"
    assert rec.title.startswith("Engineered pegRNAs improve prime editing")
    assert rec.authors[0] == "James W. Nelson"
    assert rec.attributes["grants"] == ["R01 EB031172", "R35GM118062"]
    assert rec.attributes["mesh_ids"] == ["D000072669"]
    assert rec.attributes["journal"] == "Nature Biotechnology"
    second = result.records[1]
    assert second.summary.startswith("Many genetic diseases and undesirable traits")


@respx.mock
async def test_search_works_fulltext_and_filter_only(make_registry):
    route = respx.get(f"{API}/works").mock(
        return_value=httpx.Response(200, json={"meta": {"count": 0}, "results": []})
    )
    registry = make_registry(SRC)
    result = await registry.call(SRC, "search_works", {"query": "base editing", "search_in": "fulltext", "cursor": "abc"})
    sent = route.calls.last.request.url.params
    assert sent["search"] == "base editing" and sent["cursor"] == "abc"
    assert result.next_cursor is None
    await registry.call(SRC, "search_works", {"query": "", "filter": "cites:W2981137429", "sort": "cited_by_count:desc"})
    sent = route.calls.last.request.url.params
    assert sent["filter"] == "cites:W2981137429" and "search" not in sent
    assert sent["sort"] == "cited_by_count:desc"
    empty = await registry.call(SRC, "search_works", {"query": " "})
    assert "provide a query" in empty.notes[0]


@respx.mock
async def test_get_work_by_pmid_and_doi(make_registry):
    pmid = respx.get(f"{API}/works/pmid:31634902").mock(return_value=httpx.Response(200, json=fx("work.json")))
    doi = respx.get(f"{API}/works/doi:10.1038/s41586-019-1711-4").mock(
        return_value=httpx.Response(200, json=fx("work.json"))
    )
    oa = respx.get(f"{API}/works/W2981137429").mock(return_value=httpx.Response(200, json=fx("work.json")))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "get_work", {"id": "pmid:31634902"})
    assert pmid.called
    rec = result.records[0]
    assert rec.identifiers["pmcid"] == "PMC6907074"  # recovered from the PMC location
    assert rec.identifiers["doi"] == "10.1038/s41586-019-1711-4"
    assert rec.attributes["grants"] == ["T32 GM095450", "RM1HG009490"]
    await registry.call(SRC, "get_work", {"id": "https://doi.org/10.1038/S41586-019-1711-4"})
    assert doi.called
    await registry.call(SRC, "get_work", {"id": "https://openalex.org/w2981137429"})
    assert oa.called


@respx.mock
async def test_get_work_by_pmcid_uses_landing_page_filter(make_registry):
    route = respx.get(f"{API}/works").mock(return_value=httpx.Response(200, json=fx("work_by_pmcid.json")))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "get_work", {"id": "PMC6907074"})
    flt = route.calls.last.request.url.params["filter"]
    assert flt == (
        "locations.landing_page_url:https://www.ncbi.nlm.nih.gov/pmc/articles/6907074"
        "|https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6907074"
    )
    assert result.records[0].id == "W2981137429"


@respx.mock
async def test_get_work_not_found(make_registry):
    respx.get(f"{API}/works/W0000000001").mock(return_value=httpx.Response(404, text="<html>404 Not Found</html>"))
    respx.get(f"{API}/works").mock(return_value=httpx.Response(200, json={"meta": {"count": 0}, "results": []}))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "get_work", {"id": "W0000000001"})
    assert result.records == [] and "not found" in result.notes[0]
    result = await registry.call(SRC, "get_work", {"id": "pmcid:PMC1"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_works_group_by_year(make_registry):
    route = respx.get(f"{API}/works").mock(return_value=httpx.Response(200, json=fx("group_by_year.json")))
    registry = make_registry(SRC)
    result = await registry.call(
        SRC, "works_group_by", {"group_by": "publication_year", "filter": "cites:W2981137429", "limit": 3}
    )
    sent = route.calls.last.request.url.params
    assert sent["group_by"] == "publication_year" and sent["filter"] == "cites:W2981137429"
    assert result.total == 4687
    groups = result.data["groups"]
    assert [g["key"] for g in groups] == ["2024", "2025", "2026"]  # most recent years, chronological
    assert groups[1]["count"] == 776
    assert "showing 3 of" in result.notes[0]


@respx.mock
async def test_works_group_by_institution_shortens_ids(make_registry):
    route = respx.get(f"{API}/works").mock(return_value=httpx.Response(200, json=fx("group_by_institution.json")))
    registry = make_registry(SRC)
    result = await registry.call(
        SRC, "works_group_by", {"group_by": "authorships.institutions.id", "query": "prime editing", "limit": 5}
    )
    sent = route.calls.last.request.url.params
    # query defaults to a title+abstract filter search, consistent with search_works
    assert sent["filter"] == "title_and_abstract.search:prime editing" and "search" not in sent
    first = result.data["groups"][0]
    assert first["key"].startswith("I") and first["name"] and first["count"] > 0


@respx.mock
async def test_search_authors(make_registry):
    route = respx.get(f"{API}/authors").mock(return_value=httpx.Response(200, json=fx("authors.json")))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "search_authors", {"query": "David R. Liu", "limit": 2})
    sent = route.calls.last.request.url.params
    assert sent["search"] == "David R. Liu" and sent["per-page"] == "2"
    assert result.total == 28 and result.next_cursor
    rec = result.records[0]
    assert rec.type == "author" and rec.id == "A5065535610"
    assert rec.identifiers == {"openalex": "A5065535610", "orcid": "0000-0002-9943-7557"}
    assert rec.attributes["h_index"] > 100
    assert "05a0ya142" in rec.attributes["last_known_institution_rors"]


@respx.mock
async def test_get_author_by_orcid_and_not_found(make_registry):
    route = respx.get(f"{API}/authors/orcid:0000-0002-9943-7557").mock(
        return_value=httpx.Response(200, json=fx("author.json"))
    )
    respx.get(f"{API}/authors/A1").mock(return_value=httpx.Response(404, text="not found"))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "get_author", {"id": "https://orcid.org/0000-0002-9943-7557"})
    assert route.called
    assert result.records[0].title == "David R. Liu"
    missing = await registry.call(SRC, "get_author", {"id": "A1"})
    assert missing.records == [] and "not found" in missing.notes[0]


@respx.mock
async def test_search_institutions_name_and_ror(make_registry):
    search = respx.get(f"{API}/institutions").mock(return_value=httpx.Response(200, json=fx("institutions.json")))
    ror = respx.get(f"{API}/institutions/ror:01yc7t268").mock(
        return_value=httpx.Response(200, json=fx("institution.json"))
    )
    registry = make_registry(SRC)
    result = await registry.call(SRC, "search_institutions", {"query": "Washington University in St. Louis"})
    assert search.calls.last.request.url.params["search"] == "Washington University in St. Louis"
    rec = result.records[0]
    assert rec.type == "institution"
    assert rec.identifiers == {"openalex": "I204465549", "ror": "01yc7t268"}
    assert rec.attributes["country_code"] == "US"
    exact = await registry.call(SRC, "search_institutions", {"query": "https://ror.org/01yc7t268"})
    assert ror.called and exact.records[0].id == "I204465549"


# ---------------- live ----------------


@pytest.mark.live
async def test_live_all_tools(live_registry):
    registry = live_registry(SRC)
    search = await registry.call(SRC, "search_works", {"query": "prime editing", "limit": 3})
    assert search.records and search.total and search.next_cursor
    work = await registry.call(SRC, "get_work", {"id": "pmid:31634902"})
    assert work.records[0].identifiers["openalex"] == "W2981137429"
    by_pmc = await registry.call(SRC, "get_work", {"id": "PMC6907074"})
    assert by_pmc.records[0].identifiers["doi"] == "10.1038/s41586-019-1711-4"
    groups = await registry.call(
        SRC, "works_group_by", {"group_by": "publication_year", "filter": "cites:W2981137429", "limit": 5}
    )
    assert groups.data["groups"] and groups.total
    authors = await registry.call(SRC, "search_authors", {"query": "David R. Liu", "limit": 2})
    assert authors.records
    author = await registry.call(SRC, "get_author", {"id": "0000-0002-9943-7557"})
    assert author.records[0].id.startswith("A")
    inst = await registry.call(SRC, "search_institutions", {"query": "01yc7t268"})
    assert inst.records[0].identifiers["ror"] == "01yc7t268"


@respx.mock
async def test_wildcards_stripped_and_fulltext_group_by(make_registry):
    """'?' and '*' make OpenAlex answer HTTP 400; they must be removed from queries."""
    works = respx.get(f"{API}/works").mock(return_value=httpx.Response(200, json=fx("search_works.json")))
    registry = make_registry(SRC)
    await registry.call(SRC, "search_works", {"query": "p53 & MDM2 (inhibitor)? *", "limit": 1})
    sent = works.calls.last.request.url.params
    assert sent["filter"] == "title_and_abstract.search:p53 & MDM2 (inhibitor)"
    await registry.call(SRC, "search_works", {"query": "p53?", "limit": 1, "search_in": "fulltext"})
    assert works.calls.last.request.url.params["search"] == "p53"
    works.mock(return_value=httpx.Response(200, json=fx("group_by_institution.json")))
    await registry.call(
        SRC, "works_group_by", {"group_by": "type", "query": "base editing", "search_in": "fulltext"}
    )
    sent = works.calls.last.request.url.params
    assert sent["search"] == "base editing" and "filter" not in sent


def test_unknown_work_type_is_other():
    from mosaic.connectors.openalex import work_record

    assert work_record({"id": "https://openalex.org/W1", "type": "peer-review"}).type == "other"
    assert work_record({"id": "https://openalex.org/W1", "type": "review"}).type == "article"
