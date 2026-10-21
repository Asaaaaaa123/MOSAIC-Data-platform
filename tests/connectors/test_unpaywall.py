"""Unpaywall connector tests (respx offline + live)."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.http import NotConfiguredError
from tests.conftest import load_fixture

API = "https://api.unpaywall.org/v2"
SRC = "unpaywall"


def fx(name):
    return load_fixture(SRC, name)


def test_no_federated_search():
    from mosaic.connectors.unpaywall import Unpaywall

    assert Unpaywall.federated_tool() is None  # /v2/search was retired (HTTP 410)
    assert Unpaywall.required_settings == ("biopha_contact_email",)


@respx.mock
async def test_get_oa_status_green_pmc(make_registry):
    route = respx.get(f"{API}/10.1056/nejmoa2034577").mock(return_value=httpx.Response(200, json=fx("nejm.json")))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "get_oa_status", {"doi": "https://doi.org/10.1056/NEJMoa2034577"})
    assert route.calls.last.request.url.params["email"] == "tests@mosaic.invalid"
    assert "tests@mosaic.invalid" not in result.request_url and "email=***" in result.request_url
    rec = result.records[0]
    assert rec.id == "10.1056/nejmoa2034577"
    assert rec.identifiers == {"doi": "10.1056/nejmoa2034577", "issn": "0028-4793", "pmcid": "PMC7745181"}
    assert rec.title == "Safety and Efficacy of the BNT162b2 mRNA Covid-19 Vaccine"
    assert rec.date == "2020-12-10"
    assert rec.authors[0] == "Fernando P. Polack"
    attrs = rec.attributes
    assert attrs["is_oa"] is True and attrs["oa_status"] == "green"
    assert attrs["best_oa_host_type"] == "repository"
    assert attrs["best_oa_url"].endswith("/pmc/articles/7745181")
    assert attrs["journal"] == "New England Journal of Medicine"
    assert attrs["journal_is_oa"] is False
    assert attrs["oa_locations"][0]["is_best"] is True


@respx.mock
async def test_get_oa_status_gold_license(make_registry):
    respx.get(f"{API}/10.1371/journal.pone.0000001").mock(return_value=httpx.Response(200, json=fx("gold.json")))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "get_oa_status", {"doi": "doi:10.1371/journal.pone.0000001"})
    attrs = result.records[0].attributes
    assert attrs["oa_status"] == "gold" and attrs["best_oa_license"] == "cc-by"
    assert attrs["journal_is_oa"] is True


@respx.mock
async def test_get_oa_status_not_found(make_registry):
    respx.get(f"{API}/10.9999/missing").mock(return_value=httpx.Response(404, text="<html>404 Not Found</html>"))
    registry = make_registry(SRC)
    result = await registry.call(SRC, "get_oa_status", {"doi": "10.9999/MISSING"})
    assert result.records == [] and "not found" in result.notes[0]


@respx.mock
async def test_get_oa_status_batch(make_registry):
    respx.get(f"{API}/10.1056/nejmoa2034577").mock(return_value=httpx.Response(200, json=fx("nejm.json")))
    respx.get(f"{API}/10.1038/s41586-021-03534-y").mock(return_value=httpx.Response(200, json=fx("closed.json")))
    respx.get(f"{API}/10.9999/missing").mock(return_value=httpx.Response(404, text="not found"))
    registry = make_registry(SRC)
    result = await registry.call(
        SRC,
        "get_oa_status_batch",
        {"dois": ["10.1056/NEJMoa2034577", "https://doi.org/10.1038/s41586-021-03534-y", "10.9999/missing",
                  "10.1056/nejmoa2034577"]},
    )
    assert [r.id for r in result.records] == ["10.1056/nejmoa2034577", "10.1038/s41586-021-03534-y"]
    assert result.data == {"requested": 3, "found": 2, "open_access": 1}
    assert result.records[1].attributes["oa_status"] == "closed"
    assert "10.9999/missing" in result.notes[0]


async def test_missing_email_is_not_configured(make_registry, monkeypatch):
    registry = make_registry(SRC)
    connector = registry.get(SRC)
    # Settings fall back to the real .env file, so simulate the email being unset.
    monkeypatch.setattr(connector, "setting", lambda name: None)
    assert connector.missing_settings() == ["BIOPHA_CONTACT_EMAIL"]
    with pytest.raises(NotConfiguredError):
        await registry.call(SRC, "get_oa_status", {"doi": "10.1/x"})
    health = await connector.healthcheck()
    assert health.ok is False and "BIOPHA_CONTACT_EMAIL" in health.detail


# ---------------- live ----------------


@pytest.mark.live
async def test_live_all_tools(live_registry, live_settings):
    if not live_settings.biopha_contact_email:
        pytest.skip("BIOPHA_CONTACT_EMAIL is not set: Unpaywall requires a contact email")
    registry = live_registry(SRC)
    one = await registry.call(SRC, "get_oa_status", {"doi": "10.1038/s41586-019-1711-4"})
    assert one.records and one.records[0].attributes["oa_status"]
    assert one.records[0].identifiers["pmcid"] == "PMC6907074"
    batch = await registry.call(
        SRC, "get_oa_status_batch", {"dois": ["10.1056/NEJMoa2034577", "10.1371/journal.pone.0000001", "10.9999/nope"]}
    )
    assert len(batch.records) == 2 and batch.notes


@respx.mock
async def test_invalid_doi_is_a_note_without_request(make_registry):
    route = respx.get(url__startswith="https://api.unpaywall.org/").mock(return_value=httpx.Response(200, json={}))
    registry = make_registry(SRC)
    for bad in ("", "not a doi", "https://doi.org/"):
        result = await registry.call(SRC, "get_oa_status", {"doi": bad})
        assert result.records == [] and "not a DOI" in result.notes[0]
    result = await registry.call(SRC, "get_oa_status_batch", {"dois": ["junk"]})
    assert result.records == [] and "not DOIs" in result.notes[0]
    assert not route.called
