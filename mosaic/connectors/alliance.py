"""Alliance of Genome Resources (AGR) connector.

Docs: https://www.alliancegenome.org/swagger-ui/ (OpenAPI JSON at https://www.alliancegenome.org/openapi
with ``Accept: application/json``). Auth: none. Rate limits: none published; we stay at 3 req/s.

Integrates human (HGNC, via RGD), mouse (MGI), rat (RGD), zebrafish (ZFIN), fly (FlyBase),
worm (WormBase), yeast (SGD) and frog (Xenbase) gene, disease, allele, model and phenotype data.

Quirks:
- ``/api/search`` needs the internal category names (``gene_search_result``,
  ``disease_search_result``, ``allele_search_result``, ``model_search_result``); plain ``gene``
  silently returns ``{"total": 0}``. ``species`` filters by full species name. ``limit``/``offset``.
- ``/api/gene/{id}`` returns HTTP 400 "No gene found" for unknown ids (treated as not-found).
- Gene -> disease annotations come from ``POST /api/disease`` with a JSON array of gene ids;
  disease -> genes from ``GET /api/disease/{DOID}/genes``. Tables page with ``limit`` + 1-based ``page``.
- Reference lists mix PMID: and MOD publication ids (RGD:, MGI:, ZFIN:ZDB-PUB...); only PMID: values
  are kept, in ``attributes['pmids']``. Supporting papers are not identifiers of an annotation (one
  paper supports many gene-disease / phenotype annotations, which would otherwise merge).
- Gene-disease association records carry the partner entity's ids (the disease for gene_diseases,
  the gene for disease_genes), as the record lists that partner. Model records carry only the model id;
  the modelled diseases are ``attributes['disease_models']`` (a mouse strain is not the disease).
- Allele/model names contain <sup> markup; stripped.

Identifier keys: canonical hgnc, gene_symbol (human genes only), entrez_gene, ensembl_gene, uniprot,
omim, doid, mesh, umls, orphanet, ncit; non-canonical model-organism keys ``mgi``, ``rgd``,
``zfin``, ``flybase``, ``wormbase``, ``sgd``, ``xenbase`` (values are the full prefixed ids), plus
``panther``, ``snomedct``, ``icd10``, ``gard``.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import quote

from pydantic import Field

from ..models import Record, ToolResult
from .base import Connector, SearchParams, ToolParams, first, parse_offset, strip_html, tool
from .ols import map_curie

API = "https://www.alliancegenome.org/api"
SITE = "https://www.alliancegenome.org"

_MOD_KEYS = {
    "MGI": "mgi",
    "RGD": "rgd",
    "ZFIN": "zfin",
    "FB": "flybase",
    "WB": "wormbase",
    "SGD": "sgd",
    "XENBASE": "xenbase",
}

_SEARCH_CATEGORIES = {
    "gene": "gene_search_result",
    "disease": "disease_search_result",
    "allele": "allele_search_result",
    "model": "model_search_result",
    "go": "go_search_result",
}

SPECIES = Literal[
    "Homo sapiens",
    "Mus musculus",
    "Rattus norvegicus",
    "Danio rerio",
    "Drosophila melanogaster",
    "Caenorhabditis elegans",
    "Saccharomyces cerevisiae",
    "Xenopus tropicalis",
    "Xenopus laevis",
]


_SUP = re.compile(r"<sup>(.*?)</sup>", re.IGNORECASE | re.DOTALL)


def _clean(text: str | None) -> str | None:
    """Strip markup; allele superscripts become MGI-style angle brackets (Pax6<Sey>)."""
    if not text:
        return text
    marked = _SUP.sub(lambda m: "" + m.group(1) + "", text)
    cleaned = strip_html(marked)
    return cleaned.replace("", "<").replace("", ">").replace(" <", "<") if cleaned else cleaned


def _text(value: Any) -> str | None:
    if isinstance(value, dict):
        return _clean(value.get("displayText") or value.get("formatText"))
    if isinstance(value, str):
        return _clean(value)
    return None


def _xref_identifiers(curies: list[str]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for curie in curies:
        if not isinstance(curie, str) or ":" not in curie:
            continue
        prefix, local = curie.split(":", 1)
        up = prefix.upper()
        if up in _MOD_KEYS:
            ids.setdefault(_MOD_KEYS[up], curie)
            continue
        if up.startswith("SNOMEDCT"):
            ids.setdefault("snomedct", local)
            continue
        if up == "PANTHER":
            ids.setdefault("panther", local)
            continue
        if up == "MIM" and not local.isdigit():
            continue  # OMIM phenotypic series (PS######), not an OMIM entry
        mapped = map_curie(curie)
        if mapped:
            ids.setdefault(mapped[0], mapped[1])
    return ids


def gene_identifiers(gene_id: str | None, xrefs: list[str] | None = None, *, symbol: str | None = None,
                     uniprot: str | None = None) -> dict[str, str]:
    own_prefix = (gene_id or "").split(":", 1)[0].upper()
    human = own_prefix == "HGNC"
    kept = []
    for x in xrefs or []:
        if not isinstance(x, str) or ":" not in x:
            continue
        prefix = x.split(":", 1)[0].upper()
        if prefix in _MOD_KEYS and prefix != own_prefix:
            continue  # e.g. RGD's record id for a human gene: not this gene's MOD identity
        if prefix in ("OMIM", "MIM") and not human:
            continue  # MODs cross-reference the human ortholog's OMIM entry
        kept.append(x)
    ids = _xref_identifiers([gene_id or "", *kept])
    if uniprot:
        ids["uniprot"] = uniprot.split(":", 1)[-1]
    if symbol and (gene_id or "").startswith("HGNC:"):
        ids["gene_symbol"] = symbol
    return ids


def _pmids(publications: Any) -> list[str]:
    out: list[str] = []
    for p in publications or []:
        cur = p.get("referencedCurie") if isinstance(p, dict) else p
        if isinstance(cur, str) and cur.upper().startswith("PMID:"):
            pid = cur.split(":", 1)[1]
            if pid not in out:
                out.append(pid)
    return out


def _gene_brief(g: dict[str, Any]) -> dict[str, Any]:
    taxon = g.get("taxon") or {}
    return {
        "id": g.get("primaryExternalId") or g.get("curie"),
        "symbol": _text(g.get("geneSymbol")),
        "name": _text(g.get("geneFullName")),
        "species": taxon.get("name"),
        "taxon": taxon.get("curie"),
    }


def _search_record(item: dict[str, Any]) -> Record:
    cat = item.get("category") or ""
    curie = item.get("id") or item.get("curie") or item.get("primaryKey") or ""
    xrefs = [x for x in item.get("crossReferences") or [] if isinstance(x, str)]
    if cat == "gene_search_result":
        rtype, url = "gene", f"{SITE}/gene/{curie}"
        # search xrefs list UniProt accessions unordered (often TrEMBL first): keep them out of identifiers;
        # get_gene exposes the canonical protein.
        ids = gene_identifiers(curie, [x for x in xrefs if not x.startswith("UniProtKB:")], symbol=item.get("symbol"))
        summary = item.get("automatedGeneDescription") or item.get("geneDescription")
        title = item.get("symbol") or item.get("name")
    elif cat == "disease_search_result":
        rtype, url = "disease", f"{SITE}/disease/{curie}"
        ids = _xref_identifiers([curie, *xrefs])
        summary = item.get("definition")
        title = item.get("name")
    elif cat == "allele_search_result":
        rtype, url = "variant", f"{SITE}/allele/{curie}"
        ids = _xref_identifiers([curie])
        summary = item.get("description")
        title = _clean(item.get("symbol") or item.get("name"))
    else:
        rtype, url = "other", None
        ids = _xref_identifiers([curie])
        summary = item.get("definition") or item.get("description")
        title = strip_html(item.get("name") or item.get("symbol"))
    attrs = {
        "category": cat.replace("_search_result", ""),
        "species": item.get("species"),
        "full_name": item.get("name") if cat == "gene_search_result" else None,
        "biotype": item.get("soTermName"),
        "synonyms": (item.get("synonyms") or [])[:10] or None,
        "diseases": (item.get("diseases") or [])[:15] or None,
        "genes": ((item.get("genes") or [])[:15] or None) if cat != "gene_search_result" else None,
        "xrefs": xrefs[:20] or None,
    }
    return Record(
        source="alliance",
        id=curie,
        type=rtype,  # type: ignore[arg-type]
        title=title,
        url=url,
        identifiers=ids,
        summary=strip_html(summary),
        attributes={k: v for k, v in attrs.items() if v not in (None, "", [])},
    )


class AllianceSearchParams(SearchParams):
    category: Literal["gene", "disease", "allele", "model"] = Field(
        "gene", description="What to search: gene (default), disease (DOID terms), allele, model (genotypes/strains)"
    )
    species: SPECIES | None = Field(None, description="Restrict to one species (full name)")


class GeneParams(ToolParams):
    gene_id: str = Field(
        description="Alliance gene id: HGNC:8620 (human), MGI:97490 (mouse), RGD:3258 (rat), ZFIN:ZDB-GENE-990415-200, "
        "FB:FBgn0005558 (fly), WB:WBGene00006970 (worm), SGD:S000001595 (yeast), Xenbase:XB-GENE-484088"
    )


class OrthologParams(GeneParams):
    stringency: Literal["stringent", "moderate", "all"] = Field(
        "stringent", description="Ortholog filter: stringent (Alliance best-score), moderate, or all predictions"
    )
    limit: int = Field(25, ge=1, le=100, description="Maximum orthologs to return")
    cursor: str | None = Field(None, description="Page cursor from a previous result's next_cursor")


class GeneDiseaseParams(ToolParams):
    entity_id: str = Field(
        description="A gene id (HGNC:8620, MGI:97490, ...) to list its disease annotations, OR a Disease Ontology id "
        "(DOID:12271) to list genes annotated to that disease across species"
    )
    limit: int = Field(25, ge=1, le=100, description="Maximum annotations to return")
    cursor: str | None = Field(None, description="Page cursor from a previous result's next_cursor")


class GeneTableParams(GeneParams):
    limit: int = Field(25, ge=1, le=100, description="Maximum rows to return")
    cursor: str | None = Field(None, description="Page cursor from a previous result's next_cursor")


class Alliance(Connector):
    name = "alliance"
    title = "Alliance of Genome Resources"
    category = "biology"
    description = (
        "Unified gene, disease, ortholog, allele, model and phenotype data for human plus seven model organisms "
        "(MGI mouse, RGD rat, ZFIN zebrafish, FlyBase, WormBase, SGD yeast, Xenbase frog). Best for cross-species "
        "orthology, model-organism disease models and phenotypes of a gene."
    )
    homepage = "https://www.alliancegenome.org/"
    docs_url = "https://www.alliancegenome.org/swagger-ui/"
    auth = "none"
    rate_limit = 3.0
    health_query = "PAX6"
    nih_repositories = (
        "Alliance of Genome Resources (AGR)",
        "Rat Genome Database (RGD)",
        "Zebrafish Information Network (ZFIN)",
        "WormBase",
        "FlyBase: A Drosophila Genomic and Genetic Database",
        "Saccharomyces Genome Database (SGD)",
        "Xenbase",
    )

    @tool(
        description="""
        Search the Alliance across human and model organisms. category='gene' (default) returns genes with
        Alliance ids (HGNC:, MGI:, RGD:, ZFIN:, FB:, WB:, SGD:, Xenbase:), species, automated description,
        associated diseases and cross-references (UniProt, Ensembl, NCBI Gene); category='disease' returns
        Disease Ontology terms (DOID) with xrefs (MeSH, OMIM, UMLS, Orphanet); 'allele' and 'model' search
        alleles and genotypes/strains. Narrow genes with species='Mus musculus' etc. Example: query='pax6'.
        """,
        federated=True,
    )
    async def search(self, params: AllianceSearchParams) -> ToolResult:
        offset = parse_offset(params.cursor)
        response = await self.request(
            "GET",
            f"{API}/search",
            params={
                "q": params.query,
                "category": _SEARCH_CATEGORIES[params.category],
                "species": params.species,
                "limit": params.limit,
                "offset": offset,
            },
        )
        payload = response.json() or {}
        items = [i for i in payload.get("results") or [] if isinstance(i, dict)]
        total = payload.get("total")
        nxt = offset + len(items)
        return ToolResult(
            source=self.name,
            tool="search",
            records=[_search_record(i) for i in items],
            total=total,
            next_cursor=str(nxt) if items and total and nxt < total else None,
            request_url=response.url,
        )

    @tool(
        description="""
        Get one gene by Alliance id (HGNC:8620, MGI:97490, RGD:3258, ZFIN:ZDB-GENE-..., FB:FBgn..., WB:WBGene...,
        SGD:S..., Xenbase:XB-GENE-...): symbol, full name, species, biotype, genomic location (assembly,
        chromosome, start/end/strand), synonyms, curated summary and cross-references (UniProt canonical
        protein, Ensembl, NCBI Gene, OMIM, PANTHER, MOD ids). Use search to find ids from symbols.
        """
    )
    async def get_gene(self, params: GeneParams) -> ToolResult:
        gid = params.gene_id.strip()
        response = await self.request("GET", f"{API}/gene/{quote(gid, safe=':')}", allow_status=(400, 404))
        if response.status in (400, 404):
            return ToolResult(
                source=self.name, tool="get_gene", request_url=response.url,
                notes=[f"gene {gid!r} not found in the Alliance (use ids like HGNC:8620, MGI:97490)"],
            )
        gene = (response.json() or {}).get("gene") or {}
        curie = gene.get("primaryExternalId") or gid
        xrefs = [
            x.get("referencedCurie")
            for x in gene.get("crossReferences") or []
            if isinstance(x, dict) and x.get("referencedCurie")
        ]
        uniprot = (gene.get("gcrpCrossReference") or {}).get("referencedCurie")
        symbol = _text(gene.get("geneSymbol"))
        taxon = gene.get("taxon") or {}
        loc = first(gene.get("geneGenomicLocationAssociations")) or {}
        loc_obj = loc.get("geneGenomicLocationAssociationObject") or {}
        assembly = (((loc_obj.get("taxon") or {}).get("species") or {}).get("genomeAssembly") or {}).get("primaryExternalId")
        notes_text = " ".join(
            n.get("freeText") for n in gene.get("relatedNotes") or [] if isinstance(n, dict) and n.get("freeText")
        )
        other_uniprot = sorted({x.split(":", 1)[1] for x in xrefs if x.startswith("UniProtKB:")} - {(uniprot or ":").split(":", 1)[1]})
        attrs = {
            "symbol": symbol,
            "full_name": _text(gene.get("geneFullName")),
            "species": taxon.get("name"),
            "taxon": taxon.get("curie"),
            "biotype": (gene.get("geneType") or {}).get("name"),
            "synonyms": [_text(s) for s in gene.get("geneSynonyms") or []][:25] or None,
            "secondary_ids": [s.get("secondaryId") for s in gene.get("geneSecondaryIds") or [] if isinstance(s, dict)] or None,
            "location": {
                "assembly": assembly,
                "chromosome": loc_obj.get("name"),
                "start": loc.get("start"),
                "end": loc.get("end"),
                "strand": loc.get("strand"),
            }
            if loc
            else None,
            "canonical_protein": uniprot,
            "other_uniprot": other_uniprot[:20] or None,
            "xrefs": [x for x in dict.fromkeys(xrefs) if not x.startswith("UniProtKB:")][:30] or None,
            "data_provider": (gene.get("dataProviderCrossReference") or {}).get("referencedCurie"),
        }
        record = Record(
            source=self.name,
            id=curie,
            type="gene",
            title=symbol,
            url=f"{SITE}/gene/{curie}",
            identifiers=gene_identifiers(curie, xrefs, symbol=symbol, uniprot=uniprot),
            summary=strip_html(notes_text) or _text(gene.get("geneFullName")),
            attributes={k: v for k, v in attrs.items() if v not in (None, "", [], {})},
        )
        return ToolResult(source=self.name, tool="get_gene", records=[record], total=1, request_url=response.url)

    @tool(
        description="""
        Orthologs of a gene across human and the Alliance model organisms (mouse, rat, zebrafish, fly, worm,
        yeast, frog), with the prediction methods that support each call (Ensembl Compara, PANTHER, OMA,
        InParanoid, ZFIN, HGNC, ...), best-score / best-reverse-score flags and confidence. Default
        stringency='stringent' (Alliance best-score filter). Example: gene_id='HGNC:8620' (PAX6) ->
        Pax6 (MGI), pax6a/pax6b (ZFIN), ey/toy (FB), vab-3 (WB).
        """
    )
    async def orthologs(self, params: OrthologParams) -> ToolResult:
        gid = params.gene_id.strip()
        page = max(1, parse_offset(params.cursor) or 1)
        response = await self.request(
            "GET",
            f"{API}/gene/{quote(gid, safe=':')}/orthologs",
            params={"filter.stringency": params.stringency, "limit": params.limit, "page": page},
            allow_status=(400, 404),
        )
        if response.status in (400, 404):
            return ToolResult(source=self.name, tool="orthologs", request_url=response.url,
                              notes=[f"gene {gid!r} not found in the Alliance"])
        payload = response.json() or {}
        records = []
        for row in payload.get("results") or []:
            orth = (row or {}).get("geneToGeneOrthologyGenerated") or {}
            subj = orth.get("subjectGene") or {}
            obj = orth.get("objectGene") or {}
            if (obj.get("primaryExternalId") or "") == gid and subj.get("primaryExternalId") != gid:
                subj, obj = obj, subj
            o = _gene_brief(obj)
            if not o["id"]:
                continue
            methods = [m.get("name") for m in orth.get("predictionMethodsMatched") or [] if isinstance(m, dict)]
            not_matched = [m.get("name") for m in orth.get("predictionMethodsNotMatched") or [] if isinstance(m, dict)]
            records.append(
                Record(
                    source=self.name,
                    id=o["id"],
                    type="gene",
                    title=o["symbol"],
                    url=f"{SITE}/gene/{o['id']}",
                    identifiers=gene_identifiers(o["id"], symbol=o["symbol"]),
                    attributes={
                        k: v
                        for k, v in {
                            "species": o["species"],
                            "taxon": o["taxon"],
                            "ortholog_of": subj.get("primaryExternalId") or gid,
                            "ortholog_of_symbol": _text(subj.get("geneSymbol")),
                            "best_score": (orth.get("isBestScore") or {}).get("name"),
                            "best_reverse_score": (orth.get("isBestScoreReverse") or {}).get("name"),
                            "confidence": (orth.get("confidence") or {}).get("name"),
                            "strict_filter": orth.get("strictFilter"),
                            "methods_matched": methods or None,
                            "methods_matched_count": len(methods) or None,
                            "methods_not_matched": not_matched or None,
                        }.items()
                        if v not in (None, "", [])
                    },
                )
            )
        total = payload.get("total")
        more = total is not None and page * params.limit < total and bool(records)
        return ToolResult(
            source=self.name,
            tool="orthologs",
            records=records,
            total=total,
            next_cursor=str(page + 1) if more else None,
            request_url=response.url,
            notes=[] if records else [f"no {params.stringency} orthologs found for {gid}"],
        )

    @tool(
        description="""
        Gene-disease associations from the Alliance (curated by RGD/HGNC for human, MGI, ZFIN, FlyBase,
        WormBase, SGD; DO terms). Pass a gene id (HGNC:8620) to list its diseases with relation
        (is_implicated_in, is_marker_for, is_model_of, biomarker...), evidence codes and PMIDs; or a
        Disease Ontology id (DOID:12271) to list the genes annotated to that disease across species.
        Use gene_models for experimental model genotypes/strains.
        """
    )
    async def gene_diseases(self, params: GeneDiseaseParams) -> ToolResult:
        eid = params.entity_id.strip()
        page = max(1, parse_offset(params.cursor) or 1)
        query = {"limit": params.limit, "page": page}
        by_disease = eid.upper().startswith("DOID:")
        if by_disease:
            response = await self.request(
                "GET", f"{API}/disease/{quote(eid.upper(), safe=':')}/genes", params=query, allow_status=(400, 404)
            )
        else:
            response = await self.request(
                "POST", f"{API}/disease", params=query, json_body=[eid], allow_status=(400, 404)
            )
        if response.status in (400, 404):
            return ToolResult(source=self.name, tool="gene_diseases", request_url=response.url,
                              notes=[f"{eid!r} not found in the Alliance"])
        payload = response.json() or {}
        records = []
        for row in payload.get("results") or []:
            if not isinstance(row, dict):
                continue
            gene = _gene_brief(row.get("subject") or {})
            disease = row.get("object") or {}
            doid = disease.get("curie")
            relation = (row.get("relation") or {}).get("name") or row.get("generatedRelationString")
            pmids = _pmids(row.get("pubmedPublications"))
            evidence = [e.get("abbreviation") or e.get("curie") for e in row.get("evidenceCodes") or [] if isinstance(e, dict)]
            if by_disease:
                rid, title, url = gene["id"], gene["symbol"], f"{SITE}/gene/{gene['id']}"
                ids = gene_identifiers(gene["id"], symbol=gene["symbol"])
            else:
                rid, title, url = doid, disease.get("name"), f"{SITE}/disease/{doid}"
                ids = {"doid": doid}
            records.append(
                Record(
                    source=self.name,
                    id=row.get("uniqueId") or f"{gene['id']}|{relation}|{doid}",
                    type="association",
                    title=f"{gene['symbol']} {(relation or '').replace('_', ' ')} {disease.get('name')}",
                    url=url,
                    identifiers=ids,
                    summary=disease.get("definition") if not by_disease else None,
                    attributes={
                        k: v
                        for k, v in {
                            "gene": gene["id"],
                            "gene_symbol": gene["symbol"],
                            "species": gene["species"],
                            "disease": doid,
                            "disease_name": disease.get("name"),
                            "relation": relation,
                            "negated": row.get("negated") or None,
                            "evidence_codes": evidence or None,
                            "pmids": pmids[:15] or None,
                            "via_orthology": bool(row.get("viaOrthologyOrder")) or None,
                            "providers": [
                                (p.get("dataProvider") or {}).get("abbreviation")
                                for p in row.get("providers") or []
                                if isinstance(p, dict) and (p.get("dataProvider") or {}).get("abbreviation")
                            ]
                            or None,
                            "entity": rid,
                            "entity_label": title,
                        }.items()
                        if v not in (None, "", [])
                    },
                )
            )
        total = payload.get("total")
        more = total is not None and page * params.limit < total and bool(records)
        return ToolResult(
            source=self.name,
            tool="gene_diseases",
            records=records,
            total=total,
            next_cursor=str(page + 1) if more else None,
            request_url=response.url,
            notes=[] if records else [f"no disease annotations for {eid}"],
        )

    @tool(
        description="""
        Experimental models (genotypes/strains/fish lines, mostly MGI, ZFIN, RGD, WormBase, FlyBase) involving a
        model-organism gene, with the diseases they model (is_model_of), associated phenotypes and data
        provider. Use the model-organism gene id (MGI:97490 for mouse Pax6); human HGNC ids usually have none,
        so use orthologs first. Example: gene_id='MGI:97490'.
        """
    )
    async def gene_models(self, params: GeneTableParams) -> ToolResult:
        gid = params.gene_id.strip()
        page = max(1, parse_offset(params.cursor) or 1)
        response = await self.request(
            "GET", f"{API}/gene/{quote(gid, safe=':')}/models", params={"limit": params.limit, "page": page},
            allow_status=(400, 404),
        )
        if response.status in (400, 404):
            return ToolResult(source=self.name, tool="gene_models", request_url=response.url,
                              notes=[f"gene {gid!r} not found in the Alliance"])
        payload = response.json() or {}
        records = []
        for row in payload.get("results") or []:
            model = (row or {}).get("model") or {}
            mid = model.get("primaryExternalId")
            if not mid:
                continue
            diseases = [
                {"disease": d.get("diseaseModel") or (d.get("disease") or {}).get("name"), "relation": d.get("associationType"),
                 "doid": (d.get("disease") or {}).get("curie")}
                for d in row.get("diseaseModels") or []
                if isinstance(d, dict)
            ]
            gene = row.get("gene") or {}
            records.append(
                Record(
                    source=self.name,
                    id=mid,
                    type="other",
                    title=_text(model.get("agmFullName")) or model.get("name") or mid,
                    url=f"{SITE}/model/{mid}",
                    identifiers=_xref_identifiers([mid]),
                    attributes={
                        k: v
                        for k, v in {
                            "model_type": (model.get("subtype") or {}).get("name"),
                            "gene": gene.get("primaryExternalId") or gid,
                            "gene_symbol": _text(gene.get("geneSymbol")),
                            "disease_models": [{k2: v2 for k2, v2 in d.items() if v2} for d in diseases] or None,
                            "phenotypes": (row.get("associatedPhenotype") or [])[:25] or None,
                            "data_provider": row.get("dataProvider"),
                            "has_disease_annotations": row.get("hasDiseaseAnnotations"),
                        }.items()
                        if v not in (None, "", [])
                    },
                )
            )
        total = payload.get("total")
        more = total is not None and page * params.limit < total and bool(records)
        return ToolResult(
            source=self.name,
            tool="gene_models",
            records=records,
            total=total,
            next_cursor=str(page + 1) if more else None,
            request_url=response.url,
            notes=[] if records else [f"no experimental models recorded for {gid} (try the mouse/zebrafish ortholog)"],
        )

    @tool(
        description="""
        Phenotype annotations of a gene from its model-organism database (mouse MP terms via MGI, zebrafish
        ZFIN, rat RGD, fly, worm, yeast; HPO-style statements for human), with supporting PMIDs. Example:
        gene_id='MGI:97490'. For human disease phenotypes prefer the Monarch connector.
        """
    )
    async def gene_phenotypes(self, params: GeneTableParams) -> ToolResult:
        gid = params.gene_id.strip()
        page = max(1, parse_offset(params.cursor) or 1)
        response = await self.request(
            "GET", f"{API}/gene/{quote(gid, safe=':')}/phenotypes", params={"limit": params.limit, "page": page},
            allow_status=(400, 404),
        )
        if response.status in (400, 404):
            return ToolResult(source=self.name, tool="gene_phenotypes", request_url=response.url,
                              notes=[f"gene {gid!r} not found in the Alliance"])
        payload = response.json() or {}
        records = []
        for index, row in enumerate(payload.get("results") or []):
            if not isinstance(row, dict):
                continue
            gene = _gene_brief(row.get("subject") or {})
            statement = strip_html(row.get("phenotypeStatement"))
            pmids = _pmids(row.get("pubmedPublications"))
            terms = [
                t.get("curie") for t in row.get("phenotypeTerms") or [] if isinstance(t, dict) and t.get("curie")
            ]
            ids: dict[str, Any] = {}
            for t in terms:
                mapped = map_curie(t)
                if mapped:
                    ids.setdefault(mapped[0], mapped[1])
            records.append(
                Record(
                    source=self.name,
                    id=row.get("uniqueId") or f"{gene['id']}|{statement}|{index}",
                    type="phenotype",
                    title=statement,
                    url=f"{SITE}/gene/{gene['id'] or gid}#phenotypes",
                    identifiers=ids,
                    attributes={
                        k: v
                        for k, v in {
                            "gene": gene["id"] or gid,
                            "gene_symbol": gene["symbol"],
                            "species": gene["species"],
                            "phenotype_terms": terms or None,
                            "pmids": pmids[:15] or None,
                        }.items()
                        if v not in (None, "", [])
                    },
                )
            )
        total = payload.get("total")
        more = total is not None and page * params.limit < total and bool(records)
        return ToolResult(
            source=self.name,
            tool="gene_phenotypes",
            records=records,
            total=total,
            next_cursor=str(page + 1) if more else None,
            request_url=response.url,
            notes=[] if records else [f"no phenotype annotations for {gid}"],
        )
