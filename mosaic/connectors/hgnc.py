"""HUGO Gene Nomenclature Committee (HGNC) REST connector.

Docs: https://www.genenames.org/help/rest/ (searchable/stored fields: ``GET /info``).

Auth: none (keyless). Rate limit: HGNC asks for at most 10 requests/second; we use 8.
Responses are Solr-like JSON when the request carries ``Accept: application/json``.

Quirks:
- ``/search/{query}`` returns *all* matching ``{hgnc_id, symbol, score}`` docs (no server paging), so
  pagination is an offset into that list; details come from ``/fetch/hgnc_id/A+OR+B...`` in one call.
- Query syntax: ``symbol:BRAF*``, ``alias_symbol:p53``, ``locus_type:"RNA, long non-coding"``, boolean
  ``AND``/``OR``. Multi-word free text is matched loosely (e.g. "EGFR lung cancer" does not return EGFR),
  so plain-text queries are re-ranked: exact symbol / alias / previous-symbol hits for symbol-like
  tokens come first.
- Encoded slashes (%2F) in the path are rejected by the server (404), so "/" in queries becomes a space.
- Withdrawn symbols are returned with ``status`` "Entry Withdrawn" / "Symbol Withdrawn".

Identifiers on gene records: hgnc, gene_symbol, entrez_gene, ensembl_gene, uniprot (first),
refseq (first), omim (first), ensembl_transcript (MANE Select). Everything else (MGI/RGD orthologs,
CCDS, UCSC, enzyme IDs, gene groups, PubMed IDs, Orphanet gene ID, COSMIC...) is in attributes.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import quote

from pydantic import Field

from ..http import SourceError
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, tool

API = "https://rest.genenames.org"
_HEADERS = {"Accept": "application/json"}

FetchField = Literal[
    "auto",
    "symbol",
    "hgnc_id",
    "alias_symbol",
    "prev_symbol",
    "entrez_id",
    "ensembl_gene_id",
    "uniprot_ids",
    "omim_id",
    "refseq_accession",
    "mane_select",
    "ccds_id",
    "mgd_id",
    "rgd_id",
    "ucsc_id",
    "vega_id",
    "ena",
    "name",
]


class SearchGenesParams(SearchParams):
    pass


class FetchGeneParams(ToolParams):
    value: str = Field(
        description=(
            "Identifier or symbol to resolve, e.g. BRAF, HGNC:1097, 673 (NCBI Gene), ENSG00000157764, "
            "P15056 (UniProt), NM_004333, an old symbol (prev_symbol) or alias (ERBB1)"
        )
    )
    field: FetchField = Field(
        "auto",
        description=(
            "Which HGNC field to match. 'auto' infers it from the value's format (HGNC:#### -> hgnc_id, digits -> "
            "entrez_id, ENSG -> ensembl_gene_id, UniProt accession -> uniprot_ids, NM_/NR_/XM_ -> refseq_accession, "
            "MGI:/RGD: -> mgd_id/rgd_id, CCDS -> ccds_id, ENST -> mane_select) and otherwise tries symbol, then "
            "alias_symbol, then prev_symbol. Use omim_id explicitly for OMIM numbers."
        ),
    )


class GeneGroupParams(ToolParams):
    group_id: int | None = Field(None, description="HGNC gene group ID, e.g. 1157 (RAF family), 588 (Protein kinases)")
    symbol: str | None = Field(
        None, description="Alternatively, a gene symbol: returns the members of every group this gene belongs to"
    )
    limit: int = Field(100, ge=1, le=100, description="Maximum number of member genes to return")
    cursor: str | None = Field(None, description="Pagination cursor (offset) from a previous result")


_UNIPROT = re.compile(r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})$")
_FIELD_SYNTAX = re.compile(r"(?:^|\s)[A-Za-z_]+:|\b(?:AND|OR|NOT)\b|[*\"]")


def _infer_field(value: str) -> list[str]:
    v = value.strip()
    upper = v.upper()
    if re.fullmatch(r"HGNC:\d+", upper):
        return ["hgnc_id"]
    if v.isdigit():
        return ["entrez_id"]
    if upper.startswith("ENSG"):
        return ["ensembl_gene_id"]
    if upper.startswith("ENST"):
        return ["mane_select"]
    if re.fullmatch(r"(?:NM|NR|XM|XR|NG|NP)_\d+(?:\.\d+)?", upper):
        return ["refseq_accession"]
    if upper.startswith("MGI:"):
        return ["mgd_id"]
    if upper.startswith("RGD:"):
        return ["rgd_id"]
    if upper.startswith("CCDS"):
        return ["ccds_id"]
    if _UNIPROT.fullmatch(upper.split("-")[0]):
        return ["uniprot_ids", "symbol", "alias_symbol", "prev_symbol"]
    return ["symbol", "alias_symbol", "prev_symbol"]


def _normalize_value(field: str, value: str) -> str:
    v = value.strip()
    if field == "hgnc_id":
        digits = re.sub(r"\D", "", v)
        return f"HGNC:{digits}" if digits else v
    if field in ("ensembl_gene_id", "uniprot_ids"):
        return v.upper().split(".")[0] if field == "ensembl_gene_id" else v.upper().split("-")[0]
    if field == "refseq_accession":
        return v.upper().split(".")[0]
    if field in ("mgd_id", "rgd_id"):
        return v.upper()
    return v


def _path_value(value: str) -> str:
    """Quote a user value for the URL path; HGNC rejects encoded slashes, so drop them."""
    return quote(value.replace("/", " ").strip(), safe=":*\"")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _record(doc: dict[str, Any]) -> Record:
    hgnc_id = doc.get("hgnc_id") or ""
    symbol = doc.get("symbol")
    name = doc.get("name")
    mane = _as_list(doc.get("mane_select"))
    groups = [
        {"id": gid, "name": gname}
        for gid, gname in zip(_as_list(doc.get("gene_group_id")), _as_list(doc.get("gene_group")))
    ]
    status = doc.get("status")
    summary_bits = []
    if symbol and name:
        summary_bits.append(f"{symbol} ({name})")
    if doc.get("locus_type"):
        summary_bits.append(f"is a {doc['locus_type']}")
    if doc.get("location"):
        summary_bits.append(f"at {doc['location']}")
    summary = " ".join(summary_bits) + "." if summary_bits else None
    if summary and groups:
        summary += " Gene groups: " + "; ".join(g["name"] for g in groups if g.get("name")) + "."
    if summary and status and status != "Approved":
        summary = f"[{status}] " + summary
    attributes = {
        "symbol": symbol,
        "name": name,
        "status": status,
        "locus_group": doc.get("locus_group"),
        "locus_type": doc.get("locus_type"),
        "location": doc.get("location"),
        "alias_symbols": _as_list(doc.get("alias_symbol")),
        "alias_names": _as_list(doc.get("alias_name")),
        "prev_symbols": _as_list(doc.get("prev_symbol")),
        "prev_names": _as_list(doc.get("prev_name")),
        "gene_groups": groups,
        "uniprot_ids": _as_list(doc.get("uniprot_ids")),
        "refseq_accessions": _as_list(doc.get("refseq_accession")),
        "omim_ids": _as_list(doc.get("omim_id")),
        "mane_select": mane,
        "ccds_ids": _as_list(doc.get("ccds_id")),
        "mgd_ids": _as_list(doc.get("mgd_id")),
        "rgd_ids": _as_list(doc.get("rgd_id")),
        "ucsc_id": doc.get("ucsc_id"),
        "vega_id": doc.get("vega_id"),
        "ena": _as_list(doc.get("ena")),
        "enzyme_ids": _as_list(doc.get("enzyme_id")),
        "cosmic": doc.get("cosmic"),
        "orphanet_gene_id": doc.get("orphanet"),
        "lncipedia": doc.get("lncipedia"),
        "mirbase": doc.get("mirbase"),
        "rna_central_ids": _as_list(doc.get("rna_central_id")),
        "pubmed_ids": [str(p) for p in _as_list(doc.get("pubmed_id"))][:20],
        "date_modified": (doc.get("date_modified") or "")[:10] or None,
        "date_symbol_changed": (doc.get("date_symbol_changed") or "")[:10] or None,
    }
    return Record(
        source="hgnc",
        id=hgnc_id,
        type="gene",
        title=f"{symbol}: {name}" if symbol and name else (symbol or hgnc_id),
        url=f"https://www.genenames.org/data/gene-symbol-report/#!/hgnc_id/{hgnc_id}" if hgnc_id else None,
        identifiers={
            "hgnc": hgnc_id,
            "gene_symbol": symbol,
            "entrez_gene": doc.get("entrez_id"),
            "ensembl_gene": doc.get("ensembl_gene_id"),
            "ensembl_transcript": next((m for m in mane if str(m).upper().startswith("ENST")), None),
            "uniprot": _as_list(doc.get("uniprot_ids")),
            # Versionless, like HGNC's own refseq_accession field (MANE NM_ first).
            "refseq": (
                next((str(m) for m in mane if str(m).upper().startswith("NM_")), None)
                or first_or_none(doc.get("refseq_accession"))
                or ""
            ).split(".")[0]
            or None,
            "omim": _as_list(doc.get("omim_id")),
        },
        date=(doc.get("date_approved_reserved") or "")[:10] or None,
        summary=summary,
        attributes={k: v for k, v in attributes.items() if v not in (None, [], "", {})},
    )


def first_or_none(value: Any) -> Any:
    items = _as_list(value)
    return items[0] if items else None


def _symbol_tokens(query: str) -> list[str]:
    """Tokens in a plain-text query that plausibly are gene symbols (EGFR, TP53, HLA-A, p53)."""
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-.@]*", query)
    if len(tokens) == 1:
        return tokens
    out = []
    for tok in tokens:
        letters = re.sub(r"[^A-Za-z]", "", tok)
        if not letters:
            continue
        if (letters.isupper() and len(tok) >= 2) or re.search(r"\d", tok):
            out.append(tok)
    return out[:6]


class Hgnc(Connector):
    name = "hgnc"
    title = "HGNC (HUGO Gene Nomenclature Committee)"
    category = "genomics"
    description = (
        "Authoritative human gene nomenclature for ~44k approved genes: approved symbols and names, "
        "aliases and previous symbols, locus type and cytogenetic location, gene groups, and curated "
        "cross-references (NCBI Gene, Ensembl, UniProt, RefSeq, OMIM, MANE). Best for resolving gene "
        "symbols/synonyms and as the identifier hub linking gene-centric databases."
    )
    homepage = "https://www.genenames.org/"
    docs_url = "https://www.genenames.org/help/rest/"
    auth = "none"
    rate_limit = 8.0
    health_query = "BRAF"
    nih_repositories = ("HUGO Gene Nomenclature Committee (HGNC)",)

    async def _get(self, path: str, allow_status: tuple[int, ...] = ()) -> Any:
        return await self.request("GET", f"{API}/{path}", headers=_HEADERS, allow_status=allow_status)

    async def _fetch_ids(self, hgnc_ids: list[str]) -> tuple[dict[str, dict[str, Any]], str | None]:
        if not hgnc_ids:
            return {}, None
        response = await self._get("fetch/hgnc_id/" + "+OR+".join(quote(i, safe=":") for i in hgnc_ids))
        docs = (response.json().get("response") or {}).get("docs") or []
        return {d.get("hgnc_id"): d for d in docs if d.get("hgnc_id")}, response.url

    async def _search_docs(self, query: str) -> tuple[list[dict[str, Any]], int, str]:
        response = await self._get(f"search/{_path_value(query)}", allow_status=(400,))
        if response.status == 400:
            raise SourceError(self.name, f"HGNC rejected the query {query!r}: {response.text[:200]}", status=400)
        body = response.json().get("response") or {}
        return list(body.get("docs") or []), int(body.get("numFound") or 0), response.url

    @tool(
        description="""
        Search HGNC human gene nomenclature. Returns gene records with approved symbol and name,
        HGNC ID, locus type, chromosomal location, aliases/previous symbols and cross-references
        (NCBI Gene, Ensembl, UniProt, RefSeq, OMIM). Accepts plain text (gene symbols, names, aliases:
        "BRAF", "tumor protein p53", "ERBB1") or field syntax: symbol:BRAF*, alias_symbol:p53,
        prev_symbol:MLL, name:"tumor protein", locus_type:"RNA, long non-coding", combined with AND/OR.
        Exact symbol/alias matches in plain-text queries are ranked first. Use fetch_gene when you
        already have a symbol or an external ID to resolve.
        """,
        federated=True,
    )
    async def search_genes(self, params: SearchGenesParams) -> ToolResult:
        query = params.query.strip()
        docs, num_found, url = await self._search_docs(query)
        if not _FIELD_SYNTAX.search(query):
            tokens = _symbol_tokens(query)
            if tokens:
                clauses = " OR ".join(
                    f'{f}:"{t}"' for t in tokens for f in ("symbol", "alias_symbol", "prev_symbol")
                )
                boosted, _, _ = await self._search_docs(clauses)
                exact = {t.upper() for t in tokens}
                boosted.sort(key=lambda d: 0 if (d.get("symbol") or "").upper() in exact else 1)
                upstream = {d.get("hgnc_id") for d in docs}
                num_found += sum(1 for d in boosted if d.get("hgnc_id") not in upstream)
                seen = {d.get("hgnc_id") for d in boosted}
                docs = boosted + [d for d in docs if d.get("hgnc_id") not in seen]
        offset = parse_offset(params.cursor)
        page = docs[offset : offset + params.limit]
        ids = [d.get("hgnc_id") for d in page if d.get("hgnc_id")]
        details, _ = await self._fetch_ids(ids)
        records = [_record(details[i]) for i in ids if i in details]
        nxt = offset + len(page)
        return ToolResult(
            source=self.name,
            tool="search_genes",
            records=records,
            total=num_found,
            next_cursor=str(nxt) if page and nxt < len(docs) else None,
            notes=[] if docs else [f"no HGNC genes match {query!r}"],
            request_url=url,
        )

    @tool(
        description="""
        Resolve one human gene in HGNC, the gene cross-reference hub. Accepts an approved symbol
        (BRAF), HGNC ID (HGNC:1097), NCBI Gene ID (673), Ensembl gene (ENSG00000157764), UniProt
        accession (P15056), RefSeq (NM_004333), MANE transcript (ENST...), MGI/RGD ortholog IDs,
        OMIM (set field=omim_id), or an outdated/alias symbol (ERBB1 -> EGFR, MLL -> KMT2A): with
        field=auto it tries symbol, then alias_symbol, then prev_symbol and says which matched.
        Returns the approved symbol, name, locus type, location, gene groups, and all
        cross-references (identifiers + attributes). Use this to normalize gene names before
        querying other databases.
        """
    )
    async def fetch_gene(self, params: FetchGeneParams) -> ToolResult:
        raw = params.value.strip()
        fields = _infer_field(raw) if params.field == "auto" else [params.field]
        notes: list[str] = []
        last_url = None
        for field in fields:
            value = _normalize_value(field, raw)
            response = await self._get(f"fetch/{field}/{_path_value(value)}", allow_status=(400, 404))
            last_url = response.url
            if response.status != 200:
                continue
            docs = (response.json().get("response") or {}).get("docs") or []
            if not docs:
                continue
            if field in ("alias_symbol", "prev_symbol"):
                symbols = ", ".join(d.get("symbol") or "?" for d in docs)
                kind = "an alias" if field == "alias_symbol" else "a previous symbol"
                notes.append(f"{raw} is {kind} of {symbols}")
            if len(docs) > 1:
                notes.append(f"{len(docs)} HGNC genes match {field}={value}")
            return ToolResult(
                source=self.name,
                tool="fetch_gene",
                records=[_record(d) for d in docs],
                total=len(docs),
                notes=notes,
                request_url=response.url,
            )
        return ToolResult(
            source=self.name,
            tool="fetch_gene",
            notes=[f"no HGNC gene found for {raw!r} (tried {', '.join(fields)})"],
            request_url=last_url,
        )

    @tool(
        description="""
        List the member genes of an HGNC gene group (curated families such as "RAF family",
        "Protein kinases", "Zinc fingers C2H2-type", "CD molecules"). Pass group_id, or a gene
        symbol to get every group that gene belongs to with their members. Returns gene records
        (with cross-references) and, in data, the groups (id, name, member count). Group IDs appear
        in fetch_gene results under attributes.gene_groups.
        """
    )
    async def gene_group(self, params: GeneGroupParams) -> ToolResult:
        if params.group_id is None and not (params.symbol and params.symbol.strip()):
            raise SourceError(self.name, "gene_group needs group_id or symbol")
        groups: list[dict[str, Any]] = []
        notes: list[str] = []
        if params.group_id is not None:
            group_ids = [params.group_id]
        else:
            symbol = params.symbol.strip()
            response = await self._get(f"fetch/symbol/{_path_value(symbol)}", allow_status=(400, 404))
            docs = (response.json().get("response") or {}).get("docs") or [] if response.status == 200 else []
            if not docs:
                return ToolResult(
                    source=self.name,
                    tool="gene_group",
                    notes=[f"{symbol!r} is not an approved HGNC symbol (try fetch_gene to resolve aliases)"],
                    request_url=response.url,
                )
            gene = docs[0]
            group_ids = [int(g) for g in _as_list(gene.get("gene_group_id")) if str(g).isdigit()]
            names = dict(zip(_as_list(gene.get("gene_group_id")), _as_list(gene.get("gene_group"))))
            if not group_ids:
                return ToolResult(
                    source=self.name,
                    tool="gene_group",
                    notes=[f"{symbol} is not assigned to any HGNC gene group"],
                    request_url=response.url,
                )
            groups = [{"id": gid, "name": names.get(gid)} for gid in group_ids]
        members: list[dict[str, Any]] = []
        url = None
        for gid in group_ids[:5]:
            response = await self._get(f"fetch/gene_group_id/{int(gid)}", allow_status=(400, 404, 500))
            url = response.url
            if response.status != 200:
                notes.append(f"gene group {gid} not found")
                continue
            docs = (response.json().get("response") or {}).get("docs") or []
            if not docs:
                notes.append(f"gene group {gid} not found (no member genes)")
                continue
            name = None
            for d in docs:
                for g_id, g_name in zip(_as_list(d.get("gene_group_id")), _as_list(d.get("gene_group"))):
                    if g_id == gid:
                        name = g_name
                        break
                if name:
                    break
            entry = next((g for g in groups if g["id"] == gid), None)
            if entry is None:
                entry = {"id": gid, "name": name}
                groups.append(entry)
            entry["name"] = entry.get("name") or name
            entry["member_count"] = len(docs)
            entry["url"] = f"https://www.genenames.org/data/genegroup/#!/group/{gid}"
            seen = {m.get("hgnc_id") for m in members}
            members.extend(d for d in docs if d.get("hgnc_id") not in seen)
        if len(group_ids) > 5:
            notes.append(f"gene belongs to {len(group_ids)} groups; members of the first 5 are listed")
        if params.group_id is not None and not members:
            return ToolResult(
                source=self.name, tool="gene_group", notes=notes or [f"gene group {params.group_id} not found"],
                request_url=url,
            )
        members.sort(key=lambda d: d.get("symbol") or "")
        offset = parse_offset(params.cursor)
        page = members[offset : offset + params.limit]
        nxt = offset + len(page)
        return ToolResult(
            source=self.name,
            tool="gene_group",
            records=[_record(d) for d in page],
            total=len(members),
            next_cursor=str(nxt) if page and nxt < len(members) else None,
            data={"groups": groups},
            notes=notes,
            request_url=url,
        )
