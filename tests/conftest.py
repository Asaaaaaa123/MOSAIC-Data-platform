"""Shared fixtures.

Offline tests mock HTTP with ``respx`` (connectors use ``httpx``). Live tests
are marked ``@pytest.mark.live`` and only run with ``MOSAIC_LIVE_TESTS=1``;
they use the real keys in ``.env``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mosaic.audit import AuditLog
from mosaic.cache import ResponseCache
from mosaic.config import Settings
from mosaic.registry import Registry

LIVE = os.environ.get("MOSAIC_LIVE_TESTS") == "1"
FIXTURES = Path(__file__).parent / "fixtures"

_KEY_FIELDS = [
    "ncbi_api_key",
    "semantic_scholar_api_key",
    "openalex_api_key",
    "elsevier_scopus_api_key",
    "springer_openaccess_api_key",
    "springer_meta_api_key",
    "openfda_api_key",
    "clarivate_api_key",
]


def pytest_collection_modifyitems(config, items):
    if LIVE:
        return
    skip = pytest.mark.skip(reason="live test: set MOSAIC_LIVE_TESTS=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


def load_fixture(source: str, name: str):
    """Load ``tests/fixtures/<source>/<name>`` (JSON parsed, other files as text)."""
    path = FIXTURES / source / name
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix == ".json" else text


@pytest.fixture
def offline_settings(tmp_path) -> Settings:
    """Settings with dummy keys for every keyed source and no HTTP cache."""
    return Settings(
        _env_file=None,
        **{field: "test-key" for field in _KEY_FIELDS},
        biopha_contact_email="tests@mosaic.invalid",
        biopha_data_dir=tmp_path / "data",
        biopha_audit_db=tmp_path / "data" / "audit.db",
        biopha_cache_db=tmp_path / "data" / "cache.db",
        biopha_cache_ttl=0,
        anthropic_api_key="test-anthropic-key",
    )


@pytest.fixture
def live_settings(tmp_path) -> Settings:
    """Real settings from .env, with an isolated data dir and no cache."""
    return Settings(
        biopha_data_dir=tmp_path / "data",
        biopha_audit_db=tmp_path / "data" / "audit.db",
        biopha_cache_db=tmp_path / "data" / "cache.db",
        biopha_cache_ttl=0,
    )


@pytest.fixture
async def make_registry(offline_settings):
    """Factory: ``make_registry("crossref", "pubmed", settings=...)`` -> Registry."""
    created: list[Registry] = []

    def factory(*only: str, settings: Settings | None = None) -> Registry:
        registry = Registry.create(
            settings or offline_settings,
            only=only or None,
            audit=AuditLog(None),
            cache=ResponseCache(None, 0),
        )
        created.append(registry)
        return registry

    yield factory
    for registry in created:
        await registry.aclose()


@pytest.fixture
async def live_registry(live_settings):
    """Factory for live tests: ``live_registry("crossref")``."""
    created: list[Registry] = []

    def factory(*only: str) -> Registry:
        registry = Registry.create(
            live_settings, only=only or None, audit=AuditLog(None), cache=ResponseCache(None, 0)
        )
        created.append(registry)
        return registry

    yield factory
    for registry in created:
        await registry.aclose()
