# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic native-schema construction utilities for conveyor shooter levels.

The agent decides *what* a board depicts and which mechanics carry it. This module turns
that decision into a byte-valid candidate and derives a colour-balanced queue. Colour balance
does **not** prove that the five-slot queue schedule is solvable; the rebuilt L151 agent accepts
a candidate only after ``certification.py`` finds and replays a win through ``conveyor_game``.

## What construction establishes

The earlier frontier prototype treated pooled per-colour ammo as a solvability proof. That omitted
queue order, conveyor capacity and the five waiting slots. Construction still guarantees two useful
preconditions:

1. **Ammo covers demand in every colour.** Demand is computable before a shooter exists —
   cubes weighted by health, plus the HP of every standalone coloured object, plus what a
   pipe will spawn. So the queues are *derived*, never guessed. `synthesise_queues` does it.
2. **Nothing is sealed.** Only walls can seal a region, and only if a wall ring closes all
   four axes into a pocket. `place_walls` refuses to build one.

The legacy ``save`` helper still re-derives its old structural verdict for offline experiments. It
is not the rebuilt agent's save gate.

## Ammo comes in tens

Shooter ammo across all 2,100 live levels is drawn from exactly ``{10, 20, 30, 40, 50, 60}``
— nothing else — and **1,749 of the 2,100 have every per-colour demand as a multiple of
10**. That is not a coincidence, it is how the studio makes the budget close: pick a picture
whose colour counts land on tens, and the queues can match demand to the unit.

`synthesise_queues` follows it, rounding *up* to the next ten when a colour does not land
there. Rounding up leaves slack, which 514 shipped levels also carry, so it stays inside the
corpus either way — but a board painted to land on tens is the more typical one, and
`round_to_tens` is offered so a caller can ask for it deliberately.

## No fonts, no images, no subprocesses

Shapes are composed from parametric primitives in pure stdlib, with no image library or
system thumbnailer, so everything here runs identically locally and on the hosted runtime.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from conveyor_game import demand_by_material
from conveyor_game.model import COLOUR_IDS, FIRST_APPEARANCE, Cell, grid_cells

#: The only ammo values the corpus uses.
AMMO_VALUES = (10, 20, 30, 40, 50, 60)

#: Every Pixel Pipe queue segment observed in shipped L1-150. The displayed aggregate can be
#: 10-120, but each hidden colour segment is one of these three counts.
PIXEL_PIPE_SEGMENT_COUNTS = (10, 20, 50)

#: Compact 2x2 egg trays in the recent L140-150 window use ten- or twenty-hit eggs.
EGG_BOX_COUNTS = (10, 20)

#: The order to hand out ammo in, reproducing the corpus mix.
#:
#: Measured over every shooter in every third live level: **20 is 55.4%, 10 is 31.0%, 40 is
#: 12.6%**, and 30, 50 and 60 together are under 1%. The median level carries **72
#: shooters**. So the game funds a board with many small shooters, not a few large ones — mean
#: ammo per shooter is about 19.6, and the corpus band for `mean_ammo` at the frontier is
#: 15 to 26.
#:
#: Packing greedily from the largest value instead produces a handful of 60s, a mean of ~51,
#: and a board that is off the corpus distribution on a feature nobody would think to check.
#: This 16-shooter cycle gives 31.2% tens, 56.2% twenties and 12.5% forties.
AMMO_MIX = (20, 20, 10, 20, 40, 20, 10, 20, 20, 10, 20, 40, 20, 10, 20, 10)

#: Queue counts the corpus uses. A level always has at least two lanes.
QUEUE_RANGE = (2, 5)

#: Every level in this build. Read from the level rather than hardcoded when parsing, but
#: emitted as constants when building, because nothing in the corpus varies.
SLOT_COUNT = 5
CONVEYOR_LIMIT = 5


class BuildError(Exception):
    """A construction that cannot produce a shippable level."""


@dataclass
class Design:
    """A level under construction.

    Args:
        target_level: Position in the progression this level claims. Gates which mechanics
            may be placed, and which corpus band it will be graded against.
        width: Grid width.
        height: Grid height.
        picture: Cell -> colour id. The artwork, and the thing a player sees.
        healths: Cell -> hits required, for cells that take more than one.
        board: `PixelImageData` mechanic container name -> its entries.
        top: Level-object mechanic container name -> its value, set directly.
        difficulty: Payout tier to stamp. Not a design target.
    """

    target_level: int
    width: int
    height: int
    picture: dict[Cell, int] = field(default_factory=dict)
    healths: dict[Cell, int] = field(default_factory=dict)
    board: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    top: dict[str, Any] = field(default_factory=dict)
    difficulty: str = "Hard"

    def occupied(self) -> set[Cell]:
        """Every cell holding a cube or a standalone object."""
        cells = set(self.picture)
        for entries in self.board.values():
            for entry in entries:
                points = entry.get("GridPoints") or entry.get("MainGridPoints") or []
                cells.update(grid_cells(points))
        return cells

    def free(self) -> set[Cell]:
        """Every in-grid cell nothing stands on."""
        everything = {(x, y) for x in range(self.width) for y in range(self.height)}
        return everything - self.occupied()

    def colours(self) -> set[int]:
        """Colours used by the artwork."""
        return set(self.picture.values())


# --------------------------------------------------------------------------- editing


def paint(design: Design, cells: set[Cell], colour: int) -> Design:
    """Lay a colour over cells, replacing whatever was there.

    Args:
        design: The design to modify, in place.
        cells: Where to paint.
        colour: Colour id, 0-33.

    Returns:
        The same design, for chaining.

    Raises:
        BuildError: If the colour is outside the palette.
    """
    if colour not in COLOUR_IDS:
        raise BuildError(f"colour {colour} is outside the palette 0-33")
    for cell in cells:
        if 0 <= cell[0] < design.width and 0 <= cell[1] < design.height:
            design.picture[cell] = colour
    return design


def erase(design: Design, cells: set[Cell]) -> Design:
    """Clear cells back to background."""
    for cell in cells:
        design.picture.pop(cell, None)
        design.healths.pop(cell, None)
    return design


def add_art_frame(design: Design, materials: tuple[int, ...]) -> Design:
    """Fill empty border rings without overwriting the source composition.

    The first material is the outermost ring, the second is one cell inward, and so on.
    This is artwork, not a wall mechanic.
    """
    if not materials:
        raise BuildError("an art frame needs at least one material")
    invalid = [material for material in materials if material not in COLOUR_IDS]
    if invalid:
        raise BuildError(f"frame materials outside the palette 0-33: {invalid}")
    max_depth = min(design.width, design.height) // 2
    if len(materials) > max_depth:
        raise BuildError(f"frame depth {len(materials)} exceeds grid capacity {max_depth}")
    free = design.free()
    for x in range(design.width):
        for y in range(design.height):
            distance = min(x, y, design.width - 1 - x, design.height - 1 - y)
            if distance < len(materials) and (x, y) in free:
                design.picture[(x, y)] = materials[distance]
    return design


def fill_empty_art(design: Design, material: int) -> Design:
    """Turn every currently empty grid cell into an ordinary coloured cube."""
    if material not in COLOUR_IDS:
        raise BuildError(f"background material {material} is outside the palette 0-33")
    for cell in design.free():
        design.picture[cell] = material
    return design


# --------------------------------------------------------------------------- mechanics


def add_board_mechanic(
    design: Design, container: str, entry: dict[str, Any], *, allow_overlay: bool = False
) -> Design:
    """Place one entry of a `PixelImageData` mechanic.

    Args:
        design: The design to modify.
        container: The container name, e.g. ``"crossbows"``.
        entry: The entry, already shaped for the wire format.
        allow_overlay: Permit an entry whose cells sit on artwork. Correct for the overlay
            family (ice, wood, doors, curtains, accordions, music toys, rockets) and wrong
            for everything else — a standalone object on a cube would double-occupy a cell.

    Returns:
        The same design.

    Raises:
        BuildError: If the mechanic is from later in the progression than the target level,
            or its cells collide with something when they should not.
    """
    debut = FIRST_APPEARANCE.get(container)
    if debut is None:
        raise BuildError(f"{container} is not a mechanic this game has")
    if debut > design.target_level:
        raise BuildError(
            f"{container} first appears at L{debut}, after the target L{design.target_level}"
        )

    cells = set(grid_cells(entry.get("GridPoints") or entry.get("MainGridPoints") or []))
    outside = {c for c in cells if not (0 <= c[0] < design.width and 0 <= c[1] < design.height)}
    if outside:
        raise BuildError(f"{container} entry leaves the grid at {sorted(outside)[:4]}")
    if not allow_overlay:
        clash = cells & design.occupied()
        if clash:
            raise BuildError(
                f"{container} is a standalone object and cannot sit on artwork; "
                f"{len(clash)} cells collide, first at {sorted(clash)[0]}"
            )

    design.board.setdefault(container, []).append(entry)
    return design


def place_key(design: Design, lower_left: Cell, *, replace_art: bool = True) -> Design:
    """Turn one deliberate 2x2 image region into a collectible key.

    Keys in the shipped corpus occupy exactly 2x2 cells. Source-backed design commonly uses
    them as ornaments, buttons or repeated image details, so replacing art is an explicit
    operation rather than an accidental overlap.

    Args:
        design: The design to modify.
        lower_left: Native lower-left coordinate of the key footprint.
        replace_art: Remove any ordinary pixels under the key before placing it.

    Returns:
        The same design.

    Raises:
        BuildError: If the footprint leaves the board or collides when replacement is disabled.
    """
    x0, y0 = lower_left
    cells = {(x, y) for x in range(x0, x0 + 2) for y in range(y0, y0 + 2)}
    outside = {
        cell for cell in cells if not (0 <= cell[0] < design.width and 0 <= cell[1] < design.height)
    }
    if outside:
        raise BuildError(f"2x2 key at {lower_left} leaves the grid at {sorted(outside)}")
    if replace_art:
        erase(design, cells)
    entry = {"GridPoints": [{"X": x, "Y": y} for x in range(x0, x0 + 2) for y in range(y0, y0 + 2)]}
    return add_board_mechanic(design, "keys", entry)


def place_egg_box(
    design: Design,
    lower_left: Cell,
    materials: tuple[int, int, int, int],
    *,
    count: int = 20,
    replace_art: bool = True,
) -> Design:
    """Place one corpus-shaped 4x4 tray containing four 2x2 egg compartments.

    The recipe is the compact form shipped repeatedly in Levels 140, 143 and 145. Each egg is a
    shared-health coloured target, so its complete 2x2 compartment disappears after ``count``
    matching shots.

    Args:
        design: The design to modify.
        lower_left: Native lower-left coordinate of the 4x4 tray.
        materials: Four egg colours in bottom-left, bottom-right, top-left, top-right order.
        count: Health of every egg; ten and twenty are the admitted recent-corpus values.
        replace_art: Remove ordinary art under the tray before placement.

    Returns:
        The same design.

    Raises:
        BuildError: If the tray leaves the board, uses an unsupported count, or collides with art.
    """
    if count not in EGG_BOX_COUNTS:
        raise BuildError(f"egg box count must be one of {EGG_BOX_COUNTS}, got {count}")
    x0, y0 = lower_left
    cells = {(x, y) for x in range(x0, x0 + 4) for y in range(y0, y0 + 4)}
    outside = {
        cell for cell in cells if not (0 <= cell[0] < design.width and 0 <= cell[1] < design.height)
    }
    if outside:
        raise BuildError(f"4x4 egg box at {lower_left} leaves the grid at {sorted(outside)}")
    if replace_art:
        erase(design, cells)
    entry = {
        "GridPoints": [{"X": x, "Y": y} for x, y in sorted(cells)],
        "EggArea": {"X": 2, "Y": 2},
        "Eggs": [{"Material": material, "Count": count} for material in materials],
    }
    return add_board_mechanic(design, "eggBoxes", entry)


# --------------------------------------------------------------------------- the budget


def split_ammo(total: int) -> list[int]:
    """Break an ammo total into shooters, in the proportions the corpus uses.

    Follows `AMMO_MIX` rather than packing greedily, because how ammo is *divided* is itself
    a corpus-visible feature: the studio funds a board with many small shooters, and a board
    funded by a few big ones lands outside the `mean_ammo` band.

    Args:
        total: Ammo to supply. Rounded up to a multiple of ten, since every value the game
            uses is one.

    Returns:
        Ammo per shooter, summing to the rounded total.
    """
    remaining = math.ceil(total / 10) * 10
    out: list[int] = []
    position = 0
    while remaining > 0:
        value = AMMO_MIX[position % len(AMMO_MIX)]
        position += 1
        if value > remaining:
            value = remaining if remaining in AMMO_VALUES else 10
        out.append(value)
        remaining -= value
    return out


def synthesise_queues(
    design: Design, queues: int = 4, *, caged: dict[int, int] | None = None
) -> list[list[dict[str, Any]]]:
    """Derive the lanes from what the board demands, rather than guessing them.

    Demand is fully computable before any shooter exists, so the queues can cover it per colour.
    This is necessary but not sufficient: queue order and five-slot pressure can still make the
    resulting level unsolvable. ``certification.certify`` establishes that separately.

    Args:
        design: The finished board.
        queues: How many lanes to spread the shooters across, 2-5. Clamped down when the
            board demands fewer shooters than that; the evaluator judges whether the
            result is corpus-shaped.
        caged: Colour -> ammo already supplied by caged shooters on the board, which is
            deducted from what the queues must carry.

    Returns:
        One list of shooter records per queue.

    Raises:
        BuildError: If the queue count is outside the corpus range, or the board demands
            nothing at all.
    """
    low, high = QUEUE_RANGE
    if not low <= queues <= high:
        raise BuildError(f"{queues} queues is outside the corpus range {low}-{high}")

    demand = demand_by_material(to_level(design, queues=[[]]))
    if not demand:
        raise BuildError("the board demands nothing; paint something first")

    supplied = caged or {}
    shooters: list[dict[str, Any]] = []
    next_id = 0
    for colour in sorted(demand):
        outstanding = demand[colour] - supplied.get(colour, 0)
        for ammo in split_ammo(max(0, outstanding)):
            shooters.append({"id": next_id, "ammo": ammo, "material": colour})
            next_id += 1

    # Fewer shooters than lanes is not a build error. A board too small to fill four lanes
    # is a board the *evaluator* should reject, on grid bounds and on the `queues` band —
    # raising here instead would make the builder second-guess the gate, and would turn a
    # legitimate small test board into an exception rather than a verdict.
    lanes: list[list[dict[str, Any]]] = [[] for _ in range(min(queues, len(shooters)))]
    for position, shooter in enumerate(shooters):
        lanes[position % len(lanes)].append(shooter)
    return lanes


def _frontier(picture: dict[Cell, int]) -> set[Cell]:
    """Return pixels currently first on at least one unobstructed firing line."""
    result: set[Cell] = set()
    by_row: dict[int, list[int]] = {}
    by_column: dict[int, list[int]] = {}
    for x, y in picture:
        by_row.setdefault(y, []).append(x)
        by_column.setdefault(x, []).append(y)
    for y, xs in by_row.items():
        result.update({(min(xs), y), (max(xs), y)})
    for x, ys in by_column.items():
        result.update({(x, min(ys)), (x, max(ys))})
    return result


def _peel_phases(picture: dict[Cell, int]) -> list[tuple[int, int]]:
    """Plan blocker-first material phases against exact board geometry."""
    remaining = dict(picture)
    phases: list[tuple[int, int]] = []
    while remaining:
        frontier = _frontier(remaining)
        counts: dict[int, int] = {}
        for cell in frontier:
            material = remaining[cell]
            counts[material] = counts.get(material, 0) + 1
        material = max(counts, key=lambda value: (counts[value], -value))
        removed = 0
        while True:
            matching = {cell for cell in _frontier(remaining) if remaining[cell] == material}
            if not matching:
                break
            removed += len(matching)
            for cell in matching:
                remaining.pop(cell)
        phases.append((material, removed))
    return phases


def blocker_first_queues(design: Design, queues: int = 4) -> list[list[dict[str, Any]]]:
    """Order funded shooters by the image's exposed blockers before expensive search.

    The arrangement follows the strategy reported by the human player: identify the colour
    blocking the most other colours, surface that shooter early, and use the newly exposed targets
    to clear temporarily parked shooters. It preserves exact per-material funding from
    :func:`synthesise_queues` and distributes each phase across two visible fronts.
    """
    funded = [dict(shooter) for lane in synthesise_queues(design, queues) for shooter in lane]
    buckets: dict[int, list[dict[str, Any]]] = {}
    for shooter in funded:
        buckets.setdefault(int(shooter["material"]), []).append(shooter)
    phases: list[list[dict[str, Any]]] = []
    for material, removed in _peel_phases(design.picture):
        phase: list[dict[str, Any]] = []
        phase_ammo = 0
        while buckets.get(material) and phase_ammo < removed:
            shooter = buckets[material].pop(0)
            phase.append(shooter)
            phase_ammo += int(shooter["ammo"])
        phases.append(phase)
    remainder: list[dict[str, Any]] = []
    for material in sorted(buckets):
        remainder.extend(buckets[material])
    phases.append(remainder)

    lanes: list[list[dict[str, Any]]] = [[] for _ in range(queues)]
    lane_cursor = 0
    for phase in phases:
        for shooter in phase:
            lanes[lane_cursor % queues].append(shooter)
            lane_cursor += 1
    return lanes


def apply_queue_treatment(
    design: Design,
    lanes: list[list[dict[str, Any]]],
    *,
    lock_count: int = 0,
    connected_groups: int = 0,
    surprise_count: int = 0,
) -> dict[str, int]:
    """Apply queue mechanics to explicit, already-ordered lanes.

    Connected shooters are selected only from the same depth in different lanes. That makes the
    group structurally launchable when its members reach their lane fronts. A lock is inserted as
    its own queue blocker token; it never replaces a funded shooter. Every lock is backed by one
    board key and no lock token belongs to a connected group.

    Args:
        design: The design whose top-level mechanic containers will be updated.
        lanes: Final queue lanes; their ordering must not change after this call.
        lock_count: Number of buried queue blockers to create.
        connected_groups: Number of two-shooter aligned groups to create.
        surprise_count: Number of shooters whose colour is hidden in the visible queue.

    Returns:
        Exact counts applied for the three mechanic families.

    Raises:
        BuildError: If counts are negative, locks outnumber keys, or the lanes cannot supply the
            requested non-overlapping treatment.
    """
    requested = (lock_count, connected_groups, surprise_count)
    if any(count < 0 for count in requested):
        raise BuildError(f"queue mechanic counts must be nonnegative, got {requested}")
    key_count = len(design.board.get("keys", []))
    if lock_count > key_count:
        raise BuildError(f"{lock_count} locks need at least as many board keys, found {key_count}")
    if not lanes or any(not lane for lane in lanes):
        raise BuildError("queue treatment requires every emitted lane to be non-empty")

    next_id = max(int(shooter["id"]) for lane in lanes for shooter in lane) + 1
    lock_ids: list[int] = []
    for lock_index in range(lock_count):
        lane_index = lock_index % len(lanes)
        lane = lanes[lane_index]
        depth = min(2 + lock_index // len(lanes), len(lane))
        reference = lane[min(depth, len(lane) - 1)]
        lock_id = next_id + lock_index
        lane.insert(
            depth,
            {
                "id": lock_id,
                "ammo": 20,
                "material": int(reference["material"]),
            },
        )
        lock_ids.append(lock_id)
    locked = set(lock_ids)

    max_depth = max(len(lane) for lane in lanes)
    used_connected: set[int] = set()
    connections: list[dict[str, Any]] = []
    for depth in range(max_depth if connected_groups else 0):
        for first_lane in range(len(lanes)):
            second_lane = (first_lane + 1) % len(lanes)
            if depth >= len(lanes[first_lane]) or depth >= len(lanes[second_lane]):
                continue
            members = (
                int(lanes[first_lane][depth]["id"]),
                int(lanes[second_lane][depth]["id"]),
            )
            if set(members) & (locked | used_connected):
                continue
            connections.append({"Id": len(connections), "Shooters": [members[0], members[1]]})
            used_connected.update(members)
            if len(connections) == connected_groups:
                break
        if len(connections) == connected_groups:
            break
    if len(connections) != connected_groups:
        raise BuildError(
            f"requested {connected_groups} aligned connected groups, built {len(connections)}"
        )

    breadth_first_ids = [
        int(lane[depth]["id"])
        for depth in range(max_depth)
        for lane in lanes
        if depth < len(lane)
        if int(lane[depth]["id"]) not in locked
    ]
    if len(breadth_first_ids) < surprise_count:
        raise BuildError(
            f"requested {surprise_count} surprise shooters, only {len(breadth_first_ids)} unlocked "
            "shooters are available"
        )

    design.top["Locks"] = {"Shooters": lock_ids}
    design.top["ConnectedShooters"] = {"Connections": connections}
    design.top["SurpriseShooters"] = {"Shooters": breadth_first_ids[:surprise_count]}
    return {
        "locks": len(lock_ids),
        "connected_groups": len(connections),
        "surprises": surprise_count,
    }


# --------------------------------------------------------------------------- emission


def to_level(design: Design, *, queues: list[list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Emit the wire format.

    Every one of the 21 top-level keys and 34 `PixelImageData` keys is present, empty where
    unused, because that is how every shipped level looks — an unused mechanic is ``[]``,
    never a missing key.

    Args:
        design: The design to emit.
        queues: Lanes to use. Derived from demand when ``None``.

    Returns:
        A decoded level object, ready to be judged or written.
    """
    lanes = queues if queues is not None else synthesise_queues(design)

    pid: dict[str, Any] = {
        "width": design.width,
        "height": design.height,
        "physicalWidth": float(design.width),
        "physicalHeight": float(design.height),
        "pixels": [
            {"x": x, "y": y, "material": colour, "areaX": 1, "areaY": 1}
            for (x, y), colour in sorted(design.picture.items())
        ],
        "pixelHealths": [
            {"x": x, "y": y, "health": health} for (x, y), health in sorted(design.healths.items())
        ],
        "contentHash": 0,
        "Preview": None,
    }
    for container, wrapper in BOARD_WRAPPERS.items():
        pid[container] = {wrapper: design.board.get(container, [])}

    level: dict[str, Any] = {
        "Difficulty": design.difficulty,
        "HasTimeLimit": False,
        "TimeLimit": 0.0,
        "SlotCount": SLOT_COUNT,
        "ConveyorLimit": CONVEYOR_LIMIT,
        "QueueGroup": {"shooterQueues": [{"shooters": lane} for lane in lanes]},
        "PixelImageData": pid,
        "contentHash": 0,
        "isValid": True,
        "validationErrors": "",
    }
    for container, wrapper in TOP_WRAPPERS.items():
        level[container] = design.top.get(container, {wrapper: []})
    return level


#: Container -> the single key its entries live under. Public because `toolkit.py`
#: legitimately needs to know which family a container belongs to, and a private name would
#: only mean it got reimplemented there. Taken from SCHEMA.md; `pixelHealths` is absent
#: because it is a bare list rather than a wrapper.
BOARD_WRAPPERS = {
    "keys": "Keys",
    "gates": "Gates",
    "pixelPipes": "Pipes",
    "surprisePixels": "Pixels",
    "snakes": "Snakes",
    "walls": "Walls",
    "eggBoxes": "EggBoxes",
    "pixelIceBlocks": "IceBlocks",
    "pixelWoodBlocks": "WoodBlocks",
    "pixelColorDoors": "Doors",
    "biscuits": "Biscuits",
    "splitObjects": "SplitObjects",
    "ufos": "Ufos",
    "pumpkins": "Pumpkins",
    "curtains": "Curtains",
    "shooterCages": "Cages",
    "multiSnakes": "MultiSnakes",
    "beanBoxes": "BeanBoxes",
    "musicToys": "MusicToys",
    "crossbows": "Crossbows",
    "accordions": "Accordions",
    "coloredShooterCages": "Cages",
    "matryoshkas": "Matryoshkas",
    "spaceships": "Spaceships",
    "goldenEggs": "GoldenEggs",
    "beadGroups": "BeadGroups",
}

TOP_WRAPPERS = {
    "SurpriseShooters": "Shooters",
    "ConnectedShooters": "Connections",
    "Locks": "Shooters",
    "ShooterPipes": "Pipes",
    "Hammers": "Shooters",
    "ShooterIceBlocks": "IceBlocks",
    "ChainedShooters": "Chains",
    "BullTotems": "Totems",
    "MusicToyMallets": "Mallets",
    "SlotCages": "Cages",
    "AstronautShooters": "Shooters",
}


__all__ = [
    "AMMO_MIX",
    "AMMO_VALUES",
    "BOARD_WRAPPERS",
    "CONVEYOR_LIMIT",
    "EGG_BOX_COUNTS",
    "PIXEL_PIPE_SEGMENT_COUNTS",
    "QUEUE_RANGE",
    "SLOT_COUNT",
    "TOP_WRAPPERS",
    "BuildError",
    "Design",
    "add_art_frame",
    "add_board_mechanic",
    "apply_queue_treatment",
    "blocker_first_queues",
    "erase",
    "fill_empty_art",
    "paint",
    "place_egg_box",
    "place_key",
    "split_ammo",
    "synthesise_queues",
    "to_level",
]
