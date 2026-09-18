# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Authoritative conveyor shooter parser and event-driven gameplay engine."""

from .engine import Engine, Event, EventKind, GameRules, GameState, Outcome
from .model import (
    Direction,
    Level,
    QueueSchedulingProfile,
    Ray,
    Shooter,
    Target,
    demand_by_material,
    queue_scheduling_profile,
)
from .solver import (
    Action,
    ActionKind,
    BeamPolicy,
    ReplayError,
    SolveResult,
    SolveStatus,
    replay,
    solve,
    solve_beam,
    solve_experimental_portfolio,
    solve_multi_queue,
    solve_portfolio,
)

__all__ = [
    "Action",
    "ActionKind",
    "BeamPolicy",
    "Direction",
    "Engine",
    "Event",
    "EventKind",
    "GameRules",
    "GameState",
    "Level",
    "Outcome",
    "QueueSchedulingProfile",
    "Ray",
    "ReplayError",
    "Shooter",
    "SolveResult",
    "SolveStatus",
    "Target",
    "demand_by_material",
    "queue_scheduling_profile",
    "replay",
    "solve",
    "solve_beam",
    "solve_experimental_portfolio",
    "solve_multi_queue",
    "solve_portfolio",
]
