"""DrugCentral connector (public read-only PostgreSQL, no REST API).

Docs: https://drugcentral.org/download (schema), https://drugcentral.org/
Auth: database credentials from settings ``DRUGCENTRAL_PG_HOST/PORT/DBNAME/USER/PASSWORD`` (DrugCentral
publishes public read-only credentials; the password is never logged or returned).
Rate limit: none documented; queries are serialized over one connection.

Implementation notes:
- psycopg's async mode does not work with the Windows Proactor event loop (used by uvicorn), so we
  use synchronous psycopg 3 inside ``asyncio.to_thread`` with one lazily created connection guarded
  by a lock, ``connect_timeout`` 10 s, ``statement_timeout`` 15 s, read-only session, and a
  reconnect-once on connection failure. ``aclose`` closes it.
- ``sql_query`` runs user SQL only after validation (single SELECT/WITH, no semicolons/comments,
  no DDL/DML/COPY/SET, no dangerous functions) wrapped as ``SELECT * FROM (<sql>) q LIMIT n``.

Key tables: structures (id = struct_id), synonyms, identifier (id_type: ChEMBL_ID, PUBCHEM_CID, UNII,
DRUGBANK_ID, RXNORM, MESH_DESCRIPTOR_UI, KEGG_DRUG, CHEBI, UMLSCUI, IUPHAR_LIGAND_ID ...),
act_table_full (target activities, moa=1 marks the mechanism-of-action target), omop_relationship(_doid_view)
(indication / contraindication / off-label use with UMLS, SNOMED, DOID), approval, struct2atc + atc,
pharma_class (FDA EPC/MoA/PE/CS, MeSH PA, ChEBI roles), faers (+ faers_female/_male/_ped/_ger: FAERS
likelihood-ratio signals).

Identifiers: ``drugcentral`` (struct_id), ``inchikey``, ``cas``, ``chembl``, ``pubchem_cid``, ``unii``,
``drugbank``, ``rxcui`` (DrugCentral's mapping is sometimes a brand-level RxCUI), ``mesh``, ``chebi``
(non-canonical key), ``kegg_drug`` (non-canonical), ``umls``; targets ``uniprot`` + ``gene_symbol``;
diseases ``umls``, ``snomed`` (non-canonical), ``doid``.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import decimal
import re
import threading
from typing import Any, Literal

from pydantic import Field

from ..http import SourceError
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, tool

DRUGCARD = "https://drugcentral.org/drugcard/"
MAX_SQL_ROWS = 500

_STATUS = {"OFP": "approved, off patent", "ONP": "approved, on patent", "OFM": "off market"}

# identifier.id_type -> canonical identifier key
_XREF_KEYS = {
    "ChEMBL_ID": "chembl",
    "PUBCHEM_CID": "pubchem_cid",
    "UNII": "unii",
    "DRUGBANK_ID": "drugbank",
    "RXNORM": "rxcui",
    "MESH_DESCRIPTOR_UI": "mesh",
    "MESH_SUPPLEMENTAL_RECORD_UI": "mesh_supplemental",
    "CHEBI": "chebi",
    "KEGG_DRUG": "kegg_drug",
    "UMLSCUI": "umls",
    "IUPHAR_LIGAND_ID": "iuphar_ligand",
    "INN_ID": "inn",
    "NUI": "nui",
    "PDB_CHEM_ID": "pdb_ligand",
    "SNOMEDCT_US": "snomed",
    "VUID": "vuid",
}
_LINK_XREFS = ("chembl", "pubchem_cid", "unii", "drugbank", "rxcui", "mesh", "chebi", "kegg_drug", "umls")

_LOOKUP_ID_TYPES = {
    "chembl": "ChEMBL_ID",
    "pubchem_cid": "PUBCHEM_CID",
    "unii": "UNII",
    "drugbank": "DRUGBANK_ID",
    "rxcui": "RXNORM",
    "mesh": "MESH_DESCRIPTOR_UI",
    "kegg_drug": "KEGG_DRUG",
    "chebi": "CHEBI",
    "umls": "UMLSCUI",
    "iuphar": "IUPHAR_LIGAND_ID",
}

_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|merge|upsert|drop|create|alter|truncate|grant|revoke|copy|vacuum|analyze|"
    r"cluster|reindex|comment|lock|listen|unlisten|notify|call|do|execute|prepare|deallocate|set|reset|"
    r"refresh|security|load|import|discard|into|checkpoint|begin|commit|rollback|savepoint|release)\b",
    re.IGNORECASE,
)
_FORBIDDEN_FUNCS = re.compile(
    r"\b(pg_sleep\w*|dblink\w*|lo_\w+|pg_read_\w+|pg_ls_\w+|pg_stat_file|pg_file_\w+|pg_terminate_backend|"
    r"pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|set_config|pg_advisory\w*|pg_try_advisory\w*|"
    r"query_to_xml\w*|table_to_xml\w*|cursor_to_xml\w*|database_to_xml\w*|schema_to_xml\w*|"
    r"query_to_json\w*|pg_notify|pg_logical\w*|pg_create\w*|pg_drop\w*|pg_promote|pg_switch_wal|"
    r"pg_import_system_collations|txid_\w+|pg_current_\w+|current_setting|pg_backend_pid)\s*\(",
    re.IGNORECASE,
)


def validate_sql(sql: str) -> str:
    """Return the cleaned statement or raise ValueError explaining why it is not allowed."""
    text = (sql or "").strip()
    if not text:
        raise ValueError("empty SQL")
    if ";" in text:
        raise ValueError("semicolons are not allowed: send a single SELECT statement without ';'")
    if "--" in text or "/*" in text:
        raise ValueError("SQL comments are not allowed")
    if not re.match(r"^(select|with)\b", text, re.IGNORECASE):
        raise ValueError("only SELECT or WITH ... SELECT statements are allowed")
    # ignore keywords inside string literals when checking
    stripped = re.sub(r"'(?:[^']|'')*'", "''", text)
    bad = _FORBIDDEN_SQL.search(stripped)
    if bad:
        raise ValueError(f"keyword {bad.group(1).upper()!r} is not allowed in read-only queries")
    func = _FORBIDDEN_FUNCS.search(stripped)
    if func:
        raise ValueError(f"function {func.group(1)!r} is not allowed")
    return text


# ---------------------------------------------------------------- params
class SearchDrugsParams(SearchParams):
    query: str = Field(
        description="Drug name, synonym, brand or code (e.g. 'imatinib', 'Gleevec', 'STI-571', 'CHEMBL941', "
        "'DB00619', InChIKey or CAS number)"
    )
    approved_only: bool = Field(False, description="Only drugs with at least one regulatory approval record")


class GetDrugParams(ToolParams):
    id: str = Field(description="Identifier value (DrugCentral struct_id by default), e.g. 1423, CHEMBL941, DB00619")
    id_type: Literal[
        "drugcentral", "name", "chembl", "pubchem_cid", "unii", "drugbank", "rxcui", "inchikey", "cas", "mesh",
        "kegg_drug", "chebi", "umls", "iuphar",
    ] = Field("drugcentral", description="Type of `id`")
    max_items: int = Field(25, ge=5, le=200, description="Maximum entries per list (indications, targets, ...)")


class DrugRefParams(ToolParams):
    drug: str = Field(description="DrugCentral struct_id (digits) or exact drug name / synonym, e.g. 'imatinib'")


class DrugTargetsParams(DrugRefParams):
    moa_only: bool = Field(False, description="Only mechanism-of-action targets (moa=1)")
    human_only: bool = Field(True, description="Only Homo sapiens targets")
    limit: int = Field(50, ge=1, le=500, description="Maximum targets")


class DrugsForTargetParams(ToolParams):
    target: str = Field(description="Gene symbol (e.g. 'EGFR', 'ABL1') or UniProt accession (e.g. P00533)")
    moa_only: bool = Field(False, description="Only drugs whose mechanism of action is this target")
    approved_only: bool = Field(False, description="Only drugs with an approval record")
    limit: int = Field(50, ge=1, le=500, description="Maximum drugs")


class DrugsForIndicationParams(ToolParams):
    disease: str = Field(
        description="Disease/condition name (substring, e.g. 'type 2 diabetes'), UMLS CUI (C0011860), "
        "SNOMED CT concept id (44054006) or DOID (DOID:9352)"
    )
    relationship: Literal["indication", "contraindication", "off-label use", "any"] = Field(
        "indication", description="Drug-disease relation to match"
    )
    limit: int = Field(50, ge=1, le=500, description="Maximum drugs")


class AdverseEventsParams(DrugRefParams):
    population: Literal["all", "female", "male", "pediatric", "geriatric"] = Field(
        "all", description="FAERS subpopulation table"
    )
    signals_only: bool = Field(True, description="Only terms whose log-likelihood ratio exceeds the drug's threshold")
    limit: int = Field(30, ge=1, le=500, description="Maximum MedDRA terms")


class DescribeSchemaParams(ToolParams):
    table: str | None = Field(None, description="Table or view name for full column detail; omit to list all tables")


class SqlQueryParams(ToolParams):
    sql: str = Field(
        description="A single read-only SELECT (or WITH ... SELECT) statement without ';' or comments, e.g. "
        "\"select s.name, a.gene, a.act_value from structures s join act_table_full a on a.struct_id = s.id "
        "where a.gene = 'EGFR' and a.moa = 1\""
    )
    max_rows: int = Field(100, ge=1, le=MAX_SQL_ROWS, description="Row cap (applied as an outer LIMIT)")


# ---------------------------------------------------------------- database access
class _Database:
    """One lazily created synchronous psycopg connection, used from worker threads under a lock."""

    def __init__(self, conninfo: dict[str, Any], source: str):
        self._conninfo = conninfo
        self._source = source
        self._conn: Any = None
        self._lock = threading.Lock()

    def _connect(self) -> Any:
        import psycopg

        if self._conn is not None and not self._conn.closed:
            return self._conn
        conn = psycopg.connect(**self._conninfo, connect_timeout=10, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 15000")
            cur.execute("SET default_transaction_read_only = on")
        self._conn = conn
        return conn

    def _scrub(self, message: str) -> str:
        password = str(self._conninfo.get("password") or "")
        if password:
            message = message.replace(password, "***")
        return message.strip().splitlines()[0][:300] if message.strip() else "database error"

    def run(self, batch: list[tuple[str, dict[str, Any] | None]], max_rows: int = 10_000) -> list[tuple[list[str], list[dict]]]:
        """Run several statements on one connection; returns [(columns, rows-as-dicts)] per statement."""
        import psycopg
        from psycopg.rows import dict_row

        with self._lock:
            for attempt in (1, 2):
                try:
                    conn = self._connect()
                    results = []
                    with conn.cursor(row_factory=dict_row) as cur:
                        for sql, params in batch:
                            cur.execute(sql, params)
                            cols = [d.name for d in cur.description or []]
                            results.append((cols, cur.fetchmany(max_rows) if cur.description else []))
                    return results
                except psycopg.errors.QueryCanceled as exc:
                    raise SourceError(self._source, "query exceeded the 15 s statement timeout") from exc
                except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                    self.close_locked()
                    if attempt == 2:
                        raise SourceError(
                            self._source, f"database connection failed: {self._scrub(str(exc))}", retryable=True
                        ) from exc
                except psycopg.Error as exc:
                    raise SourceError(self._source, f"SQL error: {self._scrub(str(exc))}") from exc
        raise SourceError(self._source, "database unavailable")  # pragma: no cover

    def close_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def close(self) -> None:
        with self._lock:
            self.close_locked()


def _jsonable(value: Any, max_chars: int = 500) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary {len(bytes(value))} bytes>"
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + " ..."
    if isinstance(value, (list, tuple)):
        return [_jsonable(v, max_chars) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v, max_chars) for k, v in value.items()}
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)[:max_chars]


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, [], {}, "")}


# ---------------------------------------------------------------- SQL fragments
_XREF_SQL = """
(select jsonb_object_agg(x.id_type, x.ids) from (
    select i.id_type, array_agg(distinct i.identifier order by i.identifier) as ids
    from identifier i where i.struct_id = s.id group by i.id_type) x) as xrefs
"""
_APPROVED_SQL = "exists (select 1 from approval ap where ap.struct_id = s.id)"
_DRUG_COLS = f"""
s.id, s.name, s.cas_reg_no, s.inchikey, s.status, s.stem, s.cd_formula, s.cd_molweight, s.no_formulations,
s.mrdef, (select min(ap.approval) from approval ap where ap.struct_id = s.id and ap.type = 'FDA') as fda_approval,
{_APPROVED_SQL} as approved, {_XREF_SQL}
"""


def _xref_ids(xrefs: dict[str, list[str]] | None) -> tuple[dict[str, Any], dict[str, list[str]]]:
    identifiers: dict[str, Any] = {}
    all_xrefs: dict[str, list[str]] = {}
    for id_type, values in (xrefs or {}).items():
        key = _XREF_KEYS.get(id_type)
        values = [str(v).strip() for v in values or [] if str(v).strip()]
        if not key or not values:
            continue
        if key in ("chembl", "pubchem_cid"):
            # salts/forms get later registrations: the numerically smallest id is usually the parent
            values.sort(key=lambda v: (int(re.sub(r"\D", "", v) or 0), v))
        all_xrefs[key] = values
        if key in _LINK_XREFS:
            identifiers[key] = values[0]
    return identifiers, all_xrefs


def _drug_record(row: dict[str, Any], **extra: Any) -> Record:
    struct_id = row.get("id")
    identifiers, xrefs = _xref_ids(row.get("xrefs"))
    identifiers.update(
        {"drugcentral": struct_id, "inchikey": row.get("inchikey"), "cas": row.get("cas_reg_no")}
    )
    multi = {k: v for k, v in xrefs.items() if len(v) > 1 and k in _LINK_XREFS}
    status = row.get("status")
    attributes = {
        "approval_status": _STATUS.get(status or "", None) if status else None,
        "approved": row.get("approved"),
        "first_fda_approval": _jsonable(row.get("fda_approval")),
        "formula": row.get("cd_formula") or None,
        "mol_weight": _jsonable(row.get("cd_molweight")),
        "inn_stem": row.get("stem"),
        "formulations": row.get("no_formulations"),
        "other_ids": multi or None,
        **extra,
    }
    return Record(
        source="drugcentral",
        id=str(struct_id),
        type="drug",
        title=row.get("name"),
        url=f"{DRUGCARD}{struct_id}",
        identifiers=identifiers,
        date=_jsonable(row.get("fda_approval")),
        summary=row.get("mrdef") or None,
        attributes=_compact(attributes),
    )


# ---------------------------------------------------------------- connector
class DrugCentral(Connector):
    name = "drugcentral"
    title = "DrugCentral"
    category = "drugs"
    description = (
        "Curated database of ~4,900 active pharmaceutical ingredients (approved drugs worldwide) with "
        "mechanism-of-action targets and bioactivities, indications/contraindications (UMLS, SNOMED, DOID), "
        "ATC and pharmacologic classes, approvals, FAERS signals and rich cross-references. Best for drug "
        "MoA, drug-target and drug-indication questions; supports read-only SQL."
    )
    homepage = "https://drugcentral.org/"
    docs_url = "https://drugcentral.org/download"
    auth = "database"
    required_settings = ("drugcentral_pg_host", "drugcentral_pg_dbname", "drugcentral_pg_user", "drugcentral_pg_password")
    rate_limit = 5.0
    health_query = "imatinib"

    def __init__(self, settings, http):
        super().__init__(settings, http)
        self._db: _Database | None = None
        self._schema_cache: dict[str, Any] | None = None

    def _database(self) -> _Database:
        if self._db is None:
            self._db = _Database(
                {
                    "host": self.setting("drugcentral_pg_host"),
                    "port": int(self.setting("drugcentral_pg_port") or 5432),
                    "dbname": self.setting("drugcentral_pg_dbname"),
                    "user": self.setting("drugcentral_pg_user"),
                    "password": self.setting("drugcentral_pg_password"),
                },
                self.name,
            )
        return self._db

    async def _query(self, sql: str, params: dict[str, Any] | None = None, max_rows: int = 10_000) -> list[dict]:
        return (await self._batch([(sql, params)], max_rows))[0][1]

    async def _batch(
        self, batch: list[tuple[str, dict[str, Any] | None]], max_rows: int = 10_000
    ) -> list[tuple[list[str], list[dict]]]:
        return await asyncio.to_thread(self._database().run, batch, max_rows)

    async def aclose(self) -> None:
        if self._db is not None:
            await asyncio.to_thread(self._db.close)

    async def _resolve(self, drug: str) -> tuple[int | None, str | None]:
        """struct_id for a struct_id string or exact name/synonym (case-insensitive)."""
        value = drug.strip()
        if value.isdigit():
            rows = await self._query("select id, name from structures where id = %(id)s", {"id": int(value)})
        else:
            rows = await self._query(
                """
                select s.id, s.name from structures s
                where lower(s.name) = lower(%(n)s)
                   or s.id in (select y.id from synonyms y where y.lname = lower(%(n)s))
                order by (lower(s.name) = lower(%(n)s)) desc, s.no_formulations desc nulls last limit 1
                """,
                {"n": value},
            )
        return (rows[0]["id"], rows[0]["name"]) if rows else (None, None)

    # ---- tools
    @tool(
        description="""
        Search DrugCentral drugs (active ingredients) by name, synonym, brand name or code
        (ChEMBL, DrugBank, UNII, PubChem CID, InChIKey, CAS). Exact name/synonym/code matches rank
        first, then prefix and substring matches. Records carry identifiers drugcentral (struct_id),
        inchikey, cas, chembl, pubchem_cid, unii, drugbank, rxcui, mesh, and attributes approval
        status, first FDA approval, formula. Use get_drug for the full profile (targets,
        indications, ATC, classes) and the struct_id with other DrugCentral tools.
        """,
        federated=True,
    )
    async def search_drugs(self, params: SearchDrugsParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        q = params.query.strip()
        sql = f"""
        with hits as (
            select s.id,
                   min(case
                         when upper(s.inchikey) = upper(%(q)s) or s.cas_reg_no = %(q)s then 0
                         when lower(s.name) = lower(%(q)s) then 0
                         when y.lname = lower(%(q)s) then 1
                         when lower(s.name) like lower(%(prefix)s) then 2
                         when y.lname like lower(%(prefix)s) then 3
                         else 4 end) as rank
            from structures s left join synonyms y on y.id = s.id
            where lower(s.name) like lower(%(pat)s) or y.lname like lower(%(pat)s)
               or upper(s.inchikey) = upper(%(q)s) or s.cas_reg_no = %(q)s
            group by s.id
            union all
            select i.struct_id, 0 from identifier i where upper(i.identifier) = upper(%(q)s)
        ), ranked as (
            select id, min(rank) as rank from hits group by id
        )
        select {_DRUG_COLS}, r.rank, count(*) over () as total
        from ranked r join structures s on s.id = r.id
        {"where " + _APPROVED_SQL if params.approved_only else ""}
        order by r.rank, s.no_formulations desc nulls last, s.name
        limit %(lim)s offset %(off)s
        """
        like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = await self._query(
            sql, {"q": q, "pat": f"%{like}%", "prefix": f"{like}%", "lim": params.limit, "off": offset}
        )
        total = int(rows[0]["total"]) if rows else 0
        nxt = offset + len(rows)
        return ToolResult(
            source=self.name,
            tool="search_drugs",
            records=[_drug_record(r, match="exact" if r["rank"] <= 1 else "partial") for r in rows],
            total=total if rows or offset == 0 else None,
            next_cursor=str(nxt) if rows and nxt < total else None,
            notes=[] if rows else [f"no DrugCentral drugs matched {q!r}"],
        )

    @tool(
        description="""
        Full DrugCentral drug profile by struct_id, name or external id (id_type: chembl,
        pubchem_cid, unii, drugbank, rxcui, inchikey, cas, mesh, kegg_drug, chebi, umls, iuphar).
        Returns all cross-references as identifiers/attributes, synonyms, approvals (FDA/EMA/PMDA
        dates, applicants, orphan flag), ATC codes with level names, indications, contraindications
        and off-label uses (with UMLS/SNOMED/DOID), mechanism-of-action targets (gene, UniProt,
        action type), FDA EPC/MoA/PE and MeSH pharmacologic classes, top FAERS signals, SMILES and
        physchem properties. Use for comprehensive drug annotation and cross-database linking.
        """
    )
    async def get_drug(self, params: GetDrugParams) -> ToolResult:
        value = params.id.strip()
        if params.id_type == "drugcentral":
            if not re.sub(r"\D", "", value):
                return ToolResult(source=self.name, tool="get_drug", notes=[f"{value!r} is not a struct_id"])
            ids = [int(re.sub(r"\D", "", value))]
        elif params.id_type == "name":
            sid, _ = await self._resolve(value)
            ids = [sid] if sid else []
        elif params.id_type == "inchikey":
            ids = [r["id"] for r in await self._query(
                "select id from structures where upper(inchikey) = upper(%(v)s)", {"v": value})]
        elif params.id_type == "cas":
            ids = [r["id"] for r in await self._query(
                "select id from structures where cas_reg_no = %(v)s union "
                "select struct_id from identifier where id_type = 'SECONDARY_CAS_RN' and identifier = %(v)s",
                {"v": value})]
        else:
            id_type = _LOOKUP_ID_TYPES[params.id_type]
            if params.id_type == "chebi" and not value.upper().startswith("CHEBI:"):
                value = f"CHEBI:{value}"
            ids = [r["struct_id"] for r in await self._query(
                "select distinct struct_id from identifier where id_type = %(t)s and upper(trim(identifier)) = upper(%(v)s)",
                {"t": id_type, "v": value})]
        ids = [i for i in ids if i is not None][:5]
        if not ids:
            return ToolResult(
                source=self.name, tool="get_drug", notes=[f"no DrugCentral drug for {params.id_type} {value}"]
            )
        n = params.max_items
        batch = [
            (f"select {_DRUG_COLS}, s.smiles, s.clogp, s.tpsa, s.lipinski, s.inchi from structures s "
             "where s.id = any(%(ids)s)", {"ids": ids}),
            ("select id as struct_id, name from synonyms where id = any(%(ids)s) order by preferred_name desc nulls last, name",
             {"ids": ids}),
            ("select struct_id, approval, type, applicant, orphan from approval where struct_id = any(%(ids)s) "
             "order by approval", {"ids": ids}),
            ("select sa.struct_id, a.code, a.l1_name, a.l2_name, a.l3_name, a.l4_name from struct2atc sa "
             "join atc a on a.code = sa.atc_code where sa.struct_id = any(%(ids)s) order by a.code", {"ids": ids}),
            ("select struct_id, relationship_name, concept_name, umls_cui, snomed_conceptid, doid "
             "from omop_relationship_doid_view where struct_id = any(%(ids)s) order by relationship_name, concept_name",
             {"ids": ids}),
            ("select struct_id, target_name, gene, accession, action_type, act_type, act_value, moa_source, "
             "target_class, tdl from act_table_full where struct_id = any(%(ids)s) and moa = 1 "
             "order by act_value desc nulls last", {"ids": ids}),
            ("select struct_id, type, name, class_code, source from pharma_class "
             "where struct_id = any(%(ids)s) and source in ('FDA', 'MeSH') order by type, name", {"ids": ids}),
            ("select struct_id, meddra_name, llr from faers where struct_id = any(%(ids)s) and llr > llr_threshold "
             "order by llr desc limit 200", {"ids": ids}),
            ("select struct_id, count(*) as n from act_table_full where struct_id = any(%(ids)s) group by struct_id",
             {"ids": ids}),
        ]
        results = await self._batch(batch)
        (_, drugs), (_, syns), (_, apps), (_, atcs), (_, rels), (_, moas), (_, classes), (_, faers), (_, actn) = results

        def of(rows: list[dict], sid: int) -> list[dict]:
            return [r for r in rows if r.get("struct_id") == sid]

        records = []
        for drug in drugs:
            sid = drug["id"]
            _, all_xrefs = _xref_ids(drug.get("xrefs"))
            relations: dict[str, list[str]] = {}
            disease_ids: dict[str, list[str]] = {}
            for r in of(rels, sid):
                label = r["concept_name"]
                tags = [t for t in (r.get("umls_cui"), r.get("doid")) if t]
                relations.setdefault(r["relationship_name"], []).append(f"{label} [{', '.join(tags)}]" if tags else label)
                if r["relationship_name"] == "indication" and r.get("umls_cui"):
                    disease_ids.setdefault("indication_umls", []).append(r["umls_cui"])
            targets = []
            seen_targets: set[str] = set()
            for t in of(moas, sid):
                key = f"{t.get('gene')}|{t.get('accession')}"
                if key in seen_targets:
                    continue
                seen_targets.add(key)
                targets.append(_compact({
                    "target": t.get("target_name"), "gene": t.get("gene"), "uniprot": t.get("accession"),
                    "action": t.get("action_type"), "class": t.get("target_class"), "tdl": t.get("tdl"),
                    "activity": f"{t.get('act_type')} {_jsonable(t.get('act_value'))}" if t.get("act_type") else None,
                    "source": t.get("moa_source"),
                }))
            pharma = {}
            for c in of(classes, sid):
                label = "MeSH PA" if c["source"] == "MeSH" else f"FDA {c['type']}"
                pharma.setdefault(label, []).append(f"{c['name']} [{c['class_code']}]" if c.get("class_code") else c["name"])
            act_count = next((r["n"] for r in actn if r["struct_id"] == sid), 0)
            extra = {
                "synonyms": [s["name"] for s in of(syns, sid)][:n],
                "approvals": [
                    _compact({"date": _jsonable(a["approval"]), "agency": a["type"], "applicant": a.get("applicant") or None,
                              "orphan": a.get("orphan")})
                    for a in of(apps, sid)
                ][:n],
                "atc": [f"{a['code'].strip()} {a['l4_name']} ({a['l1_name']} > {a['l2_name']} > {a['l3_name']})" for a in of(atcs, sid)][:n],
                "indications": relations.get("indication", [])[:n],
                "contraindications": relations.get("contraindication", [])[:n],
                "off_label_uses": relations.get("off-label use", [])[:n],
                "relation_counts": {k: len(v) for k, v in relations.items()} or None,
                "moa_targets": targets[:n],
                "activity_count": act_count,
                "pharmacologic_classes": pharma or None,
                "faers_signals": [f["meddra_name"] for f in of(faers, sid)][:n],
                "xrefs": {k: v[:10] for k, v in all_xrefs.items()},
                "smiles": drug.get("smiles"),
                "clogp": _jsonable(drug.get("clogp")),
                "tpsa": _jsonable(drug.get("tpsa")),
                "lipinski_violations": drug.get("lipinski"),
            }
            record = _drug_record(drug, **extra)
            atc_codes = [a["code"].strip() for a in of(atcs, sid)]
            record.identifiers.update(
                Record(source=self.name, id=str(sid), identifiers={"atc": atc_codes, "smiles": drug.get("smiles")}).identifiers
            )
            records.append(record)
        notes = []
        if len(ids) > 1:
            notes.append(f"{params.id_type} {value} maps to {len(ids)} DrugCentral structures")
        return ToolResult(source=self.name, tool="get_drug", records=records, total=len(records), notes=notes)

    @tool(
        description="""
        Protein targets of a drug from DrugCentral's curated bioactivity table (ChEMBL, IUPHAR,
        DrugMatrix, literature, labels): one record per target with gene symbol, UniProt accession,
        target class, Pharos TDL, whether it is the mechanism-of-action target, action type
        (INHIBITOR, AGONIST, ...) and the best activity (act_type with -log10 molar value, e.g.
        Kd 8.96 = 1.1 nM). `drug` is a struct_id or exact name/synonym. moa_only=true gives just
        the MoA targets.
        """
    )
    async def drug_targets(self, params: DrugTargetsParams) -> ToolResult:
        sid, name = await self._resolve(params.drug)
        if sid is None:
            return ToolResult(source=self.name, tool="drug_targets", notes=[f"no DrugCentral drug {params.drug!r}"])
        rows = await self._query(
            f"""
            select target_id, target_name, gene, accession, swissprot, target_class, tdl, organism,
                   max(moa) as moa, string_agg(distinct action_type, ', ') as action_types,
                   max(act_value) as best_value,
                   (array_agg(act_type order by act_value desc nulls last))[1] as best_type,
                   string_agg(distinct act_source, ', ') as sources, count(*) as n_activities
            from act_table_full
            where struct_id = %(sid)s {"and moa = 1" if params.moa_only else ""}
                  {"and organism = 'Homo sapiens'" if params.human_only else ""}
            group by target_id, target_name, gene, accession, swissprot, target_class, tdl, organism
            order by max(moa) desc nulls last, max(act_value) desc nulls last
            limit %(lim)s
            """,
            {"sid": sid, "lim": params.limit},
        )
        records = []
        for r in rows:
            accessions = [a for a in (r.get("accession") or "").split("|") if a]
            genes = [g for g in (r.get("gene") or "").split("|") if g]
            records.append(
                Record(
                    source=self.name,
                    id=str(r["target_id"]),
                    type="target",
                    title=r.get("target_name"),
                    url=f"https://www.uniprot.org/uniprotkb/{accessions[0]}" if accessions else None,
                    identifiers={
                        "uniprot": accessions[0] if len(accessions) == 1 else None,
                        "gene_symbol": genes[0] if len(genes) == 1 and r.get("organism") == "Homo sapiens" else None,
                    },
                    attributes=_compact({
                        "drug": name,
                        "drug_struct_id": sid,
                        "genes": genes,
                        "uniprot_accessions": accessions if len(accessions) > 1 else None,
                        "target_class": r.get("target_class"),
                        "tdl": r.get("tdl"),
                        "organism": r.get("organism"),
                        "moa_target": bool(r.get("moa")),
                        "action_type": r.get("action_types"),
                        "best_activity": f"{r['best_type']} {_jsonable(r['best_value'])}" if r.get("best_type") else None,
                        "activity_sources": r.get("sources"),
                        "n_activities": r.get("n_activities"),
                    }),
                )
            )
        notes = ["activity values are -log10(molar), e.g. 9 = 1 nM"]
        if not records:
            notes.insert(0, f"no {'MoA ' if params.moa_only else ''}targets recorded for {name}")
        return ToolResult(source=self.name, tool="drug_targets", records=records, total=len(records), notes=notes)

    @tool(
        description="""
        Drugs acting on a protein target (gene symbol such as EGFR / ABL1 / DRD2, or UniProt
        accession such as P00533) from DrugCentral's bioactivity table: one drug record per drug
        with the action type, MoA flag and best activity (-log10 molar) at that target, plus drug
        identifiers (chembl, inchikey, unii, ...). moa_only=true returns only drugs whose
        mechanism of action is this target; approved_only restricts to drugs with an approval.
        """
    )
    async def drugs_for_target(self, params: DrugsForTargetParams) -> ToolResult:
        target = params.target.strip()
        is_acc = bool(re.match(r"^[OPQ][0-9][A-Z0-9]{3}[0-9]$|^[A-NR-Z][0-9][A-Z][A-Z0-9]{2}[0-9]([A-Z][A-Z0-9]{2}[0-9])?$", target.upper()))
        cond = (
            "upper(a.accession) ~ ('(^|\\|)' || upper(%(t)s) || '($|\\|)')"
            if is_acc
            else "upper(a.gene) ~ ('(^|\\|)' || upper(%(t)s) || '($|\\|)')"
        )
        sql = f"""
        with act as (
            select a.struct_id, max(a.moa) as moa, string_agg(distinct a.action_type, ', ') as action_types,
                   max(a.act_value) as best_value,
                   (array_agg(a.act_type order by a.act_value desc nulls last))[1] as best_type,
                   string_agg(distinct a.target_name, '; ') as targets
            from act_table_full a where {cond} {"and a.moa = 1" if params.moa_only else ""}
            group by a.struct_id
        )
        select {_DRUG_COLS}, act.moa, act.action_types, act.best_value, act.best_type, act.targets,
               count(*) over () as total
        from act join structures s on s.id = act.struct_id
        {"where " + _APPROVED_SQL if params.approved_only else ""}
        order by act.moa desc nulls last, ({_APPROVED_SQL}) desc, act.best_value desc nulls last
        limit %(lim)s
        """
        rows = await self._query(sql, {"t": re.escape(target), "lim": params.limit})
        records = [
            _drug_record(
                r,
                target=r.get("targets"),
                target_query=target,
                moa_target=bool(r.get("moa")),
                action_type=r.get("action_types"),
                best_activity=f"{r['best_type']} {_jsonable(r['best_value'])}" if r.get("best_type") else None,
            )
            for r in rows
        ]
        return ToolResult(
            source=self.name,
            tool="drugs_for_target",
            records=records,
            total=int(rows[0]["total"]) if rows else 0,
            notes=[] if rows else [f"no DrugCentral activities recorded for target {target}"],
        )

    @tool(
        description="""
        Drugs indicated (or contraindicated, or used off-label) for a disease, from DrugCentral's
        curated OMOP/label-derived drug-disease relations. `disease` may be a name substring
        ('type 2 diabetes', 'melanoma'), UMLS CUI (C0011860), SNOMED CT id (44054006) or DOID
        (DOID:9352). One drug record per drug with the matched concept(s), relation, and drug
        identifiers. Use for "which approved drugs treat X" and drug repurposing context.
        """
    )
    async def drugs_for_indication(self, params: DrugsForIndicationParams) -> ToolResult:
        disease = params.disease.strip()
        if re.match(r"^C\d{7}$", disease, re.IGNORECASE):
            cond, value = "upper(o.umls_cui) = upper(%(d)s)", disease
        elif re.match(r"^DOID:\d+$", disease, re.IGNORECASE):
            cond, value = "upper(o.doid) = upper(%(d)s)", disease
        elif disease.isdigit():
            cond, value = "o.snomed_conceptid = %(d)s", int(disease)
        else:
            # every word must appear (any order): 'type 2 diabetes' matches 'Diabetes mellitus type 2'
            words = [w for w in re.split(r"[\s,]+", disease) if w][:8]
            value = None
        word_params: dict[str, Any] = {}
        if value is None:
            clauses = []
            for i, word in enumerate(words):
                like = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                word_params[f"w{i}"] = f"%{like}%"
                clauses.append(f"(o.concept_name ilike %(w{i})s or o.snomed_full_name ilike %(w{i})s)")
            cond = " and ".join(clauses) or "false"
        rel = "" if params.relationship == "any" else "and o.relationship_name = %(rel)s"
        sql = f"""
        with rel as (
            select o.struct_id, string_agg(distinct o.relationship_name, ', ') as relations,
                   array_agg(distinct o.concept_name) as concepts,
                   array_agg(distinct o.umls_cui) filter (where o.umls_cui is not null) as cuis,
                   array_agg(distinct o.doid) filter (where o.doid is not null) as doids
            from omop_relationship_doid_view o where {cond} {rel}
            group by o.struct_id
        )
        select {_DRUG_COLS}, rel.relations, rel.concepts, rel.cuis, rel.doids, count(*) over () as total
        from rel join structures s on s.id = rel.struct_id
        order by ({_APPROVED_SQL}) desc, s.no_formulations desc nulls last, s.name
        limit %(lim)s
        """
        rows = await self._query(sql, {"d": value, "rel": params.relationship, "lim": params.limit, **word_params})
        records = [
            _drug_record(
                r,
                relation=r.get("relations"),
                matched_concepts=(r.get("concepts") or [])[:8],
                concept_umls=(r.get("cuis") or [])[:8],
                concept_doid=(r.get("doids") or [])[:8],
            )
            for r in rows
        ]
        return ToolResult(
            source=self.name,
            tool="drugs_for_indication",
            records=records,
            total=int(rows[0]["total"]) if rows else 0,
            notes=[] if rows else [f"no drugs with {params.relationship} matching {disease!r}"],
        )

    @tool(
        description="""
        FAERS adverse-event signals for a drug precomputed by DrugCentral: MedDRA preferred terms
        ranked by log-likelihood ratio (LLR) with the drug-specific significance threshold and the
        2x2 report counts. population selects all reports or female / male / pediatric / geriatric
        subsets. Returns data rows. Signals are disproportionality statistics from spontaneous
        reports, not proof of causation; use openfda adverse_event_counts for raw, current counts.
        """
    )
    async def adverse_events(self, params: AdverseEventsParams) -> ToolResult:
        sid, name = await self._resolve(params.drug)
        if sid is None:
            return ToolResult(source=self.name, tool="adverse_events", notes=[f"no DrugCentral drug {params.drug!r}"])
        table = {"all": "faers", "female": "faers_female", "male": "faers_male", "pediatric": "faers_ped",
                 "geriatric": "faers_ger"}[params.population]
        rows = await self._query(
            f"""
            select meddra_name, meddra_code, llr, llr_threshold, drug_ae, drug_no_ae, no_drug_ae, no_drug_no_ae,
                   count(*) over () as total
            from {table} where struct_id = %(sid)s {"and llr > llr_threshold" if params.signals_only else ""}
            order by llr desc limit %(lim)s
            """,
            {"sid": sid, "lim": params.limit},
        )
        data = [
            {
                "term": r["meddra_name"],
                "meddra_code": r["meddra_code"],
                "llr": round(float(r["llr"]), 2) if r.get("llr") is not None else None,
                "llr_threshold": round(float(r["llr_threshold"]), 2) if r.get("llr_threshold") is not None else None,
                "reports_with_drug_and_event": r["drug_ae"],
                "reports_with_drug": (r["drug_ae"] or 0) + (r["drug_no_ae"] or 0),
                "reports_with_event_other_drugs": r["no_drug_ae"],
            }
            for r in rows
        ]
        notes = [
            "LLR = FAERS disproportionality log-likelihood ratio; terms above llr_threshold are signals, "
            "not proof of causation"
        ]
        if not rows:
            notes.insert(0, f"no FAERS {'signals' if params.signals_only else 'rows'} for {name} ({params.population})")
        return ToolResult(
            source=self.name,
            tool="adverse_events",
            total=int(rows[0]["total"]) if rows else 0,
            data={"drug": name, "struct_id": sid, "population": params.population, "rows": data},
            notes=notes,
        )

    @tool(
        description="""
        Describe the DrugCentral PostgreSQL schema for writing sql_query statements: without
        `table`, lists every table/view with approximate row counts and column names; with
        `table`, gives column names and types plus 3 sample rows. Key tables: structures (id =
        struct_id, name, inchikey, smiles, cas_reg_no), synonyms (id = struct_id), identifier
        (struct_id, id_type, identifier), act_table_full (struct_id, gene, accession, act_type,
        act_value, moa, action_type), omop_relationship_doid_view (indications), approval,
        struct2atc + atc, pharma_class, faers.
        """
    )
    async def describe_schema(self, params: DescribeSchemaParams) -> ToolResult:
        if params.table:
            table = params.table.strip().lower()
            if not re.match(r"^[a-z_][a-z0-9_]*$", table):
                return ToolResult(source=self.name, tool="describe_schema", notes=["invalid table name"])
            (_, cols), = await self._batch([(
                "select column_name, data_type from information_schema.columns "
                "where table_schema = 'public' and table_name = %(t)s order by ordinal_position", {"t": table})])
            if not cols:
                return ToolResult(source=self.name, tool="describe_schema", notes=[f"no table or view named {table!r}"])
            safe_cols = [c["column_name"] for c in cols if c["data_type"] not in ("bytea", "USER-DEFINED", "tsvector")
                         and c["column_name"] not in ("molfile",)]
            sample = await self._query(
                f'select {", ".join(chr(34) + c + chr(34) for c in safe_cols)} from "{table}" limit 3', None
            )
            return ToolResult(
                source=self.name,
                tool="describe_schema",
                data={
                    "table": table,
                    "columns": [{"name": c["column_name"], "type": c["data_type"]} for c in cols],
                    "sample_rows": [_jsonable(r, 200) for r in sample],
                },
            )
        if self._schema_cache is None:
            (_, cols), (_, counts) = await self._batch([
                ("select table_name, string_agg(column_name, ', ' order by ordinal_position) as columns "
                 "from information_schema.columns where table_schema = 'public' group by table_name order by table_name",
                 None),
                ("select c.relname as table_name, c.reltuples::bigint as approx_rows, c.relkind "
                 "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                 "where n.nspname = 'public' and c.relkind in ('r', 'v', 'm')", None),
            ])
            info = {r["table_name"]: r for r in counts}
            self._schema_cache = {
                "tables": [
                    _compact({
                        "table": r["table_name"],
                        "kind": {"r": "table", "v": "view", "m": "materialized view"}.get(
                            (info.get(r["table_name"]) or {}).get("relkind"), "table"),
                        "approx_rows": max(0, int((info.get(r["table_name"]) or {}).get("approx_rows") or 0)) or None,
                        "columns": r["columns"],
                    })
                    for r in cols
                ]
            }
        return ToolResult(
            source=self.name,
            tool="describe_schema",
            data=self._schema_cache,
            notes=["call describe_schema with table=<name> for column types and sample rows"],
        )

    @tool(
        description="""
        Run a read-only SQL query against the DrugCentral PostgreSQL database for advanced
        cross-table extraction (joins across structures, identifier, act_table_full,
        omop_relationship_doid_view, approval, atc, pharma_class, faers ...). Single SELECT or
        WITH statement only: no semicolons, comments, DDL/DML, COPY, SET or system functions.
        The query is wrapped as SELECT * FROM (<sql>) q LIMIT max_rows (<= 500) with a 15 s
        timeout. Returns data {columns, rows}. Call describe_schema first to see tables/columns.
        Example: "select s.name, a.action_type from act_table_full a join structures s on
        s.id = a.struct_id where a.gene = 'EGFR' and a.moa = 1".
        """
    )
    async def sql_query(self, params: SqlQueryParams) -> ToolResult:
        try:
            sql = validate_sql(params.sql)
        except ValueError as exc:
            return ToolResult(source=self.name, tool="sql_query", notes=[f"query rejected: {exc}"])
        wrapped = f"select * from ({sql}) q limit {int(params.max_rows) + 1}"
        try:
            (cols, rows), = await self._batch([(wrapped, None)], max_rows=params.max_rows + 1)
        except SourceError as exc:
            if exc.retryable:  # connection problems propagate; query problems go back to the caller
                raise
            return ToolResult(source=self.name, tool="sql_query", notes=[f"query failed: {exc.message}"])
        truncated = len(rows) > params.max_rows
        rows = rows[: params.max_rows]
        notes = [f"row cap {params.max_rows} reached; more rows exist"] if truncated else []
        return ToolResult(
            source=self.name,
            tool="sql_query",
            total=None if truncated else len(rows),
            data={"columns": cols, "rows": [[_jsonable(r.get(c)) for c in cols] for r in rows]},
            notes=notes,
        )
