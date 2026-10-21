"""OpenAlex connector (works, authors, institutions, bibliometric aggregations).

Docs: https://docs.openalex.org/ (API keys / pricing: https://docs.openalex.org/how-to-use-the-api/api-key).

Auth: optional. ``OPENALEX_API_KEY`` is sent as the ``api_key`` query parameter
(redacted by the HTTP layer). Without a key requests share a small free daily
budget per IP ($0.10/day); a free key gets $1/day. Costs are per request:
list/filter/group_by calls ~$0.0001, ``search=`` calls ~$0.001, single-entity
GETs free. ``mailto`` is added when BIOPHA_CONTACT_EMAIL is set (redacted).

Rate limits: 10 requests/second max; the connector uses 8.

Quirks:
- Abstracts are delivered as ``abstract_inverted_index`` and reconstructed here.
  Some publishers elide them (null).
- ``ids.pmcid`` is currently not populated (``has_pmcid:true`` matches nothing);
  the PMCID is recovered from PubMed Central locations
  (``pmh:oai:pubmedcentral.nih.gov:<n>`` / ``.../pmc/articles/<n>``), and
  ``get_work`` resolves PMCIDs through a PMC landing-page filter.
- Cursor pagination (``cursor=*``) is used for list endpoints; ``per-page`` max 200.
- ``group_by`` returns at most 200 groups per page.
- ``?`` and ``*`` (wildcards) make OpenAlex reject a search with HTTP 400; they
  are stripped from free-text queries. Commas are replaced in filter searches
  (comma means AND between filters).

Identifiers: openalex (W/A/I/S ids), doi, pmid, pmcid; orcid for authors; ror
for institutions; issn (ISSN-L) for a work's journal. NIH/other grant numbers
from ``awards`` go in ``attributes.grants``.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from pydantic import Field

from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, first, strip_html, tool

API = "https://api.openalex.org"
WEB = "https://openalex.org"

WORK_SELECT = (
    "id,doi,display_name,publication_year,publication_date,ids,type,language,primary_location,locations,"
    "best_oa_location,open_access,authorships,cited_by_count,fwci,biblio,is_retracted,primary_topic,topics,"
    "keywords,mesh,abstract_inverted_index,awards,funders,referenced_works_count,"
    "citation_normalized_percentile"
)

_WORK_TYPES = {
    "article": "article",
    "review": "article",
    "letter": "article",
    "editorial": "article",
    "erratum": "article",
    "preprint": "preprint",
    "book": "book",
    "book-chapter": "book",
    "monograph": "book",
    "reference-entry": "book",
    "dataset": "dataset",
    "review-article": "article",
    "retraction": "article",
    "report": "article",
}


def short_id(value: str | None) -> str:
    """https://openalex.org/W123 -> W123."""
    if not value:
        return ""
    return value.rstrip("/").rsplit("/", 1)[-1]


def reconstruct_abstract(index: dict[str, list[int]] | None) -> str | None:
    if not index:
        return None
    positions: list[tuple[int, str]] = []
    for word, idxs in index.items():
        for i in idxs or []:
            positions.append((i, word))
    if not positions:
        return None
    positions.sort()
    return strip_html(" ".join(w for _, w in positions))


_PMC_RE = re.compile(r"(?:pubmedcentral\.nih\.gov:|/pmc/articles/(?:PMC)?)(\d+)", re.IGNORECASE)


def _pmcid(work: dict[str, Any]) -> str | None:
    ids = work.get("ids") or {}
    if ids.get("pmcid"):
        return short_id(ids["pmcid"])
    for loc in [work.get("best_oa_location") or {}] + list(work.get("locations") or []):
        for value in (loc.get("id"), loc.get("landing_page_url"), loc.get("pdf_url")):
            match = _PMC_RE.search(value or "")
            if match:
                return f"PMC{match.group(1)}"
    return None


def clean_query(query: str | None) -> str:
    """Drop characters OpenAlex search rejects (``?``/``*`` wildcards -> HTTP 400)."""
    return re.sub(r"\s+", " ", re.sub(r"[?*]", " ", query or "")).strip()


def _compact(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v not in (None, [], "", {})}


def work_record(work: dict[str, Any]) -> Record:
    wid = short_id(work.get("id"))
    ids = work.get("ids") or {}
    loc = work.get("primary_location") or {}
    src = loc.get("source") or {}
    oa = work.get("open_access") or {}
    biblio = work.get("biblio") or {}
    pages = "-".join(p for p in (biblio.get("first_page"), biblio.get("last_page")) if p) or None
    authorships = work.get("authorships") or []
    institutions: list[str] = []
    for a in authorships:
        for inst in a.get("institutions") or []:
            name = inst.get("display_name")
            if name and name not in institutions:
                institutions.append(name)
    mesh = []
    for m in work.get("mesh") or []:
        name = m.get("descriptor_name")
        if name and name not in mesh:
            mesh.append(name)
    mesh_ids = []
    for m in work.get("mesh") or []:
        ui = m.get("descriptor_ui")
        if ui and ui not in mesh_ids:
            mesh_ids.append(ui)
    topic = work.get("primary_topic") or {}
    percentile = work.get("citation_normalized_percentile") or {}
    pmid = ids.get("pmid")
    return Record(
        source="openalex",
        id=wid,
        type=_WORK_TYPES.get(work.get("type") or "", "other"),  # type: ignore[arg-type]
        title=strip_html(work.get("display_name") or work.get("title")),
        url=f"{WEB}/{wid}" if wid else None,
        identifiers={
            "openalex": wid,
            "doi": work.get("doi") or ids.get("doi"),
            "pmid": short_id(pmid) if pmid else None,
            "pmcid": _pmcid(work),
            "issn": src.get("issn_l") or first(src.get("issn")),
        },
        date=work.get("publication_date") or (str(work["publication_year"]) if work.get("publication_year") else None),
        authors=[(a.get("author") or {}).get("display_name") for a in authorships if (a.get("author") or {}).get("display_name")],
        summary=reconstruct_abstract(work.get("abstract_inverted_index")),
        attributes=_compact(
            {
                "work_type": work.get("type"),
                "journal": src.get("display_name"),
                "source_id": short_id(src.get("id")) or None,
                "publisher": src.get("host_organization_name"),
                "volume": biblio.get("volume"),
                "issue": biblio.get("issue"),
                "pages": pages,
                "language": work.get("language"),
                "cited_by_count": work.get("cited_by_count"),
                "fwci": work.get("fwci"),
                "citation_percentile": percentile.get("value"),
                "referenced_works_count": work.get("referenced_works_count"),
                "is_oa": oa.get("is_oa"),
                "oa_status": oa.get("oa_status"),
                "oa_url": oa.get("oa_url"),
                "is_retracted": work.get("is_retracted") or None,
                "primary_topic": topic.get("display_name"),
                "primary_topic_id": short_id(topic.get("id")) or None,
                "field": (topic.get("field") or {}).get("display_name"),
                "topics": [t.get("display_name") for t in (work.get("topics") or [])[:3] if t.get("display_name")],
                "keywords": [k.get("display_name") for k in (work.get("keywords") or [])[:6] if k.get("display_name")],
                "mesh_terms": mesh[:12],
                "mesh_ids": mesh_ids[:12],
                "institutions": institutions[:10],
                "orcids": [
                    short_id((a.get("author") or {}).get("orcid"))
                    for a in authorships
                    if (a.get("author") or {}).get("orcid")
                ][:15],
                "funders": [f.get("display_name") for f in (work.get("funders") or []) if f.get("display_name")][:10],
                "grants": [
                    g.get("funder_award_id") for g in (work.get("awards") or []) if g.get("funder_award_id")
                ][:20],
            }
        ),
    )


def author_record(author: dict[str, Any]) -> Record:
    aid = short_id(author.get("id"))
    stats = author.get("summary_stats") or {}
    last = author.get("last_known_institutions") or []
    topics = author.get("topics") or []
    return Record(
        source="openalex",
        id=aid,
        type="author",
        title=author.get("display_name"),
        url=f"{WEB}/{aid}" if aid else None,
        identifiers={"openalex": aid, "orcid": author.get("orcid")},
        attributes=_compact(
            {
                "works_count": author.get("works_count"),
                "cited_by_count": author.get("cited_by_count"),
                "h_index": stats.get("h_index"),
                "i10_index": stats.get("i10_index"),
                "two_year_mean_citedness": round(stats["2yr_mean_citedness"], 2)
                if isinstance(stats.get("2yr_mean_citedness"), (int, float))
                else None,
                "last_known_institutions": [i.get("display_name") for i in last if i.get("display_name")],
                "last_known_institution_rors": [short_id(i.get("ror")) for i in last if i.get("ror")],
                "countries": sorted({i.get("country_code") for i in last if i.get("country_code")}),
                "top_topics": [t.get("display_name") for t in topics[:5] if t.get("display_name")],
                "alternative_names": (author.get("display_name_alternatives") or [])[:5] or None,
            }
        ),
    )


def institution_record(inst: dict[str, Any]) -> Record:
    iid = short_id(inst.get("id"))
    ids = inst.get("ids") or {}
    geo = inst.get("geo") or {}
    stats = inst.get("summary_stats") or {}
    return Record(
        source="openalex",
        id=iid,
        type="institution",
        title=inst.get("display_name"),
        url=f"{WEB}/{iid}" if iid else None,
        identifiers={"openalex": iid, "ror": short_id(inst.get("ror") or ids.get("ror")) or None},
        attributes=_compact(
            {
                "type": inst.get("type"),
                "country_code": inst.get("country_code"),
                "city": geo.get("city"),
                "homepage": inst.get("homepage_url"),
                "acronyms": inst.get("display_name_acronyms"),
                "works_count": inst.get("works_count"),
                "cited_by_count": inst.get("cited_by_count"),
                "h_index": stats.get("h_index"),
                "wikidata": short_id(ids.get("wikidata")) or None,
                "grid": ids.get("grid"),
                "parent_ids": [short_id(x) for x in (inst.get("lineage") or []) if short_id(x) != iid] or None,
            }
        ),
    )


class SearchWorksParams(SearchParams):
    limit: int = Field(10, ge=1, le=200, description="Maximum number of works to return (OpenAlex per-page max 200)")
    filter: str | None = Field(
        None,
        description="OpenAlex filter string, comma = AND, | = OR, ! = NOT, e.g. "
        "'publication_year:2020-2024,type:review,is_oa:true,has_pmid:true,cited_by_count:>100', "
        "'authorships.institutions.ror:01yc7t268', 'topics.id:T10878', 'authorships.author.id:A5065535610', "
        "'cites:W2981137429' (works citing W...), 'cited_by:W2981137429' (its references), "
        "'primary_location.source.id:S137773608', 'grants.award_id:R01...'",
    )
    search_in: str = Field(
        "title_and_abstract",
        pattern="^(title_and_abstract|title|fulltext|all)$",
        description="Where the query matches: 'title_and_abstract' (default, precise), 'title', or 'fulltext' "
        "(title + abstract + indexed full text: much broader, noisier)",
    )
    sort: str | None = Field(
        None, description="e.g. 'cited_by_count:desc', 'publication_date:desc', 'relevance_score:desc' (default with query)"
    )


class GetWorkParams(ToolParams):
    id: str = Field(
        description="Work id: OpenAlex 'W2981137429' or openalex.org URL, DOI ('10.1038/...', 'doi:10...', "
        "https://doi.org/...), 'pmid:31634902', or 'pmcid:PMC6907074'"
    )


class GroupByParams(ToolParams):
    group_by: str = Field(
        description="Field to aggregate works by: publication_year, type, open_access.oa_status, open_access.is_oa, "
        "topics.id, primary_topic.field.id, authorships.institutions.id, authorships.institutions.country_code, "
        "authorships.author.id, primary_location.source.id, funders.id, language, is_retracted, mesh.descriptor_ui"
    )
    query: str | None = Field(None, description="Optional free-text search to restrict the works counted")
    search_in: str = Field(
        "title_and_abstract",
        pattern="^(title_and_abstract|title|fulltext|all)$",
        description="Where query matches (same as search_works.search_in): 'title_and_abstract' (default), "
        "'title' or 'fulltext' (much broader)",
    )
    filter: str | None = Field(None, description="Optional OpenAlex filter string (same syntax as search_works.filter)")
    limit: int = Field(50, ge=1, le=200, description="Maximum number of groups to return (largest first)")


class SearchAuthorsParams(ToolParams):
    query: str = Field(description="Author name, e.g. 'David R. Liu'")
    filter: str | None = Field(
        None, description="OpenAlex author filter, e.g. 'last_known_institutions.ror:05a0ya142,works_count:>20'"
    )
    limit: int = Field(10, ge=1, le=100, description="Maximum number of authors to return")
    cursor: str | None = Field(None, description="Pagination cursor from a previous next_cursor")


class GetAuthorParams(ToolParams):
    id: str = Field(description="OpenAlex author id 'A5065535610' (or URL) or an ORCID '0000-0002-9943-7557'")


class SearchInstitutionsParams(ToolParams):
    query: str = Field(
        description="Institution name (e.g. 'Washington University in St. Louis'), or a ROR id/URL "
        "('01yc7t268' / https://ror.org/01yc7t268) for an exact lookup"
    )
    limit: int = Field(10, ge=1, le=100, description="Maximum number of institutions to return")


_ORCID = re.compile(r"^(?:https?://orcid\.org/)?(\d{4}-\d{4}-\d{4}-\d{3}[\dX])$", re.IGNORECASE)
_ROR = re.compile(r"^(?:https?://ror\.org/)?(0[a-z0-9]{6}\d{2})$", re.IGNORECASE)
_OA_ID = re.compile(r"^(?:https?://(?:api\.)?openalex\.org/(?:\w+/)?)?([WASIPFTC]\d+)$", re.IGNORECASE)


class OpenAlex(Connector):
    name = "openalex"
    title = "OpenAlex"
    category = "literature"
    description = (
        "Open catalog of 250M+ scholarly works with authors (ORCID), institutions (ROR), journals, topics, "
        "MeSH, funders/grant numbers, open-access status and the full citation graph. Best for bibliometrics "
        "(counts by year/institution/topic via group_by), institution/author publication lists and "
        "DOI/PMID-linked metadata."
    )
    homepage = "https://openalex.org/"
    docs_url = "https://docs.openalex.org/"
    auth = "optional_api_key"
    rate_limit = 8.0
    health_query = "CRISPR"

    def _params(self, params: dict[str, Any]) -> dict[str, Any]:
        key = self.setting("openalex_api_key")
        if key:
            params["api_key"] = key
        email = self.setting("BIOPHA_CONTACT_EMAIL")
        if email:
            params["mailto"] = email
        return params

    def _notes(self, notes: list[str] | None = None) -> list[str]:
        out = list(notes or [])
        if not self.setting("openalex_api_key"):
            out.append("no OPENALEX_API_KEY set: sharing the small keyless daily budget")
        return out

    def _search_filter(self, query: str | None, search_in: str) -> tuple[str | None, str | None]:
        """Return (search param, extra filter clause)."""
        q = clean_query(query)
        if not q:
            return None, None
        if search_in == "title":
            return None, f"title.search:{q.replace(',', ' ')}"
        if search_in == "title_and_abstract":
            return None, f"title_and_abstract.search:{q.replace(',', ' ')}"
        return q, None  # fulltext / all -> search=

    @tool(
        description="""
        Search OpenAlex works (articles, reviews, preprints, books, datasets) by free text (matched in title +
        abstract by default; search_in='fulltext' also matches full text) and/or structured filters.
        Returns OpenAlex W ids with DOI/PMID/PMCID, reconstructed abstract, journal, date, authors, institutions, ORCIDs, topics, MeSH terms, funders/grant numbers, citation count/FWCI and OA status.
        Use for bibliometric questions and institution/author/funder/topic-scoped lists. query may be '' when
        filter is given (e.g. filter='cites:W2981137429' lists citing works). Filter syntax: comma = AND,
        | = OR, e.g. 'publication_year:2020-2024,type:review,is_oa:true,authorships.institutions.ror:01yc7t268'.
        sort e.g. 'cited_by_count:desc'. For counts/trends use works_group_by instead of paging.
        """,
        federated=True,
    )
    async def search_works(self, params: SearchWorksParams) -> ToolResult:
        search, extra = self._search_filter(params.query, params.search_in)
        filters = ",".join(f for f in (params.filter, extra) if f) or None
        if not search and not filters:
            return ToolResult(source=self.name, tool="search_works", notes=["provide a query or a filter"])
        query = self._params(
            {
                "search": search,
                "filter": filters,
                "sort": params.sort or ("relevance_score:desc" if (search or extra) else None),
                "per-page": params.limit,
                "cursor": params.cursor or "*",
                "select": WORK_SELECT,
            }
        )
        response = await self.request("GET", f"{API}/works", params=query)
        payload = response.json() or {}
        meta = payload.get("meta") or {}
        items = payload.get("results") or []
        return ToolResult(
            source=self.name,
            tool="search_works",
            records=[work_record(w) for w in items],
            total=meta.get("count"),
            next_cursor=meta.get("next_cursor") if items and len(items) >= params.limit else None,
            request_url=response.url,
            notes=self._notes(),
        )

    @tool(
        description="""
        Fetch one OpenAlex work with full metadata (abstract, authorships with ORCID + ROR institutions,
        topics, MeSH, funders and grant numbers, OA status/URL, citation metrics). Accepts 'W2981137429',
        a DOI (bare, 'doi:' or URL), 'pmid:31634902' or 'pmcid:PMC6907074'. Use to map a DOI/PMID to its
        OpenAlex id before filtering with cites:/cited_by:.
        """
    )
    async def get_work(self, params: GetWorkParams) -> ToolResult:
        raw = params.id.strip()
        lowered = raw.lower()
        match = _OA_ID.match(raw)
        if lowered.startswith("pmcid:") or re.fullmatch(r"pmc\d+", lowered):
            digits = re.sub(r"\D", "", raw)
            landing = (
                f"https://www.ncbi.nlm.nih.gov/pmc/articles/{digits}"
                f"|https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{digits}"
            )
            response = await self.request(
                "GET",
                f"{API}/works",
                params=self._params(
                    {"filter": f"locations.landing_page_url:{landing}", "per-page": 1, "select": WORK_SELECT}
                ),
            )
            items = (response.json() or {}).get("results") or []
            if not items:
                return ToolResult(
                    source=self.name, tool="get_work", request_url=response.url,
                    notes=[f"PMC{digits} not found in OpenAlex (try pubmed/europe_pmc to map it to a PMID or DOI)"],
                )
            return ToolResult(
                source=self.name, tool="get_work", records=[work_record(items[0])], total=1, request_url=response.url
            )
        if match:
            path = match.group(1).upper()
        elif lowered.startswith("pmid:"):
            path = f"pmid:{re.sub(r'[^0-9]', '', raw)}"
        elif lowered.startswith(("doi:", "10.", "https://doi.org/", "http://doi.org/", "https://dx.doi.org/")):
            path = f"doi:{normalize_doi(raw)}"
        elif raw.isdigit():
            path = f"pmid:{raw}"
        else:
            path = raw
        # '/' is only meaningful inside DOIs; quote it everywhere else.
        safe = ":/" if path.startswith("doi:") else ":"
        response = await self.request(
            "GET",
            f"{API}/works/{quote(path, safe=safe)}",
            params=self._params({"select": WORK_SELECT}),
            allow_status=(404,),
        )
        if response.status == 404:
            return ToolResult(
                source=self.name, tool="get_work", request_url=response.url,
                notes=[f"work {path} not found in OpenAlex"],
            )
        return ToolResult(
            source=self.name, tool="get_work", records=[work_record(response.json())], total=1,
            request_url=response.url,
        )

    @tool(
        description="""
        Bibliometric aggregation: count OpenAlex works grouped by a field (publication_year, type,
        open_access.oa_status, topics.id, authorships.institutions.id, authorships.institutions.country_code,
        primary_location.source.id, funders.id, authorships.author.id, ...), optionally restricted by query
        and/or filter. Returns data=[{key, name, count}] largest first plus the total matching works.
        Examples: publications per year on a topic; top institutions for 'prime editing'; citations per year
        of a paper (filter='cites:W2981137429', group_by='publication_year').
        """
    )
    async def works_group_by(self, params: GroupByParams) -> ToolResult:
        search, extra = self._search_filter(params.query, params.search_in)
        filters = ",".join(f for f in (params.filter, extra) if f) or None
        response = await self.request(
            "GET",
            f"{API}/works",
            params=self._params({"search": search, "filter": filters, "group_by": params.group_by}),
        )
        payload = response.json() or {}
        meta = payload.get("meta") or {}
        groups = payload.get("group_by") or []
        rows = [
            {
                "key": short_id(g.get("key")) if str(g.get("key", "")).startswith("https://openalex.org/") else g.get("key"),
                "name": g.get("key_display_name"),
                "count": g.get("count"),
            }
            for g in groups
        ]
        if params.group_by == "publication_year":
            rows.sort(key=lambda r: str(r["key"]))
        notes = []
        if meta.get("groups_count") and len(groups) >= 200:
            notes.append("OpenAlex returns at most 200 groups per request; rarer groups are omitted")
        if len(rows) > params.limit:
            notes.append(f"showing {params.limit} of {len(rows)} groups")
            if params.group_by == "publication_year":
                rows = rows[-params.limit:]
            else:
                rows = rows[: params.limit]
        return ToolResult(
            source=self.name,
            tool="works_group_by",
            total=meta.get("count"),
            data={"group_by": params.group_by, "groups_count": meta.get("groups_count"), "groups": rows},
            request_url=response.url,
            notes=self._notes(notes),
        )

    @tool(
        description="""
        Search OpenAlex author profiles by name (disambiguated authors with ORCID, works/citation counts,
        h-index, last known institutions with ROR, top topics). Optional filter, e.g.
        'last_known_institutions.ror:05a0ya142'. Then list their papers with search_works
        filter='authorships.author.id:A...'.
        """
    )
    async def search_authors(self, params: SearchAuthorsParams) -> ToolResult:
        response = await self.request(
            "GET",
            f"{API}/authors",
            params=self._params(
                {
                    "search": clean_query(params.query) or None,
                    "filter": params.filter,
                    "per-page": params.limit,
                    "cursor": params.cursor or "*",
                }
            ),
        )
        payload = response.json() or {}
        meta = payload.get("meta") or {}
        items = payload.get("results") or []
        return ToolResult(
            source=self.name,
            tool="search_authors",
            records=[author_record(a) for a in items],
            total=meta.get("count"),
            next_cursor=meta.get("next_cursor") if items and len(items) >= params.limit else None,
            request_url=response.url,
            notes=self._notes(),
        )

    @tool(
        description="""
        Fetch one OpenAlex author by OpenAlex id ('A5065535610') or ORCID ('0000-0002-9943-7557'): works and
        citation counts, h-index, last known institutions (ROR), top research topics.
        """
    )
    async def get_author(self, params: GetAuthorParams) -> ToolResult:
        raw = params.id.strip()
        orcid = _ORCID.match(raw)
        oa_id = _OA_ID.match(raw)
        path = f"orcid:{orcid.group(1).upper()}" if orcid else (oa_id.group(1).upper() if oa_id else raw)
        response = await self.request(
            "GET", f"{API}/authors/{quote(path, safe=':')}", params=self._params({}), allow_status=(404,)
        )
        if response.status == 404:
            return ToolResult(
                source=self.name, tool="get_author", request_url=response.url,
                notes=[f"author {path} not found in OpenAlex"],
            )
        return ToolResult(
            source=self.name, tool="get_author", records=[author_record(response.json())], total=1,
            request_url=response.url,
        )

    @tool(
        description="""
        Find research institutions (universities, hospitals, companies, government labs) by name, or look one
        up exactly by ROR id. Returns OpenAlex I id, ROR, country, type, works/citation counts. Use the ROR
        in search_works / works_group_by filters ('authorships.institutions.ror:01yc7t268').
        """
    )
    async def search_institutions(self, params: SearchInstitutionsParams) -> ToolResult:
        raw = params.query.strip()
        ror = _ROR.match(raw)
        if ror:
            response = await self.request(
                "GET", f"{API}/institutions/ror:{ror.group(1).lower()}", params=self._params({}), allow_status=(404,)
            )
            if response.status == 404:
                return ToolResult(
                    source=self.name, tool="search_institutions", request_url=response.url,
                    notes=[f"ROR {ror.group(1)} not found in OpenAlex"],
                )
            return ToolResult(
                source=self.name, tool="search_institutions", records=[institution_record(response.json())],
                total=1, request_url=response.url,
            )
        response = await self.request(
            "GET", f"{API}/institutions", params=self._params({"search": clean_query(raw) or None, "per-page": params.limit})
        )
        payload = response.json() or {}
        items = payload.get("results") or []
        return ToolResult(
            source=self.name,
            tool="search_institutions",
            records=[institution_record(i) for i in items],
            total=(payload.get("meta") or {}).get("count"),
            request_url=response.url,
            notes=self._notes(),
        )
