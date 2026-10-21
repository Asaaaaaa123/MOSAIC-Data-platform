"""Semantic Scholar Academic Graph + Recommendations API connector.

Docs: https://api.semanticscholar.org/api-docs/graph (Graph v1),
https://api.semanticscholar.org/api-docs/recommendations (Recommendations v1).

Auth: optional. ``SEMANTIC_SCHOLAR_API_KEY`` is sent as the ``x-api-key`` header
when present. Without a key requests go to a shared, heavily throttled
unauthenticated pool (frequent HTTP 429).

Rate limits: an introductory API key allows 1 request/second cumulative across
all endpoints; the connector uses 0.9 req/s with a key and 0.5 req/s without.
The shared HTTP layer retries 429s with backoff (5 retries here, ~40 s worst
case, because a key shared by several processes regularly sees 429 bursts).

Quirks:
- ``/paper/search`` (relevance) can only page through the first 1,000 hits
  (offset + limit < 1000) and returns at most 100 per page.
- Paper ids accept S2 sha ids and prefixed external ids: ``DOI:``, ``PMID:``,
  ``PMCID:`` (digits only; ``PMC`` prefix is stripped here), ``ARXIV:``,
  ``CorpusId:``, ``MAG:``, ``ACL:``, ``URL:``.
- Publishers sometimes elide ``abstract``; ``tldr`` (AI one-sentence summary) is
  used as the summary fallback and flagged in attributes.
- Citation ``contexts``/``intents`` are frequently empty for publisher-restricted
  papers.
- ``/snippet/search`` returns passages keyed by corpusId only (no DOI/PMID);
  resolve them with ``get_papers_batch`` using ``CorpusId:<n>``.

Identifiers: s2 (paperId), doi, pmid, pmcid, arxiv; corpus id, MAG, DBLP and
ACL ids go in attributes. Authors carry orcid when S2 reports one.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from pydantic import Field

from ..ids import normalize_doi, normalize_identifiers
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, strip_html, tool

GRAPH = "https://api.semanticscholar.org/graph/v1"
RECS = "https://api.semanticscholar.org/recommendations/v1"
S2_WEB = "https://www.semanticscholar.org"

PAPER_FIELDS = (
    "paperId,corpusId,externalIds,url,title,abstract,venue,publicationVenue,year,publicationDate,journal,"
    "referenceCount,citationCount,influentialCitationCount,isOpenAccess,openAccessPdf,fieldsOfStudy,"
    "publicationTypes,tldr,authors"
)
# Nested paper fields for citations / references / author papers / recommendations
# (tldr is not available on these endpoints).
LINK_FIELDS = (
    "paperId,corpusId,externalIds,url,title,abstract,venue,publicationVenue,year,publicationDate,journal,"
    "citationCount,influentialCitationCount,isOpenAccess,openAccessPdf,fieldsOfStudy,publicationTypes,authors"
)
AUTHOR_FIELDS = "authorId,externalIds,url,name,affiliations,homepage,paperCount,citationCount,hIndex"

_PREPRINT_VENUES = re.compile(r"arxiv|biorxiv|medrxiv|chemrxiv|ssrn|research square|preprints", re.IGNORECASE)
_SHA = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


def normalize_paper_id(value: str) -> str:
    """Map user-supplied paper ids onto the S2 ``PREFIX:value`` syntax."""
    text = value.strip()
    if _SHA.match(text):
        return text.lower()
    lowered = text.lower()
    if lowered.startswith(("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi.org/")):
        return f"DOI:{normalize_doi(text)}"
    if lowered.startswith(("http://", "https://")):
        return f"URL:{text}"
    if lowered.startswith("10.") and "/" in text:
        return f"DOI:{normalize_doi(text)}"
    if re.fullmatch(r"PMC\d+", text, re.IGNORECASE):
        return f"PMCID:{text[3:]}"
    if ":" in text:
        prefix, _, rest = text.partition(":")
        key = prefix.strip().upper()
        rest = rest.strip()
        canon = {
            "DOI": "DOI",
            "PMID": "PMID",
            "PUBMED": "PMID",
            "PMCID": "PMCID",
            "PMC": "PMCID",
            "ARXIV": "ARXIV",
            "CORPUSID": "CorpusId",
            "CORPUS": "CorpusId",
            "MAG": "MAG",
            "ACL": "ACL",
            "URL": "URL",
        }.get(key)
        if canon == "DOI":
            return f"DOI:{normalize_doi(rest)}"
        if canon == "PMCID":
            return f"PMCID:{re.sub(r'(?i)^pmc', '', rest)}"
        if canon:
            return f"{canon}:{rest}"
    return text


class SearchPapersParams(SearchParams):
    year: str | None = Field(
        None, description="Publication year or range: '2019', '2016-2020', '2018-' (since), '-2015' (until)"
    )
    fields_of_study: str | None = Field(
        None,
        description="Comma-separated S2 fields of study, e.g. 'Medicine,Biology' (others: Chemistry, "
        "Computer Science, Psychology, Environmental Science, ...)",
    )
    publication_types: str | None = Field(
        None,
        description="Comma-separated: Review, JournalArticle, ClinicalTrial, MetaAnalysis, CaseReport, "
        "Study, Dataset, Conference, Editorial, LettersAndComments, Book",
    )
    open_access_pdf: bool = Field(False, description="Only papers with a public open-access PDF")
    min_citation_count: int | None = Field(None, ge=0, description="Only papers with at least this many citations")
    venue: str | None = Field(None, description="Comma-separated venue names, e.g. 'Nature,Cell'")


class PaperIdParams(ToolParams):
    paper_id: str = Field(
        description="Paper id: S2 paperId (40-char sha), 'DOI:10.1038/...', bare DOI, 'PMID:31634902', "
        "'PMCID:6907074' or 'PMC6907074', 'ARXIV:2106.15928', 'CorpusId:204832721', 'MAG:...', "
        "'URL:https://arxiv.org/abs/...' or a semanticscholar.org / arxiv.org / doi.org URL"
    )


class PapersBatchParams(ToolParams):
    ids: list[str] = Field(
        min_length=1,
        max_length=500,
        description="Up to 500 paper ids in any get_paper format (DOI:, PMID:, PMCID:, ARXIV:, CorpusId:, S2 ids)",
    )
    include_abstract: bool = Field(True, description="Include abstracts (set false for lighter enrichment of big lists)")


class CitationsParams(ToolParams):
    paper_id: str = Field(description="Paper id in any get_paper format (S2 id, DOI:..., PMID:..., PMCID:..., ARXIV:...)")
    direction: str = Field(
        "citations",
        pattern="^(citations|references)$",
        description="'citations' = papers that cite this paper (forward); 'references' = papers this paper cites (backward)",
    )
    limit: int = Field(20, ge=1, le=100, description="Maximum number of linked papers to return")
    cursor: str | None = Field(None, description="Offset cursor: pass next_cursor from a previous result")


class SearchAuthorsParams(ToolParams):
    query: str = Field(description="Author name, e.g. 'David R. Liu'")
    limit: int = Field(10, ge=1, le=100, description="Maximum number of authors to return")
    cursor: str | None = Field(None, description="Offset cursor: pass next_cursor from a previous result")


class AuthorPapersParams(ToolParams):
    author_id: str = Field(description="Semantic Scholar authorId (digits, from search_authors or paper authors)")
    limit: int = Field(20, ge=1, le=100, description="Maximum number of papers to return")
    cursor: str | None = Field(None, description="Offset cursor: pass next_cursor from a previous result")


class RecommendParams(ToolParams):
    paper_ids: list[str] = Field(
        min_length=1,
        max_length=50,
        description="Seed (positive) papers in any get_paper format; one id uses the single-paper recommender",
    )
    negative_paper_ids: list[str] = Field(
        default_factory=list, max_length=50, description="Optional papers to steer away from"
    )
    pool: str = Field(
        "recent",
        pattern="^(recent|all-cs)$",
        description="Candidate pool for single-paper recommendations: 'recent' (all fields, recent papers) or 'all-cs'",
    )
    limit: int = Field(10, ge=1, le=100, description="Maximum number of recommendations")


class SnippetSearchParams(ToolParams):
    query: str = Field(description="Natural-language question or phrase to match against full-text passages")
    limit: int = Field(10, ge=1, le=100, description="Maximum number of snippets")
    year: str | None = Field(None, description="Publication year or range, e.g. '2020-2024'")
    fields_of_study: str | None = Field(None, description="Comma-separated fields of study, e.g. 'Medicine,Biology'")
    paper_ids: list[str] = Field(
        default_factory=list, max_length=100, description="Restrict to these papers (any get_paper id format)"
    )
    min_citation_count: int | None = Field(None, ge=0, description="Only papers with at least this many citations")
    resolve_ids: bool = Field(
        True, description="Make one extra batch call to attach S2 paperId/DOI/PMID/PMCID to each snippet's paper"
    )


def _compact(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v not in (None, [], "", {})}


def _paper_type(paper: dict[str, Any]) -> str:
    types = paper.get("publicationTypes") or []
    if "Dataset" in types:
        return "dataset"
    if "Book" in types or "BookSection" in types:
        return "book"
    venue = paper.get("venue") or ""
    ext = paper.get("externalIds") or {}
    journal = (paper.get("journal") or {}).get("name") or ""
    if _PREPRINT_VENUES.search(venue) or _PREPRINT_VENUES.search(journal):
        return "preprint"
    if ext.get("ArXiv") and not journal and not ext.get("DOI"):
        return "preprint"
    return "article"


def _orcid(ext: dict[str, Any]) -> str | None:
    value = ext.get("ORCID")
    if isinstance(value, list):
        value = value[0] if value else None
    return value


def paper_record(paper: dict[str, Any], extra: dict[str, Any] | None = None) -> Record:
    ext = paper.get("externalIds") or {}
    pid = paper.get("paperId") or ""
    journal = paper.get("journal") or {}
    oa = paper.get("openAccessPdf") or {}
    tldr = (paper.get("tldr") or {}).get("text")
    abstract = strip_html(paper.get("abstract"))
    corpus = paper.get("corpusId") or ext.get("CorpusId")
    pmc = ext.get("PubMedCentral")
    date = paper.get("publicationDate") or (str(paper["year"]) if paper.get("year") else None)
    venue = paper.get("publicationVenue")
    venue = venue if isinstance(venue, dict) else {}  # some endpoints return a bare string
    attributes = _compact(
        {
            "corpus_id": corpus,
            "venue": paper.get("venue") or None,
            "journal": journal.get("name"),
            "volume": journal.get("volume"),
            "pages": re.sub(r"\s+", "", journal.get("pages") or "") or None,
            "publication_types": paper.get("publicationTypes"),
            "fields_of_study": paper.get("fieldsOfStudy"),
            "citation_count": paper.get("citationCount"),
            "influential_citation_count": paper.get("influentialCitationCount"),
            "reference_count": paper.get("referenceCount"),
            "is_open_access": paper.get("isOpenAccess"),
            "oa_pdf_url": oa.get("url") or None,
            "oa_status": oa.get("status"),
            "oa_license": oa.get("license"),
            "tldr": tldr if abstract else None,
            "summary_is_tldr": True if (tldr and not abstract) else None,
            "mag": ext.get("MAG"),
            "dblp": ext.get("DBLP"),
            "acl": ext.get("ACL"),
            "author_ids": [a.get("authorId") for a in paper.get("authors") or [] if a.get("authorId")][:10] or None,
            **(extra or {}),
        }
    )
    return Record(
        source="semantic_scholar",
        id=pid or (f"CorpusId:{corpus}" if corpus else ""),
        type=_paper_type(paper),  # type: ignore[arg-type]
        title=strip_html(paper.get("title")),
        url=paper.get("url") or (f"{S2_WEB}/paper/{pid}" if pid else None),
        identifiers={
            "s2": pid,
            "doi": ext.get("DOI"),
            "pmid": ext.get("PubMed"),
            "pmcid": f"PMC{pmc}" if pmc else None,
            "arxiv": ext.get("ArXiv"),
            "issn": venue.get("issn"),
        },
        date=date,
        authors=[a.get("name") for a in paper.get("authors") or [] if a.get("name")],
        summary=abstract or tldr,
        attributes=attributes,
    )


def author_record(author: dict[str, Any]) -> Record:
    aid = str(author.get("authorId") or "")
    ext = author.get("externalIds") or {}
    return Record(
        source="semantic_scholar",
        id=aid,
        type="author",
        title=author.get("name"),
        url=author.get("url") or (f"{S2_WEB}/author/{aid}" if aid else None),
        identifiers={"orcid": _orcid(ext)},
        attributes=_compact(
            {
                "affiliations": author.get("affiliations"),
                "homepage": author.get("homepage"),
                "paper_count": author.get("paperCount"),
                "citation_count": author.get("citationCount"),
                "h_index": author.get("hIndex"),
                "dblp": ext.get("DBLP"),
            }
        ),
    )


def _next_offset(payload: dict[str, Any], offset: int, returned: int, total: int | None = None) -> str | None:
    nxt = payload.get("next")
    if nxt is None or returned == 0:
        return None
    if total is not None and int(nxt) >= total:
        return None
    return str(nxt) if int(nxt) > offset else None


class SemanticScholar(Connector):
    name = "semantic_scholar"
    title = "Semantic Scholar"
    category = "literature"
    description = (
        "Allen AI's Semantic Scholar Academic Graph: 220M+ papers across all sciences with abstracts, TLDRs, "
        "citation/reference graphs with influential-citation flags, authors with h-index, open-access PDFs and "
        "full-text passage search. Best for citation analysis, related-paper discovery and bulk enrichment "
        "of DOI/PMID lists."
    )
    homepage = "https://www.semanticscholar.org/"
    docs_url = "https://api.semanticscholar.org/api-docs/graph"
    auth = "optional_api_key"
    rate_limit = 0.5
    health_query = "CRISPR base editing"

    def effective_rate(self) -> float:
        return 0.9 if self.setting("semantic_scholar_api_key") else self.rate_limit

    async def request(self, method: str, url: str, **kwargs: Any):  # type: ignore[override]
        # The 1 req/s key is often shared by several processes and S2 answers bursts with 429;
        # allow a longer backoff chain (1.5+3+6+12+20 s) than the default 3 retries.
        kwargs.setdefault("retries", 5)
        return await super().request(method, url, **kwargs)

    def _headers(self) -> dict[str, str]:
        key = self.setting("semantic_scholar_api_key")
        return {"x-api-key": key} if key else {}

    def _notes(self) -> list[str]:
        if self.setting("semantic_scholar_api_key"):
            return []
        return ["no SEMANTIC_SCHOLAR_API_KEY set: using the shared unauthenticated pool (expect throttling)"]

    @tool(
        description="""
        Search Semantic Scholar papers (all disciplines, strong biomedical + CS coverage) by relevance to
        free text over titles and abstracts. Returns S2 ids plus DOI/PMID/PMCID/arXiv ids, abstract (or AI
        TLDR), venue, date, citation and influential-citation counts and open-access PDF links. Use for
        citation-weighted literature discovery; prefer PubMed/Europe PMC for MeSH-precise biomedical
        queries. Plain keywords work best (no boolean syntax). Filters: year ('2018-2023'), fields_of_study
        ('Medicine,Biology'), publication_types ('Review,ClinicalTrial'), open_access_pdf, min_citation_count,
        venue. Only the first 1,000 hits are pageable.
        """,
        federated=True,
    )
    async def search_papers(self, params: SearchPapersParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        if offset >= 1000:
            return ToolResult(
                source=self.name,
                tool="search_papers",
                notes=["Semantic Scholar relevance search only pages through the first 1,000 hits; narrow the query"],
            )
        limit = min(params.limit, 1000 - offset - 1) or 1
        query: dict[str, Any] = {
            "query": params.query,
            "offset": offset,
            "limit": limit,
            "fields": PAPER_FIELDS,
            "year": params.year,
            "fieldsOfStudy": params.fields_of_study,
            "publicationTypes": params.publication_types,
            "minCitationCount": params.min_citation_count,
            "venue": params.venue,
        }
        if params.open_access_pdf:
            query["openAccessPdf"] = ""
        response = await self.request("GET", f"{GRAPH}/paper/search", params=query, headers=self._headers())
        payload = response.json() or {}
        items = [p for p in payload.get("data") or [] if p]
        total = payload.get("total")
        nxt = _next_offset(payload, offset, len(items), min(total, 1000) if isinstance(total, int) else None)
        return ToolResult(
            source=self.name,
            tool="search_papers",
            records=[paper_record(p) for p in items],
            total=total,
            next_cursor=nxt,
            request_url=response.url,
            notes=self._notes(),
        )

    @tool(
        description="""
        Fetch one paper's full Semantic Scholar record: title, abstract, TLDR, authors (with S2 author ids),
        venue/journal, date, citation/reference/influential counts, fields of study, publication types,
        open-access PDF, and cross-ids (DOI, PMID, PMCID, arXiv, CorpusId, MAG). Accepts S2 paperId,
        'DOI:10.1038/s41586-019-1711-4' (or bare DOI), 'PMID:31634902', 'PMCID:6907074'/'PMC6907074',
        'ARXIV:2106.15928', 'CorpusId:204832721' or a paper URL. For many ids use get_papers_batch.
        """
    )
    async def get_paper(self, params: PaperIdParams) -> ToolResult:
        pid = normalize_paper_id(params.paper_id)
        response = await self.request(
            "GET",
            f"{GRAPH}/paper/{quote(pid, safe=':/')}",
            params={"fields": PAPER_FIELDS},
            headers=self._headers(),
            allow_status=(404,),
        )
        if response.status == 404:
            return ToolResult(
                source=self.name, tool="get_paper", notes=[f"paper {pid} not found in Semantic Scholar"],
                request_url=response.url,
            )
        return ToolResult(
            source=self.name, tool="get_paper", records=[paper_record(response.json())], total=1,
            request_url=response.url,
        )

    @tool(
        description="""
        Resolve up to 500 papers in ONE request (POST /paper/batch): the efficient way to enrich a list of
        DOIs/PMIDs/PMCIDs/arXiv ids from another source with S2 ids, abstracts, citation counts,
        open-access PDFs and missing cross-identifiers. Ids may mix formats ('DOI:...', 'PMID:...',
        'PMC123', 'CorpusId:...', S2 ids). Unresolved ids are listed in notes; records come back in input
        order with attributes.requested_id.
        """
    )
    async def get_papers_batch(self, params: PapersBatchParams) -> ToolResult:
        ids = [normalize_paper_id(i) for i in params.ids]
        fields = PAPER_FIELDS if params.include_abstract else PAPER_FIELDS.replace("abstract,", "").replace(",tldr", "")
        response = await self.request(
            "POST",
            f"{GRAPH}/paper/batch",
            params={"fields": fields},
            json_body={"ids": ids},
            headers=self._headers(),
        )
        payload = response.json() or []
        records, missing = [], []
        for requested, paper in zip(ids, payload if isinstance(payload, list) else []):
            if paper:
                records.append(paper_record(paper, {"requested_id": requested}))
            else:
                missing.append(requested)
        notes = self._notes()
        if missing:
            shown = ", ".join(missing[:20]) + (f" (+{len(missing) - 20} more)" if len(missing) > 20 else "")
            notes.append(f"{len(missing)} id(s) not found in Semantic Scholar: {shown}")
        return ToolResult(
            source=self.name, tool="get_papers_batch", records=records, total=len(records),
            request_url=response.url, notes=notes,
        )

    @tool(
        description="""
        Citation graph for one paper. direction='citations' returns papers that CITE it (forward, newest
        first); direction='references' returns papers it CITES (its bibliography). Each linked paper comes
        with DOI/PMID ids, counts and attributes.is_influential, attributes.intents (background, methodology,
        result) and attributes.contexts (citing sentences; often empty for publisher-restricted papers).
        Use OpenAlex works_group_by with filter cites:W... for citation counts by year instead.
        """
    )
    async def paper_citations(self, params: CitationsParams) -> ToolResult:
        pid = normalize_paper_id(params.paper_id)
        offset = parse_offset(params.cursor)
        response = await self.request(
            "GET",
            f"{GRAPH}/paper/{quote(pid, safe=':/')}/{params.direction}",
            params={
                "fields": f"contexts,intents,isInfluential,{LINK_FIELDS}",
                "offset": offset,
                "limit": params.limit,
            },
            headers=self._headers(),
            allow_status=(404,),
        )
        if response.status == 404:
            return ToolResult(
                source=self.name, tool="paper_citations", notes=[f"paper {pid} not found in Semantic Scholar"],
                request_url=response.url,
            )
        payload = response.json() or {}
        key = "citingPaper" if params.direction == "citations" else "citedPaper"
        records = []
        for edge in payload.get("data") or []:
            paper = (edge or {}).get(key) or {}
            if not paper.get("paperId") and not paper.get("title"):
                continue
            contexts = [strip_html(c)[:400] for c in (edge.get("contexts") or [])[:3] if c]
            records.append(
                paper_record(
                    paper,
                    {
                        "relation": "cites_seed" if params.direction == "citations" else "cited_by_seed",
                        "seed_paper": pid,
                        "is_influential": edge.get("isInfluential"),
                        "intents": edge.get("intents") or None,
                        "contexts": contexts or None,
                    },
                )
            )
        notes = self._notes()
        if payload.get("citingPaperInfo") and not payload.get("data"):
            notes.append("no linked papers returned")
        return ToolResult(
            source=self.name,
            tool="paper_citations",
            records=records,
            next_cursor=_next_offset(payload, offset, len(payload.get("data") or [])),
            request_url=response.url,
            notes=notes,
        )

    @tool(
        description="""
        Find Semantic Scholar author profiles by name. Returns S2 authorId, affiliations, paper count,
        citation count, h-index and ORCID when known. Names are ambiguous: check counts/affiliations, then
        use author_papers with the authorId. OpenAlex search_authors has better ORCID/institution coverage.
        """
    )
    async def search_authors(self, params: SearchAuthorsParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        response = await self.request(
            "GET",
            f"{GRAPH}/author/search",
            params={"query": params.query, "offset": offset, "limit": params.limit, "fields": AUTHOR_FIELDS},
            headers=self._headers(),
        )
        payload = response.json() or {}
        items = [a for a in payload.get("data") or [] if a]
        total = payload.get("total")
        return ToolResult(
            source=self.name,
            tool="search_authors",
            records=[author_record(a) for a in items],
            total=total,
            next_cursor=_next_offset(payload, offset, len(items), total),
            request_url=response.url,
            notes=self._notes(),
        )

    @tool(
        description="""
        List an author's papers (newest first) by S2 authorId (digits from search_authors or a paper's
        attributes.author_ids), with DOI/PMID ids, venue, date and citation counts. Paginate with cursor.
        """
    )
    async def author_papers(self, params: AuthorPapersParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        aid = params.author_id.strip()
        response = await self.request(
            "GET",
            f"{GRAPH}/author/{quote(aid, safe='')}/papers",
            params={"fields": LINK_FIELDS, "offset": offset, "limit": params.limit},
            headers=self._headers(),
            allow_status=(404,),
        )
        if response.status == 404:
            return ToolResult(
                source=self.name, tool="author_papers", notes=[f"author {aid} not found in Semantic Scholar"],
                request_url=response.url,
            )
        payload = response.json() or {}
        items = [p for p in payload.get("data") or [] if p]
        notes = self._notes()
        if not items:
            notes.append(
                f"no papers for author {aid}" + (" at this offset" if offset else "")
                + " (S2 returns an empty list for unknown author ids; check the id with search_authors)"
            )
        return ToolResult(
            source=self.name,
            tool="author_papers",
            records=[paper_record(p, {"author_id": aid}) for p in items],
            next_cursor=_next_offset(payload, offset, len(items)),
            request_url=response.url,
            notes=notes,
        )

    @tool(
        description="""
        Semantic Scholar's ML paper recommender: papers similar to one or more seed papers (any get_paper
        id format), optionally steered away from negative examples. With a single seed it uses the
        'forpaper' recommender over recent papers (pool='recent'); with several seeds or negatives it uses
        the multi-paper recommender. Good for expanding a literature set beyond keyword search.
        """
    )
    async def recommend_papers(self, params: RecommendParams) -> ToolResult:
        positives = [normalize_paper_id(i) for i in params.paper_ids]
        negatives = [normalize_paper_id(i) for i in params.negative_paper_ids]
        query = {"fields": LINK_FIELDS, "limit": params.limit}
        if len(positives) == 1 and not negatives:
            response = await self.request(
                "GET",
                f"{RECS}/papers/forpaper/{quote(positives[0], safe=':/')}",
                params={**query, "from": params.pool},
                headers=self._headers(),
                allow_status=(404,),
            )
        else:
            response = await self.request(
                "POST",
                f"{RECS}/papers/",
                params=query,
                json_body={"positivePaperIds": positives, "negativePaperIds": negatives},
                headers=self._headers(),
                allow_status=(404,),
            )
        if response.status == 404:
            return ToolResult(
                source=self.name, tool="recommend_papers",
                notes=[f"seed paper(s) not found in Semantic Scholar: {', '.join(positives)}"],
                request_url=response.url,
            )
        payload = response.json() or {}
        items = [p for p in payload.get("recommendedPapers") or [] if p]
        return ToolResult(
            source=self.name,
            tool="recommend_papers",
            records=[paper_record(p, {"recommended_for": positives[:5]}) for p in items],
            total=len(items),
            request_url=response.url,
            notes=self._notes(),
        )

    @tool(
        description="""
        Full-text passage search: returns ~500-word snippets from paper bodies/abstracts that best match a
        natural-language question (e.g. 'prime editing efficiency in primary T cells'), with section name,
        match score and open-access status. Use to find specific claims, methods or numbers inside papers.
        By default one extra batch call attaches DOI/PMID/PMCID/S2 ids to each snippet's paper
        (resolve_ids=false skips it). Optional filters: year, fields_of_study, paper_ids, min_citation_count.
        """
    )
    async def snippet_search(self, params: SnippetSearchParams) -> ToolResult:
        query: dict[str, Any] = {
            "query": params.query,
            "limit": params.limit,
            "year": params.year,
            "fieldsOfStudy": params.fields_of_study,
            "minCitationCount": params.min_citation_count,
            "paperIds": ",".join(normalize_paper_id(i) for i in params.paper_ids) or None,
        }
        response = await self.request("GET", f"{GRAPH}/snippet/search", params=query, headers=self._headers())
        payload = response.json() or {}
        records = []
        for hit in payload.get("data") or []:
            paper = (hit or {}).get("paper") or {}
            snippet = hit.get("snippet") or {}
            corpus = paper.get("corpusId")
            if not corpus:
                continue
            corpus = int(corpus) if str(corpus).isdigit() else corpus
            oa = paper.get("openAccessInfo") or {}
            authors = paper.get("authors") or []
            records.append(
                Record(
                    source=self.name,
                    id=f"CorpusId:{corpus}",
                    type="article",
                    title=strip_html(paper.get("title")),
                    url=f"{S2_WEB}/p/{corpus}",
                    authors=[a if isinstance(a, str) else (a or {}).get("name", "") for a in authors if a],
                    summary=strip_html(snippet.get("text")),
                    attributes=_compact(
                        {
                            "corpus_id": corpus,
                            "score": round(hit["score"], 4) if isinstance(hit.get("score"), (int, float)) else None,
                            "section": snippet.get("section"),
                            "snippet_kind": snippet.get("snippetKind"),
                            "oa_status": oa.get("status"),
                            "oa_license": oa.get("license"),
                        }
                    ),
                )
            )
        notes = self._notes()
        if records and params.resolve_ids:
            corpus_ids = list(dict.fromkeys(r.attributes["corpus_id"] for r in records))
            batch = await self.request(
                "POST",
                f"{GRAPH}/paper/batch",
                params={"fields": "paperId,corpusId,externalIds,year,publicationDate,venue"},
                json_body={"ids": [f"CorpusId:{c}" for c in corpus_ids]},
                headers=self._headers(),
            )
            resolved = {
                str(p.get("corpusId")): p for p in (batch.json() or []) if isinstance(p, dict) and p.get("corpusId")
            }
            for record in records:
                paper = resolved.get(str(record.attributes["corpus_id"]))
                if not paper:
                    continue
                ext = paper.get("externalIds") or {}
                pmc = ext.get("PubMedCentral")
                record.identifiers = normalize_identifiers({
                    "s2": paper.get("paperId"),
                    "doi": ext.get("DOI"),
                    "pmid": ext.get("PubMed"),
                    "pmcid": f"PMC{pmc}" if pmc else None,
                    "arxiv": ext.get("ArXiv"),
                })
                record.date = paper.get("publicationDate") or (str(paper["year"]) if paper.get("year") else None)
                if paper.get("paperId"):
                    record.url = f"{S2_WEB}/paper/{paper['paperId']}"
                if paper.get("venue"):
                    record.attributes["venue"] = paper["venue"]
        elif records:
            notes.append("snippet records carry only the S2 corpus id; use get_papers_batch with CorpusId:<id> for DOI/PMID")
        return ToolResult(
            source=self.name,
            tool="snippet_search",
            records=records,
            request_url=response.url,
            notes=notes,
        )
