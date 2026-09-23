# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Immutable static model parsed from one decoded conveyor shooter level."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from typing import Any

Cell = tuple[int, int]

#: Colour ids are contiguous across board cubes and shooters.
COLOUR_IDS = range(34)

#: First appearance of every mechanic in the shipped progression.
FIRST_APPEARANCE: dict[str, int] = {
    "SurpriseShooters": 8,
    "ConnectedShooters": 14,
    "pixelHealths": 30,
    "keys": 40,
    "Locks": 40,
    "gates": 55,
    "pixelPipes": 70,
    "snakes": 100,
    "walls": 120,
    "eggBoxes": 140,
    "ShooterPipes": 171,
    "pixelIceBlocks": 200,
    "pixelWoodBlocks": 230,
    "Hammers": 230,
    "ShooterIceBlocks": 290,
    "pixelColorDoors": 350,
    "biscuits": 400,
    "splitObjects": 500,
    "ufos": 600,
    "pumpkins": 700,
    "ChainedShooters": 801,
    "curtains": 901,
    "shooterCages": 1001,
    "multiSnakes": 1101,
    "beanBoxes": 1201,
    "BullTotems": 1301,
    "musicToys": 1401,
    "MusicToyMallets": 1401,
    "crossbows": 1501,
    "accordions": 1601,
    "coloredShooterCages": 1701,
    "SlotCages": 1801,
    "matryoshkas": 1901,
    "spaceships": 2001,
    "AstronautShooters": 2001,
}


#: A connection joins at least two shooters.
_MIN_CONNECTED_SHOOTERS = 2


def grid_cells(points: list[dict[str, Any]]) -> list[Cell]:
    """Flatten a flat or grouped native ``GridPoints`` value into cells."""
    cells: list[Cell] = []
    for point in points:
        if "X" in point:
            cells.append((point["X"], point["Y"]))
        elif "x" in point:
            cells.append((point["x"], point["y"]))
        elif isinstance(point.get("GridPoints"), list):
            cells.extend(grid_cells(point["GridPoints"]))
        else:
            raise ValueError(f"grid point is neither a cell nor a group: {sorted(point)}")
    return cells


def board_colours(raw: dict[str, Any]) -> dict[Cell, int]:
    """Expand the native pixel records into a cell-to-material picture."""
    picture: dict[Cell, int] = {}
    for pixel in raw["PixelImageData"]["pixels"]:
        for dx in range(max(1, pixel.get("areaX", 1))):
            for dy in range(max(1, pixel.get("areaY", 1))):
                picture[(pixel["x"] + dx, pixel["y"] + dy)] = pixel["material"]
    return picture


def mechanics_used(raw: dict[str, Any]) -> dict[str, int]:
    """Return non-empty mechanic containers and their entry counts."""
    used: dict[str, int] = {}
    pid = raw["PixelImageData"]
    if pid.get("pixelHealths"):
        used["pixelHealths"] = len(pid["pixelHealths"])
    for source in (pid, raw):
        for name, container in source.items():
            if name not in FIRST_APPEARANCE or not isinstance(container, dict):
                continue
            entries = sum(len(value) for value in container.values() if isinstance(value, list))
            if entries:
                used[name] = entries
    return used


class Direction(StrEnum):
    """Direction a shooter fires from its current conveyor edge."""

    UP = "up"
    LEFT = "left"
    DOWN = "down"
    RIGHT = "right"


@dataclass(frozen=True)
class Shooter:
    """One immutable shooter as supplied by a queue."""

    id: int
    material: int
    ammo: int
    hidden: bool = False
    connection_id: int | None = None
    connection_order: int | None = None


@dataclass(frozen=True)
class Target:
    """One board object with shared health across all occupied cells."""

    material: int
    health: int
    cells: tuple[Cell, ...]
    retraction_length: int | None = None
    unloads_while_aligned: bool = False


@dataclass(frozen=True)
class Key:
    """One board key occupying one or more cells."""

    cells: tuple[Cell, ...]


@dataclass(frozen=True)
class PipeSegment:
    """One consecutive material run in a Pixel Pipe queue."""

    material: int
    count: int


@dataclass(frozen=True)
class PixelPipe:
    """One ordered Pixel Pipe, including its visual footprint and hidden queue."""

    cells: tuple[Cell, ...]
    queue: tuple[PipeSegment, ...]

    @property
    def total_count(self) -> int:
        """Return the aggregate counter shown when the pipe starts."""
        return sum(segment.count for segment in self.queue)

    def material_at(self, consumed: int) -> int | None:
        """Return the visible material after ``consumed`` regenerations."""
        if not 0 <= consumed <= self.total_count:
            raise ValueError(
                f"pipe progress must be between 0 and {self.total_count}, got {consumed}"
            )
        offset = consumed
        for segment in self.queue:
            if offset < segment.count:
                return segment.material
            offset -= segment.count
        return None

    def remaining_count(self, consumed: int) -> int:
        """Return the aggregate number displayed for this pipe."""
        self.material_at(consumed)
        return self.total_count - consumed


@dataclass(frozen=True)
class PixelIceBlock:
    """One colorless overlay weakened by destroying covered pixels."""

    cells: frozenset[Cell]
    health: int


class RayObjectKind(StrEnum):
    """Kinds of board objects ordered along a firing ray."""

    TARGET = "target"
    KEY = "key"
    WALL = "wall"


@dataclass(frozen=True)
class RayObject:
    """One target or key encountered along a firing ray."""

    kind: RayObjectKind
    index: int
    retraction_rank: int | None = None


@dataclass(frozen=True)
class Ray:
    """Targets encountered from one conveyor firing position, edge first."""

    direction: Direction
    line: int
    objects: tuple[RayObject, ...]

    @property
    def targets(self) -> tuple[int, ...]:
        """Target indices in edge-first order, excluding keys."""
        return tuple(item.index for item in self.objects if item.kind is RayObjectKind.TARGET)


def demand_by_material(raw: dict[str, Any]) -> dict[int, int]:
    """Calculate matching-hit demand without building lanes, conveyor rays, or solver state."""
    pid = _mapping(raw, "PixelImageData")
    demand: dict[int, int] = {}

    def add(material: int, count: int) -> None:
        """Add demand for one material."""
        demand[material] = demand.get(material, 0) + count

    def entries(container: str, field: str) -> list[dict[str, Any]]:
        """Read the entry list of one board container."""
        value = pid.get(container, {})
        if not isinstance(value, dict):
            raise ValueError(f"{container} must be an object")
        return _list(value, field)

    health_by_cell = {
        (_int(item, "x"), _int(item, "y")): _positive_int(item, "health")
        for item in _list(pid, "pixelHealths")
    }
    for pixel in _list(pid, "pixels"):
        anchor = (_int(pixel, "x"), _int(pixel, "y"))
        add(_int(pixel, "material"), health_by_cell.get(anchor, 1))
    for record in entries("gates", "Gates"):
        add(_int(record, "Material"), _positive_int(record, "Count"))
    for record in entries("snakes", "Snakes"):
        add(_int(record, "Material"), _positive_int(record, "Count"))
    for record in entries("eggBoxes", "EggBoxes"):
        for egg in _list(record, "Eggs"):
            add(_int(egg, "Material"), _positive_int(egg, "Count"))
    for record in entries("pixelPipes", "Pipes"):
        for segment in _list(record, "Queue"):
            add(_int(segment, "Material"), _positive_int(segment, "Count"))
    return demand


def _parse_targets(
    pid: dict[str, Any], width: int, height: int
) -> tuple[
    list[Target],
    dict[Cell, int],
    dict[Cell, int],
    dict[Cell, tuple[int, int]],
    dict[Cell, tuple[int, int]],
    dict[Cell, int],
]:
    """Parse every demand-bearing board target without constructing gameplay traversal."""
    health_by_cell = {
        (_int(item, "x"), _int(item, "y")): _positive_int(item, "health")
        for item in _list(pid, "pixelHealths")
    }
    pixel_records = sorted(
        _list(pid, "pixels"),
        key=lambda item: (_int(item, "x"), _int(item, "y")),
    )
    targets: list[Target] = []
    cell_targets: dict[Cell, int] = {}
    anchor_targets: dict[Cell, int] = {}
    for record in pixel_records:
        anchor = (_int(record, "x"), _int(record, "y"))
        area_x = int(record.get("areaX", 1))
        area_y = int(record.get("areaY", 1))
        if area_x < 1 or area_y < 1:
            raise ValueError(f"target {anchor} has invalid area {area_x}x{area_y}")
        cells = tuple(
            (x, y)
            for x in range(anchor[0], anchor[0] + area_x)
            for y in range(anchor[1], anchor[1] + area_y)
        )
        for cell in cells:
            if not (0 <= cell[0] < width and 0 <= cell[1] < height):
                raise ValueError(f"target {anchor} occupies out-of-bounds cell {cell}")
            if cell in cell_targets:
                raise ValueError(f"targets overlap at cell {cell}")
        target_index = len(targets)
        targets.append(
            Target(
                material=_int(record, "material"),
                health=health_by_cell.get(anchor, 1),
                cells=cells,
            )
        )
        cell_targets.update(dict.fromkeys(cells, target_index))
        anchor_targets[anchor] = target_index

    gate_targets, cell_gates = _parse_gates(pid, width, height, len(targets))
    targets.extend(gate_targets)
    snake_targets, cell_snakes = _parse_snakes(
        pid,
        width,
        height,
        len(targets),
        set(cell_gates),
    )
    targets.extend(snake_targets)
    egg_targets, cell_eggs = _parse_egg_boxes(
        pid,
        width,
        height,
        len(targets),
        set(cell_gates) | set(cell_snakes) | set(cell_targets),
    )
    targets.extend(egg_targets)
    return targets, cell_targets, cell_eggs, cell_gates, cell_snakes, anchor_targets


@dataclass(frozen=True)
class Level:
    """Static board topology and queue contents for one level."""

    number: int
    width: int
    height: int
    targets: tuple[Target, ...]
    keys: tuple[Key, ...]
    lanes: tuple[tuple[Shooter, ...], ...]
    slot_count: int
    conveyor_limit: int
    lap: tuple[Ray, ...]
    walls: frozenset[Cell] = frozenset()
    pixel_pipes: tuple[PixelPipe, ...] = ()
    pixel_ice_blocks: tuple[PixelIceBlock, ...] = ()
    target_ice_blocks: tuple[tuple[int, ...], ...] = ()
    surprise_target_ids: frozenset[int] = frozenset()
    connections: tuple[tuple[int, ...], ...] = ()
    lock_ids: frozenset[int] = frozenset()
    unsupported: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, number: int = 0) -> Level:
        """Parse the supported core without importing either legacy simulator."""
        pid = _mapping(raw, "PixelImageData")
        width = _positive_int(pid, "width")
        height = _positive_int(pid, "height")
        targets, cell_targets, cell_eggs, cell_gates, cell_snakes, anchor_targets = _parse_targets(
            pid, width, height
        )
        occupied_targets = {
            **cell_targets,
            **cell_eggs,
            **{cell: target for cell, (target, _rank) in (cell_gates | cell_snakes).items()},
        }
        keys, cell_keys = _parse_keys(pid, width, height, occupied_targets)
        walls = _parse_walls(pid, width, height, set(occupied_targets) | set(cell_keys))
        pixel_pipes = _parse_pixel_pipes(pid, width, height)
        pixel_ice_blocks = _parse_pixel_ice_blocks(pid, width, height)
        surprise_target_ids = _parse_surprise_target_ids(pid, width, height, anchor_targets)

        connections, connection_by_shooter = _parse_connections(raw)
        locked_ids = _parse_locked_ids(raw)
        shooter_pipes = _parse_shooter_pipes(raw)
        queue_group = _mapping(raw, "QueueGroup")
        hidden_ids = {
            _int({"id": shooter_id}, "id")
            for shooter_id in raw.get("SurpriseShooters", {}).get("Shooters", [])
        }
        lanes: list[tuple[Shooter, ...]] = []
        shooter_ids: set[int] = set()
        for lane_record in _list(queue_group, "shooterQueues"):
            shooters: list[Shooter] = []
            for shooter in _list(lane_record, "shooters"):
                shooter_id = _int(shooter, "id")
                records = shooter_pipes.pop(shooter_id, (shooter,))
                for record in records:
                    record_id = _int(record, "id")
                    connection_id, connection_order = connection_by_shooter.get(
                        record_id, (None, None)
                    )
                    parsed = Shooter(
                        id=record_id,
                        material=_int(record, "material"),
                        ammo=_nonnegative_int(record, "ammo"),
                        hidden=record_id in hidden_ids,
                        connection_id=connection_id,
                        connection_order=connection_order,
                    )
                    if parsed.id in shooter_ids:
                        raise ValueError(f"duplicate shooter id {parsed.id}")
                    shooter_ids.add(parsed.id)
                    shooters.append(parsed)
            lanes.append(tuple(shooters))

        if shooter_pipes:
            missing = min(shooter_pipes)
            raise ValueError(f"Shooter Pipe token id {missing} is not present in a queue")

        unknown_connected = set(connection_by_shooter) - shooter_ids
        if unknown_connected:
            unknown = min(unknown_connected)
            raise ValueError(f"connected shooter id {unknown} is not present in a queue")
        unknown_locked = locked_ids - shooter_ids
        if unknown_locked:
            unknown = min(unknown_locked)
            raise ValueError(f"locked shooter id {unknown} is not present in a queue")
        connected_locks = locked_ids & set(connection_by_shooter)
        if connected_locks:
            invalid = min(connected_locks)
            raise ValueError(f"queue lock id {invalid} cannot be a connected shooter")

        lap = _build_lap(
            width,
            height,
            cell_targets | cell_eggs,
            cell_keys,
            cell_gates | cell_snakes,
            walls,
        )
        return cls(
            number=number,
            width=width,
            height=height,
            targets=tuple(targets),
            keys=keys,
            lanes=tuple(lanes),
            slot_count=_positive_int(raw, "SlotCount"),
            conveyor_limit=_positive_int(raw, "ConveyorLimit"),
            lap=lap,
            walls=walls,
            pixel_pipes=pixel_pipes,
            pixel_ice_blocks=pixel_ice_blocks,
            target_ice_blocks=tuple(
                tuple(
                    index
                    for index, block in enumerate(pixel_ice_blocks)
                    if set(target.cells) & block.cells
                )
                for target in targets
            ),
            surprise_target_ids=surprise_target_ids,
            connections=connections,
            lock_ids=frozenset(locked_ids),
            unsupported=_unsupported_mechanics(raw, pid),
        )

    @property
    def initial_health(self) -> tuple[int, ...]:
        """Initial health in stable target order."""
        return tuple(target.health for target in self.targets)

    @property
    def demand_by_material(self) -> dict[int, int]:
        """Total initial matching-hit demand per material."""
        demand: dict[int, int] = {}
        for target in self.targets:
            demand[target.material] = demand.get(target.material, 0) + target.health
        for pipe in self.pixel_pipes:
            for segment in pipe.queue:
                demand[segment.material] = demand.get(segment.material, 0) + segment.count
        return demand

    @property
    def ammo_by_material(self) -> dict[int, int]:
        """Total queue ammunition per material."""
        ammo: dict[int, int] = {}
        for shooter in (
            shooter for lane in self.lanes for shooter in lane if shooter.id not in self.lock_ids
        ):
            ammo[shooter.material] = ammo.get(shooter.material, 0) + shooter.ammo
        return ammo


@dataclass(frozen=True)
class QueueSchedulingProfile:
    """Static ingredients that may contribute to a level's scheduling horizon."""

    lane_count: int
    tray_capacity: int
    connected_group_count: int
    connected_shooter_count: int
    lock_depths: tuple[int, ...]
    connected_group_overflow: int
    has_buried_lock: bool


def queue_scheduling_profile(level: Level) -> QueueSchedulingProfile:
    """Measure queue topology without collapsing it into a quality score."""
    lock_depths = tuple(
        index
        for lane in level.lanes
        for index, shooter in enumerate(lane)
        if shooter.id in level.lock_ids
    )
    connected_group_count = len(level.connections)
    return QueueSchedulingProfile(
        lane_count=len(level.lanes),
        tray_capacity=level.slot_count,
        connected_group_count=connected_group_count,
        connected_shooter_count=sum(len(group) for group in level.connections),
        lock_depths=lock_depths,
        connected_group_overflow=max(0, connected_group_count - level.slot_count),
        has_buried_lock=any(depth > 0 for depth in lock_depths),
    )


def _build_lap(  # noqa: PLR0913, PLR0917 - one argument per board layer
    width: int,
    height: int,
    cell_targets: dict[Cell, int],
    cell_keys: dict[Cell, int],
    cell_gates: dict[Cell, tuple[int, int]],
    walls: frozenset[Cell],
) -> tuple[Ray, ...]:
    """Compile the observed bottom-left, anticlockwise conveyor traversal."""

    def ray(  # noqa: PLR0912 - every board layer on one firing line, in order
        direction: Direction, line: int
    ) -> Ray:
        """Compile the ordered objects one firing line meets, edge first."""
        if direction is Direction.UP:
            cells: Iterable[Cell] = ((line, y) for y in range(height))
        elif direction is Direction.LEFT:
            cells = ((x, line) for x in reversed(range(width)))
        elif direction is Direction.DOWN:
            cells = ((line, y) for y in reversed(range(height)))
        else:
            cells = ((x, line) for x in range(width))
        objects: list[RayObject] = []
        for cell in cells:
            wall = cell in walls
            target = cell_targets.get(cell)
            key = cell_keys.get(cell)
            gate = cell_gates.get(cell)
            items: list[RayObject] = []
            if wall:
                items.append(RayObject(RayObjectKind.WALL, -1))
            if gate is not None:
                items.append(RayObject(RayObjectKind.TARGET, gate[0], gate[1]))
            if target is not None:
                items.append(RayObject(RayObjectKind.TARGET, target))
            elif key is not None:
                items.append(RayObject(RayObjectKind.KEY, key))
            for item in items:
                if (
                    objects
                    and objects[-1].kind is item.kind is RayObjectKind.TARGET
                    and objects[-1].index == item.index
                    and objects[-1].retraction_rank is not None
                    and item.retraction_rank is not None
                ):
                    prior = objects[-1]
                    prior_rank = prior.retraction_rank
                    item_rank = item.retraction_rank
                    if prior_rank is None or item_rank is None:  # pragma: no cover - checked above
                        raise ValueError("retracting targets must carry a retraction rank")
                    objects[-1] = RayObject(
                        kind=prior.kind,
                        index=prior.index,
                        retraction_rank=min(prior_rank, item_rank),
                    )
                elif not objects or objects[-1] != item:
                    objects.append(item)
        return Ray(direction=direction, line=line, objects=tuple(objects))

    return tuple(
        [ray(Direction.UP, x) for x in range(width)]
        + [ray(Direction.LEFT, y) for y in range(height)]
        + [ray(Direction.DOWN, x) for x in reversed(range(width))]
        + [ray(Direction.RIGHT, y) for y in reversed(range(height))]
    )


def _unsupported_mechanics(raw: dict[str, Any], pid: dict[str, Any]) -> tuple[str, ...]:
    """Name transition-affecting containers intentionally outside the core milestone."""
    checks = {
        "pixel_wood_blocks": _nested_entries(pid, "pixelWoodBlocks", "WoodBlocks"),
        "pixel_color_doors": _nested_entries(pid, "pixelColorDoors", "Doors"),
        "golden_eggs": _nested_entries(pid, "goldenEggs", "Eggs"),
        "biscuits": _nested_entries(pid, "biscuits", "Biscuits"),
        "split_objects": _nested_entries(pid, "splitObjects", "SplitObjects"),
        "ufos": _nested_entries(pid, "ufos", "Ufos"),
        "pumpkins": _nested_entries(pid, "pumpkins", "Pumpkins"),
        "curtains": _nested_entries(pid, "curtains", "Curtains"),
        "shooter_cages": _nested_entries(pid, "shooterCages", "Cages"),
        "colored_shooter_cages": _nested_entries(pid, "coloredShooterCages", "Cages"),
        "bead_groups": _nested_entries(pid, "beadGroups", "BeadGroups"),
        "multi_snakes": _nested_entries(pid, "multiSnakes", "MultiSnakes"),
        "bean_boxes": _nested_entries(pid, "beanBoxes", "BeanBoxes"),
        "music_toys": _nested_entries(pid, "musicToys", "MusicToys"),
        "crossbows": _nested_entries(pid, "crossbows", "Crossbows"),
        "accordions": _nested_entries(pid, "accordions", "Accordions"),
        "matryoshkas": _nested_entries(pid, "matryoshkas", "Matryoshkas"),
        "spaceships": _nested_entries(pid, "spaceships", "Spaceships"),
        "hammers": _nested_entries(raw, "Hammers", "Shooters"),
        "shooter_ice_blocks": _nested_entries(raw, "ShooterIceBlocks", "IceBlocks"),
        "chained_shooters": _nested_entries(raw, "ChainedShooters", "Chains"),
        "bull_totems": _nested_entries(raw, "BullTotems", "Totems"),
        "music_toy_mallets": _nested_entries(raw, "MusicToyMallets", "Mallets"),
        "slot_cages": _nested_entries(raw, "SlotCages", "Cages"),
        "astronaut_shooters": _nested_entries(raw, "AstronautShooters", "Shooters"),
    }
    return tuple(name for name, present in checks.items() if present)


def _parse_surprise_target_ids(
    pid: dict[str, Any],
    width: int,
    height: int,
    anchor_targets: dict[Cell, int],
) -> frozenset[int]:
    """Map hidden-colour pixel anchors to ordinary target indices."""
    container = pid.get("surprisePixels", {})
    if not isinstance(container, dict):
        raise ValueError("surprisePixels must be an object")
    cells = [(_int(record, "X"), _int(record, "Y")) for record in _list(container, "Pixels")]
    if len(cells) != len(set(cells)):
        raise ValueError("surprisePixels contains a duplicate cell")
    for cell in cells:
        if not (0 <= cell[0] < width and 0 <= cell[1] < height):
            raise ValueError(f"surprise pixel occupies out-of-bounds cell {cell}")
        if cell not in anchor_targets:
            raise ValueError(f"surprise pixel {cell} is not a pixel anchor")
    return frozenset(anchor_targets[cell] for cell in cells)


def _parse_walls(
    pid: dict[str, Any],
    width: int,
    height: int,
    occupied: set[Cell],
) -> frozenset[Cell]:
    """Parse permanent cells that block every firing ray crossing them."""
    container = pid.get("walls", {})
    if not isinstance(container, dict):
        raise ValueError("walls must be an object")
    cells: list[Cell] = []
    for record in _list(container, "Walls"):
        points = _list(record, "GridPoints")
        if not points:
            raise ValueError("wall GridPoints must not be empty")
        cells.extend((_int(point, "X"), _int(point, "Y")) for point in points)
    if len(cells) != len(set(cells)):
        raise ValueError("walls contain a duplicate cell")
    for cell in cells:
        if not (0 <= cell[0] < width and 0 <= cell[1] < height):
            raise ValueError(f"wall occupies out-of-bounds cell {cell}")
        if cell in occupied:
            raise ValueError(f"wall overlaps a board object at {cell}")
    return frozenset(cells)


def _parse_pixel_pipes(
    pid: dict[str, Any],
    width: int,
    height: int,
) -> tuple[PixelPipe, ...]:
    """Parse Pixel Pipes in their serialized left-right-up-down priority order."""
    container = pid.get("pixelPipes", {})
    if not isinstance(container, dict):
        raise ValueError("pixelPipes must be an object")
    pipes: list[PixelPipe] = []
    for record in _list(container, "Pipes"):
        points = _list(record, "GridPoints")
        if not points:
            raise ValueError("Pixel Pipe GridPoints must not be empty")
        cells = tuple((_int(point, "X"), _int(point, "Y")) for point in points)
        if len(cells) != len(set(cells)):
            raise ValueError("Pixel Pipe GridPoints contain a duplicate cell")
        if any(not (0 <= x < width and 0 <= y < height) for x, y in cells):
            raise ValueError("Pixel Pipe GridPoints contain an out-of-bounds cell")
        queue_records = _list(record, "Queue")
        if not queue_records:
            raise ValueError("Pixel Pipe Queue must not be empty")
        queue = tuple(
            PipeSegment(
                material=_int(segment, "Material"),
                count=_positive_int(segment, "Count"),
            )
            for segment in queue_records
        )
        pipes.append(PixelPipe(cells=cells, queue=queue))
    return tuple(pipes)


def _parse_pixel_ice_blocks(
    pid: dict[str, Any],
    width: int,
    height: int,
) -> tuple[PixelIceBlock, ...]:
    """Parse colorless covers whose counters fall with covered pixel destruction."""
    container = pid.get("pixelIceBlocks", {})
    if not isinstance(container, dict):
        raise ValueError("pixelIceBlocks must be an object")
    blocks: list[PixelIceBlock] = []
    occupied: set[Cell] = set()
    for record in _list(container, "IceBlocks"):
        points = _list(record, "GridPoints")
        if not points:
            raise ValueError("Pixel Ice Block GridPoints must not be empty")
        cells = frozenset((_int(point, "X"), _int(point, "Y")) for point in points)
        if len(cells) != len(points):
            raise ValueError("Pixel Ice Block GridPoints contain a duplicate cell")
        if any(not (0 <= x < width and 0 <= y < height) for x, y in cells):
            raise ValueError("Pixel Ice Block GridPoints contain an out-of-bounds cell")
        overlap = occupied & cells
        if overlap:
            raise ValueError(f"Pixel Ice Blocks overlap at cell {min(overlap)}")
        occupied.update(cells)
        _positive_int(record, "BlockSize")
        blocks.append(PixelIceBlock(cells=cells, health=_positive_int(record, "Health")))
    return tuple(blocks)


def _parse_gates(
    pid: dict[str, Any],
    width: int,
    height: int,
    first_target_index: int,
) -> tuple[list[Target], dict[Cell, tuple[int, int]]]:
    """Parse retracting edge gates into health-bearing dynamic targets."""
    container = pid.get("gates", {})
    if not isinstance(container, dict):
        raise ValueError("gates must be an object")
    targets: list[Target] = []
    cell_gates: dict[Cell, tuple[int, int]] = {}
    for record in _list(container, "Gates"):
        points = _list(record, "GridPoints")
        if not points:
            raise ValueError("gate GridPoints must not be empty")
        point_cells = {(_int(point, "X"), _int(point, "Y")) for point in points}
        if any(not (0 <= x < width and 0 <= y < height) for x, y in point_cells):
            raise ValueError("gate GridPoints contain an out-of-bounds cell")
        direction = _int(record, "Direction")
        if direction not in range(4):
            raise ValueError(f"gate has invalid direction {direction}")
        length = _positive_int(record, "Length")
        count = _positive_int(record, "Count")
        xs = sorted({x for x, _y in point_cells})
        ys = sorted({y for _x, y in point_cells})
        if point_cells != {(x, y) for x in xs for y in ys}:
            raise ValueError("gate GridPoints must form a rectangle")

        if direction in (0, 2):
            start = min(xs) if direction == 0 else max(xs)
            step = -1 if direction == 0 else 1
            layers = [tuple((start + step * rank, y) for y in ys) for rank in range(length)]
        else:
            start = max(ys) if direction == 1 else min(ys)
            step = 1 if direction == 1 else -1
            layers = [tuple((x, start + step * rank) for x in xs) for rank in range(length)]
        cells = tuple(cell for layer in layers for cell in layer)
        if any(not (0 <= x < width and 0 <= y < height) for x, y in cells):
            raise ValueError("gate length extends outside the board")

        target_index = first_target_index + len(targets)
        for rank, layer in enumerate(layers):
            for cell in layer:
                if cell in cell_gates:
                    raise ValueError(f"gates overlap at cell {cell}")
                cell_gates[cell] = (target_index, rank)
        targets.append(
            Target(
                material=_int(record, "Material"),
                health=count,
                cells=cells,
                retraction_length=length,
            )
        )
    return targets, cell_gates


def _parse_snakes(
    pid: dict[str, Any],
    width: int,
    height: int,
    first_target_index: int,
    occupied: set[Cell],
) -> tuple[list[Target], dict[Cell, tuple[int, int]]]:
    """Parse a snake as one shared-health body retracting from head to nest."""
    container = pid.get("snakes", {})
    if not isinstance(container, dict):
        raise ValueError("snakes must be an object")
    targets: list[Target] = []
    cell_snakes: dict[Cell, tuple[int, int]] = {}
    for record in _list(container, "Snakes"):
        footprints = [
            _snake_footprint(_list(record, "MainGridPoints"), "MainGridPoints"),
            *(
                _snake_footprint(_list(item, "GridPoints"), "SnakeGridPoints")
                for item in _list(record, "SnakeGridPoints")
            ),
        ]
        if len(footprints) == 1:
            raise ValueError("snake must contain at least one SnakeGridPoints waypoint")
        footprint_width, footprint_height = footprints[0][1:]
        if any(item[1:] != (footprint_width, footprint_height) for item in footprints[1:]):
            raise ValueError("snake waypoints must use the MainGridPoints footprint size")

        anchors = [item[0] for item in footprints]
        layer_anchors = [anchors[0]]
        for start, end in pairwise(anchors):
            dx = end[0] - start[0]
            dy = end[1] - start[1]
            if (dx == 0) == (dy == 0):
                raise ValueError("snake waypoints must form nonzero axis-aligned segments")
            step = (
                0 if dx == 0 else (1 if dx > 0 else -1),
                0 if dy == 0 else (1 if dy > 0 else -1),
            )
            for distance in range(1, max(abs(dx), abs(dy)) + 1):
                layer_anchors.append((start[0] + step[0] * distance, start[1] + step[1] * distance))

        cell_ranks: dict[Cell, int] = {}
        for rank, anchor in enumerate(layer_anchors):
            layer = (
                (x, y)
                for x in range(anchor[0], anchor[0] + footprint_width)
                for y in range(anchor[1], anchor[1] + footprint_height)
            )
            for cell in layer:
                if not (0 <= cell[0] < width and 0 <= cell[1] < height):
                    raise ValueError("snake route extends outside the board")
                cell_ranks.setdefault(cell, rank)
        overlap = occupied & set(cell_ranks) or set(cell_snakes) & set(cell_ranks)
        if overlap:
            raise ValueError(f"snake overlaps another board object at cell {min(overlap)}")

        target_index = first_target_index + len(targets)
        cells = tuple(sorted(cell_ranks, key=lambda cell: (cell_ranks[cell], cell)))
        targets.append(
            Target(
                material=_int(record, "Material"),
                health=_positive_int(record, "Count"),
                cells=cells,
                retraction_length=len(layer_anchors),
            )
        )
        cell_snakes.update((cell, (target_index, rank)) for cell, rank in cell_ranks.items())
    return targets, cell_snakes


def _parse_egg_boxes(
    pid: dict[str, Any],
    width: int,
    height: int,
    first_target_index: int,
    occupied: set[Cell],
) -> tuple[list[Target], dict[Cell, int]]:
    """Parse every visible egg as one colored shared-health compartment."""
    container = pid.get("eggBoxes", {})
    if not isinstance(container, dict):
        raise ValueError("eggBoxes must be an object")
    targets: list[Target] = []
    cell_eggs: dict[Cell, int] = {}
    for record in _list(container, "EggBoxes"):
        points = _list(record, "GridPoints")
        if not points:
            raise ValueError("egg box GridPoints must not be empty")
        cells = {(_int(point, "X"), _int(point, "Y")) for point in points}
        xs = tuple(range(min(x for x, _y in cells), max(x for x, _y in cells) + 1))
        ys = tuple(range(min(y for _x, y in cells), max(y for _x, y in cells) + 1))
        if cells != {(x, y) for x in xs for y in ys}:
            raise ValueError("egg box GridPoints must form a rectangle")
        if any(not (0 <= x < width and 0 <= y < height) for x, y in cells):
            raise ValueError("egg box GridPoints contain an out-of-bounds cell")
        overlap = occupied & cells or set(cell_eggs) & cells
        if overlap:
            raise ValueError(f"egg box overlaps another board object at cell {min(overlap)}")

        area = _mapping(record, "EggArea")
        columns = _positive_int(area, "X")
        rows = _positive_int(area, "Y")
        if len(xs) < columns or len(ys) < rows:
            raise ValueError("egg box footprint must fit every EggArea compartment")
        egg_records = _list(record, "Eggs")
        if len(egg_records) != columns * rows:
            raise ValueError(
                f"egg box EggArea requires {columns * rows} eggs, got {len(egg_records)}"
            )
        for egg_index, egg in enumerate(egg_records):
            column = egg_index % columns
            row = egg_index // columns
            x_start = column * len(xs) // columns
            x_end = (column + 1) * len(xs) // columns
            y_start = row * len(ys) // rows
            y_end = (row + 1) * len(ys) // rows
            egg_cells = tuple((x, y) for x in xs[x_start:x_end] for y in ys[y_start:y_end])
            target_index = first_target_index + len(targets)
            targets.append(
                Target(
                    material=_int(egg, "Material"),
                    health=_positive_int(egg, "Count"),
                    cells=egg_cells,
                    unloads_while_aligned=True,
                )
            )
            cell_eggs.update(dict.fromkeys(egg_cells, target_index))
    return targets, cell_eggs


def _snake_footprint(
    points: list[dict[str, Any]],
    field: str,
) -> tuple[Cell, int, int]:
    """Return the lower-left anchor and size of one rectangular snake waypoint."""
    if not points:
        raise ValueError(f"snake {field} must not be empty")
    cells = {(_int(point, "X"), _int(point, "Y")) for point in points}
    xs = range(min(x for x, _y in cells), max(x for x, _y in cells) + 1)
    ys = range(min(y for _x, y in cells), max(y for _x, y in cells) + 1)
    if cells != {(x, y) for x in xs for y in ys}:
        raise ValueError(f"snake {field} must form a rectangle")
    return (min(xs), min(ys)), len(xs), len(ys)


def _parse_keys(
    pid: dict[str, Any],
    width: int,
    height: int,
    cell_targets: dict[Cell, int],
) -> tuple[tuple[Key, ...], dict[Cell, int]]:
    """Parse key footprints and validate they occupy empty board cells."""
    container = pid.get("keys", {})
    if not isinstance(container, dict):
        raise ValueError("keys must be an object")
    records = _list(container, "Keys")
    keys: list[Key] = []
    cell_keys: dict[Cell, int] = {}
    for record in records:
        points = _list(record, "GridPoints")
        if not points:
            raise ValueError("key GridPoints must not be empty")
        cells = tuple((_int(point, "X"), _int(point, "Y")) for point in points)
        for cell in cells:
            if not (0 <= cell[0] < width and 0 <= cell[1] < height):
                raise ValueError(f"key occupies out-of-bounds cell {cell}")
            if cell in cell_targets:
                raise ValueError(f"key overlaps a target at cell {cell}")
            if cell in cell_keys:
                raise ValueError(f"keys overlap at cell {cell}")
        key_index = len(keys)
        keys.append(Key(cells=cells))
        cell_keys.update(dict.fromkeys(cells, key_index))
    return tuple(keys), cell_keys


def _parse_locked_ids(raw: dict[str, Any]) -> set[int]:
    """Parse locked shooter ids."""
    container = raw.get("Locks", {})
    if not isinstance(container, dict):
        raise ValueError("Locks must be an object")
    shooter_ids = container.get("Shooters", [])
    if not isinstance(shooter_ids, list) or not all(
        isinstance(shooter_id, int) for shooter_id in shooter_ids
    ):
        raise ValueError("locked Shooters must be a list of integer ids")
    if len(shooter_ids) != len(set(shooter_ids)):
        raise ValueError("locked Shooters contains a duplicate id")
    return set(shooter_ids)


def _parse_shooter_pipes(raw: dict[str, Any]) -> dict[int, tuple[dict[str, Any], ...]]:
    """Expand each inline Shooter Pipe token into its stored shooter queue."""
    container = raw.get("ShooterPipes", {})
    if not isinstance(container, dict):
        raise ValueError("ShooterPipes must be an object")
    pipes: dict[int, tuple[dict[str, Any], ...]] = {}
    stored_ids: set[int] = set()
    for record in _list(container, "Pipes"):
        token_id = _int(record, "ShooterId")
        if token_id in pipes:
            raise ValueError(f"duplicate Shooter Pipe token id {token_id}")
        queue = _mapping(record, "Queue")
        shooters = tuple(_list(queue, "shooters"))
        if not shooters:
            raise ValueError(f"Shooter Pipe token id {token_id} has an empty queue")
        for shooter in shooters:
            shooter_id = _int(shooter, "id")
            if shooter_id in stored_ids:
                raise ValueError(f"duplicate stored shooter id {shooter_id}")
            stored_ids.add(shooter_id)
        pipes[token_id] = shooters
    overlap = set(pipes) & stored_ids
    if overlap:
        duplicate = min(overlap)
        raise ValueError(f"Shooter Pipe token id {duplicate} is also a stored shooter")
    return pipes


def _parse_connections(
    raw: dict[str, Any],
) -> tuple[tuple[tuple[int, ...], ...], dict[int, tuple[int, int]]]:
    """Parse connection membership into stable internal group indices."""
    container = raw.get("ConnectedShooters", {})
    if not isinstance(container, dict):
        raise ValueError("ConnectedShooters must be an object")
    records = container.get("Connections", [])
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise ValueError("Connections must be a list of objects")

    external_ids: set[int] = set()
    connections: list[tuple[int, ...]] = []
    membership: dict[int, tuple[int, int]] = {}
    for record in records:
        external_id = _int(record, "Id")
        if external_id in external_ids:
            raise ValueError(f"duplicate connection id {external_id}")
        external_ids.add(external_id)
        shooter_ids = record.get("Shooters")
        if (
            not isinstance(shooter_ids, list)
            or len(shooter_ids) < _MIN_CONNECTED_SHOOTERS
            or not all(isinstance(shooter_id, int) for shooter_id in shooter_ids)
        ):
            raise ValueError("connected Shooters must contain at least two integer ids")
        group_index = len(connections)
        group = tuple(shooter_ids)
        for order, shooter_id in enumerate(group):
            if shooter_id in membership:
                raise ValueError(f"shooter {shooter_id} belongs to multiple connections")
            membership[shooter_id] = (group_index, order)
        connections.append(group)
    return tuple(connections), membership


def _nested_entries(data: dict[str, Any], container: str, entries: str) -> bool:
    """Whether a nested container holds any entries."""
    value = data.get(container, {})
    return isinstance(value, dict) and bool(value.get(entries, []))


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    """Read a required object field."""
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _list(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Read a list-of-objects field."""
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{key} must be a list of objects")
    return value


def _int(data: dict[str, Any], key: str) -> int:
    """Read a required integer field."""
    value = data.get(key)
    if not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _positive_int(data: dict[str, Any], key: str) -> int:
    """Read a required positive integer field."""
    value = _int(data, key)
    if value < 1:
        raise ValueError(f"{key} must be positive, got {value}")
    return value


def _nonnegative_int(data: dict[str, Any], key: str) -> int:
    """Read a required nonnegative integer field."""
    value = _int(data, key)
    if value < 0:
        raise ValueError(f"{key} must be nonnegative, got {value}")
    return value
