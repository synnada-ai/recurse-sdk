# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Recurse tools for the conveyor shooter level designer: a minimal loop over the toolkit."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import certification
import image_baseline
import toolkit

import recurse

_Tool = Callable[..., Any]
_READ_ONLY_TOOLS = frozenset({"compare_image_variants"})


class DesignSession:
    """Opaque run-local design workspace passed through agent storage."""

    def __init__(self, tools: dict[str, _Tool]) -> None:
        """Bind the original tool closures to one isolated design session."""
        self.tools = tools
        self.revision = 0

    def __repr__(self) -> str:
        """Show the agent a compact state handle instead of workspace internals."""
        return f"DesignSession(revision={self.revision})"

    def __getstate__(self) -> dict[str, int]:
        """Expose only a deterministic mutation marker to the runtime bridge."""
        return {"revision": self.revision}


def _context_inputs() -> dict[str, Any]:
    """Return the immutable run inputs as ordinary values."""
    return dict(recurse.context().inputs)


def _format_task(inputs: dict[str, Any]) -> str:
    """Render the original request task without introducing new choices."""
    task = (
        "Select a recognizable deterministic pixel conversion of the approved source "
        "image, then turn it into one replay-certified conveyor shooter level "
        "without destroying its comprehensibility."
    )
    brief = str(inputs.get("brief", ""))
    if brief:
        task += f" Direction from the reviewer: {brief}"
    return task


def _build_tools() -> DesignSession:
    """Create the original run-private workspace and tool set."""
    inputs = _context_inputs()
    supplied = inputs.get("source_image_base64")
    if not isinstance(supplied, str):
        raise ValueError("source_image_base64 is required")
    encoded = supplied.partition(",")[2] or supplied
    try:
        source_image = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise ValueError("source_image_base64 must contain valid base64") from error
    try:
        source_dimensions = image_baseline.png_dimensions(source_image)
    except ValueError as error:
        raise ValueError(
            f"source_image_base64 must contain a structurally valid normalized PNG: {error}"
        ) from error

    crop_value = inputs.get("crop")
    crop: tuple[int, int, int, int] | None = None
    if crop_value is not None:
        if (
            not isinstance(crop_value, (list, tuple))
            or len(crop_value) != 4
            or any(not isinstance(value, int) for value in crop_value)
        ):
            raise ValueError("crop must contain exactly four integers")
        crop = cast(tuple[int, int, int, int], tuple(crop_value))
        left, top, right, bottom = crop
        source_width, source_height = source_dimensions
        if not (0 <= left < right <= source_width and 0 <= top < bottom <= source_height):
            raise ValueError(
                f"crop {crop!r} must satisfy 0 <= left < right <= {source_width} "
                f"and 0 <= top < bottom <= {source_height}"
            )

    workspace = toolkit.Workspace(
        Path(recurse.context().workspace),
        lambda: certification.load_context(
            Path(__file__).parent / "data" / "campaign-context.json"
        ),
        task=_format_task(inputs),
        source_image=source_image,
        source_width=int(inputs.get("width", 35)),
        source_height=int(inputs.get("height", 40)),
        source_crop=crop,
    )
    return DesignSession({tool.__name__: tool for tool in toolkit.make_tools(workspace)})


def begin_design() -> DesignSession:
    """Start one isolated design session from the run's source image.

    Returns:
        An opaque session. Save it as ``design_session`` and pass
        ``{"storage_key": "design_session"}`` to every other tool call.
    """
    return _build_tools()


def _decode_array(value: str, parameter: str) -> list[Any]:
    """Decode one structured parameter carried through the scalar bridge."""
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{parameter} must be a valid JSON array") from error
    if not isinstance(decoded, list):
        raise ValueError(f"{parameter} must be a JSON array")
    return decoded


def _call(session: DesignSession, tool_name: str, **arguments: Any) -> str:
    """Call one toolkit tool, encode structured output as JSON text, and log the call."""
    try:
        result = session.tools[tool_name](**arguments)
    except ValueError as error:
        _log(tool_name, arguments, f"ERROR: {error}")
        raise
    if tool_name not in _READ_ONLY_TOOLS:
        session.revision += 1
    if not isinstance(result, str):
        result = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    _log(tool_name, arguments, result)
    return result


def _log(tool_name: str, arguments: dict[str, Any], result: str) -> None:
    """Append one call to history.jsonl, the artifact that shows how the design was revised."""
    entry = {"tool": tool_name, "arguments": arguments, "result": result[:600]}
    with (Path(recurse.context().workspace) / "history.jsonl").open("a") as history:
        history.write(json.dumps(entry) + "\n")


def extract_image_variants(session: DesignSession) -> str:
    """List the deterministic pixel-grid conversions of the source image.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.

    Returns:
        A JSON array of candidate names to inspect, compare and freeze.
    """
    return _call(session, "extract_image_variants")


def look_image_variant(session: DesignSession, candidate: str) -> str:
    """See one deterministic image conversion as a player-facing pixel grid.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.
        candidate: A name returned by extract_image_variants.

    Returns:
        A PNG data URI of that conversion.
    """
    return _call(session, "look_image_variant", candidate=candidate)


def compare_image_variants(session: DesignSession) -> str:
    """Measure every conversion to diagnose noise or clumping before choosing one.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.

    Returns:
        JSON mapping each candidate to its cell count, colour count, dominant colour share,
        colour-boundary share, local colour diversity and structured-detail share. These are
        diagnostics, not a recognizability score.
    """
    return _call(session, "compare_image_variants")


def freeze_image_baseline(session: DesignSession, candidate: str, observed_description: str) -> str:
    """Freeze one complete deterministic conversion as this run's immutable source art.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.
        candidate: A name returned by extract_image_variants.
        observed_description: A sentence naming what the chosen grid visibly depicts.

    Returns:
        JSON with the frozen grid size, cube count, source_occupied_share and the
        required_occupied_floor the finished board must reach.
    """
    return _call(
        session,
        "freeze_image_baseline",
        candidate=candidate,
        observed_description=observed_description,
    )


def start_from_source(
    session: DesignSession, observed_description: str, discard_unsaved: bool = False
) -> str:
    """Start the playable candidate from the exact frozen pixel grid.

    Call it again with discard_unsaved=true to drop the current draft and try another idea.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.
        observed_description: Honest description of what the source visibly depicts.
        discard_unsaved: Explicitly replace an existing unsaved draft.

    Returns:
        JSON with the source dimensions, cube and material counts, and occupied_share.
    """
    return _call(
        session,
        "start_from_source",
        observed_description=observed_description,
        discard_unsaved=discard_unsaved,
    )


def inspect_source_coordinates(session: DesignSession) -> str:
    """Inspect exact source coordinates and materials for deliberate key placement.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.

    Returns:
        A top-first coordinate map. Each two-character token is a material id in hex;
        ``..`` is empty. Native y increases upward, as labelled on every row.
    """
    return _call(session, "inspect_source_coordinates")


def apply_composition_school(
    session: DesignSession, school: str, palette_json: str, seed: int = 151
) -> str:
    """Arrange gameplay pixels around the frozen source using one composition school.

    The source must be freshly restored with start_from_source before every trial. The
    operation never changes a frozen source pixel. Surrounding pixels are real cubes and
    gameplay demand; they raise occupancy toward the required floor.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.
        school: One of segmented-squeeze, scenic-field, layered-inset-frame or
            peripheral-compartments.
        palette_json: JSON array of at least four integer material ids for the surrounding
            field, for example "[2,3,4,5]".
        seed: Deterministic within-school variation.

    Returns:
        JSON with added cells, occupied_share, the school and source-safe mechanic anchors.
    """
    return _call(
        session,
        "apply_composition_school",
        school=school,
        palette=_decode_array(palette_json, "palette_json"),
        seed=seed,
    )


def place_key(session: DesignSession, x: int, y: int, replace_art: bool = True) -> str:
    """Turn one 2x2 visual feature into a functional key ornament.

    A key becomes available when shooter fire gains a clear path to it. It immediately opens
    the leftmost front queue lock, or remains a physical obstacle until such a lock appears.
    A key with a clear firing line from any board edge at the start is rejected by
    certification, so bury each key behind ordinary pixels on all four sides. Use keys as
    eyespots, buttons, jewels or other repeated details, not arbitrary holes.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.
        x: Leftmost native grid coordinate of the 2x2 key.
        y: Bottom native grid coordinate of the 2x2 key.
        replace_art: Explicitly replace source pixels under the key.

    Returns:
        JSON with the footprint, removed art count and the total key count.
    """
    return _call(session, "place_key", x=x, y=y, replace_art=replace_art)


def look(session: DesignSession) -> str:
    """See the exact candidate render as an image.

    save_candidate refuses to run until look has been called after the final edit.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.

    Returns:
        A PNG data URI. State what the image depicts and identify any weak feature; do not
        infer the image from the subject label you supplied.
    """
    return _call(session, "look")


def review_visual_retention(
    session: DesignSession,
    observed_description: str,
    preserved_features_json: str,
    mechanic_role: str,
) -> str:
    """Record the final visual judgment after viewing the playable board.

    save_candidate refuses to run until this review covers the final edit.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.
        observed_description: What the current render visibly depicts, without relying on
            the source filename or intended label.
        preserved_features_json: JSON array of at least two concrete silhouette or identity
            cues still visible, for example '["horned head", "spread wings"]'.
        mechanic_role: How the keys participate in the composition rather than merely
            avoiding it.

    Returns:
        JSON with the recorded visual review and its candidate revision.
    """
    return _call(
        session,
        "review_visual_retention",
        observed_description=observed_description,
        preserved_features=_decode_array(preserved_features_json, "preserved_features_json"),
        mechanic_role=mechanic_role,
    )


def configure_gameplay(
    session: DesignSession,
    lock_count: int,
    connected_groups: int,
    surprise_count: int = 0,
) -> str:
    """Build four real lanes, then solve, replay and measure the level immediately.

    This is the independent validator. Locks are inserted blocker tokens, never converted
    funded shooters. Connected pairs are chosen only at matching depths in separate lanes.
    Each call takes roughly 20 seconds.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.
        lock_count: Queue blockers, exactly one per placed board key.
        connected_groups: Aligned two-shooter groups, from 1 to 18.
        surprise_count: Hidden queue shooters, from 0 to 30.

    Returns:
        JSON with solved, violations, activity_violations, lane sizes, the mechanic trace,
        campaign pressure measurements and meets_save_gate. A candidate can be saved only
        when meets_save_gate is true.
    """
    return _call(
        session,
        "configure_gameplay",
        lock_count=lock_count,
        connected_groups=connected_groups,
        surprise_count=surprise_count,
    )


def save_candidate(session: DesignSession) -> str:
    """Re-certify and save the native level, proof, metrics and visual report.

    This is the result tool. It refuses to save a level that does not replay to a win, whose
    keys or locks never take part in that win, or that misses the campaign gate.

    Args:
        session: Design session returned by begin_design, passed as a storage reference.

    Returns:
        JSON receipt with the artifact names and the final certification summary.
    """
    return _call(session, "save_candidate")
