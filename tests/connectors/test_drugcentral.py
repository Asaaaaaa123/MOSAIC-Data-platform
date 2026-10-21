"""DrugCentral connector tests.

Offline tests replace the database layer (``DrugCentral._batch``) with recorded results of the real
queries (tests/fixtures/drugcentral/*.json: a list of batches, each a list of [columns, rows]).
"""

from __future__ import annotations

import pytest

from mosaic.connectors import drugcentral as dc
from mosaic.connectors.drugcentral import DrugCentral, validate_sql
from mosaic.http import SourceError
from tests.conftest import load_fixture


class FakeDB:
    def __init__(self, batches: list):
        self.batches = list(batches)
        self.calls: list[list[tuple[str, dict | None]]] = []

    async def __call__(self, connector, batch, max_rows=10_000):
        self.calls.append(batch)
        if not self.batches:
            return [([], []) for _ in batch]
        recorded = self.batches.pop(0)
        return [(cols, rows) for cols, rows in recorded]


def _patch(monkeypatch, fake: FakeDB) -> None:
    async def batch(self, statements, max_rows=10_000):
        return await fake(self, statements, max_rows)

    monkeypatch.setattr(DrugCentral, "_batch", batch)


@pytest.fixture
def db(monkeypatch):
    def install(fixture: str | list) -> FakeDB:
        batches = load_fixture("drugcentral", fixture) if isinstance(fixture, str) else fixture
        fake = FakeDB(batches)
        _patch(monkeypatch, fake)
        return fake

    return install


async def test_search_drugs_by_brand(make_registry, db):
    fake = db("search_imatinib.json")
    registry = make_registry("drugcentral")
    result = await registry.call("drugcentral", "search_drugs", {"query": "gleevec", "limit": 2})
    sql, params = fake.calls[0][0]
    assert params["q"] == "gleevec" and params["pat"] == "%gleevec%" and params["lim"] == 2 and params["off"] == 0
    assert "from structures s left join synonyms" in sql
    rec = result.records[0]
    assert rec.id == "1423" and rec.title == "imatinib" and rec.type == "drug"
    assert rec.url == "https://drugcentral.org/drugcard/1423"
    ids = rec.identifiers
    assert ids["drugcentral"] == "1423"
    assert ids["inchikey"] == "KTUFNOKKBVMGRW-UHFFFAOYSA-N"
    assert ids["chembl"] == "CHEMBL941"  # parent preferred over the mesylate CHEMBL1642
    assert ids["drugbank"] == "DB00619" and ids["unii"] == "BKJ8M8G5HI" and ids["pubchem_cid"] == "5291"
    assert ids["cas"] == "152459-95-5" and ids["mesh"].startswith("D")
    assert rec.date == "2001-05-10"
    assert rec.attributes["approved"] is True and rec.attributes["match"] == "exact"
    assert result.total == 1 and result.next_cursor is None


async def test_search_escapes_like_and_paginates(make_registry, db):
    rows = load_fixture("drugcentral", "search_imatinib.json")[0][0]
    rows[1][0]["total"] = 5
    fake = db([[rows]])
    registry = make_registry("drugcentral")
    result = await registry.call("drugcentral", "search_drugs", {"query": "50%_x", "limit": 1, "approved_only": True})
    sql, params = fake.calls[0][0]
    assert params["pat"] == "%50\\%\\_x%"
    assert "exists (select 1 from approval" in sql.split("from ranked")[1]
    assert result.next_cursor == "1"


async def test_get_drug_full_profile(make_registry, db):
    fake = db("get_drug_imatinib.json")
    registry = make_registry("drugcentral")
    result = await registry.call("drugcentral", "get_drug", {"id": "db00619", "id_type": "drugbank", "max_items": 5})
    lookup_sql, lookup_params = fake.calls[0][0]
    assert lookup_params == {"t": "DRUGBANK_ID", "v": "db00619"} and "upper(trim(identifier))" in lookup_sql
    assert len(fake.calls[1]) == 9  # one round trip for the whole profile
    rec = result.records[0]
    attrs = rec.attributes
    assert rec.identifiers["atc"] == "L01EA01"
    assert rec.identifiers["smiles"].startswith("CN1CCN")
    assert attrs["moa_targets"][0]["gene"] == "ABL1" and attrs["moa_targets"][0]["uniprot"] == "P00519"
    assert attrs["approvals"][0] == {"date": "2001-05-10", "agency": "FDA", "applicant": "NOVARTIS", "orphan": True}
    assert any("C0023474" in i for i in attrs["indications"])
    assert attrs["contraindications"]
    assert "FDA EPC" in attrs["pharmacologic_classes"]
    assert attrs["atc"][0].startswith("L01EA01 BCR-ABL")
    assert attrs["xrefs"]["chembl"] == ["CHEMBL941", "CHEMBL1642"]
    assert len(attrs["synonyms"]) <= 5


async def test_get_drug_not_found(make_registry, db):
    db([[[["struct_id"], []]]])
    registry = make_registry("drugcentral")
    result = await registry.call("drugcentral", "get_drug", {"id": "CHEMBL0", "id_type": "chembl"})
    assert result.records == [] and "no DrugCentral drug" in result.notes[0]


async def test_drug_targets(make_registry, db):
    fake = db("drug_targets_imatinib.json")
    registry = make_registry("drugcentral")
    result = await registry.call("drugcentral", "drug_targets", {"drug": "imatinib", "limit": 3})
    resolve_sql, resolve_params = fake.calls[0][0]
    assert resolve_params == {"n": "imatinib"}
    sql, params = fake.calls[1][0]
    assert params == {"sid": 1423, "lim": 3} and "organism = 'Homo sapiens'" in sql
    rec = result.records[0]
    assert rec.type == "target" and rec.identifiers == {"uniprot": "P00519", "gene_symbol": "ABL1"}
    assert rec.attributes["moa_target"] is True and rec.attributes["action_type"] == "INHIBITOR"
    assert rec.attributes["best_activity"].startswith("Kd")


async def test_drug_targets_unknown_drug(make_registry, db):
    db([[[["id", "name"], []]]])
    registry = make_registry("drugcentral")
    result = await registry.call("drugcentral", "drug_targets", {"drug": "notadrug"})
    assert result.records == [] and "no DrugCentral drug" in result.notes[0]


async def test_drugs_for_target(make_registry, db):
    fake = db("drugs_for_target_egfr.json")
    registry = make_registry("drugcentral")
    result = await registry.call("drugcentral", "drugs_for_target", {"target": "EGFR", "moa_only": True, "limit": 3})
    sql, params = fake.calls[0][0]
    assert params["t"] == "EGFR" and "upper(a.gene) ~" in sql and "a.moa = 1" in sql
    assert result.total >= 3
    assert all(r.attributes["moa_target"] for r in result.records)
    assert all(r.identifiers.get("drugcentral") for r in result.records)


async def test_drugs_for_target_uniprot_uses_accession(make_registry, db):
    fake = db([[[["id"], []]]])
    registry = make_registry("drugcentral")
    result = await registry.call("drugcentral", "drugs_for_target", {"target": "P00533"})
    assert "upper(a.accession) ~" in fake.calls[0][0][0]
    assert result.records == [] and "no DrugCentral activities" in result.notes[0]


async def test_drugs_for_indication_word_match(make_registry, db):
    fake = db("drugs_for_indication_t2d.json")
    registry = make_registry("drugcentral")
    result = await registry.call("drugcentral", "drugs_for_indication", {"disease": "type 2 diabetes", "limit": 3})
    sql, params = fake.calls[0][0]
    assert params["w0"] == "%type%" and params["w1"] == "%2%" and params["w2"] == "%diabetes%"
    assert params["rel"] == "indication" and "o.relationship_name = %(rel)s" in sql
    names = [r.title for r in result.records]
    assert "metformin" in names
    assert result.records[0].attributes["relation"] == "indication"

    fake2 = db([[[["id"], []]]])
    await registry.call("drugcentral", "drugs_for_indication", {"disease": "C0011860", "relationship": "any"})
    sql2, params2 = fake2.calls[0][0]
    assert params2["d"] == "C0011860" and "umls_cui" in sql2 and "relationship_name = " not in sql2


async def test_adverse_events(make_registry, db):
    fake = db("faers_metformin.json")
    registry = make_registry("drugcentral")
    result = await registry.call("drugcentral", "adverse_events", {"drug": "metformin", "limit": 3})
    sql, params = fake.calls[1][0]
    assert "from faers where" in sql and "llr > llr_threshold" in sql and params == {"sid": 1725, "lim": 3}
    rows = result.data["rows"]
    assert rows[0]["term"] == "Lactic acidosis" and rows[0]["llr"] > rows[0]["llr_threshold"]
    assert result.data["struct_id"] == 1725 and result.total > 3


async def test_describe_schema_table(make_registry, db):
    fake = db("describe_approval.json")
    registry = make_registry("drugcentral")
    result = await registry.call("drugcentral", "describe_schema", {"table": "approval"})
    assert fake.calls[0][0][1] == {"t": "approval"}
    assert {"name": "approval", "type": "date"} in result.data["columns"]
    assert len(result.data["sample_rows"]) == 3
    bad = await registry.call("drugcentral", "describe_schema", {"table": "approval; drop"})
    assert "invalid table name" in bad.notes[0]


async def test_sql_query_wraps_and_caps(make_registry, db):
    fake = db("sql_egfr.json")
    registry = make_registry("drugcentral")
    result = await registry.call(
        "drugcentral",
        "sql_query",
        {"sql": "select s.name, a.action_type from act_table_full a join structures s on s.id = a.struct_id "
                "where a.gene = 'EGFR' and a.moa = 1", "max_rows": 3},
    )
    sql, params = fake.calls[0][0]
    assert sql.startswith("select * from (select s.name") and sql.endswith(") q limit 4") and params is None
    assert result.data["columns"] == ["name", "action_type"]
    assert len(result.data["rows"]) == 3 and any("row cap" in n for n in result.notes)


async def test_sql_query_rejections_never_hit_db(make_registry, db):
    fake = db([])
    registry = make_registry("drugcentral")
    for sql in (
        "select 1; drop table structures",
        "delete from structures",
        "select pg_sleep(30)",
        "select * from dblink('host=evil', 'select 1') as t(x int)",
        "select lo_import('/etc/passwd')",
        "select pg_read_file('postgresql.conf')",
        "with x as (select 1) insert into t select * from x",
        "select * into newtable from structures",
        "copy structures to stdout",
        "select 1 -- comment",
        "select set_config('statement_timeout', '0', false)",
    ):
        result = await registry.call("drugcentral", "sql_query", {"sql": sql})
        assert result.data is None and "query rejected" in result.notes[0], sql
    assert fake.calls == []


def test_validate_sql_allows_keywords_inside_literals():
    assert validate_sql("select name from structures where name ilike '%update%'")
    assert validate_sql("WITH t AS (select 1 as a) select a from t")
    assert validate_sql("select act_comment from act_table_full")  # 'comment' only as part of a name


async def test_sql_query_error_becomes_note(make_registry, monkeypatch):
    async def failing(self, statements, max_rows=10_000):
        raise SourceError("drugcentral", 'SQL error: column "nosuchcol" does not exist')

    monkeypatch.setattr(DrugCentral, "_batch", failing)
    registry = make_registry("drugcentral")
    result = await registry.call("drugcentral", "sql_query", {"sql": "select nosuchcol from structures"})
    assert "nosuchcol" in result.notes[0]


def test_database_reconnects_once_and_scrubs_password(monkeypatch):
    import psycopg

    attempts = {"n": 0}

    class FakeCursor:
        description = [type("D", (), {"name": "x"})()]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            pass

        def fetchmany(self, n):
            return [{"x": 1}]

    class FakeConn:
        closed = False

        def cursor(self, row_factory=None):
            return FakeCursor()

        def close(self):
            self.closed = True

    def connect(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise psycopg.OperationalError("server closed the connection (password=s3cret)")
        return FakeConn()

    monkeypatch.setattr(psycopg, "connect", connect)
    database = dc._Database({"host": "h", "password": "s3cret"}, "drugcentral")
    assert database.run([("select 1 as x", None)]) == [(["x"], [{"x": 1}])]
    assert attempts["n"] == 2

    def always_fail(**kwargs):
        raise psycopg.OperationalError("password authentication failed: s3cret")

    monkeypatch.setattr(psycopg, "connect", always_fail)
    database.close()
    with pytest.raises(SourceError) as info:
        database.run([("select 1", None)])
    assert "s3cret" not in str(info.value) and info.value.retryable


async def test_aclose_closes_connection(make_registry):
    registry = make_registry("drugcentral")
    connector = registry.get("drugcentral")
    closed = []
    connector._db = type("DB", (), {"close": lambda self: closed.append(True)})()
    await connector.aclose()
    assert closed == [True]


# ------------------------------------------------------------------ live
@pytest.mark.live
async def test_live_all_tools(live_registry):
    registry = live_registry("drugcentral")
    found = await registry.call("drugcentral", "search_drugs", {"query": "imatinib", "limit": 3})
    assert found.records[0].identifiers["drugcentral"] == "1423"

    drug = await registry.call("drugcentral", "get_drug", {"id": "CHEMBL941", "id_type": "chembl"})
    assert drug.records[0].attributes["moa_targets"]

    targets = await registry.call("drugcentral", "drug_targets", {"drug": "1423", "moa_only": True})
    assert any(r.identifiers.get("gene_symbol") == "ABL1" for r in targets.records)

    by_target = await registry.call("drugcentral", "drugs_for_target", {"target": "P00533", "moa_only": True})
    assert any(r.title == "erlotinib" for r in by_target.records)

    by_disease = await registry.call("drugcentral", "drugs_for_indication", {"disease": "DOID:9352"})
    assert any(r.title == "metformin" for r in by_disease.records)

    faers = await registry.call("drugcentral", "adverse_events", {"drug": "metformin", "limit": 5})
    assert faers.data["rows"]

    schema = await registry.call("drugcentral", "describe_schema", {})
    assert any(t["table"] == "act_table_full" for t in schema.data["tables"])

    rows = await registry.call("drugcentral", "sql_query", {"sql": "select count(*) as n from structures"})
    assert rows.data["rows"][0][0] > 4000
