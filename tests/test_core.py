"""Core platform tests: ids, models, schema cleaning, HTTP layer, registry, linking, catalog."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.audit import AuditLog
from mosaic.cache import ResponseCache
from mosaic.catalog import RepositoryCatalog
from mosaic.connectors.base import SearchParams, clean_schema
from mosaic.crosslink import cluster_records, merge_cluster
from mosaic.http import HttpClient, SourceError, redact_url
from mosaic.ids import normalize_identifiers
from mosaic.models import Record, ToolResult
from mosaic.registry import discover_connector_classes
from tests.conftest import load_fixture


def test_identifier_normalization():
    ids = normalize_identifiers(
        {
            "doi": "https://doi.org/10.1038/ABC.123",
            "pmid": "PMID: 31634902",
            "pmcid": "6907074",
            "nct": "nct01234567",
            "ensembl_gene": "ENSG00000146648.18",
            "hgnc": "3236",
            "inchikey": "bsynrygfzvkccq-uhfffaoysa-n",
            "empty": "",
            "listed": ["", "first", "second"],
        }
    )
    assert ids == {
        "doi": "10.1038/abc.123",
        "pmid": "31634902",
        "pmcid": "PMC6907074",
        "nct": "NCT01234567",
        "ensembl_gene": "ENSG00000146648",
        "hgnc": "HGNC:3236",
        "inchikey": "BSYNRYGFZVKCCQ-UHFFFAOYSA-N",
        "listed": "first",
    }


def test_tool_result_to_llm_trims_to_budget():
    records = [
        Record(source="x", id=str(i), title=f"title {i}", summary="lorem ipsum " * 400, type="article")
        for i in range(60)
    ]
    result = ToolResult(source="x", tool="search", records=records, total=1000)
    text = result.to_llm(max_chars=6000)
    assert len(text) <= 6000
    assert '"total": 1000' in text
    assert "trimmed" in text


def test_clean_schema_collapses_optional_and_titles():
    class P(SearchParams):
        year: int | None = None

    schema = clean_schema(P.model_json_schema())
    assert "title" not in schema
    assert schema["properties"]["year"] == {"type": "integer", "default": None}
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["required"] == ["query"]
    assert schema["additionalProperties"] is False


def test_redact_url_hides_secrets():
    url = redact_url("https://x.org/a?api_key=SECRET&q=1", {"apiKey": "S2", "mailto": "me@x.org", "term": "t"})
    assert "SECRET" not in url and "S2" not in url and "me%40x.org" not in url
    assert "q=1" in url and "term=t" in url


async def test_http_retries_on_429(offline_settings):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"ok": True})

    client = HttpClient(offline_settings, transport=httpx.MockTransport(handler))
    try:
        assert await client.get_json("t", "https://example.org/x") == {"ok": True}
        assert calls["n"] == 2
    finally:
        await client.aclose()


async def test_http_error_raises_source_error(offline_settings):
    client = HttpClient(offline_settings, transport=httpx.MockTransport(lambda r: httpx.Response(403, text="nope")))
    try:
        with pytest.raises(SourceError) as info:
            await client.get_json("t", "https://example.org/x", params={"api_key": "SECRET"})
        assert info.value.status == 403
        assert "SECRET" not in str(info.value)
    finally:
        await client.aclose()


async def test_http_cache_serves_second_call(offline_settings):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"n": calls["n"]})

    client = HttpClient(offline_settings, cache=ResponseCache(None, 60), transport=httpx.MockTransport(handler))
    try:
        first = await client.get_json("t", "https://example.org/x", params={"q": "a"})
        second = await client.get_json("t", "https://example.org/x", params={"q": "a"})
        assert first == second == {"n": 1}
    finally:
        await client.aclose()


def test_discovery_finds_reference_connector():
    classes, errors = discover_connector_classes()
    names = {c.name for c in classes}
    assert "crossref" in names
    assert not errors, errors


def test_every_connector_is_well_formed():
    classes, _ = discover_connector_classes()
    from mosaic.connectors.base import CATEGORIES

    for klass in classes:
        assert klass.category in CATEGORIES, klass
        assert klass.title and klass.description and klass.homepage, klass
        assert klass.tool_defs(), f"{klass.name} has no tools"
        for spec in klass.tool_specs():
            assert len(spec.qualified_name) <= 64, spec.qualified_name
            assert spec.description, spec.qualified_name
        catalog_names = {r["name"] for r in RepositoryCatalog().repositories}
        unknown = set(klass.nih_repositories) - catalog_names
        assert not unknown, f"{klass.name}: nih_repositories not in catalog: {unknown}"
        federated = klass.federated_tool()
        if federated:
            fields = federated.params_model.model_fields
            required = {n for n, f in fields.items() if f.is_required()}
            assert required <= {"query"}, f"{klass.name}.{federated.name} federated tool requires {required}"
            assert "limit" in fields, klass.name


@respx.mock
async def test_registry_call_is_audited(make_registry):
    respx.get("https://api.crossref.org/works").mock(
        return_value=httpx.Response(200, json=load_fixture("crossref", "search_works.json"))
    )
    registry = make_registry("crossref")
    await registry.call("crossref", "search_works", {"query": "q", "limit": 2}, session_id="s1")
    rows = registry.audit.recent_tool_calls(session_id="s1")
    assert rows[0]["ok"] == 1 and rows[0]["n_records"] == 2


@respx.mock
async def test_federated_search_reports_failures(make_registry):
    respx.get("https://api.crossref.org/works").mock(return_value=httpx.Response(500, text="boom"))
    registry = make_registry("crossref")
    outcome = await registry.federated_search("anything", sources=["crossref"])
    assert outcome.sources[0].ok is False
    assert "500" in outcome.sources[0].error


def test_cluster_records_links_transitively():
    a = Record(source="pubmed", id="1", identifiers={"pmid": "1"}, title="A", type="article")
    b = Record(source="openalex", id="W1", identifiers={"pmid": "1", "doi": "10.1/x"}, title="A long", type="article")
    c = Record(source="crossref", id="10.1/x", identifiers={"doi": "10.1/X"}, type="article")
    d = Record(source="crossref", id="other", identifiers={"doi": "10.2/y"}, type="article")
    clusters = cluster_records([a, b, c, d])
    assert len(clusters) == 2
    merged = merge_cluster(clusters[0])
    assert merged["sources"] == ["crossref", "openalex", "pubmed"]
    assert merged["identifiers"] == {"pmid": "1", "doi": "10.1/x"}
    assert merged["title"] == "A long"


def test_catalog_search():
    catalog = RepositoryCatalog({"Gene Expression Omnibus (GEO)": ["ncbi"]})
    stats = catalog.stats()
    assert stats["total"] == 232 and stats["generalist"] == 9
    hits = catalog.search("gene expression microarray")
    assert hits[0]["name"] == "Gene Expression Omnibus (GEO)"
    assert hits[0]["connectors"] == ["ncbi"]
    generalist = catalog.search(None, kind="generalist", limit=50)
    assert {r["name"] for r in generalist} >= {"Zenodo", "Dryad", "Figshare", "Vivli"}


def test_audit_redacts_secret_params():
    audit = AuditLog(None)
    audit.record_tool_call(source="s", tool="t", params={"api_key": "SECRET", "q": "x"}, ok=True, latency_ms=1)
    row = audit.recent_tool_calls()[0]
    assert "SECRET" not in row["params"]
