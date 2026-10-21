"""Elsevier Scopus + ScienceDirect APIs connector.

Docs: https://dev.elsevier.com/api_docs.html (Scopus Search, Abstract Retrieval, Serial Title,
Author Search, ScienceDirect Search v2, Article Retrieval).

Auth: ``ELSEVIER_SCOPUS_API_KEY`` sent as the ``X-ELS-APIKey`` header; optional
``ELSEVIER_INSTTOKEN`` sent as ``X-ELS-Insttoken`` (headers only, never query params).

Entitlements are per key and per view. Probed for the configured key (no insttoken, off-campus):

* Scopus Search, STANDARD view: OK (``view=COMPLETE`` -> 401 AUTHORIZATION_ERROR). STANDARD has
  no abstract and only the first author (``dc:creator``); count is capped at 25 per request and
  ``start + count`` at 5000.
* Abstract Retrieval (doi / eid / pubmed_id / scopus_id), META view: OK. META_ABS, REF and FULL
  views -> 401, so no abstracts, subject areas or references from Scopus itself.
* Serial Title (journal metrics: CiteScore, SNIP, SJR), STANDARD view: OK (ENHANCED -> 401).
* ScienceDirect Article Retrieval (``/content/article/...``): OK with the default view, which
  returns full text (``originalText``) for open-access / open-archive articles and metadata +
  abstract otherwise (an explicit ``view=FULL`` on a non-entitled article is a 400 INVALID_INPUT).
* Author Search / Author Retrieval / Affiliation Search: 401. ScienceDirect Search (PUT) and the
  legacy GET ScienceDirect search: 401. Citation Overview / citation-count: 403
  ("Requestor configuration settings insufficient"). Author and ScienceDirect search tools are
  still implemented and raise a clear entitlement SourceError; citation overview is not exposed.

Quotas: weekly per-API quotas (Scopus Search 20,000/week, Abstract Retrieval 10,000/week,
Author Search 5,000/week) and ~9 req/s throttling; this connector stays at 3 req/s.

Quirks: responses echo the API key inside ``link`` hrefs, so links are never copied into records.
Empty Scopus searches return ``entry: [{"error": "Result set was empty"}]`` instead of ``[]``.
ISSNs arrive without hyphens (``00928674``) and are hyphenated here for cross-source linking.
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

API = "https://api.elsevier.com/content"
SCOPUS_RECORD = "https://www.scopus.com/record/display.uri?eid={eid}&origin=inward"
SCOPUS_MAX_WINDOW = 5000

_FIELD_CODE = re.compile(
    r"(?<![A-Za-z0-9-])(?:TITLE-ABS-KEY(?:-AUTH)?|TITLE-ABS|TITLE|ABS|KEY|AUTHKEY|INDEXTERMS|ALL|AUTH|"
    r"AUTHOR-NAME|AUTHLASTNAME|AUTHFIRST|AU-ID|ORCID|AFFIL|AFFILORG|AFFILCITY|AFFILCOUNTRY|AF-ID|SRCTITLE|"
    r"EXACTSRCTITLE|SRCTYPE|ISSN|ISBN|DOI|PMID|EID|DOCTYPE|SUBJAREA|LANGUAGE|OPENACCESS|PUBLISHER|"
    r"FUND-SPONSOR|FUND-NO|CHEM|CASREGNUMBER|REF|VOLUME|ISSUE|ARTNUM)\s*\(|\bPUBYEAR\s*[<>=]",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- params
class ScopusSearchParams(SearchParams):
    limit: int = Field(10, ge=1, le=25, description="Maximum number of records to return (Scopus caps at 25)")
    year_from: int | None = Field(None, description="Only documents published in or after this year")
    year_to: int | None = Field(None, description="Only documents published in or before this year")
    open_access: bool | None = Field(None, description="true = only open-access documents")
    sort: Literal["relevance", "cited", "newest"] = Field(
        "relevance", description="relevance (default), cited (most cited first) or newest (cover date)"
    )


class AbstractParams(ToolParams):
    identifier: str = Field(
        description=(
            "DOI (10.1016/j.cell.2013.04.025), Scopus EID (2-s2.0-84877707375), PubMed ID (23643243) "
            "or numeric Scopus ID (84877707375)"
        )
    )
    id_type: Literal["auto", "doi", "eid", "pmid", "scopus_id"] = Field(
        "auto", description="Identifier type; auto detects DOI / EID / PMID (<=9 digits) / Scopus ID (>=10 digits)"
    )


class SerialParams(ToolParams):
    issn: str | None = Field(None, description="Journal ISSN or eISSN, e.g. 0092-8674 (exact lookup)")
    title: str | None = Field(None, description="Journal title words, e.g. 'lancet oncology' (when ISSN unknown)")
    limit: int = Field(5, ge=1, le=25, description="Maximum journals to return for a title search")


class FullTextParams(ToolParams):
    identifier: str = Field(
        description="DOI (10.1016/...), ScienceDirect PII (S0092867413004674), or PubMed ID of an Elsevier article"
    )
    id_type: Literal["auto", "doi", "pii", "pmid"] = Field(
        "auto", description="Identifier type; auto detects DOI / PII (starts with S) / PMID (digits)"
    )
    max_chars: int = Field(20000, ge=500, le=100000, description="Truncate the returned full text to this many characters")


class AuthorSearchParams(ToolParams):
    last_name: str | None = Field(None, description="Author surname, e.g. Doudna (required unless orcid is given)")
    first_name: str | None = Field(None, description="Given name or initial, e.g. Jennifer or J")
    affiliation: str | None = Field(None, description="Affiliation words, e.g. Berkeley")
    orcid: str | None = Field(None, description="ORCID iD (0000-0001-9161-999X); overrides the name fields")
    limit: int = Field(10, ge=1, le=25, description="Maximum number of authors to return")
    cursor: str | None = Field(None, description="Pagination cursor from a previous result")


class ScienceDirectSearchParams(SearchParams):
    limit: int = Field(10, ge=1, le=100, description="Maximum number of records to return")
    year_from: int | None = Field(None, description="Only articles published in or after this year")
    year_to: int | None = Field(None, description="Only articles published in or before this year")
    open_access: bool | None = Field(None, description="true = only open-access articles")


# ---------------------------------------------------------------- helpers
def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _issn(value: Any) -> str | None:
    """Hyphenate an 8-character ISSN (Scopus drops the hyphen)."""
    if not value:
        return None
    text = str(value).strip().upper()
    if re.fullmatch(r"\d{7}[\dX]", text):
        return f"{text[:4]}-{text[4:]}"
    return text or None


def _issns(*values: Any) -> list[str]:
    out: list[str] = []
    for value in values:
        for part in str(value or "").split():
            norm = _issn(part)
            if norm and norm not in out:
                out.append(norm)
    return out


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _isbn(value: Any) -> str | None:
    """First ISBN. Search results pack several into one string: ``[9781493908905, 9781493908912]``."""
    text = _text(first(_list(value))) or ""
    match = re.search(r"[0-9][0-9Xx-]{8,16}[0-9Xx]", text)
    return match.group(0) if match else (text or None)


def _text(value: Any) -> str | None:
    """Elsevier JSON wraps many strings as {"$": "..."}."""
    if isinstance(value, dict):
        value = value.get("$")
    if isinstance(value, list):
        return "; ".join(t for t in (_text(v) for v in value) if t) or None
    return str(value).strip() if value not in (None, "") else None


def _entitlement_message(exc: SourceError, what: str) -> str:
    match = re.search(r'"statusText"\s*:\s*"([^"]+)"', exc.message)
    code = re.search(r'"statusCode"\s*:\s*"([^"]+)"', exc.message)
    detail = " - ".join(x.group(1) for x in (code, match) if x) or "access denied"
    return (
        f"{what} is not available with the configured Elsevier API key (HTTP {exc.status}: {detail}). "
        "This API/view needs an institutional entitlement: set ELSEVIER_INSTTOKEN, run from a subscribing "
        "institution's network, or ask Elsevier to enable it for the key."
    )


def _search_record(entry: dict[str, Any]) -> Record:
    eid = entry.get("eid") or ""
    issns = _issns(entry.get("prism:issn"), entry.get("prism:eIssn"))
    affils = [a.get("affilname") for a in _list(entry.get("affiliation")) if isinstance(a, dict) and a.get("affilname")]
    countries = sorted({a.get("affiliation-country") for a in _list(entry.get("affiliation"))
                        if isinstance(a, dict) and a.get("affiliation-country")})
    creator = entry.get("dc:creator")
    return Record(
        source="scopus",
        id=eid or entry.get("dc:identifier", ""),
        type="book" if (entry.get("prism:aggregationType") or "").lower() in ("book", "book series") else "article",
        title=strip_html(entry.get("dc:title")),
        url=SCOPUS_RECORD.format(eid=eid) if eid else None,
        identifiers={
            "scopus_eid": eid,
            "doi": entry.get("prism:doi"),
            "pmid": entry.get("pubmed-id"),
            "issn": first(issns),
            "isbn": _isbn(entry.get("prism:isbn")),
        },
        date=entry.get("prism:coverDate"),
        authors=[creator] if isinstance(creator, str) and creator else [],
        attributes=_clean(
            {
                "journal": entry.get("prism:publicationName"),
                "citedby_count": _int(entry.get("citedby-count")),
                "document_type": entry.get("subtypeDescription"),
                "source_type": entry.get("prism:aggregationType"),
                "volume": entry.get("prism:volume"),
                "issue": entry.get("prism:issueIdentifier"),
                "pages": entry.get("prism:pageRange"),
                "article_number": entry.get("article-number"),
                "issns": issns if len(issns) > 1 else None,
                "affiliations": affils[:8],
                "affiliation_countries": countries,
                "open_access": bool(entry.get("openaccessFlag")) if "openaccessFlag" in entry else None,
                "scopus_source_id": entry.get("source-id"),
                "pii": entry.get("pii"),
            }
        ),
    )


def _abstract_record(payload: dict[str, Any]) -> Record:
    core = payload.get("coredata") or {}
    eid = core.get("eid") or ""
    authors_raw = _list(((payload.get("authors") or {}).get("author")) or (core.get("dc:creator") or {}).get("author"))
    authors: list[str] = []
    author_ids: list[str] = []
    for a in authors_raw:
        if not isinstance(a, dict):
            continue
        pref = a.get("preferred-name") or {}
        given = a.get("ce:given-name") or pref.get("ce:given-name")
        surname = a.get("ce:surname") or pref.get("ce:surname")
        name = " ".join(x for x in (given, surname) if x) or a.get("ce:indexed-name")
        if name:
            authors.append(name)
        if a.get("@auid"):
            author_ids.append(a["@auid"])
    affils = [a for a in _list(payload.get("affiliation")) if isinstance(a, dict)]
    issns = _issns(core.get("prism:issn"), core.get("prism:eIssn"))
    subjects = [_text(s) for s in _list((payload.get("subject-areas") or {}).get("subject-area"))]
    keywords = [_text(k) for k in _list((payload.get("authkeywords") or {}).get("author-keyword"))]
    return Record(
        source="scopus",
        id=eid or core.get("dc:identifier", ""),
        type="book" if (core.get("prism:aggregationType") or "").lower() in ("book", "book series") else "article",
        title=strip_html(core.get("dc:title")),
        url=SCOPUS_RECORD.format(eid=eid) if eid else None,
        identifiers={
            "scopus_eid": eid,
            "doi": core.get("prism:doi"),
            "pmid": core.get("pubmed-id"),
            "issn": first(issns),
            "isbn": _isbn(core.get("prism:isbn")),
        },
        date=core.get("prism:coverDate"),
        authors=authors,
        summary=strip_html(_text(core.get("dc:description"))),
        attributes=_clean(
            {
                "journal": core.get("prism:publicationName"),
                "publisher": core.get("dc:publisher"),
                "citedby_count": _int(core.get("citedby-count")),
                "document_type": core.get("subtypeDescription"),
                "source_type": core.get("prism:aggregationType"),
                "volume": core.get("prism:volume"),
                "issue": core.get("prism:issueIdentifier"),
                "pages": core.get("prism:pageRange"),
                "issns": issns if len(issns) > 1 else None,
                "open_access": str(core.get("openaccessFlag")).lower() == "true" if "openaccessFlag" in core else None,
                "affiliations": [
                    ", ".join(x for x in (a.get("affilname"), a.get("affiliation-city"), a.get("affiliation-country")) if x)
                    for a in affils[:10]
                ],
                "scopus_author_ids": author_ids[:25],
                "subject_areas": [s for s in subjects if s],
                "author_keywords": [k for k in keywords if k],
                "scopus_source_id": core.get("source-id"),
                "pii": core.get("pii"),
            }
        ),
    )


def _latest(entries: Any, key: str = "$") -> tuple[float | None, str | None]:
    best: tuple[str, Any] | None = None
    for item in _list(entries):
        if isinstance(item, dict) and item.get("@year"):
            if best is None or str(item["@year"]) > best[0]:
                best = (str(item["@year"]), item.get(key))
    return (_float(best[1]), best[0]) if best else (None, None)


def _serial_record(entry: dict[str, Any]) -> Record:
    source_id = entry.get("source-id") or ""
    issn, eissn = _issn(entry.get("prism:issn")), _issn(entry.get("prism:eIssn"))
    snip, snip_year = _latest((entry.get("SNIPList") or {}).get("SNIP"))
    sjr, sjr_year = _latest((entry.get("SJRList") or {}).get("SJR"))
    cs = entry.get("citeScoreYearInfoList") or {}
    return Record(
        source="scopus",
        id=f"source:{source_id}" if source_id else (issn or eissn or ""),
        type="journal",
        title=entry.get("dc:title"),
        url=f"https://www.scopus.com/sourceid/{source_id}" if source_id else None,
        identifiers={"issn": issn or eissn},
        attributes=_clean(
            {
                "eissn": eissn,
                "publisher": entry.get("dc:publisher"),
                "source_type": entry.get("prism:aggregationType"),
                "citescore": _float(cs.get("citeScoreCurrentMetric")),
                "citescore_year": cs.get("citeScoreCurrentMetricYear"),
                "citescore_tracker": _float(cs.get("citeScoreTracker")),
                "citescore_tracker_year": cs.get("citeScoreTrackerYear"),
                "snip": snip,
                "snip_year": snip_year,
                "sjr": sjr,
                "sjr_year": sjr_year,
                "subject_areas": [s.get("$") for s in _list(entry.get("subject-area")) if isinstance(s, dict)],
                "coverage": "-".join(x for x in (entry.get("coverageStartYear"), entry.get("coverageEndYear")) if x),
                "open_access": entry.get("openaccess") == "1" if entry.get("openaccess") is not None else None,
                "scopus_source_id": source_id,
            }
        ),
    )


def _flatten_text(node: Any, out: list[str]) -> None:
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for item in node:
            _flatten_text(item, out)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key.startswith("@") or key in ("xocs:meta", "ce:bibliography", "ce:floats"):
                continue
            _flatten_text(value, out)


def _full_text(original: Any, abstract: str | None) -> str | None:
    if isinstance(original, dict):
        parts: list[str] = []
        _flatten_text(original.get("xocs:doc", original), parts)
        original = " ".join(parts)
    if not isinstance(original, str):
        return None
    text = re.sub(r"\s+", " ", original).strip()
    # originalText starts with a long run of XOCS index metadata; skip to the abstract or introduction.
    start = -1
    if abstract:
        start = text.find(re.sub(r"\s+", " ", abstract)[:80])
    if start < 0:
        match = re.search(r"\b(?:1 )?Introduction\b", text)
        start = match.start() if match else 0
    text = text[start:]
    refs = text.rfind(" References ")
    if refs > len(text) // 2:  # drop the trailing reference list
        text = text[:refs]
    return text or None


class Scopus(Connector):
    name = "scopus"
    title = "Scopus (Elsevier)"
    category = "literature"
    description = (
        "Elsevier's curated abstract & citation index (~100M records, 27k+ journals) with Scopus "
        "citation counts and CiteScore/SNIP/SJR journal metrics, plus ScienceDirect full text for "
        "open-access Elsevier articles. Best for citation counts, affiliation-aware bibliographic "
        "search and journal impact metrics."
    )
    homepage = "https://www.scopus.com/"
    docs_url = "https://dev.elsevier.com/api_docs.html"
    auth = "api_key"
    required_settings = ("elsevier_scopus_api_key",)
    rate_limit = 3.0
    health_query = "CRISPR"

    def _headers(self) -> dict[str, str]:
        headers = {"X-ELS-APIKey": self.setting("elsevier_scopus_api_key") or "", "Accept": "application/json"}
        token = self.setting("elsevier_insttoken")
        if token:
            headers["X-ELS-Insttoken"] = token
        return headers

    async def _els(self, method: str, url: str, what: str, **kwargs: Any) -> HttpResponse:
        try:
            return await self.request(method, url, headers=self._headers(), **kwargs)
        except SourceError as exc:
            if exc.status in (401, 403):
                raise SourceError(self.name, _entitlement_message(exc, what), status=exc.status, url=exc.url) from exc
            raise

    # ------------------------------------------------------------ tools
    @tool(
        description="""
        Search Scopus (Elsevier's curated abstract & citation index, all disciplines incl. biomedicine)
        and return documents with Scopus EID, DOI, PMID, ISSN, journal, cover date, first author,
        affiliations and Scopus cited-by counts. Use for citation-ranked or affiliation-aware literature
        searches (sort=cited) and to get Scopus EIDs; abstracts are NOT included (use pubmed/europepmc
        for abstracts). Plain text is searched as TITLE-ABS-KEY(...); Scopus advanced syntax is passed
        through, e.g. 'TITLE-ABS-KEY(crispr AND "base editing") AND PUBYEAR > 2020',
        'AUTHLASTNAME(doudna) AND AFFIL(berkeley)', 'DOI(10.1016/j.cell.2013.04.025)', 'PMID(23643243)',
        'SRCTITLE(lancet)'. Max 25 per page; at most 5000 results reachable by paging.
        """,
        federated=True,
    )
    async def search(self, params: ScopusSearchParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        query = params.query.strip()
        if not _FIELD_CODE.search(query):
            query = f"TITLE-ABS-KEY({query})"
        clauses = [f"({query})" if params.year_from or params.year_to or params.open_access else query]
        if params.year_from:
            clauses.append(f"PUBYEAR > {params.year_from - 1}")
        if params.year_to:
            clauses.append(f"PUBYEAR < {params.year_to + 1}")
        if params.open_access:
            clauses.append("OPENACCESS(1)")
        sort = {"relevance": "relevancy", "cited": "-citedby-count", "newest": "-coverDate"}[params.sort]
        if offset >= SCOPUS_MAX_WINDOW:
            return ToolResult(source=self.name, tool="search", notes=["Scopus Search cannot page beyond 5000 results; narrow the query"])
        count = min(params.limit, SCOPUS_MAX_WINDOW - offset)
        response = await self._els(
            "GET",
            f"{API}/search/scopus",
            "Scopus Search",
            params={"query": " AND ".join(clauses), "start": offset, "count": count, "sort": sort},
        )
        results = response.json().get("search-results") or {}
        entries = [e for e in _list(results.get("entry")) if isinstance(e, dict) and not e.get("error")]
        total = _int(results.get("opensearch:totalResults"))
        nxt = offset + len(entries)
        return ToolResult(
            source=self.name,
            tool="search",
            records=[_search_record(e) for e in entries],
            total=total,
            next_cursor=str(nxt) if entries and total and nxt < min(total, SCOPUS_MAX_WINDOW) else None,
            request_url=response.url,
            notes=[] if entries else ["no Scopus documents matched"],
        )

    @tool(
        description="""
        Fetch one Scopus record by DOI, Scopus EID, PubMed ID or Scopus ID (Abstract Retrieval API, META
        view): affiliations, journal, ISSNs, publisher, cover date, Scopus cited-by count and the
        DOI/PMID/EID cross-references. Use to get a paper's Scopus citation count or EID from a DOI or
        PMID. Without an institutional entitlement the META view lists only the first author and no
        abstract text; use pubmed/europepmc/crossref for full author lists and abstracts.
        """
    )
    async def get_abstract(self, params: AbstractParams) -> ToolResult:
        ident = params.identifier.strip()
        kind = params.id_type
        if kind == "auto":
            if ident.lower().startswith("2-s2.0-"):
                kind = "eid"
            elif re.fullmatch(r"\d{1,9}", ident):
                kind = "pmid"
            elif re.fullmatch(r"\d{10,}", ident):
                kind = "scopus_id"
            else:
                kind = "doi"
        if kind == "doi":
            ident = normalize_doi(ident)
        path = {"doi": "doi", "eid": "eid", "pmid": "pubmed_id", "scopus_id": "scopus_id"}[kind]
        response = await self._els(
            "GET", f"{API}/abstract/{path}/{quote(ident, safe='/')}", "Scopus Abstract Retrieval", allow_status=(404,)
        )
        if response.status == 404:
            return ToolResult(
                source=self.name, tool="get_abstract", request_url=response.url,
                notes=[f"{kind} {ident} not found in Scopus"],
            )
        payload = response.json().get("abstracts-retrieval-response") or {}
        record = _abstract_record(payload)
        notes = [] if record.summary else [
            "abstract and full author list not included (Abstract Retrieval META view entitlement)"
        ]
        return ToolResult(
            source=self.name, tool="get_abstract", records=[record], total=1, request_url=response.url, notes=notes
        )

    @tool(
        description="""
        Journal metrics from Scopus Serial Title API: CiteScore (current + tracker), SNIP and SJR with
        their years, publisher, subject areas, coverage years and open-access status. Look up by ISSN
        (exact, e.g. 0092-8674) or by title words (e.g. 'nature medicine'). Use to judge journal impact
        or to resolve a journal name to its ISSN.
        """
    )
    async def serial_metrics(self, params: SerialParams) -> ToolResult:
        if params.issn:
            issn = _issn(params.issn.replace("-", "")) or params.issn.strip()
            response = await self._els(
                "GET", f"{API}/serial/title/issn/{quote(issn, safe='')}", "Scopus Serial Title", allow_status=(404, 400)
            )
        elif params.title:
            response = await self._els(
                "GET", f"{API}/serial/title", "Scopus Serial Title",
                params={"title": params.title, "count": params.limit}, allow_status=(404, 400),
            )
        else:
            raise SourceError(self.name, "serial_metrics needs issn or title")
        body = response.json() if response.status == 200 else {}
        entries = [e for e in _list((body.get("serial-metadata-response") or {}).get("entry")) if isinstance(e, dict)]
        records = [_serial_record(e) for e in entries[: params.limit]]
        return ToolResult(
            source=self.name,
            tool="serial_metrics",
            records=records,
            total=len(records),
            request_url=response.url,
            notes=[] if records else [f"no Scopus journal found for {params.issn or params.title!r}"],
        )

    @tool(
        description="""
        Retrieve an Elsevier/ScienceDirect article (Article Retrieval API) by DOI, PII or PMID: metadata,
        abstract, keywords, open-access status and, for open-access / open-archive articles, the full
        text as plain text in data.text (truncated to max_chars). Use when you need the body text of an
        Elsevier article (Cell, Lancet, journals on sciencedirect.com). Subscription-only articles return
        metadata only unless the key carries an institutional entitlement.
        """
    )
    async def get_full_text(self, params: FullTextParams) -> ToolResult:
        ident = params.identifier.strip()
        kind = params.id_type
        if kind == "auto":
            if re.fullmatch(r"[SB][\dX]{16}", re.sub(r"[^0-9A-Za-z]", "", ident), re.I):
                kind = "pii"
            elif re.fullmatch(r"\d{1,9}", ident):
                kind = "pmid"
            else:
                kind = "doi"
        if kind == "doi":
            ident = normalize_doi(ident)
        elif kind == "pii":
            ident = re.sub(r"[^0-9A-Za-z]", "", ident).upper()
        path = {"doi": "doi", "pii": "pii", "pmid": "pubmed_id"}[kind]
        response = await self._els(
            "GET",
            f"{API}/article/{path}/{quote(ident, safe='/')}",
            "ScienceDirect Article Retrieval",
            allow_status=(404,),
        )
        if response.status == 404:
            return ToolResult(
                source=self.name, tool="get_full_text", request_url=response.url,
                notes=[f"{kind} {ident} not found on ScienceDirect (only Elsevier-published articles are available)"],
            )
        payload = response.json().get("full-text-retrieval-response") or {}
        core = payload.get("coredata") or {}
        doi = core.get("prism:doi")
        abstract = strip_html(_text(core.get("dc:description")))
        issns = _issns(core.get("prism:issn"))
        creators = [_text(c) for c in _list(core.get("dc:creator"))]
        authors = []
        for c in creators:
            if c:
                last, _, given = c.partition(", ")
                authors.append(f"{given} {last}".strip() if given else c)
        pii = re.sub(r"[^0-9A-Za-z]", "", core.get("pii") or "")
        record = Record(
            source=self.name,
            id=pii or doi or ident,
            type="article",
            title=strip_html(core.get("dc:title")),
            url=f"https://www.sciencedirect.com/science/article/pii/{pii}" if pii else (f"https://doi.org/{doi}" if doi else None),
            identifiers={
                "doi": doi,
                "pmid": _text(payload.get("pubmed-id")),
                "scopus_eid": _text(payload.get("scopus-eid")),
                "issn": first(issns),
            },
            date=core.get("prism:coverDate"),
            authors=authors,
            summary=abstract,
            attributes=_clean(
                {
                    "journal": core.get("prism:publicationName"),
                    "publisher": core.get("prism:publisher"),
                    "volume": core.get("prism:volume"),
                    "issue": core.get("prism:issueIdentifier"),
                    "pages": core.get("prism:pageRange"),
                    "open_access": bool(core.get("openaccessArticle")) or core.get("openaccess") == "1",
                    "open_access_type": core.get("openaccessType"),
                    "license": core.get("openaccessUserLicense"),
                    "keywords": [_text(k) for k in _list(core.get("dcterms:subject")) if _text(k)][:20],
                    "pii": core.get("pii"),
                    "scopus_id": _text(payload.get("scopus-id")),
                }
            ),
        )
        text = _full_text(payload.get("originalText"), abstract)
        notes: list[str] = []
        data: dict[str, Any] | None = None
        if text:
            truncated = len(text) > params.max_chars
            data = {"text": text[: params.max_chars], "chars": len(text), "truncated": truncated}
            record.attributes["full_text_chars"] = len(text)
            if truncated:
                notes.append(f"full text truncated to {params.max_chars} of {len(text)} characters; raise max_chars for more")
        else:
            notes.append("full text not available for this article with this key (not open access / not entitled); metadata only")
        return ToolResult(
            source=self.name, tool="get_full_text", records=[record], total=1, data=data,
            request_url=response.url, notes=notes,
        )

    @tool(
        description="""
        Find Scopus author profiles (Author Search API) by surname plus optional given name / affiliation,
        or by ORCID: Scopus author ID, preferred name, ORCID, current affiliation, document count and
        subject areas. Requires the Author Search entitlement (institutional); keys without it get a
        clear entitlement error. For free author disambiguation prefer openalex or orcid.
        """
    )
    async def search_authors(self, params: AuthorSearchParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        if params.orcid:
            query = f"ORCID({params.orcid.strip()})"
        elif not params.last_name:
            raise SourceError(self.name, "search_authors needs last_name or orcid")
        else:
            parts = [f"AUTHLASTNAME({params.last_name})"]
            if params.first_name:
                parts.append(f"AUTHFIRST({params.first_name})")
            if params.affiliation:
                parts.append(f"AFFIL({params.affiliation})")
            query = " AND ".join(parts)
        response = await self._els(
            "GET", f"{API}/search/author", "Scopus Author Search",
            params={"query": query, "start": offset, "count": params.limit},
        )
        results = response.json().get("search-results") or {}
        entries = [e for e in _list(results.get("entry")) if isinstance(e, dict) and not e.get("error")]
        records = []
        for e in entries:
            author_id = (e.get("dc:identifier") or "").replace("AUTHOR_ID:", "")
            name = e.get("preferred-name") or {}
            current = e.get("affiliation-current") or {}
            records.append(
                Record(
                    source=self.name,
                    id=author_id,
                    type="author",
                    title=" ".join(x for x in (name.get("given-name"), name.get("surname")) if x) or None,
                    url=f"https://www.scopus.com/authid/detail.uri?authorId={author_id}" if author_id else None,
                    identifiers={"orcid": e.get("orcid")},
                    attributes=_clean(
                        {
                            "scopus_author_id": author_id,
                            "document_count": _int(e.get("document-count")),
                            "affiliation": ", ".join(
                                x for x in (current.get("affiliation-name"), current.get("affiliation-city"),
                                            current.get("affiliation-country")) if x
                            ),
                            "subject_areas": [s.get("$") for s in _list(e.get("subject-area")) if isinstance(s, dict)][:8],
                        }
                    ),
                )
            )
        total = _int(results.get("opensearch:totalResults"))
        nxt = offset + len(records)
        return ToolResult(
            source=self.name, tool="search_authors", records=records, total=total,
            next_cursor=str(nxt) if records and total and nxt < total else None, request_url=response.url,
        )

    @tool(
        description="""
        Search ScienceDirect full-text articles (ScienceDirect Search API v2, Elsevier journals and books)
        by free text over full text: title, journal, date, DOI, PII and open-access flag. Requires the
        ScienceDirect Search entitlement (institutional); keys without it get a clear entitlement error,
        in which case use the scopus search tool instead.
        """
    )
    async def sciencedirect_search(self, params: ScienceDirectSearchParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        body: dict[str, Any] = {
            "qs": params.query,
            "display": {"offset": offset, "show": params.limit, "sortBy": "relevance"},
        }
        if params.year_from or params.year_to:
            body["date"] = f"{params.year_from or 1823}-{params.year_to or 2100}"
        if params.open_access:
            body["filters"] = {"openAccess": True}
        response = await self._els(
            "PUT", f"{API}/search/sciencedirect", "ScienceDirect Search", json_body=body, cache_ttl=0
        )
        payload = response.json()
        records = []
        for item in payload.get("results") or []:
            pii = item.get("pii") or ""
            authors = sorted(_list(item.get("authors")), key=lambda a: a.get("order", 0) if isinstance(a, dict) else 0)
            pages = item.get("pages") or {}
            records.append(
                Record(
                    source=self.name,
                    id=pii or item.get("doi") or "",
                    type="article",
                    title=strip_html(item.get("title")),
                    url=item.get("uri") or (f"https://www.sciencedirect.com/science/article/pii/{pii}" if pii else None),
                    identifiers={"doi": item.get("doi")},
                    date=(item.get("publicationDate") or "")[:10] or None,
                    authors=[a.get("name") for a in authors if isinstance(a, dict) and a.get("name")],
                    attributes=_clean(
                        {
                            "journal": item.get("sourceTitle"),
                            "volume_issue": item.get("volumeIssue"),
                            "pages": "-".join(x for x in (pages.get("first"), pages.get("last")) if x),
                            "open_access": item.get("openAccess"),
                            "pii": pii,
                        }
                    ),
                )
            )
        total = _int(payload.get("resultsFound"))
        nxt = offset + len(records)
        return ToolResult(
            source=self.name, tool="sciencedirect_search", records=records, total=total,
            next_cursor=str(nxt) if records and total and nxt < min(total, 6000) else None,
            request_url=response.url,
        )
