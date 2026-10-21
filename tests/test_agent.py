"""MosaicAgent tests.

Claude is the real ``anthropic`` SDK talking to a scripted ``httpx2.MockTransport``
(tests/agent_fakes.py), so request bodies are exactly what the SDK would send.
Connector HTTP (``httpx``) is mocked with ``respx``.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re

import httpx
import httpx2
import pytest
import respx

from mosaic.agent import MosaicAgent, SessionStore
from mosaic.agent.blocks import echo_content, strip_thinking
from mosaic.agent.exports import csv_cell
from mosaic.agent.meta_tools import MetaResult, ToolContext, default_meta_tools
from mosaic.agent.results import ResultHandles
from mosaic.connectors.base import ToolParams, strip_html
from tests.agent_fakes import (
    FakeClaude,
    collect,
    error_response,
    of_type,
    stream,
    text,
    thinking,
    tool_use,
)
from tests.conftest import load_fixture

API = "https://api.crossref.org"
EXPORT_ID = re.compile(r"^[0-9a-f]{32}$")
META_TOOLS = {
    "describe_source",
    "extract_structured",
    "federated_search",
    "list_sources",
    "save_table",
    "search_repository_catalog",
}


@pytest.fixture
def build_agent(make_registry, offline_settings):
    """``build_agent(fake, **settings_overrides)`` -> MosaicAgent over the crossref connector."""

    def factory(fake: FakeClaude | None, *, sessions: SessionStore | None = None, **overrides) -> MosaicAgent:
        settings = offline_settings.model_copy(update=overrides) if overrides else offline_settings
        registry = make_registry("crossref", settings=settings)
        return MosaicAgent(registry, settings, client=fake.client() if fake else None, sessions=sessions)

    return factory


def _work_item() -> dict:
    return load_fixture("crossref", "search_works.json")["message"]["items"][0]


def _tools_by_name(body: dict) -> dict[str, dict]:
    return {t["name"]: t for t in body["tools"]}


def _strip_cache_markers(blocks: list[dict]) -> list[dict]:
    return [{k: v for k, v in b.items() if k != "cache_control"} for b in blocks]


# ---------------------------------------------------------------- request shape
async def test_text_answer_and_request_shape(build_agent):
    fake = FakeClaude(stream(thinking("Planning the answer."), text("CRISPR **prime editing** was reported in 2019.")))
    agent = build_agent(fake)

    events = await collect(agent, "When was prime editing reported?")

    assert events[0].type == "session_started" and events[0].model == "claude-opus-5-5"
    assert events[-1].type == "done"
    sid = events[0].session_id
    assert "".join(e.text for e in of_type(events, "thinking_delta")) == "Planning the answer."
    streamed = "".join(e.text for e in of_type(events, "text_delta"))
    assert streamed == events[-1].final_text == "CRISPR **prime editing** was reported in 2019."
    assert events[-1].stop_reason == "end_turn"
    assert events[-1].usage == {
        "input_tokens": 1200,
        "output_tokens": 80,
        "cache_creation_input_tokens": 900,
        "cache_read_input_tokens": 300,
    }
    assert [e.stop_reason for e in of_type(events, "turn_completed")] == ["end_turn"]

    body = fake.requests[0]
    assert fake.paths == ["/v1/messages"]
    assert body["model"] == "claude-opus-5-5"
    assert body["max_tokens"] == 64000
    assert body["stream"] is True
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "high"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["fallbacks"] == "default"
    assert fake.headers[0]["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert "tool_choice" not in body
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "When was prime editing reported?"}]}]

    # System prompt: one block with the explicit breakpoint, deterministic content.
    assert len(body["system"]) == 1 and body["system"][0]["cache_control"] == {"type": "ephemeral"}
    prompt = body["system"][0]["text"]
    assert "crossref__search_works" in prompt and "dbGaP" in prompt and "FAERS" in prompt
    assert "Database tools load on demand" in prompt

    # Tools: tool search first, meta tools, web tools, then deferred connector tools.
    tools = body["tools"]
    assert tools[0] == {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}
    by_name = _tools_by_name(body)
    assert META_TOOLS <= set(by_name)
    for name in META_TOOLS:
        assert by_name[name]["eager_input_streaming"] is True
        assert "defer_loading" not in by_name[name]
    assert by_name["web_search"]["type"] == "web_search_20260318"
    assert by_name["web_fetch"]["type"] == "web_fetch_20260318"
    connector_tools = [t for t in tools if "__" in t["name"]]
    assert [t["name"] for t in connector_tools] == ["crossref__get_work", "crossref__search_works"]
    for tool in connector_tools:
        assert tool["defer_loading"] is True and tool["eager_input_streaming"] is True
        assert tool["description"].startswith("[Crossref] ")
        assert tool["input_schema"]["type"] == "object"
    for tool in tools:
        assert "cache_control" not in tool  # a deferred tool may not carry one; caching is top-level
        if "type" in tool:  # server tools never get client-only fields
            assert "eager_input_streaming" not in tool and "defer_loading" not in tool

    turns = agent.registry.audit.recent_agent_turns(session_id=sid)
    assert len(turns) == 1
    assert turns[0]["stop_reason"] == "end_turn" and turns[0]["cache_read_tokens"] == 300
    assert json.loads(turns[0]["detail"])["request_id"] == "req_test"


def test_request_prefix_is_deterministic(build_agent):
    first = build_agent(FakeClaude()).request_prefix()
    second = build_agent(FakeClaude()).request_prefix()
    assert json.dumps(first) == json.dumps(second)
    # nothing volatile in front of the cache breakpoint
    assert not re.search(r"20\d\d-\d\d-\d\d", first["system"][0]["text"])


async def test_eager_loading_without_web_tools_or_fallbacks(build_agent, monkeypatch):
    monkeypatch.setenv("MOSAIC_FALLBACKS", "off")
    fake = FakeClaude(stream(text("ok")))
    agent = build_agent(fake, mosaic_tool_loading="eager", mosaic_enable_web_tools=False, mosaic_effort="medium")

    await collect(agent, "hello")

    body = fake.requests[0]
    names = [t["name"] for t in body["tools"]]
    assert "tool_search_tool_bm25" not in names and "web_search" not in names and "web_fetch" not in names
    assert not any("defer_loading" in t for t in body["tools"])
    assert all(t["eager_input_streaming"] is True for t in body["tools"])
    assert "crossref__search_works" in names
    assert "fallbacks" not in body and "anthropic-beta" not in fake.headers[0]
    assert body["output_config"] == {"effort": "medium"}
    assert "Database tools load on demand" not in body["system"][0]["text"]


# ---------------------------------------------------------------- tool rounds
async def test_parallel_tool_calls_answered_in_one_user_message(build_agent):
    active = {"now": 0, "max": 0}

    def slow(response: httpx.Response, delay: float):
        async def effect(request):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            await asyncio.sleep(delay)
            active["now"] -= 1
            return response

        return effect

    first = stream(
        thinking("I will search Crossref and fetch two DOIs."),
        tool_use("toolu_1", "crossref__search_works", {"query": "prime editing", "limit": 2}),
        tool_use("toolu_2", "crossref__get_work", {"doi": "10.1038/s41586-019-1711-4"}),
        tool_use("toolu_3", "crossref__get_work", {"doi": "10.9999/broken"}),
        stop_reason="tool_use",
    )
    fake = FakeClaude(first, stream(text("Two records found; one DOI failed.")))
    agent = build_agent(fake)

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{API}/works").mock(
            side_effect=slow(httpx.Response(200, json=load_fixture("crossref", "search_works.json")), 0.1)
        )
        router.get(f"{API}/works/10.1038/s41586-019-1711-4").mock(
            side_effect=slow(httpx.Response(200, json={"status": "ok", "message": _work_item()}), 0.3)
        )
        router.get(f"{API}/works/10.9999/broken").mock(return_value=httpx.Response(400, text="bad DOI"))
        events = await collect(agent, "Find prime editing papers")

    assert active["max"] == 2  # both slow calls were in flight together
    started = of_type(events, "tool_call_started")
    assert [e.call_id for e in started] == ["toolu_1", "toolu_2", "toolu_3"]
    assert started[0].source == "crossref" and started[0].input == {"query": "prime editing", "limit": 2}
    finished = {e.call_id: e for e in of_type(events, "tool_call_finished")}
    assert finished["toolu_1"].ok and finished["toolu_1"].summary == "crossref: 2 of 4,321 records"
    assert finished["toolu_1"].total == 4321 and len(finished["toolu_1"].preview) == 2
    assert finished["toolu_2"].ok and finished["toolu_2"].summary == "crossref: 1 record"
    assert not finished["toolu_3"].ok and "HTTP 400" in finished["toolu_3"].error
    assert finished["toolu_1"].result_id != finished["toolu_2"].result_id
    assert finished["toolu_3"].result_id is None

    assert len(fake.requests) == 2
    second = fake.requests[1]
    user, assistant, results = second["messages"]
    assert user["role"] == "user"
    # the assistant turn goes back exactly as streamed, thinking signature included
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == {
        "type": "thinking",
        "thinking": "I will search Crossref and fetch two DOIs.",
        "signature": "sig-abc123",
    }
    assert [b["id"] for b in assistant["content"][1:]] == ["toolu_1", "toolu_2", "toolu_3"]
    assert assistant["content"][1]["input"] == {"query": "prime editing", "limit": 2}
    # all three results, in request order, in ONE user message
    assert results["role"] == "user"
    blocks = results["content"]
    assert [b["tool_use_id"] for b in blocks] == ["toolu_1", "toolu_2", "toolu_3"]
    assert blocks[0]["content"].startswith(f"result_id: {finished['toolu_1'].result_id}\n")
    assert '"total": 4321' in blocks[0]["content"]
    assert "is_error" not in blocks[0] and "is_error" not in blocks[1]
    assert blocks[2]["is_error"] is True and "[crossref] HTTP 400" in blocks[2]["content"]
    # the frozen prefix is byte-identical across requests (prompt cache + preserved thinking)
    assert json.dumps(second["system"]) == json.dumps(fake.requests[0]["system"])
    assert json.dumps(second["tools"]) == json.dumps(fake.requests[0]["tools"])

    sid = events[0].session_id
    calls = agent.registry.audit.recent_tool_calls(session_id=sid)
    assert sorted(c["ok"] for c in calls) == [0, 1, 1]
    assert [e.stop_reason for e in of_type(events, "turn_completed")] == ["tool_use", "end_turn"]
    assert events[-1].final_text == "Two records found; one DOI failed."


async def test_invalid_tool_input_and_unknown_tool_become_error_results(build_agent):
    first = stream(
        tool_use("toolu_a", "crossref__search_works", {"limit": 500}),
        tool_use("toolu_b", "nosuch__tool", {"query": "x"}),
        tool_use("toolu_c", "describe_source", {"source": "nowhere"}),
        stop_reason="tool_use",
    )
    fake = FakeClaude(first, stream(text("Sorry.")))
    agent = build_agent(fake)

    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{API}/works")
        await collect(agent, "search")
        assert not route.called

    results = fake.requests[1]["messages"][-1]["content"]
    assert all(r["is_error"] for r in results)
    assert "invalid parameters for search_works" in results[0]["content"]
    assert "unknown source 'nosuch'" in results[1]["content"]
    assert "unknown source 'nowhere'" in results[2]["content"]


async def test_pause_turn_is_resent_without_an_extra_user_message(build_agent):
    paused = [
        text("Let me search the web."),
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "dbGaP access"}},
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_1",
            "content": [
                {
                    "type": "web_search_result",
                    "url": "https://dbgap.ncbi.nlm.nih.gov/",
                    "title": "dbGaP",
                    "encrypted_content": "EqgfCioIARgBIiQ3YTAw",
                    "page_age": "2026-01-01",
                }
            ],
        },
    ]
    fake = FakeClaude(stream(*paused, stop_reason="pause_turn"), stream(text("Access needs an approved request.")))
    agent = build_agent(fake)

    events = await collect(agent, "How do I access dbGaP data?")

    assert len(fake.requests) == 2
    messages = fake.requests[1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]  # no "continue" message added
    assert messages[1]["content"] == paused  # server-tool blocks echoed exactly (encrypted_content intact)
    activity = of_type(events, "server_tool")
    assert [(a.name, a.phase) for a in activity] == [("web_search", "call"), ("web_search", "result")]
    assert activity[0].detail == "dbGaP access" and activity[1].detail == "1 result(s)"
    assert [e.stop_reason for e in of_type(events, "turn_completed")] == ["pause_turn", "end_turn"]
    assert events[-1].final_text == "Let me search the web.\n\nAccess needs an approved request."


async def test_tool_search_loads_a_deferred_tool_then_it_runs(build_agent):
    searched = [
        {
            "type": "server_tool_use",
            "id": "srvtoolu_ts",
            "name": "tool_search_tool_bm25",
            "input": {"query": "crossref search works"},
        },
        {
            "type": "tool_search_tool_result",
            "tool_use_id": "srvtoolu_ts",
            "content": {
                "type": "tool_search_tool_search_result",
                "tool_references": [{"type": "tool_reference", "tool_name": "crossref__search_works"}],
            },
        },
        tool_use("toolu_1", "crossref__search_works", {"query": "prime editing", "limit": 2}),
    ]
    fake = FakeClaude(stream(*searched, stop_reason="tool_use"), stream(text("Done.")))
    agent = build_agent(fake)

    with respx.mock() as router:
        router.get(f"{API}/works").mock(return_value=httpx.Response(200, json=load_fixture("crossref", "search_works.json")))
        events = await collect(agent, "find prime editing papers")

    activity = of_type(events, "server_tool")
    assert [(a.name, a.phase, a.detail) for a in activity] == [
        ("tool_search", "call", "crossref search works"),
        ("tool_search", "result", "loaded: crossref__search_works"),
    ]
    assert of_type(events, "tool_call_finished")[0].ok
    assert fake.requests[1]["messages"][1]["content"] == searched  # tool-search blocks echoed unchanged


async def test_max_tokens_truncated_tool_use_is_not_run(build_agent, offline_settings):
    truncated = stream(
        text("Saving the table now."),
        # jiter's partial mode turns this into a plausible object: exactly why it must not run
        tool_use("toolu_t", "save_table", partial_json='{"name": "trials", "columns": ["nct"], "rows": [["NCT0'),
        stop_reason="max_tokens",
    )
    fake = FakeClaude(truncated, stream(text("I will save it in smaller pieces.")))
    agent = build_agent(fake)

    events = await collect(agent, "Save a table of trials")

    assert not list(offline_settings.exports_dir.glob("*"))  # nothing was written
    assert not of_type(events, "table_saved")
    finished = of_type(events, "tool_call_finished")
    assert len(finished) == 1 and not finished[0].ok and "truncated" in finished[0].error
    messages = fake.requests[1]["messages"]
    assert messages[1]["content"][1]["name"] == "save_table"
    result = messages[2]["content"][0]
    assert result["tool_use_id"] == "toolu_t" and result["is_error"] is True
    assert "cut off" in result["content"]
    assert events[-1].type == "done" and events[-1].stop_reason == "end_turn"


async def test_unparseable_tool_json_retries_the_turn(build_agent):
    broken = stream(tool_use("toolu_x", "list_sources", partial_json='{"category": ]'), stop_reason="tool_use")
    fake = FakeClaude(broken, stream(text("Recovered.")))
    agent = build_agent(fake)

    events = await collect(agent, "what sources?")

    assert len(fake.requests) == 2
    assert fake.requests[0]["messages"] == fake.requests[1]["messages"]
    assert events[-1].final_text == "Recovered." and not of_type(events, "error")


async def test_max_agent_turns_stops_cleanly(build_agent):
    first = stream(tool_use("toolu_1", "list_sources", {}), stop_reason="tool_use")
    fake = FakeClaude(first)
    agent = build_agent(fake, mosaic_max_agent_turns=1)

    events = await collect(agent, "loop")

    errors = of_type(events, "error")
    assert len(errors) == 1 and "Stopped after 1 model turns" in errors[0].message
    assert events[-1].type == "done" and events[-1].stop_reason == "max_turns"
    history = agent.sessions.messages(events[0].session_id)
    assert [m["role"] for m in history] == ["user", "assistant", "user"]  # valid history to continue from


# ---------------------------------------------------------------- refusal & fallback
async def test_refusal_is_surfaced_nothing_runs_and_the_run_is_rolled_back(build_agent):
    refused = stream(
        thinking("Considering."),
        text("Partial answ"),
        tool_use("toolu_r", "crossref__search_works", {"query": "toxin synthesis"}),
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": "bio", "explanation": "This request was declined."},
        usage={"output_tokens": 12},
    )
    fake = FakeClaude(stream(text("Hello.")), refused, stream(text("Sure.")))
    agent = build_agent(fake)

    first = await collect(agent, "hi")
    sid = first[0].session_id
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{API}/works")
        events = await collect(agent, "a refused question", session_id=sid)
        assert not route.called

    errors = of_type(events, "error")
    assert len(errors) == 1 and "category: bio" in errors[0].message and not errors[0].retryable
    assert events[-1].type == "done" and events[-1].stop_reason == "refusal" and events[-1].final_text == ""
    finished = of_type(events, "tool_call_finished")
    assert len(finished) == 1 and not finished[0].ok
    # the refused run's messages are gone; earlier history is untouched
    history = agent.sessions.messages(sid)
    assert [m["content"][0].get("text") for m in history] == ["hi", "Hello."]

    await collect(agent, "a different question", session_id=sid)
    sent = fake.requests[2]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[-1]["content"][0]["text"] == "a different question"
    transcript = agent.get_transcript(sid)
    assert transcript[3]["text"] == "" and "category: bio" in transcript[3]["error"]


async def test_fallback_turn_is_echoed_by_the_documented_rules(build_agent):
    fallback_block = {
        "type": "fallback",
        "from": {"model": "claude-opus-5-5"},
        "to": {"model": "claude-opus-4-8"},
        "trigger": {"type": "refusal", "category": "cyber"},
    }
    content = [
        thinking("Starting."),
        text("Here is what I found so far. "),
        tool_use("toolu_pre", "crossref__search_works", {"query": "x"}),
        fallback_block,
        text("The fallback model's answer."),
    ]
    fake = FakeClaude(stream(*content, model="claude-opus-4-8"))
    agent = build_agent(fake)

    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{API}/works")
        events = await collect(agent, "question")
        assert not route.called

    stored = agent.sessions.messages(events[0].session_id)[-1]["content"]
    # thinking and the client tool_use before the final fallback block are dropped; the rest stays in place
    assert stored == [text("Here is what I found so far. "), fallback_block, text("The fallback model's answer.")]
    finished = of_type(events, "tool_call_finished")
    assert len(finished) == 1 and finished[0].call_id == "toolu_pre" and not finished[0].ok
    activity = of_type(events, "server_tool")
    assert activity[0].name == "fallback" and "claude-opus-4-8 continued" in activity[0].detail
    turns = agent.registry.audit.recent_agent_turns(session_id=events[0].session_id)
    assert turns[0]["model"] == "claude-opus-4-8"


def test_echo_content_keeps_paired_server_tools_before_the_boundary():
    blocks = [
        {"type": "server_tool_use", "id": "srv_paired", "name": "web_search", "input": {}},
        {"type": "web_search_tool_result", "tool_use_id": "srv_paired", "content": []},
        {"type": "server_tool_use", "id": "srv_orphan", "name": "web_search", "input": {}},
        {"type": "redacted_thinking", "data": "xyz"},
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
        {"type": "thinking", "thinking": "", "signature": "s"},
        {"type": "tool_use", "id": "toolu_after", "name": "list_sources", "input": {}},
    ]
    kept = echo_content(blocks)
    assert [b.get("id") or b["type"] for b in kept] == [
        "srv_paired",
        "web_search_tool_result",
        "fallback",
        "thinking",
        "toolu_after",
    ]
    assert echo_content(blocks[:2]) == blocks[:2]  # no fallback block: unchanged


# ---------------------------------------------------------------- errors
async def test_missing_credentials_reports_not_configured(build_agent, monkeypatch, tmp_path):
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_CONFIG_DIR",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_IDENTITY_TOKEN",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in ("APPDATA", "HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(tmp_path / "no-anthropic-config"))
    agent = build_agent(None, anthropic_api_key=None)

    events = await collect(agent, "hello")

    assert [e.type for e in events] == ["session_started", "error", "done"]
    assert "ANTHROPIC_API_KEY" in events[1].message and not events[1].retryable
    assert events[2].stop_reason == "not_configured"
    transcript = agent.get_transcript(events[0].session_id)
    assert transcript[0]["text"] == "hello" and "ANTHROPIC_API_KEY" in transcript[1]["error"]
    assert agent.sessions.messages(events[0].session_id) == []  # nothing sent, nothing recorded


async def test_authentication_error_maps_to_not_configured(build_agent):
    agent = build_agent(FakeClaude(error_response(401, "authentication_error", "invalid x-api-key")))
    events = await collect(agent, "hello")
    assert [e.type for e in events] == ["session_started", "error", "done"]
    assert "ANTHROPIC_API_KEY" in events[1].message and not events[1].retryable


def _midstream_overload(body: dict) -> httpx2.Response:
    start = stream(text("partial"))
    head = start.split(b"event: content_block_stop")[0]
    payload = head + b'event: error\ndata: {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}\n\n'
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=payload)


def _connection_failure(body: dict) -> httpx2.Response:
    raise httpx2.ConnectError("network unreachable")


@pytest.mark.parametrize(
    ("scripted", "needle", "retryable"),
    [
        (error_response(429, "rate_limit_error", "slow down"), "rate limit", True),
        (error_response(529, "overloaded_error", "Overloaded"), "overloaded", True),
        (error_response(500, "api_error", "boom"), "HTTP 500", True),
        (_midstream_overload, "overloaded", True),
        (_connection_failure, "Could not reach the Claude API", True),
        (error_response(400, "invalid_request_error", "messages: bad shape"), "rejected the request", False),
        (error_response(404, "not_found_error", "model: nope"), "MOSAIC_MODEL", False),
    ],
)
async def test_api_errors_map_to_agent_errors(build_agent, scripted, needle, retryable):
    agent = build_agent(FakeClaude(scripted))
    events = await collect(agent, "hello")
    errors = of_type(events, "error")
    assert len(errors) == 1 and needle in errors[0].message and errors[0].retryable is retryable
    assert events[-1].type == "done" and events[-1].stop_reason == "error"


async def test_connection_lost_mid_stream_is_retryable(build_agent):
    body = stream(text("partial answer"), tool_use("toolu_1", "list_sources", {}), stop_reason="tool_use")
    head = body.split(b"event: message_delta")[0]

    async def chunks():
        yield head
        raise httpx2.ReadError("connection reset")

    def dropped(_: dict) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=chunks())

    agent = build_agent(FakeClaude(dropped))
    events = await collect(agent, "hello")
    errors = of_type(events, "error")
    assert len(errors) == 1 and errors[0].retryable and "connection" in errors[0].message.lower()
    finished = of_type(events, "tool_call_finished")
    assert all(not f.ok for f in finished)
    assert events[-1].type == "done"


async def test_stream_without_stop_reason_is_not_persisted(build_agent):
    body = stream(tool_use("toolu_1", "save_table", {"name": "x", "columns": ["a"], "rows": [["1"]]}))
    truncated = body.split(b"event: message_delta")[0]  # stream closes cleanly but early
    fake = FakeClaude(truncated)
    agent = build_agent(fake)

    events = await collect(agent, "hello")

    errors = of_type(events, "error")
    assert len(errors) == 1 and errors[0].retryable and not of_type(events, "table_saved")
    assert [m["role"] for m in agent.sessions.messages(events[0].session_id)] == ["user"]


async def test_busy_session_is_rejected(build_agent):
    agent = build_agent(FakeClaude())
    agent._running.add("busy-session")
    events = await collect(agent, "hello", session_id="busy-session")
    assert [e.type for e in events] == ["session_started", "error", "done"]
    assert events[1].retryable and events[2].stop_reason == "busy"


async def test_thinking_binding_rejection_strips_thinking_once(build_agent):
    rejection = error_response(
        400,
        "invalid_request_error",
        "messages.1.content.0: Invalid `signature` in `thinking` block. The block is bound to a different conversation.",
    )
    fake = FakeClaude(stream(thinking("first thoughts"), text("First.")), rejection, stream(text("Second.")))
    agent = build_agent(fake)
    sid = (await collect(agent, "one"))[0].session_id

    events = await collect(agent, "two", session_id=sid)

    assert events[-1].final_text == "Second." and not of_type(events, "error")
    assert fake.requests[1]["messages"][1]["content"][0]["type"] == "thinking"
    retried = fake.requests[2]["messages"]
    assert retried[1]["content"] == [text("First.")]
    assert agent.sessions.messages(sid)[1]["content"] == [text("First.")]
    assert strip_thinking([{"role": "assistant", "content": [thinking("x")]}]) == []


# ---------------------------------------------------------------- tables
async def test_save_table_writes_csv_and_json_and_emits_table_saved(build_agent, offline_settings):
    rows = [
        ["aspirin", '=HYPERLINK("http://evil.example")', 3],
        {"drug": "-2+3", "note": "@SUM(A1)", "count": -5},
    ]
    call = tool_use(
        "toolu_s",
        "save_table",
        {
            "name": "Drug notes",
            "columns": ["drug", "note", "count"],
            "rows": rows,
            "description": "test table",
            "derived_from": ["r9"],
        },
    )
    fake = FakeClaude(stream(call, stop_reason="tool_use"), stream(text("Saved.")))
    agent = build_agent(fake)

    events = await collect(agent, "save it")

    saved = of_type(events, "table_saved")
    assert len(saved) == 1
    event = saved[0]
    assert EXPORT_ID.match(event.export_id)
    assert event.csv_url == f"/api/exports/{event.export_id}.csv"
    assert event.json_url == f"/api/exports/{event.export_id}.json"
    assert event.n_rows == 2 and event.columns == ["drug", "note", "count"]

    csv_path = offline_settings.exports_dir / f"{event.export_id}.csv"
    parsed = list(csv.reader(io.StringIO(csv_path.read_text(encoding="utf-8-sig"))))
    assert parsed == [
        ["drug", "note", "count"],
        ["aspirin", "'=HYPERLINK(\"http://evil.example\")", "3"],
        ["'-2+3", "'@SUM(A1)", "-5"],
    ]
    document = json.loads((offline_settings.exports_dir / f"{event.export_id}.json").read_text(encoding="utf-8"))
    assert set(document) == {
        "export_id", "name", "description", "columns", "rows", "created_at", "session_id", "provenance",
    }
    assert document["export_id"] == event.export_id and document["session_id"] == events[0].session_id
    assert document["rows"][0]["note"] == '=HYPERLINK("http://evil.example")'  # JSON keeps raw values
    assert document["rows"][1] == {"drug": "-2+3", "note": "@SUM(A1)", "count": -5}
    assert document["provenance"] == {"method": "rows", "derived_from": ["r9"]}

    exports = agent.registry.audit.recent_exports()
    assert exports[0]["name"] == "Drug notes" and exports[0]["n_rows"] == 2
    assert exports[0]["path"].endswith(f"{event.export_id}.csv")
    finished = of_type(events, "tool_call_finished")[0]
    assert finished.ok and finished.result_id == "r1"
    result = fake.requests[1]["messages"][-1]["content"][0]["content"]
    assert result.startswith("result_id: r1\n") and event.csv_url in result
    assert agent.get_transcript(events[0].session_id)[1]["tables"][0]["export_id"] == event.export_id


def test_csv_cell_neutralizes_formulas_but_not_numbers():
    assert csv_cell("=1+1") == "'=1+1"
    assert csv_cell("+cmd") == "'+cmd"
    assert csv_cell("@x") == "'@x"
    assert csv_cell("\tx") == "'\tx"
    assert csv_cell("-1.5e3") == "-1.5e3"
    assert csv_cell(-5) == "-5"
    assert csv_cell(None) == "" and csv_cell(True) == "true"
    assert csv_cell(["a", "b"]) == '["a", "b"]'


async def test_extract_structured_builds_a_table_with_provenance(build_agent, monkeypatch, offline_settings):
    monkeypatch.setenv("MOSAIC_EXTRACTION_MODEL", "claude-sonnet-5-5")
    extraction_json = {
        "rows": [
            {"ref": "1", "editing_method": "prime editing", "year": 2019, "evidence": "prime editing"},
            {"ref": "2", "editing_method": None, "year": 2023, "evidence": None},
        ]
    }
    fields = [
        {"name": "editing_method", "type": "string", "description": "Genome editing method named in the record"},
        {"name": "year", "type": "integer", "description": "Publication year"},
    ]
    fake = FakeClaude(
        stream(tool_use("toolu_1", "crossref__search_works", {"query": "prime editing", "limit": 2}), stop_reason="tool_use"),
        stream(
            tool_use("toolu_2", "extract_structured", {"result_ids": ["r1"], "fields": fields, "instructions": "Use lowercase."}),
            stop_reason="tool_use",
        ),
        stream(tool_use("toolu_3", "save_table", {"name": "Editing methods", "result_id": "r2"}), stop_reason="tool_use"),
        stream(text("The table is ready.")),
        extraction=(stream(text(json.dumps(extraction_json)), model="claude-sonnet-5-5"),),
    )
    agent = build_agent(fake)

    with respx.mock() as router:
        router.get(f"{API}/works").mock(return_value=httpx.Response(200, json=load_fixture("crossref", "search_works.json")))
        events = await collect(agent, "Tabulate the editing methods")

    assert len(fake.extraction_requests) == 1
    request = fake.extraction_requests[0]
    assert request["model"] == "claude-sonnet-5-5"
    assert request["output_config"]["effort"] == "low"
    schema = request["output_config"]["format"]["schema"]
    assert request["output_config"]["format"]["type"] == "json_schema"
    row_schema = schema["properties"]["rows"]["items"]
    assert row_schema["additionalProperties"] is False
    assert row_schema["required"] == ["ref", "editing_method", "year", "evidence"]
    assert request["fallbacks"] == "default" and "tools" not in request
    prompt = request["messages"][0]["content"][0]["text"]
    full_abstract = strip_html(load_fixture("crossref", "search_works.json")["message"]["items"][0]["abstract"])
    assert full_abstract in prompt  # full records, not the trimmed text Claude saw
    assert "Use lowercase." in prompt and "editing_method (string)" in prompt

    finished = {e.call_id: e for e in of_type(events, "tool_call_finished")}
    assert finished["toolu_2"].ok and finished["toolu_2"].result_id == "r2"
    assert finished["toolu_2"].summary == "extracted 2 field(s) from 2 record(s)"
    extract_result = fake.requests[2]["messages"][-1]["content"][0]["content"]
    assert extract_result.startswith("result_id: r2\n") and "prime editing" in extract_result

    saved = of_type(events, "table_saved")[0]
    assert saved.columns == ["source", "record_id", "title", "editing_method", "year", "url", "evidence"]
    document = json.loads((offline_settings.exports_dir / f"{saved.export_id}.json").read_text(encoding="utf-8"))
    first_row = document["rows"][0]
    assert first_row["source"] == "crossref" and first_row["record_id"] == "10.1038/s41586-019-1711-4"
    assert first_row["url"] == "https://doi.org/10.1038/s41586-019-1711-4"
    assert first_row["editing_method"] == "prime editing" and first_row["year"] == 2019
    assert document["rows"][1]["editing_method"] is None
    provenance = document["provenance"]
    assert provenance["method"] == "extract_structured" and provenance["model"] == "claude-sonnet-5-5"
    assert provenance["result_ids"] == ["r1"] and provenance["result_id"] == "r2"
    assert provenance["rows"][0] == {
        "result_id": "r1",
        "source": "crossref",
        "record_id": "10.1038/s41586-019-1711-4",
        "url": "https://doi.org/10.1038/s41586-019-1711-4",
    }
    extraction_turns = [
        t for t in agent.registry.audit.recent_agent_turns(session_id=events[0].session_id)
        if json.loads(t["detail"]).get("purpose") == "extract_structured"
    ]
    assert len(extraction_turns) == 1 and extraction_turns[0]["model"] == "claude-sonnet-5-5"


async def test_extractor_splits_truncated_chunks_and_reports_refusals():
    from mosaic.agent.extraction import FieldSpec, SourceRecord, StructuredExtractor
    from mosaic.models import Record

    def refs(body: dict) -> list[str]:
        return re.findall(r'\{"ref": "(\d+)"', body["messages"][0]["content"][0]["text"])

    def rows_for(body: dict) -> bytes:
        return stream(text(json.dumps({"rows": [{"ref": r, "phase": f"P{r}", "evidence": None} for r in refs(body)]})))

    def refuse_chunks_with_3(body: dict) -> bytes:
        if "3" in refs(body):
            return stream(stop_reason="refusal", stop_details={"type": "refusal", "category": "bio", "explanation": None})
        return rows_for(body)

    fake = FakeClaude(
        extraction=(
            stream(text('{"rows": [{"ref": "1", "pha'), stop_reason="max_tokens"),  # all 3 records: truncated
            refuse_chunks_with_3,  # halves: [1] and [2, 3]
            refuse_chunks_with_3,
        )
    )
    client = fake.client()
    extractor = StructuredExtractor(lambda: client, model="claude-opus-5-5", effort="medium", fallbacks=False)
    records = [SourceRecord("r1", Record(source="ctgov", id=f"NCT0000000{i}", title=f"trial {i}")) for i in (1, 2, 3)]

    table = await extractor.extract(records, [FieldSpec(name="phase", description="Trial phase")])

    assert len(fake.extraction_requests) == 3
    assert sorted(refs(r) for r in fake.extraction_requests[1:]) == [["1"], ["2", "3"]]
    assert [r["phase"] for r in table.rows] == ["P1", None, None]  # a refused chunk is reported, not split
    assert [r["record_id"] for r in table.rows] == ["NCT00000001", "NCT00000002", "NCT00000003"]
    assert any("2 record(s) not extracted: Claude declined (category: bio)" in n for n in table.notes)
    assert any("2 of 3 row(s) have no extracted values" in n for n in table.notes)
    assert all("fallbacks" not in r and r["output_config"]["effort"] == "medium" for r in fake.extraction_requests)


async def test_federated_search_meta_tool_stores_linked_records(make_registry, offline_settings):
    registry = make_registry("crossref")
    store = SessionStore(None)
    store.create("s1", title="t", model="m", prefix={"system": [], "tools": []})
    handles = ResultHandles(store)
    agent = MosaicAgent(registry, offline_settings, client=None, sessions=store)
    ctx = ToolContext(
        registry=registry, settings=offline_settings, session_id="s1", results=handles,
        extractor=agent._extractor, deferred_tools=False, catalog_factory=lambda: agent.catalog,
    )
    with respx.mock() as router:
        router.get(f"{API}/works").mock(return_value=httpx.Response(200, json=load_fixture("crossref", "search_works.json")))
        result = await default_meta_tools().get("federated_search").run(ctx, {"query": "prime editing", "categories": ["literature"]})

    assert result.kind == "records" and result.n_records == 2
    assert result.summary.startswith("federated: 1/1 sources ok, 2 records")
    stored = handles.add("s1", kind=result.kind, tool="federated_search", payload=result.payload)
    assert [r.id for r in handles.get("s1", stored.result_id).records()] == [
        "10.1038/s41586-019-1711-4",
        "10.1101/2023.01.01.522500",
    ]
    with pytest.raises(Exception, match="invalid parameters for federated_search"):
        await default_meta_tools().get("federated_search").run(ctx, {"query": "x", "categories": ["astrology"]})


# ---------------------------------------------------------------- sessions
async def test_session_persistence_round_trip(build_agent, offline_settings):
    fake1 = FakeClaude(
        stream(
            thinking("Search first."),
            tool_use("toolu_1", "crossref__search_works", {"query": "prime editing", "limit": 2}),
            stop_reason="tool_use",
        ),
        stream(text("Found two works.")),
    )
    agent1 = build_agent(fake1)
    with respx.mock() as router:
        router.get(f"{API}/works").mock(return_value=httpx.Response(200, json=load_fixture("crossref", "search_works.json")))
        first = await collect(agent1, "First question about prime editing")
    sid = first[0].session_id
    history = agent1.sessions.messages(sid)
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]

    # "Restart": a new agent, new store object on the same file, different settings.
    fake2 = FakeClaude(
        stream(tool_use("toolu_9", "save_table", {"name": "Works", "result_id": "r1"}), stop_reason="tool_use"),
        stream(text("Exported.")),
    )
    agent2 = build_agent(fake2, mosaic_enable_web_tools=False)
    second = await collect(agent2, "Export them", session_id=sid)

    sent = fake2.requests[0]
    assert sent["messages"][:4] == history  # exact history, thinking signature included, append-only
    assert sent["messages"][1]["content"][0] == {"type": "thinking", "thinking": "Search first.", "signature": "sig-abc123"}
    assert sent["messages"][4] == {"role": "user", "content": [{"type": "text", "text": "Export them"}]}
    # the session keeps the prefix it started with (web tools stay declared)
    assert json.dumps(sent["tools"]) == json.dumps(fake1.requests[0]["tools"])
    assert json.dumps(sent["system"]) == json.dumps(fake1.requests[0]["system"])
    # result handles survive the restart
    saved = of_type(second, "table_saved")[0]
    assert saved.n_rows == 2 and saved.columns[:2] == ["source", "record_id"]
    assert of_type(second, "tool_call_finished")[0].result_id == "r2"

    sessions = agent2.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == sid and sessions[0]["turns"] == 2
    assert sessions[0]["title"] == "First question about prime editing"
    transcript = agent2.get_transcript(sid)
    assert [(t["role"], t["text"]) for t in transcript] == [
        ("user", "First question about prime editing"),
        ("assistant", "Found two works."),
        ("user", "Export them"),
        ("assistant", "Exported."),
    ]
    assert transcript[1]["tool_calls"][0] == {
        "name": "crossref__search_works",
        "input": {"query": "prime editing", "limit": 2},
        "ok": True,
        "summary": "crossref: 2 of 4,321 records",
        "result_id": "r1",
    }
    assert transcript[3]["tables"][0]["export_id"] == saved.export_id

    assert agent2.delete_session(sid) is True
    assert agent2.list_sessions() == [] and agent2.get_transcript(sid) == []
    assert agent2.delete_session(sid) is False


async def test_invalid_session_id_is_rejected(build_agent):
    agent = build_agent(FakeClaude())
    events = await collect(agent, "hi", session_id="../etc/passwd")
    assert [e.type for e in events] == ["session_started", "error", "done"]
    assert agent.list_sessions() == []


# ---------------------------------------------------------------- meta tools (no Claude)
async def test_meta_tools_list_describe_and_catalog(make_registry, offline_settings):
    registry = make_registry("crossref")
    store = SessionStore(None)
    store.create("s1", title="t", model="m", prefix={"system": [], "tools": []})
    agent = MosaicAgent(registry, offline_settings, client=None, sessions=store)
    ctx = ToolContext(
        registry=registry,
        settings=offline_settings,
        session_id="s1",
        results=ResultHandles(store),
        extractor=agent._extractor,
        deferred_tools=True,
        catalog_factory=lambda: agent.catalog,
    )
    tools = default_meta_tools()

    listed = await tools.get("list_sources").run(ctx, {})
    assert listed.payload["sources"][0]["tools"] == ["crossref__search_works", "crossref__get_work"]

    described = await tools.get("describe_source").run(ctx, {"source": "crossref"})
    assert {t["name"] for t in described.payload["tools"]} == {"crossref__search_works", "crossref__get_work"}
    assert "load on demand" in described.payload["note"]

    catalog = await tools.get("search_repository_catalog").run(ctx, {"query": "genotype phenotype", "limit": 5})
    repos = catalog.payload["repositories"]
    assert catalog.payload["stats"]["total"] == 232 and repos
    assert all("how_to_access" in r and "data_access_url" in r for r in repos)
    controlled = await tools.get("search_repository_catalog").run(ctx, {"access_type": "controlled", "limit": 3})
    assert all("cannot retrieve" in r["how_to_access"] for r in controlled.payload["repositories"])


# ---------------------------------------------------------------- interruption & history validity
class _NoParams(ToolParams):
    pass


def _agent_with_tools(build_agent, fake: FakeClaude, tools) -> MosaicAgent:
    base = build_agent(fake)
    return MosaicAgent(base.registry, base.settings, client=fake.client(), sessions=base.sessions, meta_tools=tools)


async def test_closing_the_run_mid_tools_cancels_them_and_keeps_history_valid(build_agent):
    """A client disconnect closes the event iterator while it is suspended between events."""
    cancelled = asyncio.Event()
    tools = default_meta_tools()

    @tools.tool(description="Returns at once.")
    async def quick(ctx: ToolContext, params: _NoParams) -> MetaResult:
        return MetaResult(text="{}", summary="quick")

    @tools.tool(description="Blocks until cancelled.")
    async def slow(ctx: ToolContext, params: _NoParams) -> MetaResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return MetaResult(text="{}", summary="never")

    first = stream(tool_use("toolu_q", "quick", {}), tool_use("toolu_s", "slow", {}), stop_reason="tool_use")
    fake = FakeClaude(first, stream(text("Fresh answer.")))
    agent = _agent_with_tools(build_agent, fake, tools)

    run = agent.run("go")
    sid = None
    async for event in run:
        sid = sid or event.session_id
        if event.type == "tool_call_finished":
            break  # the consumer stops reading while "slow" is still running
    await run.aclose()

    await asyncio.wait_for(cancelled.wait(), 2)  # the still-running tool is cancelled, not left running
    assert sid not in agent._running
    assert [m["role"] for m in agent.sessions.messages(sid)] == ["user"]  # no half-finished tool round
    assert agent.get_transcript(sid)[-1].get("interrupted") is True

    events = await collect(agent, "again", session_id=sid)
    assert events[-1].final_text == "Fresh answer." and not of_type(events, "error")
    sent = fake.requests[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "user"]


async def test_unanswered_server_tool_call_at_the_end_of_history_is_dropped(build_agent):
    """A paused turn persisted before its continuation failed must not precede a new user message.

    Live API: an assistant ``server_tool_use`` with no result followed by a user message is a 400
    ("tool use with id ... was found without a corresponding web_search_tool_result block").
    """
    paused = [
        text("Searching."),
        {"type": "server_tool_use", "id": "srvtoolu_p", "name": "web_search", "input": {"query": "dbGaP"}},
    ]
    fake = FakeClaude(
        stream(*paused, stop_reason="pause_turn"),
        error_response(529, "overloaded_error", "Overloaded"),
        stream(text("Answer.")),
    )
    agent = build_agent(fake)

    events = await collect(agent, "first")
    sid = events[0].session_id
    assert of_type(events, "error") and [m["role"] for m in agent.sessions.messages(sid)] == ["user", "assistant"]

    events = await collect(agent, "second", session_id=sid)

    assert events[-1].final_text == "Answer." and not of_type(events, "error")
    sent = fake.requests[2]["messages"]
    assert [m["role"] for m in sent] == ["user", "user"]
    assert all(b.get("type") != "server_tool_use" for m in sent for b in m["content"])


def test_echo_content_drops_unrecognized_internal_blocks_before_the_boundary():
    blocks = [
        text("partial "),
        {"type": "some_future_internal_block", "data": "x"},
        {"type": "mcp_tool_use", "id": "mcp_orphan", "name": "q", "server_name": "s", "input": {}},
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
        {"type": "some_future_internal_block", "data": "after"},
    ]
    assert [b["type"] for b in echo_content(blocks)] == ["text", "fallback", "some_future_internal_block"]
