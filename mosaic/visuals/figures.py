"""Journal-style figures of the visual summary, drawn by Figure Studio's renderer (figuregen).

Figure Studio is the separate "graph generation" project (``figuregen/``: figure-plan models and a
matplotlib renderer in Nature house style). Mosaic does not copy it: it loads that package from

1. ``MOSAIC_FIGUREGEN_PATH`` (the Figure Studio folder, or its ``figuregen`` folder), else
2. a sibling of the Mosaic folder named ``graph generation``, ``graph-generation``,
   ``graph_generation``, ``figure-studio`` or ``figure_studio``,

and turns each ``ChartData`` / the mind map into a figuregen ``Figure`` spec. Only the renderer is
used (no Claude call): the numbers drawn are exactly the counted values in ``ChartData``. Loading is
by file location, so Figure Studio's own ``web``/``tests`` folders never shadow Mosaic modules.
When figuregen or matplotlib is missing, ``load_engine`` returns the reason and the rest of
Mosaic (data, in-browser mind map and charts) keeps working.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from .build import GROUP_LABELS, ChartData, MindNode, Visuals, clip_words, group_label

log = logging.getLogger(__name__)

FORMATS = {"svg": "image/svg+xml", "png": "image/png", "pdf": "application/pdf"}
SIBLING_NAMES = ("graph generation", "graph-generation", "graph_generation", "figure-studio", "figure_studio")
PNG_DPI = 300
CJK_FONTS = ("Microsoft YaHei", "SimHei", "PingFang SC", "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Zen Hei")
# Colours are always given by accent name (figuregen's palette), so a fixed colour is never handed
# out a second time to another series: queries, grades and kinds of data each keep their own.
QUERY_COLORS = ("blue", "vermilion", "teal", "gold")
GRADE_COLORS = {"Grade A": "teal", "Grade B": "blue", "Grade C": "gold", "Grade D": "vermilion"}
GROUP_COLORS = {
    "papers": "blue", "clinical_trials": "vermilion", "datasets_repositories": "teal", "drugs_compounds": "gold",
    "genes_proteins": "magenta", "pathways_ontology": "purple", "diseases_phenotypes": "sky",
    "safety_regulatory": "vermilion", "variants_associations": "magenta", "projects_studies": "sky",
    "bioactivity_assays": "gold", "people_institutions": "purple", "guidelines": "teal", "other": "sky",
}
MINDMAP_GROUPS = 6
MINDMAP_LEAVES = 2

_lock = threading.Lock()
_engine: "FigureEngine | None" = None
_engine_error: str | None = None
_engine_key: str | None = None


@dataclass
class FigureEngine:
    path: Path
    spec: ModuleType
    render: ModuleType
    style: ModuleType
    glyphs: ModuleType | None = None


def candidate_paths(configured: str | None) -> list[Path]:
    if configured:
        return [Path(configured).expanduser()]
    mosaic_root = Path(__file__).resolve().parents[2]
    return [mosaic_root.parent / name for name in SIBLING_NAMES]


def _package_dir(path: Path) -> Path | None:
    for candidate in (path / "figuregen", path):
        if (candidate / "__init__.py").is_file() and (candidate / "render.py").is_file() and (candidate / "spec.py").is_file():
            return candidate
    return None


def _import_figuregen(package_dir: Path) -> FigureEngine:
    existing = sys.modules.get("figuregen")
    if existing is None or Path(getattr(existing, "__file__", "") or "").resolve().parent != package_dir.resolve():
        for name in [m for m in sys.modules if m == "figuregen" or m.startswith("figuregen.")]:
            del sys.modules[name]
        spec = importlib.util.spec_from_file_location(
            "figuregen", package_dir / "__init__.py", submodule_search_locations=[str(package_dir)]
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load figuregen from {package_dir}")
        module = importlib.util.module_from_spec(spec)
        sys.modules["figuregen"] = module
        spec.loader.exec_module(module)
    style = importlib.import_module("figuregen.style")
    _add_cjk_fallback(style)
    return FigureEngine(path=package_dir.parent, spec=importlib.import_module("figuregen.spec"),
                        render=importlib.import_module("figuregen.render"), style=style,
                        glyphs=importlib.import_module("figuregen.glyphs"))


def _add_cjk_fallback(style: ModuleType) -> None:
    """Queries and titles can be Chinese: let matplotlib fall back to an installed CJK font for those glyphs."""
    rc = getattr(style, "RC", None)
    if not isinstance(rc, dict):
        return
    from matplotlib import font_manager

    fonts = list(rc.get("font.sans-serif") or [])
    for name in CJK_FONTS:
        if name in fonts:
            break
        try:
            font_manager.findfont(font_manager.FontProperties(family=name), fallback_to_default=False)
        except ValueError:
            continue
        fonts.append(name)
        break
    rc["font.sans-serif"] = fonts


def load_engine(configured_path: str | None = None) -> tuple[FigureEngine | None, str | None]:
    """The figuregen renderer, or ``(None, reason)``. Cached per configured path."""
    global _engine, _engine_error, _engine_key
    key = configured_path or ""
    with _lock:
        if _engine_key == key and (_engine is not None or _engine_error is not None):
            return _engine, _engine_error
        _engine, _engine_error, _engine_key = None, None, key
        tried = candidate_paths(configured_path)
        package_dir = next((p for p in map(_package_dir, tried) if p is not None), None)
        if package_dir is None:
            _engine_error = ("Figure Studio (figuregen) was not found; set MOSAIC_FIGUREGEN_PATH to the 'graph generation' "
                             "folder. Looked in: " + ", ".join(str(p) for p in tried))
            return None, _engine_error
        try:
            _engine = _import_figuregen(package_dir)
        except Exception as exc:  # noqa: BLE001 - missing matplotlib, errors in the other project ...
            log.warning("could not load figuregen from %s", package_dir, exc_info=True)
            _engine_error = f"Figure Studio found at {package_dir} but could not be loaded: {type(exc).__name__}: {exc}"
        return _engine, _engine_error


def reset_engine() -> None:
    global _engine, _engine_error, _engine_key
    with _lock:
        _engine, _engine_error, _engine_key = None, None, None


# ---------------------------------------------------------------- spec building
def safe_text(text: Any, limit: int = 52) -> str:
    """Plain text for matplotlib: '$' would start mathtext and '^' a superscript in figuregen."""
    s = clip_words(str(text if text is not None else ""), limit)
    return s.replace("\\", "/").replace("$", r"\$").replace("^", "ˆ")


def palette(visuals: Visuals) -> dict[str, str]:
    """Series name -> accent name, the same in every figure of the summary."""
    colors = {q: QUERY_COLORS[i % len(QUERY_COLORS)] for i, q in enumerate(visuals.queries)}
    colors.update(GRADE_COLORS)
    for group, label in GROUP_LABELS.items():
        colors.setdefault(label, GROUP_COLORS.get(group, "sky"))
    return colors


def _chart_height(chart: ChartData) -> float:
    n_cat = len(chart.categories)
    n_series = max(1, len(chart.series))
    if chart.kind == "hbar":
        return min(240.0, 22.0 + n_cat * (4.2 + 2.6 * n_series))
    if chart.kind == "heatmap":
        return min(240.0, 34.0 + 6.5 * n_series)
    return 72.0


def _chart_width(chart: ChartData) -> str:
    if chart.kind in ("bar", "stacked") and len(chart.categories) * max(1, len(chart.series) if chart.kind == "bar" else 1) > 10:
        return "double"
    if chart.kind == "heatmap" and len(chart.categories) > 4:
        return "double"
    return "single"


def chart_spec(engine: FigureEngine, chart: ChartData, colors: dict[str, str] | None = None) -> Any:
    s = engine.spec
    colors = colors or {}
    kind = {"hbar": "horizontal_bar", "bar": "grouped_bar" if len(chart.series) > 1 else "bar",
            "stacked": "stacked_bar", "heatmap": "heatmap"}[chart.kind]
    series = []
    for item in chart.series:
        color = "blue" if chart.kind == "heatmap" else colors.get(item.name, QUERY_COLORS[len(series) % len(QUERY_COLORS)])
        series.append(s.Series(name=safe_text(item.name, 40), values=[float(v) for v in item.values], color_key=color))
    return s.Chart(
        chart_type=kind, categories=[safe_text(c, 44) for c in chart.categories], series=series,
        x_label=safe_text(chart.x_label), y_label=safe_text(chart.y_label), value_label=safe_text(chart.value_label),
        show_totals=chart.kind == "stacked" and len(chart.categories) <= 12,
        data_source=chart.definition,
    )


def chart_figure(engine: FigureEngine, chart: ChartData, colors: dict[str, str] | None = None) -> Any:
    s = engine.spec
    return s.Figure(
        id=chart.id, title=safe_text(chart.title, 120), key_message=chart.definition,
        width=_chart_width(chart), height_mm=_chart_height(chart),
        panels=[s.Panel(letter="a", subheading=safe_text(chart.title, 80), kind="chart",
                        chart=chart_spec(engine, chart, colors))],
        legend=chart.definition,
    )


def overview_figure(engine: FigureEngine, visuals: Visuals) -> Any | None:
    """One multi-panel figure (a-d) of the main charts, Nature double-column width."""
    s = engine.spec
    preferred = ["entities_by_type", "overlap", "records_by_source", "quality_grades", "timeline", "trial_phases"]
    by_id = {c.id: c for c in visuals.charts}
    chosen = [by_id[k] for k in preferred if k in by_id][:4]
    if len(chosen) < 2:
        return None
    letters = "abcd"
    colors = palette(visuals)
    panels = [s.Panel(letter=letters[i], subheading=safe_text(c.title, 60), kind="chart", chart=chart_spec(engine, c, colors))
              for i, c in enumerate(chosen)]
    layout = ["ab", "cd"] if len(panels) == 4 else ["ab", "cc"] if len(panels) == 3 else ["ab"]
    height = 0.0
    for row in layout:
        height += max(_chart_height(chosen[letters.index(ch)]) for ch in set(row))
    legend = " ".join(f"{letters[i]} | {c.definition}" for i, c in enumerate(chosen))
    return s.Figure(id="overview", title=safe_text("Search overview: " + " vs ".join(visuals.queries), 120),
                    width="double", height_mm=min(247.0, max(90.0, height * 0.8)), layout=layout, panels=panels,
                    legend=legend)


def mindmap_figure(engine: FigureEngine, visuals: Visuals) -> Any:
    """The mind map as a figuregen diagram: the query on the left and one shaded band (lane) per
    branch holding the branch and its best entities, so every branch lines up with its leaves."""
    s = engine.spec
    root = visuals.mindmap
    colors = palette(visuals)
    nodes = [s.Node(id="root", label=safe_text(root.label, 60), detail=safe_text(root.detail, 60), shape="pill",
                    color_key="" if visuals.compare else QUERY_COLORS[0], rank=0, order=0)]
    edges: list[Any] = []
    lanes: list[Any] = []
    order = {1: 0, 2: 0}

    def add(node: MindNode, parent: str, rank: int, lane: str, color: str) -> str:
        nid = f"n{len(nodes)}"
        leaf = node.kind == "entity"
        label = safe_text(node.label, 34 if leaf else 36)
        if node.count is not None and not leaf:
            label = f"{label} ({node.count})"
        nodes.append(s.Node(id=nid, label=label, detail=safe_text(node.detail, 40) if leaf else "",
                            color_key="" if leaf else color, emphasis="context" if leaf else "focus",
                            group=lane, rank=rank, order=order[rank]))
        order[rank] += 1
        edges.append(s.Edge(source=parent, target=nid, kind="association", emphasis="context" if leaf else "focus"))
        return nid

    branches = root.children if visuals.compare else root.children[:MINDMAP_GROUPS]
    # compare: every query shows the same kinds of data (the largest overall), so the bands line up
    totals: dict[str, int] = {}
    for q in root.children:
        if q.kind == "query":
            for g in q.children:
                totals[g.group] = totals.get(g.group, 0) + (g.count or 0)
    shared_kinds = sorted(totals, key=lambda k: -totals[k])[:MINDMAP_GROUPS]
    for i, branch in enumerate(branches):
        if branch.kind == "group":
            color = GROUP_COLORS.get(branch.group, "sky")
        elif branch.kind == "query":
            color = colors.get(branch.label, QUERY_COLORS[i % len(QUERY_COLORS)])
        else:
            color = "purple"
        lane = f"lane{i}"
        lanes.append(s.Group(id=lane, label="", style="box", color_key=color))
        bid = add(branch, "root", 1, lane, color)
        if branch.kind == "query":  # compare: each query's kinds of data, with counts (0 when it found none)
            by_kind = {g.group: g for g in branch.children}
            for kind in shared_kinds:
                group = by_kind.get(kind) or MindNode(id=f"{branch.id}:g:{kind}", label=group_label(kind), kind="group",
                                                      group=kind, count=0)
                add(group.model_copy(update={"children": []}), bid, 2, lane, GROUP_COLORS.get(kind, "sky"))
        else:
            for leaf in branch.children[: MINDMAP_LEAVES + 1 if branch.kind == "shared" else MINDMAP_LEAVES]:
                add(leaf, bid, 2, lane, color)
    n_rows = max(order[2], order[1], 1)
    height = min(247.0, max(80.0, 16.0 + n_rows * 14.0))
    hidden = len(root.children) - len(branches)
    if visuals.compare:
        legend = ("Mind map of the data found: each query with the kinds of data it found (number of entities), "
                  "and the entities found by two or more queries.")
    else:
        legend = ("Mind map of the data found: each band is one kind of data with its number of entities and its "
                  "best-graded entities (year and key facts underneath).")
    if hidden:
        legend += f" {hidden} smaller branch{'es are' if hidden != 1 else ' is'} in mindmap.json."
    return s.Figure(id="mindmap", title=safe_text("Mind map: " + root.label, 120), width="double", height_mm=height,
                    panels=[s.Panel(letter="a", subheading=safe_text(root.label, 80), kind="diagram",
                                    diagram=s.Diagram(direction="left_right", nodes=nodes, edges=edges, groups=lanes))],
                    legend=legend)


def figure_specs(engine: FigureEngine, visuals: Visuals) -> dict[str, Any]:
    """name -> figuregen Figure, in display order."""
    figures: dict[str, Any] = {"mindmap": mindmap_figure(engine, visuals)}
    overview = overview_figure(engine, visuals)
    if overview is not None:
        figures["overview"] = overview
    colors = palette(visuals)
    for chart in visuals.charts:
        figures[chart.id] = chart_figure(engine, chart, colors)
    return figures


def figure_names(visuals: Visuals) -> list[str]:
    """Names ``figure_specs`` produces, without needing the renderer."""
    by_id = {c.id for c in visuals.charts}
    overview = sum(1 for k in ("entities_by_type", "overlap", "records_by_source", "quality_grades", "timeline",
                               "trial_phases") if k in by_id) >= 2
    return ["mindmap"] + (["overview"] if overview else []) + [c.id for c in visuals.charts]


def render(engine: FigureEngine, visuals: Visuals, name: str, fmt: str) -> bytes:
    """Render one figure (``mindmap``, ``overview`` or a chart id) as svg / png / pdf."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown figure format {fmt!r}")
    figures = figure_specs(engine, visuals)
    if name not in figures:
        raise KeyError(name)
    return engine.render.render_figure(figures[name], engine.style.ColorRegistry(), fmt, PNG_DPI).data


def render_all(engine: FigureEngine, visuals: Visuals,
               formats: tuple[str, ...] = ("svg", "png", "pdf")) -> tuple[dict[str, bytes], list[str]]:
    """Every figure in every format: ``{"figures/<name>.<fmt>": bytes}`` plus notes about failures."""
    out: dict[str, bytes] = {}
    notes: list[str] = []
    for name, figure in figure_specs(engine, visuals).items():
        for fmt in formats:
            try:
                out[f"figures/{name}.{fmt}"] = engine.render.render_figure(figure, engine.style.ColorRegistry(), fmt, PNG_DPI).data
            except Exception as exc:  # noqa: BLE001 - one bad figure must not lose the package
                log.warning("figure %s.%s failed", name, fmt, exc_info=True)
                notes.append(f"figure {name}.{fmt} could not be drawn ({type(exc).__name__}: {exc})")
        out[f"figures/{name}_legend.txt"] = f"{figure.title}\r\n\r\n{figure.legend}\r\n".encode("utf-8-sig")
    return out, notes
