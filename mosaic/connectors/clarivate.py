"""Clarivate Web of Science connector (Web of Science API Expanded).

Docs: https://developer.clarivate.com/apis/wos (API Expanded, v1:
``https://wos-api.clarivate.com/api/wos``). Swagger:
https://api.clarivate.com/swagger-ui/?url=https://developer.clarivate.com/apis/wos/swagger

Auth: ``CLARIVATE_API_KEY`` sent as the ``X-ApiKey`` header.

Entitlement probe for the configured key:

* Web of Science API Expanded (``wos-api.clarivate.com/api/wos``): OK - search (``/``), record by
  UID (``/id/{uid}``), citing (``/citing``), cited references (``/references``) and related
  records (``/related``). Monthly record quota is reported in ``x-rec-amtpermonth-remaining``.
* Web of Science Starter API (``api.clarivate.com/apis/wos-starter/v1``) documents and journals,
  and the Journals API / JCR (``api.clarivate.com/apis/wos-journals/v1``): HTTP 403
  ``{"message": "You cannot consume this service"}``. Journal Impact Factor tools are therefore not
  exposed; use scopus.serial_metrics for journal metrics.

Rate limit: API Expanded allows 2 requests/second per key (5/s for some subscriptions); this
connector uses 2. Page size ``count`` max 100; ``firstRecord`` is 1-based.

Query syntax (``usrQuery``): WoS advanced search field tags TS= (topic), TI= (title), AU= (author),
AI= (author identifier), OG= (organization), SO= (source title), PY= (year or range 2020-2024),
DO= (DOI), PMID=, IS= (ISSN), UT= (accession number), DT= (document type), combined with
AND/OR/NOT and parentheses.

Quirks: the JSON is a direct XML conversion, so a list with one element is returned as a bare object
(and an empty result as ``"records": ""``); every list access goes through ``_list``. PMIDs appear as
``MEDLINE:22745249`` in ``dynamic_data.cluster_related.identifiers``. Times cited is per collection
in ``tc_list.silo_tc`` (``WOS`` = Core Collection, ``WOK`` = all databases).
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import quote

from pydantic import Field

from ..http import HttpResponse, SourceError
from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, first, parse_offset, strip_html, tool

API = "https://wos-api.clarivate.com/api/wos"
RECORD_URL = "https://www.webofscience.com/wos/woscc/full-record/{uid}"
DATABASE = "WOS"

_FIELD_TAG = re.compile(r"\b(?:TS|TI|AU|AI|AB|AK|OG|OO|SO|PY|DO|PMID|IS|UT|DT|FO|FG|SU|WC|CU|KP|ALL)\s*=", re.I)
_SORT = {"relevance": "RS D", "times_cited": "TC D", "newest": "PY D", "oldest": "PY A"}  # "<field> <A|D>"


# ---------------------------------------------------------------- params
class WosSearchParams(SearchParams):
    year_from: int | None = Field(None, description="Only records published in or after this year")
    year_to: int | None = Field(None, description="Only records published in or before this year")
    sort: Literal["relevance", "times_cited", "newest", "oldest"] = Field(
        "relevance", description="relevance (default), times_cited (most cited first), newest, oldest"
    )


class DocumentParams(ToolParams):
    identifier: str = Field(
        description="Web of Science UID (WOS:000307535600036), DOI (10.1126/science.1225829) or PubMed ID (22745249)"
    )


class UidPageParams(ToolParams):
    uid: str = Field(description="Web of Science UID, e.g. WOS:000307535600036 (get it from search_documents/get_document)")
    limit: int = Field(10, ge=1, le=100, description="Maximum number of records to return")
    cursor: str | None = Field(None, description="Pagination cursor from a previous result")


class CitingParams(UidPageParams):
    sort: Literal["relevance", "times_cited", "newest", "oldest"] = Field(
        "newest", description="newest (default), times_cited, relevance or oldest"
    )


class CitationCountParams(ToolParams):
    dois: list[str] = Field(min_length=1, max_length=50, description="1-50 DOIs to look up")


# ---------------------------------------------------------------- helpers
def _list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _content(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("content")
    if value in (None, ""):
        return None
    return str(value).strip()


def _person(name: str) -> str:
    last, sep, given = name.partition(", ")
    return f"{given} {last}".strip() if sep else name.strip()


def _normalize_uid(uid: str) -> str:
    uid = uid.strip()
    if re.fullmatch(r"\d{15}", uid):
        return f"WOS:{uid}"
    if uid.upper().startswith("WOS:"):
        return "WOS:" + uid[4:]
    return uid


def _rec_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = ((payload.get("Data") or {}).get("Records") or {}).get("records") or {}
    if not isinstance(records, dict):
        return []
    return [r for r in _list(records.get("REC")) if isinstance(r, dict)]


def _found(payload: dict[str, Any]) -> int | None:
    return _int((payload.get("QueryResult") or {}).get("RecordsFound"))


def _record(rec: dict[str, Any]) -> Record:
    uid = rec.get("UID", "")
    static = rec.get("static_data") or {}
    summary = static.get("summary") or {}
    pub = summary.get("pub_info") or {}
    titles = {t.get("type"): t.get("content") for t in _list((summary.get("titles") or {}).get("title")) if isinstance(t, dict)}
    names = [n for n in _list((summary.get("names") or {}).get("name")) if isinstance(n, dict)]
    authors = [_person(n.get("display_name") or n.get("full_name") or "") for n in names if n.get("role") == "author"]
    orcids = [n.get("orcid_id") for n in names if n.get("role") == "author" and n.get("orcid_id")]
    full = static.get("fullrecord_metadata") or {}
    abstract_parts: list[str] = []
    for ab in _list((full.get("abstracts") or {}).get("abstract")):
        if isinstance(ab, dict):
            for p in _list((ab.get("abstract_text") or {}).get("p")):
                if isinstance(p, str):
                    abstract_parts.append(p)
    category = full.get("category_info") or {}
    subjects = [s for s in _list((category.get("subjects") or {}).get("subject")) if isinstance(s, dict)]
    keywords = [_content(k) for k in _list((full.get("keywords") or {}).get("keyword"))]
    item = static.get("item") or {}
    keywords_plus = [_content(k) for k in _list((item.get("keywords_plus") or {}).get("keyword"))]
    dynamic = rec.get("dynamic_data") or {}
    ids: dict[str, str] = {}
    for ident in _list(((dynamic.get("cluster_related") or {}).get("identifiers") or {}).get("identifier")):
        if isinstance(ident, dict) and ident.get("type") and ident.get("value"):
            ids.setdefault(ident["type"], str(ident["value"]))
    citation = dynamic.get("citation_related") or {}
    silo = {s.get("coll_id"): _int(s.get("local_count")) for s in _list((citation.get("tc_list") or {}).get("silo_tc")) if isinstance(s, dict)}
    topics = [s.get("content") for s in _list(((citation.get("citation_topics") or {}).get("subj-group") or {}).get("subject"))
              if isinstance(s, dict) and s.get("content-type") == "micro"]
    doctypes = [_content(d) for d in _list((summary.get("doctypes") or {}).get("doctype"))]
    publisher_names = [
        n
        for p in _list((summary.get("publishers") or {}).get("publisher"))
        if isinstance(p, dict)
        for n in _list((p.get("names") or {}).get("name"))
    ]
    publisher = next((n.get("unified_name") or n.get("full_name") for n in publisher_names if isinstance(n, dict)), None)
    page = pub.get("page") or {}
    date = pub.get("sortdate") or (str(pub.get("pubyear")) if pub.get("pubyear") else None)
    pmid = (ids.get("pmid") or "").removeprefix("MEDLINE:") or None
    return Record(
        source="clarivate",
        id=uid,
        type="book" if (pub.get("pubtype") or "").lower().startswith("book") else "article",
        title=strip_html(titles.get("item")),
        url=RECORD_URL.format(uid=uid) if uid else None,
        identifiers={
            "wos_uid": uid,
            "doi": ids.get("doi") or ids.get("xref_doi"),
            "pmid": pmid,
            "issn": ids.get("issn") or ids.get("eissn"),
            "isbn": ids.get("isbn") or ids.get("eisbn"),
        },
        date=date,
        authors=[a for a in authors if a],
        summary=strip_html(" ".join(abstract_parts)) or None,
        attributes=_clean(
            {
                "journal": titles.get("source"),
                "journal_abbrev": titles.get("abbrev_iso"),
                "times_cited": silo.get("WOS"),
                "times_cited_all_databases": silo.get("WOK"),
                "document_types": [d for d in doctypes if d],
                "volume": pub.get("vol"),
                "issue": pub.get("issue"),
                "pages": page.get("content") if isinstance(page, dict) else None,
                "publisher": publisher,
                "eissn": ids.get("eissn") if ids.get("issn") else None,
                "author_orcids": orcids[:10],
                "author_keywords": [k for k in keywords if k][:15],
                "keywords_plus": [k for k in keywords_plus if k][:15],
                "wos_categories": [_content(s) for s in subjects if s.get("ascatype") == "traditional"],
                "research_areas": [_content(s) for s in subjects if s.get("ascatype") == "extended"],
                "citation_topic": first(topics),
                "references_count": _int((full.get("refs") or {}).get("count")),
                "open_access_gold": pub.get("journal_oas_gold") == "Y" if pub.get("journal_oas_gold") else None,
                "early_access": pub.get("early_access_date"),
            }
        ),
    )


def _reference_record(ref: dict[str, Any]) -> Record:
    uid = ref.get("UID") or ""
    doi = ref.get("DOI")
    return Record(
        source="clarivate",
        id=uid or doi or f"{ref.get('CitedAuthor', '')} {ref.get('Year', '')} {ref.get('CitedWork', '')}".strip(),
        type="article",
        title=strip_html(ref.get("CitedTitle")) or ref.get("CitedWork"),
        url=RECORD_URL.format(uid=uid) if uid.startswith("WOS:") else (f"https://doi.org/{doi}" if doi else None),
        identifiers={"wos_uid": uid if uid.startswith("WOS:") else None, "doi": doi},
        date=str(ref["Year"]) if ref.get("Year") else None,
        authors=[_person(ref["CitedAuthor"])] if ref.get("CitedAuthor") else [],
        attributes=_clean(
            {
                "cited_work": ref.get("CitedWork"),
                "times_cited": _int(ref.get("TimesCited")),
                "volume": ref.get("Volume"),
                "page": ref.get("Page"),
                "uid": uid if uid and not uid.startswith("WOS:") else None,
            }
        ),
    )


def _doi_clause(dois: list[str]) -> str:
    return " OR ".join(f'"{d}"' for d in dois)


class Clarivate(Connector):
    name = "clarivate"
    title = "Web of Science (Clarivate)"
    category = "literature"
    description = (
        "Clarivate Web of Science Core Collection (~90M curated records since 1900) via the WoS API "
        "Expanded: full bibliographic records with abstracts, WoS times-cited counts, cited references, "
        "citing and related records. Best for authoritative citation counts and citation-network traversal."
    )
    homepage = "https://www.webofscience.com/"
    docs_url = "https://developer.clarivate.com/apis/wos"
    auth = "api_key"
    required_settings = ("clarivate_api_key",)
    rate_limit = 2.0
    health_query = "CRISPR"

    def _headers(self) -> dict[str, str]:
        return {"X-ApiKey": self.setting("clarivate_api_key") or "", "Accept": "application/json"}

    async def _get(self, path: str, params: dict[str, Any]) -> HttpResponse:
        try:
            return await self.request(
                "GET", f"{API}{path}", params={"databaseId": DATABASE, **params}, headers=self._headers(),
                allow_status=(404,),
            )
        except SourceError as exc:
            if exc.status in (401, 403):
                match = re.search(r'"(?:message|Message)"\s*:\s*"([^"]+)"', exc.message)
                detail = match.group(1) if match else "access denied"
                raise SourceError(
                    self.name,
                    f"Clarivate refused the request (HTTP {exc.status}: {detail}). The configured "
                    "CLARIVATE_API_KEY must be subscribed to the Web of Science API Expanded "
                    "(developer.clarivate.com); the monthly record quota may also be exhausted.",
                    status=exc.status,
                    url=exc.url,
                ) from exc
            raise

    def _page(self, tool_name: str, response: HttpResponse, offset: int, notes: list[str] | None = None) -> ToolResult:
        payload = response.json() if response.status == 200 else {}
        recs = _rec_list(payload)
        total = _found(payload)
        nxt = offset + len(recs)
        return ToolResult(
            source=self.name,
            tool=tool_name,
            records=[_record(r) for r in recs],
            total=total,
            next_cursor=str(nxt) if recs and total and nxt < total else None,
            request_url=response.url,
            notes=(notes or []) + ([] if recs else ["no Web of Science records matched"]),
        )

    # ------------------------------------------------------------ tools
    @tool(
        description="""
        Search the Web of Science Core Collection (curated, all sciences incl. biomedicine) and return full
        records: WoS UID, DOI, PMID, ISSN, title, all authors, journal, date, abstract, WoS times-cited
        count, keywords, WoS categories and citation topic. Use for authoritative citation counts,
        times_cited-ranked searches and as the entry point for citation-network tools. Plain text is
        searched as a topic query TS=(...); WoS advanced syntax is passed through, e.g.
        'TS=(crispr AND "sickle cell") AND PY=(2020-2024)', 'AU=(Doudna J*) AND OG=(UC Berkeley)',
        'DO=(10.1126/science.1225829)', 'PMID=(22745249)', 'SO=(NATURE MEDICINE)'. Max 100 per page.
        """,
        federated=True,
    )
    async def search_documents(self, params: WosSearchParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        query = params.query.strip()
        if not _FIELD_TAG.search(query):
            query = f"TS=({query})"
        if params.year_from or params.year_to:
            query = f"({query}) AND PY=({params.year_from or 1900}-{params.year_to or 2100})"
        response = await self._get(
            "/", {"usrQuery": query, "count": params.limit, "firstRecord": offset + 1, "sortField": _SORT[params.sort]}
        )
        return self._page("search_documents", response, offset)

    @tool(
        description="""
        Fetch one full Web of Science record by WoS UID (WOS:...), DOI or PubMed ID: all authors with
        ORCIDs, abstract, journal, ISSN, WoS times-cited count (Core Collection and all databases),
        keywords, WoS categories, references count. Use to get the WoS citation count / UID for a known
        paper before calling citing_documents or cited_references.
        """
    )
    async def get_document(self, params: DocumentParams) -> ToolResult:
        ident = params.identifier.strip()
        if re.fullmatch(r"(?i)(WOS:)?\d{15}", ident):
            uid = _normalize_uid(ident)
            response = await self._get(f"/id/{quote(uid, safe=':')}", {"count": 1, "firstRecord": 1})
            label = f"UID {uid}"
        elif re.fullmatch(r"\d{1,9}", ident):
            response = await self._get("/", {"usrQuery": f"PMID=({ident})", "count": 1, "firstRecord": 1})
            label = f"PMID {ident}"
        else:
            doi = normalize_doi(ident)
            response = await self._get("/", {"usrQuery": f'DO=("{doi}")', "count": 1, "firstRecord": 1})
            label = f"DOI {doi}"
        result = self._page("get_document", response, 0)
        result.next_cursor = None
        if not result.records:
            result.notes = [f"{label} not found in the Web of Science Core Collection"]
        return result

    @tool(
        description="""
        List the Web of Science records that CITE a given record (forward citations), as full records with
        DOI/PMID/times-cited; total = WoS Core Collection times-cited count. Input is a WoS UID (use
        get_document to resolve a DOI/PMID first). sort=newest (default) or times_cited for the most
        influential citing papers.
        """
    )
    async def citing_documents(self, params: CitingParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        uid = _normalize_uid(params.uid)
        response = await self._get(
            "/citing",
            {"uniqueId": uid, "count": params.limit, "firstRecord": offset + 1, "sortField": _SORT[params.sort]},
        )
        return self._page("citing_documents", response, offset)

    @tool(
        description="""
        List the cited references (bibliography) of a Web of Science record: cited title, first author,
        year, cited work (journal), DOI, WoS UID when the reference is indexed and its times-cited count.
        Input is a WoS UID (use get_document to resolve a DOI/PMID first). Use for backward citation
        traversal.
        """
    )
    async def cited_references(self, params: UidPageParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        uid = _normalize_uid(params.uid)
        response = await self._get("/references", {"uniqueId": uid, "count": params.limit, "firstRecord": offset + 1})
        payload = response.json() if response.status == 200 else {}
        data = payload.get("Data")
        refs = [r for r in _list(data) if isinstance(r, dict)]
        total = _found(payload)
        nxt = offset + len(refs)
        return ToolResult(
            source=self.name,
            tool="cited_references",
            records=[_reference_record(r) for r in refs],
            total=total,
            next_cursor=str(nxt) if refs and total and nxt < total else None,
            request_url=response.url,
            notes=[] if refs else [f"no cited references found for {uid}"],
        )

    @tool(
        description="""
        List Web of Science records RELATED to a given record (records sharing the most cited references,
        i.e. bibliographic coupling), as full records. Input is a WoS UID. Use to find topically similar
        papers that keyword search misses.
        """
    )
    async def related_documents(self, params: UidPageParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        uid = _normalize_uid(params.uid)
        response = await self._get("/related", {"uniqueId": uid, "count": params.limit, "firstRecord": offset + 1})
        return self._page("related_documents", response, offset)

    @tool(
        description="""
        Batch lookup of Web of Science times-cited counts for 1-50 DOIs in a single call. Returns
        data.counts {doi: {wos_uid, times_cited, times_cited_all_databases}} plus the matched records;
        DOIs not indexed in the Core Collection are listed in data.not_found. Use to compare citation
        impact of a set of papers found in other databases.
        """
    )
    async def citation_counts(self, params: CitationCountParams) -> ToolResult:
        dois = list(dict.fromkeys(normalize_doi(d) for d in params.dois if d.strip()))
        response = await self._get(
            "/", {"usrQuery": f"DO=({_doi_clause(dois)})", "count": min(100, len(dois) * 2), "firstRecord": 1}
        )
        result = self._page("citation_counts", response, 0)
        counts: dict[str, dict[str, Any]] = {}
        for record in result.records:
            doi = record.identifiers.get("doi")
            if doi in dois and doi not in counts:
                counts[doi] = _clean(
                    {
                        "wos_uid": record.id,
                        "times_cited": record.attributes.get("times_cited"),
                        "times_cited_all_databases": record.attributes.get("times_cited_all_databases"),
                    }
                )
        result.next_cursor = None
        result.data = {"counts": counts, "not_found": [d for d in dois if d not in counts]}
        result.notes = []
        return result
