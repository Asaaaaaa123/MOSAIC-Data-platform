"""CLI tests: argument parsing, parameter coercion and a few commands with mocks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import respx

from mosaic import __version__, cli
from mosaic.audit import AuditLog
from mosaic.cache import ResponseCache
from mosaic.registry import Registry

from .conftest import load_fixture
from .test_api import CROSSREF_WORKS, FakeAgent, write_export_files

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def patched(monkeypatch, offline_settings):
    """Point the CLI at offline settings and a crossref-only registry."""
    created: list[Registry] = []

    def make_registry(settings):
        registry = Registry.create(settings, only=["crossref"], audit=AuditLog(None), cache=ResponseCache(None, 0))
        created.append(registry)
        return registry

    monkeypatch.setattr(cli, "_settings", lambda: offline_settings)
    monkeypatch.setattr(cli, "_make_registry", make_registry)
    return created


@pytest.fixture
def crossref_mock():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(CROSSREF_WORKS).mock(return_value=httpx.Response(200, json=load_fixture("crossref", "search_works.json")))
        yield mock


# ------------------------------------------------------------------ parsing
def test_parser_commands():
    parser = cli.build_parser()
    args = parser.parse_args(["serve", "--host", "0.0.0.0", "--port", "9000", "--reload"])
    assert (args.command, args.host, args.port, args.reload) == ("serve", "0.0.0.0", 9000, True)
    args = parser.parse_args(["serve"])
    assert args.host is None and args.port is None and args.reload is False

    args = parser.parse_args(["sources", "--category", "drugs"])
    assert args.category == "drugs"
    with pytest.raises(SystemExit):
        parser.parse_args(["sources", "--category", "nope"])

    assert parser.parse_args(["health", "pubmed", "crossref"]).names == ["pubmed", "crossref"]
    assert parser.parse_args(["health"]).names == []

    args = parser.parse_args(["call", "crossref", "search_works", "query=CRISPR", "limit=3", "--params", '{"sort": "published"}'])
    assert (args.source, args.tool, args.pairs, args.params) == ("crossref", "search_works", ["query=CRISPR", "limit=3"], '{"sort": "published"}')

    args = parser.parse_args(["search", "KRAS G12C", "--categories", "drugs", "clinical", "--limit", "3"])
    assert args.query == "KRAS G12C" and args.categories == ["drugs", "clinical"] and args.limit == 3

    args = parser.parse_args(["catalog", "imaging", "--kind", "generalist", "--access", "open"])
    assert (args.query, args.kind, args.access) == ("imaging", "generalist", "open")
    assert parser.parse_args(["catalog"]).query is None

    args = parser.parse_args(["ask", "What is KRAS?", "--session", "abc"])
    assert (args.question, args.session) == ("What is KRAS?", "abc")
    assert parser.parse_args(["exports"]).command == "exports"

    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_parse_params_typed_by_schema():
    schema = {
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "score": {"type": "number"},
            "open_access": {"type": "boolean"},
            "ids": {"type": "array", "items": {"type": "integer"}},
            "names": {"type": "array", "items": {"type": "string"}},
            "phase": {"enum": ["PHASE1", "PHASE2"]},
        }
    }
    params = cli.parse_params(
        ["query=KRAS G12C", "limit=5", "score=0.5", "open_access=yes", "ids=1,2,3", "names=a, b", "phase=PHASE2"],
        '{"limit": 1, "extra": true}',
        schema,
    )
    assert params == {
        "query": "KRAS G12C",
        "limit": 5,
        "score": 0.5,
        "open_access": True,
        "ids": [1, 2, 3],
        "names": ["a", "b"],
        "phase": "PHASE2",
        "extra": True,
    }
    # values that look like numbers stay strings when the schema says string
    assert cli.parse_params(["query=12345"], None, schema) == {"query": "12345"}
    # unknown keys fall back to JSON parsing
    assert cli.parse_params(["x=10", "y=hello"], None, schema) == {"x": 10, "y": "hello"}
    with pytest.raises(ValueError):
        cli.parse_params(["limit=many"], None, schema)
    with pytest.raises(ValueError):
        cli.parse_params(["novalue"], None, schema)
    with pytest.raises(ValueError):
        cli.parse_params([], "[1, 2]", schema)


def test_limit_must_be_positive(capsys):
    with pytest.raises(SystemExit):
        cli.main(["search", "x", "--limit", "0"])


# ----------------------------------------------------------------- commands
def test_sources_command(patched, capsys):
    assert cli.main(["sources"]) == 0
    out = capsys.readouterr().out
    assert "crossref" in out and "ready" in out
    assert "1 sources, 1 configured" in out
    assert patched and patched[0].http.__class__.__name__ == "HttpClient"
    assert cli.main(["sources", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["name"] == "crossref"


def test_call_command(patched, crossref_mock, capsys):
    assert cli.main(["call", "crossref", "search_works", "query=prime editing", "limit=2"]) == 0
    out = capsys.readouterr().out
    assert "crossref.search_works" in out
    assert "4,321" in out
    request = crossref_mock.calls.last.request
    assert request.url.params["query"] == "prime editing"
    assert request.url.params["rows"] == "2"


def test_call_command_errors(patched, capsys):
    assert cli.main(["call", "nope", "x"]) == 2
    assert "unknown source" in capsys.readouterr().err
    assert cli.main(["call", "crossref", "nope"]) == 2
    assert "unknown tool" in capsys.readouterr().err
    assert cli.main(["call", "crossref", "search_works", "limit=x"]) == 2
    assert "invalid parameters" in capsys.readouterr().err
    assert cli.main(["call", "crossref", "search_works", "limit=3"]) == 1  # missing query -> SourceError
    assert "query" in capsys.readouterr().err


def test_search_command(patched, crossref_mock, capsys):
    assert cli.main(["search", "prime editing", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "Searched 1 source(s)" in out
    assert "[crossref]" in out
    assert cli.main(["search", "x", "--categories", "bogus"]) == 2


def test_catalog_command(capsys):
    assert cli.main(["catalog", "imaging", "--json", "--limit", "5"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert 0 < len(data) <= 5
    assert cli.main(["catalog", "--kind", "generalist", "--limit", "50"]) == 0
    out = capsys.readouterr().out
    assert "9 shown" in out


def test_health_command(patched, crossref_mock, capsys):
    assert cli.main(["health", "crossref"]) == 0
    assert "1/1 healthy" in capsys.readouterr().out
    assert cli.main(["health", "nope"]) == 2


def test_ask_command_streams(patched, monkeypatch, offline_settings, capsys):
    agent = FakeAgent(offline_settings.exports_dir, None)
    monkeypatch.setattr(cli, "_make_agent", lambda registry, settings: agent)
    assert cli.main(["ask", "KRAS G12C landscape"]) == 0
    out = capsys.readouterr().out
    assert "[session " in out
    assert "-> crossref__search_works" in out
    assert "ok crossref: 2 of 4,321 records" in out
    assert "[table saved] kras_trials: 2 rows" in out
    assert "Hello **world**\nline two" in out
    assert "continue with --session" in out
    assert agent.closed is True
    assert agent.calls == [("KRAS G12C landscape", None)]

    assert cli.main(["ask", "again", "--session", "abc", "--json"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[0]["type"] == "session_started" and lines[-1]["type"] == "done"
    assert agent.calls[-1] == ("again", "abc")


def test_ask_command_agent_unavailable(patched, monkeypatch, capsys):
    def unavailable(registry, settings):
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(cli, "_make_agent", unavailable)
    assert cli.main(["ask", "hi"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_exports_command(patched, offline_settings, capsys):
    assert cli.main(["exports"]) == 0
    assert "no exports yet" in capsys.readouterr().out
    export_id, _, _ = write_export_files(offline_settings.exports_dir, None, name="KRAS trials", session_id="s1")
    assert cli.main(["exports"]) == 0
    out = capsys.readouterr().out
    assert "KRAS trials" in out and export_id[:12] in out
    assert cli.main(["exports", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["export_id"] == export_id


def test_serve_command(patched, monkeypatch, capsys):
    import uvicorn

    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app, **kw))
    # cmd_serve exports MOSAIC_BIND_HOST; setenv registers an undo so it does not leak
    monkeypatch.setenv("MOSAIC_BIND_HOST", "127.0.0.1")
    assert cli.main(["serve", "--port", "8765"]) == 0
    assert captured["app"] == "mosaic.api.app:create_app"
    assert captured["factory"] is True
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8765
    assert captured["reload"] is False
    assert "http://localhost:8765/" in capsys.readouterr().out

    assert cli.main(["serve", "--host", "0.0.0.0"]) == 0
    assert "no authentication" in capsys.readouterr().err


def test_python_dash_m_mosaic():
    result = subprocess.run(
        [sys.executable, "-m", "mosaic", "--version"], cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"mosaic {__version__}"
