# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One memoized state-graph search over the authoritative gameplay engine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from heapq import heappop, heappush

from .engine import Engine, GameRules, GameState, Outcome, ShooterState
from .model import Level, RayObjectKind, Shooter, queue_scheduling_profile


class SolveStatus(StrEnum):
    """What the bounded state-graph search established."""

    SOLVED = "solved"
    UNSUPPORTED = "unsupported"
    INCOMPLETE = "incomplete"
    CONTRADICTION = "contradiction"


class ActionKind(StrEnum):
    """Player decisions understood by the clean engine."""

    LAUNCH_LANE = "launch_lane"
    LAUNCH_TRAY = "launch_tray"
    ADVANCE = "advance"


class BeamPolicy(StrEnum):
    """Deterministic state ordering used by a bounded beam search."""

    DEFAULT = "default"
    QUEUE_PROGRESS = "queue_progress"


@dataclass(frozen=True)
class Action:
    """A replayable decision with its expected shooter identity."""

    kind: ActionKind
    index: int | None = None
    shooter_id: int | None = None

    def with_shooter_id(self, shooter_id: int) -> Action:
        """Return a copy useful for certificate-falsification tests."""
        return Action(kind=self.kind, index=self.index, shooter_id=shooter_id)


@dataclass(frozen=True)
class SolveResult:
    """Bounded search result, explicitly labelled by timing calibration."""

    status: SolveStatus
    explored_states: int
    actions: tuple[Action, ...] = ()
    unsupported: tuple[str, ...] = ()
    strategy: str | None = None


class ReplayError(ValueError):
    """A saved action no longer matches the state in which it was recorded."""


def solve_portfolio(
    level: Level,
    rules: GameRules,
    *,
    max_states: int = 50_000,
) -> SolveResult:
    """Try best-first search, then one structural beam policy for expensive levels."""
    profile = queue_scheduling_profile(level)
    if len(level.lock_ids) == 1 and profile.has_buried_lock and profile.connected_group_overflow:
        return solve_beam(
            level,
            rules,
            beam_width=500,
            max_depth=2_000,
            planning_spacing=1,
        )
    if _is_large_plain_board(level):
        result = solve_beam(
            level,
            rules,
            beam_width=100,
            max_depth=2_000,
            planning_spacing=2 if level.connections else 1,
        )
        if result.status is not SolveStatus.INCOMPLETE or level.connections:
            return result
        return solve_beam(
            level,
            rules,
            beam_width=100,
            max_depth=2_000,
            planning_spacing=1,
            policy=BeamPolicy.QUEUE_PROGRESS,
        )
    has_shared_target = any(target.health > 1 or len(target.cells) > 1 for target in level.targets)
    if (level.keys and level.pixel_pipes) or (
        has_shared_target and profile.connected_group_overflow
    ):
        return solve_beam(
            level,
            rules,
            beam_width=100,
            max_depth=2_000,
            planning_spacing=1,
        )
    result = solve(level, rules, max_states=max_states)
    if result.status is not SolveStatus.INCOMPLETE:
        return result
    return solve_beam(
        level,
        rules,
        beam_width=100,
        max_depth=2_000,
        planning_spacing=1,
        policy=BeamPolicy.QUEUE_PROGRESS,
    )


def solve_experimental_portfolio(
    level: Level,
    rules: GameRules,
    *,
    max_states: int = 50_000,
) -> SolveResult:
    """Preserve the certified solver, then add complementary bounded alternatives."""
    if level.pixel_pipes and level.connections:
        pipe_result = solve_beam(
            level,
            rules,
            beam_width=100,
            max_depth=2_000,
            planning_spacing=1,
        )
        if pipe_result.status is not SolveStatus.INCOMPLETE:
            return SolveResult(
                status=pipe_result.status,
                explored_states=pipe_result.explored_states,
                actions=pipe_result.actions,
                unsupported=pipe_result.unsupported,
                strategy="connected_pipe_beam",
            )
    if len(level.targets) >= 900 and any(len(group) >= 3 for group in level.connections):
        large_train_result = solve_beam(
            level,
            rules,
            beam_width=100,
            max_depth=2_000,
            planning_spacing=2,
        )
        if large_train_result.status is not SolveStatus.INCOMPLETE:
            return SolveResult(
                status=large_train_result.status,
                explored_states=large_train_result.explored_states,
                actions=large_train_result.actions,
                unsupported=large_train_result.unsupported,
                strategy="large_train_spacing_2",
            )
    has_retracting_target = any(target.retraction_length is not None for target in level.targets)
    if has_retracting_target and any(len(group) >= 3 for group in level.connections):
        retracting_result = solve_multi_queue(level, rules, max_states=max_states)
        if retracting_result.status is not SolveStatus.INCOMPLETE:
            return SolveResult(
                status=retracting_result.status,
                explored_states=retracting_result.explored_states,
                actions=retracting_result.actions,
                unsupported=retracting_result.unsupported,
                strategy="retracting_train_multi_queue",
            )
    certified = solve_portfolio(level, rules, max_states=max_states)
    if certified.status is not SolveStatus.INCOMPLETE:
        return SolveResult(
            status=certified.status,
            explored_states=certified.explored_states,
            actions=certified.actions,
            unsupported=certified.unsupported,
            strategy="certified_portfolio",
        )
    strategies: tuple[tuple[str, Callable[[], SolveResult]], ...] = (
        (
            "beam_default_spacing_1",
            lambda: solve_beam(
                level,
                rules,
                beam_width=100,
                max_depth=2_000,
                planning_spacing=1,
            ),
        ),
        (
            "beam_default_spacing_2",
            lambda: solve_beam(
                level,
                rules,
                beam_width=100,
                max_depth=2_000,
                planning_spacing=2,
            ),
        ),
        (
            "multi_queue",
            lambda: solve_multi_queue(level, rules, max_states=max_states),
        ),
        (
            "beam_queue_progress",
            lambda: solve_beam(
                level,
                rules,
                beam_width=100,
                max_depth=2_000,
                planning_spacing=1,
                policy=BeamPolicy.QUEUE_PROGRESS,
            ),
        ),
        (
            "beam_wide_spacing_1",
            lambda: solve_beam(
                level,
                rules,
                beam_width=500,
                max_depth=2_000,
                planning_spacing=1,
            ),
        ),
    )
    explored = certified.explored_states
    last: SolveResult | None = None
    for name, run in strategies:
        result = run()
        explored += result.explored_states
        if result.status is not SolveStatus.INCOMPLETE:
            return SolveResult(
                status=result.status,
                explored_states=explored,
                actions=result.actions,
                unsupported=result.unsupported,
                strategy=name,
            )
        last = result
    assert last is not None
    return SolveResult(
        status=last.status,
        explored_states=explored,
        strategy="portfolio_exhausted",
    )


def solve(
    level: Level,
    rules: GameRules,
    *,
    max_states: int = 50_000,
) -> SolveResult:
    """Search canonical event states once each and return a replayable certificate."""
    if max_states < 1:
        raise ValueError("max_states must be positive")
    if level.unsupported:
        return SolveResult(
            status=SolveStatus.UNSUPPORTED,
            explored_states=0,
            unsupported=level.unsupported,
        )

    engine = Engine(level, rules)
    start = engine.initial_state()
    if start.outcome(level) is Outcome.WON:
        return SolveResult(
            status=SolveStatus.SOLVED,
            explored_states=1,
        )

    depth_weight = _long_cycle_depth_weight(level)
    heap: list[tuple[tuple[int, ...], int, int, GameState]] = [
        (_search_priority(engine, start, 0, depth_weight), 0, 0, start)
    ]
    seen = {start}
    predecessor: dict[GameState, tuple[GameState, Action]] = {}
    push_order = 0
    explored = 0

    while heap and explored < max_states:
        _, depth, _, state = heappop(heap)
        explored += 1
        for action in _legal_actions(engine, state):
            chain = _forced_chain(engine, state, action)
            if any(child.outcome(level) is Outcome.LOST or child in seen for child, _, _ in chain):
                continue
            for child, parent, step in chain:
                seen.add(child)
                predecessor[child] = (parent, step)
            child = chain[-1][0]
            if child.outcome(level) is Outcome.WON:
                return SolveResult(
                    status=SolveStatus.SOLVED,
                    explored_states=explored,
                    actions=_reconstruct(predecessor, child),
                )
            push_order += 1
            child_depth = depth + len(chain)
            heappush(
                heap,
                (
                    _search_priority(engine, child, child_depth, depth_weight),
                    child_depth,
                    push_order,
                    child,
                ),
            )

    return SolveResult(
        status=SolveStatus.INCOMPLETE if heap else SolveStatus.CONTRADICTION,
        explored_states=explored,
    )


def solve_multi_queue(
    level: Level,
    rules: GameRules,
    *,
    max_states: int = 50_000,
) -> SolveResult:
    """Alternate several heuristics over one shared, memoized state graph.

    Each discovered state is visible to every queue, but is expanded only once. This keeps the
    different views complementary: no single blended score can permanently hide a state preferred
    by board progress, queue progress, tray safety, or unlock progress.
    """
    if max_states < 1:
        raise ValueError("max_states must be positive")
    if level.unsupported:
        return SolveResult(
            status=SolveStatus.UNSUPPORTED,
            explored_states=0,
            unsupported=level.unsupported,
        )

    engine = Engine(level, rules)
    start = engine.initial_state()
    if start.outcome(level) is Outcome.WON:
        return SolveResult(status=SolveStatus.SOLVED, explored_states=1)

    queues: list[list[tuple[tuple[int, ...], int, int, GameState]]] = [[], [], [], []]
    for queue, priority in zip(queues, _multi_queue_priorities(engine, start), strict=True):
        heappush(queue, (priority, 0, 0, start))
    seen = {start}
    expanded: set[GameState] = set()
    predecessor: dict[GameState, tuple[GameState, Action]] = {}
    push_order = 0
    explored = 0
    queue_index = 0

    while explored < max_states:
        selected: tuple[tuple[int, ...], int, int, GameState] | None = None
        for _attempt in range(len(queues)):
            queue = queues[queue_index]
            queue_index = (queue_index + 1) % len(queues)
            while queue and queue[0][3] in expanded:
                heappop(queue)
            if queue:
                selected = heappop(queue)
                break
        if selected is None:
            break

        _, depth, _, state = selected
        if state in expanded:
            continue
        expanded.add(state)
        explored += 1
        for action in _legal_actions(engine, state):
            chain = _forced_chain(engine, state, action)
            if any(child.outcome(level) is Outcome.LOST or child in seen for child, _, _ in chain):
                continue
            for child, parent, step in chain:
                seen.add(child)
                predecessor[child] = (parent, step)
            child = chain[-1][0]
            if child.outcome(level) is Outcome.WON:
                return SolveResult(
                    status=SolveStatus.SOLVED,
                    explored_states=explored,
                    actions=_reconstruct(predecessor, child),
                )
            push_order += 1
            child_depth = depth + len(chain)
            for queue, priority in zip(
                queues,
                _multi_queue_priorities(engine, child),
                strict=True,
            ):
                heappush(queue, (priority, child_depth, push_order, child))

    return SolveResult(
        status=SolveStatus.INCOMPLETE if any(queues) else SolveStatus.CONTRADICTION,
        explored_states=explored,
    )


def solve_beam(
    level: Level,
    rules: GameRules,
    *,
    beam_width: int = 500,
    max_depth: int = 2_000,
    planning_spacing: int = 0,
    policy: BeamPolicy = BeamPolicy.DEFAULT,
) -> SolveResult:
    """Search depth-progressing exact states with a bounded planning frontier."""
    if beam_width < 1:
        raise ValueError("beam_width must be positive")
    if max_depth < 1:
        raise ValueError("max_depth must be positive")
    if planning_spacing < 0:
        raise ValueError("planning_spacing must be nonnegative")
    if level.unsupported:
        return SolveResult(
            status=SolveStatus.UNSUPPORTED,
            explored_states=0,
            unsupported=level.unsupported,
        )

    engine = Engine(level, rules)
    start = engine.initial_state()
    if start.outcome(level) is Outcome.WON:
        return SolveResult(
            status=SolveStatus.SOLVED,
            explored_states=1,
        )

    frontier = [start]
    seen = {start}
    predecessor: dict[GameState, tuple[GameState, Action]] = {}
    explored = 0
    for _depth in range(max_depth):
        candidates: dict[GameState, tuple[tuple[GameState, GameState, Action], ...]] = {}
        for state in frontier:
            explored += 1
            for action in _legal_actions(engine, state, planning_spacing):
                chain = _forced_chain(engine, state, action, planning_spacing)
                if any(
                    child.outcome(level) is Outcome.LOST or child in seen for child, _, _ in chain
                ):
                    continue
                child = chain[-1][0]
                candidates[child] = chain
                if child.outcome(level) is Outcome.WON:
                    for chained_child, parent, step in chain:
                        predecessor[chained_child] = (parent, step)
                    return SolveResult(
                        status=SolveStatus.SOLVED,
                        explored_states=explored,
                        actions=_reconstruct(predecessor, child),
                    )
        if not candidates:
            break
        frontier = sorted(
            candidates,
            key=lambda state: _beam_priority(engine, state, policy),
        )[:beam_width]
        for state in frontier:
            for child, parent, step in candidates[state]:
                seen.add(child)
                predecessor[child] = (parent, step)

    return SolveResult(
        status=SolveStatus.INCOMPLETE,
        explored_states=explored,
    )


def replay(level: Level, rules: GameRules, actions: tuple[Action, ...]) -> GameState:
    """Replay a certificate through the same public transitions used during search."""
    engine = Engine(level, rules)
    state = engine.initial_state()
    for step, action in enumerate(actions, start=1):
        try:
            state = _apply(engine, state, action)
        except (IndexError, ValueError) as error:
            raise ReplayError(f"certificate failed at step {step}: {error}") from error
    return state


def _legal_actions(
    engine: Engine,
    state: GameState,
    planning_spacing: int = 0,
) -> tuple[Action, ...]:
    """Enumerate every observable player decision at an event boundary."""
    actions: list[Action] = []
    if state.active:
        actions.append(Action(ActionKind.ADVANCE))
    actions.extend(_launch_actions(engine, state, planning_spacing))
    return tuple(actions)


def _launch_actions(
    engine: Engine,
    state: GameState,
    planning_spacing: int = 0,
) -> tuple[Action, ...]:
    """Enumerate queue and tray launches available without advancing time."""
    actions: list[Action] = []
    if planning_spacing and any(group.position < planning_spacing for group in state.active):
        return ()
    # Launch order is interchangeable only while launching cannot consume a waiting key.
    # With a key available, the first tapped lane may determine which front lock opens.
    prior_keys = (
        []
        if state.waiting_keys
        else [
            tuple(sorted(shooter.id for shooter in group.shooters))
            for group in state.active
            if group.position == 0
        ]
    )
    last_key = max(prior_keys, default=())
    active_count = sum(len(group.shooters) for group in state.active)
    if active_count >= engine.level.conveyor_limit:
        return ()
    for slot_index in engine.launchable_tray_slots(state):
        tray_shooter = state.tray[slot_index]
        if _launch_group_key(engine, tray_shooter) <= last_key:
            continue
        actions.append(Action(ActionKind.LAUNCH_TRAY, slot_index, tray_shooter.id))
    for lane_index in engine.launchable_lanes(state):
        lane = engine.level.lanes[lane_index]
        lane_shooter = lane[state.lane_heads[lane_index]]
        if _launch_group_key(engine, lane_shooter) <= last_key:
            continue
        actions.append(Action(ActionKind.LAUNCH_LANE, lane_index, lane_shooter.id))
    return tuple(actions)


def _launch_group_key(engine: Engine, shooter: Shooter | ShooterState) -> tuple[int, ...]:
    """Return a stable identity used only to canonicalize simultaneous tap order."""
    if shooter.connection_id is None:
        return (shooter.id,)
    return tuple(sorted(engine.level.connections[shooter.connection_id]))


def _apply(engine: Engine, state: GameState, action: Action) -> GameState:
    """Apply one checked action and return only the resulting canonical state."""
    if action.kind is ActionKind.ADVANCE:
        return engine.advance(state)[0]
    if action.index is None or action.shooter_id is None:
        raise ReplayError(f"{action.kind} requires an index and shooter id")
    if action.kind is ActionKind.LAUNCH_LANE:
        lane = engine.level.lanes[action.index]
        actual = lane[state.lane_heads[action.index]].id
        if actual != action.shooter_id:
            raise ReplayError(f"expected shooter {action.shooter_id}, found lane shooter {actual}")
        return engine.launch_lane(state, action.index)
    actual = state.tray[action.index].id
    if actual != action.shooter_id:
        raise ReplayError(f"expected shooter {action.shooter_id}, found tray shooter {actual}")
    return engine.launch_tray(state, action.index)


def _forced_chain(
    engine: Engine,
    state: GameState,
    action: Action,
    planning_spacing: int = 0,
) -> tuple[tuple[GameState, GameState, Action], ...]:
    """Apply one choice, then cross states where advancing is the only legal action."""
    chain: list[tuple[GameState, GameState, Action]] = []
    while True:
        child = _apply(engine, state, action)
        chain.append((child, state, action))
        if child.outcome(engine.level) is not Outcome.IN_PROGRESS:
            return tuple(chain)
        actions = _legal_actions(engine, child, planning_spacing)
        if len(actions) != 1 or actions[0].kind is not ActionKind.ADVANCE:
            return tuple(chain)
        state = child
        action = actions[0]


def _rank(engine: Engine, state: GameState) -> tuple[int, ...]:
    """Prefer progress and shooters that can reach an exposed matching frontier."""
    unreachable = 0
    distance = 0
    connected_gap = engine.rules.connected_gap or 0
    for group in state.active:
        for member_index, shooter in enumerate(group.shooters):
            if shooter.ammo == 0:
                continue
            position = group.position + (len(group.shooters) - member_index - 1) * connected_gap
            next_hit = _next_matching_frontier(engine, state, position, shooter.material)
            if next_hit is None:
                unreachable += 1
                distance += len(engine.level.lap)
            else:
                distance += next_hit
    lock_depth = sum(
        index - state.lane_heads[lane_index]
        for lane_index, lane in enumerate(engine.level.lanes)
        for index, shooter in enumerate(lane)
        if shooter.id in state.locked
    )
    remaining_pipe_pixels = sum(
        pipe.total_count - consumed
        for pipe, consumed in zip(engine.level.pixel_pipes, state.pipe_progress, strict=True)
    )
    return (
        sum(state.health) + remaining_pipe_pixels,
        len(state.locked),
        lock_depth,
        -len(state.freed_keys),
        len(state.waiting_keys),
        unreachable,
        len(state.tray),
        distance,
        sum(shooter.ammo for shooter in state.tray),
        -sum(state.lane_heads),
        -sum(len(group.shooters) for group in state.active),
    )


def _priority(
    engine: Engine,
    state: GameState,
    rank: tuple[int, ...] | None = None,
) -> tuple[int, ...]:
    """Balance board progress against lock, tray, and reachability pressure."""
    if _is_large_plain_board(engine.level):
        remaining_pipe_pixels = sum(
            pipe.total_count - consumed
            for pipe, consumed in zip(
                engine.level.pixel_pipes,
                state.pipe_progress,
                strict=True,
            )
        )
        return _prefer_pipe_progress(
            engine,
            state,
            (
                sum(state.health) + remaining_pipe_pixels,
                len(state.tray),
                sum(shooter.ammo for shooter in state.tray),
                -sum(state.lane_heads),
                -sum(len(group.shooters) for group in state.active),
            ),
        )
    rank = _rank(engine, state) if rank is None else rank
    if not engine.level.keys:
        has_shared_target = any(
            target.health > 1 or len(target.cells) > 1 for target in engine.level.targets
        )
        has_large_connected_group = any(len(group) >= 3 for group in engine.level.connections)
        if not has_shared_target and not has_large_connected_group:
            return _prefer_pipe_progress(
                engine,
                state,
                (
                    rank[0],  # remaining board and pipe work
                    rank[6],  # occupied waiting slots
                    rank[8],  # ammunition stranded in those slots
                    rank[5],  # active shooters with no reachable target
                    rank[7],  # distance to the next matching target
                    rank[9],  # negative queue progress
                    rank[10],  # negative active shooter count
                    *rank[1:6],
                ),
            )
        weights = (10, 100, 5, 10, 5, 10, 40, 1, 2, 30, 0)
        score = sum(value * weight for value, weight in zip(rank, weights, strict=True))
        return _prefer_pipe_progress(engine, state, (score, *rank))
    front_ids = {lane[0].id for lane in engine.level.lanes if lane}
    all_locks_start_at_front = bool(engine.level.lock_ids) and engine.level.lock_ids <= front_ids
    if all_locks_start_at_front:
        weights = (10, 300, 30, 40, 5, 10, 40, 1, 2, 20, 0)
    elif _buried_lock_needs_reachability_pressure(engine.level):
        weights = (10, 80, 3, 10, 5, 150, 120, 3, 3, 50, 0)
    else:
        weights = (10, 100, 5, 20, 5, 20, 50, 1, 2, 5, 0)
    retracting_blocker_health = sum(
        state.health[index]
        for index, target in enumerate(engine.level.targets)
        if target.retraction_length is not None
    )
    score = (
        sum(value * weight for value, weight in zip(rank, weights, strict=True))
        + 30 * retracting_blocker_health
    )
    return _prefer_pipe_progress(engine, state, (score, *rank))


def _multi_queue_priorities(engine: Engine, state: GameState) -> tuple[tuple[int, ...], ...]:
    """Project one exact state into four independent search views."""
    rank = _rank(engine, state)
    remaining_work = rank[0]
    queue_progress = rank[9]
    return (
        _priority(engine, state, rank),
        _prefer_pipe_progress(
            engine,
            state,
            (
                remaining_work,
                queue_progress,
                rank[1],
                rank[6],
                rank[5],
                rank[7],
            ),
        ),
        (
            rank[6],
            rank[8],
            rank[5],
            rank[7],
            remaining_work,
            queue_progress,
            rank[1],
        ),
        (
            rank[1],
            rank[2],
            rank[4],
            rank[3],
            queue_progress,
            remaining_work,
            rank[6],
        ),
    )


def _beam_priority(
    engine: Engine,
    state: GameState,
    policy: BeamPolicy,
) -> tuple[int, ...]:
    """Order beam states under one named, reproducible planning policy."""
    if policy is BeamPolicy.QUEUE_PROGRESS:
        return (
            -sum(state.lane_heads),
            sum(state.health),
            len(state.tray),
            sum(shooter.ammo for shooter in state.tray),
            -sum(len(group.shooters) for group in state.active),
        )
    return _priority(engine, state)


def _prefer_pipe_progress(
    engine: Engine,
    state: GameState,
    priority: tuple[int, ...],
) -> tuple[int, ...]:
    """Finish the pipe's current colour before equally safe ordinary cleanup."""
    if not engine.level.pixel_pipes:
        return priority
    remaining = sum(
        pipe.total_count - consumed
        for pipe, consumed in zip(engine.level.pixel_pipes, state.pipe_progress, strict=True)
    )
    return (remaining, *priority)


def _is_large_plain_board(level: Level) -> bool:
    """Identify boards where frontier-ray ranking costs more than its late tie-break value."""
    return (
        len(level.targets) >= 900
        and not level.keys
        and not any(len(group) >= 3 for group in level.connections)
        and all(target.health == 1 and len(target.cells) == 1 for target in level.targets)
    )


def _search_priority(
    engine: Engine,
    state: GameState,
    depth: int,
    depth_weight: int,
) -> tuple[int, ...]:
    """Add path cost when a level's queue topology creates unusually long cycles."""
    priority = _priority(engine, state)
    return (priority[0] + depth * depth_weight, *priority[1:])


def _long_cycle_depth_weight(level: Level) -> int:
    """Price long paths only for buried locks with more linked groups than tray slots."""
    profile = queue_scheduling_profile(level)
    if len(level.lock_ids) != 1 or profile.connected_group_overflow == 0:
        return 0
    return 20 if profile.has_buried_lock else 0


def _buried_lock_needs_reachability_pressure(level: Level) -> bool:
    """Identify a single buried unlock whose search is dominated by tray scheduling."""
    profile = queue_scheduling_profile(level)
    return (
        len(level.lock_ids) == 1
        and profile.has_buried_lock
        and profile.connected_group_overflow == 0
    )


def _next_matching_frontier(
    engine: Engine,
    state: GameState,
    position: int,
    material: int,
) -> int | None:
    """Return belt distance to the next currently exposed target of one material."""
    for distance, ray in enumerate(engine.level.lap[position:]):
        for item in ray.objects:
            if item.kind is RayObjectKind.WALL:
                break
            if item.kind is RayObjectKind.KEY:
                if item.index not in state.freed_keys:
                    return distance
                continue
            if not engine.is_live_target(item, state.health):
                continue
            if engine.level.targets[item.index].material == material:
                return distance
            break
    return None


def _reconstruct(
    predecessor: dict[GameState, tuple[GameState, Action]],
    state: GameState,
) -> tuple[Action, ...]:
    """Walk predecessor links back to the initial state."""
    reversed_actions: list[Action] = []
    while state in predecessor:
        state, action = predecessor[state]
        reversed_actions.append(action)
    return tuple(reversed(reversed_actions))
