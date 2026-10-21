"""Species comparison API routes and store (engine stubbed; see tests/test_species_engine.py for the engine)."""

from __future__ import annotations

import json

import pytest

from mosaic.api import create_app
from mosaic.species import DEFAULT_SPECIES, SpeciesComparison
from mosaic.species.service import event_summary
from mosaic.species.store import list_comparisons, load_comparison, save_comparison

from .conftest import FIXTURES
from .test_api import _client

SAMPLE_ID = "0" * 31 + "1"


def sample() -> SpeciesComparison:
    return SpeciesComparison.model_validate(json.loads((FIXTURES / "species" / "sample_comparison.json").read_text(encoding="utf-8")))


@pytest.fixture
async def species_client(offline_settings, make_registry):
    app = create_app(offline_settings, registry=make_registry("crossref"), agent=None, load_agent_on_startup=False)
    async with _client(app) as c:
        yield c


def test_store_round_trip(offline_settings):
    comparison = sample()
    save_comparison(offline_settings, comparison)
    loaded = load_comparison(offline_settings, SAMPLE_ID)
    assert loaded is not None and loaded.compound.name == "SOTORASIB" and len(loaded.species) == 13
    assert list_comparisons(offline_settings)[0]["targets"] == ["KRAS"]
    assert load_comparison(offline_settings, "../../etc/passwd") is None
    assert load_comparison(offline_settings, "f" * 32) is None


def test_event_summary_is_small_and_complete():
    summary = event_summary(sample())
    assert summary["compound"] == "SOTORASIB" and summary["targets"] == ["KRAS"]
    mouse = next(s for s in summary["species"] if s["species"] == "mouse")
    assert mouse["label_zh"] == "小鼠" and mouse["mean_identity_pct"] == 98.9 and mouse["n_papers"] == 168
    assert len(json.dumps(summary)) < 6000


async def test_species_routes(species_client, offline_settings, monkeypatch):
    meta = (await species_client.get("/api/species/meta")).json()
    assert meta["default"] == DEFAULT_SPECIES and meta["species"][0]["key"] == "human"
    assert any(s["key"] == "arabidopsis" and s["group"] == "plant" for s in meta["species"])

    save_comparison(offline_settings, sample())
    listing = (await species_client.get("/api/species")).json()
    assert listing["comparisons"][0]["comparison_id"] == SAMPLE_ID
    got = await species_client.get(f"/api/species/{SAMPLE_ID}")
    assert got.status_code == 200 and got.json()["compound"]["name"] == "SOTORASIB" and "records" not in got.json()
    assert (await species_client.get("/api/species/not-an-id")).status_code == 404

    calls = {}

    async def fake_run(registry, settings, query, **kwargs):
        calls.update(query=query, **kwargs)
        return sample()

    monkeypatch.setattr("mosaic.species.service.run_comparison", fake_run)
    res = await species_client.post("/api/species/compare", json={"query": " sotorasib ", "species": ["mouse", "rat"]})
    assert res.status_code == 200 and res.json()["comparison_id"] == SAMPLE_ID
    assert calls == {"query": "sotorasib", "species": ["mouse", "rat"], "max_papers": 5, "extract_with_claude": False}
    bad = await species_client.post("/api/species/compare", json={"query": "x", "species": ["unicorn"]})
    assert bad.status_code == 400
