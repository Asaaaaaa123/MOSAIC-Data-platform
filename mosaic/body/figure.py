"""The body map as Figure Studio figures: an illustrated body with the organs coloured by evidence,
labelled in the margins with leader lines, and a chart of the evidence behind each organ.

The scene uses Figure Studio's ``human_body`` and organ illustrations; positions come from
``anatomy.ORGANS`` (the same coordinates the web page uses).
"""

from __future__ import annotations

from typing import Any

from ..visuals.figures import FigureEngine, safe_text
from .anatomy import ORGANS, ORGANS_BY_KEY
from .models import LEVEL_LABELS, BodyMap, OrganFinding

LEVEL_COLORS = {"primary": "vermilion", "safety": "gold", "investigational": "purple", "expression": "sky"}
LEVEL_ORDER = ("primary", "safety", "investigational", "expression")
COLOUR_WORDS = {"primary": "Red", "safety": "Yellow", "investigational": "Purple", "expression": "Blue"}
SCENE_W = 179.0          # mm: a double-column panel (183 mm less the layout padding)
ASPECT = 1.0             # scene height / width
BODY_FRACTION = 0.91     # body height / scene height
BODY_CENTRE = 46.5       # % from the top: leaves a strip for the colour key under the feet
LABEL_COLUMN = 31.0      # % of the width taken by each label column
DETAIL_CHARS = 42        # characters per detail line (fits the label column at 7 pt)
MAX_LABELS = 14
TITLE_H = 2.6            # % of the height taken by a label's bold first line
DETAIL_H = 2.3           # % of the height per detail line
LABEL_GAP = 1.2          # % between two labels


def _labelled(bm: BodyMap) -> list[OrganFinding]:
    """Organs that get a label: every approved use / disease site, then the strongest others."""
    primary = [f for f in bm.organs if f.level == "primary"]
    others = sorted((f for f in bm.organs if f.level != "primary"), key=lambda f: -f.score)
    return (primary + others)[:MAX_LABELS]


def _detail_lines(f: OrganFinding) -> list[str]:
    parts = [p.strip() for p in (f.summary or "").split(" · ") if p.strip()]
    return [safe_text(p, DETAIL_CHARS) for p in parts[:2]] or [safe_text(LEVEL_LABELS[f.level], DETAIL_CHARS)]


def _spread(rows: list[list[Any]], lo: float = 2.0, hi: float = 92.0) -> None:
    """rows: [top_y, height, ...] in %; move labels apart (in place) so they never overlap."""
    rows.sort(key=lambda r: r[0])
    for i in range(1, len(rows)):
        rows[i][0] = max(rows[i][0], rows[i - 1][0] + rows[i - 1][1] + LABEL_GAP)
    overflow = rows[-1][0] + rows[-1][1] - hi if rows else 0
    if overflow > 0:
        rows[-1][0] -= overflow
        for i in range(len(rows) - 2, -1, -1):
            rows[i][0] = min(rows[i][0], rows[i + 1][0] - rows[i][1] - LABEL_GAP)
    for r in rows:
        r[0] = max(lo, r[0])


def body_scene(engine: FigureEngine, bm: BodyMap) -> Any:
    s = engine.spec
    H = SCENE_W * ASPECT
    body_h = BODY_FRACTION * H
    k = body_h / 2                     # mm per body unit
    cx, cy = SCENE_W / 2, H * BODY_CENTRE / 100

    def pct(u: float, v: float) -> tuple[float, float]:
        return 100 * (cx + u * k) / SCENE_W, 100 * (cy - v * k) / H

    found = {f.key: f for f in bm.organs}
    items = [s.SceneItem(id="body", icon="human_body", x=50.0, y=BODY_CENTRE, size=body_h, label_position="none", emphasis="context")]
    anchors: dict[str, tuple[float, float]] = {}
    for organ in ORGANS:
        f = found.get(organ.key)
        if f is None and not organ.always:
            continue
        positions = [(organ.u, organ.v)] + ([(-organ.u, organ.v)] if organ.mirror else [])
        for i, (u, v) in enumerate(positions):
            x, y = pct(u, v)
            items.append(s.SceneItem(id=f"{organ.key}{'_2' if i else ''}", icon=organ.glyph, x=x, y=y, size=organ.size * k,
                                     rotation=organ.rotation, label_position="none",
                                     emphasis="focus" if f else "context", color_key=LEVEL_COLORS[f.level] if f else ""))
        anchors[organ.key] = pct(*(organ.anchor or (organ.u, organ.v)))

    texts, arrows = [], []
    sides: dict[str, list[list[Any]]] = {"left": [], "right": []}
    for f in _labelled(bm):
        organ = ORGANS_BY_KEY[f.key]
        ax, ay = anchors.get(f.key, pct(organ.u, organ.v))
        side = "left" if organ.u < 0 else "right" if organ.u > 0 else ("left" if len(sides["left"]) <= len(sides["right"]) else "right")
        if organ.mirror:  # paired organ: point at the copy on the label's side
            ax = pct(-abs(organ.u) if side == "left" else abs(organ.u), organ.v)[0]
        lines = _detail_lines(f)
        height = TITLE_H + DETAIL_H * len(lines)
        sides[side].append([ay - TITLE_H / 2, height, f, lines, ax, ay])
    for side, rows in sides.items():
        if not rows:
            continue
        _spread(rows)
        for top, _height, f, lines, ax, ay in rows:
            left = side == "left"
            x_text = 1.0 if left else 99.0
            align = "left" if left else "right"
            title_y = top + TITLE_H / 2
            texts.append(s.SceneText(text=safe_text(f.en, 30), x=x_text, y=title_y, bold=True, align=align))
            for i, line in enumerate(lines):
                texts.append(s.SceneText(text=line, x=x_text, y=top + TITLE_H + DETAIL_H * (i + 0.5), size="small", align=align))
            x_edge = LABEL_COLUMN if left else 100 - LABEL_COLUMN
            arrows.append(s.SceneArrow(start=[x_edge, title_y], end=[ax, ay], kind="association", emphasis="context"))
    present = [lvl for lvl in LEVEL_ORDER if any(f.level == lvl for f in bm.organs)]
    key = " · ".join(f"{COLOUR_WORDS[lvl]}: {LEVEL_LABELS[lvl].lower()}" for lvl in present) + " · Grey: no evidence found"
    texts.append(s.SceneText(text=safe_text(key, 200), x=50.0, y=97.5, size="small", align="center"))
    return s.Scene(backdrop="none", aspect=ASPECT, items=items, texts=texts, arrows=arrows)


def body_figure(engine: FigureEngine, bm: BodyMap) -> Any:
    s = engine.spec
    present = [lvl for lvl in LEVEL_ORDER if any(f.level == lvl for f in bm.organs)]
    legend = (f"Where {bm.name} acts in the human body, from database evidence. Colours: "
              + "; ".join(f"{COLOUR_WORDS[l].lower()} = {LEVEL_LABELS[l].lower()}" for l in present)
              + "; grey = no evidence found. " + " ".join(bm.notes))
    return s.Figure(id="body_map", title=safe_text(f"Body map: {bm.name}", 120), width="double", height_mm=SCENE_W * ASPECT + 8,
                    panels=[s.Panel(letter="a", subheading=safe_text(f"{bm.name} ({bm.kind})", 80), kind="scene",
                                    scene=body_scene(engine, bm))],
                    legend=legend)


def evidence_figure(engine: FigureEngine, bm: BodyMap) -> Any | None:
    """Stacked bars: how many evidence items of each kind put each organ on the map."""
    s = engine.spec
    organs = sorted(bm.organs, key=lambda f: (LEVEL_ORDER.index(f.level), -f.score))[:16]
    if not organs:
        return None
    series = []
    for level in LEVEL_ORDER:
        values = [float(sum(1 for ev in f.evidence if _level(ev.kind) == level)) for f in organs]
        if any(values):
            series.append(s.Series(name=safe_text(LEVEL_LABELS[level], 40), values=values, color_key=LEVEL_COLORS[level]))
    chart = s.Chart(chart_type="horizontal_bar" if len(series) == 1 else "stacked_bar", categories=[safe_text(f.en, 30) for f in organs],
                    series=series, x_label="Evidence items (n)" if len(series) == 1 else "",
                    y_label="Evidence items (n)" if len(series) > 1 else "",
                    data_source="Number of database evidence items per organ and kind (see organ_evidence.csv)")
    return s.Figure(id="organ_evidence", title=safe_text(f"Evidence per organ: {bm.name}", 120),
                    width="double" if len(organs) > 6 else "single", height_mm=80.0,
                    panels=[s.Panel(letter="a", subheading="Evidence per organ", kind="chart", chart=chart)],
                    legend="Number of evidence items per organ, by kind: " + "; ".join(LEVEL_LABELS[l] for l in LEVEL_ORDER))


def _level(kind: str) -> str:
    from .models import LEVEL_OF

    return LEVEL_OF[kind]


def figure_specs(engine: FigureEngine, bm: BodyMap) -> dict[str, Any]:
    out = {"body_map": body_figure(engine, bm)}
    ev = evidence_figure(engine, bm)
    if ev is not None:
        out["organ_evidence"] = ev
    return out


def figure_names(bm: BodyMap) -> list[str]:
    return ["body_map"] + (["organ_evidence"] if bm.organs else [])


def render(engine: FigureEngine, bm: BodyMap, name: str, fmt: str) -> bytes:
    from ..visuals.figures import FORMATS, PNG_DPI

    if fmt not in FORMATS:
        raise ValueError(fmt)
    specs = figure_specs(engine, bm)
    if name not in specs:
        raise KeyError(name)
    return engine.render.render_figure(specs[name], engine.style.ColorRegistry(), fmt, PNG_DPI).data


def render_all(engine: FigureEngine, bm: BodyMap) -> tuple[dict[str, bytes], list[str]]:
    out: dict[str, bytes] = {}
    notes: list[str] = []
    for name, spec in figure_specs(engine, bm).items():
        for fmt in ("svg", "png", "pdf"):
            try:
                out[f"figures/{name}.{fmt}"] = render(engine, bm, name, fmt)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"figure {name}.{fmt} could not be drawn ({type(exc).__name__}: {exc})")
        out[f"figures/{name}_legend.txt"] = f"{spec.title}\r\n\r\n{spec.legend}\r\n".encode("utf-8-sig")
    return out, notes
