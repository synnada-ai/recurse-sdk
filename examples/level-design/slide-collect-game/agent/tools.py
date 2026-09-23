# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Recurse tools for the slide-and-collect level designer: a minimal loop over the toolkit."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import toolkit

import recurse

_Tool = Callable[..., str]
_READ_ONLY_TOOLS = frozenset({"window"})


class DesignSession:
    """Opaque run-local design workspace passed through agent storage."""

    def __init__(self, tools: dict[str, _Tool]) -> None:
        """Bind the toolkit closures to one isolated design session."""
        self.tools = tools
        self.revision = 0

    def __repr__(self) -> str:
        """Show the agent a compact state handle instead of workspace internals."""
        return f"DesignSession(revision={self.revision})"

    def __getstate__(self) -> dict[str, int]:
        """Expose only a deterministic mutation marker to the runtime bridge."""
        return {"revision": self.revision}


def _call(session: DesignSession, tool_name: str, **arguments: Any) -> str:
    """Call one toolkit tool, track design changes, and log the call as run evidence."""
    try:
        result = session.tools[tool_name](**arguments)
    except ValueError as error:
        _log(tool_name, arguments, f"ERROR: {error}")
        raise
    if tool_name not in _READ_ONLY_TOOLS:
        session.revision += 1
    _log(tool_name, arguments, result)
    return result


def _log(tool_name: str, arguments: dict[str, Any], result: str) -> None:
    """Append one call to history.jsonl, the artifact that shows how the design was revised."""
    entry = {"tool": tool_name, "arguments": arguments, "result": result[:600]}
    with (Path(recurse.context().workspace) / "history.jsonl").open("a") as history:
        history.write(json.dumps(entry) + "\n")


def begin_design() -> DesignSession:
    """Start one isolated design session for the run's target slot.

    Returns:
        An opaque session. Save it as ``design_session`` and pass
        ``{"storage_key": "design_session"}`` to every other tool call.
    """
    context = recurse.context()
    workspace = toolkit.Workspace(
        slot=int(context.inputs.get("slot", 34)), out_dir=Path(context.workspace)
    )
    return DesignSession({tool.__name__: tool for tool in toolkit.make_tools(workspace)})


def window(session: DesignSession) -> str:
    """Reference feature bands and allowed mechanics for the target slot.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.

    Returns:
        The p10-p95 band per feature over nearby reference levels (features marked
        "(gates)" can fail a design), plus the mechanics allowed at this slot.
    """
    return _call(session, "window")


def new_board(session: DesignSession, width: int, height: int) -> str:
    """Start a fresh design with an empty board, discarding the current one.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.
        width: Board width in cells (reference range 3-9).
        height: Board height in cells (reference range 4-12); pick height > width.
            Reference boards are portrait, occasionally square, never landscape.

    Returns:
        The empty board.
    """
    return _call(session, "new_board", width=width, height=height)


def place_seat(  # noqa: PLR0913, PLR0917 - parameters mirror the seat record
    session: DesignSession,
    shape_id: int,
    x: int,
    y: int,
    color: int,
    rotation: int = 0,
    mirrored: bool = False,
) -> str:
    """Place a colored polyomino seat.

    Shapes: 0 single, 1 domino, 2 I-tromino, 3 T-tetromino, 4 plus-pentomino,
    5 L-tetromino, 6 L-tromino, 7 2x2 square. Rotation is quarter-turns
    clockwise; (x, y) is the anchor cell (the middle cell for shapes 2-4).

    Args:
        session: Design session returned by begin_design, passed as a storage reference.
        shape_id: Shape template 0-7.
        x: Anchor cell x.
        y: Anchor cell y.
        color: Color index 0-9.
        rotation: Quarter-turns clockwise, 0-3.
        mirrored: Mirror across X before rotating.

    Returns:
        The updated board and the seat's index.
    """
    return _call(
        session,
        "place_seat",
        shape_id=shape_id,
        x=x,
        y=y,
        color=color,
        rotation=rotation,
        mirrored=mirrored,
    )


def add_obstacle(session: DesignSession, x: int, y: int) -> str:
    """Permanently block one cell.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.
        x: Cell x.
        y: Cell y.

    Returns:
        The updated board.
    """
    return _call(session, "add_obstacle", x=x, y=y)


def set_inner(session: DesignSession, seat_index: int, color: int) -> str:
    """Give a seat a hidden second layer of another color (doubles its demand).

    Neither color of a layered seat may be used by any other seat.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.
        seat_index: Index returned by place_seat.
        color: Inner layer color, different from the outer.

    Returns:
        The updated board.
    """
    return _call(session, "set_inner", seat_index=seat_index, color=color)


def populate(
    session: DesignSession,
    seed: int = 0,
    adjacency_fraction: float = 0.4,
) -> str:
    """Fill the whole remaining settler deficit automatically.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.
        seed: Re-roll with different seeds until the scatter looks natural.
        adjacency_fraction: Target share of settlers starting adjacent to their seat
            (reference levels are around 0.4).

    Returns:
        The populated board and its feature summary.
    """
    return _call(
        session, "populate", seed=seed, queue_fraction=0.0, adjacency_fraction=adjacency_fraction
    )


def clear_settlers(session: DesignSession) -> str:
    """Remove all placed settlers (keeps seats and obstacles).

    Args:
        session: Design session returned by begin_design, passed as a storage reference.

    Returns:
        The emptied board, ready for a fresh populate().
    """
    return _call(session, "clear_settlers")


def critique_design(session: DesignSession) -> str:
    """Certify solvability and judge the design against its slot's reference window.

    This is the independent validator: it solves the level under the real sliding rules
    and measures the design. It can take up to a minute on crowded boards.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.

    Returns:
        FAIL/WARN/INFO findings, each with a hint where one exists, and the solution
        length in drags when solvable. Fix every FAIL before saving.
    """
    return _call(session, "critique_design")


def save_design(session: DesignSession, note: str) -> str:
    """Re-check everything and write the level to disk; refuses on any FAIL.

    This is the result tool. It writes the level JSON and an HTML report as run artifacts.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.
        note: One-line design intent, stored alongside the level.

    Returns:
        The saved file name.
    """
    return _call(session, "save_design", note=note)
