"""Search the NIH repository catalog.

The catalog (``nih_repositories.json``) lists every repository on NIH's
"Accessing Scientific Data" page: 223 NIH-supported domain-specific repositories
and the 9 generalist repositories. Entries served by a live Mosaic connector are
annotated with that connector's name, so Claude knows whether it can query the
repository directly or should point the user to its access page.
"""

from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

CATALOG_PATH = Path(__file__).with_name("nih_repositories.json")
_TOKEN = re.compile(r"[a-z0-9]+")


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Any]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _tokens(text: str | None) -> list[str]:
    return _TOKEN.findall((text or "").lower())


class RepositoryCatalog:
    def __init__(self, connector_map: dict[str, list[str]] | None = None):
        """``connector_map``: repository name -> connector names that serve it."""
        data = load_catalog()
        self.meta = data["meta"]
        self.repositories: list[dict[str, Any]] = []
        connector_map = connector_map or {}
        for repo in data["repositories"]:
            entry = dict(repo)
            entry["connectors"] = sorted(connector_map.get(repo["name"], []))
            self.repositories.append(entry)
        self._docs = [self._doc_tokens(r) for r in self.repositories]
        df: dict[str, int] = {}
        for doc in self._docs:
            for tok in set(doc):
                df[tok] = df.get(tok, 0) + 1
        n = len(self._docs)
        self._idf = {tok: math.log(1 + (n - c + 0.5) / (c + 0.5)) for tok, c in df.items()}
        self._avg_len = sum(len(d) for d in self._docs) / max(n, 1)

    @staticmethod
    def _doc_tokens(repo: dict[str, Any]) -> list[str]:
        # Name and subject weigh more than the free-text description.
        return (
            _tokens(repo["name"]) * 3
            + _tokens(repo.get("subject_area")) * 2
            + _tokens(" ".join(repo.get("institutes") or []))
            + _tokens(repo.get("model_system"))
            + _tokens(repo.get("description"))
        )

    @classmethod
    def from_connectors(cls, connectors: Iterable[Any]) -> "RepositoryCatalog":
        mapping: dict[str, list[str]] = {}
        for connector in connectors:
            for repo_name in getattr(connector, "nih_repositories", ()) or ():
                mapping.setdefault(repo_name, []).append(connector.name)
        return cls(mapping)

    def search(
        self,
        query: str | None = None,
        *,
        kind: str | None = None,
        access_type: str | None = None,
        institute: str | None = None,
        has_connector: bool | None = None,
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        terms = _tokens(query)
        scored: list[tuple[float, int]] = []
        for idx, (repo, doc) in enumerate(zip(self.repositories, self._docs)):
            if kind and repo["kind"] != kind:
                continue
            if access_type and access_type.lower() not in (repo.get("access_type") or "").lower():
                continue
            if institute and institute.upper() not in [i.upper() for i in repo.get("institutes") or []]:
                continue
            if has_connector is not None and bool(repo["connectors"]) != has_connector:
                continue
            score = self._bm25(terms, doc) if terms else 0.0
            if terms and score <= 0:
                continue
            if repo["connectors"]:
                score *= 1.15  # prefer repositories Mosaic can query live
            scored.append((score, idx))
        scored.sort(key=lambda s: (-s[0], self.repositories[s[1]]["name"]))
        return [self.repositories[i] for _, i in scored[:limit]]

    def _bm25(self, terms: list[str], doc: list[str], k1: float = 1.4, b: float = 0.75) -> float:
        if not doc:
            return 0.0
        score = 0.0
        length = len(doc)
        for term in terms:
            tf = doc.count(term)
            if not tf:
                continue
            idf = self._idf.get(term, 0.0)
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / self._avg_len))
        return score

    def get(self, name: str) -> dict[str, Any] | None:
        lowered = name.strip().lower()
        for repo in self.repositories:
            if repo["name"].lower() == lowered:
                return repo
        for repo in self.repositories:
            if lowered in repo["name"].lower():
                return repo
        return None

    def stats(self) -> dict[str, Any]:
        live = [r for r in self.repositories if r["connectors"]]
        return {
            **self.meta["counts"],
            "total": len(self.repositories),
            "with_live_connector": len(live),
        }
