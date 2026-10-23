"""Single Figure Studio illustrations as SVG, for the interactive body map in the web page.

The page builds the body from one picture per organ (and per body system), so every organ can be
hovered, clicked, recoloured and zoomed on its own while keeping Figure Studio's detailed drawing.
Each picture is drawn at its real size on a body ``BODY_MM`` tall, centred in a box padded by
``PAD`` x its height on every side, so the page can place it from the organ layout alone.
"""

from __future__ import annotations

import hashlib
import io
import threading
from pathlib import Path

from ..visuals.figures import FigureEngine
from .anatomy import ORGANS

BODY_MM = 180.0
PAD = 0.2
TONES = {"primary": "vermilion", "safety": "gold", "investigational": "purple", "expression": "sky"}
TONE_NAMES = tuple(TONES) + ("none", "natural")
ART_NAMES = frozenset({"human_body"} | {o.glyph for o in ORGANS})

_cache: dict[tuple[str, str, str], bytes] = {}
_lock = threading.Lock()


def art_version(engine: FigureEngine) -> str:
    """Changes when Figure Studio's illustrations change (browser cache key)."""
    path = Path(getattr(engine.glyphs, "__file__", "") or "")
    try:
        stamp = f"{path.stat().st_mtime_ns}-{path.stat().st_size}"
    except OSError:
        stamp = "0"
    return hashlib.sha1(f"{stamp}-{BODY_MM}-{PAD}".encode()).hexdigest()[:12]


def glyph_height_mm(name: str) -> float:
    if name == "human_body":
        return BODY_MM
    organ = next((o for o in ORGANS if o.glyph == name), None)
    return (organ.size if organ else 0.1) * BODY_MM / 2


def render_art(engine: FigureEngine, name: str, tone: str) -> bytes:
    """SVG of one illustration in a tone: an evidence colour, 'none' (grey context) or 'natural'."""
    if name not in ART_NAMES:
        raise KeyError(name)
    if tone not in TONE_NAMES:
        raise ValueError(tone)
    glyphs = engine.glyphs
    if glyphs is None or name not in glyphs.GLYPHS:
        raise KeyError(name)
    key = (art_version(engine), name, tone)
    with _lock:
        if key in _cache:
            return _cache[key]
    from matplotlib.backends.backend_svg import FigureCanvasSVG
    from matplotlib.figure import Figure

    style = engine.style
    height = glyph_height_mm(name)
    width = glyphs.GLYPHS[name].aspect * height
    pad = PAD * height
    color = style.ACCENT_BY_NAME[TONES[tone]] if tone in TONES else None
    with getattr(engine.render, "_RENDER_LOCK", _lock), style.house_style():
        fig = Figure(figsize=((width + 2 * pad) * style.MM, (height + 2 * pad) * style.MM))
        FigureCanvasSVG(fig)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_xlim(-width / 2 - pad, width / 2 + pad)
        ax.set_ylim(-height / 2 - pad, height / 2 + pad)
        ax.set_aspect("equal")
        ax.set_axis_off()
        glyphs.draw_glyph(ax, name, 0.0, 0.0, height, color=color, context=tone == "none")
        buffer = io.BytesIO()
        fig.savefig(buffer, format="svg", transparent=True)
    data = buffer.getvalue()
    with _lock:
        _cache[key] = data
    return data


def art_info(engine: FigureEngine | None) -> dict | None:
    """What the page needs to place the pictures (None without Figure Studio's anatomy)."""
    if engine is None or engine.glyphs is None or "human_body" not in engine.glyphs.GLYPHS:
        return None
    available = sorted(n for n in ART_NAMES if n in engine.glyphs.GLYPHS)
    return {"version": art_version(engine), "pad": PAD, "names": available,
            "aspects": {n: engine.glyphs.GLYPHS[n].aspect for n in available}}
