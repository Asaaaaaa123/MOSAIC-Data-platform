"""Body maps kept in memory for the web page's figures, rendered off the event loop."""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict

from ..visuals.figures import FORMATS, load_engine
from . import figure as body_figure
from .models import BodyMap

MAX_MAPS = 24


class BodyMaps:
    def __init__(self, limit: int = MAX_MAPS) -> None:
        self._items: OrderedDict[str, BodyMap] = OrderedDict()
        self._rendered: dict[tuple[str, str, str], bytes] = {}
        self._limit = limit
        self._lock = threading.Lock()

    def add(self, bm: BodyMap) -> None:
        with self._lock:
            self._items[bm.map_id] = bm
            while len(self._items) > self._limit:
                old, _ = self._items.popitem(last=False)
                for key in [k for k in self._rendered if k[0] == old]:
                    del self._rendered[key]

    def get(self, map_id: str) -> BodyMap | None:
        with self._lock:
            return self._items.get(map_id)

    async def figure(self, configured_path: str | None, bm: BodyMap, name: str, fmt: str) -> bytes:
        if fmt not in FORMATS:
            raise ValueError(fmt)
        key = (bm.map_id, name, fmt)
        if key not in self._rendered:
            engine, error = load_engine(configured_path)
            if engine is None:
                raise RuntimeError(error or "Figure Studio is not available")
            self._rendered[key] = await asyncio.to_thread(body_figure.render, engine, bm, name, fmt)
        return self._rendered[key]


async def render_package_figures(configured_path: str | None, bm: BodyMap) -> tuple[dict[str, bytes], list[str]]:
    engine, error = load_engine(configured_path)
    if engine is None:
        return {}, [f"figures were not drawn: {error}"]
    return await asyncio.to_thread(body_figure.render_all, engine, bm)
