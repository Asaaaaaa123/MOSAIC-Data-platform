"""UniProt connector tests (offline respx mocks + live)."""

from __future__ import annotations

import httpx
import pytest
import respx

from mosaic.http import SourceError
from tests.conftest import load_fixture

API = "https://rest.uniprot.org"
NEXT_LINK = (
    '<https://rest.uniprot.org/uniprotkb/search?fields=accession,id,gene_names&query=BRAF'
    '&cursor=88d67348nie4k9wu&size=2>; rel="next"'
)


@respx.mock
async def test_search_proteins_normalizes_records(make_registry):
    route = respx.get(f"{API}/uniprotkb/search").mock(
        return_value=httpx.Response(
            200,
            json=load_fixture("uniprot", "search.json"),
            headers={"Link": NEXT_LINK, "X-Total-Results": "2219"},
        )
    )
    registry = make_registry("uniprot")
    result = await registry.call("uniprot", "search_proteins", {"query": "BRAF", "limit": 2})

    sent = route.calls.last.request.url.params
    assert sent["query"] == "BRAF"
    assert sent["size"] == "2"
    assert sent["format"] == "json"
    assert "xref_hgnc" in sent["fields"] and "xref_pdb" in sent["fields"]
    assert result.total == 2219
    assert result.next_cursor == "88d67348nie4k9wu"  # parsed from a Link URL that itself contains commas
    human = result.records[0]
    assert human.id == "P15056"
    assert human.type == "protein"
    assert human.title == "Serine/threonine-protein kinase B-raf"
    assert human.url == "https://www.uniprot.org/uniprotkb/P15056/entry"
    assert human.date == "1990-11-01"
    ids = human.identifiers
    assert ids["uniprot"] == "P15056"
    assert ids["gene_symbol"] == "BRAF"
    assert ids["hgnc"] == "HGNC:1097"
    assert ids["entrez_gene"] == "673"
    assert ids["ensembl_gene"] == "ENSG00000157764"  # version stripped
    assert ids["ensembl_transcript"] == "ENST00000646891"  # MANE Select
    assert ids["refseq"] == "NP_004324"  # versionless
    assert ids["chembl"] == "CHEMBL5145"
    assert ids["omim"] == "164757"
    assert "pdb" not in ids  # structures are related entities (see attributes.pdb_structures)
    assert human.attributes["organism"] == "Homo sapiens"
    assert human.attributes["taxon_id"] == 9606
    assert human.attributes["reviewed"] is True
    assert "PubMed:" not in (human.summary or "")
    mouse = result.records[1]
    assert mouse.attributes["organism"] == "Mus musculus"
    assert mouse.identifiers["gene_symbol"] == "Braf"


@respx.mock
async def test_search_proteins_filters_and_cursor(make_registry):
    route = respx.get(f"{API}/uniprotkb/search").mock(return_value=httpx.Response(200, json={"results": []}))
    registry = make_registry("uniprot")
    result = await registry.call(
        "uniprot",
        "search_proteins",
        {"query": "gene:EGFR", "organism_id": 9606, "reviewed": True, "cursor": "abc", "limit": 5},
    )
    sent = route.calls.last.request.url.params
    assert sent["query"] == "(gene:EGFR) AND (organism_id:9606) AND (reviewed:true)"
    assert sent["cursor"] == "abc"
    assert result.records == [] and result.next_cursor is None


@respx.mock
async def test_get_protein_full_annotation(make_registry):
    route = respx.get(f"{API}/uniprotkb/P04637").mock(
        return_value=httpx.Response(200, json=load_fixture("uniprot", "entry_P04637.json"))
    )
    registry = make_registry("uniprot")
    result = await registry.call("uniprot", "get_protein", {"accession": "P04637"})

    sent = route.calls.last.request.url.params
    assert "cc_disease" in sent["fields"] and "sequence" not in sent["fields"].split(",")
    record = result.records[0]
    assert record.id == "P04637"
    assert record.title == "Cellular tumor antigen p53"
    assert record.identifiers["gene_symbol"] == "TP53"
    assert record.identifiers["hgnc"] == "HGNC:11998"
    assert record.identifiers["entrez_gene"] == "7157"
    assert record.identifiers["ensembl_gene"] == "ENSG00000141510"
    assert record.identifiers["omim"] == "191170"
    attrs = record.attributes
    assert attrs["diseases"] and all("name" in d for d in attrs["diseases"])
    assert any(d.get("mim") for d in attrs["diseases"])
    assert "Nucleus" in attrs["subcellular_location"]
    resolutions = [p["resolution"] for p in attrs["pdb_structures"]]
    known = [r for r in resolutions if r is not None]
    assert known == sorted(known) and resolutions[: len(known)] == known  # best first, unknown (NMR) last
    # a structure is a related entity (complexes are shared by several proteins): attributes only
    assert "pdb" not in record.identifiers and attrs["pdb_structures"][0]["id"]
    assert attrs["drugbank_drugs"] and attrs["drugbank_drugs"][0]["drugbank"].startswith("DB")
    # drug / pathway IDs identify other entities and must not become record identifiers
    assert "drugbank" not in record.identifiers and "reactome" not in record.identifiers


@respx.mock
async def test_get_protein_entry_name_is_resolved_first(make_registry):
    lookup = respx.get(f"{API}/uniprotkb/search").mock(
        return_value=httpx.Response(200, json=load_fixture("uniprot", "entry_name_lookup.json"))
    )
    entry = respx.get(f"{API}/uniprotkb/P00533").mock(
        return_value=httpx.Response(200, json=load_fixture("uniprot", "entry_P04637.json"))
    )
    registry = make_registry("uniprot")
    await registry.call("uniprot", "get_protein", {"accession": "egfr_human"})
    assert lookup.calls.last.request.url.params["query"] == "id:EGFR_HUMAN"
    assert entry.called


@respx.mock
async def test_get_protein_not_found_and_inactive(make_registry):
    respx.get(f"{API}/uniprotkb/Q00000").mock(
        return_value=httpx.Response(404, json={"messages": ["Resource not found"]})
    )
    respx.get(f"{API}/uniprotkb/A0A008APQ8").mock(
        return_value=httpx.Response(200, json=load_fixture("uniprot", "inactive.json"))
    )
    registry = make_registry("uniprot")
    missing = await registry.call("uniprot", "get_protein", {"accession": "Q00000"})
    assert missing.records == [] and "not found" in missing.notes[0]
    inactive = await registry.call("uniprot", "get_protein", {"accession": "A0A008APQ8"})
    assert inactive.records == [] and "inactive" in inactive.notes[0] and "DELETED" in inactive.notes[0]


@respx.mock
async def test_map_ids_gene_names_to_uniprot(make_registry):
    run = respx.post(f"{API}/idmapping/run").mock(
        return_value=httpx.Response(200, json=load_fixture("uniprot", "idmapping_run.json"))
    )
    results = respx.get(f"{API}/idmapping/uniprotkb/results/2RS2mGWZLN").mock(
        side_effect=[
            httpx.Response(404, json={"messages": ["Resource not found"]}),  # job still running
            httpx.Response(200, json=load_fixture("uniprot", "idmapping_uniprotkb.json"), headers={"X-Total-Results": "1"}),
        ]
    )
    status = respx.get(f"{API}/idmapping/status/2RS2mGWZLN").mock(
        return_value=httpx.Response(200, json={"jobStatus": "RUNNING"})
    )
    registry = make_registry("uniprot")
    result = await registry.call(
        "uniprot",
        "map_ids",
        {"ids": ["EGFR", "NOTAGENE"], "from_db": "Gene_Name", "to_db": "UniProtKB-Swiss-Prot", "taxon_id": 9606},
    )
    form = dict(httpx.QueryParams(run.calls.last.request.content.decode()))
    assert form == {"ids": "EGFR,NOTAGENE", "from": "Gene_Name", "to": "UniProtKB-Swiss-Prot", "taxId": "9606"}
    assert status.called and results.call_count == 2
    assert results.calls.last.request.url.params["size"] == "100"
    assert result.records[0].id == "P00533"
    assert result.records[0].identifiers["hgnc"] == "HGNC:3236"
    assert result.records[0].attributes["mapped_from"] == ["EGFR"]
    assert result.data["mappings"] == [{"from": "EGFR", "to": "P00533"}]
    assert result.data["failed_ids"] == ["NOTAGENE"]
    assert result.total == 1


@respx.mock
async def test_map_ids_to_pdb_paginates_with_job_cursor(make_registry):
    respx.post(f"{API}/idmapping/run").mock(return_value=httpx.Response(200, json={"jobId": "JOB1"}))
    page = respx.get(f"{API}/idmapping/results/JOB1").mock(
        return_value=httpx.Response(
            200,
            json=load_fixture("uniprot", "idmapping_pdb.json"),
            headers={
                "X-Total-Results": "354",
                "Link": '<https://rest.uniprot.org/idmapping/results/JOB1?cursor=jxzylcj10&size=3>; rel="next"',
            },
        )
    )
    registry = make_registry("uniprot")
    result = await registry.call("uniprot", "map_ids", {"ids": ["P00533"], "to_db": "PDB", "limit": 3})
    assert result.records == []
    assert result.data["mappings"][0] == {"from": "P00533", "to": "1IVO"}
    assert result.total == 354
    assert result.next_cursor == "JOB1:jxzylcj10"

    again = await registry.call(
        "uniprot", "map_ids", {"ids": ["P00533"], "to_db": "PDB", "limit": 3, "cursor": "JOB1:jxzylcj10"}
    )
    assert page.calls.last.request.url.params["cursor"] == "jxzylcj10"
    assert again.data["job_id"] == "JOB1"


@respx.mock
async def test_map_ids_chains_external_to_external(make_registry):
    run = respx.post(f"{API}/idmapping/run").mock(
        side_effect=[httpx.Response(200, json={"jobId": "HOP1"}), httpx.Response(200, json={"jobId": "HOP2"})]
    )
    respx.get(f"{API}/idmapping/uniprotkb/results/HOP1").mock(
        return_value=httpx.Response(200, json=load_fixture("uniprot", "idmapping_geneid_swissprot.json"))
    )
    respx.get(f"{API}/idmapping/results/HOP2").mock(
        return_value=httpx.Response(200, json=load_fixture("uniprot", "idmapping_chembl.json"))
    )
    registry = make_registry("uniprot")
    result = await registry.call("uniprot", "map_ids", {"ids": ["1956"], "from_db": "GeneID", "to_db": "ChEMBL"})
    first = dict(httpx.QueryParams(run.calls[0].request.content.decode()))
    second = dict(httpx.QueryParams(run.calls[1].request.content.decode()))
    assert first["from"] == "GeneID" and first["to"] == "UniProtKB-Swiss-Prot"
    assert second == {"ids": "P00533", "from": "UniProtKB_AC-ID", "to": "ChEMBL"}
    assert result.data["mappings"] == [{"from": "1956", "via_uniprot": "P00533", "to": "CHEMBL203"}]
    assert "chained" in result.notes[0]


@respx.mock
async def test_map_ids_job_error_raises(make_registry):
    respx.post(f"{API}/idmapping/run").mock(return_value=httpx.Response(200, json={"jobId": "BAD"}))
    respx.get(f"{API}/idmapping/uniprotkb/results/BAD").mock(return_value=httpx.Response(404, json={}))
    respx.get(f"{API}/idmapping/status/BAD").mock(
        return_value=httpx.Response(200, json={"jobStatus": "ERROR", "errors": [{"message": "bad ids"}]})
    )
    registry = make_registry("uniprot")
    with pytest.raises(SourceError, match="bad ids"):
        await registry.call("uniprot", "map_ids", {"ids": ["x"]})


async def test_invalid_params(make_registry):
    registry = make_registry("uniprot")
    with pytest.raises(SourceError, match="invalid parameters"):
        await registry.call("uniprot", "map_ids", {"ids": []})


@pytest.mark.live
async def test_live_uniprot_all_tools(live_registry):
    registry = live_registry("uniprot")
    search = await registry.call(
        "uniprot", "search_proteins", {"query": "gene:EGFR", "organism_id": 9606, "reviewed": True, "limit": 3}
    )
    assert search.records[0].id == "P00533"
    assert search.records[0].identifiers["hgnc"] == "HGNC:3236"

    detail = await registry.call("uniprot", "get_protein", {"accession": "P04637"})
    record = detail.records[0]
    assert record.identifiers["gene_symbol"] == "TP53" and record.attributes["diseases"]

    mapped = await registry.call(
        "uniprot", "map_ids", {"ids": ["BRAF", "TP53"], "from_db": "Gene_Name", "to_db": "UniProtKB-Swiss-Prot", "taxon_id": 9606}
    )
    assert {r.id for r in mapped.records} >= {"P15056", "P04637"}

    to_pdb = await registry.call("uniprot", "map_ids", {"ids": ["P15056"], "to_db": "PDB", "limit": 5})
    assert to_pdb.data["mappings"] and to_pdb.next_cursor


def _seq_entry(acc: str, gene: str, taxon: int, organism: str, seq: str, reviewed: bool = True) -> dict:
    return {
        "entryType": "UniProtKB reviewed (Swiss-Prot)" if reviewed else "UniProtKB unreviewed (TrEMBL)",
        "primaryAccession": acc,
        "uniProtkbId": f"{gene.upper()}_X",
        "organism": {"scientificName": organism, "taxonId": taxon},
        "genes": [{"geneName": {"value": gene}}],
        "proteinDescription": {"recommendedName": {"fullName": {"value": "GTPase KRas"}}},
        "sequence": {"value": seq, "length": len(seq)},
    }


@respx.mock
async def test_get_sequences_batch(make_registry):
    route = respx.get(f"{API}/uniprotkb/accessions").mock(return_value=httpx.Response(200, json={"results": [
        _seq_entry("P01116", "KRAS", 9606, "Homo sapiens", "MTEYKLVVVG"),
        _seq_entry("P32883", "Kras", 10090, "Mus musculus", "MTEYKLVVVA"),
    ]}))
    registry = make_registry("uniprot")
    result = await registry.call("uniprot", "get_sequences", {"accessions": ["p01116", "P32883-2", "P01116", "not-an-acc", "Q99999"]})
    sent = route.calls.last.request.url.params
    assert sent["accessions"] == "P01116,P32883,Q99999"
    assert "sequence" in sent["fields"].split(",")
    seqs = {s["accession"]: s for s in result.data["sequences"]}
    assert seqs["P32883"]["sequence"] == "MTEYKLVVVA"
    assert seqs["P32883"]["gene"] == "Kras" and seqs["P32883"]["taxon_id"] == 10090 and seqs["P32883"]["length"] == 10
    assert seqs["P01116"]["reviewed"] is True
    assert "sequence" not in result.records[0].attributes  # records stay compact
    assert any("NOT-AN-ACC" in n for n in result.notes)
    assert any("Q99999" in n for n in result.notes)


@respx.mock
async def test_get_sequences_uniref50_members_in_taxon(make_registry):
    uniref = respx.get(f"{API}/uniref/search").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "UniRef50_P01116", "memberCount": 56}]})
    )
    members = respx.get(f"{API}/uniprotkb/search").mock(
        return_value=httpx.Response(200, json={"results": [{"primaryAccession": "Q9XFP1"}]})
    )
    respx.get(f"{API}/uniprotkb/accessions").mock(return_value=httpx.Response(200, json={"results": [
        _seq_entry("Q9XFP1", "RAB1", 3702, "Arabidopsis thaliana", "MNPEYDYLFK", reviewed=False),
    ]}))
    registry = make_registry("uniprot")
    result = await registry.call("uniprot", "get_sequences", {"uniref50_of": "P01116", "taxon_id": 3702})
    assert uniref.calls.last.request.url.params["query"] == "uniprot_id:P01116 AND identity:0.5"
    assert members.calls.last.request.url.params["query"] == "uniref_cluster_50:UniRef50_P01116 AND organism_id:3702"
    assert result.data["uniref50_cluster"] == "UniRef50_P01116"
    assert result.data["sequences"][0]["accession"] == "Q9XFP1"
    assert result.data["sequences"][0]["reviewed"] is False


@respx.mock
async def test_get_sequences_uniref50_no_member_is_a_note(make_registry):
    respx.get(f"{API}/uniref/search").mock(return_value=httpx.Response(200, json={"results": [{"id": "UniRef50_P01116"}]}))
    respx.get(f"{API}/uniprotkb/search").mock(return_value=httpx.Response(200, json={"results": []}))
    registry = make_registry("uniprot")
    result = await registry.call("uniprot", "get_sequences", {"uniref50_of": "P01116", "taxon_id": 3702})
    assert result.data["sequences"] == []
    assert any("no member of UniRef50_P01116 in taxon 3702" in n for n in result.notes)
    with pytest.raises(SourceError):
        await registry.call("uniprot", "get_sequences", {"uniref50_of": "KRAS"})


@pytest.mark.live
async def test_live_uniprot_get_sequences(live_registry):
    registry = live_registry("uniprot")
    result = await registry.call("uniprot", "get_sequences", {"accessions": ["P01116", "P32883"]})
    seqs = {s["accession"]: s["sequence"] for s in result.data["sequences"]}
    assert len(seqs["P01116"]) == 189 and seqs["P01116"].startswith("MTEYKLVVVG")
    assert len(seqs["P32883"]) == 189
