# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Corpus-derived, source-preserving composition treatments for conveyor shooter boards.

These functions do not draw the subject. They arrange ordinary gameplay pixels around an
already approved frozen source and return source-safe coordinates where a later design loop can
integrate board mechanics. Every treatment is deterministic and independently ablatable.
"""

from dataclasses import dataclass

from builder import BuildError, Design
from conveyor_game.model import COLOUR_IDS, Cell

SCHOOLS = (
    "segmented-squeeze",
    "scenic-field",
    "layered-inset-frame",
    "peripheral-compartments",
)


#: Scenic clouds span this many cells either side of their centre.
_CLOUD_HALF_WIDTH = 3
#: Cells this close to the board edge belong to the outer band.
_EDGE_BAND = 2
#: A surrounding field needs at least this many materials.
MIN_PALETTE_SIZE = 4


@dataclass(frozen=True)
class MechanicAnchor:
    """One source-safe rectangular region proposed for a later mechanic."""

    kind: str
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class CompositionResult:
    """Factual result of one deterministic composition treatment."""

    school: str
    added_cells: int
    source_cells_changed: int
    occupied_share: float
    mechanic_anchors: tuple[MechanicAnchor, ...]


def _distance_to_edge(cell: Cell, width: int, height: int) -> int:
    """Distance in cells from a cell to the nearest board edge."""
    x, y = cell
    return min(x, y, width - 1 - x, height - 1 - y)


def _paint_free(design: Design, source: set[Cell], cell: Cell, material: int) -> None:
    """Paint one cell unless it belongs to the approved source."""
    if cell not in source:
        design.picture[cell] = material


def _candidate_anchors(  # noqa: PLR0913, PLR0917 - geometry inputs of one placement search
    width: int,
    height: int,
    footprint_width: int,
    footprint_height: int,
    seed: int,
    prefer_interior: bool,
) -> list[Cell]:
    """Return deterministic edge-first lower-left anchor candidates."""
    candidates = [
        (x, y)
        for x in range(width - footprint_width + 1)
        for y in range(height - footprint_height + 1)
    ]

    def edge_distance(cell: Cell) -> int:
        """Distance in cells from a cell to the nearest board edge."""
        x, y = cell
        return min(
            x,
            y,
            width - footprint_width - x,
            height - footprint_height - y,
        )

    candidates.sort(
        key=lambda cell: (
            -edge_distance(cell) if prefer_interior else edge_distance(cell),
            (cell[0] * 37 + cell[1] * 19 + seed) % 101,
            cell[1],
            cell[0],
        )
    )
    return candidates


def _source_safe_anchors(  # noqa: PLR0913 - geometry inputs of one placement search
    source: set[Cell],
    width: int,
    height: int,
    *,
    kind: str,
    footprint_width: int,
    footprint_height: int,
    count: int,
    seed: int,
    prefer_interior: bool = False,
) -> tuple[MechanicAnchor, ...]:
    """Choose non-overlapping candidate footprints that do not touch frozen source pixels."""
    selected: list[MechanicAnchor] = []
    used: set[Cell] = set()
    ordered = _candidate_anchors(
        width,
        height,
        footprint_width,
        footprint_height,
        seed,
        prefer_interior,
    )
    valid = []
    for rank, (x, y) in enumerate(ordered):
        footprint = frozenset(
            (px, py)
            for px in range(x, x + footprint_width)
            for py in range(y, y + footprint_height)
        )
        if not footprint & source:
            valid.append((rank, x, y, footprint))
    while valid and len(selected) < count:
        if not selected:
            chosen = valid[0]
        else:
            centres = [
                (anchor.x + anchor.width / 2, anchor.y + anchor.height / 2) for anchor in selected
            ]
            chosen = max(
                valid,
                key=lambda candidate: (
                    min(
                        abs(candidate[1] + footprint_width / 2 - cx)
                        + abs(candidate[2] + footprint_height / 2 - cy)
                        for cx, cy in centres
                    ),
                    -candidate[0],
                ),
            )
        _rank, x, y, footprint = chosen
        if footprint & used:
            valid.remove(chosen)
            continue
        selected.append(MechanicAnchor(kind, x, y, footprint_width, footprint_height))
        used.update(footprint)
        valid = [candidate for candidate in valid if not candidate[3] & used]
    if not selected:
        raise BuildError(
            f"{kind} composition found no {footprint_width}x{footprint_height} "
            "source-safe mechanic anchor"
        )
    return tuple(selected)


def _segmented_squeeze(design: Design, source: set[Cell], palette: list[int], seed: int) -> None:
    """Build L58-like multicolour edge bands with irregular inward cuffs."""
    width, height = design.width, design.height
    depth = 2 + seed % 2
    segment = 4 + seed % 4
    for x in range(width):
        for y in range(height):
            distance = _distance_to_edge((x, y), width, height)
            cuff = distance == depth and ((x // segment + y // segment + seed) % 3 == 0)
            if distance >= depth and not cuff:
                continue
            if y < depth or y >= height - depth:
                along, side = x, 0 if y < depth else 2
            else:
                along, side = y, 1 if x >= width - depth else 3
            material = palette[(along // segment + side + distance + seed) % len(palette)]
            _paint_free(design, source, (x, y), material)


def _scenic_field(design: Design, source: set[Cell], palette: list[int], seed: int) -> None:
    """Build an L110-like full field with horizon, clouds and low-frequency motifs."""
    width, height = design.width, design.height
    horizon = max(4, height // 4 + seed % 3 - 1)
    cloud_centres = (
        (max(3, width // 5 + seed % 3), height - 6),
        (min(width - 4, 4 * width // 5 - seed % 4), height - 10),
    )
    for x in range(width):
        for y in range(height):
            if y <= horizon + ((x + seed) % 7 in {0, 1}):
                material = palette[3]
            elif any(
                abs(x - cx) <= _CLOUD_HALF_WIDTH and abs(y - cy) <= 1 for cx, cy in cloud_centres
            ):
                material = palette[2]
            elif (x // 5 + y // 6 + seed) % 7 == 0:
                material = palette[1]
            else:
                material = palette[0]
            _paint_free(design, source, (x, y), material)


def _layered_inset_frame(design: Design, source: set[Cell], palette: list[int], seed: int) -> None:
    """Build an L114-like double frame with a patterned moat between the layers."""
    width, height = design.width, design.height
    inset = 4 + seed % 2
    for x in range(width):
        for y in range(height):
            edge_distance = _distance_to_edge((x, y), width, height)
            inner_distance = min(
                abs(x - inset),
                abs(x - (width - 1 - inset)),
                abs(y - inset),
                abs(y - (height - 1 - inset)),
            )
            inside_inner_box = inset <= x <= width - 1 - inset and inset <= y <= height - 1 - inset
            if edge_distance < _EDGE_BAND:
                material = palette[edge_distance]
            elif inside_inner_box and inner_distance == 0:
                material = palette[2 + (x // 6 + y // 6 + seed) % (len(palette) - 2)]
            elif edge_distance < inset and (x // 2 + y // 2 + seed) % 2 == 0:
                material = palette[3]
            else:
                continue
            _paint_free(design, source, (x, y), material)


def _peripheral_compartments(
    design: Design, source: set[Cell], palette: list[int], seed: int
) -> None:
    """Build a quiet field with differentiated corner/side mechanic compartments."""
    width, height = design.width, design.height
    for x in range(width):
        for y in range(height):
            band = (x // 6 + y // 7 + seed) % 5 == 0
            material = palette[1] if band else palette[0]
            _paint_free(design, source, (x, y), material)
    panel_origins = ((1, 1), (width - 5, 1), (1, height - 5), (width - 5, height - 5))
    for index, (x0, y0) in enumerate(panel_origins):
        for x in range(x0, x0 + 4):
            for y in range(y0, y0 + 4):
                material = palette[2 + (index + seed) % (len(palette) - 2)]
                _paint_free(design, source, (x, y), material)


def apply(design: Design, school: str, palette: list[int], *, seed: int = 151) -> CompositionResult:
    """Apply one corpus-derived composition school without changing source pixels.

    Args:
        design: Candidate containing the approved source art.
        school: One value from :data:`SCHOOLS`.
        palette: At least four screenshot-confirmed ordinary-pixel materials.
        seed: Deterministic low-frequency variation within the school.

    Returns:
        Added occupancy and source-safe mechanic anchors.

    Raises:
        BuildError: If the school, palette or source-safe anchor geometry is invalid.
    """
    if school not in SCHOOLS:
        raise BuildError(f"unknown composition school {school!r}; expected one of {SCHOOLS}")
    if len(palette) < MIN_PALETTE_SIZE:
        raise BuildError("a composition palette needs at least four materials")
    invalid = [material for material in palette if material not in COLOUR_IDS]
    if invalid:
        raise BuildError(f"composition materials outside the palette 0-33: {invalid}")

    source_picture = dict(design.picture)
    source = set(source_picture)
    source_materials = set(source_picture.values())
    reused_palette = [material for material in palette if material in source_materials]
    treatment_palette = reused_palette if len(reused_palette) >= MIN_PALETTE_SIZE else palette
    if school == "segmented-squeeze":
        anchors = _source_safe_anchors(
            source,
            design.width,
            design.height,
            kind="egg-box",
            footprint_width=4,
            footprint_height=4,
            count=2,
            seed=seed,
        )
        _segmented_squeeze(design, source, treatment_palette, seed)
    elif school == "scenic-field":
        anchors = _source_safe_anchors(
            source,
            design.width,
            design.height,
            kind="key",
            footprint_width=2,
            footprint_height=2,
            count=4,
            seed=seed,
            prefer_interior=True,
        )
        _scenic_field(design, source, treatment_palette, seed)
    elif school == "layered-inset-frame":
        anchors = _source_safe_anchors(
            source,
            design.width,
            design.height,
            kind="pixel-pipe",
            footprint_width=3,
            footprint_height=3,
            count=2,
            seed=seed,
        )
        _layered_inset_frame(design, source, treatment_palette, seed)
    else:
        anchors = _source_safe_anchors(
            source,
            design.width,
            design.height,
            kind="egg-box",
            footprint_width=4,
            footprint_height=4,
            count=4,
            seed=seed,
        )
        _peripheral_compartments(design, source, treatment_palette, seed)

    changed = sum(design.picture.get(cell) != material for cell, material in source_picture.items())
    return CompositionResult(
        school=school,
        added_cells=len(design.picture) - len(source_picture),
        source_cells_changed=changed,
        occupied_share=len(design.picture) / (design.width * design.height),
        mechanic_anchors=anchors,
    )


__all__ = ["SCHOOLS", "CompositionResult", "MechanicAnchor", "apply"]
