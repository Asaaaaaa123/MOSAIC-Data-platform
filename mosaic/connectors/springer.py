"""Springer Nature APIs connector (Meta API v2 + Open Access API).

Docs: https://dev.springernature.com/ (Meta API v2 ``/meta/v2/json``, Open Access API
``/openaccess/json`` and ``/openaccess/jats``; query constraints doc at
https://docs-dev.springernature.com/docs/).

Auth: two separate keys, each valid only for its own API (probed: the Meta key gets 401
"API key is invalid or missing" on /openaccess and vice versa):

* ``SPRINGER_META_API_KEY``  -> Meta API v2 (all Springer Nature content: ~16M journal
  articles, book chapters, protocols, conference papers).
* ``SPRINGER_OPENACCESS_API_KEY`` -> Open Access API (OA subset, incl. full-text JATS XML).

The key must be the ``api_key`` query parameter (no header option); ``api_key`` is in
``http.SECRET_PARAM_NAMES`` so it is redacted from request URLs and audit logs.

Limits (basic plan): page size ``p`` max 25 (p=50 -> 403 "This is a premium feature"),
start index ``s`` is 1-based; daily call quotas apply. Searches with zero hits return HTTP 404
``{"status": "Fail", "message": "No data was found for the given query."}``, handled as empty.

Query syntax: free words / "quoted phrases" plus constraints. Probed on this plan: ``keyword:``,
``doi:``, ``journalid:``, ``issn:``, ``type:Journal|Book``, ``datefrom:``/``dateto:`` and
``onlinedatefrom:``/``onlinedateto:`` (YYYY-MM-DD) work; ``year:``, ``subject:``, ``openaccess:``,
``title:`` and ``name:`` return 403 "premium feature", so ``year`` is sent as datefrom/dateto and
``open_access=true`` is served by the Open Access API instead.

Quirks: ``abstract`` is a string in Meta v2 but ``{"h1": "Abstract", "p": ...}`` in the OA API;
``genre`` may be a string or list; books carry ``isbn`` / ``printIsbn`` / ``electronicIsbn``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Literal

from pydantic import Field

from ..http import HttpResponse, NotConfiguredError, SourceError
from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, first, parse_offset, strip_html, tool

API = "https://api.springernature.com"
META = f"{API}/meta/v2/json"
OA_JSON = f"{API}/openaccess/json"
OA_JATS = f"{API}/openaccess/jats"
PAGE_MAX = 25


# ---------------------------------------------------------------- params
class SpringerSearchParams(SearchParams):
    limit: int = Field(10, ge=1, le=PAGE_MAX, description="Maximum number of records to return (max 25 on the basic plan)")
    year: int | None = Field(None, description="Only content published in this year")
    date_from: str | None = Field(None, description="Online date from, YYYY-MM-DD")
    date_to: str | None = Field(None, description="Online date to, YYYY-MM-DD")
    content_type: Literal["Journal", "Book"] | None = Field(
        None, description="Journal = journal articles only; Book = book chapters/protocols/conference papers"
    )
    open_access: bool | None = Field(
        None, description="true = search only the open-access collection (uses the Open Access API)"
    )


class OpenAccessSearchParams(SearchParams):
    limit: int = Field(10, ge=1, le=PAGE_MAX, description="Maximum number of records to return (max 25)")
    year: int | None = Field(None, description="Only content published in this year")
    date_from: str | None = Field(None, description="Online date from, YYYY-MM-DD")
    date_to: str | None = Field(None, description="Online date to, YYYY-MM-DD")
    content_type: Literal["Journal", "Book"] | None = Field(None, description="Journal or Book content only")


class DoiParams(ToolParams):
    doi: str = Field(description="DOI of a Springer Nature / BMC / Nature item, with or without https://doi.org/")


class FacetParams(ToolParams):
    query: str = Field(description="Springer query (same syntax as search), e.g. 'keyword:\"CAR-T\" year:2024'")
    facets: list[Literal["subject", "keyword", "pub", "year", "country", "type"]] = Field(
        default_factory=lambda: ["subject", "year", "type"],
        description="Which facets to return: subject, keyword, pub (journal/book title), year, country, type",
    )
    top: int = Field(10, ge=1, le=50, description="Maximum values per facet")


class FullTextParams(ToolParams):
    doi: str = Field(description="DOI of an open-access Springer Nature / BMC article, e.g. 10.1186/s13059-016-1012-2")
    max_chars: int = Field(20000, ge=500, le=100000, description="Truncate the total section text to this many characters")


# ---------------------------------------------------------------- helpers
def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _person(name: str) -> str:
    """'Wang, Hao' -> 'Hao Wang'."""
    last, sep, given = name.partition(", ")
    return f"{given} {last}".strip() if sep else name.strip()


def _abstract(value: Any) -> str | None:
    if isinstance(value, dict):
        parts = []
        for key in ("p", "sec"):
            for item in _list(value.get(key)):
                parts.append(_abstract(item) or "")
        return strip_html(" ".join(p for p in parts if p))
    if isinstance(value, list):
        return strip_html(" ".join(a for a in (_abstract(v) for v in value) if a))
    return strip_html(value) if isinstance(value, str) else None


def _record(item: dict[str, Any]) -> Record:
    doi = item.get("doi") or (item.get("identifier") or "").removeprefix("doi:")
    content_type = item.get("contentType") or ""
    oa = item.get("openaccess", item.get("openAccess"))
    html_url = next(
        (u.get("value") for u in _list(item.get("url")) if isinstance(u, dict) and u.get("format") == "html"), None
    )
    isbn = item.get("isbn") or item.get("printIsbn") or item.get("electronicIsbn")
    conference = first([c.get("confEventAbbreviation") or c.get("confSeriesName")
                        for c in _list(item.get("conferenceInfo")) if isinstance(c, dict)])
    pages = "-".join(x for x in (item.get("startingPage"), item.get("endingPage")) if x)
    return Record(
        source="springer",
        id=doi or item.get("identifier", ""),
        type="article" if content_type.lower() == "article" or item.get("publicationType") == "Journal" else "book",
        title=strip_html(item.get("title")),
        url=f"https://doi.org/{doi}" if doi else html_url,
        identifiers={
            "doi": doi,
            "issn": item.get("issn") or item.get("eIssn"),
            "isbn": isbn,
        },
        date=item.get("publicationDate") or item.get("onlineDate"),
        authors=[_person(c.get("creator", "")) for c in _list(item.get("creators")) if isinstance(c, dict) and c.get("creator")],
        summary=_abstract(item.get("abstract")),
        attributes=_clean(
            {
                "journal": item.get("publicationName"),
                "publisher": item.get("publisherName") or item.get("publisher"),
                "content_type": content_type,
                "genre": ", ".join(_list(item.get("genre"))) or None,
                "open_access": str(oa).lower() == "true" if oa is not None else None,
                "volume": item.get("volume"),
                "issue": item.get("number"),
                "pages": pages,
                "online_date": item.get("onlineDate"),
                "eissn": item.get("eIssn") if item.get("issn") else None,
                "electronic_isbn": item.get("electronicIsbn") if item.get("electronicIsbn") != isbn else None,
                "journal_id": item.get("journalId"),
                "keywords": _list(item.get("keyword"))[:15],
                "subjects": list(dict.fromkeys(_list(item.get("subjects"))))[:10],
                "conference": conference,
                "book_editors": [_person(e.get("bookEditor", "")) for e in _list(item.get("bookEditors"))
                                 if isinstance(e, dict) and e.get("bookEditor")][:6],
                "language": item.get("language"),
            }
        ),
    )


def _query(params: SpringerSearchParams | OpenAccessSearchParams) -> str:
    # year:, subject:, openaccess:, title: and name: are premium constraints (HTTP 403 on the basic
    # plan), so a year filter is expressed with the free datefrom:/dateto: constraints.
    parts = [params.query.strip()]
    if params.year:
        parts.append(f"datefrom:{params.year}-01-01 dateto:{params.year}-12-31")
    if params.date_from:
        parts.append(f"onlinedatefrom:{params.date_from}")
    if params.date_to:
        parts.append(f"onlinedateto:{params.date_to}")
    if params.content_type:
        parts.append(f"type:{params.content_type}")
    return " ".join(p for p in parts if p)


def _total(payload: dict[str, Any]) -> int | None:
    return _int((first(payload.get("result")) or {}).get("total"))


# ---- JATS ----
_SKIP_TAGS = {"table-wrap", "table", "disp-formula", "inline-formula", "graphic", "media", "xref",
              "supplementary-material", "fn-group", "ref-list", "alternatives", "object-id"}


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text_of(node: ET.Element, skip_titles: bool = False) -> str:
    out: list[str] = []

    def walk(el: ET.Element, top: bool) -> None:
        name = _local(el.tag)
        if name in _SKIP_TAGS or name == "math":
            if el.tail:
                out.append(el.tail)
            return
        if not (top and skip_titles and name == "title"):
            if el.text:
                out.append(el.text)
            for child in el:
                walk(child, False)
        if el.tail and not top:
            out.append(el.tail)

    for child in node:
        if skip_titles and _local(child.tag) == "title":
            continue
        walk(child, False)
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def _parse_jats(xml_text: str) -> ET.Element | None:
    cleaned = re.sub(r"<!DOCTYPE[^\[>]*(\[.*?\])?\s*>", "", xml_text, count=1, flags=re.S)
    cleaned = re.sub(r"<\?xml-stylesheet[^>]*\?>", "", cleaned)
    try:
        root = ET.fromstring(cleaned.encode("utf-8"))
    except ET.ParseError:
        return None
    for el in root.iter():
        if _local(el.tag) in ("article", "book-part-wrapper", "book"):
            return el
    return None


def _jats_record(article: ET.Element, doi: str) -> tuple[Record, list[dict[str, str]]]:
    def find(path_names: list[str]) -> ET.Element | None:
        for el in article.iter():
            if _local(el.tag) in path_names:
                return el
        return None

    ids: dict[str, str] = {}
    for el in article.iter():
        if _local(el.tag) == "article-id" and el.text:
            ids.setdefault(el.get("pub-id-type", ""), el.text.strip())
    title_el = find(["article-title", "chapter-title"])
    journal_el = find(["journal-title", "book-title"])
    issn_el = find(["issn"])
    authors = []
    for contrib in article.iter():
        if _local(contrib.tag) == "contrib" and contrib.get("contrib-type") == "author":
            given = surname = None
            for el in contrib.iter():
                if _local(el.tag) == "given-names":
                    given = (el.text or "").strip()
                elif _local(el.tag) == "surname":
                    surname = (el.text or "").strip()
            name = " ".join(x for x in (given, surname) if x)
            if name:
                authors.append(name)
    date = None
    for el in article.iter():
        if _local(el.tag) == "pub-date":
            parts = {(_local(c.tag)): (c.text or "").strip() for c in el}
            if parts.get("year"):
                date = "-".join(
                    [parts["year"]] + [parts[k].zfill(2) for k in ("month", "day") if parts.get(k, "").isdigit()]
                )
                break
    abstract_el = find(["abstract"])
    sections: list[dict[str, str]] = []
    if abstract_el is not None:
        sections.append({"title": "Abstract", "text": _text_of(abstract_el, skip_titles=True)})
    body = find(["body"])
    if body is not None:
        loose = []
        for child in body:
            if _local(child.tag) == "sec":
                title = next((_text_of_title(c) for c in child if _local(c.tag) == "title"), "") or "Section"
                sections.append({"title": title, "text": _text_of(child, skip_titles=True)})
            else:
                loose.append(_text_of(child) if len(child) else re.sub(r"\s+", " ", child.text or "").strip())
        if any(loose):
            sections.insert(1 if abstract_el is not None else 0, {"title": "Body", "text": " ".join(x for x in loose if x)})
    back = find(["back"])
    if back is not None:
        # Back-matter sections: acknowledgements, funding, data availability (dataset accessions), ...
        parent = {id(c): p for p in back.iter() for c in p}
        for el in back.iter():
            name = _local(el.tag)
            up = parent.get(id(el))
            title = next((_text_of_title(c) for c in el if _local(c.tag) == "title"), "")
            if name == "ack" and not any(_local(c.tag) == "sec" for c in el):
                sections.append({"title": title or "Acknowledgements", "text": _text_of(el, skip_titles=True)})
            elif name == "sec" and (up is None or _local(up.tag) != "sec"):
                in_ack = up is not None and _local(up.tag) == "ack"
                sections.append({"title": title or ("Acknowledgements" if in_ack else "Notes"),
                                 "text": _text_of(el, skip_titles=True)})
    record = Record(
        source="springer",
        id=ids.get("doi") or doi,
        type="article" if _local(article.tag) == "article" else "book",
        title=_text_of_title(title_el) if title_el is not None else None,
        url=f"https://doi.org/{ids.get('doi') or doi}",
        identifiers={"doi": ids.get("doi") or doi, "pmid": ids.get("pmid"), "pmcid": ids.get("pmcid"),
                     "issn": (issn_el.text or "").strip() if issn_el is not None else None},
        date=date,
        authors=authors,
        summary=sections[0]["text"] if sections and sections[0]["title"] == "Abstract" else None,
        attributes=_clean(
            {
                "journal": (journal_el.text or "").strip() if journal_el is not None else None,
                "article_type": article.get("article-type"),
                "open_access": True,
                "section_titles": [s["title"] for s in sections][:40],
            }
        ),
    )
    return record, [s for s in sections if s["text"]]


def _text_of_title(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


class Springer(Connector):
    name = "springer"
    title = "Springer Nature"
    category = "literature"
    description = (
        "Springer Nature metadata (~16M journal articles, book chapters, protocols incl. Nature, BMC, "
        "Springer journals) with abstracts, keywords and subjects, plus open-access full text (JATS) for "
        "the OA subset. Best for Springer/Nature/BMC content, Springer Protocols and OA full text."
    )
    homepage = "https://dev.springernature.com/"
    docs_url = "https://docs-dev.springernature.com/docs/"
    auth = "api_key"
    required_settings = ("springer_meta_api_key", "springer_openaccess_api_key")
    rate_limit = 1.0
    health_query = "CRISPR"

    def missing_settings(self) -> list[str]:
        # Each API has its own key; the connector is usable when either is configured.
        missing = [s.upper() for s in self.required_settings if not self.setting(s)]
        return missing if len(missing) == len(self.required_settings) else []

    def _key(self, setting: str, api: str) -> str:
        key = self.setting(setting)
        if not key:
            raise NotConfiguredError(self.name, f"{api} needs {setting.upper()} in .env")
        return key

    async def _get(self, url: str, setting: str, api: str, params: dict[str, Any]) -> HttpResponse:
        try:
            return await self.request(
                "GET", url, params={**params, "api_key": self._key(setting, api)}, allow_status=(404,)
            )
        except SourceError as exc:
            if exc.status in (401, 403):
                match = re.search(r'"message"\s*:\s*"([^"]+)"', exc.message)
                detail = match.group(1) if match else "access denied"
                raise SourceError(
                    self.name,
                    f"Springer Nature {api} refused the request (HTTP {exc.status}: {detail}). "
                    f"Check {setting.upper()} is a key for the {api}; page sizes above 25 and some "
                    "constraints are premium-plan features.",
                    status=exc.status,
                    url=exc.url,
                ) from exc
            raise

    async def _search(self, url: str, setting: str, api: str, tool_name: str, q: str, limit: int, cursor: str | None) -> ToolResult:
        offset = parse_offset(cursor)
        response = await self._get(url, setting, api, {"q": q, "s": offset + 1, "p": limit})
        if response.status == 404:
            return ToolResult(source=self.name, tool=tool_name, total=0, request_url=response.url,
                              notes=[f"no Springer Nature records matched {q!r}"])
        payload = response.json()
        items = [r for r in payload.get("records") or [] if isinstance(r, dict)]
        total = _total(payload)
        nxt = offset + len(items)
        return ToolResult(
            source=self.name,
            tool=tool_name,
            records=[_record(r) for r in items],
            total=total,
            next_cursor=str(nxt) if items and total and nxt < total else None,
            request_url=response.url,
        )

    # ------------------------------------------------------------ tools
    @tool(
        description="""
        Search Springer Nature content (Meta API v2: Nature, BMC, Springer journals, books, Springer
        Protocols, conference papers) and return DOI, title, authors, journal/book, ISSN/ISBN, dates,
        abstract, keywords, subjects and open-access flag. Use for Springer/Nature/BMC literature and
        lab protocols; prefer pubmed/europepmc for comprehensive biomedical searches. Free words and
        "quoted phrases" are ANDed; supported constraints: keyword:CRISPR, journalid:41586 (Nature),
        issn:1474-760X, doi:, type:Journal|Book, datefrom:/dateto:/onlinedatefrom:/onlinedateto:
        YYYY-MM-DD, or use the year / date_from / date_to / content_type / open_access params.
        Example: 'keyword:"prime editing" "sickle cell"'. The title:, name: (author), subject:, year:
        and openaccess: constraints are premium-plan only and fail with HTTP 403. Max 25 per page.
        """,
        federated=True,
    )
    async def search(self, params: SpringerSearchParams) -> ToolResult:
        if params.open_access:
            return await self._search(
                OA_JSON, "springer_openaccess_api_key", "Open Access API", "search",
                _query(params), params.limit, params.cursor,
            )
        return await self._search(
            META, "springer_meta_api_key", "Meta API", "search", _query(params), params.limit, params.cursor
        )

    @tool(
        description="""
        Fetch Springer Nature metadata for one DOI (Meta API v2): title, authors, journal/book, ISSN/ISBN,
        dates, abstract, keywords, subjects, open-access flag. Use to resolve a Springer/Nature/BMC DOI
        (prefixes 10.1007, 10.1038, 10.1186, 10.1057...). Other publishers' DOIs return not-found.
        """
    )
    async def get_by_doi(self, params: DoiParams) -> ToolResult:
        doi = normalize_doi(params.doi)
        result = await self._search(META, "springer_meta_api_key", "Meta API", "get_by_doi", f'doi:"{doi}"', 1, None)
        if not result.records:
            result.notes = [f"DOI {doi} not found in Springer Nature metadata"]
        result.next_cursor = None
        return result

    @tool(
        description="""
        Search only the open-access Springer Nature / BMC collection (Open Access API) with the same query
        syntax as search; every hit has free full text retrievable with get_open_access_full_text. Use
        when you need OA papers whose full text can be read. Max 25 per page.
        """
    )
    async def search_open_access(self, params: OpenAccessSearchParams) -> ToolResult:
        return await self._search(
            OA_JSON, "springer_openaccess_api_key", "Open Access API", "search_open_access",
            _query(params), params.limit, params.cursor,
        )

    @tool(
        description="""
        Get the full text of an OPEN-ACCESS Springer Nature / BMC article by DOI (Open Access API, JATS XML
        converted to plain-text sections: Abstract, Background/Introduction, Results, Methods, ...),
        returned in data.sections and truncated to max_chars. Also returns the article metadata record.
        Use to read methods/results of OA papers (e.g. Genome Biology, BMC, Scientific Reports, Nature
        Communications). Non-OA DOIs return not-found.
        """
    )
    async def get_open_access_full_text(self, params: FullTextParams) -> ToolResult:
        doi = normalize_doi(params.doi)
        response = await self._get(OA_JATS, "springer_openaccess_api_key", "Open Access API", {"q": f'doi:"{doi}"'})
        article = _parse_jats(response.text) if response.status == 200 else None
        if article is None:
            return ToolResult(
                source=self.name, tool="get_open_access_full_text", request_url=response.url,
                notes=[f"DOI {doi} not found in the Springer Nature open-access collection (or not parseable)"],
            )
        record, sections = _jats_record(article, doi)
        budget = params.max_chars
        out_sections: list[dict[str, str]] = []
        total_chars = sum(len(s["text"]) for s in sections)
        for section in sections:
            if budget <= 0:
                break
            text = section["text"]
            out_sections.append({"title": section["title"], "text": text[:budget]})
            budget -= len(text)
        truncated = total_chars > params.max_chars
        notes = []
        if truncated:
            notes.append(f"full text truncated to {params.max_chars} of {total_chars} characters; raise max_chars for more")
        if not any(s["title"] != "Abstract" for s in sections):
            notes.append("no body text in the JATS record (abstract/metadata only)")
        record.attributes["full_text_chars"] = total_chars
        return ToolResult(
            source=self.name,
            tool="get_open_access_full_text",
            records=[record],
            total=1,
            data={"sections": out_sections, "chars": total_chars, "truncated": truncated},
            request_url=response.url,
            notes=notes,
        )

    @tool(
        description="""
        Count Springer Nature content matching a query broken down by facet (Meta API v2 facets): subject,
        keyword, pub (journal/book title), year, country (author affiliation) and type. Returns
        data.facets {facet: [{value, count}]} and the total hit count. Use for landscape questions such
        as 'which journals / years / countries publish most on X'.
        """
    )
    async def count_facets(self, params: FacetParams) -> ToolResult:
        response = await self._get(META, "springer_meta_api_key", "Meta API", {"q": params.query, "s": 1, "p": 1})
        if response.status == 404:
            return ToolResult(source=self.name, tool="count_facets", total=0, data={"facets": {}},
                              request_url=response.url, notes=[f"no Springer Nature records matched {params.query!r}"])
        payload = response.json()
        facets: dict[str, list[dict[str, Any]]] = {}
        wanted = set(params.facets)
        for facet in payload.get("facets") or []:
            if not isinstance(facet, dict) or facet.get("name") not in wanted:
                continue
            facets[facet["name"]] = [
                {"value": v.get("value"), "count": _int(v.get("count"))}
                for v in (facet.get("values") or [])[: params.top]
                if isinstance(v, dict)
            ]
        return ToolResult(
            source=self.name, tool="count_facets", total=_total(payload),
            data={"facets": facets}, request_url=response.url,
        )
