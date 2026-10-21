"""NCBI Entrez E-utilities connector tests (offline respx mocks + live)."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.connectors.ncbi import NCBI, map_summary
from tests.conftest import load_fixture

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def _esummary_for(docs: dict[str, dict]):
    """respx side effect: answer esummary from the per-db fixture documents."""

    def handler(request: httpx.Request) -> httpx.Response:
        db = request.url.params["db"]
        entry = docs[db]
        return httpx.Response(200, json={"result": {"uids": [entry["uid"]], entry["uid"]: entry["doc"]}})

    return handler


@respx.mock
async def test_pubmed_search_returns_full_records(make_registry):
    search = respx.get(f"{EUTILS}/esearch.fcgi").mock(
        return_value=httpx.Response(200, json=load_fixture("ncbi", "esearch_pubmed.json"))
    )
    fetch = respx.get(f"{EUTILS}/efetch.fcgi").mock(
        return_value=httpx.Response(200, text=load_fixture("ncbi", "efetch_pubmed.xml"))
    )
    registry = make_registry("ncbi")

    result = await registry.call(
        "ncbi", "pubmed_search",
        {"query": "glioblastoma[mh] AND pembrolizumab[tiab]", "limit": 2, "mindate": "2019", "maxdate": "2019"},
    )

    sent = search.calls.last.request.url.params
    assert sent["db"] == "pubmed"
    assert sent["term"] == "glioblastoma[mh] AND pembrolizumab[tiab]"
    assert sent["retmax"] == "2" and sent["retstart"] == "0"
    assert sent["mindate"] == "2019" and sent["maxdate"] == "2019" and sent["datetype"] == "pdat"
    assert sent["tool"] == "mosaic" and sent["api_key"] == "test-key" and sent["email"] == "tests@mosaic.invalid"
    assert "sort" not in sent
    assert fetch.calls.last.request.url.params["id"] == "30777100,30742122"
    assert result.total == 3
    assert result.next_cursor == "2"
    assert [r.id for r in result.records] == ["30777100", "30742122"]  # esearch order preserved
    rec = result.records[1]
    assert rec.identifiers == {"pmid": "30742122", "doi": "10.1038/s41591-018-0337-7", "pmcid": "PMC6408961"}
    assert rec.title.startswith("Neoadjuvant anti-PD-1 immunotherapy")
    assert rec.date == "2019-02-11"
    assert rec.authors[0] == "Timothy F Cloughesy"
    assert rec.summary and rec.summary.startswith("Glioblastoma is the most common")
    assert rec.url == "https://pubmed.ncbi.nlm.nih.gov/30742122/"
    assert rec.attributes["journal"] == "Nature medicine"
    assert "Glioblastoma*" in rec.attributes["mesh_terms"] and "D005909" in rec.attributes["mesh_ids"]
    assert {"id": "P50 CA211015", "agency": "NCI NIH HHS", "country": "United States"} in rec.attributes["grants"]
    assert "Randomized Controlled Trial" in rec.attributes["publication_types"]
    assert any(c.get("registry") == "DPT0O3T46P" for c in rec.attributes["chemicals"])
    assert "test-key" not in result.request_url and "api_key=***" in result.request_url
    assert "tests@mosaic.invalid" not in result.request_url
    assert "MeSH Terms" in result.data["query_translation"]


@respx.mock
async def test_pubmed_search_sort_and_paging_limit(make_registry):
    search = respx.get(f"{EUTILS}/esearch.fcgi").mock(
        return_value=httpx.Response(200, json={"esearchresult": {"count": "0", "idlist": [],
                                                                 "errorlist": {"phrasesnotfound": ["zzqx"]}}})
    )
    registry = make_registry("ncbi")
    result = await registry.call("ncbi", "pubmed_search", {"query": "zzqx", "sort": "pub_date", "cursor": "20"})
    params = search.calls.last.request.url.params
    assert params["sort"] == "pub_date" and params["retstart"] == "20"
    assert result.records == [] and result.next_cursor is None
    assert "phrases not found: zzqx" in result.notes

    beyond = await registry.call("ncbi", "pubmed_search", {"query": "cancer", "cursor": "10000"})
    assert beyond.records == [] and "10,000" in beyond.notes[0]


@respx.mock
async def test_pubmed_fetch_normalizes_ids_and_reports_missing(make_registry):
    fetch = respx.get(f"{EUTILS}/efetch.fcgi").mock(
        return_value=httpx.Response(200, text=load_fixture("ncbi", "efetch_pubmed.xml"))
    )
    registry = make_registry("ncbi")
    result = await registry.call("ncbi", "pubmed_fetch", {"pmids": ["PMID:30742122", "30777100", "99999999999"]})
    params = fetch.calls.last.request.url.params
    assert params["id"] == "30742122,30777100,99999999999" and params["retmode"] == "xml"
    assert [r.id for r in result.records] == ["30742122", "30777100"]
    assert result.records[1].identifiers["pmcid"] == "PMC6380009"
    assert result.records[1].attributes["publication_types"] == ["Journal Article", "Review"]
    assert "99999999999" in result.notes[0]


@respx.mock
async def test_pubmed_links_similar_with_scores_and_cursor(make_registry):
    links = load_fixture("ncbi", "elink_similar.json")
    links["linksets"][0]["linksetdbs"][0]["links"] = [
        {"id": "30742122", "score": 99}, {"id": "30777100", "score": 50}, {"id": "30742120", "score": 40}
    ]
    link_route = respx.get(f"{EUTILS}/elink.fcgi").mock(return_value=httpx.Response(200, json=links))
    fetch = respx.get(f"{EUTILS}/efetch.fcgi").mock(
        return_value=httpx.Response(200, text=load_fixture("ncbi", "efetch_pubmed.xml"))
    )
    registry = make_registry("ncbi")
    result = await registry.call("ncbi", "pubmed_links", {"pmid": "30742122", "relation": "similar", "limit": 1})
    params = link_route.calls.last.request.url.params
    assert params["linkname"] == "pubmed_pubmed" and params["cmd"] == "neighbor_score" and params["id"] == "30742122"
    assert fetch.calls.last.request.url.params["id"] == "30777100"  # the input article itself is dropped
    assert result.total == 2 and result.next_cursor == "1"
    assert result.records[0].id == "30777100" and result.records[0].attributes["similarity_score"] == 50


@respx.mock
async def test_pubmed_links_cited_by_none(make_registry):
    route = respx.get(f"{EUTILS}/elink.fcgi").mock(
        return_value=httpx.Response(200, json={"linksets": [{"dbfrom": "pubmed", "ids": ["1"]}]})
    )
    registry = make_registry("ncbi")
    result = await registry.call("ncbi", "pubmed_links", {"pmid": "1", "relation": "cited_by"})
    assert route.calls.last.request.url.params["linkname"] == "pubmed_pubmed_citedin"
    assert result.records == [] and result.total == 0 and "no 'cited_by' links" in result.notes[0]


@respx.mock
async def test_entrez_search_gds(make_registry):
    search = respx.get(f"{EUTILS}/esearch.fcgi").mock(
        return_value=httpx.Response(200, json=load_fixture("ncbi", "esearch_gds.json"))
    )
    summary = respx.get(f"{EUTILS}/esummary.fcgi").mock(
        return_value=httpx.Response(200, json=load_fixture("ncbi", "esummary_gds.json"))
    )
    registry = make_registry("ncbi")
    result = await registry.call("ncbi", "entrez_search", {"db": "gds", "query": "glioblastoma AND gse[etyp]", "limit": 2})
    assert search.calls.last.request.url.params["db"] == "gds"
    assert summary.calls.last.request.url.params["id"] == "200121810,200045209"
    assert result.total == 2759 and result.next_cursor == "2"
    gse = result.records[0]
    assert gse.id == "GSE121810" and gse.type == "dataset"
    assert gse.identifiers == {"geo": "GSE121810", "sra": "SRP166889"}  # the paper is a related entity
    assert gse.attributes["pubmed_ids"] == ["30742122"]
    assert gse.url == "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE121810"
    assert gse.date == "2018-12-05"
    assert gse.attributes["n_samples"] == 29 and gse.attributes["entrez_db"] == "gds"


EXPECTED = {
    "gene": ("7157", "gene", {"entrez_gene": "7157", "gene_symbol": "TP53", "omim": "191170"}),
    "clinvar": ("12375", "variant", {"clinvar": "12375", "rsid": "rs121912662", "gene_symbol": None, "entrez_gene": None}),
    "snp": ("rs334", "variant", {"rsid": "rs334", "gene_symbol": None, "entrez_gene": None}),
    "sra": ("ERX2477416", "dataset", {"sra": "ERX2477416", "bioproject": None, "biosample": None}),
    "gap": ("phs000007.v30.p11", "study", {"dbgap": "phs000007"}),
    "bioproject": ("PRJNA257197", "study", {"bioproject": "PRJNA257197", "taxonomy": "186538"}),
    "biosample": ("SAMN02604305", "sample", {"biosample": "SAMN02604305", "bioproject": None}),
    "medgen": ("C0010674", "disease", {"umls": "C0010674", "mesh": "D003550", "omim": "219700", "mondo": "MONDO:0009061",
                                        "orphanet": "586"}),
    "taxonomy": ("9606", "other", {"taxonomy": "9606"}),
    "nuccore": ("NM_000546.6", "other", {"refseq": "NM_000546"}),
    "protein": ("NP_000537.3", "protein", {"refseq": "NP_000537"}),
    "pmc": ("PMC6408961", "article", {"pmcid": "PMC6408961", "pmid": "30742122", "doi": "10.1038/s41591-018-0337-7"}),
    "pubmed": ("30742122", "article", {"pmid": "30742122", "pmcid": "PMC6408961"}),
    "dbvar": ("nsv7909547", "variant", {"dbvar": "nsv7909547"}),
    "gtr": ("GTR000021517", "assay", {"gtr": "GTR000021517", "gene_symbol": None, "entrez_gene": None}),
    "mesh": ("D001249", "ontology_term", {"mesh": "D001249"}),
    "cdd": ("cl49619", "other", {"cdd": "cl49619"}),
    "assembly": ("GCF_000001405.40", "dataset", {"assembly": "GCF_000001405.40", "taxonomy": "9606"}),
    "structure": ("1TUP", "structure", {"pdb": "1TUP"}),
    "books": ("NBK625262", "book", {"ncbi_bookshelf": "NBK625262"}),
}


@pytest.mark.parametrize("db", sorted(EXPECTED))
def test_summary_mappers(db):
    entry = load_fixture("ncbi", "esummary_docs.json")[db]
    record = map_summary(db, entry["uid"], entry["doc"])
    rid, rtype, ids = EXPECTED[db]
    assert record.id == rid
    assert record.type == rtype
    for key, value in ids.items():
        assert record.identifiers.get(key) == value, (db, key, record.identifiers)
    assert record.title and record.url and record.url.startswith("https://")
    assert record.attributes["entrez_db"] == db
    # related entities (variant's / test's gene, a sample's parent project) are attributes only
    related = {"clinvar": ("entrez_genes", ["7157"]), "snp": ("entrez_genes", ["3043"]), "gtr": ("entrez_genes", ["672"]),
               "sra": ("bioproject", "PRJEB25794"), "biosample": ("bioproject", "PRJNA40")}
    if db in related:
        key, value = related[db]
        assert record.attributes[key] == value


def test_summary_generic_fallback():
    record = map_summary("unknowndb", "42", {"uid": "42", "title": "<b>Thing</b>", "count": 3})
    assert record.id == "42" and record.title == "Thing" and record.attributes["count"] == 3


@respx.mock
async def test_entrez_summary_resolves_accessions(make_registry):
    docs = load_fixture("ncbi", "esummary_docs.json")
    summary = respx.get(f"{EUTILS}/esummary.fcgi").mock(side_effect=_esummary_for(docs))
    search = respx.get(f"{EUTILS}/esearch.fcgi").mock(
        return_value=httpx.Response(200, json={"esearchresult": {"count": "1", "idlist": ["2604305"]}})
    )
    registry = make_registry("ncbi")

    snp = await registry.call("ncbi", "entrez_summary", {"db": "snp", "ids": ["rs334"]})
    assert summary.calls.last.request.url.params["id"] == "334"
    assert snp.records[0].identifiers["rsid"] == "rs334"

    clinvar = await registry.call("ncbi", "entrez_summary", {"db": "clinvar", "ids": ["VCV000012375.13"]})
    assert summary.calls.last.request.url.params["id"] == "12375"
    assert clinvar.records[0].attributes["classification"] == "Likely pathogenic"

    await registry.call("ncbi", "entrez_summary", {"db": "pmc", "ids": ["PMC6408961"]})
    assert summary.calls.last.request.url.params["id"] == "6408961"
    assert not search.called  # computable accessions need no esearch

    sample = await registry.call("ncbi", "entrez_summary", {"db": "biosample", "ids": ["SAMN02604305"]})
    assert search.calls.last.request.url.params["term"] == "SAMN02604305[ACCN]"
    assert sample.records[0].id == "SAMN02604305"


@respx.mock
async def test_entrez_summary_geo_uid_convention(make_registry):
    summary = respx.get(f"{EUTILS}/esummary.fcgi").mock(
        return_value=httpx.Response(200, json=load_fixture("ncbi", "esummary_gds.json"))
    )
    registry = make_registry("ncbi")
    result = await registry.call("ncbi", "entrez_summary", {"db": "gds", "ids": ["GSE121810", "GSE45209"]})
    assert summary.calls.last.request.url.params["id"] == "200121810,200045209"
    assert {r.id for r in result.records} == {"GSE121810", "GSE45209"}


@respx.mock
async def test_entrez_summary_not_found(make_registry):
    respx.get(f"{EUTILS}/esummary.fcgi").mock(
        return_value=httpx.Response(200, json=load_fixture("ncbi", "esummary_notfound.json"))
    )
    registry = make_registry("ncbi")
    result = await registry.call("ncbi", "entrez_summary", {"db": "gene", "ids": ["999999999"]})
    assert result.records == [] and "not found in gene: 999999999" in result.notes[0]


@respx.mock
async def test_entrez_link_pubmed_to_gds(make_registry):
    link = respx.get(f"{EUTILS}/elink.fcgi").mock(
        return_value=httpx.Response(200, json=load_fixture("ncbi", "elink_pubmed_gds.json"))
    )
    summary = respx.get(f"{EUTILS}/esummary.fcgi").mock(
        return_value=httpx.Response(200, json=load_fixture("ncbi", "esummary_gds.json"))
    )
    registry = make_registry("ncbi")
    result = await registry.call("ncbi", "entrez_link", {"dbfrom": "pubmed", "db": "gds", "ids": ["30742122"]})
    params = link.calls.last.request.url.params
    assert params["dbfrom"] == "pubmed" and params["db"] == "gds" and params.get_list("id") == ["30742122"]
    assert params["api_key"] == "test-key"
    assert summary.calls.last.request.url.params["id"] == "200121810"
    assert result.records[0].identifiers["geo"] == "GSE121810"
    assert result.data["linkname"] == "pubmed_gds"
    assert result.data["by_input"] == {"30742122": ["200121810"]}
    assert "test-key" not in result.request_url


@respx.mock
async def test_entrez_link_linkname_choice_and_paging(make_registry):
    docs = load_fixture("ncbi", "esummary_docs.json")
    respx.get(f"{EUTILS}/elink.fcgi").mock(
        return_value=httpx.Response(200, json=load_fixture("ncbi", "elink_gene_clinvar.json"))
    )
    summary = respx.get(f"{EUTILS}/esummary.fcgi").mock(side_effect=_esummary_for(docs))
    registry = make_registry("ncbi")

    default = await registry.call("ncbi", "entrez_link", {"dbfrom": "gene", "db": "clinvar", "ids": ["7157"], "limit": 2})
    assert default.data["linkname"] == "gene_clinvar"
    assert default.data["linknames"] == {"gene_clinvar": 5, "gene_clinvar_specific": 5}
    assert default.total == 5 and default.next_cursor == "2"
    assert summary.calls.last.request.url.params["id"] == "4982772,4933289"

    specific = await registry.call(
        "ncbi", "entrez_link",
        {"dbfrom": "gene", "db": "clinvar", "ids": ["7157"], "linkname": "gene_clinvar_specific", "limit": 2, "cursor": "2"},
    )
    assert specific.data["linkname"] == "gene_clinvar_specific"
    assert summary.calls.last.request.url.params["id"] == "4933287,4933286"


def test_rate_depends_on_key(offline_settings):
    assert NCBI(offline_settings, _NullHttp()).effective_rate() == 9.0
    keyless = offline_settings.model_copy(update={"ncbi_api_key": None})
    assert NCBI(keyless, _NullHttp()).effective_rate() <= 3.0


def test_tool_names_and_metadata():
    names = {d.name for d in NCBI.tool_defs()}
    assert names == {"pubmed_search", "pubmed_fetch", "pubmed_links", "entrez_search", "entrez_summary", "entrez_link"}
    assert NCBI.federated_tool().name == "pubmed_search"
    assert all(len(f"ncbi__{n}") <= 64 for n in names)
    assert "Gene Expression Omnibus (GEO)" in NCBI.nih_repositories


class _NullHttp:
    def set_rate(self, *args, **kwargs):
        pass


# ---------------------------------------------------------------- live


@pytest.mark.live
async def test_live_pubmed_tools(live_registry):
    registry = live_registry("ncbi")
    search = await registry.call("ncbi", "pubmed_search", {"query": "pembrolizumab glioblastoma[mh]", "limit": 3})
    assert search.records and search.total and search.records[0].identifiers.get("pmid")
    assert any(r.summary for r in search.records)
    fetched = await registry.call("ncbi", "pubmed_fetch", {"pmids": ["30742122"]})
    assert fetched.records[0].identifiers["doi"] == "10.1038/s41591-018-0337-7"
    similar = await registry.call("ncbi", "pubmed_links", {"pmid": "30742122", "relation": "similar", "limit": 3})
    assert similar.records and similar.total > 3
    cited = await registry.call("ncbi", "pubmed_links", {"pmid": "30742122", "relation": "cited_by", "limit": 2})
    assert cited.records


@pytest.mark.live
async def test_live_entrez_tools(live_registry):
    registry = live_registry("ncbi")
    genes = await registry.call("ncbi", "entrez_search", {"db": "gene", "query": "TP53[sym] AND human[orgn]", "limit": 1})
    assert genes.records[0].identifiers["entrez_gene"] == "7157"
    variants = await registry.call("ncbi", "entrez_summary", {"db": "snp", "ids": ["rs334"]})
    assert variants.records[0].identifiers["rsid"] == "rs334"
    geo = await registry.call("ncbi", "entrez_summary", {"db": "gds", "ids": ["GSE121810"]})
    assert geo.records[0].attributes["pubmed_ids"] == ["30742122"]
    linked = await registry.call("ncbi", "entrez_link", {"dbfrom": "pubmed", "db": "gds", "ids": ["30742122"]})
    assert any(r.identifiers.get("geo") == "GSE121810" for r in linked.records)
