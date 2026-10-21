"""HTTP API tests: every endpoint, SSE framing, agent-unavailable 503s, export path safety."""

from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from mosaic.agent.events import (
    AgentError,
    Done,
    ServerToolActivity,
    SessionStarted,
    TableSaved,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnCompleted,
)
from mosaic.api import create_app
from mosaic.api.exports import resolve_export_file
from mosaic.api.streaming import HEARTBEAT, sse_stream

from .conftest import load_fixture

CROSSREF_WORKS = "https://api.crossref.org/works"


# ---------------------------------------------------------------------------
# Fake agent (implements mosaic.agent.events.AgentLike)
# ---------------------------------------------------------------------------
def write_export_files(exports_dir: Path, audit: Any, *, name: str, session_id: str | None) -> tuple[str, list[str], list[dict]]:
    """Write an export the way MosaicAgent does (CSV + JSON + audit row)."""
    export_id = uuid.uuid4().hex
    columns = ["drug", "nct_id", "phase"]
    rows = [
        {"drug": "sotorasib", "nct_id": "NCT04303780", "phase": "PHASE3"},
        {"drug": "adagrasib", "nct_id": "NCT04685135", "phase": "PHASE3"},
    ]
    exports_dir.mkdir(parents=True, exist_ok=True)
    csv_text = ",".join(columns) + "\r\n" + "".join(",".join(r[c] for c in columns) + "\r\n" for r in rows)
    (exports_dir / f"{export_id}.csv").write_text(csv_text, encoding="utf-8")
    document = {
        "export_id": export_id,
        "name": name,
        "description": "KRAS G12C phase 3 trials",
        "columns": columns,
        "rows": rows,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "provenance": {"sources": ["clinicaltrials"]},
    }
    (exports_dir / f"{export_id}.json").write_text(json.dumps(document), encoding="utf-8")
    if audit is not None:
        audit.record_export(
            name=name,
            path=str(exports_dir / f"{export_id}.csv"),
            n_rows=len(rows),
            columns=columns,
            session_id=session_id,
        )
    return export_id, columns, rows


class FakeAgent:
    model = "fake-claude"

    def __init__(self, exports_dir: Path | None = None, audit: Any = None, *, delay: float = 0.0, fail: bool = False):
        self.exports_dir = exports_dir
        self.audit = audit
        self.delay = delay
        self.fail = fail
        self.sessions: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str | None]] = []
        self.closed = False

    async def run(self, user_message: str, *, session_id: str | None = None):
        self.calls.append((user_message, session_id))
        sid = session_id or uuid.uuid4().hex
        yield SessionStarted(session_id=sid, model=self.model, title=user_message[:40])
        yield ThinkingDelta(session_id=sid, text="Plan: search trials.")
        yield ToolCallStarted(
            session_id=sid, call_id="c1", name="crossref__search_works", source="crossref", input={"query": "KRAS"}
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("model exploded")
        yield ToolCallFinished(
            session_id=sid,
            call_id="c1",
            name="crossref__search_works",
            ok=True,
            summary="crossref: 2 of 4,321 records",
            n_records=2,
            total=4321,
            latency_ms=12.5,
            result_id="r1",
            preview=[{"id": "10.1/x", "title": "A paper", "url": "https://doi.org/10.1/x"}],
        )
        yield ServerToolActivity(session_id=sid, name="web_search", phase="call", detail="KRAS G12C")
        if self.exports_dir is not None:
            export_id, columns, rows = write_export_files(
                self.exports_dir, self.audit, name="kras_trials", session_id=sid
            )
            yield TableSaved(
                session_id=sid,
                export_id=export_id,
                name="kras_trials",
                n_rows=len(rows),
                columns=columns,
                csv_url=f"/api/exports/{export_id}.csv",
                json_url=f"/api/exports/{export_id}.json",
            )
        yield TextDelta(session_id=sid, text="Hello **world**\n")
        yield TextDelta(session_id=sid, text="line two")
        usage = {"input_tokens": 10, "output_tokens": 5}
        yield TurnCompleted(session_id=sid, stop_reason="end_turn", usage=usage)
        self.sessions[sid] = {
            "session_id": sid,
            "title": user_message[:40],
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "turns": len([c for c in self.calls if c[1] in (None, sid)]),
            "transcript": [
                {"role": "user", "text": user_message, "tool_calls": []},
                {"role": "assistant", "text": "Hello **world**\nline two", "tool_calls": [{"name": "crossref__search_works"}]},
            ],
        }
        yield Done(session_id=sid, final_text="Hello **world**\nline two", stop_reason="end_turn", usage=usage)

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return [{k: v for k, v in s.items() if k != "transcript"} for s in list(self.sessions.values())[:limit]]

    def get_transcript(self, session_id: str) -> list[dict[str, Any]]:
        if session_id not in self.sessions:
            raise KeyError(session_id)
        return self.sessions[session_id]["transcript"]

    def delete_session(self, session_id: str) -> bool:
        return self.sessions.pop(session_id, None) is not None

    async def aclose(self) -> None:
        self.closed = True


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for frame in text.replace("\r\n", "\n").split("\n\n"):
        if not frame.strip():
            continue
        name, data = None, []
        for line in frame.split("\n"):
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                name = value
            elif field == "data":
                data.append(value)
        if data:
            events.append((name, json.loads("\n".join(data))))
    return events


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def registry(make_registry):
    return make_registry("crossref")


@pytest.fixture
def fake_agent(offline_settings, registry):
    return FakeAgent(offline_settings.exports_dir, registry.audit)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
async def app(offline_settings, registry, fake_agent):
    return create_app(offline_settings, registry=registry, agent=fake_agent)


@pytest.fixture
async def client(app):
    async with _client(app) as c:
        yield c


@pytest.fixture
async def no_agent_client(offline_settings, registry):
    app = create_app(offline_settings, registry=registry, agent=None, load_agent_on_startup=False)
    async with _client(app) as c:
        yield c


@pytest.fixture
def crossref_mock():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(CROSSREF_WORKS).mock(return_value=httpx.Response(200, json=load_fixture("crossref", "search_works.json")))
        yield mock


# ---------------------------------------------------------------------------
# Status, sources, tools
# ---------------------------------------------------------------------------
async def test_health(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["version"]
    assert body["sources"] == 1
    assert body["configured"] == 1
    assert body["agent_available"] is True
    assert body["model"] == "fake-claude"
    assert body["catalog_entries"] == 232
    # security headers
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_sources_list_and_filter(client):
    r = await client.get("/api/sources")
    assert r.status_code == 200
    body = r.json()
    assert "literature" in body["categories"]
    assert [s["name"] for s in body["sources"]] == ["crossref"]
    source = body["sources"][0]
    assert source["configured"] is True
    assert {t["name"] for t in source["tools"]} >= {"search_works", "get_work"}

    r = await client.get("/api/sources", params={"category": "clinical"})
    assert r.status_code == 200 and r.json()["sources"] == []

    r = await client.get("/api/sources", params={"category": "nope"})
    assert r.status_code == 400
    assert "unknown category" in r.json()["error"]


async def test_get_source(client):
    r = await client.get("/api/sources/crossref")
    assert r.status_code == 200
    assert r.json()["title"] == "Crossref"
    r = await client.get("/api/sources/nope")
    assert r.status_code == 404
    assert r.json()["kind"] == "unknown_source"


async def test_no_secrets_in_responses(client, registry):
    registry.connectors["crossref"].required_settings = ("ELSEVIER_SCOPUS_API_KEY", "MOSAIC_TEST_MISSING_KEY")
    texts = [
        (await client.get(path)).text
        for path in ("/api/health", "/api/sources", "/api/sources/crossref", "/api/tools")
    ]
    joined = "\n".join(texts)
    assert "test-key" not in joined
    assert "test-anthropic-key" not in joined
    # only the NAME of the missing setting is exposed
    info = (await client.get("/api/sources/crossref")).json()
    assert info["missing_settings"] == ["MOSAIC_TEST_MISSING_KEY"]


async def test_tools(client):
    r = await client.get("/api/tools")
    assert r.status_code == 200
    tools = r.json()["tools"]
    names = {t["qualified_name"] for t in tools}
    assert "crossref__search_works" in names
    search = next(t for t in tools if t["name"] == "search_works")
    assert search["federated"] is True
    assert search["input_schema"]["properties"]["query"]["type"] == "string"
    r = await client.get("/api/tools", params={"source": "crossref"})
    assert r.json()["total"] == len(tools)
    r = await client.get("/api/tools", params={"source": "nope"})
    assert r.status_code == 404


async def test_source_health(client, crossref_mock):
    r = await client.post("/api/sources/crossref/health")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "crossref" and body["ok"] is True
    assert body["latency_ms"] is not None

    r = await client.post("/api/sources/nope/health")
    assert r.status_code == 404


async def test_all_sources_health(client, crossref_mock):
    r = await client.post("/api/health/sources")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] == 1 and body["failed"] == 0
    assert body["results"][0]["source"] == "crossref"

    r = await client.post("/api/health/sources", json={"sources": ["crossref"]})
    assert r.status_code == 200 and len(r.json()["results"]) == 1

    r = await client.post("/api/health/sources", json={"sources": ["nope"]})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /api/call and SourceError mapping
# ---------------------------------------------------------------------------
async def test_call_ok(client, crossref_mock, registry):
    r = await client.post(
        "/api/call", json={"source": "crossref", "tool": "search_works", "params": {"query": "prime editing", "limit": 2}}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "crossref" and body["tool"] == "search_works"
    assert body["total"] == 4321
    assert body["records"][0]["identifiers"]["doi"]
    assert crossref_mock.calls.call_count == 1
    # audited
    rows = registry.audit.recent_tool_calls()
    assert rows and rows[0]["source"] == "crossref" and rows[0]["ok"] == 1


async def test_call_unknown_tool_is_400(client):
    r = await client.post("/api/call", json={"source": "crossref", "tool": "nope", "params": {}})
    assert r.status_code == 400
    body = r.json()
    assert body["kind"] == "unknown_tool"
    assert body["source"] == "crossref"
    assert "search_works" in body["available"]


async def test_call_invalid_params_is_400(client):
    r = await client.post("/api/call", json={"source": "crossref", "tool": "search_works", "params": {"limit": 5}})
    assert r.status_code == 400
    body = r.json()
    assert body["kind"] == "invalid_params"
    assert body["source"] == "crossref"
    assert "query" in body["error"]

    r = await client.post(
        "/api/call", json={"source": "crossref", "tool": "search_works", "params": {"query": "x", "bogus": 1}}
    )
    assert r.status_code == 400


async def test_call_unknown_source_is_404(client):
    r = await client.post("/api/call", json={"source": "nope", "tool": "x", "params": {}})
    assert r.status_code == 404


async def test_call_upstream_failure_is_502(client):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(CROSSREF_WORKS).mock(return_value=httpx.Response(404, json={"message": "nope"}))
        r = await client.post("/api/call", json={"source": "crossref", "tool": "search_works", "params": {"query": "x"}})
    assert r.status_code == 502
    body = r.json()
    assert body["source"] == "crossref"
    assert body["status"] == 502
    assert body["kind"] == "upstream_error"
    assert body["upstream_status"] == 404


async def test_call_not_configured_is_503(client, registry):
    registry.connectors["crossref"].required_settings = ("MOSAIC_TEST_MISSING_KEY",)
    r = await client.post("/api/call", json={"source": "crossref", "tool": "search_works", "params": {"query": "x"}})
    assert r.status_code == 503
    body = r.json()
    assert body["kind"] == "not_configured"
    assert "MOSAIC_TEST_MISSING_KEY" in body["error"]


async def test_call_body_validation(client):
    r = await client.post("/api/call", json={"tool": "search_works"})
    assert r.status_code == 422
    assert "source" in r.json()["error"]


# ---------------------------------------------------------------------------
# Federated search
# ---------------------------------------------------------------------------
async def test_search(client, crossref_mock):
    r = await client.post("/api/search", json={"query": "prime editing", "limit_per_source": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["query"] == "prime editing"
    assert body["sources"][0]["source"] == "crossref" and body["sources"][0]["ok"] is True
    assert body["records"]
    assert body["clusters"]
    assert "linked" in body and "elapsed_ms" in body


async def test_search_filters_and_errors(client, crossref_mock):
    r = await client.post("/api/search", json={"query": "x", "categories": ["clinical"]})
    assert r.status_code == 200 and r.json()["sources"] == []
    r = await client.post("/api/search", json={"query": "x", "categories": ["bogus"]})
    assert r.status_code == 400
    r = await client.post("/api/search", json={"query": "x", "sources": ["nope"]})
    assert r.status_code == 404
    r = await client.post("/api/search", json={"query": "   "})
    assert r.status_code == 422


async def test_search_reports_failing_source(client):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(CROSSREF_WORKS).mock(return_value=httpx.Response(400, json={"message": "bad"}))
        r = await client.post("/api/search", json={"query": "x", "sources": ["crossref"]})
    assert r.status_code == 200
    status = r.json()["sources"][0]
    assert status["ok"] is False and status["error"]


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
async def test_catalog_filters(client):
    r = await client.get("/api/catalog", params={"limit": 500})
    assert r.status_code == 200
    assert r.json()["count"] == 232

    r = await client.get("/api/catalog", params={"q": "imaging"})
    body = r.json()
    assert body["count"] > 0 and body["repositories"]

    r = await client.get("/api/catalog", params={"kind": "generalist", "limit": 500})
    repos = r.json()["repositories"]
    assert len(repos) == 9 and all(x["kind"] == "generalist" for x in repos)

    r = await client.get("/api/catalog", params={"access_type": "controlled", "limit": 500})
    assert all("controlled" in x["access_type"] for x in r.json()["repositories"])

    r = await client.get("/api/catalog", params={"institute": "NCI", "limit": 500})
    repos = r.json()["repositories"]
    assert repos and all("NCI" in [i.upper() for i in x["institutes"]] for x in repos)

    r = await client.get("/api/catalog", params={"has_connector": "false", "limit": 500})
    assert all(not x["connectors"] for x in r.json()["repositories"])

    r = await client.get("/api/catalog", params={"limit": 3})
    assert r.json()["returned"] == 3 and len(r.json()["repositories"]) == 3

    r = await client.get("/api/catalog", params={"kind": "weird"})
    assert r.status_code == 422


async def test_catalog_stats(client):
    r = await client.get("/api/catalog/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 232
    assert body["domain_specific"] == 223 and body["generalist"] == 9
    assert "institutes" in body["facets"] and "kind" in body["facets"]


# ---------------------------------------------------------------------------
# Chat (SSE) and sessions
# ---------------------------------------------------------------------------
async def test_chat_streams_sse(client, fake_agent, offline_settings):
    r = await client.post("/api/chat", json={"message": "KRAS G12C landscape"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"].startswith("no-cache")
    assert r.headers["x-accel-buffering"] == "no"
    text = r.text
    assert text.startswith("event: session_started\ndata: ")
    events = parse_sse(text)
    names = [name for name, _ in events]
    assert names[0] == "session_started" and names[-1] == "done"
    for expected in ("thinking_delta", "tool_call_started", "tool_call_finished", "server_tool", "table_saved", "text_delta", "turn_completed"):
        assert expected in names
    for name, data in events:
        assert data["type"] == name
    sid = events[0][1]["session_id"]
    assert all(data["session_id"] == sid for _, data in events)
    saved = next(data for name, data in events if name == "table_saved")
    # the export written by the agent is downloadable through the advertised URL
    csv = await client.get(saved["csv_url"])
    assert csv.status_code == 200 and "sotorasib" in csv.text
    assert fake_agent.calls == [("KRAS G12C landscape", None)]


async def test_chat_continues_session(client, fake_agent):
    first = parse_sse((await client.post("/api/chat", json={"message": "hi"})).text)
    sid = first[0][1]["session_id"]
    second = parse_sse((await client.post("/api/chat", json={"message": "more", "session_id": sid})).text)
    assert second[0][1]["session_id"] == sid
    assert fake_agent.calls[-1] == ("more", sid)


async def test_chat_validation(client):
    assert (await client.post("/api/chat", json={"message": "   "})).status_code == 422
    assert (await client.post("/api/chat", json={})).status_code == 422
    r = await client.post("/api/chat", json={"message": "hi", "session_id": "../etc/passwd"})
    assert r.status_code == 422


async def test_chat_agent_failure_yields_error_and_done(offline_settings, registry):
    app = create_app(offline_settings, registry=registry, agent=FakeAgent(fail=True))
    async with _client(app) as c:
        r = await c.post("/api/chat", json={"message": "boom"})
    events = parse_sse(r.text)
    names = [n for n, _ in events]
    assert names[-2:] == ["error", "done"]
    assert "model exploded" in events[-2][1]["message"]
    assert events[-1][1]["session_id"] == events[0][1]["session_id"]


async def test_chat_heartbeat(offline_settings, registry):
    app = create_app(offline_settings, registry=registry, agent=FakeAgent(delay=0.3), heartbeat_interval=0.05)
    async with _client(app) as c:
        r = await c.post("/api/chat", json={"message": "slow"})
    assert HEARTBEAT in r.text
    assert parse_sse(r.text)[-1][0] == "done"


async def test_sse_stream_stops_when_client_disconnects():
    cancelled = asyncio.Event()

    async def endless():
        yield SessionStarted(session_id="s1", model="m")
        try:
            await asyncio.sleep(3600)
        finally:
            cancelled.set()
        yield Done(session_id="s1", final_text="")  # pragma: no cover

    checks = 0

    async def is_disconnected() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 2

    frames = []
    async for frame in sse_stream(endless, is_disconnected=is_disconnected, heartbeat=0.02):
        frames.append(frame)
    assert frames[0].startswith("event: session_started\n")
    assert HEARTBEAT in frames
    await asyncio.wait_for(cancelled.wait(), 2)


async def test_sessions_endpoints(client):
    events = parse_sse((await client.post("/api/chat", json={"message": "hello there"})).text)
    sid = events[0][1]["session_id"]

    r = await client.get("/api/sessions")
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert [s["session_id"] for s in sessions] == [sid]
    assert sessions[0]["active"] is False

    r = await client.get(f"/api/sessions/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "hello there"
    assert [e["role"] for e in body["transcript"]] == ["user", "assistant"]

    assert (await client.get("/api/sessions/unknown-session")).status_code == 404
    assert (await client.get("/api/sessions/bad%20id")).status_code == 422

    r = await client.delete(f"/api/sessions/{sid}")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert (await client.delete(f"/api/sessions/{sid}")).status_code == 404
    assert (await client.get("/api/sessions")).json()["sessions"] == []


async def test_agent_unavailable_returns_503(no_agent_client, crossref_mock):
    c = no_agent_client
    health = (await c.get("/api/health")).json()
    assert health["agent_available"] is False and health["agent_error"]
    for method, path, body in (
        ("POST", "/api/chat", {"message": "hi"}),
        ("GET", "/api/sessions", None),
        ("GET", "/api/sessions/abc", None),
        ("DELETE", "/api/sessions/abc", None),
    ):
        r = await c.request(method, path, json=body)
        assert r.status_code == 503, path
        assert r.headers["content-type"].startswith("application/json")
        assert r.json()["kind"] == "agent_unavailable"
        assert "unavailable" in r.json()["error"]
    # everything else still works
    assert (await c.get("/api/sources")).status_code == 200
    assert (await c.get("/api/catalog", params={"q": "imaging"})).status_code == 200
    assert (await c.post("/api/search", json={"query": "x"})).status_code == 200
    assert (await c.get("/api/exports")).status_code == 200


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------
async def test_exports_list_and_download(client, offline_settings, registry):
    export_id, columns, rows = write_export_files(offline_settings.exports_dir, registry.audit, name="KRAS trials", session_id="s1")
    r = await client.get("/api/exports")
    assert r.status_code == 200
    items = r.json()["exports"]
    assert len(items) == 1
    item = items[0]
    assert item["export_id"] == export_id
    assert item["name"] == "KRAS trials"
    assert item["n_rows"] == 2 and item["columns"] == columns
    assert item["formats"] == ["csv", "json"]
    assert item["csv_url"] == f"/api/exports/{export_id}.csv"

    r = await client.get(f"/api/exports/{export_id}.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "KRAS_trials.csv" in r.headers["content-disposition"]
    assert r.text.splitlines()[0] == "drug,nct_id,phase"

    r = await client.get(f"/api/exports/{export_id}.json")
    assert r.status_code == 200
    assert r.json()["rows"] == rows


async def test_exports_path_validation(client, offline_settings, tmp_path):
    secret = offline_settings.data_dir / "secret.csv"
    secret.write_text("TOP-SECRET", encoding="utf-8")
    (offline_settings.exports_dir / "notes.csv").write_text("TOP-SECRET", encoding="utf-8")
    attempts = [
        "/api/exports/..%2Fsecret.csv",
        "/api/exports/%2e%2e%2fsecret.csv",
        "/api/exports/..%5Csecret.csv",
        "/api/exports/../secret.csv",
        "/api/exports/notes.csv",
        "/api/exports/secret.csv",
        "/api/exports/" + "A" * 32 + ".csv",
        "/api/exports/" + "a" * 31 + ".csv",
        "/api/exports/" + "a" * 33 + ".csv",
        "/api/exports/" + "a" * 32 + ".txt",
        "/api/exports/" + "a" * 32 + ".csv.json",
        "/api/exports/" + "a" * 32,
        "/api/exports/" + "g" * 32 + ".csv",
        "/api/exports/%00" + "a" * 32 + ".csv",
    ]
    for path in attempts:
        r = await client.get(path)
        assert r.status_code in (400, 404), path
        assert "TOP-SECRET" not in r.text, path
    r = await client.get("/api/exports/" + "a" * 32 + ".csv")
    assert r.status_code == 404
    r = await client.get("/api/exports/notes.csv")
    assert r.status_code == 400

    exports_dir = offline_settings.exports_dir
    assert resolve_export_file(exports_dir, "../" + "a" * 32 + ".csv") is None
    assert resolve_export_file(exports_dir, "a" * 32 + ".csv")[1] == "csv"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
async def test_audit_endpoints(client, crossref_mock, registry):
    await client.post("/api/call", json={"source": "crossref", "tool": "search_works", "params": {"query": "x"}})
    await client.post("/api/call", json={"source": "crossref", "tool": "search_works", "params": {"limit": 1}})
    registry.audit.record_agent_turn(
        session_id="s1", model="fake", stop_reason="end_turn", usage={"input_tokens": 7, "output_tokens": 3}, tool_calls=2
    )

    r = await client.get("/api/audit/tool-calls")
    assert r.status_code == 200
    rows = r.json()["tool_calls"]
    assert len(rows) == 2
    assert {row["ok"] for row in rows} == {True, False}
    assert isinstance(rows[0]["params"], dict)

    r = await client.get("/api/audit/tool-calls", params={"source": "other"})
    assert r.json()["tool_calls"] == []
    r = await client.get("/api/audit/tool-calls", params={"limit": 1})
    assert len(r.json()["tool_calls"]) == 1

    r = await client.get("/api/audit/stats")
    assert r.status_code == 200
    stats = r.json()
    assert stats["tool_calls"]["calls"] == 2 and stats["tool_calls"]["failed"] == 1
    assert stats["agent"]["input_tokens"] == 7
    assert stats["sources"][0]["source"] == "crossref"

    r = await client.get("/api/audit/agent-turns", params={"session_id": "s1"})
    turns = r.json()["agent_turns"]
    assert len(turns) == 1 and turns[0]["tool_calls"] == 2


# ---------------------------------------------------------------------------
# Web UI and request guards
# ---------------------------------------------------------------------------
async def test_web_ui_served(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "/static/app.js" in r.text
    for path, kind in (("/static/app.js", "javascript"), ("/static/styles.css", "text/css")):
        res = await client.get(path)
        assert res.status_code == 200 and kind in res.headers["content-type"]
    assert (await client.get("/static/../../config.py")).status_code == 404


def test_web_ui_has_no_external_dependencies():
    web = Path(__file__).resolve().parent.parent / "mosaic" / "web"
    for name in ("index.html", "app.js", "styles.css"):
        text = (web / name).read_text(encoding="utf-8")
        for needle in ("cdn.", "googleapis", "unpkg", "jsdelivr", "<script src=\"http", "@import url(http"):
            assert needle not in text, (name, needle)
    js = (web / "app.js").read_text(encoding="utf-8")
    for needle in (".innerHTML", "insertAdjacentHTML", ".outerHTML", "document.write"):
        assert needle not in js, needle
    # Export download links are built only from a strictly validated export id.
    assert "const EXPORT_ID_RE = /^[0-9a-f]{32}$/;" in js
    assert "href: e.csv_url" not in js and "href: e.json_url" not in js


async def test_host_and_origin_guards(client):
    r = await client.get("/api/health", headers={"host": "evil.example"})
    assert r.status_code == 400
    r = await client.post(
        "/api/call",
        json={"source": "crossref", "tool": "nope"},
        headers={"origin": "http://evil.example"},
    )
    assert r.status_code == 403
    r = await client.post("/api/chat", json={"message": "hi"}, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403
    # same-origin browser requests are fine
    r = await client.post(
        "/api/call", json={"source": "crossref", "tool": "nope"}, headers={"origin": "http://testserver"}
    )
    assert r.status_code == 400


async def test_cors_off_by_default(client):
    r = await client.options(
        "/api/health", headers={"origin": "http://elsewhere.example", "access-control-request-method": "GET"}
    )
    assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------------------------
# Lifespan: registry creation and agent loading
# ---------------------------------------------------------------------------
def test_lifespan_without_agent_module(offline_settings, monkeypatch):
    broken = types.ModuleType("mosaic.agent")  # no MosaicAgent attribute
    monkeypatch.setitem(sys.modules, "mosaic.agent", broken)
    app = create_app(offline_settings)
    with TestClient(app) as c:
        health = c.get("/api/health").json()
        assert health["agent_available"] is False
        assert "mosaic.agent" in health["agent_error"]
        assert health["sources"] >= 1
        r = c.post("/api/chat", json={"message": "hi"})
        assert r.status_code == 503
    assert app.state.mosaic.registry is None  # closed on shutdown


def test_lifespan_loads_agent(offline_settings, monkeypatch):
    created: list[Any] = []

    class StubAgent(FakeAgent):
        def __init__(self, registry, settings=None, **kwargs):
            super().__init__()
            self.registry = registry
            self.settings = settings
            created.append(self)

    module = types.ModuleType("mosaic.agent")
    module.MosaicAgent = StubAgent
    monkeypatch.setitem(sys.modules, "mosaic.agent", module)
    app = create_app(offline_settings)
    with TestClient(app) as c:
        assert c.get("/api/health").json()["agent_available"] is True
        r = c.post("/api/chat", json={"message": "hi"})
        assert r.status_code == 200
        assert parse_sse(r.text)[-1][0] == "done"
    assert len(created) == 1
    assert created[0].settings is offline_settings
    assert created[0].registry.connectors
    assert created[0].closed is True


def test_lifespan_agent_constructor_failure(offline_settings, monkeypatch):
    class Exploding:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("no model access")

    module = types.ModuleType("mosaic.agent")
    module.MosaicAgent = Exploding
    monkeypatch.setitem(sys.modules, "mosaic.agent", module)
    with TestClient(create_app(offline_settings)) as c:
        health = c.get("/api/health").json()
        assert health["agent_available"] is False
        assert "no model access" in health["agent_error"]
        r = c.get("/api/sessions")
        assert r.status_code == 503 and "no model access" in r.json()["error"]
        assert c.get("/api/sources").status_code == 200


async def test_agent_error_event_passthrough(offline_settings, registry):
    class ErrAgent(FakeAgent):
        async def run(self, user_message, *, session_id=None):
            yield SessionStarted(session_id="s9", model="m")
            yield AgentError(session_id="s9", message="rate limited", retryable=True)
            yield Done(session_id="s9", final_text="")

    app = create_app(offline_settings, registry=registry, agent=ErrAgent())
    async with _client(app) as c:
        events = parse_sse((await c.post("/api/chat", json={"message": "x"})).text)
    assert [n for n, _ in events] == ["session_started", "error", "done"]
    assert events[1][1]["retryable"] is True
