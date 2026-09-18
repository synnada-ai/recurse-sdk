# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic construction kit for slide-and-collect levels.

The agent composes levels from these primitives; everything here is pure Python with
no LLM involvement. The contract with :mod:`.sim`:

- A :class:`Design` is mutable authoring state (seats, settlers, obstacles,
  elevator queues) that converts to an immutable :class:`~.sim.Level`.
- Color conservation is *by construction*: settlers are only addable while a color
  deficit remains, and :meth:`Design.auto_populate` fills exactly the remaining
  deficit, so a sealed design always satisfies the corpus-exact invariant.
- Solvability is *by certification*, not construction: :meth:`Design.certify` runs
  the structural validator plus :func:`~.sim.solve_beam` under the live rules
  (fifo queues, all-keys locks) — that gates shipping — and additionally records
  whether the level survives reverse queue order (``Certificate.order_robust``)
  as engine-uncertainty insurance. Nothing ships uncertified.
- :func:`to_game_json` emits the game's on-disk schema, byte-faithful to the
  corpus generation appropriate for the target slot (V1's plain vectors, string
  enums and null configs; V2's Unity float32 ``magnitude``/``sqrMagnitude``
  fields, ``normalized`` sub-objects and ``ShapeGridData``), including the
  editor's harmless quirks (zero ``LocalScale``, occasional raw rotations > 3,
  per-level camera-z scatter sampled from corpus neighbours).
"""

from __future__ import annotations

import json
import math
import random
import struct
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sim import (
    AXIS_NONE,
    CORPUS_DIR,
    SHAPES,
    Coord,
    Elevator,
    Level,
    Rules,
    Seat,
    Settler,
    SolveResult,
    seat_cells,
    solve_beam,
    validate,
)

_STEPS4 = ((1, 0), (-1, 0), (0, 1), (0, -1))


@dataclass
class SeatSpec:
    """Mutable authoring twin of :class:`~.sim.Seat`."""

    pos: Coord
    color: int
    shape_id: int
    rotation: int = 0
    mirrored: bool = False
    inner_color: int | None = None
    axis_lock: int = AXIS_NONE
    connected_id: int | None = None
    lock_id: int | None = None

    @property
    def cells(self) -> tuple[Coord, ...]:
        """World footprint at the authored position."""
        return seat_cells(self.shape_id, self.rotation, self.mirrored, self.pos)

    def to_seat(self) -> Seat:
        """Freeze into the simulator's seat type."""
        return Seat(
            pos=self.pos,
            color=self.color,
            shape_id=self.shape_id,
            rotation=self.rotation,
            mirrored=self.mirrored,
            inner_color=self.inner_color,
            axis_lock=self.axis_lock,
            connected_id=self.connected_id,
            lock_id=self.lock_id,
        )


@dataclass
class ElevatorSpec:
    """Mutable authoring twin of :class:`~.sim.Elevator`."""

    cell: Coord
    offset: Coord
    queue: list[Settler] = field(default_factory=list)


@dataclass(frozen=True)
class Certificate:
    """Outcome of :meth:`Design.certify`."""

    errors: tuple[str, ...]
    fifo: SolveResult
    lifo: SolveResult
    escalated: bool = False
    """True when the fifo verdict needed the wide-beam retry (dense board)."""

    @property
    def ok(self) -> bool:
        """Structurally valid and solvable under the live rules (fifo queues)."""
        return not self.errors and self.fifo.solved

    @property
    def order_robust(self) -> bool:
        """Also solvable if queues released in reverse — insurance, not a gate."""
        return self.ok and self.lifo.solved


class Design:
    """Mutable level under construction.

    Args:
        width: Board width in cells (positive).
        height: Board height in cells (positive).

    Raises:
        ValueError: If the board dimensions are not positive.
    """

    def __init__(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise ValueError(f"board must be positive, got {width}x{height}")
        self.width = width
        self.height = height
        self.obstacles: set[Coord] = set()
        self.seats: list[SeatSpec] = []
        self.settlers: list[tuple[Coord, Settler]] = []
        self.elevators: list[ElevatorSpec] = []

    # ------------------------------------------------------------------ queries

    def in_bounds(self, c: Coord) -> bool:
        """Whether a cell lies on the board."""
        return 0 <= c[0] < self.width and 0 <= c[1] < self.height

    def seat_cell_map(self) -> dict[Coord, int]:
        """Cell -> seat index for every covered cell."""
        out: dict[Coord, int] = {}
        for i, s in enumerate(self.seats):
            for c in s.cells:
                out[c] = i
        return out

    def free_cells(self) -> set[Coord]:
        """Cells not covered by seats, obstacles or settlers."""
        taken = set(self.seat_cell_map()) | self.obstacles
        taken |= {pos for pos, _ in self.settlers}
        return {
            (x, y) for x in range(self.width) for y in range(self.height) if (x, y) not in taken
        }

    def capacity(self) -> Counter[int]:
        """Required settlers per color (inner layers count once more)."""
        cap: Counter[int] = Counter()
        for s in self.seats:
            cap[s.color] += len(s.cells)
            if s.inner_color is not None:
                cap[s.inner_color] += len(s.cells)
        return cap

    def supply(self) -> Counter[int]:
        """Settlers placed so far per color (board plus queues)."""
        sup: Counter[int] = Counter(s.color for _, s in self.settlers)
        sup.update(s.color for e in self.elevators for s in e.queue)
        return sup

    def deficit(self) -> Counter[int]:
        """Settlers still missing per color; empty when conservation holds."""
        d = self.capacity()
        d.subtract(self.supply())
        return +d

    # ------------------------------------------------------------------ authoring

    def place_seat(
        self,
        shape_id: int,
        pos: Coord,
        color: int,
        *,
        rotation: int = 0,
        mirrored: bool = False,
    ) -> int:
        """Place a seat; returns its index.

        Raises:
            ValueError: If the shape id is unknown, or any footprint cell is out
                of bounds, on an obstacle, or overlaps another seat — the message
                names the offending cell.
        """
        if shape_id not in SHAPES:
            raise ValueError(f"unknown shape id {shape_id}; valid ids are 0-7")
        spec = SeatSpec(
            pos=pos,
            color=color,
            shape_id=shape_id,
            rotation=rotation % 4,
            mirrored=mirrored,
        )
        covered = self.seat_cell_map()
        occupied = {p for p, _ in self.settlers}
        for c in spec.cells:
            if not self.in_bounds(c):
                raise ValueError(f"seat cell {c} is outside the {self.width}x{self.height} board")
            if c in self.obstacles:
                raise ValueError(f"seat cell {c} sits on an obstacle")
            if c in covered:
                raise ValueError(f"seat cell {c} overlaps seat {covered[c]}")
            if c in occupied:
                raise ValueError(
                    f"seat cell {c} sits on a settler - clear_settlers() first, "
                    "then re-add seats and populate() again"
                )
        self.seats.append(spec)
        return len(self.seats) - 1

    def add_obstacle(self, cell: Coord) -> None:
        """Block a cell permanently.

        Raises:
            ValueError: If the cell is out of bounds or already used.
        """
        if not self.in_bounds(cell):
            raise ValueError(f"obstacle {cell} is outside the board")
        if cell in self.seat_cell_map():
            raise ValueError(f"obstacle {cell} would sit under a seat")
        if any(pos == cell for pos, _ in self.settlers):
            raise ValueError(f"obstacle {cell} would sit under a settler")
        if any(e.cell == cell for e in self.elevators):
            raise ValueError(f"obstacle {cell} would sit on an elevator cell")
        self.obstacles.add(cell)

    def set_inner(self, seat_index: int, color: int) -> None:
        """Give a seat a second (inner) layer of a different color.

        Raises:
            ValueError: If the color equals the outer color.
        """
        seat = self.seats[seat_index]
        if color == seat.color:
            raise ValueError(f"inner color must differ from outer color {seat.color}")
        seat.inner_color = color

    def set_axis_lock(self, seat_index: int, axis: int) -> None:
        """Restrict a seat's movement axis (1 = vertical, 2 = horizontal)."""
        if axis not in (1, 2):
            raise ValueError(f"axis must be 1 (vertical) or 2 (horizontal), got {axis}")
        self.seats[seat_index].axis_lock = axis

    def connect(self, seat_a: int, seat_b: int) -> None:
        """Weld two adjacent seats into a rigid pair.

        Raises:
            ValueError: If the seats are not orthogonally adjacent, share a
                color, or either is already connected.
        """
        a, b = self.seats[seat_a], self.seats[seat_b]
        if a.connected_id is not None or b.connected_id is not None:
            raise ValueError("one of the seats is already connected")
        if a.color == b.color:
            raise ValueError("connected seats always have different colors in the corpus")
        adjacent = any(
            abs(ca[0] - cb[0]) + abs(ca[1] - cb[1]) == 1 for ca in a.cells for cb in b.cells
        )
        if not adjacent:
            raise ValueError("connected seats must have orthogonally adjacent footprints")
        next_id = max((s.connected_id or 0 for s in self.seats), default=0) + 1
        a.connected_id = next_id
        b.connected_id = next_id

    def set_lock(self, seat_index: int, lock_id: int) -> None:
        """Chain a seat; it stays inert until all matching keys are collected."""
        self.seats[seat_index].lock_id = lock_id

    def add_settler(self, pos: Coord, color: int, *, key_id: int | None = None) -> None:
        """Stand a settler on a free cell.

        Raises:
            ValueError: If the cell is unusable or the color has no remaining
                deficit (conservation would break).
        """
        if pos not in self.free_cells():
            raise ValueError(f"cell {pos} is not free")
        if self.deficit()[color] <= 0:
            raise ValueError(
                f"color {color} needs no more settlers (capacity already met); "
                f"current deficit: {dict(self.deficit())}"
            )
        self.settlers.append((pos, Settler(color=color, key_id=key_id)))

    def assign_keys(self, lock_id: int, count: int = 1, color: int | None = None) -> int:
        """Turn already-placed settlers into key carriers for a chained seat.

        Corpus convention: keys ride ordinary settlers of a color that belongs
        to an *unchained* seat, so they stay collectable while the lock holds.

        Args:
            lock_id: Lock family id; must match a chained seat.
            count: How many keys to assign.
            color: Restrict carriers to this color; default picks any color not
                used by a chained seat.

        Returns:
            Number of keys assigned.

        Raises:
            ValueError: If no seat carries this lock or there are not enough
                eligible settlers (place/populate settlers first).
        """
        if not any(s.lock_id == lock_id for s in self.seats):
            raise ValueError(f"no chained seat has lock id {lock_id} - call set_lock first")
        locked_colors = {s.color for s in self.seats if s.lock_id is not None}
        assigned = 0
        for i, (pos, settler) in enumerate(self.settlers):
            if assigned >= count:
                break
            if settler.key_id is not None or settler.color in locked_colors:
                continue
            if color is not None and settler.color != color:
                continue
            self.settlers[i] = (pos, Settler(color=settler.color, key_id=lock_id))
            assigned += 1
        for e in self.elevators:
            for j, settler in enumerate(e.queue):
                if assigned >= count:
                    break
                if settler.key_id is not None or settler.color in locked_colors:
                    continue
                if color is not None and settler.color != color:
                    continue
                e.queue[j] = Settler(color=settler.color, key_id=lock_id)
                assigned += 1
        if assigned < count:
            raise ValueError(
                f"only {assigned}/{count} eligible settlers to carry keys - "
                "populate() first or drop the color restriction"
            )
        return assigned

    def add_elevator(self, cell: Coord, offset: Coord) -> int:
        """Add an elevator feeding ``cell`` from direction ``offset``; returns its index.

        Raises:
            ValueError: If the cell is out of bounds, duplicated, or on an obstacle.
        """
        if not self.in_bounds(cell):
            raise ValueError(f"elevator cell {cell} is outside the board")
        if any(e.cell == cell for e in self.elevators):
            raise ValueError(f"an elevator already feeds {cell}")
        if cell in self.obstacles:
            raise ValueError(f"elevator cell {cell} is an obstacle")
        self.elevators.append(ElevatorSpec(cell=cell, offset=offset))
        return len(self.elevators) - 1

    def push_queue(self, elevator_index: int, color: int, *, key_id: int | None = None) -> None:
        """Append a settler to an elevator queue (released in this order).

        Raises:
            ValueError: If the color has no remaining deficit.
        """
        if self.deficit()[color] <= 0:
            raise ValueError(
                f"color {color} needs no more settlers (capacity already met); "
                f"current deficit: {dict(self.deficit())}"
            )
        self.elevators[elevator_index].queue.append(Settler(color=color, key_id=key_id))

    def auto_populate(
        self,
        *,
        seed: int = 0,
        queue_fraction: float = 0.0,
        adjacency_fraction: float = 0.4,
        headroom: int | None = None,
    ) -> None:
        """Fill the remaining color deficit with settlers.

        Board settlers are scattered over free cells, aiming for
        ``adjacency_fraction`` of them to start orthogonally adjacent to a
        same-colored seat (corpus-wide this is ~41%). When elevators exist,
        ``queue_fraction`` of the deficit is dealt into their queues round-robin
        in a shuffled color order. Certification decides whether the result
        plays well; re-roll ``seed`` if it does not.

        Args:
            seed: RNG seed; same seed, same population.
            queue_fraction: Share of the deficit routed into elevator queues.
            adjacency_fraction: Target share of board settlers adjacent to a
                matching seat.
            headroom: Required number of free cells left after population;
                raises if the board cannot honor it.

        Raises:
            ValueError: If there is no deficit to place, not enough free cells,
                or the headroom target cannot be met.
        """
        rng = random.Random(seed)
        deficit = self.deficit()
        pool = [c for c in deficit.elements()]
        if not pool:
            raise ValueError("nothing to place: capacity is already met")
        rng.shuffle(pool)

        n_queue = round(len(pool) * queue_fraction) if self.elevators else 0
        queue_pool, board_pool = pool[:n_queue], pool[n_queue:]
        # Colors whose capacity is immediately available (an unlocked outer
        # layer) may sit anywhere in a queue; inner-layer or locked-only colors
        # must come after their enablers, so push them to the back. This avoids
        # authoring the level-31 deadlock pattern by accident.
        exposed = {s.color for s in self.seats if s.lock_id is None}
        queue_pool.sort(key=lambda color: color not in exposed)
        for i, color in enumerate(queue_pool):
            self.elevators[i % len(self.elevators)].queue.append(Settler(color=color))

        free = self.free_cells()
        if len(board_pool) > len(free):
            raise ValueError(f"{len(board_pool)} settlers left but only {len(free)} free cells")
        if headroom is not None and len(free) - len(board_pool) < headroom:
            raise ValueError(
                f"cannot keep headroom {headroom}: {len(free)} free cells minus "
                f"{len(board_pool)} settlers leaves {len(free) - len(board_pool)}"
            )

        adjacent_of: dict[int, set[Coord]] = {}
        for s in self.seats:
            cells = set(s.cells)
            ring = {(cx + dx, cy + dy) for cx, cy in cells for dx, dy in _STEPS4} - cells
            adjacent_of.setdefault(s.color, set()).update(ring & free)

        n_adjacent = round(len(board_pool) * adjacency_fraction)
        placed_adjacent = 0
        for color in board_pool:
            near = list((adjacent_of.get(color, set())) & free)
            if placed_adjacent < n_adjacent and near:
                cell = rng.choice(near)
                placed_adjacent += 1
            else:
                far = list(free - adjacent_of.get(color, set()))
                cell = rng.choice(far or list(free))
            self.settlers.append((cell, Settler(color=color)))
            free.discard(cell)

    # ------------------------------------------------------------------ sealing

    def to_level(self, name: str = "") -> Level:
        """Freeze into an immutable simulator level."""
        return Level(
            width=self.width,
            height=self.height,
            seats=tuple(s.to_seat() for s in self.seats),
            settlers=tuple(self.settlers),
            obstacles=frozenset(self.obstacles),
            elevators=tuple(
                Elevator(cell=e.cell, offset=e.offset, queue=tuple(e.queue)) for e in self.elevators
            ),
            name=name,
        )

    def certify(
        self,
        *,
        width: int = 160,
        max_taps: int = 180,
        escalate_width: int = 640,
        escalate_taps: int = 260,
    ) -> Certificate:
        """Structural validation plus solvability under both queue orders.

        The beam is a semi-decision procedure: a fail at survey width is often
        a false rejection on dense, corpus-typical boards (13/94 shipped levels
        resist the survey beam). A fifo fail therefore retries once at
        ``escalate_width`` before the design is declared unsolvable - pure CPU,
        and it stops the gate from steering the agent toward plain, spacious,
        mechanic-free boards (2026-07-31 A/B finding). Pass
        ``escalate_width=0`` to disable the retry.

        Soft spots, accepted: an escalated-and-still-failing fifo keeps
        the wide result (honest failure, one extra solve); worst case is
        a single 4x-width beam inside the tool-timeout budget — dense
        boards measure well under it, but the retry is the first suspect
        if a certify tool call ever times out.
        """
        level = self.to_level("candidate")
        errors = tuple(validate(level))
        if errors:
            failed = SolveResult(False, 0, 0, 0)
            return Certificate(errors=errors, fifo=failed, lifo=failed)
        # Gate on ENGINE physics as established by the studio's play test
        # (2026-07-29): on the board a hole collects ONLY by sliding under its
        # characters (cover); adjacency collection exists solely at elevator
        # mouths, which also fire at spawn. Board-adjacency collection made six
        # certified levels unplayable in-engine - never again.
        strict = Rules(
            teleport=False,
            figures_solid=False,
            absorb_cover=True,
            absorb_adjacent=False,
            elevator_order="fifo",
        )
        strict_lifo = Rules(
            teleport=False,
            figures_solid=False,
            absorb_cover=True,
            absorb_adjacent=False,
            elevator_order="lifo",
        )
        fifo = solve_beam(
            level,
            width=width,
            max_taps=max_taps,
            branch=16,
            park_branch=8,
            rules=strict,
        )
        escalated = False
        if not fifo.solved and escalate_width > width:
            escalated = True
            fifo = solve_beam(
                level,
                width=escalate_width,
                max_taps=escalate_taps,
                branch=20,
                park_branch=12,
                rules=strict,
            )
        lifo = solve_beam(
            level,
            width=width,
            max_taps=max_taps,
            branch=16,
            park_branch=8,
            rules=strict_lifo,
        )
        return Certificate(errors=(), fifo=fifo, lifo=lifo, escalated=escalated)

    def render_ascii(self) -> str:
        """Human/LLM-readable board picture (digits = seat colors, letters = settlers)."""
        cell_map = self.seat_cell_map()
        figures = dict(self.settlers)
        rows = []
        for y in range(self.height - 1, -1, -1):
            row = ""
            for x in range(self.width):
                c = (x, y)
                if c in self.obstacles:
                    row += "#"
                elif c in cell_map:
                    row += str(self.seats[cell_map[c]].color)
                elif c in figures:
                    row += "abcdefghij"[figures[c].color]
                else:
                    row += "."
            rows.append(f"{y:2} {row}")
        rows.append("   " + "".join(str(x % 10) for x in range(self.width)))
        for i, e in enumerate(self.elevators):
            rows.append(f"elevator {i} at {e.cell} queue {[s.color for s in e.queue]}")
        return "\n".join(rows)


# --------------------------------------------------------------------- JSON emitter


def _f32(v: float) -> float:
    """Round to the nearest float32 (Unity's serialization precision)."""
    out: float = struct.unpack("f", struct.pack("f", v))[0]
    return out


def _f32_repr(v: float) -> float:
    """Float32 as Unity/.NET's ``"R"`` format prints it: G7, or G9 when G7 loses bits.

    Matches the corpus byte-for-byte (e.g. sqrt(130) -> 11.4017544, sqrt(65) ->
    8.062258, 1/sqrt(3) -> 0.577350259).
    """
    f = _f32(v)
    for digits in (7, 9):
        cand = float(f"{f:.{digits}g}")
        if _f32(cand) == f:
            return cand
    return f


def _vec2int(x: int, y: int, *, v2: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"x": x, "y": y}
    if v2:
        out["magnitude"] = _f32_repr(math.sqrt(x * x + y * y))
        out["sqrMagnitude"] = x * x + y * y
    return out


def _vec3f(x: float, y: float, z: float, *, v2: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"x": x, "y": y, "z": z}
    if v2:
        out["magnitude"] = _f32_repr(math.sqrt(x * x + y * y + z * z))
        out["sqrMagnitude"] = _f32_repr(x * x + y * y + z * z)
    return out


_AXIS_TO_NAME = {0: "None", 1: "Vertical", 2: "Horizontal"}

_UNIT_SCALE = {
    "x": 1.0,
    "y": 1.0,
    "z": 1.0,
    "normalized": {
        "x": _f32_repr(1 / math.sqrt(3)),
        "y": _f32_repr(1 / math.sqrt(3)),
        "z": _f32_repr(1 / math.sqrt(3)),
        "magnitude": 1.0,
        "sqrMagnitude": _f32_repr(3 * _f32(1 / math.sqrt(3)) ** 2),
    },
    "magnitude": _f32_repr(math.sqrt(3)),
    "sqrMagnitude": 3.0,
}


def _lockcfg(type_: int, color_id: int, *, v2: bool) -> dict[str, Any] | None:
    if not v2 and type_ == 0:
        return None
    return {"Type": type_, "ColorId": color_id}


def _settler_json(settler: Settler, pos: Coord, *, v2: bool) -> dict[str, Any]:
    lock = _lockcfg(2 if settler.key_id is not None else 0, settler.key_id or -1, v2=v2)
    if v2:
        return {
            "BombTimer": 0,
            "GridPos": _vec2int(*pos, v2=True),
            "ColorIndex": settler.color,
            "HammerColorIndex": -1,
            "ScissorsColorIndex": -1,
            "LockNKeyConfig": lock,
        }
    return {
        "GridPos": _vec2int(*pos, v2=False),
        "ColorIndex": settler.color,
        "LockNKeyConfig": lock,
    }


def _seat_json(spec: SeatSpec, rotation_raw: int, *, v2: bool) -> dict[str, Any]:
    lock = _lockcfg(1 if spec.lock_id is not None else 0, spec.lock_id or -1, v2=v2)
    out: dict[str, Any] = {
        "GridPos": _vec2int(*spec.pos, v2=v2),
        "ColorIndex": spec.color,
        "InnerBrickColorIndex": (
            spec.inner_color if spec.inner_color is not None else (-1 if v2 else None)
        ),
        "SeatID": spec.shape_id,
        "Rotation": rotation_raw,
        "AxisLockType": spec.axis_lock if v2 else _AXIS_TO_NAME[spec.axis_lock],
        "IsMirrored": spec.mirrored,
        "ConnectedBrickID": (
            spec.connected_id if spec.connected_id is not None else (-1 if v2 else 0)
        ),
        "LockNKeyConfig": lock,
        "LocalScale": dict(_UNIT_SCALE) if v2 else {"x": 0.0, "y": 0.0, "z": 0.0},
    }
    if v2:
        out["IsRock"] = False
        out["IceCount"] = 0
        out["ShapeGridData"] = [
            {"GridPos": _vec2int(x, y, v2=True), "ActiveBlocks": []}
            for x, y in SHAPES[spec.shape_id]
        ]
    return out


def _camera_z(width: int, height: int, rng: random.Random) -> float:
    """Sample a plausible camera distance from corpus levels of similar size."""
    dim = max(width, height)
    samples: list[float] = []
    if CORPUS_DIR.is_dir():
        for path in CORPUS_DIR.glob("Level *.json"):
            raw = json.loads(Path(path).read_text())
            cam = raw.get("CameraLocalPosition")
            if cam is None:
                continue
            d = max(int(raw["BoardSize"]["x"]), int(raw["BoardSize"]["y"]))
            if abs(d - dim) <= 1:
                samples.append(float(cam["z"]))
    if not samples:
        return round(-2.98 * dim - 22.9, 2)
    return round(rng.choice(samples) + rng.uniform(-0.5, 0.5), 2)


def to_game_json(
    design: Design,
    *,
    variant: str = "v1",
    seed: int = 0,
) -> dict[str, Any]:
    """Emit the game's on-disk level schema.

    Args:
        design: A (preferably certified) design.
        variant: ``"v1"`` for the old-editor schema (plain vectors, string axis
            enums, null configs, ``CameraLocalPosition``) or ``"v2"`` for the
            new one (Unity vector metadata, ``DifficultyType`` and feature-stub
            arrays, no camera).
        seed: Drives the editor-noise mimicry (camera-z scatter and the ~2%
            chance of a raw rotation offset by 4 or 8, both present in the
            corpus).

    Returns:
        A JSON-serializable dict matching the corpus files of that generation.

    Raises:
        ValueError: If ``variant`` is unknown.
    """
    if variant not in ("v1", "v2"):
        raise ValueError(f"unknown variant {variant!r}; use 'v1' or 'v2'")
    v2 = variant == "v2"
    rng = random.Random(seed)

    seats = []
    for spec in design.seats:
        raw_rotation = spec.rotation
        if rng.random() < 0.02:
            raw_rotation += rng.choice((4, 8))
        seats.append(_seat_json(spec, raw_rotation, v2=v2))

    elevators = []
    for e in design.elevators:
        colors = [s.color for s in e.queue]
        entry: dict[str, Any] = {
            "GridPos": _vec2int(*e.cell, v2=v2),
            "PositionToCell": _vec3f(float(e.offset[0]), 0.0, float(e.offset[1]), v2=v2),
            "SettlerColorsList": colors,
            "SettlerConfig": ([_settler_json(s, e.cell, v2=True) for s in e.queue] if v2 else None),
        }
        elevators.append(entry)

    out: dict[str, Any] = {
        "BoardSize": _vec2int(design.width, design.height, v2=v2),
        "LevelDuration": 300,
        "Seats": seats,
        "Settlers": [_settler_json(s, pos, v2=v2) for pos, s in design.settlers],
        "ObstaclePositions": [_vec2int(*c, v2=v2) for c in sorted(design.obstacles)],
        "ElevatorConfigs": elevators,
    }
    if v2:
        out["CrateConfigs"] = []
        out["ColorPathConfigs"] = []
        out["DifficultyType"] = 0
        out["ConnectionData"] = []
    else:
        out["CameraLocalPosition"] = {
            "x": 0.0,
            "y": 0.0,
            "z": _camera_z(design.width, design.height, rng),
        }
    return out


def variant_for_slot(slot: int) -> str:
    """Schema generation used by the corpus level at this slot (nearest if absent)."""
    if not CORPUS_DIR.is_dir():
        return "v1"
    for delta in range(0, 100):
        for n in (slot - delta, slot + delta):
            path = CORPUS_DIR / f"Level {n}.json"
            if path.is_file():
                raw = json.loads(path.read_text())
                return "v1" if "CameraLocalPosition" in raw else "v2"
    return "v1"


def save_level(design: Design, path: str | Path, *, variant: str = "v1", seed: int = 0) -> Path:
    """Write the emitted JSON to disk with the corpus' 2-space formatting."""
    p = Path(path)
    p.write_text(json.dumps(to_game_json(design, variant=variant, seed=seed), indent=2) + "\n")
    return p
