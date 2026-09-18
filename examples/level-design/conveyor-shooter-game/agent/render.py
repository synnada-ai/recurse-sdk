# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Render a conveyor shooter board for human review: ASCII for the agent, SVG for the studio.

An image library such as Pillow is **not a declared dependency**, so rendering through one
would work locally and fail on the hosted runtime. SVG needs nothing but string formatting and is
sharper at this scale anyway, since a board
is a grid of flat squares.

Colours come from the extracted palette when it is bundled and fall back to a generated
ramp when it is not. The ramp is honest about being wrong: the id -> name binding was never
established (RULES.md §8), so a render is a *shape* review, never a colour proof.
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path
from typing import Any

from conveyor_game.model import FIRST_APPEARANCE, Cell, board_colours, grid_cells

APP_DIR = Path(__file__).resolve().parent
PALETTE_PATH = APP_DIR / "data" / "palette.json"

#: Characters for ASCII previews, one per colour id, plus markers for the other layers.
_GLYPHS = "0123456789abcdefghijklmnopqrstuvwx"
WALL_GLYPH = "#"
OBJECT_GLYPH = "*"
EMPTY_GLYPH = "."


#: Material ids confirmed against real gameplay screenshots, by matching the level files' own
#: id grids cell-for-cell to what is on screen. Levels 14 and 15 established the original eight;
#: levels 101, 112, 114, 115, 116 and 119 cover every additional id used in levels 1-150.
#:
#: This is the only ground truth there is for the binding. The extracted `palette.json` carries
#: 34 real material colours but explicitly does not say which *id* is which family, and the
#: families happen to be stored alphabetically — so every render before this used a colour
#: order with nothing behind it.
#:
#: Values are representative lit face colours sampled from the screen rather than raw Unity
#: material values. The game applies lighting and highlights, so these are perceptual render
#: colours, not claims about the underlying shader constants.
CONFIRMED_HEX: dict[int, str] = {
    0: "#18c8ef",  # blue-cyan — L116 corner accent
    1: "#1c4df3",  # deep blue — L116 camera lens and lower corners
    2: "#3c4049",  # charcoal — L15 outlines and its bottom row
    3: "#58e62f",  # bright green — L101 border, L115 and L119 accents
    4: "#ff8a0f",  # orange — L14 band, L15 fish body
    5: "#f55da6",  # pink — L14 top rows, L15 side edges
    6: "#8b44e8",  # violet — L14 band under the orange
    7: "#d91528",  # red — L116 upper-left accent
    8: "#4fd4f2",  # light cyan — L14 stripes, L15 water field
    9: "#ffc531",  # gold — L14 mid columns, L15 bottom corners
    10: "#f2f2f2",  # white — L15 top rows
    11: "#7f593c",  # brown — L101 outlines, L114 frame
    12: "#1ca613",  # dark green — L114 leaves, L119 accents
    13: "#0f8e6e",  # deep green — L14 bottom rows
    14: "#efb5f5",  # lilac — L116 lens highlight
    15: "#a4b788",  # sage — L112 lower snake
    16: "#efacc2",  # powder pink — L119 pale petals
    17: "#c3d3fb",  # lavender — L114 picture background
    18: "#bb5479",  # burgundy — L119 numbered corner blocks
    19: "#f7efb7",  # parmesan — L116 camera body
    20: "#e4727d",  # salmon — L101 ball, L112 upper snake
    21: "#c5cae5",  # cool grey — L112 rocket body
    22: "#656979",  # grey — L115 flower field
    23: "#4e2593",  # dark purple — L101 lower band
    24: "#cd0d8c",  # magenta — L114 cherries
    25: "#efb3e8",  # pale pink — L115 flower
    26: "#f3ca80",  # pastel orange — L101 animal body
}


def palette() -> list[str]:
    """The 34 colour families as ``#rrggbb``.

    Returns:
        One hex colour per id. Ids in `CONFIRMED_HEX` are screenshot-confirmed; the remaining
        later-game ids come from the extracted materials in an **unverified** order, or from a
        generated ramp when the palette is not bundled.
    """
    colours: list[str] = []
    if PALETTE_PATH.is_file():
        loaded = json.loads(PALETTE_PATH.read_text())
        if isinstance(loaded, list) and len(loaded) >= 34:
            colours = [str(entry["hex_srgb"]) for entry in loaded[:34]]
    if not colours:
        for index in range(34):
            hue = (index * 360 // 34) / 360
            colours.append(_hsv_hex(hue, 0.62 if index % 2 else 0.85, 0.95 if index % 3 else 0.7))
    # Screenshot-confirmed ids override the extracted order, which is only a guess for ids 27+.
    for index, hexcode in CONFIRMED_HEX.items():
        if index < len(colours):
            colours[index] = hexcode
    return colours


def _hsv_hex(hue: float, saturation: float, value: float) -> str:
    """Convert HSV in 0..1 to a hex colour."""
    i = int(hue * 6) % 6
    f = hue * 6 - int(hue * 6)
    p = value * (1 - saturation)
    q = value * (1 - f * saturation)
    t = value * (1 - (1 - f) * saturation)
    wheel = [
        (value, t, p),
        (q, value, p),
        (p, value, t),
        (p, q, value),
        (t, p, value),
        (value, p, q),
    ]
    r, g, b = wheel[i]
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def ascii_board(raw: dict[str, Any], max_side: int = 60) -> str:
    """A text preview of a board, for the agent to read back.

    Downsamples above `max_side` so a 119x140 board stays readable in a tool result. Each
    cell shows its colour's glyph, a wall, a standalone object, or empty background.

    Args:
        raw: A decoded level object.
        max_side: Longest side to render before downsampling.

    Returns:
        One line per row, y increasing **upward** as the game stores it.
    """
    pid = raw["PixelImageData"]
    width, height = pid["width"], pid["height"]
    colours = board_colours(raw)
    walls = _cells_of(pid, {"walls"})
    objects = _cells_of(pid, set(FIRST_APPEARANCE) - {"walls"}) - set(colours)

    step = max(1, (max(width, height) + max_side - 1) // max_side)
    rows = []
    for y in range(height - 1, -1, -step):
        row = []
        for x in range(0, width, step):
            cell = (x, y)
            if cell in colours:
                row.append(_GLYPHS[colours[cell] % len(_GLYPHS)])
            elif cell in walls:
                row.append(WALL_GLYPH)
            elif cell in objects:
                row.append(OBJECT_GLYPH)
            else:
                row.append(EMPTY_GLYPH)
        rows.append("".join(row))
    header = f"{width}x{height}"
    if step > 1:
        header += f" (shown 1:{step})"
    return f"{header}\n" + "\n".join(rows)


def _cells_of(pid: dict[str, Any], containers: set[str]) -> set[Cell]:
    """Cells occupied by the named board containers."""
    cells: set[Cell] = set()
    for name in containers:
        container = pid.get(name)
        if not isinstance(container, dict):
            continue
        for entries in container.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict):
                    points = entry.get("GridPoints") or entry.get("MainGridPoints") or []
                    cells.update(grid_cells(points))
    return cells


def _pixel_pipes(pid: dict[str, Any]) -> list[tuple[set[Cell], int, int]]:
    """Return pipe footprints, visible materials and aggregate counters for rendering."""
    result: list[tuple[set[Cell], int, int]] = []
    container = pid.get("pixelPipes")
    if not isinstance(container, dict):
        return result
    entries = container.get("Pipes")
    if not isinstance(entries, list):
        return result
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        queue = entry.get("Queue")
        if not isinstance(queue, list) or not queue or not isinstance(queue[0], dict):
            continue
        cells = set(grid_cells(entry.get("GridPoints", [])))
        material = queue[0].get("Material")
        if not cells or not isinstance(material, int):
            continue
        count = sum(
            segment.get("Count", 0)
            for segment in queue
            if isinstance(segment, dict) and isinstance(segment.get("Count"), int)
        )
        result.append((cells, material, count))
    return result


def _keys(pid: dict[str, Any]) -> list[set[Cell]]:
    """Return each collectible key footprint for distinct review rendering."""
    container = pid.get("keys")
    if not isinstance(container, dict):
        return []
    entries = container.get("Keys")
    if not isinstance(entries, list):
        return []
    result: list[set[Cell]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cells = set(grid_cells(entry.get("GridPoints", [])))
        if cells:
            result.append(cells)
    return result


def _egg_boxes(pid: dict[str, Any]) -> list[list[tuple[set[Cell], int, int]]]:
    """Return egg compartments with their visible material and health counter."""
    container = pid.get("eggBoxes")
    if not isinstance(container, dict):
        return []
    entries = container.get("EggBoxes")
    if not isinstance(entries, list):
        return []
    boxes: list[list[tuple[set[Cell], int, int]]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cells = set(grid_cells(entry.get("GridPoints", [])))
        area = entry.get("EggArea")
        eggs = entry.get("Eggs")
        if not cells or not isinstance(area, dict) or not isinstance(eggs, list):
            continue
        columns, rows = area.get("X"), area.get("Y")
        if not isinstance(columns, int) or not isinstance(rows, int) or columns < 1 or rows < 1:
            continue
        xs = tuple(range(min(x for x, _y in cells), max(x for x, _y in cells) + 1))
        ys = tuple(range(min(y for _x, y in cells), max(y for _x, y in cells) + 1))
        compartments: list[tuple[set[Cell], int, int]] = []
        for index, egg in enumerate(eggs):
            if not isinstance(egg, dict):
                continue
            material, count = egg.get("Material"), egg.get("Count")
            if not isinstance(material, int) or not isinstance(count, int):
                continue
            column, row = index % columns, index // columns
            x0, x1 = column * len(xs) // columns, (column + 1) * len(xs) // columns
            y0, y1 = row * len(ys) // rows, (row + 1) * len(ys) // rows
            compartments.append(({(x, y) for x in xs[x0:x1] for y in ys[y0:y1]}, material, count))
        if compartments:
            boxes.append(compartments)
    return boxes


def svg_board(raw: dict[str, Any], scale: int = 8) -> str:
    """A standalone SVG of a board's artwork.

    Args:
        raw: A decoded level object.
        scale: Pixels per cell.

    Returns:
        An `<svg>` element, self-contained.
    """
    pid = raw["PixelImageData"]
    width, height = pid["width"], pid["height"]
    colours = palette()
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width * scale}" '
        f'height="{height * scale}" viewBox="0 0 {width} {height}" '
        f'shape-rendering="crispEdges">',
        f'<rect width="{width}" height="{height}" fill="#141419"/>',
    ]
    for (x, y), colour in sorted(board_colours(raw).items()):
        # y is stored increasing upward; SVG y grows downward, so flip it.
        parts.append(
            f'<rect x="{x}" y="{height - 1 - y}" width="1" height="1" '
            f'fill="{colours[colour % len(colours)]}"/>'
        )
    for x, y in sorted(_cells_of(pid, {"walls"})):
        parts.append(
            f'<rect x="{x}" y="{height - 1 - y}" width="1" height="1" '
            f'fill="none" stroke="#f4f4f8" stroke-width="0.12"/>'
        )
    for box in _egg_boxes(pid):
        box_cells: set[Cell] = set()
        for cells, material, count in box:
            box_cells.update(cells)
            for x, y in sorted(cells):
                parts.append(
                    f'<rect data-kind="egg" x="{x}" y="{height - 1 - y}" '
                    f'width="1" height="1" fill="{colours[material % len(colours)]}" '
                    'stroke="#17171d" stroke-width="0.08"/>'
                )
            centre_x = sum(x for x, _y in cells) / len(cells) + 0.5
            centre_y = height - (sum(y for _x, y in cells) / len(cells) + 0.5)
            parts.append(
                f'<text x="{centre_x:g}" y="{centre_y:g}" fill="#fff" stroke="#111" '
                'stroke-width="0.06" paint-order="stroke" font-family="system-ui" '
                f'font-size="0.72" font-weight="800" text-anchor="middle" '
                f'dominant-baseline="central">{count}</text>'
            )
        min_x = min(x for x, _y in box_cells)
        max_x = max(x for x, _y in box_cells)
        min_y = min(y for _x, y in box_cells)
        max_y = max(y for _x, y in box_cells)
        parts.append(
            f'<rect data-kind="egg-box" x="{min_x}" y="{height - 1 - max_y}" '
            f'width="{max_x - min_x + 1}" height="{max_y - min_y + 1}" fill="none" '
            'stroke="#f1dfb0" stroke-width="0.2"/>'
        )
    for cells, material, count in _pixel_pipes(pid):
        for x, y in sorted(cells):
            parts.append(
                f'<rect data-kind="pixel-pipe" x="{x}" y="{height - 1 - y}" '
                f'width="1" height="1" fill="{colours[material % len(colours)]}" '
                'stroke="#121218" stroke-width="0.16"/>'
            )
        centre_x = sum(x for x, _y in cells) / len(cells) + 0.5
        centre_y = height - (sum(y for _x, y in cells) / len(cells) + 0.5)
        parts.append(
            f'<text x="{centre_x:g}" y="{centre_y:g}" fill="#fff" '
            'stroke="#111" stroke-width="0.08" paint-order="stroke" '
            f'font-family="system-ui" font-size="1.1" font-weight="800" '
            f'text-anchor="middle" dominant-baseline="central">{count}</text>'
        )
    for cells in _keys(pid):
        for x, y in sorted(cells):
            parts.append(
                f'<rect data-kind="key" x="{x}" y="{height - 1 - y}" '
                'width="1" height="1" fill="#ffd34d" stroke="#17171d" '
                'stroke-width="0.13"/>'
            )
        centre_x = sum(x for x, _y in cells) / len(cells) + 0.5
        centre_y = height - (sum(y for _x, y in cells) / len(cells) + 0.5)
        parts.append(
            f'<circle data-kind="keyhole" cx="{centre_x:g}" cy="{centre_y - 0.16:g}" '
            'r="0.22" fill="#17171d"/>'
        )
        parts.append(
            f'<rect data-kind="keyhole" x="{centre_x - 0.09:g}" y="{centre_y:g}" '
            'width="0.18" height="0.34" fill="#17171d"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _png(rows: list[list[tuple[int, int, int]]]) -> bytes:
    """Encode RGB rows as a PNG, stdlib only — Pillow is not a declared dependency."""
    width, height = len(rows[0]), len(rows)
    raw = b"".join(b"\x00" + b"".join(struct.pack("BBB", *px) for px in row) for row in rows)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def board_image_uri(raw: dict[str, Any], scale: int = 4) -> str:
    """The board as a base64 PNG data URI — what `look()` hands to the model.

    This is the vision path: agentia's image content parts (spike 993c3749) turn a tool
    result that is exactly a data URI into an image the model *sees*. The ASCII preview was
    the only channel before it, and the measured cost of that blindness is on record: the
    agent drew a lighthouse as an olive mass over 18 strokes with three previews and
    reported "fit 1.00".

    Rendered exactly as `svg_board` and the judges' PNGs: the same screenshot-calibrated palette,
    the same y-flip, the same dark canvas — the agent must see
    what the player and the panel see, not a third rendering.

    Args:
        raw: A decoded level object.
        scale: Pixels per cell. Kept small on purpose: the image re-uploads every turn and
            the context budget counts its base64 length, so a 36x44 board at 4 is ~150x180
            px and a few KB — plenty for a model to read a picture, cheap enough to call
            after every change.

    Returns:
        A ``data:image/png;base64,...`` string. Return it as the WHOLE tool result.
    """
    pid = raw["PixelImageData"]
    width, height = pid["width"], pid["height"]
    colours = palette()
    board = board_colours(raw)
    pipe_cells: dict[Cell, int] = {}
    for cells, material, _count in _pixel_pipes(pid):
        pipe_cells.update(dict.fromkeys(cells, material))
    key_cells = set().union(*_keys(pid)) if _keys(pid) else set()
    egg_cells: dict[Cell, int] = {}
    egg_edges: set[Cell] = set()
    for box in _egg_boxes(pid):
        for cells, material, _count in box:
            egg_cells.update(dict.fromkeys(cells, material))
            for x, y in cells:
                if any(
                    neighbour not in cells
                    for neighbour in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1))
                ):
                    egg_edges.add((x, y))
    background = (0x14, 0x14, 0x19)
    rows: list[list[tuple[int, int, int]]] = []
    for y in range(height - 1, -1, -1):
        logical: list[tuple[tuple[int, int, int], str | None]] = []
        for x in range(width):
            cell = (x, y)
            colour = egg_cells.get(cell, pipe_cells.get(cell, board.get(cell)))
            if cell in key_cells:
                pixel = (0xFF, 0xD3, 0x4D)
                kind = "key"
            elif colour is None:
                pixel = background
                kind = None
            else:
                code = colours[colour % len(colours)]
                pixel = (int(code[1:3], 16), int(code[3:5], 16), int(code[5:7], 16))
                if cell in egg_edges:
                    kind = "egg"
                elif cell in pipe_cells:
                    kind = "pipe"
                else:
                    kind = None
            logical.append((pixel, kind))
        for sub_y in range(scale):
            line: list[tuple[int, int, int]] = []
            for pixel, kind in logical:
                for sub_x in range(scale):
                    if kind is not None and (sub_x == 0 or sub_y == 0):
                        line.append(background)
                    else:
                        line.append(pixel)
            rows.append(line)
    return "data:image/png;base64," + base64.b64encode(_png(rows)).decode()


__all__ = [
    "CONFIRMED_HEX",
    "EMPTY_GLYPH",
    "OBJECT_GLYPH",
    "WALL_GLYPH",
    "ascii_board",
    "board_image_uri",
    "palette",
    "svg_board",
]
