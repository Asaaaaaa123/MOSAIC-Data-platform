"""The Cancer Imaging Archive (TCIA) connector tests."""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import load_fixture

WP = "https://www.cancerimagingarchive.net/api/v1"
NBIA = "https://services.cancerimagingarchive.net/nbia-api/services/v1"
BRCA_DOI = "10.7937/k9/tcia.2016.ab2nazrp"


def _collection_pages(request: httpx.Request) -> httpx.Response:
    page = request.url.params.get("page")
    if page == "1":
        return httpx.Response(200, json=load_fixture("tcia", "collections_page1.json"))
    if page == "2":
        return httpx.Response(200, json=load_fixture("tcia", "collections_page2.json"))
    return httpx.Response(400, json={"code": "rest_post_invalid_page_number"})


def _mock_collections():
    return respx.get(f"{WP}/collections/").mock(side_effect=_collection_pages)


@respx.mock
async def test_search_collections_ranks_and_normalizes(make_registry):
    route = _mock_collections()
    registry = make_registry("tcia")
    result = await registry.call("tcia", "search_collections", {"query": "breast MRI", "limit": 2})
    sent = route.calls[0].request.url.params
    assert sent["per_page"] == "100" and "collection_short_title" in sent["_fields"]
    assert {c.request.url.params["page"] for c in route.calls} == {"1", "2", "3"}
    ids = [r.id for r in result.records]
    assert set(ids) == {"TCGA-BRCA", "Breast-MRI-NACT-Pilot"}  # MRI matches the MR modality code
    brca = next(r for r in result.records if r.id == "TCGA-BRCA")
    assert brca.type == "dataset"
    assert brca.title == "The Cancer Genome Atlas Breast Invasive Carcinoma Collection"
    assert brca.url == "https://www.cancerimagingarchive.net/collection/tcga-brca/"
    assert brca.identifiers == {"doi": BRCA_DOI, "tcia": "TCGA-BRCA", "gdc_project": "TCGA-BRCA"}
    assert brca.date == "2020-05-29"
    assert brca.attributes["subjects"] == 139
    assert brca.attributes["modalities"] == ["MR", "MG"]
    assert brca.attributes["access"] == "Public"
    assert brca.summary and "<" not in brca.summary
    assert result.total == 4 and result.next_cursor == "2"  # + 2 partial (MRI-only) matches ranked after
    assert any("match all query terms" in n for n in result.notes)


@respx.mock
async def test_search_filters_cursor_and_limited_note(make_registry):
    _mock_collections()
    registry = make_registry("tcia")
    lung = await registry.call("tcia", "search_collections", {"query": "lung", "limit": 1})
    assert lung.total == 3  # QIN LUNG CT, NSCLC-Radiomics, LIDC-IDRI
    assert lung.next_cursor == "1"
    page2 = await registry.call("tcia", "search_collections", {"query": "lung", "limit": 1, "cursor": "1"})
    assert page2.records[0].id != lung.records[0].id

    limited = await registry.call("tcia", "search_collections", {"query": "*", "access": "limited"})
    assert [r.id for r in limited.records] == ["RHUH-GBM"]
    assert "gdc_project" not in limited.records[0].identifiers
    assert any("limited-access" in n for n in limited.notes)

    ct = await registry.call("tcia", "search_collections", {"query": "*", "modality": "CT", "cancer_type": "lung"})
    assert ct.records and all("CT" in r.attributes["modalities"] for r in ct.records)

    gbm = await registry.call("tcia", "search_collections", {"query": "glioblastoma MRI"})
    assert {r.id for r in gbm.records[:2]} == {"RHUH-GBM", "GBM-DSC-MRI-DRO"}

    none = await registry.call("tcia", "search_collections", {"query": "zzzzqqq"})
    assert none.records == [] and none.total == 0


@respx.mock
async def test_get_collection_full(make_registry):
    _mock_collections()
    detail = respx.get(f"{WP}/collections/43753").mock(
        return_value=httpx.Response(200, json=load_fixture("tcia", "collection_tcga-brca.json"))
    )
    downloads = respx.get(f"{WP}/downloads/").mock(
        return_value=httpx.Response(200, json=load_fixture("tcia", "downloads_tcga-brca.json"))
    )
    citations = respx.get(f"{WP}/citations/").mock(
        return_value=httpx.Response(200, json=load_fixture("tcia", "citations_tcga-brca.json"))
    )
    analyses = respx.get(f"{WP}/analysis-results/").mock(
        return_value=httpx.Response(200, json=load_fixture("tcia", "analyses_tcga-brca.json"))
    )
    registry = make_registry("tcia")
    result = await registry.call("tcia", "get_collection", {"collection": "tcga-brca"})
    assert detail.called
    assert downloads.calls.last.request.url.params["include"] == "43735"
    assert citations.calls.last.request.url.params["include"] == "43731,43733,9225"
    assert analyses.calls.last.request.url.params["include"] == "45551,45635"
    record = result.records[0]
    assert record.id == "TCGA-BRCA"
    assert record.identifiers["doi"] == BRCA_DOI
    attrs = record.attributes
    assert attrs["licenses"] == ["CC BY 3.0"]
    dl = attrs["downloads"][0]
    assert dl["access"] == "Public" and dl["series"] == 1877 and dl["file_types"] == ["DICOM"]
    assert attrs["image_series"] == 1877 and attrs["image_studies"] == 164 and attrs["images"] == 230167
    assert attrs["data_citation"].startswith("Lingle, W.")
    assert attrs["citation_dois"] == ["10.1007/s10278-013-9622-7"]
    assert "10.7937/k9/tcia.2014.8sipiy6g" in attrs["related_dois"]
    assert attrs["related_analysis_results"][0]["url"].startswith("https://www.cancerimagingarchive.net/analysis-result/")
    assert result.notes == []


@respx.mock
async def test_get_collection_by_doi_limited_and_not_found(make_registry):
    _mock_collections()
    respx.get(f"{WP}/collections/45351").mock(return_value=httpx.Response(404, json={"code": "rest_post_invalid_id"}))
    registry = make_registry("tcia")
    limited = await registry.call("tcia", "get_collection", {"collection": "https://doi.org/10.7937/4545-C905"})
    assert limited.records[0].id == "RHUH-GBM"
    assert any(n.startswith("LIMITED ACCESS") for n in limited.notes)
    missing = await registry.call("tcia", "get_collection", {"collection": "No-Such-Collection"})
    assert missing.records == [] and "no TCIA collection matches" in missing.notes[0]


@respx.mock
async def test_series_for_collection(make_registry):
    route = respx.get(f"{NBIA}/getSeries").mock(
        return_value=httpx.Response(200, json=load_fixture("tcia", "series_tcga-brca_patient.json"))
    )
    registry = make_registry("tcia")
    result = await registry.call(
        "tcia",
        "series_for_collection",
        {"collection": "TCGA-BRCA", "patient_id": "TCGA-AR-A1AQ", "modality": "MR", "limit": 4},
    )
    sent = route.calls.last.request.url.params
    assert sent["Collection"] == "TCGA-BRCA" and sent["PatientID"] == "TCGA-AR-A1AQ" and sent["Modality"] == "MR"
    assert "StudyInstanceUID" not in sent
    assert result.total == 6 and result.next_cursor == "4" and len(result.records) == 4
    first = result.records[0]
    assert first.identifiers["dicom_series_uid"] == first.id
    assert first.url.startswith("https://nbia.cancerimagingarchive.net/viewer/?series=1.3.6.1.4.1.14519")
    assert first.title.startswith("MR series")
    assert first.attributes["patient_id"] == "TCGA-AR-A1AQ"
    assert first.attributes["collection_doi"] == BRCA_DOI
    assert first.date and len(first.date) == 10
    numbers = [r.attributes["series_number"] for r in result.records]
    assert numbers == sorted(numbers)
    assert result.data["series"] == 6 and result.data["patients"] == 1 and result.data["modalities"] == {"MR": 6}


@respx.mock
async def test_series_empty_limited_collection(make_registry):
    _mock_collections()
    respx.get(f"{NBIA}/getSeries").mock(return_value=httpx.Response(200, content=b""))
    registry = make_registry("tcia")
    result = await registry.call("tcia", "series_for_collection", {"collection": "RHUH-GBM"})
    assert result.records == [] and result.total == 0
    assert "limited-access" in result.notes[0]
    unknown = await registry.call("tcia", "series_for_collection", {"collection": "Nope"})
    assert "not recognised" in unknown.notes[0]


@respx.mock
async def test_studies_for_collection(make_registry):
    route = respx.get(f"{NBIA}/getPatientStudy").mock(
        return_value=httpx.Response(200, json=load_fixture("tcia", "studies_tcga-brca.json"))
    )
    registry = make_registry("tcia")
    result = await registry.call("tcia", "studies_for_collection", {"collection": "TCGA-BRCA", "limit": 2})
    assert route.calls.last.request.url.params["Collection"] == "TCGA-BRCA"
    assert result.total == 5 and result.next_cursor == "2"
    first = result.records[0]
    assert first.type == "study"
    assert first.attributes["patient_id"] == "TCGA-AR-A1AQ" and first.date == "2001-11-21"
    assert first.identifiers["dicom_study_uid"] == first.id
    assert result.data == {"patients": 3, "studies": 5, "series": 64, "patient_sex": {"F": 3}}


@pytest.mark.live
async def test_live_tcia_all_tools(live_registry):
    registry = live_registry("tcia")
    search = await registry.call("tcia", "search_collections", {"query": "breast MRI", "limit": 5})
    assert search.records and search.total
    assert all(r.identifiers.get("tcia") and r.identifiers.get("doi", "").startswith("10.") for r in search.records)
    detail = await registry.call("tcia", "get_collection", {"collection": "TCGA-BRCA"})
    record = detail.records[0]
    assert record.identifiers["gdc_project"] == "TCGA-BRCA"
    assert record.attributes["licenses"] and record.attributes["image_series"]
    series = await registry.call(
        "tcia", "series_for_collection", {"collection": "TCGA-BRCA", "patient_id": "TCGA-AR-A1AQ", "limit": 3}
    )
    assert len(series.records) == 3 and series.data["patients"] == 1
    studies = await registry.call("tcia", "studies_for_collection", {"collection": "TCGA-BRCA", "limit": 3})
    assert studies.records and studies.data["patients"] > 100
