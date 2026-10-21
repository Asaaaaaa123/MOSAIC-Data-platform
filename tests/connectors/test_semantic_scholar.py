"""Semantic Scholar connector tests (respx offline + live)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mosaic.connectors.semantic_scholar import normalize_paper_id
from tests.conftest import load_fixture

G = "https://api.semanticscholar.org/graph/v1"
R = "https://api.semanticscholar.org/recommendations/v1"
SRC = "semantic_scholar"


def fx(name):
    return load_fixture(SRC, name)


def test_normalize_paper_id():
    assert normalize_paper_id("10.1038/S41586-019-1711-4") == "DOI:10.1038/s41586-019-1711-4"
    assert normalize_paper_id("https://doi.org/10.1038/x") == "DOI:10.1038/x"
    assert normalize_paper_id("doi:10.1038/x") == "DOI:10.1038/x"
    assert normalize_paper_id("PMC6907074") == "PMCID:6907074"
    assert normalize_paper_id("pmcid:PMC6907074") == "PMCID:6907074"
    assert normalize_paper_id("pmid:31634902") == "PMID:31634902"
    assert normalize_paper_id("corpusid:204832721") == "CorpusId:204832721"
    assert normalize_paper_id("arxiv:1706.03762") == "ARXIV:1706.03762"
    assert normalize_paper_id("https://arxiv.org/abs/1706.03762") == "URL:https://arxiv.org/abs/1706.03762"
    sha = "3AF5518CD1A523532CB2F2B43142843980AF15A2"
    assert normalize_paper_id(sha) == sha.lower()


@respx.mock
async def test_search_papers_params_and_records(make_registry):
    route = respx.get(f"{G}/paper/search").mock(return_value=httpx.Response(200, json=fx("search_papers.json")))
    registry = make_registry(SRC)
    result = await registry.call(
        SRC,
        "search_papers",
        {
            "query": "prime editing sickle cell",
            "limit": 2,
            "year": "2020-",
            "fields_of_study": "Medicine,Biology",
            "open_access_pdf": True,
            "min_citation_count": 10,
        },
    )
    request = route.calls.last.request
    sent = request.url.params
    assert request.headers["x-api-key"] == "test-key"
    assert sent["query"] == "prime editing sickle cell"
    assert sent["limit"] == "2" and sent["offset"] == "0"
    assert sent["year"] == "2020-"
    assert sent["fieldsOfStudy"] == "Medicine,Biology"
    assert sent["minCitationCount"] == "10"
    assert "openAccessPdf" in sent
    assert "externalIds" in sent["fields"] and "tldr" in sent["fields"]
    assert "test-key" not in result.request_url
    assert result.total == 1763
    assert result.next_cursor == "2"
    rec = result.records[0]
    assert rec.id == "c28355b63cd66f16786f62a8afbe2d41d8b9e902"
    assert rec.identifiers["s2"] == rec.id
    assert rec.identifiers["doi"] == "10.1182/blood.2024028166"
    assert rec.identifiers["pmid"] == "40857666"
    assert rec.identifiers["pmcid"] == "PMC12824697"
    assert rec.title.startswith("A prime editing strategy to rewrite")
    assert rec.date == "2025-08-26"
    assert rec.authors[0] == "A. Chalumeau"
    assert rec.attributes["corpus_id"] == 280926129
    assert rec.attributes["venue"] == "Blood"
    assert rec.type == "article"


@respx.mock
async def test_search_papers_past_1000_is_a_note(make_registry):
    registry = make_registry(SRC)
    result = await registry.call(SRC, "search_papers", {"query": "x", "cursor": "1000"})
    assert result.records == [] and "1,000" in result.notes[0]


@respx.mock
async def test_get_paper_by_pmcid(make_registry):
    route = respx.get(f"{G}/paper/PMCID:6907074").mock(return_value=httpx.Response(200, json=fx("paper.json")))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "get_paper", {"paper_id": "PMC6907074"})
    assert route.called
    rec = result.records[0]
    assert rec.identifiers == {
        "s2": "3af5518cd1a523532cb2f2b43142843980af15a2",
        "doi": "10.1038/s41586-019-1711-4",
        "pmid": "31634902",
        "pmcid": "PMC6907074",
        "issn": "0028-0836",
    }
    assert rec.attributes["pages"] == "149-157"
    assert rec.attributes["mag"] == "2981137429"
    assert rec.attributes["oa_status"] == "GREEN"
    assert rec.attributes["tldr"].startswith("A new DNA-editing technique")
    assert rec.summary.startswith("Most genetic variants")


@respx.mock
async def test_get_paper_not_found(make_registry):
    respx.get(f"{G}/paper/DOI:10.9999/missing").mock(
        return_value=httpx.Response(404, json={"error": "Paper with id DOI:10.9999/missing not found"})
    )
    registry = make_registry(SRC)
    result = await registry.call(SRC, "get_paper", {"paper_id": "https://doi.org/10.9999/MISSING"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_get_papers_batch(make_registry):
    route = respx.post(f"{G}/paper/batch").mock(return_value=httpx.Response(200, json=fx("batch.json")))
    registry = make_registry(SRC)
    result = await registry.call(
        SRC, "get_papers_batch", {"ids": ["10.1038/s41586-021-03534-y", "arxiv:1706.03762", "PMID:00000000"]}
    )
    body = json.loads(route.calls.last.request.content)
    assert body == {"ids": ["DOI:10.1038/s41586-021-03534-y", "ARXIV:1706.03762", "PMID:00000000"]}
    assert len(result.records) == 2
    assert result.records[0].identifiers["pmid"] == "34012082"
    assert result.records[0].attributes["requested_id"] == "DOI:10.1038/s41586-021-03534-y"
    arxiv = result.records[1]
    assert arxiv.identifiers["arxiv"] == "1706.03762"
    assert arxiv.attributes["dblp"] == "journals/corr/VaswaniSPUJGKP17"
    assert "PMID:00000000" in result.notes[0]


@respx.mock
async def test_paper_citations_forward(make_registry):
    route = respx.get(f"{G}/paper/PMID:31634902/citations").mock(
        return_value=httpx.Response(200, json=fx("citations.json"))
    )
    registry = make_registry(SRC)
    result = await registry.call(SRC, "paper_citations", {"paper_id": "pmid:31634902", "limit": 2})
    sent = route.calls.last.request.url.params
    assert sent["limit"] == "2" and sent["offset"] == "0"
    assert sent["fields"].startswith("contexts,intents,isInfluential")
    assert result.next_cursor == "2"
    rec = result.records[0]
    assert rec.id == "890986910ce72c11d1360e292a45ee6105de490b"
    assert rec.identifiers["doi"] == "10.1016/j.apmt.2026.103429"
    assert rec.attributes["relation"] == "cites_seed"
    assert rec.attributes["is_influential"] is True
    assert rec.attributes["intents"] == ["methodology"]
    assert rec.attributes["contexts"][0].startswith("Prime editing (PE)")


@respx.mock
async def test_paper_references_backward(make_registry):
    route = respx.get(f"{G}/paper/PMID:31634902/references").mock(
        return_value=httpx.Response(200, json=fx("references.json"))
    )
    registry = make_registry(SRC)
    result = await registry.call(
        SRC, "paper_citations", {"paper_id": "PMID:31634902", "direction": "references", "limit": 2, "cursor": "0"}
    )
    assert route.called
    rec = result.records[0]
    assert rec.identifiers["pmcid"] == "PMC6728210"
    assert rec.identifiers["doi"] == "10.1038/s41587-019-0193-0"
    assert rec.attributes["relation"] == "cited_by_seed"


@respx.mock
async def test_paper_citations_not_found(make_registry):
    respx.get(f"{G}/paper/PMID:1/citations").mock(return_value=httpx.Response(404, json={"error": "not found"}))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "paper_citations", {"paper_id": "PMID:1"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_search_authors(make_registry):
    route = respx.get(f"{G}/author/search").mock(return_value=httpx.Response(200, json=fx("author_search.json")))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "search_authors", {"query": "Kiran Musunuru", "limit": 2})
    assert route.calls.last.request.url.params["query"] == "Kiran Musunuru"
    assert result.total == 7 and result.next_cursor == "2"
    rec = result.records[0]
    assert rec.type == "author" and rec.id == "117290206"
    assert rec.attributes["h_index"] == 71


@respx.mock
async def test_author_papers(make_registry):
    route = respx.get(f"{G}/author/117290206/papers").mock(
        return_value=httpx.Response(200, json=fx("author_papers.json"))
    )
    registry = make_registry(SRC)
    result = await registry.call(SRC, "author_papers", {"author_id": "117290206", "limit": 2})
    assert route.calls.last.request.url.params["limit"] == "2"
    assert result.records[0].identifiers["pmid"] == "41885675"
    assert result.next_cursor == "2"


@respx.mock
async def test_recommend_single_and_multi(make_registry):
    single = respx.get(f"{R}/papers/forpaper/PMID:31634902").mock(
        return_value=httpx.Response(200, json=fx("recommendations.json"))
    )
    multi = respx.post(f"{R}/papers/").mock(return_value=httpx.Response(200, json=fx("recommendations.json")))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "recommend_papers", {"paper_ids": ["PMID:31634902"], "limit": 2})
    assert single.calls.last.request.url.params["from"] == "recent"
    assert result.records[0].identifiers["doi"] == "10.1038/s41587-026-03296-w"
    await registry.call(
        SRC, "recommend_papers", {"paper_ids": ["PMID:31634902", "10.1038/x"], "negative_paper_ids": ["PMC1"]}
    )
    body = json.loads(multi.calls.last.request.content)
    assert body == {"positivePaperIds": ["PMID:31634902", "DOI:10.1038/x"], "negativePaperIds": ["PMCID:1"]}


@respx.mock
async def test_snippet_search_resolves_ids(make_registry):
    route = respx.get(f"{G}/snippet/search").mock(return_value=httpx.Response(200, json=fx("snippets.json")))
    batch = respx.post(f"{G}/paper/batch").mock(return_value=httpx.Response(200, json=fx("snippet_batch.json")))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "snippet_search", {"query": "PCSK9 base editing in primates", "limit": 2})
    assert route.calls.last.request.url.params["limit"] == "2"
    assert json.loads(batch.calls.last.request.content) == {"ids": ["CorpusId:234793050", "CorpusId:254150221"]}
    rec = result.records[0]
    assert rec.id == "CorpusId:234793050"
    assert rec.identifiers["doi"] == "10.1038/s41587-021-00933-4"
    assert rec.identifiers["pmcid"] == "PMC8352781"
    assert rec.url.endswith("/paper/c443f9b5e4a8940977d4ae02f4c5e9c523615952")
    assert result.records[1].attributes["section"] == "PCSK9 gene therapy"
    assert result.records[1].summary


@respx.mock
async def test_snippet_search_without_resolution(make_registry):
    respx.get(f"{G}/snippet/search").mock(return_value=httpx.Response(200, json=fx("snippets.json")))
    batch = respx.post(f"{G}/paper/batch")
    registry = make_registry(SRC)
    result = await registry.call(SRC, "snippet_search", {"query": "x", "resolve_ids": False})
    assert not batch.called
    assert result.records[0].identifiers == {}
    assert "CorpusId" in result.notes[-1]


# ---------------- live ----------------


@pytest.mark.live
async def test_live_all_tools(live_registry):
    registry = live_registry(SRC)
    search = await registry.call(SRC, "search_papers", {"query": "prime editing sickle cell", "limit": 3})
    assert search.records and search.total
    paper = await registry.call(SRC, "get_paper", {"paper_id": "PMID:31634902"})
    assert paper.records[0].identifiers["doi"] == "10.1038/s41586-019-1711-4"
    batch = await registry.call(SRC, "get_papers_batch", {"ids": ["10.1038/s41586-021-03534-y", "PMC6907074"]})
    assert len(batch.records) == 2
    cites = await registry.call(SRC, "paper_citations", {"paper_id": "PMID:31634902", "limit": 3})
    assert cites.records
    refs = await registry.call(SRC, "paper_citations", {"paper_id": "PMID:31634902", "direction": "references"})
    assert refs.records
    authors = await registry.call(SRC, "search_authors", {"query": "Kiran Musunuru", "limit": 3})
    assert authors.records
    papers = await registry.call(SRC, "author_papers", {"author_id": authors.records[0].id, "limit": 3})
    assert papers.records
    recs = await registry.call(SRC, "recommend_papers", {"paper_ids": ["PMID:31634902"], "limit": 3})
    assert recs.records
    snippets = await registry.call(SRC, "snippet_search", {"query": "PCSK9 base editing LDL cholesterol", "limit": 2})
    assert snippets.records and snippets.records[0].attributes["corpus_id"]


@respx.mock
async def test_author_papers_unknown_author_is_a_note(make_registry):
    # S2 answers 200 with an empty list (not 404) for unknown author ids.
    respx.get(f"{G}/author/999999999999/papers").mock(
        return_value=httpx.Response(200, json={"offset": 0, "data": []})
    )
    registry = make_registry(SRC)
    result = await registry.call(SRC, "author_papers", {"author_id": "999999999999"})
    assert result.records == [] and result.next_cursor is None
    assert any("no papers for author 999999999999" in n for n in result.notes)


def test_paper_record_tolerates_string_publication_venue():
    from mosaic.connectors.semantic_scholar import paper_record

    rec = paper_record({"paperId": "a" * 40, "title": "X", "publicationVenue": "Nature"})
    assert rec.identifiers == {"s2": "a" * 40}
