"""CTD connector tests (offline respx mocks + live).

CTD's batch endpoint served an ALTCHA CAPTCHA page to every automated request while this
connector was built, so the JSON/TSV fixtures are small hand-made samples following CTD's
documented batch-report columns; ``captcha.html`` is the real (trimmed) block page.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.http import SourceError
from tests.conftest import load_fixture

BATCH = "https://ctdbase.org/tools/batchQuery.go"


def _json(name: str) -> httpx.Response:
    return httpx.Response(200, json=load_fixture("ctd", name), headers={"content-type": "application/json"})


@respx.mock
async def test_chemical_gene_interactions_filters_ranks_and_normalizes(make_registry):
    route = respx.get(BATCH).mock(return_value=_json("cgixns_chem.json"))
    registry = make_registry("ctd")
    result = await registry.call(
        "ctd", "chemical_gene_interactions", {"chemical": "bisphenol A", "organism": "Homo sapiens", "limit": 1}
    )
    sent = route.calls.last.request.url.params
    assert sent["inputType"] == "chem" and sent["inputTerms"] == "bisphenol A"
    assert sent["report"] == "cgixns" and sent["format"] == "json"
    assert result.total == 2 and result.next_cursor == "1"
    rec = result.records[0]  # most PubMed IDs first
    assert rec.type == "association"
    assert rec.title == "bisphenol A binds to ESR1 protein"
    # only the listed (answer) entity: carrying the chemical too would merge bisphenol A with ESR1
    assert rec.identifiers == {"entrez_gene": "2099", "gene_symbol": "ESR1"}
    assert rec.attributes["chemical_mesh"] == "C006780" and rec.attributes["pmids"][0] == "16123044"
    assert rec.attributes["actions"] == ["affects^binding"]
    assert rec.attributes["organism"] == "Homo sapiens" and rec.attributes["n_pmids"] == 3
    assert rec.url == "https://ctdbase.org/detail.go?type=chem&acc=C006780&view=ixn"

    filtered = await registry.call(
        "ctd", "chemical_gene_interactions", {"chemical": "bisphenol A", "action": "decreases^expression"}
    )
    assert [r.attributes["gene"] for r in filtered.records] == ["CYP19A1"]
    partner = await registry.call("ctd", "chemical_gene_interactions", {"chemical": "bisphenol A", "partner": "esr1"})
    assert partner.total == 2


@respx.mock
async def test_gene_input_with_tsv_body(make_registry):
    route = respx.get(BATCH).mock(
        return_value=httpx.Response(
            200, text=load_fixture("ctd", "cgixns_gene.tsv"), headers={"content-type": "text/tab-separated-values"}
        )
    )
    registry = make_registry("ctd")
    result = await registry.call("ctd", "chemical_gene_interactions", {"gene": "ESR1"})
    assert route.calls.last.request.url.params["inputType"] == "gene"
    assert result.total == 2
    names = {r.attributes["chemical"] for r in result.records}
    assert names == {"bisphenol A", "Estradiol"}
    assert result.records[0].attributes["pmids"][0] == "16123044"  # PubMedIDs column read case-insensitively
    assert result.records[0].identifiers == {"mesh": "C006780", "cas": "80-05-7"}  # gene query -> chemicals


async def test_interactions_requires_one_input(make_registry):
    registry = make_registry("ctd")
    result = await registry.call("ctd", "chemical_gene_interactions", {})
    assert "exactly one" in result.notes[0]


@respx.mock
async def test_chemical_disease_direct_and_inferred(make_registry):
    def respond(request):
        report = request.url.params["report"]
        return _json("chem_diseases_curated.json" if report == "diseases_curated" else "chem_diseases_inferred.json")

    route = respx.get(BATCH).mock(side_effect=respond)
    registry = make_registry("ctd")
    direct = await registry.call("ctd", "chemical_disease_associations", {"chemical": "arsenic"})
    assert route.calls.last.request.url.params["report"] == "diseases_curated"
    assert direct.total == 2
    top = direct.records[0]
    assert top.attributes["direct_evidence"] == "marker/mechanism"
    assert top.identifiers == {"mesh": "D012878"}  # the disease; arsenic's CAS would bridge chemical and disease
    assert top.attributes["cas"] == "7440-38-2"
    assert top.attributes["chemical_mesh"] == "D001151"
    assert top.title == "Arsenic - Skin Neoplasms (marker/mechanism)"
    assert direct.records[1].identifiers["omim"] == "612376"

    both = await registry.call("ctd", "chemical_disease_associations", {"chemical": "arsenic", "evidence": "both"})
    reports = [c.request.url.params["report"] for c in route.calls[-2:]]
    assert reports == ["diseases_curated", "diseases_inferred"]
    assert both.total == 4
    inferred = [r for r in both.records if "direct_evidence" not in r.attributes]
    assert inferred[0].attributes["inference_score"] == 88.41  # ranked by score
    assert inferred[0].attributes["inference_gene"] == ["TP53", "CDKN2A", "MT1A"]

    liver = await registry.call(
        "ctd", "chemical_disease_associations", {"chemical": "arsenic", "evidence": "inferred", "disease": "liver"}
    )
    assert [r.attributes["disease"] for r in liver.records] == ["Liver Neoplasms"]


@respx.mock
async def test_gene_disease_associations(make_registry):
    route = respx.get(BATCH).mock(return_value=_json("gene_diseases_curated.json"))
    registry = make_registry("ctd")
    result = await registry.call("ctd", "gene_disease_associations", {"gene": "TP53"})
    sent = route.calls.last.request.url.params
    assert sent["inputType"] == "gene" and sent["report"] == "diseases_curated"
    lfs = result.records[0]
    assert lfs.identifiers == {"mesh": "D016864", "omim": "151623"}  # the disease only
    assert lfs.attributes["gene_id"] == "7157" and "1978757" in lfs.attributes["pmids"]
    omim_only = result.records[1]
    assert omim_only.identifiers["omim"] == "202300" and "mesh" not in omim_only.identifiers


@respx.mock
async def test_disease_chemicals(make_registry):
    route = respx.get(BATCH).mock(return_value=_json("disease_chems_curated.json"))
    registry = make_registry("ctd")
    result = await registry.call("ctd", "disease_chemicals", {"disease": "asthma", "direct_evidence": "therapeutic"})
    sent = route.calls.last.request.url.params
    assert sent["inputType"] == "disease" and sent["report"] == "chems_curated"
    assert result.total == 1
    rec = result.records[0]
    assert rec.attributes["chemical"] == "Budesonide"
    assert rec.identifiers == {"mesh": "D019819", "cas": "51333-22-3"}  # the chemical only
    assert rec.url == "https://ctdbase.org/detail.go?type=chem&acc=D019819"


@respx.mock
async def test_no_matches_is_a_note(make_registry):
    respx.get(BATCH).mock(return_value=httpx.Response(200, json=[{"Input": "zzqx"}]))
    registry = make_registry("ctd")
    result = await registry.call("ctd", "chemical_gene_interactions", {"chemical": "zzqx"})
    assert result.records == [] and result.total == 0 and "no CTD" in result.notes[0]


@respx.mock
@pytest.mark.parametrize("status,ctype", [(302, "text/html; charset=utf-8"), (200, "text/html; charset=utf-8")])
async def test_captcha_page_raises_clear_error(make_registry, status, ctype):
    respx.get(BATCH).mock(
        return_value=httpx.Response(status, text=load_fixture("ctd", "captcha.html"), headers={"content-type": ctype})
    )
    registry = make_registry("ctd")
    with pytest.raises(SourceError, match="CAPTCHA"):
        await registry.call("ctd", "gene_disease_associations", {"gene": "TP53"})


@pytest.mark.live
async def test_live_all_tools(live_registry):
    """Skips (with the reason) while CTD blocks automated clients from this network with a CAPTCHA."""
    registry = live_registry("ctd")
    try:
        ixn = await registry.call(
            "ctd", "chemical_gene_interactions", {"chemical": "bisphenol A", "organism": "Homo sapiens", "limit": 3}
        )
    except SourceError as exc:
        if "CAPTCHA" in str(exc):
            pytest.skip("CTD serves a human-verification CAPTCHA to automated clients from this network")
        if "network error" in str(exc):
            pytest.skip("ctdbase.org is unreachable from this network")
        raise
    assert ixn.records and ixn.records[0].identifiers.get("entrez_gene")  # chemical query lists genes
    chem = await registry.call("ctd", "chemical_disease_associations", {"chemical": "arsenic", "limit": 3})
    assert chem.records
    gene = await registry.call("ctd", "gene_disease_associations", {"gene": "TP53", "limit": 3})
    assert gene.records
    dis = await registry.call("ctd", "disease_chemicals", {"disease": "asthma", "limit": 3})
    assert dis.records
