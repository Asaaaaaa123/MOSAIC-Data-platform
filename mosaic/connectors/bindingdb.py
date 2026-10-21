"""BindingDB connector (measured protein-ligand binding affinities).

Docs: https://www.bindingdb.org/rwd/bind/BindingDBRESTfulAPI.jsp
Endpoints (JSON with ``response=application/json``; XML otherwise):
- ``https://bindingdb.org/rest/getLigandsByUniprots?uniprot=P00533,P04626&cutoff=100`` (cutoff in nM)
- ``https://bindingdb.org/rest/getLigandsByPDBs?pdb=1M17&cutoff=100&identity=92`` (identity in %)
- ``https://bindingdb.org/rest/getTargetByCompound?smiles=...&cutoff=0.85`` (Tanimoto similarity)

Auth: none. Rate limit: none published, but the service is slow and fragile (multi-MB responses,
5-15 s latency), so we run at 1 req/s with a 120 s timeout and rely on the response cache for paging.

Quirks:
- No free-text search exists, so this connector has no federated tool.
- Responses carry no totals or paging; every call returns the full set for the cutoff, which the
  connector aggregates and pages locally (``next_cursor`` is an offset into the aggregated list).
- Rows are individual measurements (Ki / Kd / IC50 / EC50 in nM, possibly with ``>``/``<``). They are
  grouped into one record per ligand (or per target for ``targets_for_compound``) with the best
  (lowest) affinity, measurement count and source publications.
- The ``query`` field is the target *name*; the per-row UniProt accession is not returned, so the
  queried accessions are echoed in ``attributes.query_uniprot``.
- Unknown PDB IDs make the server fail with HTTP 500 (bad SQL); unknown UniProt accessions return an
  empty list. Both become empty results with a note.
- Ligand names are not returned, only BindingDB monomer IDs and SMILES.

Identifier policy: ligand records identify the compound (``bindingdb`` monomer ID, ``smiles``); the
measured target and the source articles (PMID / DOI) are kept in ``attributes`` so a binding record
never bridges a compound, a protein and a paper during cross-database clustering.
"""

from __future__ import annotations

import re
import time
from typing import Any, Literal

from pydantic import Field, field_validator

from ..models import HealthResult, Record, ToolResult
from .base import Connector, ToolParams, parse_offset, tool

API = "https://bindingdb.org/rest"
WEB = "https://www.bindingdb.org/rwd/bind"
TIMEOUT = 120.0

_UNIPROT = re.compile(r"^[OPQ][0-9][A-Z0-9]{3}[0-9]$|^[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2}$")
_PDB = re.compile(r"^[0-9][A-Z0-9]{3}$")
AffinityType = Literal["Ki", "Kd", "IC50", "EC50"]


def _split_ids(value: str | list[str]) -> list[str]:
    items = value if isinstance(value, list) else re.split(r"[,\s;]+", value)
    return [i.strip().upper() for i in items if i and i.strip()]


# ---------------------------------------------------------------- params
class LigandsForTargetParams(ToolParams):
    uniprot: str | list[str] = Field(
        description="UniProt accession(s), e.g. 'P00533' or 'P00533,P04626' (max 10)"
    )
    cutoff_nm: float = Field(100, gt=0, le=1_000_000, description="Affinity cutoff in nM (keep measurements at or below it)")
    affinity_type: AffinityType | None = Field(None, description="Keep only Ki, Kd, IC50 or EC50 measurements")
    limit: int = Field(25, ge=1, le=100, description="Ligands per page (most potent first)")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor of a previous call)")

    @field_validator("uniprot")
    @classmethod
    def _check(cls, value: str | list[str]) -> list[str]:
        ids = _split_ids(value)
        if not ids or len(ids) > 10 or not all(_UNIPROT.match(i) for i in ids):
            raise ValueError("give 1-10 UniProt accessions such as P00533")
        return ids


class LigandsForPdbParams(ToolParams):
    pdb: str | list[str] = Field(description="PDB ID(s), e.g. '1M17' or '1M17,3ANM' (max 10)")
    cutoff_nm: float = Field(100, gt=0, le=1_000_000, description="Affinity cutoff in nM")
    identity: int = Field(92, ge=30, le=100, description="Minimum sequence identity (%) to the PDB chain's protein")
    affinity_type: AffinityType | None = Field(None, description="Keep only Ki, Kd, IC50 or EC50 measurements")
    limit: int = Field(25, ge=1, le=100, description="Ligands per page (most potent first)")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor of a previous call)")

    @field_validator("pdb")
    @classmethod
    def _check(cls, value: str | list[str]) -> list[str]:
        ids = _split_ids(value)
        if not ids or len(ids) > 10 or not all(_PDB.match(i) for i in ids):
            raise ValueError("give 1-10 four-character PDB IDs such as 1M17")
        return ids


class TargetsForCompoundParams(ToolParams):
    smiles: str = Field(description="Query compound as SMILES, e.g. imatinib Cc1ccc(NC(=O)c2ccc(CN3CCN(C)CC3)cc2)cc1Nc1nccc(-c2cccnc2)n1")
    similarity: float = Field(0.85, ge=0.5, le=1.0, description="Tanimoto similarity cutoff for BindingDB compounds (1.0 = same structure)")
    max_affinity_nm: float | None = Field(
        None, gt=0, description="Drop targets whose best measured affinity is weaker than this (nM)"
    )
    limit: int = Field(25, ge=1, le=100, description="Targets per page (most potent first)")
    cursor: str | None = Field(None, description="Pagination cursor (next_cursor of a previous call)")


# ---------------------------------------------------------------- helpers
def parse_affinity(value: Any) -> tuple[str, float | None]:
    """'0.006' -> ('=', 0.006); '>10000' -> ('>', 10000.0)."""
    text = str(value or "").strip()
    relation = "="
    if text[:1] in "<>~":
        relation, text = text[0], text[1:].lstrip("=").strip()
    try:
        return relation, float(text)
    except ValueError:
        return relation, None


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Extract the affinity list from BindingDB's wrapper ({"getLinds...Response": {"affinities": [...]}})."""
    if not isinstance(payload, dict):
        return []
    body = next(iter(payload.values()), {}) if payload else {}
    if not isinstance(body, dict):
        return []
    rows = body.get("affinities", body.get("bdb.affinities")) or []
    if isinstance(rows, dict):
        rows = [rows]
    return [{k.removeprefix("bdb."): v for k, v in r.items()} for r in rows if isinstance(r, dict)]


def _sort_key(group: dict[str, Any]) -> tuple:
    best = group["best"]
    # exact values before censored (>) ones, then potency
    return (best is None, group["best_relation"] == ">", best if best is not None else 0.0)


def _fmt_nm(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:g}"


def group_ligands(rows: list[dict[str, Any]], affinity_type: str | None) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        if affinity_type and (r.get("affinity_type") or "").upper() != affinity_type.upper():
            continue
        mid = str(r.get("monomerid") or "").strip()
        if not mid:
            continue
        target = r.get("query") or r.get("target") or ""
        g = groups.setdefault(
            (mid, target),
            {"monomerid": mid, "smiles": r.get("smile") or r.get("smiles"), "target": target, "best": None,
             "best_relation": "=", "best_type": None, "types": set(), "n": 0, "pmids": [], "dois": []},
        )
        g["n"] += 1
        atype = r.get("affinity_type")
        if atype:
            g["types"].add(atype)
        relation, value = parse_affinity(r.get("affinity"))
        better = value is not None and (
            g["best"] is None or (relation != ">" and (g["best_relation"] == ">" or value < g["best"]))
        )
        if better:
            g["best"], g["best_relation"], g["best_type"] = value, relation, atype
        pmid = str(r.get("pmid") or "").strip()
        doi = str(r.get("doi") or "").strip()
        if pmid.isdigit() and pmid not in g["pmids"]:
            g["pmids"].append(pmid)
        if doi and doi not in g["dois"]:
            g["dois"].append(doi)
    return sorted(groups.values(), key=_sort_key)


def _ligand_record(g: dict[str, Any], query_ids: list[str], query_kind: str) -> Record:
    rel = "" if g["best_relation"] == "=" else g["best_relation"]
    best = f"{g['best_type'] or 'affinity'} {rel}{_fmt_nm(g['best'])} nM"
    return Record(
        source="bindingdb",
        id=g["monomerid"],
        type="compound",
        title=f"BDBM{g['monomerid']} ({best} vs {g['target'] or ', '.join(query_ids)})",
        url=f"{WEB}/chemsearch/marvin/MolStructure.jsp?monomerid={g['monomerid']}",
        identifiers={"bindingdb": g["monomerid"], "smiles": g["smiles"]},
        summary=f"{g['n']} measurement(s) against {g['target'] or 'the query target'}; best {best}",
        attributes={
            k: v
            for k, v in {
                "target_name": g["target"] or None,
                f"query_{query_kind}": query_ids,
                "best_affinity_nM": g["best"],
                "best_affinity_relation": g["best_relation"],
                "best_affinity_type": g["best_type"],
                "affinity_types": sorted(g["types"]),
                "n_measurements": g["n"],
                "pmids": g["pmids"][:10],
                "dois": g["dois"][:10],
            }.items()
            if v not in (None, [], "")
        },
    )


class BindingDB(Connector):
    name = "bindingdb"
    title = "BindingDB"
    category = "chemistry"
    description = (
        "Public database of ~3M measured binding affinities (Ki, Kd, IC50, EC50) for ~1.4M small molecules "
        "against ~11K protein targets, curated from articles, patents and ChEMBL/PubChem. Best for "
        "finding potent ligands of a protein (by UniProt or PDB) and the targets of a chemotype."
    )
    homepage = "https://www.bindingdb.org/"
    docs_url = "https://www.bindingdb.org/rwd/bind/BindingDBRESTfulAPI.jsp"
    auth = "none"
    rate_limit = 1.0
    health_query = "P00533"
    nih_repositories = ("Binding Database (BindingDB)",)

    async def healthcheck(self) -> HealthResult:
        """No federated search here: probe ligands_for_target for EGFR at a tight cutoff."""
        started = time.perf_counter()
        try:
            result = await self.call("ligands_for_target", {"uniprot": self.health_query, "cutoff_nm": 0.1, "limit": 1})
        except Exception as exc:  # report, never raise
            return HealthResult(source=self.name, ok=False, latency_ms=(time.perf_counter() - started) * 1000, detail=str(exc)[:300])
        return HealthResult(
            source=self.name,
            ok=True,
            latency_ms=(time.perf_counter() - started) * 1000,
            detail=f"ligands_for_target: {len(result.records)} record(s), total={result.total}",
        )

    async def _fetch(self, endpoint: str, params: dict[str, Any], allow: tuple[int, ...] = ()) -> tuple[list[dict[str, Any]], str, int]:
        response = await self.request(
            "GET",
            f"{API}/{endpoint}",
            params={**params, "response": "application/json"},
            timeout=TIMEOUT,
            retries=1 if allow else 2,
            allow_status=allow,
        )
        if response.status != 200 or not response.content.strip():
            return [], response.url, response.status
        try:
            payload = response.json()
        except ValueError:
            return [], response.url, response.status
        return _rows(payload), response.url, response.status

    def _page(self, tool_name: str, groups: list[dict[str, Any]], query_ids: list[str], kind: str,
              cursor: str | None, limit: int, url: str, n_rows: int) -> ToolResult:
        offset = parse_offset(cursor)
        page = groups[offset: offset + limit]
        nxt = offset + len(page)
        notes = []
        if not groups:
            notes.append(f"no BindingDB measurements for {', '.join(query_ids)} at this cutoff")
        return ToolResult(
            source=self.name,
            tool=tool_name,
            records=[_ligand_record(g, query_ids, kind) for g in page],
            total=len(groups),
            next_cursor=str(nxt) if nxt < len(groups) else None,
            data={"measurements": n_rows, "ligands": len(groups)} if groups else None,
            notes=notes,
            request_url=url,
        )

    @tool(
        description="""
        Ligands with measured binding affinity for protein target(s) given as UniProt accessions
        (e.g. 'P00533' EGFR), at or below cutoff_nm (default 100 nM). One record per ligand (BindingDB
        monomer ID + SMILES) with best affinity (Ki/Kd/IC50/EC50 in nM), number of measurements and source
        PMIDs/DOIs, most potent first. Optional affinity_type filter. Large targets return thousands of
        ligands: keep cutoff_nm low (1-100) and page with cursor. ChEMBL get_activities is the curated
        alternative with assay context.
        """
    )
    async def ligands_for_target(self, params: LigandsForTargetParams) -> ToolResult:
        ids: list[str] = params.uniprot  # type: ignore[assignment]  (validator returns a list)
        rows, url, _ = await self._fetch("getLigandsByUniprots", {"uniprot": ",".join(ids), "cutoff": f"{params.cutoff_nm:g}"})
        groups = group_ligands(rows, params.affinity_type)
        result = self._page("ligands_for_target", groups, ids, "uniprot", params.cursor, params.limit, url, len(rows))
        if len(ids) > 1:
            result.notes.append("BindingDB reports the target by name only; with several accessions see target_name")
        return result

    @tool(
        description="""
        Ligands with measured affinity for proteins similar to the protein in a PDB structure (e.g.
        '1M17' EGFR kinase domain), using a sequence-identity threshold (default 92%) and an affinity
        cutoff in nM. Same record shape as ligands_for_target (one record per ligand, best affinity
        first). Use when you start from a crystal structure rather than a UniProt accession.
        """
    )
    async def ligands_for_pdb(self, params: LigandsForPdbParams) -> ToolResult:
        ids: list[str] = params.pdb  # type: ignore[assignment]
        rows, url, status = await self._fetch(
            "getLigandsByPDBs",
            {"pdb": ",".join(ids), "cutoff": f"{params.cutoff_nm:g}", "identity": params.identity},
            allow=(500,),
        )
        groups = group_ligands(rows, params.affinity_type)
        result = self._page("ligands_for_pdb", groups, ids, "pdb", params.cursor, params.limit, url, len(rows))
        if status == 500:
            result.notes = [f"BindingDB failed (HTTP 500) for PDB {', '.join(ids)}; it does so for IDs it does not know"]
        return result

    @tool(
        description="""
        Protein targets measured for compounds structurally similar to a query SMILES (Tanimoto >=
        similarity, default 0.85; use 1.0 for the exact compound). One record per target name + species
        with best affinity (nM), affinity types, number of measurements and the similar BindingDB
        compounds measured. Use for target fishing / polypharmacology of a chemotype. BindingDB returns
        target names only (no UniProt accessions).
        """
    )
    async def targets_for_compound(self, params: TargetsForCompoundParams) -> ToolResult:
        rows, url, _ = await self._fetch(
            "getTargetByCompound", {"smiles": params.smiles.strip(), "cutoff": f"{params.similarity:g}"}
        )
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for r in rows:
            target = (r.get("target") or "").strip()
            if not target:
                continue
            species = (r.get("species") or "").strip()
            g = groups.setdefault(
                (target, species),
                {"target": target, "species": species, "best": None, "best_relation": "=", "best_type": None,
                 "types": set(), "n": 0, "monomers": {}},
            )
            g["n"] += 1
            atype = r.get("affinity_type")
            if atype:
                g["types"].add(atype)
            relation, value = parse_affinity(r.get("affinity"))
            mid = str(r.get("monomerid") or "")
            if mid:
                g["monomers"].setdefault(mid, r.get("smiles"))
            better = value is not None and (
                g["best"] is None or (relation != ">" and (g["best_relation"] == ">" or value < g["best"]))
            )
            if better:
                g["best"], g["best_relation"], g["best_type"] = value, relation, atype
                g["best_monomer"] = mid
        ranked = sorted(groups.values(), key=_sort_key)
        if params.max_affinity_nm is not None:
            ranked = [g for g in ranked if g["best"] is not None and g["best_relation"] != ">" and g["best"] <= params.max_affinity_nm]
        offset = parse_offset(params.cursor)
        page = ranked[offset: offset + params.limit]
        records = []
        for g in page:
            rel = "" if g["best_relation"] == "=" else g["best_relation"]
            best = f"{g['best_type'] or 'affinity'} {rel}{_fmt_nm(g['best'])} nM"
            records.append(
                Record(
                    source=self.name,
                    id=f"{g['target']}|{g['species']}",
                    type="target",
                    title=f"{g['target']} ({g['species'] or 'species n/a'}): {best}",
                    url=f"{WEB}/chemsearch/marvin/MolStructure.jsp?monomerid={g.get('best_monomer')}" if g.get("best_monomer") else None,
                    summary=f"{g['n']} measurement(s) for {len(g['monomers'])} BindingDB compound(s) similar to the query",
                    attributes={
                        k: v
                        for k, v in {
                            "target_name": g["target"],
                            "species": g["species"] or None,
                            "best_affinity_nM": g["best"],
                            "best_affinity_relation": g["best_relation"],
                            "best_affinity_type": g["best_type"],
                            "affinity_types": sorted(g["types"]),
                            "n_measurements": g["n"],
                            "best_compound_bindingdb": g.get("best_monomer"),
                            "compounds_bindingdb": list(g["monomers"])[:10],
                        }.items()
                        if v not in (None, [], "")
                    },
                )
            )
        nxt = offset + len(page)
        return ToolResult(
            source=self.name,
            tool="targets_for_compound",
            records=records,
            total=len(ranked),
            next_cursor=str(nxt) if nxt < len(ranked) else None,
            data={"measurements": len(rows), "similar_compounds": len({str(r.get('monomerid')) for r in rows})} if rows else None,
            notes=[] if ranked else ["no BindingDB compounds at this similarity (try a lower similarity, e.g. 0.7)"],
            request_url=url,
        )
