# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Candidate deconstruction dimensions for the first 150 conveyor shooter levels.

This module does not score quality. It turns a shipped board and one certified solution into
separate observations about the opening artwork, its destruction, and the shooter choreography.
Human review decides which observations matter for each school of level design.
"""

import math
import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Any

from conveyor_game import Action, ActionKind, Engine, GameRules, GameState, Level, Outcome

Cell = tuple[int, int]

STAGE_PROGRESS = (0.0, 0.25, 0.5, 0.75)


@dataclass(frozen=True)
class Region:
    """One four-connected, same-material region in the opening artwork."""

    material: int
    cells: frozenset[Cell]


@dataclass(frozen=True)
class Stage:
    """One replay state nearest a requested fraction of completed work."""

    requested_progress: float
    progress: float
    action_index: int
    state: GameState
    live_cells: dict[Cell, int]
    regions_per_100_cells: float
    colours_remaining: int
    largest_region_share: float


@dataclass(frozen=True)
class ReplayProfile:
    """Measured solution trajectory and the states used by the visual filmstrip."""

    stages: tuple[Stage, ...]
    removal_phase: dict[Cell, float]
    metrics: dict[str, float]
    final_state: GameState


def opening_picture(raw: dict[str, Any]) -> dict[Cell, int]:
    """Expand every serialized artwork pixel into its occupied grid cells."""
    picture: dict[Cell, int] = {}
    records = raw["PixelImageData"].get("pixels", [])
    for record in records:
        x0 = int(record["x"])
        y0 = int(record["y"])
        material = int(record["material"])
        width = int(record.get("areaX", 1))
        height = int(record.get("areaY", 1))
        for x in range(x0, x0 + width):
            for y in range(y0, y0 + height):
                picture[(x, y)] = material
    return picture


def physical_opening_cells(raw: dict[str, Any]) -> set[Cell]:
    """Return ordinary artwork plus serialized board-mechanic footprints.

    Artwork metrics intentionally describe coloured pixels. Physical density is different: a
    key, pipe, wall root or other visible object occupies grid space even when it replaces an
    ordinary cube. Every nested ``GridPoints`` and ``MainGridPoints`` footprint is included.
    """
    cells = set(opening_picture(raw))

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for name, child in value.items():
                if name in {"GridPoints", "MainGridPoints"} and isinstance(child, list):
                    for point in child:
                        if isinstance(point, dict) and "X" in point and "Y" in point:
                            cells.add((int(point["X"]), int(point["Y"])))
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(raw["PixelImageData"])
    return cells


def regions(picture: dict[Cell, int]) -> tuple[Region, ...]:
    """Return all four-connected same-material regions in stable cell order."""
    seen: set[Cell] = set()
    result: list[Region] = []
    for start in sorted(picture):
        if start in seen:
            continue
        material = picture[start]
        pending = [start]
        cells: set[Cell] = {start}
        seen.add(start)
        while pending:
            x, y = pending.pop()
            for neighbour in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if neighbour not in seen and picture.get(neighbour) == material:
                    seen.add(neighbour)
                    cells.add(neighbour)
                    pending.append(neighbour)
        result.append(Region(material=material, cells=frozenset(cells)))
    return tuple(result)


def _runs(picture: dict[Cell, int], width: int, height: int, *, vertical: bool) -> list[int]:
    """Return lengths of non-empty, same-material runs on one grid axis."""
    result: list[int] = []
    outer = width if vertical else height
    inner = height if vertical else width
    for first in range(outer):
        previous: int | None = None
        length = 0
        for second in range(inner):
            cell = (first, second) if vertical else (second, first)
            material = picture.get(cell)
            if material is not None and material == previous:
                length += 1
            else:
                if length:
                    result.append(length)
                length = 1 if material is not None else 0
            previous = material
        if length:
            result.append(length)
    return result


def _mirror_score(picture: dict[Cell, int], width: int, height: int, *, vertical: bool) -> float:
    """Measure exact material agreement under one axial reflection."""
    if not picture:
        return 0.0
    if vertical:
        matches = sum(
            picture.get((x, height - 1 - y)) == material for (x, y), material in picture.items()
        )
    else:
        matches = sum(
            picture.get((width - 1 - x, y)) == material for (x, y), material in picture.items()
        )
    return matches / len(picture)


def _entropy(counts: Counter[int]) -> float:
    """Return normalized material entropy in the closed interval zero to one."""
    total = sum(counts.values())
    if total == 0 or len(counts) < 2:
        return 0.0
    entropy = -sum((count / total) * math.log(count / total) for count in counts.values())
    return entropy / math.log(len(counts))


def _local_colour_diversity(picture: dict[Cell, int], radius: int = 1) -> float:
    """Return mean distinct materials visible around each occupied cell."""
    diversities = []
    for x, y in picture:
        materials = {
            picture[(neighbour_x, neighbour_y)]
            for neighbour_x in range(x - radius, x + radius + 1)
            for neighbour_y in range(y - radius, y + radius + 1)
            if (neighbour_x, neighbour_y) in picture
        }
        diversities.append(len(materials))
    return statistics.fmean(diversities) if diversities else 0.0


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Return Pearson correlation, preserving constant observations as zero."""
    if len(xs) < 2:
        return 0.0
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    dx = [value - mean_x for value in xs]
    dy = [value - mean_y for value in ys]
    denominator = math.sqrt(sum(value * value for value in dx) * sum(value * value for value in dy))
    if denominator == 0:
        return 0.0
    return sum(x * y for x, y in zip(dx, dy, strict=True)) / denominator


def _ring_cells(width: int, height: int, depth: int) -> tuple[Cell, ...]:
    """Return one rectangular grid ring in clockwise order without duplicate corners."""
    left = depth
    right = width - 1 - depth
    bottom = depth
    top = height - 1 - depth
    if left > right or bottom > top:
        return ()
    if bottom == top:
        return tuple((x, bottom) for x in range(left, right + 1))
    if left == right:
        return tuple((left, y) for y in range(bottom, top + 1))
    return (
        *((x, top) for x in range(left, right + 1)),
        *((right, y) for y in range(top - 1, bottom - 1, -1)),
        *((x, bottom) for x in range(right - 1, left - 1, -1)),
        *((left, y) for y in range(bottom + 1, top)),
    )


def _principal_axis(cells: set[Cell]) -> tuple[float, float]:
    """Return inferred axis anisotropy and displacement from the nearest grid axis."""
    if len(cells) < 2:
        return 0.0, 0.0
    xs = [float(x) for x, _y in cells]
    ys = [float(y) for _x, y in cells]
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    variance_x = statistics.fmean((x - mean_x) ** 2 for x in xs)
    variance_y = statistics.fmean((y - mean_y) ** 2 for y in ys)
    covariance = statistics.fmean((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    discriminant = math.hypot(variance_x - variance_y, 2 * covariance)
    total_variance = variance_x + variance_y
    if total_variance == 0:
        return 0.0, 0.0
    angle = math.degrees(math.atan2(2 * covariance, variance_x - variance_y) / 2) % 180
    axis_angle = angle % 90
    displacement = min(axis_angle, 90 - axis_angle)
    return discriminant / total_variance, displacement


def composition_metrics(
    picture: dict[Cell, int], mechanic_cells: set[Cell], width: int, height: int
) -> dict[str, float]:
    """Measure border, outer-field, and inferred interior-subject composition.

    The interior subject is deliberately a proxy: it contains ordinary same-material regions
    that do not touch a grid edge. This is useful for finding tilted or tightly framed subjects,
    but it is not a semantic foreground segmentation and must remain visually reviewed.
    """
    if width < 1 or height < 1:
        raise ValueError("composition dimensions must be positive")
    if not picture:
        raise ValueError("composition metrics require at least one artwork pixel")

    occupied = set(picture) | mechanic_cells
    outer_ring = _ring_cells(width, height, 0)
    outer_ordinary = [cell for cell in outer_ring if cell in picture]
    outer_mechanics = [cell for cell in outer_ring if cell in mechanic_cells]
    transition_pairs = [
        (picture.get(cell), picture.get(outer_ring[(index + 1) % len(outer_ring)]))
        for index, cell in enumerate(outer_ring)
    ]
    comparable_pairs = [
        (left, right) for left, right in transition_pairs if None not in (left, right)
    ]
    colour_transitions = sum(left != right for left, right in comparable_pairs)

    complete_rings = 0
    for depth in range((min(width, height) + 1) // 2):
        ring = _ring_cells(width, height, depth)
        if not ring or len(occupied & set(ring)) / len(ring) < 0.85:
            break
        complete_rings += 1

    frame_depth = min(4, max(2, math.ceil(min(width, height) * 0.12)))
    frame_zone = {
        (x, y)
        for x in range(width)
        for y in range(height)
        if min(x, y, width - 1 - x, height - 1 - y) < frame_depth
    }

    region_set = regions(picture)
    outer_regions = [
        region
        for region in region_set
        if any(x in {0, width - 1} or y in {0, height - 1} for x, y in region.cells)
    ]
    outer_field = (
        set().union(*(region.cells for region in outer_regions)) if outer_regions else set()
    )
    outer_counts = Counter(picture[cell] for cell in outer_field)
    interior_proxy = set(picture) - outer_field
    anisotropy, angular_displacement = _principal_axis(interior_proxy)
    min_clearance = min(
        (min(x, y, width - 1 - x, height - 1 - y) / min(width, height) for x, y in interior_proxy),
        default=0.0,
    )

    return {
        "outer_border_coverage": len(occupied & set(outer_ring)) / len(outer_ring),
        "outer_border_ordinary_share": len(outer_ordinary) / len(outer_ring),
        "outer_border_mechanic_share": len(outer_mechanics) / len(outer_ring),
        "outer_border_materials": float(len({picture[cell] for cell in outer_ordinary})),
        "outer_border_colour_transitions": (
            colour_transitions / len(comparable_pairs) if comparable_pairs else 0.0
        ),
        "complete_border_rings_85": float(complete_rings),
        "frame_zone_depth": float(frame_depth),
        "frame_zone_ordinary_share": len(set(picture) & frame_zone) / len(frame_zone),
        "frame_zone_mechanic_share": len(mechanic_cells & frame_zone) / len(frame_zone),
        "frame_zone_materials": float(
            len({material for cell, material in picture.items() if cell in frame_zone})
        ),
        "outer_field_share": len(outer_field) / len(picture),
        "outer_field_regions": float(len(outer_regions)),
        "outer_field_materials": float(len(outer_counts)),
        "outer_field_dominant_material_share": (
            outer_counts.most_common(1)[0][1] / len(outer_field) if outer_field else 0.0
        ),
        "outer_field_regions_per_100_cells": (
            100 * len(outer_regions) / len(outer_field) if outer_field else 0.0
        ),
        "interior_proxy_share": len(interior_proxy) / len(picture),
        "interior_axis_anisotropy": anisotropy,
        "interior_angular_displacement_degrees": angular_displacement,
        "interior_min_edge_clearance": min_clearance,
    }


def artwork_metrics(raw: dict[str, Any]) -> dict[str, float]:
    """Measure the opening composition without assigning aesthetic preference."""
    pid = raw["PixelImageData"]
    width = int(pid["width"])
    height = int(pid["height"])
    picture = opening_picture(raw)
    if not picture:
        raise ValueError("visual audit requires at least one artwork pixel")
    region_set = regions(picture)
    counts = Counter(picture.values())
    sizes = sorted((len(region.cells) for region in region_set), reverse=True)
    horizontal_runs = _runs(picture, width, height, vertical=False)
    vertical_runs = _runs(picture, width, height, vertical=True)
    xs = [cell[0] for cell in picture]
    ys = [cell[1] for cell in picture]
    box_area = (max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1)
    centroid_x = statistics.fmean(xs)
    centroid_y = statistics.fmean(ys)

    outer_edges = 0
    colour_edges = 0
    neighbour_pairs = 0
    for (x, y), material in picture.items():
        for neighbour in ((x + 1, y), (x, y + 1)):
            other = picture.get(neighbour)
            if other is not None:
                neighbour_pairs += 1
                colour_edges += int(other != material)
        outer_edges += sum(
            neighbour not in picture
            for neighbour in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1))
        )

    total = len(picture)
    singleton_cells = sum(size for size in sizes if size == 1)
    structured_detail_cells = sum(size for size in sizes if 2 <= size <= 12)
    return {
        "cells": float(total),
        "width": float(width),
        "height": float(height),
        "fill": total / (width * height),
        "bbox_fill": total / box_area,
        "colours": float(len(counts)),
        "palette_entropy": _entropy(counts),
        "dominant_colour_share": counts.most_common(1)[0][1] / total,
        "regions": float(len(region_set)),
        "regions_per_100_cells": 100 * len(region_set) / total,
        "largest_region_share": sizes[0] / total,
        "median_region_cells": float(statistics.median(sizes)),
        "singleton_cell_share": singleton_cells / total,
        "small_region_cell_share": sum(size for size in sizes if size <= 4) / total,
        "structured_detail_share": structured_detail_cells / total,
        "local_colour_diversity_3x3": _local_colour_diversity(picture),
        "horizontal_run_median": float(statistics.median(horizontal_runs)),
        "vertical_run_median": float(statistics.median(vertical_runs)),
        "horizontal_mirror": _mirror_score(picture, width, height, vertical=False),
        "vertical_mirror": _mirror_score(picture, width, height, vertical=True),
        "centroid_offset": math.hypot(
            (centroid_x - (width - 1) / 2) / max(1, width),
            (centroid_y - (height - 1) / 2) / max(1, height),
        ),
        "outer_edge_per_cell": outer_edges / total,
        "colour_boundary_share": colour_edges / neighbour_pairs if neighbour_pairs else 0.0,
    }


def mechanic_counts(raw: dict[str, Any]) -> dict[str, int]:
    """Count supported mechanics by their player-facing names."""
    pid = raw["PixelImageData"]
    return {
        "Surprise shooters": len(raw.get("SurpriseShooters", {}).get("Shooters", [])),
        "Connected shooters": len(raw.get("ConnectedShooters", {}).get("Connections", [])),
        "Large cubes": len(pid.get("pixelHealths", [])),
        "Keys": len(pid.get("keys", {}).get("Keys", [])),
        "Queue locks": len(raw.get("Locks", {}).get("Shooters", [])),
        "Gates": len(pid.get("gates", {}).get("Gates", [])),
        "Pixel Pipes": len(pid.get("pixelPipes", {}).get("Pipes", [])),
        "Surprise pixels": len(pid.get("surprisePixels", {}).get("Pixels", [])),
        "Snakes": len(pid.get("snakes", {}).get("Snakes", [])),
        "Walls": len(pid.get("walls", {}).get("Walls", [])),
        "Egg Boxes": len(pid.get("eggBoxes", {}).get("EggBoxes", [])),
    }


def _actions_from_records(records: list[dict[str, Any]]) -> tuple[Action, ...]:
    """Decode the stable primitive certificate representation."""
    return tuple(
        Action(
            kind=ActionKind(str(record["kind"])),
            index=int(record["index"]) if record.get("index") is not None else None,
            shooter_id=int(record["shooter_id"]) if record.get("shooter_id") is not None else None,
        )
        for record in records
    )


def _apply(engine: Engine, state: GameState, action: Action) -> tuple[GameState, int, bool]:
    """Apply one public transition and return ticks plus whether a target was hit."""
    if action.kind is ActionKind.ADVANCE:
        child, event = engine.advance(state)
        return child, event.ticks, bool(event.targets)
    if action.index is None or action.shooter_id is None:
        raise ValueError(f"{action.kind} requires an index and shooter id")
    if action.kind is ActionKind.LAUNCH_LANE:
        lane = engine.level.lanes[action.index]
        actual = lane[state.lane_heads[action.index]].id
        if actual != action.shooter_id:
            raise ValueError(f"expected shooter {action.shooter_id}, found lane shooter {actual}")
        return engine.launch_lane(state, action.index), 0, False
    actual = state.tray[action.index].id
    if actual != action.shooter_id:
        raise ValueError(f"expected shooter {action.shooter_id}, found tray shooter {actual}")
    return engine.launch_tray(state, action.index), 0, False


def _work(level: Level, state: GameState) -> int:
    """Return remaining health and regeneration work at one replay state."""
    pipe_work = sum(
        pipe.total_count - consumed
        for pipe, consumed in zip(level.pixel_pipes, state.pipe_progress, strict=True)
    )
    return sum(state.health) + sum(state.ice_health) + pipe_work


def _ordinary_cell_targets(raw: dict[str, Any]) -> dict[Cell, int]:
    """Map artwork cells to the clean parser's leading target indices."""
    result: dict[Cell, int] = {}
    records = sorted(
        raw["PixelImageData"].get("pixels", []),
        key=lambda record: (int(record["x"]), int(record["y"])),
    )
    for target_index, record in enumerate(records):
        x0 = int(record["x"])
        y0 = int(record["y"])
        for x in range(x0, x0 + int(record.get("areaX", 1))):
            for y in range(y0, y0 + int(record.get("areaY", 1))):
                result[(x, y)] = target_index
    return result


def live_artwork(raw: dict[str, Any], level: Level, state: GameState) -> dict[Cell, int]:
    """Return opening-artwork cells whose simulator targets still occupy the board."""
    picture = opening_picture(raw)
    targets = _ordinary_cell_targets(raw)
    return {cell: material for cell, material in picture.items() if state.health[targets[cell]] > 0}


def _stage(
    requested: float,
    progress: float,
    action_index: int,
    state: GameState,
    raw: dict[str, Any],
    level: Level,
) -> Stage:
    """Measure the artwork remaining in one selected replay state."""
    live = live_artwork(raw, level, state)
    live_regions = regions(live)
    largest = max((len(region.cells) for region in live_regions), default=0)
    return Stage(
        requested_progress=requested,
        progress=progress,
        action_index=action_index,
        state=state,
        live_cells=live,
        regions_per_100_cells=(100 * len(live_regions) / len(live)) if live else 0.0,
        colours_remaining=len(set(live.values())),
        largest_region_share=(largest / len(live)) if live else 0.0,
    )


def _destruction_metrics(
    raw: dict[str, Any], removal_phase: dict[Cell, float], stages: tuple[Stage, ...]
) -> dict[str, float]:
    """Relate spatial artwork structure to when cells disappear in the solution."""
    picture = opening_picture(raw)
    phase = {cell: removal_phase.get(cell, 1.0) for cell in picture}
    all_deltas: list[float] = []
    same_colour_deltas: list[float] = []
    for (x, y), material in picture.items():
        for neighbour in ((x + 1, y), (x, y + 1)):
            if neighbour not in picture:
                continue
            delta = abs(phase[(x, y)] - phase[neighbour])
            all_deltas.append(delta)
            if picture[neighbour] == material:
                same_colour_deltas.append(delta)

    region_set = regions(picture)
    largest_region = max(region_set, key=lambda region: len(region.cells))
    focal_phases = [phase[cell] for cell in largest_region.cells]
    xs = [float(cell[0]) for cell in picture]
    ys = [float(cell[1]) for cell in picture]
    phases = [phase[cell] for cell in picture]
    initial_cells = len(picture)
    stage_fragmentation = [stage.regions_per_100_cells for stage in stages]
    stage_retention = [len(stage.live_cells) / initial_cells for stage in stages]
    return {
        "neighbour_removal_coherence": 1 - statistics.fmean(all_deltas) if all_deltas else 1.0,
        "same_region_removal_coherence": 1 - statistics.fmean(same_colour_deltas)
        if same_colour_deltas
        else 1.0,
        "largest_region_median_removal": float(statistics.median(focal_phases)),
        "spatial_sweep": max(abs(_pearson(xs, phases)), abs(_pearson(ys, phases))),
        "fragmentation_change": max(stage_fragmentation, default=0.0) - stage_fragmentation[0],
        "retention_at_quarter": stage_retention[1],
        "retention_at_half": stage_retention[2],
        "retention_at_three_quarters": stage_retention[3],
        "colours_at_half": float(stages[2].colours_remaining),
    }


def replay_profile(
    raw: dict[str, Any],
    certificate: dict[str, Any],
    *,
    connected_gap: int = 1,
) -> ReplayProfile:
    """Replay a persisted certificate and measure visual and decision trajectories."""
    number = int(certificate["level"])
    level = Level.from_dict(raw, number=number)
    engine = Engine(level, GameRules(connected_gap=connected_gap))
    actions = _actions_from_records(certificate["actions"])
    state = engine.initial_state()
    initial_work = _work(level, state)
    states: list[tuple[int, float, GameState]] = [(0, 0.0, state)]
    removal_phase: dict[Cell, float] = {}
    cell_targets = _ordinary_cell_targets(raw)
    target_cells: dict[int, list[Cell]] = {}
    for cell, target in cell_targets.items():
        target_cells.setdefault(target, []).append(cell)

    launches = 0
    relaunches = 0
    advances = 0
    hit_advances = 0
    dry_advances = 0
    ticks = 0
    tray_samples = [0]
    capacity_samples = [0.0]
    choice_samples: list[int] = []
    high_tray_samples = 0

    for action_index, action in enumerate(actions, start=1):
        if state.outcome(level) is Outcome.IN_PROGRESS:
            choices = len(engine.launchable_lanes(state)) + len(engine.launchable_tray_slots(state))
            choices += int(bool(state.active))
            choice_samples.append(choices)
        before = state
        state, action_ticks, hit = _apply(engine, state, action)
        ticks += action_ticks
        if action.kind is ActionKind.ADVANCE:
            advances += 1
            hit_advances += int(hit)
            dry_advances += int(not hit)
        elif action.kind is ActionKind.LAUNCH_TRAY:
            launches += 1
            relaunches += 1
        else:
            launches += 1

        progress = 1 - _work(level, state) / initial_work if initial_work else 1.0
        for target_index, cells in target_cells.items():
            if before.health[target_index] > 0 and state.health[target_index] == 0:
                for cell in cells:
                    removal_phase.setdefault(cell, progress)
        states.append((action_index, progress, state))
        tray_samples.append(len(state.tray))
        active_shooters = sum(len(group.shooters) for group in state.active)
        capacity_samples.append(active_shooters / level.conveyor_limit)
        high_tray_samples += int(len(state.tray) >= max(1, level.slot_count - 1))

    if state.outcome(level) is not Outcome.WON:
        raise ValueError(f"level {number} certificate replay ended {state.outcome(level)}")

    selected: list[Stage] = []
    for requested in STAGE_PROGRESS:
        action_index, progress, stage_state = min(
            states,
            key=lambda item: (abs(item[1] - requested), item[0]),
        )
        selected.append(_stage(requested, progress, action_index, stage_state, raw, level))
    stages = tuple(selected)

    trajectory_metrics = {
        "launches": float(launches),
        "relaunches": float(relaunches),
        "relaunch_share": relaunches / launches if launches else 0.0,
        "advance_events": float(advances),
        "hit_event_share": hit_advances / advances if advances else 0.0,
        "dry_event_share": dry_advances / advances if advances else 0.0,
        "conveyor_ticks": float(ticks),
        "peak_tray": float(max(tray_samples)),
        "mean_tray": statistics.fmean(tray_samples),
        "high_tray_state_share": high_tray_samples / len(actions) if actions else 0.0,
        "mean_conveyor_utilization": statistics.fmean(capacity_samples),
        "peak_conveyor_utilization": max(capacity_samples),
        "mean_available_actions": statistics.fmean(choice_samples) if choice_samples else 0.0,
        "min_available_actions": float(min(choice_samples, default=0)),
        "queue_progress_actions": float(sum(state.lane_heads)),
    }
    trajectory_metrics.update(_destruction_metrics(raw, removal_phase, stages))
    return ReplayProfile(
        stages=stages,
        removal_phase=removal_phase,
        metrics=trajectory_metrics,
        final_state=state,
    )


__all__ = [
    "STAGE_PROGRESS",
    "Region",
    "ReplayProfile",
    "Stage",
    "artwork_metrics",
    "live_artwork",
    "mechanic_counts",
    "opening_picture",
    "regions",
    "replay_profile",
]
