"""Security regression tests: secret scrubbing, the DrugCentral SQL guard, API guards.

Each test pins a defect that was found and fixed:

* upstreams that echo the request back (NCBI answers an invalid key with
  ``{"error":"API key invalid","api-key":"<the key>"}``) leaked the key into
  ``SourceError`` text, i.e. into tool output, audit rows and chat events;
* ``validate_sql`` only understood plain '...' literals, so E'\\'' strings, $$
  dollar quotes, "quoted"(...) function names and U& identifiers hid forbidden
  calls (verified live: ``select "current_setting"('statement_timeout')`` ran);
* the API accepted request bodies of any size and streamed SSE frames unscrubbed.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import respx

from mosaic.agent.events import Done, SessionStarted, TextDelta
from mosaic.api import create_app
from mosaic.api.security import MAX_BODY_BYTES, SecretScrubber
from mosaic.config import Settings
from mosaic.connectors import drugcentral as dc
from mosaic.connectors.drugcentral import DrugCentral, validate_sql
from mosaic.http import HttpClient, SourceError, request_secret_values, scrub_secrets

NCBI_KEY = "0123456789abcdef0123456789abcdef0123"
EMAIL = "researcher@lab.example"
ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"


def _settings(tmp_path, **extra) -> Settings:
    return Settings(
        _env_file=None,
        ncbi_api_key=NCBI_KEY,
        biopha_contact_email=EMAIL,
        biopha_data_dir=tmp_path / "data",
        biopha_audit_db=tmp_path / "data" / "audit.db",
        biopha_cache_db=tmp_path / "data" / "cache.db",
        biopha_cache_ttl=0,
        **extra,
    )


# ---------------------------------------------------------------------------
# mosaic.http: secrets never survive into SourceError / ValueError text
# ---------------------------------------------------------------------------
async def test_upstream_echo_of_api_key_is_scrubbed(tmp_path):
    """NCBI's real response to an invalid key (captured live) repeats the key."""
    body = {"error": "API key invalid", "api-key": NCBI_KEY, "type": "invalid", "status": "unknown"}
    client = HttpClient(_settings(tmp_path))
    try:
        with respx.mock() as mock:
            mock.get(ESEARCH).mock(return_value=httpx.Response(400, json=body))
            with pytest.raises(SourceError) as info:
                await client.request(
                    "ncbi", "GET", ESEARCH, params={"term": "brca1", "api_key": NCBI_KEY, "email": EMAIL}, retries=0
                )
    finally:
        await client.aclose()
    text = str(info.value)
    assert NCBI_KEY not in text and EMAIL not in text
    assert "API key invalid" in text and "api_key=***" in text


async def test_reflected_url_with_encoded_email_and_bearer_token_is_scrubbed(tmp_path):
    token = "zen-" + "t0k3n" * 8
    reflected = f"bad request for /search?q=x&mailto={EMAIL.replace('@', '%40')} (auth Bearer {token})"
    client = HttpClient(_settings(tmp_path))
    try:
        with respx.mock() as mock:
            mock.get("https://api.example.org/search").mock(return_value=httpx.Response(422, text=reflected))
            with pytest.raises(SourceError) as info:
                await client.request(
                    "x", "GET", "https://api.example.org/search", params={"q": "x", "mailto": EMAIL},
                    headers={"Authorization": f"Bearer {token}"}, retries=0,
                )
    finally:
        await client.aclose()
    text = str(info.value)
    assert token not in text and EMAIL not in text and "%40lab.example" not in text


async def test_key_at_snippet_boundary_leaks_no_prefix(tmp_path):
    body = "x" * 290 + NCBI_KEY  # the 300-char snippet would cut the key after 10 chars
    client = HttpClient(_settings(tmp_path))
    try:
        with respx.mock() as mock:
            mock.get(ESEARCH).mock(return_value=httpx.Response(400, text=body))
            with pytest.raises(SourceError) as info:
                await client.request("ncbi", "GET", ESEARCH, params={"api_key": NCBI_KEY}, retries=0)
    finally:
        await client.aclose()
    assert NCBI_KEY[:10] not in str(info.value)


async def test_non_json_success_body_is_scrubbed_in_value_error(tmp_path):
    client = HttpClient(_settings(tmp_path))
    try:
        with respx.mock() as mock:
            mock.get(ESEARCH).mock(return_value=httpx.Response(200, text=f"<html>?api_key={NCBI_KEY}</html>"))
            response = await client.request("ncbi", "GET", ESEARCH, params={"api_key": NCBI_KEY})
    finally:
        await client.aclose()
    with pytest.raises(ValueError) as info:
        response.json()
    assert NCBI_KEY not in str(info.value)


async def test_graphql_error_is_scrubbed(tmp_path):
    client = HttpClient(_settings(tmp_path))
    try:
        with respx.mock() as mock:
            mock.post("https://gql.example.org/").mock(
                return_value=httpx.Response(200, json={"errors": [{"message": f"bad token {NCBI_KEY}"}]})
            )
            with pytest.raises(SourceError) as info:
                await client.graphql("x", "https://gql.example.org/", "{ a }")
    finally:
        await client.aclose()
    assert NCBI_KEY not in str(info.value)


async def test_registry_audit_never_stores_echoed_key(make_registry, tmp_path):
    from mosaic.audit import AuditLog

    registry = make_registry("ncbi", settings=_settings(tmp_path))
    registry.audit = AuditLog(tmp_path / "audit.db")
    body = {"error": "API key invalid", "api-key": NCBI_KEY}
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="eutils.ncbi.nlm.nih.gov").mock(return_value=httpx.Response(400, json=body))
        with pytest.raises(SourceError) as info:
            await registry.call("ncbi", "pubmed_search", {"query": "brca1"}, timeout=10)
    assert "API key invalid" in str(info.value) and NCBI_KEY not in str(info.value)
    rows = registry.audit.recent_tool_calls(limit=10)
    assert rows and all(NCBI_KEY not in json.dumps(row, default=str) for row in rows)
    registry.audit.close()


def test_scrub_helpers():
    assert scrub_secrets("a SECRETVALUE1 b", {"SECRETVALUE1"}) == "a *** b"
    assert scrub_secrets("m=me%40x.org", {"me@x.org"}) == "m=***"
    # short values (DrugCentral's public password "dosage") are not treated as secrets
    assert request_secret_values([("password", "dosage")], None) == set()
    values = request_secret_values([("api_key", NCBI_KEY), ("q", "brca1")], {"X-ELS-APIKey": "k" * 20, "Accept": "json"})
    assert values == {NCBI_KEY, "k" * 20}


def test_api_scrubber_covers_contact_email_and_env_tokens(tmp_path, monkeypatch):
    token = "syn-" + "a" * 30
    monkeypatch.setenv("SYNAPSE_AUTH_TOKEN", token)
    scrub = SecretScrubber(_settings(tmp_path))
    assert scrub(f"{EMAIL} {token} {NCBI_KEY}") == "*** *** ***"
    assert scrub("dosage information") == "dosage information"


# ---------------------------------------------------------------------------
# DrugCentral read-only guard
# ---------------------------------------------------------------------------
Q, DQ, BS = "'", '"', "\\"

BYPASSES = [
    # E'' escape strings: the old scanner ended the literal at \' and hid the call
    f"select E{Q}{BS}{Q}{Q} as a, current_setting({Q}statement_timeout{Q}) as b, {Q}x{Q}",
    f"select e{Q}{BS}{Q}{Q}, pg_read_file({Q}/etc/passwd{Q}), {Q}{Q}",
    # dollar quoting containing a single quote
    f"select $${Q}$$ as a, pg_sleep(20) as b, $${Q}$$ as c",
    "select $tag$x$tag$",
    # quoted identifiers are still function names
    f'select "current_setting"({Q}statement_timeout{Q})',
    f'select pg_catalog."set_config"({Q}statement_timeout{Q}, {Q}0{Q}, false)',
    f'select "pg_read_file"({Q}postgresql.conf{Q})',
    f'select "lo_import"({Q}/etc/passwd{Q})',
    f'select * from "dblink"({Q}host=evil{Q}, {Q}select 1{Q}) as t(x int)',
    # Unicode-escaped identifiers can spell anything
    f'select U&"{BS}0070g_sleep"(20)',
]

REJECTED = [
    "select 1; select 2",
    "select 1 /* hidden */",
    "select 1 -- comment",
    "with d as (delete from structures returning *) select * from d",
    "with u as (update structures set name = 'x' returning *) select * from u",
    "select * from structures for update",
    "copy structures to stdout",
    "select set_config('statement_timeout', '0', false)",
    "select set_config ('statement_timeout', '0', false)",
    "select current_setting('data_directory')",
    "select pg_read_binary_file('pg_hba.conf')",
    "select pg_ls_dir('.')",
    "select lo_get(1)",
    "select query_to_xml('select 1', true, true, '')",
    "select pg_terminate_backend(1)",
    "set statement_timeout = 0",
    "explain analyze select 1",
    "(select 1)",
    "select 1 " + "x" * dc.MAX_SQL_CHARS,
]

ALLOWED = [
    "select name from structures where name ilike '%update%'",
    f"select E{Q}it{BS}{Q}s an update{Q} as a",  # keyword inside an E-string is just text
    "select name from structures where name ~ '^ima.*b$'",  # '$' inside a literal is fine
    'select "comment" from (select 1 as "comment") t',  # quoted identifier named like a keyword
    "select s.name, a.gene from act_table_full a join structures s on s.id = a.struct_id where a.moa = 1",
    "WITH t AS (select 1 as a) select a from t",
]


@pytest.mark.parametrize("sql", BYPASSES + REJECTED)
def test_validate_sql_rejects(sql):
    with pytest.raises(ValueError):
        validate_sql(sql)


@pytest.mark.parametrize("sql", ALLOWED)
def test_validate_sql_allows(sql):
    assert validate_sql(sql) == sql.strip()


async def test_sql_query_tool_rejects_bypasses_without_touching_db(make_registry, monkeypatch):
    calls: list = []

    async def batch(self, statements, max_rows=10_000):
        calls.append(statements)
        return [([], [])]

    monkeypatch.setattr(DrugCentral, "_batch", batch)
    registry = make_registry("drugcentral")
    for sql in BYPASSES:
        result = await registry.call("drugcentral", "sql_query", {"sql": sql})
        assert result.data is None and "query rejected" in result.notes[0], sql
    with pytest.raises(SourceError, match="invalid parameters"):
        await registry.call("drugcentral", "sql_query", {"sql": "select 1 " + "x" * dc.MAX_SQL_CHARS})
    assert calls == []


def test_session_pins_standard_conforming_strings(monkeypatch):
    """The literal scanner assumes backslashes are literal outside E'...'."""
    import psycopg

    executed: list[str] = []

    class Cursor:
        description = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            executed.append(sql)

    class Conn:
        closed = False

        def cursor(self, row_factory=None):
            return Cursor()

        def close(self):
            self.closed = True

    monkeypatch.setattr(psycopg, "connect", lambda **kw: Conn())
    database = dc._Database({"host": "h", "password": "p"}, "drugcentral")
    database.run([("select 1", None)])
    session = [s.lower() for s in executed[:-1]]
    assert "set standard_conforming_strings = on" in session
    assert "set default_transaction_read_only = on" in session
    assert any(s.startswith("set statement_timeout") for s in session)


# ---------------------------------------------------------------------------
# API guards
# ---------------------------------------------------------------------------
class EchoAgent:
    """Agent whose answer repeats a credential (as a model quoting a tool error would)."""

    model = "fake"

    def __init__(self, secret: str):
        self.secret = secret

    async def run(self, message, *, session_id=None):
        sid = session_id or uuid.uuid4().hex
        yield SessionStarted(session_id=sid, model="fake", title="t")
        yield TextDelta(session_id=sid, text=f"the upstream said: api-key {self.secret}\nnext line")
        yield Done(session_id=sid, final_text=self.secret, stop_reason="end_turn", usage={})

    def list_sessions(self, limit=50):
        return []

    def get_transcript(self, session_id):
        raise KeyError(session_id)

    def delete_session(self, session_id):
        return False


@pytest.fixture
async def api(make_registry, tmp_path):
    settings = _settings(tmp_path)
    registry = make_registry("crossref", settings=settings)
    app = create_app(settings, registry=registry, agent=EchoAgent(NCBI_KEY))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        yield client


async def test_sse_frames_are_scrubbed(api):
    r = await api.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 200
    assert NCBI_KEY not in r.text and "***" in r.text
    # newlines in model text stay JSON-escaped inside one data: line
    frames = [f for f in r.text.split("\n\n") if f.strip()]
    assert all(sum(1 for line in f.split("\n") if line.startswith("data:")) == 1 for f in frames)


async def test_oversized_body_is_413(api):
    big = json.dumps({"message": "x" * (MAX_BODY_BYTES + 10)})
    r = await api.post("/api/chat", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert "content-security-policy" in r.headers


async def test_oversized_chunked_body_is_413(api):
    async def chunks():
        for _ in range(3):
            yield b"x" * MAX_BODY_BYTES

    r = await api.post("/api/call", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413


async def test_normal_body_still_accepted(api):
    r = await api.post("/api/call", json={"source": "crossref", "tool": "nope"})
    assert r.status_code == 400  # reached the endpoint


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"host": "evil.example:8000"}, 400),
        ({"host": "127.0.0.1.evil.example"}, 400),
        ({"origin": "null"}, 403),
        ({"origin": "http://testserver.evil.example"}, 403),
        ({"origin": "http://testserver:9999"}, 403),
        ({"sec-fetch-site": "cross-site"}, 403),
    ],
)
async def test_host_and_origin_rejections(api, headers, status):
    r = await api.post("/api/call", json={"source": "crossref", "tool": "nope"}, headers=headers)
    assert r.status_code == status
    assert r.headers["content-security-policy"].startswith("default-src 'self'")


@pytest.mark.parametrize(
    "path",
    [
        "/api/exports/..%2f..%2fpyproject.toml",
        "/api/exports/..%5c..%5cpyproject.toml",
        "/api/exports/" + "A" * 32 + ".csv",
        "/api/exports/" + "a" * 32 + ".csv%00.txt",
        "/api/exports/" + "a" * 32 + ".exe",
    ],
)
async def test_export_paths_rejected(api, path):
    r = await api.get(path)
    assert r.status_code in (400, 404)
    assert "pyproject" not in r.text.lower() or r.json().get("error")


@pytest.mark.parametrize("sid", ["../etc", "a/b", "a\\b", "-x", "x" * 200, "a b"])
async def test_session_ids_validated(api, sid):
    r = await api.post("/api/chat", json={"message": "hi", "session_id": sid})
    assert r.status_code == 422


async def test_health_and_sources_have_no_secrets(api):
    texts = [(await api.get(p)).text for p in ("/api/health", "/api/sources", "/api/tools")]
    joined = "\n".join(texts)
    assert NCBI_KEY not in joined and EMAIL not in joined
