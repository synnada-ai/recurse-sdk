# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Slide-and-collect level-designer tool kit.

The design tools and the mutable session ``Workspace`` they close over. The
deterministic domain library stays in the sibling modules (``builder``,
``sim``, ``metrics``, ``report_util``); ``tools.py`` exposes a minimal subset
of these tools to Recurse.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import builder
import metrics
import report_util
from builder import Certificate, Design


@dataclass
class Workspace:
    """Mutable state shared by the tools of one design session."""

    slot: int
    out_dir: Path
    design: Design | None = None
    certificate: Certificate | None = None
    saved: list[dict[str, Any]] = field(default_factory=list)


def _require_design(ws: Workspace) -> Design:
    if ws.design is None:
        raise ValueError("no board yet - call new_board(width, height) first")
    return ws.design


def _board_state(ws: Workspace) -> str:
    design = _require_design(ws)
    deficit = dict(design.deficit())
    return (
        f"{design.render_ascii()}\n"
        f"deficit (settlers still needed per color): {deficit or 'none - balanced'}"
    )


def make_tools(ws: Workspace) -> list[Any]:
    """Build the design tool set closing over one workspace."""

    def window() -> str:
        """Corpus feature bands and allowed mechanics for the target slot.

        Returns:
            The p10-p95 band per feature over nearby corpus levels, plus the
            mechanic introduction schedule relative to this slot.
        """
        bands = metrics.corpus_window(ws.slot)
        lines = [f"target slot: {ws.slot}"]
        for key, (lo, hi) in sorted(bands.items()):
            marker = " (gates)" if key in metrics.ADMITTED else ""
            lines.append(f"  {key}: {lo:g} .. {hi:g}{marker}")
        allowed = [m for m, n in metrics.MECHANIC_INTRODUCED.items() if n <= ws.slot]
        lines.append("mechanics allowed at this slot: " + ", ".join(allowed))
        return "\n".join(lines)

    def new_board(width: int, height: int) -> str:
        """Start a fresh design with an empty board.

        Args:
            width: Board width in cells (corpus range 3-9).
            height: Board height in cells (corpus range 4-12); pick height >
                width - the studio ships portrait boards (occasionally square,
                never landscape).

        Returns:
            The empty board.
        """
        ws.design = Design(width, height)
        ws.certificate = None
        return _board_state(ws)

    def place_seat(
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
            shape_id: Shape template 0-7.
            x: Anchor cell x.
            y: Anchor cell y.
            color: Color index 0-9.
            rotation: Quarter-turns clockwise, 0-3.
            mirrored: Mirror across X before rotating.

        Returns:
            The updated board and the seat's index.
        """
        design = _require_design(ws)
        index = design.place_seat(shape_id, (x, y), color, rotation=rotation, mirrored=mirrored)
        return f"seat {index} placed\n{_board_state(ws)}"

    def add_obstacle(x: int, y: int) -> str:
        """Permanently block one cell.

        Args:
            x: Cell x.
            y: Cell y.

        Returns:
            The updated board.
        """
        design = _require_design(ws)
        design.add_obstacle((x, y))
        return _board_state(ws)

    def add_elevator(x: int, y: int, dx: int, dy: int) -> str:
        """Add a settler queue feeding cell (x, y) from direction (dx, dy).

        Corpus elevators sit on board edges with the offset pointing outward
        (e.g. bottom edge: dy=-1); interior spawners use (0, 0).

        Args:
            x: Fed cell x.
            y: Fed cell y.
            dx: Outward offset x (-1, 0 or 1).
            dy: Outward offset y (-1, 0 or 1).

        Returns:
            The updated board.
        """
        design = _require_design(ws)
        design.add_elevator((x, y), (dx, dy))
        return _board_state(ws)

    def set_inner(seat_index: int, color: int) -> str:
        """Give a seat a hidden second layer of another color (doubles demand).

        Args:
            seat_index: Index returned by place_seat.
            color: Inner layer color, different from the outer.

        Returns:
            The updated board.
        """
        design = _require_design(ws)
        design.set_inner(seat_index, color)
        return _board_state(ws)

    def connect_seats(seat_a: int, seat_b: int) -> str:
        """Weld two adjacent, differently-colored seats into one rigid pair.

        Args:
            seat_a: First seat index.
            seat_b: Second seat index.

        Returns:
            The updated board.
        """
        design = _require_design(ws)
        design.connect(seat_a, seat_b)
        return _board_state(ws)

    def set_axis_lock(seat_index: int, axis: int) -> str:
        """Restrict a seat to one movement axis.

        Args:
            seat_index: Seat to restrict.
            axis: 1 = vertical only, 2 = horizontal only.

        Returns:
            The updated board.
        """
        design = _require_design(ws)
        design.set_axis_lock(seat_index, axis)
        return _board_state(ws)

    def set_lock(seat_index: int, lock_id: int) -> str:
        """Chain a seat; it stays inert until all keys with this id are collected.

        Place the keys afterwards with add_settler(..., key_id=lock_id) on
        settlers whose color belongs to a DIFFERENT, unchained seat.

        Args:
            seat_index: Seat to chain.
            lock_id: Lock family id (small positive int).

        Returns:
            The updated board.
        """
        design = _require_design(ws)
        design.set_lock(seat_index, lock_id)
        return _board_state(ws)

    def assign_keys(lock_id: int, count: int = 1, color: int | None = None) -> str:
        """Turn placed settlers into key carriers for a chained seat (after populate).

        Args:
            lock_id: The id used in set_lock.
            count: Number of keys (the padlock requires all of them).
            color: Optional carrier color; defaults to any unchained seat's color.

        Returns:
            The updated board.
        """
        design = _require_design(ws)
        n = design.assign_keys(lock_id, count=count, color=color)
        return f"{n} key(s) assigned to lock {lock_id}\n{_board_state(ws)}"

    def add_settler(x: int, y: int, color: int, key_id: int | None = None) -> str:
        """Stand one settler on a free cell (use for keys or deliberate placement).

        Args:
            x: Cell x.
            y: Cell y.
            color: Settler color; must still have seat capacity left.
            key_id: Lock id if this settler carries a key.

        Returns:
            The updated board.
        """
        design = _require_design(ws)
        design.add_settler((x, y), color, key_id=key_id)
        return _board_state(ws)

    def populate(
        seed: int = 0,
        queue_fraction: float = 0.0,
        adjacency_fraction: float = 0.4,
    ) -> str:
        """Fill the whole remaining settler deficit automatically.

        Args:
            seed: Re-roll with different seeds until the scatter looks natural.
            queue_fraction: Share of settlers dealt into elevator queues.
            adjacency_fraction: Target share of board settlers starting adjacent
                to their seat (corpus is ~0.4).

        Returns:
            The populated board and its feature summary.
        """
        design = _require_design(ws)
        design.auto_populate(
            seed=seed,
            queue_fraction=queue_fraction,
            adjacency_fraction=adjacency_fraction,
        )
        feats = metrics.level_features(design.to_level())
        summary = ", ".join(f"{k}={v:g}" for k, v in sorted(feats.items()))
        return f"{_board_state(ws)}\nfeatures: {summary}"

    def clear_settlers() -> str:
        """Remove all placed settlers and queued elevator settlers (keeps seats).

        Returns:
            The emptied board, ready for a fresh populate().
        """
        design = _require_design(ws)
        design.settlers.clear()
        for elevator in design.elevators:
            elevator.queue.clear()
        return _board_state(ws)

    def critique_design() -> str:
        """Certify solvability and judge the design against its slot's corpus window.

        Returns:
            FAIL/WARN/INFO findings; fix every FAIL before saving.
        """
        design = _require_design(ws)
        ws.certificate = design.certify()
        findings = metrics.critique(
            design.to_level(f"Gen_{ws.slot}"),
            ws.slot,
            certificate_ok=ws.certificate.ok,
        )
        hints = {
            "band_n_board_settlers": "add seats (or an inner layer) for more capacity, "
            "then clear_settlers() and populate() again",
            "band_headroom": "adjust seat total or board size",
            "band_board_cells": "restart with new_board() at a size inside the window",
            "band_adjacency_frac": "re-populate with adjacency_fraction nearer 0.4",
            "band_queued_total": "queues are not used in this example; keep queued_total at 0",
            "solvable": "re-populate with another seed or a lower adjacency_fraction, or thin "
            "the board so every seat has a sliding route to its settlers",
        }
        lines = []
        for f in findings:
            hint = f" -> {hints[f.check]}" if f.severity == "FAIL" and f.check in hints else ""
            lines.append(f"{f.severity}: {f.check} - {f.detail}{hint}")
        if ws.certificate.ok:
            lines.append(
                f"INFO: solve - fifo taps {ws.certificate.fifo.taps}, "
                f"order-robust: {ws.certificate.order_robust}"
            )
        return "\n".join(lines)

    def save_design(note: str) -> str:
        """Re-check everything and write the level to disk; refuses on any FAIL.

        Args:
            note: One-line design intent, stored alongside the level.

        Returns:
            The saved file path.
        """
        design = _require_design(ws)
        ws.certificate = design.certify()
        findings = metrics.critique(
            design.to_level(f"Gen_{ws.slot}"),
            ws.slot,
            certificate_ok=ws.certificate.ok,
        )
        fails = [f for f in findings if f.severity == "FAIL"]
        if fails:
            raise ValueError(
                "cannot save, critique FAILs remain: "
                + "; ".join(f"{f.check}: {f.detail}" for f in fails)
            )
        ws.out_dir.mkdir(parents=True, exist_ok=True)
        variant = builder.variant_for_slot(ws.slot)
        path = ws.out_dir / f"Gen_{ws.slot}.json"
        builder.save_level(design, path, variant=variant, seed=ws.slot)
        ws.saved.append(
            {
                "slot": ws.slot,
                "path": str(path),
                "note": note,
                "variant": variant,
                "taps": ws.certificate.fifo.taps,
                "order_robust": ws.certificate.order_robust,
            }
        )
        report_util.write_report(ws.saved, ws.out_dir, note=f"slot {ws.slot}")
        return f"saved {path.name} (variant {variant})"

    return [
        window,
        new_board,
        place_seat,
        add_obstacle,
        add_elevator,
        set_inner,
        connect_seats,
        set_axis_lock,
        set_lock,
        assign_keys,
        add_settler,
        populate,
        clear_settlers,
        critique_design,
        save_design,
    ]
