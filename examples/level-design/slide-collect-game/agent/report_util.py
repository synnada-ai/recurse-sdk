# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Report utility: game-look SVG boards + the reviewer HTML page.

Produces the human-reviewable report.html; it lands in the run workspace and
rides back through artifacts.
"""

import html
from collections import defaultdict
from collections.abc import Callable
from itertools import count
from pathlib import Path
from typing import Any

import metrics
from sim import Coord, Level

# --------------------------------------------------------------------- rendering

_PALETTE = {
    # Approximation of the game's ten color indices (see the studio's level-20 shot).
    0: "#2fa3a0",
    1: "#4caf50",
    2: "#d3382f",
    3: "#f0bc2e",
    4: "#8d6e63",
    5: "#f48fb1",
    6: "#c2185b",
    7: "#2b4a9b",
    8: "#2e7d32",
    9: "#7e57c2",
}


def _outline(
    cells: tuple[Coord, ...] | list[Coord],
    cell: int,
    xy: Callable[[Coord], tuple[float, float]],
) -> str:
    """Closed path(s) around the union of cells (rectilinear edge tracing)."""
    cell_set = set(cells)
    nxt = defaultdict(list)
    for x, y in cell_set:
        px, py = xy((x, y))
        if (x, y + 1) not in cell_set:
            nxt[(px, py)].append((px + cell, py))
        if (x, y - 1) not in cell_set:
            nxt[(px + cell, py + cell)].append((px, py + cell))
        if (x - 1, y) not in cell_set:
            nxt[(px, py + cell)].append((px, py))
        if (x + 1, y) not in cell_set:
            nxt[(px + cell, py)].append((px + cell, py + cell))
    paths = []
    while nxt:
        a = next(iter(nxt))
        loop = [a]
        b = nxt[a].pop()
        if not nxt[a]:
            del nxt[a]
        while b != loop[0]:
            loop.append(b)
            outs = nxt[b]
            c = outs.pop()
            if not outs:
                del nxt[b]
            b = c
        paths.append("M" + "L".join(f"{x:.0f},{y:.0f}" for x, y in loop) + "Z")
    return "".join(paths)


def _shade(hexcolor: str, f: float) -> str:
    """Scale a hex color's brightness by a factor."""
    r, g, b = (int(hexcolor[i : i + 2], 16) for i in (1, 3, 5))
    r, g, b = (max(0, min(255, round(v * f))) for v in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


# Deterministic per-process SVG id source: unique clipPath ids in multi-board
# pages regardless of GC address reuse, and stable output for snapshots.
_SVG_UID = count()


def render_svg(  # noqa: PLR0915 - one linear drawing pass over the board layers
    level: Level, cell: int = 26, mini: bool = False
) -> str:
    """Draw a board snapshot the way the game reads.

    The floor has cut-out obstacles, seats are sunken holes with a bright rim and a corner
    capacity chip, settlers are little figures, and elevator tabs sit outside the silhouette.

    The markup relies on the report page's CSS classes (``board``, ``fig``,
    ``chip``...) — embed it in a page that defines them (``write_report``);
    it is not a self-contained ``.svg`` file.
    """
    m = cell
    w, h = level.width * cell, level.height * cell
    uid = f"b{next(_SVG_UID):x}"
    p = [f'<svg class="board" role="img" viewBox="{-m} {-m} {w + 2 * m} {h + 2 * m}">']

    def xy(c: Coord) -> tuple[float, float]:
        """Map a cell to its top-left SVG coordinate; row 0 draws at the bottom."""
        return c[0] * cell, (level.height - 1 - c[1]) * cell

    floor_cells = [
        (x, y)
        for x in range(level.width)
        for y in range(level.height)
        if (x, y) not in level.obstacles
    ]
    floor_path = _outline(floor_cells, cell, xy)
    p.append(f'<clipPath id="{uid}"><path d="{floor_path}" fill-rule="evenodd"/></clipPath>')
    p.append(f'<path class="floor" d="{floor_path}" fill-rule="evenodd"/>')
    grid = []
    for x in range(1, level.width):
        grid.append(
            f'<line class="gridl" x1="{x * cell}" y1="{-cell}" x2="{x * cell}" y2="{h + cell}"/>'
        )
    for y in range(1, level.height):
        grid.append(
            f'<line class="gridl" x1="{-cell}" y1="{y * cell}" x2="{w + cell}" y2="{y * cell}"/>'
        )
    p.append(f'<g clip-path="url(#{uid})">{"".join(grid)}</g>')

    for seat in level.seats:
        base = _PALETTE[seat.color]
        outline = _outline(seat.cells, cell, xy)
        # sunken well: dark interior, bright rim of the seat's color
        p.append(
            f'<path d="{outline}" fill="{_shade(base, 0.45)}" stroke="{base}" '
            f'stroke-width="{cell * 0.16:.1f}" stroke-linejoin="round"/>'
        )
        p.append(
            f'<path d="{outline}" fill="none" stroke="rgba(0,0,0,.35)" '
            f'stroke-width="1.4" stroke-linejoin="round"/>'
        )
        if not mini:
            corner = max(seat.cells, key=lambda c: (-c[1], c[0]))  # bottom-right cell
            ax, ay = xy(corner)
            marks = (
                ("D" if seat.inner_color is not None else "")
                + ("L" if seat.lock_id is not None else "")
                + ("C" if seat.connected_id is not None else "")
            )
            if seat.axis_lock:
                # the live game draws a double-headed arrow along the movable
                # axis on the seat itself (level-15 tutorial footage)
                glyph = "&#8597;" if seat.axis_lock == 1 else "&#8596;"
                cxs = [c[0] for c in seat.cells]
                cys = [c[1] for c in seat.cells]
                gx, gy = sum(cxs) / len(cxs), sum(cys) / len(cys)
                gc = min(seat.cells, key=lambda c: (c[0] - gx) ** 2 + (c[1] - gy) ** 2)
                gpx, gpy = xy(gc)
                p.append(
                    f'<text class="axisg" x="{gpx + cell / 2}" '
                    f'y="{gpy + cell / 2 + cell * 0.16}" '
                    f'font-size="{cell * 0.52:.0f}">{glyph}</text>'
                )
            label = str(len(seat.cells)) + marks
            bw = cell * (0.42 + 0.17 * len(marks))
            bx, by = ax + cell - bw - 1.5, ay + cell - cell * 0.42 - 1.5
            p.append(
                f'<rect class="chip" x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" '
                f'height="{cell * 0.42:.1f}" rx="3.5"/>'
            )
            p.append(
                f'<text class="cap" x="{bx + bw / 2:.1f}" y="{by + cell * 0.33:.1f}" '
                f'font-size="{cell * 0.34:.0f}">{label}</text>'
            )

    for (x, y), s in level.settlers:
        px, py = xy((x, y))
        cx, cy = px + cell / 2, py + cell / 2
        base = _PALETTE[s.color]
        # a small standing figure: body + head, single color, soft dark edge
        p.append(
            f'<ellipse class="figb" cx="{cx}" cy="{cy + cell * 0.13}" '
            f'rx="{cell * 0.26}" ry="{cell * 0.24}" fill="{base}"/>'
        )
        p.append(
            f'<circle class="figh" cx="{cx}" cy="{cy - cell * 0.16}" '
            f'r="{cell * 0.17}" fill="{base}"/>'
        )
        if s.key_id is not None and not mini:
            p.append(
                f'<text class="keyg" x="{cx}" y="{cy + cell * 0.26}" '
                f'font-size="{cell * 0.36:.0f}">K</text>'
            )

    for e in level.elevators:
        px, py = xy(e.cell)
        dx, dy = e.offset
        if (dx, dy) == (0, 0):
            p.append(
                f'<circle class="spawn" cx="{px + cell / 2}" cy="{py + cell / 2}" '
                f'r="{cell * 0.42}"/>'
            )
            tx, ty = px + cell / 2, py + cell / 2
        else:
            tx = px + cell / 2 + dx * cell * 0.94
            ty = py + cell / 2 - dy * cell * 0.94
            p.append(
                f'<rect class="etab" x="{tx - cell * 0.38}" y="{ty - cell * 0.38}" '
                f'width="{cell * 0.76}" height="{cell * 0.76}" rx="6"/>'
            )
            hx, hy = px + cell / 2 + dx * cell * 0.44, py + cell / 2 - dy * cell * 0.44
            p.append(
                f'<path class="ehead" d="M{hx - dy * cell * 0.15},{hy - dx * cell * 0.15} '
                f"L{hx - dx * cell * 0.17},{hy + dy * cell * 0.17} "
                f'L{hx + dy * cell * 0.15},{hy + dx * cell * 0.15}Z"/>'
            )
        if not mini:
            p.append(
                f'<text class="ecount" x="{tx}" y="{ty + cell * 0.14}" '
                f'font-size="{cell * 0.38:.0f}">{len(e.queue)}</text>'
            )
    p.append("</svg>")
    return "".join(p)


def write_report(records: list[dict[str, Any]], out_dir: Path, note: str) -> Path:
    """One self-contained HTML page with a board per saved level."""
    rows = []
    for record in records:
        level = Level.load(record["path"])
        feats = metrics.level_features(level)
        summary = ", ".join(f"{k}={v:g}" for k, v in sorted(feats.items()))
        rows.append(
            f"<h2>slot {record['slot']} — {html.escape(record['note'])}</h2>"
            f"<p class='m'>taps={record['taps']} variant={record['variant']} "
            f"order_robust={record['order_robust']}<br>{html.escape(summary)}</p>"
            f"{render_svg(level)}"
        )
    page = (
        "<!doctype html><meta charset='utf-8'><title>slide-collect designs</title>"
        "<style>body{font-family:system-ui;margin:24px} .m{font-family:monospace;"
        "font-size:12px;color:#444}"
        ".board{max-width:420px}"
        ".board .floor{fill:#CBD6E4;stroke:#93A5BD;stroke-width:2;stroke-linejoin:round}"
        ".board .gridl{stroke:#AEBDD0;stroke-width:.8}"
        ".board .chip{fill:#fff;stroke:rgba(0,0,0,.25);stroke-width:.8}"
        ".board .cap{fill:#26303F;text-anchor:middle;font:700 13px monospace}"
        ".board .figb,.board .figh{stroke:rgba(0,0,0,.4);stroke-width:1.2}"
        ".board .keyg,.board .axisg{fill:#fff;text-anchor:middle;font:700 12px monospace;"
        "paint-order:stroke;stroke:rgba(0,0,0,.65);stroke-width:2px}"
        ".board .etab,.board .ehead{fill:#2E4C7E}"
        ".board .spawn{fill:none;stroke:#2E4C7E;stroke-width:2;stroke-dasharray:4 3}"
        ".board .ecount{fill:#fff;text-anchor:middle;font:700 12px monospace}</style>"
        f"<h1>level-designer-slide-collect — generated levels</h1><p>{html.escape(note)}</p>"
        + "".join(rows)
    )
    out = out_dir / "report.html"
    out.write_text(page)
    return out
