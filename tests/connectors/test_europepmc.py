"""Europe PMC connector tests (offline respx mocks + live)."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.connectors.europepmc import EuropePMC, parse_identifier, tag_identifiers
from tests.conftest import load_fixture

API = "https://www.ebi.ac.uk/europepmc/webservices/rest"
ANN = "https://www.ebi.ac.uk/europepmc/annotations_api/annotationsByArticleIds"


@respx.mock
async def test_search_normalizes_records_and_cursor(make_registry):
    route = respx.get(f"{API}/search").mock(
        return_value=httpx.Response(200, json=load_fixture("europepmc", "article_core.json"))
    )
    registry = make_registry("europepmc")
    result = await registry.call(
        "europepmc", "search", {"query": 'TITLE:"glioblastoma"', "limit": 1, "open_access_only": True, "sort": "cited"}
    )
    sent = route.calls.last.request.url.params
    assert sent["query"] == '(TITLE:"glioblastoma") AND OPEN_ACCESS:y'
    assert sent["resultType"] == "core" and sent["format"] == "json"
    assert sent["pageSize"] == "1" and sent["cursorMark"] == "*" and sent["sort"] == "CITED desc"
    assert result.total == 1
    rec = result.records[0]
    assert rec.id == "30742122" and rec.type == "article"
    assert rec.identifiers == {"pmid": "30742122", "pmcid": "PMC6408961", "doi": "10.1038/s41591-018-0337-7",
                               "issn": "1078-8956"}
    assert rec.url == "https://europepmc.org/article/MED/30742122"
    assert rec.date == "2019-02-11"
    assert rec.authors[0] == "Timothy F Cloughesy"
    assert rec.summary.startswith("Glioblastoma is the most common")
    attrs = rec.attributes
    assert attrs["journal"] == "Nature medicine" and attrs["cited_by"] > 1000
    assert attrs["is_open_access"] is True and attrs["has_full_text"] is True
    assert {"agency": "NCI NIH HHS", "id": "P50 CA211015", "acronym": "CA"} in attrs["grants"]
    assert {"name": "pembrolizumab", "registry": "DPT0O3T46P"} in attrs["chemicals"]
    assert "Randomized Controlled Trial" in attrs["publication_types"]
    assert "geo" in attrs["text_mined_accession_types"]
    assert result.next_cursor is None  # single hit: no further page


@respx.mock
async def test_search_paging_and_preprints(make_registry):
    payload = load_fixture("europepmc", "search_preprint.json")
    route = respx.get(f"{API}/search").mock(return_value=httpx.Response(200, json=payload))
    registry = make_registry("europepmc")
    result = await registry.call("europepmc", "search", {"query": "glioblastoma", "limit": 1, "source": "PPR", "cursor": "abc"})
    sent = route.calls.last.request.url.params
    assert sent["query"] == "(glioblastoma) AND SRC:PPR" and sent["cursorMark"] == "abc"
    assert result.next_cursor == payload["nextCursorMark"]
    rec = result.records[0]
    assert rec.type == "preprint" and rec.id.startswith("PPR")
    assert rec.identifiers["doi"] == "10.21203/rs.3.rs-11028208/v1"
    assert rec.attributes["preprint_server"] == "Research Square"


@respx.mock
async def test_search_pmc_only_records(make_registry):
    respx.get(f"{API}/search").mock(return_value=httpx.Response(200, json=load_fixture("europepmc", "search_core.json")))
    registry = make_registry("europepmc")
    result = await registry.call("europepmc", "search", {"query": "glioblastoma AND pembrolizumab", "limit": 2})
    assert result.total == 1073 and len(result.records) == 2 and result.next_cursor
    assert result.records[0].identifiers["pmcid"] == "PMC6847508"
    assert result.records[0].url == "https://europepmc.org/article/PMC/PMC6847508"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("30742122", ("pmid", "30742122")),
        ("PMID: 30742122", ("pmid", "30742122")),
        ("MED:30742122", ("pmid", "30742122")),
        ("pmc6408961", ("pmcid", "PMC6408961")),
        ("PMC:PMC6408961", ("pmcid", "PMC6408961")),
        ("https://doi.org/10.1038/S41591-018-0337-7", ("doi", "10.1038/s41591-018-0337-7")),
        ("PPR1344431", ("src", "PPR:PPR1344431")),
        ("AGR:AGR123", ("src", "AGR:AGR123")),
    ],
)
def test_parse_identifier(value, expected):
    assert parse_identifier(value) == expected


@respx.mock
async def test_get_article_by_doi_and_not_found(make_registry):
    route = respx.get(f"{API}/search").mock(
        side_effect=[
            httpx.Response(200, json=load_fixture("europepmc", "article_core.json")),
            httpx.Response(200, json={"hitCount": 0, "resultList": {"result": []}}),
        ]
    )
    registry = make_registry("europepmc")
    found = await registry.call("europepmc", "get_article", {"identifier": "doi:10.1038/S41591-018-0337-7"})
    assert route.calls[0].request.url.params["query"] == 'DOI:"10.1038/s41591-018-0337-7"'
    assert found.records[0].identifiers["pmid"] == "30742122"
    missing = await registry.call("europepmc", "get_article", {"identifier": "PMC0000001"})
    assert route.calls[1].request.url.params["query"] == "PMCID:PMC0000001"
    assert missing.records == [] and "not found" in missing.notes[0]


@respx.mock
async def test_citations_by_pmid_paging(make_registry):
    route = respx.get(f"{API}/MED/30742122/citations").mock(
        return_value=httpx.Response(200, json=load_fixture("europepmc", "citations.json"))
    )
    registry = make_registry("europepmc")
    result = await registry.call("europepmc", "get_citations", {"identifier": "30742122", "limit": 2})
    sent = route.calls.last.request.url.params
    assert sent["page"] == "1" and sent["pageSize"] == "2" and sent["format"] == "json"
    assert result.total == 1152 and result.next_cursor == "2"
    rec = result.records[0]
    assert rec.id == "42843924" and rec.identifiers == {"pmid": "42843924"}
    assert rec.attributes["journal"] == "J Immunother Cancer" and rec.date == "2026"
    assert rec.authors[0] == "Zhang Y"


@respx.mock
async def test_references_resolve_pmcid_first(make_registry):
    search = respx.get(f"{API}/search").mock(
        return_value=httpx.Response(200, json=load_fixture("europepmc", "article_core.json"))
    )
    refs = respx.get(f"{API}/MED/30742122/references").mock(
        return_value=httpx.Response(200, json=load_fixture("europepmc", "references.json"))
    )
    registry = make_registry("europepmc")
    result = await registry.call("europepmc", "get_references", {"identifier": "PMC6408961", "limit": 2, "cursor": "3"})
    assert search.calls.last.request.url.params["query"] == "PMCID:PMC6408961"
    assert refs.calls.last.request.url.params["page"] == "3"
    assert result.total == 71 and result.next_cursor == "4"
    assert result.records[0].identifiers["pmid"] == "19706884"
    assert result.records[0].attributes["cited_order"] == 1


@respx.mock
async def test_citations_not_found(make_registry):
    respx.get(f"{API}/search").mock(return_value=httpx.Response(200, json={"hitCount": 0, "resultList": {"result": []}}))
    registry = make_registry("europepmc")
    result = await registry.call("europepmc", "get_citations", {"identifier": "10.9999/none"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_full_text_sections_and_budget(make_registry):
    route = respx.get(f"{API}/PMC6408961/fullTextXML").mock(
        return_value=httpx.Response(200, content=load_fixture("europepmc", "fulltext.xml").encode("utf-8"))
    )
    registry = make_registry("europepmc")
    result = await registry.call("europepmc", "full_text", {"identifier": "PMC6408961"})
    assert route.called
    rec = result.records[0]
    assert rec.identifiers == {"pmcid": "PMC6408961", "pmid": "30742122", "doi": "10.1038/s41591-018-0337-7"}
    assert rec.title.startswith("Neoadjuvant anti-PD-1") and rec.date == "2019-02-11"
    assert rec.summary.startswith("Glioblastoma is the most common")
    names = [s["section"] for s in result.data["sections"]]
    assert names == ["abstract", "introduction", "results", "discussion", "methods", "other"]
    assert not any(s["truncated"] for s in result.data["sections"])
    assert "[Fig. 1] CONSORT diagram Flow diagram" in result.data["sections"][-1]["text"]

    small = await registry.call("europepmc", "full_text", {"identifier": "PMC6408961", "sections": ["methods", "results"],
                                                           "max_chars": 1000})
    assert [s["section"] for s in small.data["sections"]] == ["results", "methods"]
    assert all(s["truncated"] and len(s["text"]) <= 520 for s in small.data["sections"])
    assert small.notes and "truncated" in small.notes[0]


@respx.mock
async def test_full_text_unavailable_is_a_note(make_registry):
    respx.get(f"{API}/search").mock(return_value=httpx.Response(200, json=load_fixture("europepmc", "article_core.json")))
    respx.get(f"{API}/PMC6408961/fullTextXML").mock(return_value=httpx.Response(404, text="not found"))
    registry = make_registry("europepmc")
    result = await registry.call("europepmc", "full_text", {"identifier": "30742122"})
    assert result.records[0].id == "30742122"
    assert "not available" in result.notes[0]


@respx.mock
async def test_annotations_map_entities_to_identifiers(make_registry):
    route = respx.get(ANN).mock(return_value=httpx.Response(200, json=load_fixture("europepmc", "annotations.json")))
    registry = make_registry("europepmc")
    result = await registry.call("europepmc", "annotations", {"identifier": "30742122"})
    sent = route.calls.last.request.url.params
    assert sent["articleIds"] == "MED:30742122" and sent["format"] == "JSON"
    by_type: dict[str, list] = {}
    for rec in result.records:
        by_type.setdefault(rec.attributes["annotation_type"], []).append(rec)
    accessions = by_type["Accession Numbers"]
    assert result.records[0].attributes["annotation_type"] == "Accession Numbers"  # accessions first
    assert any(r.identifiers == {"nct": "NCT02550249"} and r.type == "trial" for r in accessions)
    assert any(r.identifiers.get("geo") == "GSE121810" and r.type == "dataset" for r in accessions)
    assert by_type["Gene_Proteins"][0].identifiers.get("uniprot") or by_type["Gene_Proteins"][0].identifiers.get("ensembl_gene")
    assert by_type["Diseases"][0].identifiers["umls"].startswith("C")
    assert by_type["Organisms"][0].identifiers["taxonomy"].isdigit()
    assert by_type["Chemicals"][0].identifiers  # ChEBI / ChEMBL
    assert result.data["article"]["pmcid"] == "PMC6408961"
    assert result.data["mentions_by_type"]["Accession Numbers"] == 4
    assert result.data["relationships"] and result.data["relationships"][0]["entities"]


@respx.mock
async def test_annotations_type_filter_and_empty(make_registry):
    respx.get(ANN).mock(
        side_effect=[
            httpx.Response(200, json=load_fixture("europepmc", "annotations.json")),
            httpx.Response(200, json=[]),
        ]
    )
    registry = make_registry("europepmc")
    only = await registry.call("europepmc", "annotations", {"identifier": "PMC6408961", "types": ["Accession Numbers"]})
    assert {r.attributes["annotation_type"] for r in only.records} == {"Accession Numbers"}
    empty = await registry.call("europepmc", "annotations", {"identifier": "1"})
    assert empty.records == [] and "no text-mined annotations" in empty.notes[0]


@pytest.mark.parametrize(
    "uri, expected",
    [
        ("https://www.uniprot.org/uniprotkb/P18621/entry", {"uniprot": "P18621"}),
        ("https://www.ensembl.org/Homo_sapiens/Gene/Summary?db=core;g=ENSG00000120217", {"ensembl_gene": "ENSG00000120217"}),
        ("http://linkedlifedata.com/resource/umls-concept/C1621958", {"umls": "C1621958"}),
        ("http://purl.obolibrary.org/obo/CHEBI_82976", {"chebi": "CHEBI:82976"}),
        ("https://www.ebi.ac.uk/chembl/compound_report_card/CHEMBL3137343", {"chembl": "CHEMBL3137343"}),
        ("https://www.ebi.ac.uk/ols/ontologies/efo/terms?short_form=EFO_0000519", {"efo": "EFO_0000519"}),
        ("http://purl.obolibrary.org/obo/MONDO_0018177", {"mondo": "MONDO:0018177"}),
        ("http://identifiers.org/GO:0045087", {"go": "GO:0045087"}),
        ("http://identifiers.org/taxonomy/9606", {"taxonomy": "9606"}),
        ("http://identifiers.org/clinicaltrials:NCT02550249", {"nct": "NCT02550249"}),
        ("http://identifiers.org/geo:GSE121810", {"geo": "GSE121810"}),
        ("http://identifiers.org/pdb:1TUP", {"pdb": "1TUP"}),
        ("http://identifiers.org/refsnp:rs334", {"rsid": "rs334"}),
        ("http://identifiers.org/doi:10.12688/f1000research.11622.1", {"doi": "10.12688/f1000research.11622.1"}),
        ("http://purl.bioontology.org/ontology/RXNORM/1547545", {"rxcui": "1547545"}),
    ],
)
def test_tag_identifiers(uri, expected):
    assert tag_identifiers(uri) == expected


def test_metadata():
    names = {d.name for d in EuropePMC.tool_defs()}
    assert names == {"search", "get_article", "get_citations", "get_references", "full_text", "annotations"}
    assert EuropePMC.federated_tool().name == "search"
    assert all(len(f"europepmc__{n}") <= 64 for n in names)


# ---------------------------------------------------------------- live


@pytest.mark.live
async def test_live_search_and_article(live_registry):
    registry = live_registry("europepmc")
    result = await registry.call("europepmc", "search", {"query": 'TITLE:"CRISPR" AND OPEN_ACCESS:y', "limit": 3})
    assert result.records and result.total and result.next_cursor
    page2 = await registry.call("europepmc", "search",
                                {"query": 'TITLE:"CRISPR" AND OPEN_ACCESS:y', "limit": 3, "cursor": result.next_cursor})
    assert page2.records and page2.records[0].id != result.records[0].id
    article = await registry.call("europepmc", "get_article", {"identifier": "10.1038/s41591-018-0337-7"})
    assert article.records[0].identifiers["pmid"] == "30742122"


@pytest.mark.live
async def test_live_citations_fulltext_annotations(live_registry):
    registry = live_registry("europepmc")
    cites = await registry.call("europepmc", "get_citations", {"identifier": "30742122", "limit": 3})
    assert cites.records and cites.total > 100
    refs = await registry.call("europepmc", "get_references", {"identifier": "PMC6408961", "limit": 3})
    assert refs.records and refs.total
    text = await registry.call("europepmc", "full_text", {"identifier": "30742122", "sections": ["methods"], "max_chars": 3000})
    assert text.data["sections"] and text.data["sections"][0]["section"] == "methods"
    ann = await registry.call("europepmc", "annotations", {"identifier": "30742122", "types": ["Accession Numbers"]})
    assert any(r.identifiers.get("geo") == "GSE121810" for r in ann.records)
