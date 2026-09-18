# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Measurement and critique for generated slide-and-collect levels.

Three layers:

1. :func:`level_features` — cheap static descriptors of one level (counts,
   composition, spawn adjacency, queue shape, headroom).
2. :func:`corpus_window` — position-conditional feature bands (p25-p90 style)
   over the solvable corpus levels near a target slot, because a "level 85"
   must look like its neighbours, not like level 5.
3. :func:`critique` — the gate a candidate must pass before it may ship: hard
   invariants (structural validity + live-rules solvability via the builder's
   certificate), the mechanic introduction schedule, and windowed bands for the
   *admitted* features only.

Admission: a feature may gate only if it separates real levels from counterfeits.
The admission study (offline) compared corpus
levels vs. degraded twins (settlers rescattered, queues drained onto the board,
board inflated) — and prints per-feature false-alarm and detection rates on a
holdout split. Outcome at p10-p95 bands (2026-07-27): admitted ``headroom``
(FA 0.04 / DET 0.50), ``board_cells`` (0.08/0.33), ``n_board_settlers``
(0.00/0.15), ``adjacency_frac`` (0.08/0.18), ``queued_total`` (0.04/0.11);
everything else showed detection ~= false alarm and only warns. Bands FAIL only
when two or more admitted features breach at once (level-granularity FA 0.11 /
DET 0.49); a single breach warns. ``ADMITTED`` records this; edit it only
together with a fresh study run.

The six known-broken exports (:data:`~.sim.BROKEN_EXPORTS`) are excluded from
every corpus statistic.
"""

from __future__ import annotations

import json
import random
import statistics
from collections import Counter
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from sim import (
    CORPUS_DIR,
    Coord,
    Elevator,
    Level,
    Settler,
)

_STEPS4 = ((1, 0), (-1, 0), (0, 1), (0, -1))

#: Corpus slot at which each mechanic first appears; a candidate for slot N must
#: not use a mechanic introduced later than N.
MECHANIC_INTRODUCED = {
    "rotation": 2,
    "multi_cell": 3,
    "mirrored": 3,
    "obstacles": 5,
    "elevators": 7,
    "axis_lock": 15,
    "inner": 30,
    "connected": 50,
    "lock": 70,
}

#: Features allowed to FAIL a candidate, per the admission study (see module
#: docstring; rerun `python3 -m ... .metrics` before changing).
#: ``aspect_ratio`` was admitted on direct human-judge evidence rather than the
#: synthetic study: in the round-1 blind test the studio's designer caught every
#: landscape board we generated ("board size olarak portrait tercih ediyoruz"),
#: and the corpus is 85 portrait / 9 square / 0 landscape.
ADMITTED = (
    "headroom",
    "board_cells",
    "n_board_settlers",
    "adjacency_frac",
    "queued_total",
    "aspect_ratio",
)


def level_features(level: Level) -> dict[str, float]:
    """Static descriptors of a level, the raw material for windows and critique."""
    seat_cells: set[Coord] = set()
    for s in level.seats:
        seat_cells.update(s.cells)
    board = level.width * level.height
    free = board - len(seat_cells) - len(level.obstacles)
    board_settlers = [(pos, s) for pos, s in level.settlers]
    queued = [s for e in level.elevators for s in e.queue]

    adjacent_cells: dict[int, set[Coord]] = {}
    for s in level.seats:
        cells = set(s.cells)
        ring = {(cx + dx, cy + dy) for cx, cy in cells for dx, dy in _STEPS4} - cells
        adjacent_cells.setdefault(s.color, set()).update(ring)
    n_adjacent = sum(1 for pos, s in board_settlers if pos in adjacent_cells.get(s.color, set()))

    colors = {s.color for s in level.seats}
    colors |= {s.inner_color for s in level.seats if s.inner_color is not None}
    sizes = [len(s.cells) for s in level.seats]

    return {
        "board_cells": float(board),
        "aspect_ratio": level.height / level.width,
        "n_seats": float(len(level.seats)),
        "n_settlers": float(len(board_settlers) + len(queued)),
        "n_board_settlers": float(len(board_settlers)),
        "queued_total": float(len(queued)),
        "n_colors": float(len(colors)),
        "n_obstacles": float(len(level.obstacles)),
        "n_elevators": float(len(level.elevators)),
        "max_queue": float(max((len(e.queue) for e in level.elevators), default=0)),
        "headroom": float(free - len(board_settlers)),
        "adjacency_frac": (n_adjacent / len(board_settlers)) if board_settlers else 0.0,
        "mean_seat_size": statistics.fmean(sizes) if sizes else 0.0,
        "n_inner": float(sum(s.inner_color is not None for s in level.seats)),
        "n_connected": float(
            len({s.connected_id for s in level.seats if s.connected_id is not None})
        ),
        "n_axis_locks": float(sum(s.axis_lock != 0 for s in level.seats)),
        "n_locks": float(sum(s.lock_id is not None for s in level.seats)),
        "n_far_seats": float(_far_served_seats(level)),
        "n_mechanic_types": float(_mechanic_types(level)),
        "n_side_elevators": float(sum(1 for e in level.elevators if e.offset[0] != 0)),
        "n_central_obstacles": float(_central_obstacles(level)),
    }


def _central_obstacles(level: Level) -> int:
    """Obstacles in the middle half of the board; the studio keeps blockers
    near edges (13/94 corpus levels have any central one — a round-2 tell).
    """
    cx, cy = (level.width - 1) / 2, (level.height - 1) / 2
    return sum(
        1
        for (x, y) in level.obstacles
        if abs(x - cx) <= level.width * 0.25 and abs(y - cy) <= level.height * 0.25
    )


def _far_served_seats(level: Level) -> int:
    """Seats whose same-color settlers sit far away (the studio's
    opposite-corner strategy; used increasingly in later levels).
    """
    per_color: dict[int, list[Coord]] = {}
    for pos, s in level.settlers:
        per_color.setdefault(s.color, []).append(pos)
    norm = level.width + level.height
    far = 0
    for seat in level.seats:
        pts = per_color.get(seat.color, [])
        if not pts:
            continue
        cx = sum(c[0] for c in seat.cells) / len(seat.cells)
        cy = sum(c[1] for c in seat.cells) / len(seat.cells)
        mean_d = sum(abs(px - cx) + abs(py - cy) for px, py in pts) / len(pts)
        if mean_d > norm * 0.45:
            far += 1
    return far


def _mechanic_types(level: Level) -> int:
    """Distinct extra mechanics in play; the studio caps this at three per
    level, and elevators deliberately do not count (they call them a
    production helper, not a mechanic).
    """
    return sum(
        [
            any(s.axis_lock for s in level.seats),
            any(s.inner_color is not None for s in level.seats),
            any(s.connected_id is not None for s in level.seats),
            any(s.lock_id is not None for s in level.seats),
            bool(level.obstacles),
        ]
    )


@cache
def _corpus_features() -> tuple[tuple[int, tuple[tuple[str, float], ...]], ...]:
    """Per-slot feature rows of the solvable reference levels (precomputed aggregates)."""
    table = json.loads(
        (Path(__file__).resolve().parent / "data" / "reference-features.json").read_text()
    )
    return tuple((int(row["slot"]), tuple(row["features"].items())) for row in table["levels"])


def corpus_window(
    slot: int, *, span: int = 15, lo_q: float = 0.10, hi_q: float = 0.95
) -> dict[str, tuple[float, float]]:
    """Per-feature (lo, hi) bands over solvable corpus levels within ``span`` slots.

    The window widens automatically (up to the whole corpus) until it holds at
    least eight levels, so edge slots still get meaningful bands.
    """
    rows: list[dict[str, float]] = []
    width = span
    while True:
        rows = [dict(feats) for s, feats in _corpus_features() if abs(s - slot) <= width]
        if len(rows) >= 8 or width > 100:
            break
        width += 5
    bands: dict[str, tuple[float, float]] = {}
    for key in rows[0]:
        values = sorted(r[key] for r in rows)
        lo = values[int(lo_q * (len(values) - 1))]
        hi = values[int(hi_q * (len(values) - 1))]
        bands[key] = (lo, hi)
    return bands


@dataclass(frozen=True)
class Finding:
    """One critique line."""

    severity: str  # "FAIL" | "WARN" | "INFO"
    check: str
    detail: str


def _schedule_findings(feats: dict[str, float], level: Level, slot: int) -> list[Finding]:
    used = {
        "rotation": any(s.rotation != 0 for s in level.seats),
        "multi_cell": any(len(s.cells) >= 4 for s in level.seats),
        "mirrored": any(s.mirrored for s in level.seats),
        "obstacles": feats["n_obstacles"] > 0,
        "elevators": feats["n_elevators"] > 0,
        "axis_lock": feats["n_axis_locks"] > 0,
        "inner": feats["n_inner"] > 0,
        "connected": feats["n_connected"] > 0,
        "lock": feats["n_locks"] > 0,
    }
    out = []
    for mechanic, introduced in MECHANIC_INTRODUCED.items():
        if used[mechanic] and slot < introduced:
            out.append(
                Finding(
                    "FAIL",
                    f"schedule_{mechanic}",
                    f"{mechanic} first appears at corpus level {introduced}, "
                    f"but this candidate targets slot {slot}",
                )
            )
    return out


def critique(
    level: Level,
    slot: int,
    *,
    certificate_ok: bool | None = None,
    span: int = 15,
) -> list[Finding]:
    """Judge a candidate level for a target slot.

    Args:
        level: The candidate (typically ``Design.to_level()``).
        slot: Corpus position the level pretends to occupy.
        certificate_ok: Result of ``Design.certify().ok`` if already computed;
            pass ``None`` to skip the solvability line (the caller must gate on
            it elsewhere).
        span: Window half-width in slots.

    Returns:
        FAIL/WARN/INFO findings; a shippable level has zero FAILs.
    """
    feats = level_features(level)
    findings: list[Finding] = []
    if certificate_ok is False:
        findings.append(Finding("FAIL", "solvable", "certificate failed under live rules"))
    findings.extend(_schedule_findings(feats, level, slot))
    if level.width > level.height:
        findings.append(
            Finding(
                "FAIL",
                "landscape_board",
                f"{level.width}x{level.height} is landscape; the corpus ships 85 "
                "portrait and 9 square boards, zero landscape (round-1 blind-test "
                "tell)",
            )
        )
    if feats["n_mechanic_types"] > 3:
        findings.append(
            Finding(
                "FAIL",
                "mechanic_budget",
                f"{feats['n_mechanic_types']:g} extra mechanic types; the studio "
                "caps levels at three (elevators excluded), corpus max is 3",
            )
        )
    for i, seat in enumerate(level.seats):
        if not seat.axis_lock:
            continue
        if sum(1 for t in level.seats if t.color == seat.color) > 1:
            continue  # another seat of the color can serve off-corridor settlers
        cols = {c[0] for c in seat.cells}
        rows = {c[1] for c in seat.cells}
        off = [
            pos
            for pos, st in level.settlers
            if st.color == seat.color
            and not ((pos[0] in cols) if seat.axis_lock == 1 else (pos[1] in rows))
        ]
        if off:
            findings.append(
                Finding(
                    "FAIL",
                    f"axis_corridor_seat_{i}",
                    f"axis-locked seat {i} can never reach its settlers at {off} "
                    "(outside its movement corridor); the studio places an "
                    "axis-locked hole's characters along its arrow, corpus 0/94 "
                    "violations",
                )
            )
    # Deliberately STRICTER than the shipped corpus: 7/94 corpus levels reuse a
    # layered hole's colors, but the studio's current rule (July 29 call) bans
    # it outright as their dead-end fix. Running corpus levels through
    # critique() as a regression check will therefore flag those seven.
    for i, seat in enumerate(level.seats):
        if seat.inner_color is None:
            continue
        reused = sorted(
            {
                c
                for t in level.seats
                if t is not seat
                for c in (t.color, t.inner_color)
                if c is not None and c in (seat.color, seat.inner_color)
            }
        )
        if reused:
            findings.append(
                Finding(
                    "FAIL",
                    f"layered_color_reuse_seat_{i}",
                    f"layered seat {i} shares color(s) {reused} with other holes; "
                    "the studio reserves a layered hole's colors for it alone "
                    "(their own dead-end fix, July 29 call: 'eger layer'liysa o "
                    "renkte ayni rengi kullanma board'da'; 87/94 corpus levels "
                    "comply)",
                )
            )
    for e in level.elevators:
        if not e.queue:
            findings.append(
                Finding(
                    "FAIL",
                    "empty_elevator",
                    f"elevator at {e.cell} ships with an empty queue - in the "
                    "live game it vanishes at spawn (play-test round 3, level "
                    "05); author elevators only with characters to deliver",
                )
            )
            continue
        front = e.queue[0]
        for s2 in level.seats:
            if s2.color != front.color or s2.lock_id is not None:
                continue
            cells = set(s2.cells)
            if e.cell in cells or any(
                (e.cell[0] + dx, e.cell[1] + dy) in cells
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
            ):
                findings.append(
                    Finding(
                        "WARN",
                        "spawn_mouth_jump",
                        f"a {front.color}-colored hole is parked at the elevator "
                        f"mouth {e.cell} at spawn - the queue fires instantly; "
                        "the studio designs against it (July 29 call: 'ilk kisi "
                        "kirmizi olmasin cunku direkt atliyor') though 15/94 "
                        "corpus levels still do it",
                    )
                )
                break
    inner_singles = sum(1 for s in level.seats if s.inner_color is not None and len(s.cells) == 1)
    if inner_singles:
        findings.append(
            Finding(
                "WARN",
                "inner_on_single",
                f"{inner_singles} hidden layer(s) on 1-cell seats; the corpus puts "
                "28 of 30 inner layers on multi-cell seats (18 on dominoes)",
            )
        )
    bands = corpus_window(slot, span=span)
    breached = [key for key in ADMITTED if not bands[key][0] <= feats[key] <= bands[key][1]]
    # One breached band is within real levels' natural variation; two or more is
    # the counterfeit signature (level-granularity FA 0.11 / DET 0.49).
    band_severity = "FAIL" if len(breached) >= 2 else "WARN"
    for key in breached:
        lo, hi = bands[key]
        findings.append(
            Finding(
                band_severity,
                f"band_{key}",
                f"{key}={feats[key]:g} outside corpus window [{lo:g}, {hi:g}] "
                f"for slots {slot}±{span}",
            )
        )
    for key, value in feats.items():
        if key not in ADMITTED:
            lo, hi = bands[key]
            if not lo <= value <= hi:
                findings.append(
                    Finding(
                        "WARN",
                        f"band_{key}",
                        f"{key}={value:g} outside [{lo:g}, {hi:g}]",
                    )
                )
    findings.append(
        Finding(
            "INFO",
            "features",
            ", ".join(f"{k}={v:g}" for k, v in sorted(feats.items())),
        )
    )
    return findings


# --------------------------------------------------------------------- admission study


def _scatter_settlers(level: Level, rng: random.Random) -> Level:
    """Counterfeit: same everything, board settlers rehoused uniformly at random."""
    seat_cells = {c for s in level.seats for c in s.cells}
    free = [
        (x, y)
        for x in range(level.width)
        for y in range(level.height)
        if (x, y) not in seat_cells and (x, y) not in level.obstacles
    ]
    cells = rng.sample(free, len(level.settlers))
    settlers = tuple(
        (cell, Settler(color=s.color, key_id=s.key_id))
        for cell, (_, s) in zip(cells, level.settlers, strict=True)
    )
    return Level(
        width=level.width,
        height=level.height,
        seats=level.seats,
        settlers=settlers,
        obstacles=level.obstacles,
        elevators=level.elevators,
        name=level.name + "+scatter",
    )


def _drain_queues(level: Level, rng: random.Random) -> Level:
    """Counterfeit: elevator settlers dumped onto the board, queues left empty."""
    seat_cells = {c for s in level.seats for c in s.cells}
    taken = set(seat_cells) | set(level.obstacles) | {p for p, _ in level.settlers}
    free = [(x, y) for x in range(level.width) for y in range(level.height) if (x, y) not in taken]
    queued = [s for e in level.elevators for s in e.queue]
    if len(queued) > len(free):
        queued = queued[: len(free)]
    cells = rng.sample(free, len(queued))
    settlers = level.settlers + tuple(zip(cells, queued, strict=True))
    elevators = tuple(Elevator(cell=e.cell, offset=e.offset, queue=()) for e in level.elevators)
    return Level(
        width=level.width,
        height=level.height,
        seats=level.seats,
        settlers=settlers,
        obstacles=level.obstacles,
        elevators=elevators,
        name=level.name + "+drain",
    )


def _inflate_board(level: Level, rng: random.Random) -> Level:
    """Counterfeit: same content on a board two cells wider and taller."""
    return Level(
        width=level.width + 2,
        height=level.height + 2,
        seats=level.seats,
        settlers=level.settlers,
        obstacles=level.obstacles,
        elevators=level.elevators,
        name=level.name + "+inflate",
    )


DEGRADATIONS = {
    "scatter_settlers": _scatter_settlers,
    "drain_queues": _drain_queues,
    "inflate_board": _inflate_board,
}


def run_study(seed: int = 7) -> dict[str, dict[str, float]]:
    """False-alarm vs. detection per feature; the basis for :data:`ADMITTED`.

    Real corpus levels are split into calibration (windows come from these via
    :func:`corpus_window`, which uses the full corpus — good enough since
    windows are positional, not global) and holdout. A feature's false-alarm
    rate is how often a *real* holdout level falls outside its own slot's band;
    its detection rate is how often a counterfeit does.

    Returns:
        ``{feature: {"fa": ..., "det": ...}}``.
    """
    rng = random.Random(seed)
    rows = [(slot, dict(feats)) for slot, feats in _corpus_features()]
    holdout = rows[1::4]

    features = list(rows[0][1])
    fa = Counter[str]()
    det = Counter[str]()
    n_fake = 0
    for slot, feats in holdout:
        bands = corpus_window(slot)
        for key in features:
            lo, hi = bands[key]
            if not lo <= feats[key] <= hi:
                fa[key] += 1
    for slot, _ in holdout:
        level = Level.load(CORPUS_DIR / f"Level {slot}.json")
        for degrade in DEGRADATIONS.values():
            fake = degrade(level, rng)
            fake_feats = level_features(fake)
            bands = corpus_window(slot)
            n_fake += 1
            for key in features:
                lo, hi = bands[key]
                if not lo <= fake_feats[key] <= hi:
                    det[key] += 1

    return {key: {"fa": fa[key] / len(holdout), "det": det[key] / n_fake} for key in features}


def main() -> int:
    """Print the admission study table."""
    results = run_study()
    print(f"{'feature':<20} {'false-alarm':>12} {'detection':>10}")
    for key, r in sorted(results.items(), key=lambda kv: -kv[1]["det"]):
        marker = " *" if key in ADMITTED else ""
        print(f"{key:<20} {r['fa']:>12.2f} {r['det']:>10.2f}{marker}")
    print("\n* currently admitted to gate (ADMITTED)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
