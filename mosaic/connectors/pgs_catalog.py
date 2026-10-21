"""PGS Catalog connector (polygenic scores).

Docs: https://www.pgscatalog.org/rest/  (REST API v1.8.x)
Auth: none. Rate limit: 100 requests/minute per IP (we use 1.5/s).

Quirks:
* Paginated list endpoints return ``{"count", "next", "previous", "results"}`` and take
  ``limit`` / ``offset``.
* Unknown ids return HTTP 200 with an empty object ``{}`` (not 404).
* ``/score/search?trait_id=`` only matches scores annotated with exactly that trait and
  ignores ``include_children``; ``scores_for_trait`` therefore reads the trait's
  ``associated_pgs_ids`` (+ ``child_associated_pgs_ids``) and fetches those scores via
  ``/score/search?pgs_ids=``.
* Trait ids are EFO_/MONDO_/HP_/Orphanet_ style; mapped to canonical ``efo`` (EFO_#######),
  ``mondo`` (MONDO:#######), ``hp`` (HP:#######) on trait records. Score records carry only
  ``pgs``; their trait (``trait_efo``), source GWAS (``source_gwas_studies``) and publication
  (``publication_pmid`` / ``publication_doi``) are related entities kept in attributes (one
  paper or trait has many scores, so as identifiers they would merge unrelated scores).
  Publication records carry ``pmid`` / ``doi``.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import quote

from pydantic import Field

from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, tool

API = "https://www.pgscatalog.org/rest"
WEB = "https://www.pgscatalog.org"

_PGS = re.compile(r"^PGS\d{6}$", re.IGNORECASE)
_PGP = re.compile(r"^PGP\d{6}$", re.IGNORECASE)
_TRAIT_ID = re.compile(r"^([A-Za-z]+)[_:](\d+)$")


class PgsSearchParams(SearchParams):
    kind: Literal["traits", "scores"] = Field(
        "traits",
        description="traits (default): trait terms matching the text, with how many PGS exist for each; "
        "scores: polygenic scores for the best-matching trait (including child traits)",
    )


class GetScoreParams(ToolParams):
    pgs_id: str = Field(description="PGS Catalog score id, e.g. PGS000004")


class ScoresForTraitParams(ToolParams):
    trait_id: str = Field(
        description="Trait ontology id as used by PGS Catalog: MONDO_0005010 (coronary artery disorder), "
        "MONDO_0007254 (breast cancer), EFO_0004611 (LDL cholesterol); MONDO:0007254 also accepted. PGS Catalog "
        "uses its own current trait ids (often MONDO rather than legacy EFO), so find ids with search."
    )
    include_children: bool = Field(True, description="Also include scores for more specific child traits")
    limit: int = Field(20, ge=1, le=100, description="Maximum number of scores")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor from a previous call)")


class GetPublicationParams(ToolParams):
    publication: str = Field(
        description="PGS publication id (PGP000002), PubMed ID (30554720), or a PGS score id (PGS000004) to get the "
        "publication that developed it. DOI lookup is not supported upstream."
    )


def trait_identifiers(trait_id: str | None) -> dict[str, str]:
    if not trait_id:
        return {}
    match = _TRAIT_ID.match(trait_id.strip())
    if not match:
        return {}
    prefix, num = match.group(1).upper(), match.group(2)
    if prefix == "EFO":
        return {"efo": f"EFO_{num}"}
    if prefix in ("MONDO", "HP", "GO", "DOID"):
        return {prefix.lower(): f"{prefix}:{num}"}
    if prefix == "ORPHANET":
        return {"orphanet": num}
    return {}


def _normalize_trait_id(value: str) -> str:
    match = _TRAIT_ID.match(value.strip())
    if not match:
        return value.strip()
    prefix = match.group(1)
    prefix = "Orphanet" if prefix.upper() == "ORPHANET" else prefix.upper()
    return f"{prefix}_{match.group(2)}"


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {}, "NR")}


def _samples(samples: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for s in samples or []:
        out.append(
            _compact(
                {
                    "n": s.get("sample_number"),
                    "cases": s.get("sample_cases"),
                    "controls": s.get("sample_controls"),
                    "ancestry": s.get("ancestry_broad"),
                    "ancestry_detail": s.get("ancestry_free"),
                    "country": s.get("ancestry_country"),
                    "gwas_study": s.get("source_GWAS_catalog"),
                    "cohorts": [c.get("name_short") for c in s.get("cohorts") or [] if c.get("name_short")][:10],
                }
            )
        )
    return out


def _gwas_accessions(item: dict[str, Any]) -> list[str]:
    accs = []
    for s in item.get("samples_variants") or []:
        acc = s.get("source_GWAS_catalog")
        if acc and acc not in accs:
            accs.append(acc)
    return accs


def score_record(item: dict[str, Any]) -> Record:
    pgs_id = item.get("id") or ""
    pub = item.get("publication") or {}
    traits = [{"id": t.get("id"), "label": t.get("label")} for t in item.get("trait_efo") or []]
    identifiers: dict[str, Any] = {"pgs": pgs_id}
    gwas = _gwas_accessions(item)
    dist = item.get("ancestry_distribution") or {}
    ancestry = {
        stage: (dist.get(stage) or {}).get("dist")
        for stage in ("gwas", "dev", "eval")
        if (dist.get(stage) or {}).get("dist")
    }
    bits = [
        f"Polygenic score for {item.get('trait_reported') or ', '.join(t['label'] or '' for t in traits)}",
        f"{item.get('variants_number')} variants" if item.get("variants_number") is not None else None,
        f"method: {item.get('method_name')}" if item.get("method_name") else None,
        f"genome build {item.get('variants_genomebuild')}" if item.get("variants_genomebuild") not in (None, "NR") else None,
    ]
    if ancestry.get("gwas"):
        bits.append("GWAS ancestry: " + ", ".join(f"{k} {v}%" for k, v in ancestry["gwas"].items()))
    if pub.get("firstauthor"):
        bits.append(f"source: {pub.get('firstauthor')} et al., {pub.get('journal')} ({(pub.get('date_publication') or '')[:4]})")
    harmonized = {
        build: (files or {}).get("positions")
        for build, files in (item.get("ftp_harmonized_scoring_files") or {}).items()
        if (files or {}).get("positions")
    }
    return Record(
        source="pgs_catalog",
        id=pgs_id,
        type="other",
        title=f"{pgs_id}: {item.get('name') or ''} ({item.get('trait_reported') or 'polygenic score'})".replace(": (", " ("),
        url=f"{WEB}/score/{pgs_id}" if pgs_id else None,
        identifiers=identifiers,
        date=pub.get("date_publication"),
        authors=[pub["firstauthor"]] if pub.get("firstauthor") else [],
        summary="; ".join(b for b in bits if b),
        attributes=_compact(
            {
                "name": item.get("name"),
                "trait_reported": item.get("trait_reported"),
                "trait_additional": item.get("trait_additional"),
                "trait_efo": traits,
                "method_name": item.get("method_name"),
                "method_params": item.get("method_params"),
                "variants_number": item.get("variants_number"),
                "variants_interactions": item.get("variants_interactions") or None,
                "genome_build": item.get("variants_genomebuild"),
                "weight_type": item.get("weight_type"),
                "ancestry_distribution": ancestry,
                "gwas_samples": _samples(item.get("samples_variants")),
                "training_samples": _samples(item.get("samples_training")),
                "source_gwas_studies": gwas,
                "publication_id": pub.get("id"),
                "publication_pmid": str(pub["PMID"]) if pub.get("PMID") else None,
                "publication_doi": pub.get("doi"),
                "publication_title": pub.get("title"),
                "journal": pub.get("journal"),
                "scoring_file": item.get("ftp_scoring_file"),
                "harmonized_scoring_files": harmonized,
                "date_release": item.get("date_release"),
                "matches_publication": item.get("matches_publication"),
            }
        ),
    )


def trait_record(item: dict[str, Any]) -> Record:
    tid = item.get("id") or ""
    direct = item.get("associated_pgs_ids") or []
    child = item.get("child_associated_pgs_ids") or []
    return Record(
        source="pgs_catalog",
        id=tid,
        type="phenotype",
        title=item.get("label"),
        url=f"{WEB}/trait/{tid}" if tid else None,
        identifiers=trait_identifiers(tid),
        summary=item.get("description"),
        attributes=_compact(
            {
                "trait_categories": item.get("trait_categories"),
                "synonyms": (item.get("trait_synonyms") or [])[:10],
                "mapped_terms": (item.get("trait_mapped_terms") or [])[:10],
                "pgs_count": len(direct),
                "child_pgs_count": len(child),
                "pgs_ids": direct[:20],
                "child_traits": [
                    f"{c.get('label')} ({c.get('id')})" for c in (item.get("child_traits") or [])[:15] if isinstance(c, dict)
                ],
                "ontology_uri": item.get("url"),
            }
        ),
    )


def publication_record(item: dict[str, Any]) -> Record:
    pgp = item.get("id") or ""
    assoc = item.get("associated_pgs_ids") or {}
    authors = [a.strip() for a in (item.get("authors") or "").split(",") if a.strip()]
    return Record(
        source="pgs_catalog",
        id=pgp,
        type="article",
        title=item.get("title"),
        url=f"{WEB}/publication/{pgp}" if pgp else None,
        identifiers={"pmid": item.get("PMID"), "doi": item.get("doi")},
        date=item.get("date_publication"),
        authors=authors or ([item["firstauthor"]] if item.get("firstauthor") else []),
        attributes=_compact(
            {
                "journal": item.get("journal"),
                "pgs_developed": assoc.get("development"),
                "pgs_evaluated": assoc.get("evaluation"),
                "date_release": item.get("date_release"),
            }
        ),
    )


def _rank_traits(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    q = query.strip().lower()

    def key(item: dict[str, Any]) -> tuple[int, int]:
        label = (item.get("label") or "").lower()
        synonyms = [s.lower() for s in item.get("trait_synonyms") or []]
        tier = 0 if label == q else 1 if q in synonyms else 2 if label.startswith(q) else 3
        return tier, -len((item.get("associated_pgs_ids") or []) + (item.get("child_associated_pgs_ids") or []))

    return sorted(items, key=key)


class PgsCatalog(Connector):
    name = "pgs_catalog"
    title = "PGS Catalog"
    category = "genomics"
    description = (
        "Open catalog of ~7,000 published polygenic scores (PGS) for ~800 traits: variant counts, "
        "development method, GWAS/training sample ancestry, source publication (PMID/DOI) and "
        "downloadable scoring files. Use to find polygenic risk scores for a disease or trait."
    )
    homepage = "https://www.pgscatalog.org/"
    docs_url = "https://www.pgscatalog.org/rest/"
    auth = "none"
    rate_limit = 1.5  # documented: 100 requests/minute
    health_query = "breast cancer"
    nih_repositories = ("PGS Catalog",)

    @tool(
        description="""
        Search the PGS Catalog. kind=traits (default) returns trait terms (EFO/MONDO/HP ids) matching
        the text with the number of polygenic scores for each (direct and via child traits);
        kind=scores returns the polygenic scores for the best-matching trait. A PGS id (PGS000004) or
        publication id (PGP000002) as the query returns that score/publication. Example:
        query="coronary artery disease", kind=scores.
        """,
        federated=True,
    )
    async def search(self, params: PgsSearchParams) -> ToolResult:
        query = params.query.strip()
        if _PGS.match(query):
            result = await self.get_score(GetScoreParams(pgs_id=query))
            result.tool = "search"
            return result
        if _PGP.match(query):
            result = await self.get_publication(GetPublicationParams(publication=query))
            result.tool = "search"
            return result
        offset = parse_offset(params.cursor)
        if params.kind == "scores":
            lookup = await self.request("GET", f"{API}/trait/search", params={"term": query, "limit": 20})
            traits = _rank_traits(lookup.json().get("results") or [], query)
            if not traits:
                return ToolResult(source=self.name, tool="search", notes=[f"no PGS Catalog trait matches {query!r}"], request_url=lookup.url)
            best = traits[0]
            result = await self._scores_from_ids(
                "search", best, include_children=True, limit=params.limit, offset=offset
            )
            result.notes.insert(0, f"scores for trait {best.get('label')} ({best.get('id')}) and its child traits")
            return result
        response = await self.request(
            "GET", f"{API}/trait/search", params={"term": query, "limit": params.limit, "offset": offset}
        )
        payload = response.json() or {}
        items = payload.get("results") or []
        total = payload.get("count")
        nxt = offset + len(items)
        return ToolResult(
            source=self.name,
            tool="search",
            records=[trait_record(i) for i in _rank_traits(items, query)],
            total=total,
            next_cursor=str(nxt) if items and total and nxt < total else None,
            request_url=response.url,
            notes=[] if items else [f"no PGS Catalog trait matches {query!r}"],
        )

    @tool(
        description="""
        Full metadata for one polygenic score by id (PGS000004): reported and ontology-mapped trait
        (EFO/MONDO), number of variants, development method and parameters, GWAS/training sample
        sizes and ancestry distribution, source GWAS (GCST), publication PMID/DOI, and the scoring
        file URLs (original and harmonized GRCh37/GRCh38).
        """
    )
    async def get_score(self, params: GetScoreParams) -> ToolResult:
        pgs_id = params.pgs_id.strip().upper()
        if not _PGS.match(pgs_id):
            return ToolResult(source=self.name, tool="get_score", notes=[f"{params.pgs_id!r} is not a PGS id (PGS + 6 digits)"])
        response = await self.request("GET", f"{API}/score/{quote(pgs_id)}", allow_status=(404,))
        item = response.json() if response.status != 404 else {}
        if not item or not item.get("id"):
            return ToolResult(source=self.name, tool="get_score", notes=[f"score {pgs_id} not found in the PGS Catalog"], request_url=response.url)
        return ToolResult(source=self.name, tool="get_score", records=[score_record(item)], total=1, request_url=response.url)

    async def _scores_from_ids(
        self, tool_name: str, trait: dict[str, Any], *, include_children: bool, limit: int, offset: int
    ) -> ToolResult:
        ids = list(trait.get("associated_pgs_ids") or [])
        if include_children:
            ids += [i for i in trait.get("child_associated_pgs_ids") or [] if i not in ids]
        ids = sorted(set(ids))
        page = ids[offset : offset + limit]
        if not page:
            return ToolResult(
                source=self.name, tool=tool_name, total=len(ids),
                notes=[f"no polygenic scores for {trait.get('label')} ({trait.get('id')})"] if not ids else [],
            )
        response = await self.request(
            "GET", f"{API}/score/search", params={"pgs_ids": ",".join(page), "limit": len(page)}
        )
        items = (response.json() or {}).get("results") or []
        nxt = offset + len(page)
        return ToolResult(
            source=self.name,
            tool=tool_name,
            records=[score_record(i) for i in items],
            total=len(ids),
            next_cursor=str(nxt) if nxt < len(ids) else None,
            request_url=response.url,
        )

    @tool(
        description="""
        Polygenic scores developed for a trait, by PGS Catalog trait id (MONDO_0005010 coronary artery
        disorder, MONDO_0007254 breast cancer, EFO_0004611 LDL cholesterol). include_children (default true) adds scores for more
        specific sub-traits. Returns score records with variant counts, methods, ancestry and
        publication ids, sorted by PGS id. Find trait ids with search (kind=traits).
        """
    )
    async def scores_for_trait(self, params: ScoresForTraitParams) -> ToolResult:
        trait_id = _normalize_trait_id(params.trait_id)
        response = await self.request("GET", f"{API}/trait/{quote(trait_id)}", allow_status=(404,))
        trait = response.json() if response.status != 404 else {}
        if not trait or not trait.get("id"):
            return ToolResult(
                source=self.name, tool="scores_for_trait",
                notes=[f"trait {trait_id} not found in the PGS Catalog (it may use a different/newer ontology id); find ids with search kind=traits"],
                request_url=response.url,
            )
        result = await self._scores_from_ids(
            "scores_for_trait", trait, include_children=params.include_children,
            limit=params.limit, offset=parse_offset(params.cursor),
        )
        result.data = {"trait": {"id": trait.get("id"), "label": trait.get("label")}}
        return result

    @tool(
        description="""
        A PGS Catalog publication by PGP id, PubMed ID, or the PGS id of a score it developed: title, journal, date, authors and
        which PGS ids it developed or evaluated. Use to go from a paper to its polygenic scores.
        """
    )
    async def get_publication(self, params: GetPublicationParams) -> ToolResult:
        value = params.publication.strip()
        if _PGP.match(value):
            response = await self.request("GET", f"{API}/publication/{quote(value.upper())}", allow_status=(404,))
            item = response.json() if response.status != 404 else {}
            items = [item] if item and item.get("id") else []
        else:
            if value.isdigit():
                query = {"pmid": value}
            elif _PGS.match(value):
                query = {"pgs_id": value.upper()}
            else:
                return ToolResult(
                    source=self.name, tool="get_publication",
                    notes=[f"{value!r} is not a PGP id, PMID or PGS id (DOI lookup is not supported by the PGS Catalog API)"],
                )
            response = await self.request("GET", f"{API}/publication/search", params=query)
            items = (response.json() or {}).get("results") or []
        if not items:
            return ToolResult(source=self.name, tool="get_publication", notes=[f"publication {value} not found in the PGS Catalog"], request_url=response.url)
        return ToolResult(
            source=self.name, tool="get_publication",
            records=[publication_record(i) for i in items], total=len(items), request_url=response.url,
        )
