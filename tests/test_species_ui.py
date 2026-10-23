"""Cross-species dashboard (web UI): static wiring checks + the API endpoints the page reads."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mosaic.api import create_app

WEB = Path(__file__).resolve().parents[1] / "mosaic" / "web"
SAMPLE = Path(__file__).parent / "fixtures" / "species" / "sample_comparison.json"
SAMPLE_ID = "00000000000000000000000000000001"


@pytest.fixture
def client(offline_settings, make_registry):
    target = offline_settings.data_dir / "species"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(SAMPLE, target / f"{SAMPLE_ID}.json")
    app = create_app(offline_settings, registry=make_registry(), agent=None, load_agent_on_startup=False)
    with TestClient(app) as c:
        yield c


def test_index_has_species_view_and_nav():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="view-species"' in html and 'data-view="species"' in html
    assert 'href="#species"' in html
    for el in ("sp-form", "sp-q", "sp-species-groups", "sp-papers", "sp-claude", "sp-run", "sp-status", "sp-history", "sp-dashboard"):
        assert f'id="{el}"' in html, el
    assert "Extract dose and effect from abstracts with Claude" in html


def test_app_js_wires_species_feature():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert "'species'" in js.split("const VIEWS", 1)[1].split("\n", 1)[0]
    assert "case 'species_comparison'" in js
    assert "entry.comparisons" in js  # restored from session transcripts
    for path in ("/api/species/meta", "/api/species?limit=", "/api/species/compare", "'/api/species/' + id"):
        assert path in js, path
    # charts are hand-written SVG; no external libraries or CDNs
    assert "createElementNS" in js
    assert not re.search(r"https?://(cdn|unpkg|cdnjs)\.", js)
    # untrusted strings never go through innerHTML / insertAdjacentHTML
    species_js = js.split("// Cross-species comparison", 1)[1]
    assert not re.search(r"\.innerHTML\s*=|insertAdjacentHTML|outerHTML\s*=", species_js)


def test_styles_define_light_and_dark_tokens():
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    blocks = css.split("@media (prefers-color-scheme: dark)")
    light = [b for b in blocks if "--sp-seq-5: #104281" in b]
    dark = [b for b in blocks[1:] if "--sp-seq-5: #9ec5f4" in b.split("\n}\n", 1)[0]]
    assert light and dark, "species colour tokens must be defined for light and dark mode"
    assert "--sp-c8" in css


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_js_syntax():
    result = subprocess.run(["node", "--check", str(WEB / "app.js")], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_species_api_serves_dashboard_data(client):
    meta = client.get("/api/species/meta").json()
    keys = [s["key"] for s in meta["species"]]
    assert keys[0] == "human" and "mouse" in keys and "arabidopsis" in keys
    assert {"label", "label_zh", "group", "scientific_name"} <= set(meta["species"][0])

    listing = client.get("/api/species").json()
    assert any(item["comparison_id"] == SAMPLE_ID for item in listing["comparisons"])

    comp = client.get(f"/api/species/{SAMPLE_ID}").json()
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    assert comp["compound"]["name"] == sample["compound"]["name"]
    # every field the dashboard reads is present
    for key in ("targets", "species", "similarity", "measurements", "papers", "methods", "notes", "errors", "package"):
        assert key in comp, key
    s = comp["species"][0]
    for key in ("species", "label", "label_zh", "group", "mean_identity_pct", "per_target_identity", "n_papers",
                "n_measurements", "measurements_by_kind", "median_pchembl", "evidence_level"):
        assert key in s, key

    assert client.get("/api/species/" + "0" * 31 + "9").status_code == 404


def test_static_assets_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "view-species" in page.text
    js = client.get("/static/app.js")
    assert js.status_code == 200 and "speciesChatCard" in js.text


def test_species_dashboard_review_fixes_present():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    section = js.split("// Cross-species comparison: form, dashboard, chat card", 1)[1]
    # Escape closes the species detail panel and focus goes back to its graph node
    assert "e.key === 'Escape'" in section and "closeDetail" in section
    # the dose-effect chart keeps a single ResizeObserver (redraws via spRedraw on unit change)
    assert "holder.spRedraw" in section
    # stale fetches of a previous comparison never overwrite a newer one
    assert "spState.openToken" in section
    # empty comparisons get empty states, not a lone fake human node
    assert "No species in this comparison" in section
