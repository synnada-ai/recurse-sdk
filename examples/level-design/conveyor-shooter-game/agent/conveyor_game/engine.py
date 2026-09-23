# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Pure event-driven transitions for the supported conveyor shooter core."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .model import Level, Ray, RayObject, RayObjectKind, Shooter


class Outcome(StrEnum):
    """Terminal status derived from one immutable gameplay state."""

    IN_PROGRESS = "in_progress"
    WON = "won"
    LOST = "lost"


class EventKind(StrEnum):
    """Reason an event-driven advance stopped."""

    HIT = "hit"
    KEY_WAITING = "key_waiting"
    KEY_FREED = "key_freed"
    PARKED = "parked"
    SHOOTER_EXITED = "shooter_exited"
    WON = "won"
    LOST = "lost"


@dataclass(frozen=True)
class GameRules:
    """Behavioral constants kept explicit until their calibration is established."""

    connected_gap: int | None = None

    def __post_init__(self) -> None:
        """Reject nonsensical timing before a simulation starts."""
        if self.connected_gap is not None and self.connected_gap < 0:
            raise ValueError(f"connected_gap must be nonnegative, got {self.connected_gap}")


@dataclass(frozen=True)
class ShooterState:
    """A shooter's mutable gameplay values represented immutably."""

    id: int
    material: int
    ammo: int
    connection_id: int | None = None
    connection_order: int | None = None

    @classmethod
    def from_shooter(cls, shooter: Shooter) -> ShooterState:
        """Create an in-play shooter from static queue data."""
        return cls(
            id=shooter.id,
            material=shooter.material,
            ammo=shooter.ammo,
            connection_id=shooter.connection_id,
            connection_order=shooter.connection_order,
        )


@dataclass(frozen=True)
class BeltGroup:
    """One launch action currently travelling around the conveyor."""

    position: int
    shooters: tuple[ShooterState, ...]


@dataclass(frozen=True)
class GameState:
    """Complete canonical state required to continue a supported level."""

    health: tuple[int, ...]
    lane_heads: tuple[int, ...]
    tray: tuple[ShooterState, ...]
    active: tuple[BeltGroup, ...]
    freed_keys: frozenset[int]
    waiting_keys: frozenset[int]
    locked: frozenset[int]
    pipe_progress: tuple[int, ...] = ()
    ice_health: tuple[int, ...] = ()
    lost: bool = False

    def outcome(self, level: Level) -> Outcome:
        """Return the state outcome without storing redundant terminal flags."""
        pipes_complete = all(
            consumed == pipe.total_count
            for pipe, consumed in zip(level.pixel_pipes, self.pipe_progress, strict=True)
        )
        if not any(self.health) and pipes_complete and not any(self.ice_health):
            return Outcome.WON
        if self.lost:
            return Outcome.LOST
        return Outcome.IN_PROGRESS


@dataclass(frozen=True)
class Event:
    """One meaningful boundary reached after one or more conveyor ticks."""

    kind: EventKind
    ticks: int
    targets: tuple[int, ...] = ()
    keys: tuple[int, ...] = ()


class Engine:
    """Authoritative state transition engine for the currently supported mechanics."""

    def __init__(self, level: Level, rules: GameRules) -> None:
        """Create an engine only for mechanics this transition core supports."""
        if level.unsupported:
            mechanics = ", ".join(level.unsupported)
            raise ValueError(f"level uses unsupported mechanics: {mechanics}")
        if level.connections and rules.connected_gap is None:
            raise ValueError("connected_gap is required for a level with connected shooters")
        self.level = level
        self.rules = rules
        self._advance_cache: dict[GameState, tuple[GameState, Event]] = {}
        self._shooters_by_id = {shooter.id: shooter for lane in level.lanes for shooter in lane}
        target_by_cell = {
            cell: target_index
            for target_index, target in enumerate(level.targets)
            for cell in target.cells
        }
        self._key_cover_targets = tuple(
            frozenset(
                target_by_cell[cell]
                for x, y in key.cells
                for cell in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1))
                if cell not in key.cells and cell in target_by_cell
            )
            for key in level.keys
        )

    def initial_state(self) -> GameState:
        """Return the immutable state before the first player action."""
        state = GameState(
            health=self.level.initial_health,
            lane_heads=tuple(0 for _lane in self.level.lanes),
            tray=(),
            active=(),
            freed_keys=frozenset(),
            waiting_keys=frozenset(),
            locked=self.level.lock_ids,
            pipe_progress=tuple(0 for _pipe in self.level.pixel_pipes),
            ice_health=tuple(block.health for block in self.level.pixel_ice_blocks),
        )
        return self._stabilize_initial_keys(state)

    def _stabilize_initial_keys(self, state: GameState) -> GameState:
        """Resolve every key already reachable from the empty conveyor at level start.

        An exposed key needs no shooter or bullet. It immediately opens the leftmost lock at a
        lane front, or remains on the board as a physical blocker until such a lock appears.
        Opening one key can expose another on the same ray, so initialization continues until
        no further key or lock transition is possible.
        """
        while True:
            exposed = self._exposed_keys(state)
            waiting = state.waiting_keys | exposed
            waiting, freed, locked, lane_heads = self._unlock_front_locks(
                waiting, state.freed_keys, state.locked, state.lane_heads
            )
            child = GameState(
                health=state.health,
                lane_heads=lane_heads,
                tray=state.tray,
                active=state.active,
                freed_keys=freed,
                waiting_keys=waiting,
                locked=locked,
                pipe_progress=state.pipe_progress,
                ice_health=state.ice_health,
                lost=state.lost,
            )
            if child == state:
                return child
            state = child

    def _exposed_keys(self, state: GameState) -> frozenset[int]:
        """Return untouched keys that are the first live object on at least one firing ray."""
        exposed: set[int] = set()
        unavailable = state.freed_keys | state.waiting_keys
        for ray in self.level.lap:
            for item in ray.objects:
                if item.kind is RayObjectKind.WALL:
                    break
                if item.kind is RayObjectKind.KEY:
                    if item.index in state.freed_keys:
                        continue
                    if item.index not in unavailable:
                        exposed.add(item.index)
                    break
                if self.is_live_target(item, state.health):
                    break
        return frozenset(exposed)

    def launch_lane(self, state: GameState, lane_index: int) -> GameState:
        """Launch one front shooter or its complete connected group."""
        if not 0 <= lane_index < len(self.level.lanes):
            raise ValueError(f"lane index {lane_index} is out of range")
        head = state.lane_heads[lane_index]
        lane = self.level.lanes[lane_index]
        if head >= len(lane):
            raise ValueError(f"lane {lane_index} is empty")
        shooters, heads = self._lane_group(state, lane[head], lane_index)
        return self._launch(state, shooters, lane_heads=heads, tray=state.tray)

    def launch_tray(self, state: GameState, slot_index: int) -> GameState:
        """Relaunch one parked shooter or its complete connected group."""
        if not 0 <= slot_index < len(state.tray):
            raise ValueError(f"tray index {slot_index} is out of range")
        selected = state.tray[slot_index]
        shooters, tray = self._tray_group(state.tray, selected)
        return self._launch(state, shooters, lane_heads=state.lane_heads, tray=tray)

    def launchable_lanes(self, state: GameState) -> tuple[int, ...]:
        """Return one canonical lane tap for every currently ready queue group."""
        active_count = sum(len(group.shooters) for group in state.active)
        candidates: dict[tuple[int, ...], int] = {}
        for lane_index, (lane, head) in enumerate(
            zip(self.level.lanes, state.lane_heads, strict=True)
        ):
            if head >= len(lane):
                continue
            try:
                shooters, _heads = self._lane_group(state, lane[head], lane_index)
            except ValueError:
                continue
            ids = tuple(shooter.id for shooter in shooters)
            if active_count + len(ids) > self.level.conveyor_limit:
                continue
            candidates.setdefault(ids, lane_index)
        return tuple(candidates.values())

    def launchable_tray_slots(self, state: GameState) -> tuple[int, ...]:
        """Return one canonical tray tap for every parked group."""
        active_count = sum(len(group.shooters) for group in state.active)
        candidates: dict[tuple[int, ...], int] = {}
        for slot_index, shooter in enumerate(state.tray):
            shooters, _tray = self._tray_group(state.tray, shooter)
            ids = tuple(member.id for member in shooters)
            if active_count + len(ids) > self.level.conveyor_limit:
                continue
            candidates.setdefault(ids, slot_index)
        return tuple(candidates.values())

    def advance(self, state: GameState) -> tuple[GameState, Event]:
        """Advance to the next hit, launch opportunity, exit, park, or terminal state."""
        cached = self._advance_cache.get(state)
        if cached is not None:
            return cached
        result = self._advance_uncached(state)
        self._advance_cache[state] = result
        return result

    def _advance_uncached(  # noqa: PLR0911 - one return per kind of game event
        self, state: GameState
    ) -> tuple[GameState, Event]:
        """Compute one pure event transition before it is memoized by ``advance``."""
        if not state.active:
            raise ValueError("cannot advance without an active shooter")
        ticks = 0
        while True:
            before = state
            state, targets = self._tick(state)
            ticks += 1
            outcome = state.outcome(self.level)
            if outcome is Outcome.WON:
                return state, Event(EventKind.WON, ticks, targets)
            if outcome is Outcome.LOST:
                return state, Event(EventKind.LOST, ticks, targets)
            if targets:
                return state, Event(EventKind.HIT, ticks, targets)
            if len(state.waiting_keys) > len(before.waiting_keys):
                keys = tuple(sorted(state.waiting_keys - before.waiting_keys))
                return state, Event(EventKind.KEY_WAITING, ticks, keys=keys)
            if len(state.freed_keys) > len(before.freed_keys):
                keys = tuple(sorted(state.freed_keys - before.freed_keys))
                return state, Event(EventKind.KEY_FREED, ticks, keys=keys)
            if len(state.tray) > len(before.tray):
                return state, Event(EventKind.PARKED, ticks)
            if len(state.active) < len(before.active):
                return state, Event(EventKind.SHOOTER_EXITED, ticks)

    def is_live_target(self, item: RayObject, health: Sequence[int]) -> bool:
        """Return whether a target still occupies this particular firing-ray position."""
        if item.kind is not RayObjectKind.TARGET:
            raise ValueError("live-target checks require a target ray object")
        remaining = health[item.index]
        if remaining == 0:
            return False
        if item.retraction_rank is None:
            return True
        target = self.level.targets[item.index]
        if target.retraction_length is None:
            raise ValueError("retraction rank refers to a non-retracting target")
        active_layers = (remaining * target.retraction_length + target.health - 1) // target.health
        return item.retraction_rank < active_layers

    def _launch(
        self,
        state: GameState,
        shooters: tuple[ShooterState, ...],
        *,
        lane_heads: tuple[int, ...],
        tray: tuple[ShooterState, ...],
    ) -> GameState:
        """Put a shooter group on the belt and open front locks a waiting key allows."""
        if state.outcome(self.level) is not Outcome.IN_PROGRESS:
            raise ValueError("cannot launch after the level has ended")
        active_count = sum(len(group.shooters) for group in state.active)
        if active_count + len(shooters) > self.level.conveyor_limit:
            raise ValueError("launch would exceed conveyor capacity")
        active = _canonical_active((*state.active, BeltGroup(position=0, shooters=shooters)))
        waiting_keys, freed_keys, locked, lane_heads = self._unlock_front_locks(
            state.waiting_keys, state.freed_keys, state.locked, lane_heads
        )
        return GameState(
            health=state.health,
            lane_heads=lane_heads,
            tray=tray,
            active=active,
            freed_keys=freed_keys,
            waiting_keys=waiting_keys,
            locked=locked,
            pipe_progress=state.pipe_progress,
            ice_health=state.ice_health,
            lost=state.lost,
        )

    def _tick(self, state: GameState) -> tuple[GameState, tuple[int, ...]]:
        """Advance one belt tick: each shooter fires, then moves, exits, parks, or loses."""
        health = list(state.health)
        tray = list(state.tray)
        next_active: list[BeltGroup] = []
        hits: list[int] = []
        freed_keys = set(state.freed_keys)
        waiting_keys = set(state.waiting_keys)
        locked = state.locked
        lane_heads = state.lane_heads
        pipe_progress = list(state.pipe_progress)
        ice_health = list(state.ice_health)
        lost = state.lost

        for group in sorted(state.active, key=lambda item: item.position, reverse=True):
            shooters: list[ShooterState] = []
            for member_index, shooter in enumerate(group.shooters):
                position = self._member_position(group, member_index)
                updated, target, key = self._fire(
                    position,
                    shooter,
                    health,
                    freed_keys,
                    waiting_keys,
                    pipe_progress,
                    ice_health,
                )
                shooters.append(updated)
                if target is not None:
                    hits.append(target)
                if key is not None:
                    waiting_keys.add(key)
                    waiting, newly_freed, locked, lane_heads = self._unlock_front_locks(
                        frozenset(waiting_keys), frozenset(freed_keys), locked, lane_heads
                    )
                    freed_keys = set(newly_freed)
                    waiting_keys = set(waiting)
            if not any(health):
                next_active.append(BeltGroup(group.position + 1, tuple(shooters)))
                continue
            if all(shooter.ammo == 0 for shooter in shooters):
                continue
            next_position = group.position + 1
            if next_position < len(self.level.lap):
                next_active.append(BeltGroup(next_position, tuple(shooters)))
                continue
            if len(tray) + len(shooters) > self.level.slot_count:
                lost = True
                continue
            tray.extend(shooters)

        active = _canonical_active(tuple(next_active))
        child = GameState(
            health=tuple(health),
            lane_heads=lane_heads,
            tray=tuple(tray),
            active=active,
            freed_keys=frozenset(freed_keys),
            waiting_keys=frozenset(waiting_keys),
            locked=locked,
            pipe_progress=tuple(pipe_progress),
            ice_health=tuple(ice_health),
            lost=lost,
        )
        return child, tuple(hits)

    def _lane_group(
        self,
        state: GameState,
        selected: Shooter,
        selected_lane: int,
    ) -> tuple[tuple[ShooterState, ...], tuple[int, ...]]:
        """Resolve the shooter or complete connected group launched from a lane front."""
        heads = list(state.lane_heads)
        if selected.id in state.locked:
            raise ValueError(f"queue lock {selected.id} blocks lane {selected_lane}")
        if selected.connection_id is None:
            heads[selected_lane] += 1
            return (ShooterState.from_shooter(selected),), tuple(heads)

        member_ids = self.level.connections[selected.connection_id]
        member_set = set(member_ids)
        consumed: set[int] = set()
        for lane_index, (lane, head) in enumerate(
            zip(self.level.lanes, state.lane_heads, strict=True)
        ):
            position = head
            while position < len(lane) and lane[position].id in member_set:
                consumed.add(lane[position].id)
                position += 1
            heads[lane_index] = position
        if consumed != member_set:
            raise ValueError("connected shooters are not at every queue front")
        return tuple(
            ShooterState.from_shooter(self._shooters_by_id[shooter_id]) for shooter_id in member_ids
        ), tuple(heads)

    def _tray_group(
        self,
        tray: tuple[ShooterState, ...],
        selected: ShooterState,
    ) -> tuple[tuple[ShooterState, ...], tuple[ShooterState, ...]]:
        """Resolve the shooter or complete connected group relaunched from the tray."""
        if selected.connection_id is None:
            slot_index = tray.index(selected)
            return (selected,), (*tray[:slot_index], *tray[slot_index + 1 :])

        member_ids = self.level.connections[selected.connection_id]
        shooters_by_id = {
            shooter.id: shooter
            for shooter in tray
            if shooter.connection_id == selected.connection_id
        }
        if set(shooters_by_id) != set(member_ids):
            raise ValueError("connected shooters are not together in the tray")
        shooters = tuple(shooters_by_id[shooter_id] for shooter_id in member_ids)
        remaining = tuple(
            shooter for shooter in tray if shooter.connection_id != selected.connection_id
        )
        return shooters, remaining

    def _member_position(self, group: BeltGroup, member_index: int) -> int:
        """Belt position of one member of a connected group."""
        connected_gap = self.rules.connected_gap or 0
        return group.position + (len(group.shooters) - member_index - 1) * connected_gap

    def _unlock_front_locks(
        self,
        waiting_keys: frozenset[int],
        freed_keys: frozenset[int],
        locked: frozenset[int],
        lane_heads: tuple[int, ...],
    ) -> tuple[frozenset[int], frozenset[int], frozenset[int], tuple[int, ...]]:
        """Open front-of-lane locks with waiting keys, lowest lane first."""
        waiting = set(waiting_keys)
        freed = set(freed_keys)
        remaining = set(locked)
        heads = list(lane_heads)
        while waiting:
            ready = next(
                (
                    (lane_index, lane[head].id)
                    for lane_index, (lane, head) in enumerate(
                        zip(self.level.lanes, heads, strict=True)
                    )
                    if head < len(lane) and lane[head].id in remaining
                ),
                None,
            )
            if ready is None:
                break
            lane_index, lock_id = ready
            key_index = min(waiting)
            waiting.remove(key_index)
            freed.add(key_index)
            remaining.remove(lock_id)
            heads[lane_index] += 1
        return frozenset(waiting), frozenset(freed), frozenset(remaining), tuple(heads)

    def _fire(  # noqa: PLR0911, PLR0912, PLR0913, PLR0917 - the complete shot rule in one place
        self,
        position: int,
        shooter: ShooterState,
        health: list[int],
        freed_keys: set[int],
        waiting_keys: set[int],
        pipe_progress: list[int],
        ice_health: list[int],
    ) -> tuple[ShooterState, int | None, int | None]:
        """Fire one shot; return the updated shooter, the hit target, and any released key."""
        if position >= len(self.level.lap):
            return shooter, None, None
        ray = self.level.lap[position]
        for object_position, item in enumerate(ray.objects):
            if item.kind is RayObjectKind.WALL:
                return shooter, None, None
            if item.kind is RayObjectKind.KEY:
                if item.index not in freed_keys and item.index not in waiting_keys:
                    return shooter, None, item.index
                if item.index not in freed_keys:
                    return shooter, None, None
                continue
            if not self.is_live_target(item, health):
                continue
            target = self.level.targets[item.index]
            if shooter.ammo == 0 or target.material != shooter.material:
                return shooter, None, None
            shots = 1
            if target.unloads_while_aligned:
                shots = min(shooter.ammo, health[item.index])
            health[item.index] -= shots
            released_key = None
            if health[item.index] == 0:
                for ice_index in self.level.target_ice_blocks[item.index]:
                    if ice_health[ice_index]:
                        ice_health[ice_index] -= 1
                regenerated = self._regenerate_pixel(item.index, health, pipe_progress)
                if not regenerated and self.level.keys:
                    released_key = self._released_key_after_shot(
                        item.index,
                        ray,
                        object_position,
                        health,
                        freed_keys | waiting_keys,
                    )
            return (
                ShooterState(
                    id=shooter.id,
                    material=shooter.material,
                    ammo=shooter.ammo - shots,
                    connection_id=shooter.connection_id,
                    connection_order=shooter.connection_order,
                ),
                item.index,
                released_key,
            )
        return shooter, None, None

    def _released_key_after_shot(
        self,
        target_index: int,
        ray: Ray,
        object_position: int,
        health: list[int],
        freed_keys: set[int],
    ) -> int | None:
        """Return a key exposed by this direct cover shot or its continued firing line."""
        for key_index, cover_targets in enumerate(self._key_cover_targets):
            if key_index not in freed_keys and target_index in cover_targets:
                return key_index
        for item in ray.objects[object_position + 1 :]:
            if item.kind is RayObjectKind.WALL:
                return None
            if item.kind is RayObjectKind.KEY:
                if item.index in freed_keys:
                    continue
                return item.index
            if not self.is_live_target(item, health):
                continue
            return None
        return None

    def _regenerate_pixel(
        self,
        target_index: int,
        health: list[int],
        pipe_progress: list[int],
    ) -> bool:
        """Let the first matching Pixel Pipe revive one destroyed ordinary pixel."""
        target = self.level.targets[target_index]
        if len(target.cells) != 1 or target.retraction_length is not None:
            return False
        for pipe_index, pipe in enumerate(self.level.pixel_pipes):
            consumed = pipe_progress[pipe_index]
            if pipe.material_at(consumed) != target.material:
                continue
            pipe_progress[pipe_index] += 1
            health[target_index] = target.health
            return True
        return False


def _canonical_active(groups: tuple[BeltGroup, ...]) -> tuple[BeltGroup, ...]:
    """Give equivalent active sets one stable hashable representation."""
    return tuple(
        sorted(
            groups,
            key=lambda group: (-group.position, tuple(shooter.id for shooter in group.shooters)),
        )
    )
