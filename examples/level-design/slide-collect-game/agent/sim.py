# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Simulator for slide-and-collect levels.

Rules are inferred from the 100-level corpus plus frame-by-frame study of gameplay
videos (levels 2, 11, 30, 31 and 70 were watched and matched against the corpus
JSON), not from game code. Two evidence sources rank a rule variant: direct video
observation of the live game, then corpus solvability under :func:`solve_beam`.
Rejected variants fail large parts of the corpus (sliding movement <= 60/100,
settlers-are-solid 90/100 with provable geometric deadlocks, cover-only absorption
93/100).

**The reference corpus is a stale export, not the live level set.** Six levels —
31, 53, 62, 72, 84 and 92 — are unsolvable under the live rules (levels 31 and 84
by exact counting arguments: level 31's first queued settler needs a seat layer
only exposed by the settlers behind it; level 84's third key is locked behind the
seat it unlocks). The live game's level 31 is a *different level* (verified on
video), i.e. the broken exports were later replaced. Under the default rules the
solver clears the remaining **94/100**; exclude the six from golden pools and never
imitate them.

Board: rectangular grid ``BoardSize.x`` x ``BoardSize.y``, cell ``(0, 0)`` at the
bottom-left. Obstacles are permanently blocked cells.

Seats ("holes" in the game's own tutorial copy): colored polyomino blocks. ``SeatID``
selects one of 8 fixed shape templates; the world footprint is ``(shape - anchor) ->
mirror-X -> rotate CW (Rotation % 4) -> translate by GridPos``. This transform
reproduces all 810 corpus seats with zero out-of-bounds or overlap violations.

Dragging: the player lifts a seat over the board and drops it on any legal placement
— there is no slide path requirement (``Rules.teleport``). A placement is legal when
every cell is on the board, off obstacles, off other seats, and holds no *mismatched*
settler; cells holding same-colored settlers may be covered (they board the seat).
``AxisLockType`` restricts the drop to the seat's own row (``Horizontal``) or column
(``Vertical``). Seats sharing a positive ``ConnectedBrickID`` are welded and move as
one rigid unit (each half absorbs its own color; each half drops independently).

Absorption: after a drop, settlers covered by a matching seat and settlers
orthogonally adjacent to a matching seat with free capacity board it, cascading to a
fixpoint (boardings free cells and expose layers, enabling more boardings). Nothing
happens at spawn — gameplay video shows the board static until the first drag — and
the real game most likely cascades only around the dragged seat; the global cascade
here can under-count taps but cannot change solvability. A seat whose every cell is
filled leaves the board (frees its cells) — or, if it has an ``InnerBrickColorIndex``,
reveals a second layer of that color in the same footprint with fresh capacity.

Lock & key (level-70 tutorial, verbatim): "Chained holes can't move until unlocked.
Collect keys to unchain." A chained seat (``LockNKeyConfig.Type == 1``) is fully
inert — it neither moves nor absorbs — until the keys of its lock family are
collected (settlers with ``Type == 2`` and the same ``ColorId`` boarding any seat).
The live game shows a countdown badge on the padlock equal to the number of
authored keys (level-70 video), so the default requires *all* keys; ``ColorId`` is
a lock-family id, independent of brick color.

Elevators: off-board queues at cell ``GridPos``; ``PositionToCell`` holds the
outward offset (its ``z`` is the grid's vertical axis; ``(0, 0, 0)`` marks the
interior spawners of levels 90-99). Queued settlers never stand on the board: the
front settler boards a matching seat parked at the mouth (covering or beside the
elevator's cell), confirmed by video — the level-11 queue counts down with no
figures appearing on the grid. Release order is the ``SettlerConfig`` array order
(level 11's queue [2,7,1,1] visibly drains red, blue, green). ``SettlerConfig`` is
authoritative; ``SettlerColorsList`` is a stale denormalized cache (wrong in 29% of
elevators) used only when ``SettlerConfig`` is null.

Exact corpus invariant backing all of this: per color, total seat capacity (cells,
counting inner layers twice) equals total settler count (board + elevator queues) in
100/100 levels.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Coord = tuple[int, int]

#: Canonical (unrotated, unmirrored) cells for each ``SeatID``, x right / y up.
SHAPES: dict[int, tuple[Coord, ...]] = {
    0: ((0, 0),),
    1: ((0, 0), (1, 0)),
    2: ((0, 0), (1, 0), (2, 0)),
    3: ((0, 0), (1, 0), (1, 1), (2, 0)),
    4: ((0, 0), (1, -1), (1, 0), (1, 1), (2, 0)),
    5: ((0, 0), (0, 1), (0, 2), (1, 0)),
    6: ((0, 0), (0, 1), (1, 0)),
    7: ((0, 0), (0, 1), (1, 0), (1, 1)),
}

#: ``GridPos`` names this cell of the shape (the middle cell for the three
#: end-anchored templates), not ``(0, 0)``.
ANCHORS: dict[int, Coord] = {2: (1, 0), 3: (1, 0), 4: (1, 0)}

AXIS_NONE, AXIS_VERTICAL, AXIS_HORIZONTAL = 0, 1, 2
_AXIS_NAMES = {
    "None": AXIS_NONE,
    "Vertical": AXIS_VERTICAL,
    "Horizontal": AXIS_HORIZONTAL,
}

N_COLORS = 10


def seat_cells(shape_id: int, rotation: int, mirrored: bool, pos: Coord) -> tuple[Coord, ...]:
    """World-space footprint of a seat.

    Args:
        shape_id: Shape template id, 0-7.
        rotation: Quarter-turns clockwise; only ``rotation % 4`` matters.
        mirrored: Mirror across the X axis *before* rotating.
        pos: World position of the shape's anchor cell.

    Returns:
        The absolute board cells covered by the seat.
    """
    ax, ay = ANCHORS.get(shape_id, (0, 0))
    out = []
    for cx, cy in SHAPES[shape_id]:
        x, y = cx - ax, cy - ay
        if mirrored:
            x = -x
        for _ in range(rotation % 4):
            x, y = y, -x
        out.append((pos[0] + x, pos[1] + y))
    return tuple(out)


@dataclass(frozen=True)
class Settler:
    """A stick figure: a color and an optional key it carries."""

    color: int
    key_id: int | None = None


@dataclass(frozen=True)
class Seat:
    """A draggable colored block, as authored in the level file."""

    pos: Coord
    color: int
    shape_id: int
    rotation: int
    mirrored: bool = False
    inner_color: int | None = None
    axis_lock: int = AXIS_NONE
    connected_id: int | None = None
    lock_id: int | None = None

    @property
    def cells(self) -> tuple[Coord, ...]:
        """World footprint at the authored position."""
        return seat_cells(self.shape_id, self.rotation, self.mirrored, self.pos)

    @property
    def capacity(self) -> int:
        """Settlers required to clear the seat, counting an inner layer twice."""
        return len(self.cells) * (2 if self.inner_color is not None else 1)


@dataclass(frozen=True)
class Elevator:
    """An off-board settler queue feeding one cell."""

    cell: Coord
    offset: Coord
    queue: tuple[Settler, ...]


@dataclass(frozen=True)
class Level:
    """A parsed, version-normalized slide-and-collect level."""

    width: int
    height: int
    seats: tuple[Seat, ...]
    settlers: tuple[tuple[Coord, Settler], ...]
    obstacles: frozenset[Coord]
    elevators: tuple[Elevator, ...]
    name: str = ""

    @classmethod
    def parse(cls, raw: dict[str, Any], name: str = "") -> Level:
        """Build a level from any of the corpus' schema generations."""
        seats = tuple(_parse_seat(s) for s in raw["Seats"])
        settlers = tuple((_xy(t["GridPos"]), _parse_settler(t)) for t in raw.get("Settlers") or [])
        obstacles = frozenset(_xy(o) for o in raw.get("ObstaclePositions") or [])
        elevators = tuple(_parse_elevator(e) for e in raw.get("ElevatorConfigs") or [])
        return cls(
            width=int(raw["BoardSize"]["x"]),
            height=int(raw["BoardSize"]["y"]),
            seats=seats,
            settlers=settlers,
            obstacles=obstacles,
            elevators=elevators,
            name=name,
        )

    @classmethod
    def load(cls, path: str | Path) -> Level:
        """Parse a level JSON file; the level is named after the file stem."""
        p = Path(path)
        return cls.parse(json.loads(p.read_text()), name=p.stem)

    def in_bounds(self, c: Coord) -> bool:
        """Whether a cell lies on the board."""
        return 0 <= c[0] < self.width and 0 <= c[1] < self.height


def _xy(v: dict[str, Any]) -> Coord:
    return int(v["x"]), int(v["y"])


def _opt_color(v: Any) -> int | None:
    return None if v is None or int(v) < 0 else int(v)


def _lock_id(cfg: Any, want_type: int) -> int | None:
    if not isinstance(cfg, dict) or int(cfg.get("Type", 0)) != want_type:
        return None
    return int(cfg["ColorId"])


def _axis_lock(v: Any) -> int:
    return _AXIS_NAMES[v] if isinstance(v, str) else int(v)


def _parse_seat(s: dict[str, Any]) -> Seat:
    # ConnectedBrickID "none" sentinel is 0 in old exports and -1 in new ones;
    # real groups use positive ids.
    connected = int(s.get("ConnectedBrickID") or 0)
    return Seat(
        pos=_xy(s["GridPos"]),
        color=int(s["ColorIndex"]),
        shape_id=int(s["SeatID"]),
        rotation=int(s["Rotation"]) % 4,
        mirrored=bool(s["IsMirrored"]),
        inner_color=_opt_color(s.get("InnerBrickColorIndex")),
        axis_lock=_axis_lock(s.get("AxisLockType", 0)),
        connected_id=connected if connected > 0 else None,
        lock_id=_lock_id(s.get("LockNKeyConfig"), 1),
    )


def _parse_settler(t: dict[str, Any]) -> Settler:
    return Settler(color=int(t["ColorIndex"]), key_id=_lock_id(t.get("LockNKeyConfig"), 2))


def _parse_elevator(e: dict[str, Any]) -> Elevator:
    cfg = e.get("SettlerConfig")
    if cfg is not None:
        queue = tuple(_parse_settler(t) for t in cfg)
    else:
        queue = tuple(Settler(color=int(c)) for c in e.get("SettlerColorsList") or [])
    p = e.get("PositionToCell") or {}
    return Elevator(
        cell=_xy(e["GridPos"]),
        offset=(int(p.get("x", 0)), int(p.get("z", 0))),
        queue=queue,
    )


# --------------------------------------------------------------------------- validation


def validate(level: Level) -> list[str]:
    """Check every corpus-exact invariant; return human-readable violations.

    An empty list means the level satisfies all rules that hold 100/100 in the
    reference corpus (bounds, uniqueness, non-overlap, color conservation,
    key/lock pairing, free-cell headroom, connected-pair well-formedness).
    """
    errors: list[str] = []

    def err(msg: str) -> None:
        errors.append(msg)

    if level.width <= 0 or level.height <= 0:
        return [f"board size {level.width}x{level.height} is not positive"]

    covered: dict[Coord, int] = {}
    for i, seat in enumerate(level.seats):
        if not 0 <= seat.shape_id <= 7:
            err(f"seat {i}: SeatID {seat.shape_id} outside 0-7")
            continue
        if not 0 <= seat.color < N_COLORS:
            err(f"seat {i}: color {seat.color} outside 0-{N_COLORS - 1}")
        if seat.inner_color is not None and not 0 <= seat.inner_color < N_COLORS:
            err(f"seat {i}: inner color {seat.inner_color} outside 0-{N_COLORS - 1}")
        if seat.inner_color is not None and seat.inner_color == seat.color:
            err(f"seat {i}: inner color equals outer color {seat.color}")
        for c in seat.cells:
            if not level.in_bounds(c):
                err(f"seat {i}: cell {c} out of bounds")
            elif c in covered:
                err(f"seat {i}: cell {c} overlaps seat {covered[c]}")
            elif c in level.obstacles:
                err(f"seat {i}: cell {c} sits on an obstacle")
            covered[c] = i

    for o in level.obstacles:
        if not level.in_bounds(o):
            err(f"obstacle {o} out of bounds")

    occupied: set[Coord] = set()
    for pos, settler in level.settlers:
        if not level.in_bounds(pos):
            err(f"settler at {pos} out of bounds")
        if pos in occupied:
            err(f"duplicate settler position {pos}")
        occupied.add(pos)
        if pos in covered:
            err(f"settler at {pos} sits on seat {covered[pos]}")
        if pos in level.obstacles:
            err(f"settler at {pos} sits on an obstacle")
        if not 0 <= settler.color < N_COLORS:
            err(f"settler at {pos}: color {settler.color} outside 0-{N_COLORS - 1}")

    free_cells = level.width * level.height - len(covered) - len(level.obstacles)
    if len(level.settlers) > free_cells:
        err(f"{len(level.settlers)} board settlers exceed {free_cells} free cells")

    seen_elevator_cells: set[Coord] = set()
    for i, elev in enumerate(level.elevators):
        if not level.in_bounds(elev.cell):
            err(f"elevator {i}: cell {elev.cell} out of bounds")
        if elev.cell in seen_elevator_cells:
            err(f"elevator {i}: duplicate cell {elev.cell}")
        seen_elevator_cells.add(elev.cell)
        if elev.cell in level.obstacles:
            err(f"elevator {i}: cell {elev.cell} is an obstacle")
        for settler in elev.queue:
            if not 0 <= settler.color < N_COLORS:
                err(f"elevator {i}: queued color {settler.color} outside 0-{N_COLORS - 1}")

    capacity: Counter[int] = Counter()
    for seat in level.seats:
        capacity[seat.color] += len(seat.cells)
        if seat.inner_color is not None:
            capacity[seat.inner_color] += len(seat.cells)
    supply: Counter[int] = Counter(s.color for _, s in level.settlers)
    supply.update(s.color for e in level.elevators for s in e.queue)
    for color in sorted(set(capacity) | set(supply)):
        if capacity[color] != supply[color]:
            err(f"color {color}: seat capacity {capacity[color]} != settler count {supply[color]}")

    locks = Counter(s.lock_id for s in level.seats if s.lock_id is not None)
    keys = Counter(
        s.key_id
        for s in [s for _, s in level.settlers] + [s for e in level.elevators for s in e.queue]
        if s.key_id is not None
    )
    for lock_id in locks:
        if keys[lock_id] == 0:
            err(f"lock {lock_id} has no keys")
    for key_id in keys:
        if locks[key_id] == 0:
            err(f"key {key_id} has no lock")

    groups: dict[int, list[int]] = {}
    for i, seat in enumerate(level.seats):
        if seat.connected_id is not None:
            groups.setdefault(seat.connected_id, []).append(i)
    for gid, members in groups.items():
        if len(members) != 2:
            err(f"connected group {gid} has {len(members)} seats, expected 2")
            continue
        a, b = (level.seats[m] for m in members)
        adjacent = any(
            abs(ca[0] - cb[0]) + abs(ca[1] - cb[1]) == 1 for ca in a.cells for cb in b.cells
        )
        if not adjacent:
            err(f"connected group {gid}: seat footprints are not adjacent")

    return errors


# --------------------------------------------------------------------------- simulation

_STEPS: dict[int, tuple[Coord, ...]] = {
    AXIS_NONE: ((1, 0), (-1, 0), (0, 1), (0, -1)),
    AXIS_VERTICAL: ((0, 1), (0, -1)),
    AXIS_HORIZONTAL: ((1, 0), (-1, 0)),
}


@dataclass
class Block:
    """Runtime state of one seat."""

    seat: Seat
    delta: Coord = (0, 0)
    layer: int = 0
    filled: int = 0
    alive: bool = True

    @property
    def color(self) -> int:
        """Color of the currently exposed layer."""
        if self.layer == 1:
            assert self.seat.inner_color is not None
            return self.seat.inner_color
        return self.seat.color

    @property
    def cells(self) -> tuple[Coord, ...]:
        """Current world footprint."""
        dx, dy = self.delta
        return tuple((x + dx, y + dy) for x, y in self.seat.cells)


@dataclass(frozen=True)
class Rules:
    """Movement/absorption rule variant (see module docstring for the default).

    Attributes:
        figures_solid: Settlers block a seat from occupying their cell. When
            false, a seat may rest on a *matching* settler's cell (boarding it).
        absorb_adjacent: Settlers orthogonally adjacent to a matching seat board
            it during the cascade.
        absorb_cover: Settlers whose cell is covered by a matching seat board it
            during the cascade (only reachable when ``figures_solid`` is false).
        teleport: A drag lifts the seat over the board and drops it on any legal
            placement (no connected slide path required). Axis locks still
            constrain the destination to the seat's row/column.
    """

    figures_solid: bool = False
    absorb_adjacent: bool = True
    absorb_cover: bool = True
    teleport: bool = True
    elevator_order: str = "fifo"
    """Release order of elevator queues: ``fifo`` (``SettlerConfig`` order —
    the live game's rule, confirmed by watching level 11's queue drain in
    exactly JSON order), ``lifo`` (reverse), or ``pool`` (any queued settler,
    order-free). Queued settlers never stand on the board — they board a
    matching seat parked at the elevator's mouth (adjacent to or covering its
    cell)."""


class Simulator:
    """Mutable game state driven by unit-step block moves.

    Args:
        level: A parsed level; it is not mutated.
        rules: Movement/absorption variant; default is the model that best fits
            the video evidence.
        unlock_requires_all_keys: If true (default), a chained seat unchains
            only after *every* key of its lock family has boarded a seat — the
            live game displays a key counter on the padlock (level-70 video)
            equal to the number of authored keys. Set false for the
            single-key-suffices variant.
    """

    def __init__(
        self,
        level: Level,
        *,
        rules: Rules = Rules(),
        unlock_requires_all_keys: bool = True,
    ) -> None:
        self.level = level
        self.rules = rules
        self.unlock_all = unlock_requires_all_keys
        self.blocks = [Block(seat=s) for s in level.seats]
        self.figures: dict[Coord, Settler] = dict(level.settlers)
        self.queues: list[list[Settler]] = [list(e.queue) for e in level.elevators]
        self.keys_needed: Counter[int] = Counter(
            s.key_id
            for s in [s for _, s in level.settlers] + [s for e in level.elevators for s in e.queue]
            if s.key_id is not None
        )
        self.keys_collected: Counter[int] = Counter()
        self.taps = 0
        self.steps = 0
        # Board settlers stay put at spawn (level-11 footage), but elevator
        # mouths fire immediately: the studio's play test showed a queued
        # character jumping into an adjacent matching hole before any input.
        self._mouth_cascade()

    def clone(self) -> Simulator:
        """Cheap deep copy for lookahead (shares the immutable level)."""
        twin = object.__new__(Simulator)
        twin.level = self.level
        twin.rules = self.rules
        twin.unlock_all = self.unlock_all
        twin.blocks = [
            Block(
                seat=b.seat,
                delta=b.delta,
                layer=b.layer,
                filled=b.filled,
                alive=b.alive,
            )
            for b in self.blocks
        ]
        twin.figures = dict(self.figures)
        twin.queues = [list(q) for q in self.queues]
        twin.keys_needed = Counter(self.keys_needed)
        twin.keys_collected = Counter(self.keys_collected)
        twin.taps = self.taps
        twin.steps = self.steps
        return twin

    # -- state queries

    def _blocked_cells(self, exclude: frozenset[int]) -> set[Coord]:
        out: set[Coord] = set()
        for i, b in enumerate(self.blocks):
            if b.alive and i not in exclude:
                out.update(b.cells)
        return out

    def group_of(self, index: int) -> tuple[int, ...]:
        """Indices of the blocks welded to ``index`` (including itself)."""
        cid = self.blocks[index].seat.connected_id
        if cid is None:
            return (index,)
        return tuple(i for i, b in enumerate(self.blocks) if b.alive and b.seat.connected_id == cid)

    def is_unlocked(self, index: int) -> bool:
        """Whether the block's lock (if any) has been opened."""
        lock = self.blocks[index].seat.lock_id
        if lock is None:
            return True
        need = self.keys_needed[lock] if self.unlock_all else 1
        return self.keys_collected[lock] >= need

    def movable(self, index: int) -> bool:
        """Whether the block (with its welded partner) may move at all."""
        group = self.group_of(index)
        return all(self.blocks[i].alive and self.is_unlocked(i) for i in group)

    def allowed_steps(self, index: int) -> tuple[Coord, ...]:
        """Unit steps permitted by the group's combined axis locks."""
        axes = {self.blocks[i].seat.axis_lock for i in self.group_of(index)}
        axes.discard(AXIS_NONE)
        if not axes:
            return _STEPS[AXIS_NONE]
        if len(axes) > 1:
            return ()
        return _STEPS[axes.pop()]

    def cell_ok(self, block: Block, c: Coord, blocked: set[Coord]) -> bool:
        """Whether ``block`` may occupy cell ``c`` given other blocks' cells."""
        if not self.level.in_bounds(c) or c in self.level.obstacles or c in blocked:
            return False
        fig = self.figures.get(c)
        if fig is None:
            return True
        return not self.rules.figures_solid and fig.color == block.color

    def can_step(self, index: int, step: Coord) -> bool:
        """Whether the group containing ``index`` may take one unit step."""
        if not self.movable(index) or step not in self.allowed_steps(index):
            return False
        group = frozenset(self.group_of(index))
        blocked = self._blocked_cells(exclude=group)
        return all(
            self.cell_ok(self.blocks[i], (x + step[0], y + step[1]), blocked)
            for i in group
            for x, y in self.blocks[i].cells
        )

    # -- state changes

    def step(self, index: int, step: Coord) -> bool:
        """Move the group one step, then run the absorption cascade.

        Returns:
            False if the step is illegal (state unchanged), True otherwise.
        """
        if not self.can_step(index, step):
            return False
        for i in self.group_of(index):
            b = self.blocks[i]
            b.delta = (b.delta[0] + step[0], b.delta[1] + step[1])
        self.steps += 1
        self._cascade()
        return True

    def place(self, index: int, delta: Coord) -> bool:
        """Drop the group at absolute translation ``delta`` (teleport drag).

        Returns:
            False if the placement is illegal (state unchanged), True otherwise.
        """
        if not self.movable(index):
            return False
        start = self.blocks[index].delta
        d = (delta[0] - start[0], delta[1] - start[1])
        if d == (0, 0):
            return False
        allowed = self.allowed_steps(index)
        can_x = any(s[0] != 0 for s in allowed)
        can_y = any(s[1] != 0 for s in allowed)
        if (d[0] != 0 and not can_x) or (d[1] != 0 and not can_y):
            return False
        group = frozenset(self.group_of(index))
        blocked = self._blocked_cells(exclude=group)
        for i in group:
            block = self.blocks[i]
            if not all(self.cell_ok(block, (x + d[0], y + d[1]), blocked) for x, y in block.cells):
                return False
        for i in group:
            b = self.blocks[i]
            b.delta = (b.delta[0] + d[0], b.delta[1] + d[1])
        self.steps += 1
        self._cascade()
        return True

    def _absorbers(self, cell: Coord, color: int) -> list[int]:
        """Blocks able to take a settler of ``color`` standing at ``cell`` right now."""
        out = []
        for i, b in enumerate(self.blocks):
            if not b.alive or b.color != color or not self.is_unlocked(i):
                continue
            if b.filled >= len(b.cells):
                continue
            footprint = set(b.cells)
            covered = self.rules.absorb_cover and cell in footprint
            adjacent = self.rules.absorb_adjacent and any(
                (cell[0] + dx, cell[1] + dy) in footprint for dx, dy in _STEPS[AXIS_NONE]
            )
            if covered or adjacent:
                out.append(i)
        return out

    def _take(self, settler: Settler, index: int) -> None:
        if settler.key_id is not None:
            self.keys_collected[settler.key_id] += 1
        block = self.blocks[index]
        block.filled += 1
        if block.filled == len(block.cells):
            if block.layer == 0 and block.seat.inner_color is not None:
                block.layer = 1
                block.filled = 0
            else:
                block.alive = False

    def _board(self, cell: Coord, index: int) -> None:
        self._take(self.figures.pop(cell), index)

    def _mouth_takers(self, cell: Coord, color: int) -> list[int]:
        """Blocks parked at an elevator mouth (covering or beside its cell)."""
        out = []
        for i, b in enumerate(self.blocks):
            if not b.alive or b.color != color or not self.is_unlocked(i):
                continue
            if b.filled >= len(b.cells):
                continue
            footprint = set(b.cells)
            if cell in footprint or any(
                (cell[0] + dx, cell[1] + dy) in footprint for dx, dy in _STEPS[AXIS_NONE]
            ):
                out.append(i)
        return out

    def _release_positions(self, queue: list[Settler]) -> tuple[int, ...]:
        """Queue indices eligible for release, per ``rules.elevator_order``."""
        if not queue:
            return ()
        match self.rules.elevator_order:
            case "fifo":
                return (0,)
            case "lifo":
                return (len(queue) - 1,)
            case _:
                return tuple(range(len(queue)))

    def _mouth_cascade(self) -> None:
        """Elevator-mouth releases only (runs at spawn; board settlers wait)."""
        changed = True
        while changed:
            changed = False
            for elev, queue in zip(self.level.elevators, self.queues, strict=True):
                for qi in self._release_positions(queue):
                    takers = self._mouth_takers(elev.cell, queue[qi].color)
                    if takers:
                        self._take(queue.pop(qi), takers[0])
                        changed = True
                        break

    def _cascade(self) -> None:
        """Run auto-boarding and elevator releases to a fixpoint.

        Any settler orthogonally adjacent to a matching, unlocked, non-full seat
        boards it. Queued elevator settlers never stand on the board: the
        releasable one(s) board a matching seat parked at the elevator's mouth.
        Completions (which free cells, flip layers and open locks) enable
        further boardings, so the loop repeats until nothing changes. Boarding
        order is deterministic: settlers by position, blocks by index — the
        corpus never makes the outcome depend on it.
        """
        changed = True
        while changed:
            changed = False
            for cell in sorted(self.figures):
                takers = self._absorbers(cell, self.figures[cell].color)
                if takers:
                    self._board(cell, takers[0])
                    changed = True
            for elev, queue in zip(self.level.elevators, self.queues, strict=True):
                for qi in self._release_positions(queue):
                    takers = self._mouth_takers(elev.cell, queue[qi].color)
                    if takers:
                        self._take(queue.pop(qi), takers[0])
                        changed = True
                        break

    @property
    def solved(self) -> bool:
        """True when every block has left the board."""
        return not any(b.alive for b in self.blocks)

    @property
    def remaining(self) -> int:
        """Settlers not yet boarded (on the board plus queued)."""
        return len(self.figures) + sum(len(q) for q in self.queues)

    def potential(self) -> int:
        """Search heuristic: how far holes sit from their remaining work.

        Sum over alive, unlocked holes of the Manhattan distance from the hole
        to its nearest same-colored standing character (mouths count via the
        elevator cell). Lower is better; it makes non-collecting "park" moves
        rank by usefulness instead of luck.
        """
        total = 0
        for i, b in enumerate(self.blocks):
            if not b.alive or not self.is_unlocked(i):
                continue
            targets = [pos for pos, f in self.figures.items() if f.color == b.color]
            for elev, queue in zip(self.level.elevators, self.queues, strict=True):
                if any(s.color == b.color for s in queue):
                    targets.append(elev.cell)
            if not targets:
                continue
            best = min(abs(tx - cx) + abs(ty - cy) for tx, ty in targets for cx, cy in b.cells)
            total += best
        return total

    def state_key(self) -> tuple[object, ...]:
        """Hashable identity of the game state, for search deduplication."""
        return (
            tuple((i, b.delta, b.layer, b.filled) for i, b in enumerate(self.blocks) if b.alive),
            frozenset(self.figures.items()),
            tuple(len(q) for q in self.queues),
            tuple(sorted(self.keys_collected.items())),
        )


# --------------------------------------------------------------------------- solver


@dataclass(frozen=True)
class SolveResult:
    """Outcome of :func:`solve`."""

    solved: bool
    taps: int
    steps: int
    playouts_tried: int


@dataclass(frozen=True)
class _Plan:
    index: int
    path: tuple[Coord, ...]
    covered: int
    completes: bool
    dest: Coord | None = None


def _plans_for(sim: Simulator, index: int, limit: int = 4096) -> tuple[list[_Plan], list[_Plan]]:
    """BFS the group's translation space for reachable placements.

    Returns:
        Two plan lists: placements that put at least one matching settler
        orthogonally adjacent to the group (absorbing drags), and non-absorbing
        repositionings ("parks") used to clear lanes when no absorbing drag
        exists anywhere.

    The search is static — it does not model settlers boarding en route, which
    only ever *loosens* real passability and adds free absorptions.
    """
    if not sim.movable(index):
        return [], []
    steps = sim.allowed_steps(index)
    group = frozenset(sim.group_of(index))
    blocked = sim._blocked_cells(exclude=group)
    members = [(i, sim.blocks[i]) for i in group]
    start = sim.blocks[index].delta

    def legal(delta: Coord) -> bool:
        dx, dy = delta[0] - start[0], delta[1] - start[1]
        return all(
            sim.cell_ok(b, (x + dx, y + dy), blocked) for _, b in members for x, y in b.cells
        )

    def gain(delta: Coord) -> tuple[int, bool]:
        dx, dy = delta[0] - start[0], delta[1] - start[1]
        total = 0
        completes = False
        for _, b in members:
            footprint = {(x + dx, y + dy) for x, y in b.cells}
            sources: set[Coord] = set()
            if sim.rules.absorb_cover:
                sources |= footprint
            if sim.rules.absorb_adjacent:
                sources |= {
                    (cx + sx, cy + sy) for cx, cy in footprint for sx, sy in _STEPS[AXIS_NONE]
                } - footprint
            near = sum(
                1 for c in sources if (f := sim.figures.get(c)) is not None and f.color == b.color
            )
            mouth = footprint | {
                (cx + sx, cy + sy) for cx, cy in footprint for sx, sy in _STEPS[AXIS_NONE]
            }
            for elev, queue in zip(sim.level.elevators, sim.queues, strict=True):
                if elev.cell in mouth:
                    near += sum(
                        1 for qi in sim._release_positions(queue) if queue[qi].color == b.color
                    )
            near = min(near, len(b.cells) - b.filled)
            total += near
            if b.filled + near >= len(b.cells):
                completes = True
        return total, completes

    def path_to(delta: Coord, prev: dict[Coord, Coord]) -> tuple[Coord, ...]:
        path = [delta]
        node = delta
        while prev[node] != node:
            node = prev[node]
            path.append(node)
        path.reverse()
        return tuple((b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:], strict=False))

    absorbing: list[_Plan] = []
    parks: list[_Plan] = []
    if sim.rules.teleport:
        can_x = any(s[0] != 0 for s in steps)
        can_y = any(s[1] != 0 for s in steps)
        w, h = sim.level.width, sim.level.height
        for dx in range(-w + 1, w) if can_x else (0,):
            for dy in range(-h + 1, h) if can_y else (0,):
                cand = (start[0] + dx, start[1] + dy)
                if cand == start or not legal(cand):
                    continue
                covered, completes = gain(cand)
                dist = abs(dx) + abs(dy)
                plan = _Plan(index, ((0, 0),) * dist, covered, completes, dest=cand)
                (absorbing if covered else parks).append(plan)
        return absorbing, parks

    prev: dict[Coord, Coord] = {start: start}
    frontier = [start]
    while frontier and len(prev) < limit:
        nxt: list[Coord] = []
        for cur in frontier:
            for sx, sy in steps:
                cand = (cur[0] + sx, cur[1] + sy)
                if cand in prev or not legal(cand):
                    continue
                prev[cand] = cur
                covered, completes = gain(cand)
                plan = _Plan(index, path_to(cand, prev), covered, completes)
                (absorbing if covered else parks).append(plan)
                nxt.append(cand)
        frontier = nxt
    return absorbing, parks


def _execute(sim: Simulator, plan: _Plan) -> None:
    sim.taps += 1
    if plan.dest is not None:
        sim.place(plan.index, plan.dest)
        return
    for step in plan.path:
        alive_before = sim.blocks[plan.index].alive
        if not alive_before or not sim.step(plan.index, step):
            return


def _pick_park(sim: Simulator, parks: list[_Plan], rng: random.Random, sample: int = 32) -> _Plan:
    """Choose the repositioning most likely to re-open the game.

    Tries up to ``sample`` random parks in a cloned simulator and keeps the one
    whose resulting state offers the most absorbing drags (ties broken toward
    longer parks, which shuffle a gridlocked board more).
    """
    candidates = parks if len(parks) <= sample else rng.sample(parks, sample)
    best, best_score = candidates[0], -1
    for plan in candidates:
        twin = sim.clone()
        _execute(twin, plan)
        score = 0
        seen: set[tuple[int, ...]] = set()
        for i, b in enumerate(twin.blocks):
            if not b.alive:
                continue
            g = twin.group_of(i)
            if g in seen:
                continue
            seen.add(g)
            score += len(_plans_for(twin, i)[0])
        if twin.solved:
            score += 1000
        if score > best_score:
            best, best_score = plan, score
    return best


def _gather_plans(sim: Simulator) -> tuple[list[_Plan], list[_Plan]]:
    absorbing: list[_Plan] = []
    parks: list[_Plan] = []
    seen: set[tuple[int, ...]] = set()
    for i, b in enumerate(sim.blocks):
        if not b.alive:
            continue
        g = sim.group_of(i)
        if g in seen:
            continue
        seen.add(g)
        found, found_parks = _plans_for(sim, i)
        absorbing.extend(found)
        parks.extend(found_parks)
    return absorbing, parks


def solve_beam(
    level: Level,
    *,
    width: int = 48,
    max_taps: int = 96,
    branch: int = 10,
    park_branch: int = 4,
    rules: Rules = Rules(),
    unlock_requires_all_keys: bool = True,
) -> SolveResult:
    """Clear a level with a deduplicated beam search over drag plans.

    Each search node is a full game state; children are absorbing drags (best
    ``branch`` by completion/coverage/shortness) plus ``park_branch`` short
    repositionings, so lines that need a lane cleared before the next pickup
    stay in the beam. States are ranked by settlers boarded, then blocks
    cleared, then fewest taps.

    Args:
        level: The level to attempt.
        width: Beam width (states kept per depth).
        max_taps: Maximum search depth in drags.
        branch: Absorbing children per state.
        park_branch: Parking children per state.
        rules: Movement/absorption variant to simulate.
        unlock_requires_all_keys: Passed through to :class:`Simulator`.

    Returns:
        The first solving line found, or the best failing state's counters.
    """
    root = Simulator(level, rules=rules, unlock_requires_all_keys=unlock_requires_all_keys)
    if root.solved:
        return SolveResult(True, root.taps, root.steps, 1)
    beam = [root]
    seen: set[tuple[object, ...]] = {root.state_key()}
    best_fail = root
    for _ in range(max_taps):
        children: list[Simulator] = []
        for sim in beam:
            absorbing, parks = _gather_plans(sim)
            absorbing.sort(key=lambda p: (not p.completes, -p.covered, len(p.path)))
            # diversity: keep the two best plans of EVERY hole before filling
            # the global quota, so the beam cannot collapse onto a single hole
            per_group: dict[int, int] = {}
            diverse: list[_Plan] = []
            rest: list[_Plan] = []
            for plan in absorbing:
                if per_group.get(plan.index, 0) < 2:
                    per_group[plan.index] = per_group.get(plan.index, 0) + 1
                    diverse.append(plan)
                else:
                    rest.append(plan)
            chosen = (diverse + rest)[:branch]
            parks.sort(key=lambda p: len(p.path))
            for plan in chosen + parks[:park_branch]:
                twin = sim.clone()
                _execute(twin, plan)
                if twin.solved:
                    return SolveResult(True, twin.taps, twin.steps, 1)
                key = twin.state_key()
                if key not in seen:
                    seen.add(key)
                    children.append(twin)
        if not children:
            break
        children.sort(
            key=lambda s: (
                s.remaining,
                sum(b.alive for b in s.blocks),
                s.potential(),
                s.taps,
            )
        )
        beam = children[:width]
        if beam[0].remaining < best_fail.remaining:
            best_fail = beam[0]
    return SolveResult(False, best_fail.taps, best_fail.steps, 1)


def solve(
    level: Level,
    *,
    playouts: int = 32,
    max_taps: int = 400,
    seed: int = 0,
    park_patience: int = 6,
    rules: Rules = Rules(),
    unlock_requires_all_keys: bool = True,
) -> SolveResult:
    """Try to clear a level with randomized greedy playouts.

    Each playout repeatedly picks a block, BFS-plans a drag that covers at least
    one matching settler, and executes it step by step. When no block has an
    absorbing drag, up to ``park_patience`` consecutive random repositioning
    moves are tried to clear a lane (needed e.g. when two blocks must swap
    sides, or a block squats on an elevator cell). The level counts as solved on
    the first clearing playout.

    Args:
        level: The level to attempt.
        playouts: Maximum randomized attempts.
        max_taps: Per-playout drag budget (a generous multiple of corpus taps).
        seed: Base RNG seed; playout ``k`` uses ``seed + k``.
        park_patience: Consecutive non-absorbing moves allowed before giving up.
        unlock_requires_all_keys: Passed through to :class:`Simulator`.

    Returns:
        Result of the first successful playout, or the last failure.
    """
    last = SolveResult(False, 0, 0, 0)
    for k in range(playouts):
        rng = random.Random(seed + k)
        sim = Simulator(level, rules=rules, unlock_requires_all_keys=unlock_requires_all_keys)
        parked = 0
        while not sim.solved and sim.taps < max_taps:
            absorbing, parks = _gather_plans(sim)
            if absorbing:
                parked = 0
                absorbing.sort(key=lambda p: (not p.completes, -p.covered, len(p.path)))
                top = absorbing[: max(1, min(4, len(absorbing)))]
                _execute(sim, rng.choice(top) if k else top[0])
            elif parks and parked < park_patience:
                parked += 1
                _execute(sim, _pick_park(sim, parks, rng))
            else:
                break
        if sim.solved:
            return SolveResult(True, sim.taps, sim.steps, k + 1)
        last = SolveResult(False, sim.taps, sim.steps, k + 1)
    return last


# --------------------------------------------------------------------------- CLI

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"

#: Corpus levels unsolvable under the live rules — stale exports later replaced in
#: the shipped game (see module docstring). Excluded from golden pools.
BROKEN_EXPORTS = frozenset({31, 53, 62, 72, 84, 92})


def _level_files(corpus: Path) -> list[Path]:
    return sorted(corpus.glob("Level *.json"), key=lambda p: int(p.stem.split()[1]))


def main(argv: list[str] | None = None) -> int:
    """Validate and solve every corpus level; print a per-level report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=CORPUS_DIR)
    parser.add_argument("--playouts", type=int, default=32)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--beam", action="store_true", help="use beam search instead of playouts")
    parser.add_argument("--width", type=int, default=256)
    parser.add_argument("--max-taps", type=int, default=200)
    parser.add_argument("--branch", type=int, default=16)
    parser.add_argument("--park-branch", type=int, default=8)
    parser.add_argument("--solid-figures", action="store_true")
    parser.add_argument("--no-absorb-cover", action="store_true")
    parser.add_argument("--no-absorb-adjacent", action="store_true")
    parser.add_argument("--no-teleport", action="store_true")
    parser.add_argument("--single-key", action="store_true")
    parser.add_argument("--elevator-order", choices=("fifo", "lifo", "pool"), default="fifo")
    args = parser.parse_args(argv)
    rules = Rules(
        figures_solid=args.solid_figures,
        absorb_adjacent=not args.no_absorb_adjacent,
        absorb_cover=not args.no_absorb_cover,
        teleport=not args.no_teleport,
        elevator_order=args.elevator_order,
    )

    n_valid = n_solved = n_total = 0
    unexpected: list[str] = []
    for path in _level_files(args.corpus):
        level = Level.load(path)
        number = int(path.stem.split()[1])
        errors = validate(level)
        if args.beam:
            result = solve_beam(
                level,
                width=args.width,
                max_taps=args.max_taps,
                branch=args.branch,
                park_branch=args.park_branch,
                rules=rules,
                unlock_requires_all_keys=not args.single_key,
            )
        else:
            result = solve(
                level,
                playouts=args.playouts,
                seed=args.seed,
                rules=rules,
                unlock_requires_all_keys=not args.single_key,
            )
        n_total += 1
        n_valid += not errors
        n_solved += result.solved
        status = "ok" if not errors else f"INVALID({len(errors)})"
        if result.solved:
            solved = f"solved taps={result.taps}"
        elif number in BROKEN_EXPORTS:
            solved = "UNSOLVED (known-broken export)"
        else:
            solved = "UNSOLVED"
            unexpected.append(level.name)
        print(f"{level.name:<12} {status:<12} {solved}")
        for e in errors:
            print(f"    {e}")
    print(f"\n{n_total} levels: {n_valid} valid, {n_solved} solved")
    if unexpected:
        print("unexpected unsolved:", ", ".join(unexpected))
    return 0 if n_valid == n_total and not unexpected else 1


if __name__ == "__main__":
    raise SystemExit(main())
