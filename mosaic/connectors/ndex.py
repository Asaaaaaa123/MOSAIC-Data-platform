"""NDEx (Network Data Exchange) connector.

Docs: https://home.ndexbio.org/using-the-ndex-server-api/ ; OpenAPI: https://www.ndexbio.org/v2/api-docs
(REST v2). Auth: none for public networks (private networks would need a user login, which
this connector never uses). No published rate limit; we use 3 requests/second.

Quirks:
* ``POST /search/network?start=&size=`` with ``{"searchString": ...}`` (Lucene syntax:
  ``TP53 AND apoptosis``, ``name:"NCI PID"``, ``nodeName:EGFR``); ``start`` is a page
  index, not an offset, so our cursor is converted to ``start = offset // limit``.
* Neighborhood queries (``/search/network/{uuid}/query`` and ``/interconnectquery``) return
  CX1: a list of aspect fragments (``nodes`` {@id, n, r}, ``edges`` {@id, s, t, i},
  ``nodeAttributes``/``edgeAttributes`` {po, n, v}). We parse it into compact node/edge
  summaries; the edge limit truncates large neighborhoods.
* Unknown network -> 404 ``NDEx_Object_Not_Found_Exception``.
Identifiers: custom key ``ndex`` (network UUID) and ``doi`` only when NDEx minted one for the
network itself. The DOI / PMID parsed from the ``reference`` property identify the cited paper (a
related entity, often a database paper shared by hundreds of networks), so they are kept in
``attributes['reference_doi']`` / ``attributes['reference_pmid']``; gene nodes carry ``gene_symbol`` / ``uniprot`` /
``entrez_gene`` / ``ensembl_gene`` taken from the node name and represents (``r``) field.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import quote

from pydantic import Field

from ..ids import normalize_doi
from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, parse_offset, strip_html, tool

API = "https://www.ndexbio.org/v2"
WEB = "https://www.ndexbio.org"

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
_PMID = re.compile(r"(?:pubmed[/:=]|PMID:?\s*)(\d{5,9})", re.IGNORECASE)
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9-]{0,14}$")


class NetworkParams(ToolParams):
    uuid: str = Field(description="NDEx network UUID, e.g. 640e2cef-795d-11e8-a4bf-0ac135e8bacf (from search_networks)")


class QueryNetworkParams(ToolParams):
    uuid: str = Field(description="NDEx network UUID (from search_networks)")
    genes: list[str] = Field(min_length=1, max_length=50, description="Gene/protein node names to query, e.g. ['TP53','MDM2']")
    mode: Literal["neighborhood", "interconnect"] = Field(
        "neighborhood",
        description="neighborhood (default): the genes plus their neighbors up to depth; interconnect: only edges "
        "among the query genes (and direct connectors)",
    )
    depth: int = Field(1, ge=1, le=2, description="Neighborhood depth (1 or 2) for mode=neighborhood")
    edge_limit: int = Field(500, ge=10, le=5000, description="Maximum edges the server returns")
    max_edges: int = Field(40, ge=1, le=200, description="Number of edges to list in the output")


def _iso(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _props(item: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in item.get("properties") or []:
        key = p.get("predicateString")
        if key and not key.startswith("__") and key != "@context" and p.get("value") not in (None, ""):
            out[key] = str(p.get("value"))
    return out


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def network_record(item: dict[str, Any]) -> Record:
    uuid = item.get("externalId") or ""
    props = _props(item)
    reference = props.get("reference") or ""
    ref_doi = m.group(1).rstrip(".);,") if (m := _DOI.search(reference)) else None
    ref_pmid = m.group(1) if (m := _PMID.search(reference)) else None
    authors = [a.strip() for a in re.split(r"[;,]\s*", strip_html(props.get("author")) or "") if a.strip()]
    return Record(
        source="ndex",
        id=uuid,
        type="pathway",
        title=item.get("name") or uuid,
        url=f"{WEB}/viewer/networks/{uuid}",
        identifiers={"ndex": uuid, "doi": item.get("doi")},
        date=_iso(item.get("modificationTime") or item.get("creationTime")),
        authors=authors[:20],
        summary=strip_html(item.get("description")),
        attributes=_compact(
            {
                "owner": item.get("owner"),
                "node_count": item.get("nodeCount"),
                "edge_count": item.get("edgeCount"),
                "version": item.get("version"),
                "organism": props.get("organism"),
                "network_type": props.get("networkType"),
                "labels": props.get("labels"),
                "disease": props.get("disease"),
                "tissue": props.get("tissue"),
                "rights": props.get("rights"),
                "reference": (strip_html(reference) or "")[:300] or None,
                "reference_doi": normalize_doi(ref_doi) if ref_doi else None,
                "reference_pmid": ref_pmid,
                "certified": item.get("isCertified") or None,
                "visibility": item.get("visibility"),
                "created": _iso(item.get("creationTime")),
            }
        ),
    )


def _aspects(cx: Any) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for fragment in cx if isinstance(cx, list) else []:
        if isinstance(fragment, dict):
            for key, values in fragment.items():
                if isinstance(values, list):
                    out.setdefault(key, []).extend(v for v in values if isinstance(v, dict))
    return out


def _node_identifiers(name: str | None, represents: str | None, aliases: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Canonical ids for a network node, plus all UniProt accessions seen.

    Loaders such as NCI-PID put an arbitrary (often secondary/TrEMBL) accession in ``r`` and
    the rest in ``alias``; a UniProt id is only used as an identifier when it is unambiguous.
    """
    ids: dict[str, Any] = {}
    uniprots: list[str] = []
    for ref in [represents or ""] + aliases:
        prefix, _, local = ref.partition(":")
        prefix = prefix.lower()
        if not local:
            continue
        if prefix in ("uniprot", "uniprotkb"):
            if local not in uniprots:
                uniprots.append(local)
        elif prefix in ("ncbigene", "entrez", "ncbi") and local.isdigit():
            ids.setdefault("entrez_gene", local)
        elif prefix == "ensembl" and local.upper().startswith("ENSG"):
            ids.setdefault("ensembl_gene", local)
        elif prefix == "hgnc.symbol":
            ids.setdefault("gene_symbol", local)
        elif prefix == "hgnc" and local.isdigit():
            ids.setdefault("hgnc", local)
    if len(uniprots) == 1:
        ids["uniprot"] = uniprots[0]
    if "gene_symbol" not in ids and name and _SYMBOL.match(name):
        ids["gene_symbol"] = name
    return ids, uniprots


class Ndex(Connector):
    name = "ndex"
    title = "NDEx - The Network Data Exchange"
    category = "biology"
    description = (
        "Public commons of 100k+ biological networks and pathway models (NCI-PID, SIGNOR, WikiPathways, "
        "BioPlex, INDRA, disease and drug-response networks) shared by researchers and Cytoscape users. "
        "Use to find interaction networks about a gene, pathway or disease and to extract a gene's "
        "neighborhood subnetwork."
    )
    homepage = "https://www.ndexbio.org/"
    docs_url = "https://home.ndexbio.org/using-the-ndex-server-api/"
    auth = "none"
    rate_limit = 3.0
    health_query = "TP53"
    nih_repositories = ("The Network Data Exchange (NDEx)",)

    @tool(
        description="""
        Search public NDEx networks by free text over names, descriptions, properties and node
        names (Lucene syntax: "TP53 apoptosis", "EGFR AND lung", name:"NCI PID", nodeName:BRCA1).
        Returns network UUIDs, names, owner, node/edge counts, organism, network type, reference
        DOI/PMID and description. Then use network_summary or query_network.
        """,
        federated=True,
    )
    async def search_networks(self, params: SearchParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        response = await self.request(
            "POST",
            f"{API}/search/network",
            params={"start": offset // params.limit, "size": params.limit},
            json_body={"searchString": params.query.strip()},
            cache_ttl=3600,
        )
        payload = response.json() or {}
        items = payload.get("networks") or []
        total = payload.get("numFound")
        nxt = offset + len(items)
        return ToolResult(
            source=self.name,
            tool="search_networks",
            records=[network_record(i) for i in items],
            total=total,
            next_cursor=str(nxt) if items and total and nxt < total else None,
            request_url=response.url,
            notes=[] if items else [f"no public NDEx network matches {params.query!r}"],
        )

    @tool(
        description="""
        Metadata for one NDEx network by UUID: name, description, owner, node and edge counts,
        version, organism, network type, labels, rights/license, reference (DOI/PMID) and all
        other network properties (data.properties).
        """
    )
    async def network_summary(self, params: NetworkParams) -> ToolResult:
        uuid = params.uuid.strip().lower()
        if not _UUID.match(uuid):
            return ToolResult(source=self.name, tool="network_summary", notes=[f"{params.uuid!r} is not an NDEx network UUID"])
        response = await self.request("GET", f"{API}/network/{quote(uuid)}/summary", allow_status=(404,))
        if response.status == 404:
            return ToolResult(source=self.name, tool="network_summary", notes=[f"network {uuid} not found (or not public) in NDEx"])
        item = response.json() or {}
        props = {k: (strip_html(v) or "")[:500] for k, v in _props(item).items()}
        return ToolResult(
            source=self.name, tool="network_summary", records=[network_record(item)], total=1,
            data={"properties": props}, request_url=response.url,
        )

    @tool(
        description="""
        Extract the subnetwork around query genes from one NDEx network (by UUID): mode=neighborhood
        (genes + neighbors up to depth 1-2) or interconnect (edges among the query genes). Returns
        gene node records (gene symbol / UniProt ids), and data with node/edge counts, the most
        connected nodes, interaction type counts and the first edges as {source, interaction,
        target, citations}. Example: uuid of 'NCI PID - Complete Interactions', genes=['TP53','MDM2'].
        """
    )
    async def query_network(self, params: QueryNetworkParams) -> ToolResult:
        uuid = params.uuid.strip().lower()
        if not _UUID.match(uuid):
            return ToolResult(source=self.name, tool="query_network", notes=[f"{params.uuid!r} is not an NDEx network UUID"])
        genes = [g.strip() for g in params.genes if g.strip()]
        path = "query" if params.mode == "neighborhood" else "interconnectquery"
        body: dict[str, Any] = {"searchString": " ".join(genes), "edgeLimit": params.edge_limit}
        if params.mode == "neighborhood":
            body["searchDepth"] = params.depth
        response = await self.request(
            "POST", f"{API}/search/network/{quote(uuid)}/{path}", json_body=body, allow_status=(404,), cache_ttl=3600
        )
        if response.status == 404:
            return ToolResult(source=self.name, tool="query_network", notes=[f"network {uuid} not found (or not public) in NDEx"])
        aspects = _aspects(response.json())
        nodes = {n.get("@id"): n for n in aspects.get("nodes", [])}
        edges = aspects.get("edges", [])
        node_attrs: dict[Any, dict[str, Any]] = {}
        for a in aspects.get("nodeAttributes", []):
            node_attrs.setdefault(a.get("po"), {})[a.get("n")] = a.get("v")
        edge_attrs: dict[Any, dict[str, Any]] = {}
        for a in aspects.get("edgeAttributes", []):
            edge_attrs.setdefault(a.get("po"), {})[a.get("n")] = a.get("v")
        degree: Counter = Counter()
        for e in edges:
            degree[e.get("s")] += 1
            degree[e.get("t")] += 1

        def label(node_id: Any) -> str:
            node = nodes.get(node_id) or {}
            return str(node.get("n") or node.get("r") or node_id)

        query_set = {g.upper() for g in genes}
        edge_rows = []
        ordered = sorted(
            edges,
            key=lambda e: (
                -(label(e.get("s")).upper() in query_set) - (label(e.get("t")).upper() in query_set),
                -(degree[e.get("s")] + degree[e.get("t")]),
            ),
        )
        for e in ordered[: params.max_edges]:
            attrs = edge_attrs.get(e.get("@id"), {})
            citations = attrs.get("citation") or attrs.get("citations") or []
            if isinstance(citations, str):
                citations = [citations]
            edge_rows.append(
                _compact(
                    {
                        "source": label(e.get("s")),
                        "interaction": e.get("i"),
                        "target": label(e.get("t")),
                        "citations": [str(c) for c in citations][:5],
                    }
                )
            )
        records = []
        for node_id, _count in degree.most_common():
            node = nodes.get(node_id)
            if not node:
                continue
            attrs = node_attrs.get(node_id, {})
            aliases = attrs.get("alias") or []
            aliases = aliases if isinstance(aliases, list) else [str(aliases)]
            ids, uniprots = _node_identifiers(node.get("n"), node.get("r"), [str(a) for a in aliases])
            if not ids.get("gene_symbol") and not uniprots:
                continue
            records.append(
                Record(
                    source="ndex",
                    id=f"{uuid}:{node_id}",
                    type="gene" if ids.get("gene_symbol") else "protein",
                    title=node.get("n") or node.get("r"),
                    url=f"{WEB}/viewer/networks/{uuid}",
                    identifiers=ids,
                    attributes=_compact(
                        {
                            "degree_in_subnetwork": degree[node_id],
                            "node_type": attrs.get("type"),
                            "represents": node.get("r"),
                            "uniprot_accessions": uniprots[:8] if len(uniprots) > 1 else None,
                            "query_gene": (node.get("n") or "").upper() in query_set or None,
                        }
                    ),
                )
            )
            if len(records) >= 50:
                break
        notes = []
        if len(edges) >= params.edge_limit:
            notes.append(f"edge limit {params.edge_limit} reached: the neighborhood is truncated; raise edge_limit or query fewer genes")
        if not nodes:
            notes.append("none of the query genes were found in this network (node names must match exactly)")
        return ToolResult(
            source=self.name,
            tool="query_network",
            records=records,
            total=len(nodes),
            data={
                "network": uuid,
                "mode": params.mode,
                "node_count": len(nodes),
                "edge_count": len(edges),
                "interaction_types": dict(Counter(e.get("i") for e in edges if e.get("i")).most_common(10)),
                "hub_nodes": [f"{label(n)} ({c})" for n, c in degree.most_common(10)],
                "edges": edge_rows,
            },
            request_url=response.url,
            notes=notes,
        )
