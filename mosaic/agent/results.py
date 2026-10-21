"""Session-scoped result handles (``r1``, ``r2`` ...).

Every connector or meta tool result Claude receives is stored in full under a
handle. Claude sees a trimmed rendering (``ToolResult.to_llm``) plus the handle,
and passes handles to ``extract_structured`` / ``save_table`` so extraction runs
over complete records rather than the trimmed text.

Handles are cached in memory for the life of the process and written through to
the :class:`~mosaic.agent.sessions.SessionStore`, so a session resumed after a
restart can still extract from or export its earlier results.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Literal

from pydantic import BaseModel

from ..models import Record
from .sessions import SessionStore

ResultKind = Literal["records", "table", "data"]

_MEMORY_SLOTS = 512  # results kept in memory per process (all sessions)


class StoredResult(BaseModel):
    result_id: str
    kind: ResultKind  # records: has "records"; table: has "columns" + "rows"; data: anything else
    tool: str
    source: str | None = None
    created_at: float
    payload: dict[str, Any]

    def records(self) -> list[Record]:
        return [Record.model_validate(r) for r in self.payload.get("records") or []]


class ResultHandles:
    """Allocates, caches and persists result handles for one session store."""

    def __init__(self, store: SessionStore):
        self.store = store
        self._memory: OrderedDict[tuple[str, str], StoredResult] = OrderedDict()

    def add(
        self, session_id: str, *, kind: ResultKind, tool: str, payload: dict[str, Any], source: str | None = None
    ) -> StoredResult:
        result = StoredResult(
            result_id=self.store.allocate_result_id(session_id),
            kind=kind,
            tool=tool,
            source=source,
            created_at=time.time(),
            payload=payload,
        )
        self.store.save_result(session_id, result.model_dump(mode="json"))
        self._remember(session_id, result)
        return result

    def get(self, session_id: str, result_id: str) -> StoredResult | None:
        key = (session_id, result_id.strip())
        cached = self._memory.get(key)
        if cached is not None:
            self._memory.move_to_end(key)
            return cached
        row = self.store.load_result(session_id, key[1])
        if row is None:
            return None
        result = StoredResult.model_validate(row)
        self._remember(session_id, result)
        return result

    def known(self, session_id: str) -> list[str]:
        return self.store.result_ids(session_id)

    def forget_session(self, session_id: str) -> None:
        for key in [k for k in self._memory if k[0] == session_id]:
            del self._memory[key]

    def _remember(self, session_id: str, result: StoredResult) -> None:
        self._memory[(session_id, result.result_id)] = result
        while len(self._memory) > _MEMORY_SLOTS:
            self._memory.popitem(last=False)
