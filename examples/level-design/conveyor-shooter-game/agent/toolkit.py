# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Small, session-stateful tool surface for the L151 conveyor shooter design loop."""

import base64
import html
import json
import re
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

import audit_dimensions
import builder
import certification
import composition
import image_baseline
import render
from builder import BuildError, Design
from certification import CampaignContext
from conveyor_game.model import grid_cells

Cell = tuple[int, int]
_SOLVER_LOCK = threading.Lock()


def _serialized_certify(
    raw: dict[str, Any], context: CampaignContext
) -> certification.CertificationResult:
    """Run one solver-heavy certification at a time within the worker process."""
    with _SOLVER_LOCK:
        return certification.certify(raw, context)


def _serialized_certify_fast(
    raw: dict[str, Any], context: CampaignContext
) -> certification.CertificationResult:
    """Run one bounded exact preflight at a time within the worker process."""
    with _SOLVER_LOCK:
        return certification.certify_fast(raw, context)


def _art_feedback(raw: dict[str, Any], context: CampaignContext) -> dict[str, Any]:
    """Compare the frozen opening artwork with recent structured-richness bands."""
    artwork = audit_dimensions.artwork_metrics(raw)
    comparisons: dict[str, dict[str, float | bool]] = {}
    advice: list[str] = []
    guidance = {
        "regions_per_100_cells": (
            "Use more connected internal features when low; merge visual noise when high."
        ),
        "local_colour_diversity_3x3": (
            "Add nearby outline, shade and highlight transitions across the subject."
        ),
        "structured_detail_share": (
            "Add meaningful connected details of 2-12 cells, such as eyes, seams or texture."
        ),
        "colour_boundary_share": (
            "Break broad flat fields with coherent internal contours, shading and markings."
        ),
        "palette_entropy": (
            "Redistribute colour area across coherent shades instead of one dominant fill."
        ),
        "dominant_colour_share": (
            "Reduce the largest flat colour mass while preserving a readable silhouette."
        ),
    }
    for name in sorted(certification.RICHNESS_DIMENSIONS):
        value = artwork[name]
        band = context.target_band[name]
        inside = band.contains(value)
        comparisons[name] = {
            "value": value,
            "low": band.low,
            "median": band.median,
            "high": band.high,
            "inside": inside,
        }
        if not inside:
            direction = "low" if value < band.low else "high"
            gap = band.low - value if direction == "low" else value - band.high
            advice.append(
                f"{name} is too {direction}: {value:.4f}; required band "
                f"{band.low:.4f}-{band.high:.4f}; gap {gap:.4f}. {guidance[name]}"
            )
    inside_count = sum(bool(result["inside"]) for result in comparisons.values())
    return {
        "richness_inside": inside_count,
        "richness_total": len(certification.RICHNESS_DIMENSIONS),
        "richness_ready": inside_count >= 5,
        "comparisons": comparisons,
        "advice": advice,
    }


def _meets_save_gate(ws: Any, measurement: certification.CampaignMeasurement) -> bool:
    """Apply the correct acceptance gate for authored versus approved-source artwork."""
    if ws.source_art is not None:
        return certification.source_campaign_gate(measurement)
    return certification.campaign_gate(measurement)


def _art_raw(width: int, height: int, picture: dict[Cell, int]) -> dict[str, Any]:
    """Return the minimal native surface needed by renderers and art metrics."""
    return {
        "PixelImageData": {
            "width": width,
            "height": height,
            "pixels": [
                {"x": x, "y": y, "material": material, "areaX": 1, "areaY": 1}
                for (x, y), material in sorted(picture.items())
            ],
        }
    }


def _source_comparison_html(ws: Any, comparison: dict[str, Any]) -> str:
    """Render source and playable board side by side for human judgment."""
    assert ws.source_art is not None
    assert ws.source_width is not None and ws.source_height is not None
    review = ws.visual_review or {}
    preserved = "".join(
        f"<li>{html.escape(str(feature))}</li>" for feature in review.get("preserved_features", [])
    )
    return (
        "<!doctype html><meta charset=utf-8><title>conveyor shooter source retention</title>"
        "<style>body{margin:0;background:#111117;color:#f2f1f7;font:16px system-ui}"
        "main{max-width:1100px;margin:auto;padding:28px}.boards{display:grid;"
        "grid-template-columns:1fr 1fr;gap:24px}section{background:#191920;padding:18px;"
        "border-radius:14px}svg{width:100%;height:auto;background:#0b0b10}"
        "p,li{color:#b7b5c3}code{color:#65d9ff}@media(max-width:720px){.boards{"
        "grid-template-columns:1fr}}</style><main><h1>Source → playable level</h1>"
        f"<div class=boards><section><h2>Approved source</h2>"
        f"{render.svg_board(_art_raw(ws.source_width, ws.source_height, ws.source_art), 16)}"
        f"<p>{html.escape(ws.source_description)}</p>"
        f"</section><section><h2>Playable candidate</h2>{render.svg_board(ws.raw(), 16)}"
        "</section></div>"
        f"<p>{html.escape(str(review.get('observed_description', '')))}</p>"
        "<p><strong>Mechanic role:</strong> "
        f"{html.escape(str(review.get('mechanic_role', '')))}</p>"
        f"<ul>{preserved}</ul><pre>{html.escape(json.dumps(comparison, indent=2))}</pre>"
        "</main>"
    )


class Workspace:
    """One run-private candidate and the evidence used to judge it."""

    def __init__(
        self,
        out_dir: Path,
        context_loader: Callable[[], CampaignContext],
        *,
        source_image: bytes,
        source_width: int,
        source_height: int,
        source_crop: tuple[int, int, int, int] | None,
        task: str = "",
    ) -> None:
        """Create a source-image-backed L151 design session."""
        self.out_dir = out_dir
        self._context_loader = context_loader
        self._context: CampaignContext | None = None
        self.task = task
        self.source_image = source_image
        self.requested_source_width = source_width
        self.requested_source_height = source_height
        self.source_crop = source_crop
        self.source_description = ""
        self.design: Design | None = None
        self.subject = ""
        self.saved = False
        self.last_result: certification.CertificationResult | None = None
        self.queues: list[list[dict[str, Any]]] | None = None
        self.revision = 0
        self.patch_calls = 0
        self.patch_cells = 0
        self.looked_at_revision: int | None = None
        self.visual_review_revision: int | None = None
        self.visual_review: dict[str, Any] | None = None
        self.source_width: int | None = None
        self.source_height: int | None = None
        self.source_art: dict[Cell, int] | None = None
        self.source_art_sha256: str | None = None
        self.composition_school: str | None = None
        self.composition_anchors: tuple[composition.MechanicAnchor, ...] = ()
        self.variants: dict[str, dict[str, Any]] = {}
        self.trace: list[dict[str, Any]] = []

    @property
    def context(self) -> CampaignContext:
        """Resolve the versioned campaign asset on first tool use, never at import."""
        if self._context is None:
            self._context = self._context_loader()
        return self._context

    def need(self) -> Design:
        """Return the current candidate or explain how to start one."""
        if self.design is None:
            raise ValueError("no candidate yet — freeze and start from the supplied source")
        return self.design

    def raw(self) -> dict[str, Any]:
        """Emit the current candidate in native game schema."""
        return builder.to_level(self.need(), queues=self.queues)

    def record(self, event: str, **detail: Any) -> None:
        """Persist one compact diagnostic event even when the run never saves."""
        self.trace.append({"event": event, "revision": self.revision, **detail})
        diagnostics = self.out_dir / "diagnostics"
        diagnostics.mkdir(parents=True, exist_ok=True)
        (diagnostics / "trace.json").write_text(
            json.dumps(
                {
                    "schema": "conveyor-designer-trace-v1",
                    "task": self.task,
                    "subject": self.subject,
                    "events": self.trace,
                },
                indent=2,
            )
            + "\n"
        )

    def snapshot(self) -> None:
        """Persist the current native board so rejected art remains visually auditable."""
        diagnostics = self.out_dir / "diagnostics"
        diagnostics.mkdir(parents=True, exist_ok=True)
        (diagnostics / f"candidate-{self.revision:02d}.json").write_text(
            json.dumps(self.raw(), separators=(",", ":")) + "\n"
        )


def make_tools(ws: Workspace) -> list[Any]:
    """Build the stateful L151 design tools in workflow order."""
    conversion_cache: dict[str, image_baseline.Conversion] | None = None

    def image_conversions() -> dict[str, image_baseline.Conversion]:
        """Build deterministic candidates once from one decoded request image."""
        nonlocal conversion_cache
        if conversion_cache is not None:
            return conversion_cache
        prepared = image_baseline.prepare_png(ws.source_image)
        crops = (
            {"custom": ws.source_crop}
            if ws.source_crop is not None
            else image_baseline.crop_variants(prepared)
        )
        unique_crops: dict[str, tuple[int, int, int, int]] = {}
        seen: set[tuple[int, int, int, int]] = set()
        for crop_name, crop_box in crops.items():
            if crop_box not in seen:
                unique_crops[crop_name] = crop_box
                seen.add(crop_box)
        treatments: tuple[image_baseline.Variant, ...] = (
            ("crisp", "balanced", "edge", "contrast")
            if len(unique_crops) == 1
            else ("crisp", "edge", "contrast")
        )
        conversion_cache = {
            f"{crop_name}-{variant}": image_baseline.convert(
                prepared,
                width=ws.requested_source_width,
                height=ws.requested_source_height,
                crop=crop_box,
                variant=variant,
            )
            for crop_name, crop_box in unique_crops.items()
            for variant in treatments
        }
        return conversion_cache

    def look_source_image() -> str:
        """See the untouched, reviewer-approved PNG before selecting a grid conversion."""
        ws.record("image_source_viewed")
        return "data:image/png;base64," + base64.b64encode(ws.source_image).decode()

    def extract_image_variants() -> list[str]:
        """List deterministic crop and sampling candidates available for visual inspection."""
        variants = list(image_conversions())
        ws.record("image_variants_extracted", variants=variants)
        return variants

    def look_image_variant(candidate: str) -> str:
        """See one deterministic image conversion as a player-facing pixel grid."""
        normalized = candidate.strip().lower().replace(" · ", "-").replace("_", "-")
        converted = image_conversions().get(normalized)
        if converted is None:
            raise ValueError(f"unknown candidate {candidate!r}; extract variants first")
        ws.record("image_variant_viewed", candidate=normalized)
        return render.board_image_uri(converted.raw_level(), scale=8)

    def compare_image_variants() -> dict[str, dict[str, float]]:
        """Compare descriptive art metrics without treating them as recognizability scores."""
        names = (
            "cells",
            "colours",
            "local_colour_diversity_3x3",
            "structured_detail_share",
            "colour_boundary_share",
            "dominant_colour_share",
        )
        return {
            candidate: {
                name: float(audit_dimensions.artwork_metrics(converted.raw_level())[name])
                for name in names
            }
            for candidate, converted in image_conversions().items()
        }

    def freeze_image_baseline(candidate: str, observed_description: str) -> dict[str, Any]:
        """Freeze one complete deterministic conversion as this run's immutable source art."""
        if ws.design is not None:
            raise ValueError("freeze the image baseline before starting level composition")
        normalized = candidate.strip().lower().replace(" · ", "-").replace("_", "-")
        converted = image_conversions().get(normalized)
        if converted is None:
            raise ValueError(f"unknown candidate {candidate!r}; extract variants first")
        description = observed_description.strip()
        if len(description) < 3:
            raise ValueError("observed_description must identify what the chosen grid depicts")
        frozen = converted.frozen_artifact()
        frozen["candidate"] = normalized
        ws.source_width = converted.width
        ws.source_height = converted.height
        ws.source_art = dict(converted.picture)
        ws.source_art_sha256 = str(frozen["art_sha256"])
        ws.source_description = description
        ws.out_dir.mkdir(parents=True, exist_ok=True)
        (ws.out_dir / "frozen_art.json").write_text(json.dumps(frozen, indent=2) + "\n")
        (ws.out_dir / "source-art-report.html").write_text(
            "<!doctype html><meta charset=utf-8><title>Frozen conveyor shooter source</title>"
            "<style>body{margin:0;background:#111117;color:#f2f1f7;font:16px system-ui;"
            "display:grid;place-items:center;min-height:100vh}main{max-width:620px;padding:28px}"
            "svg{width:100%;height:auto;background:#0b0b10}p{color:#aaa8b7}</style>"
            f"<main><h1>{html.escape(description)}</h1>"
            f"{render.svg_board(converted.raw_level(), 16)}"
            f"<p>{normalized} · frozen {ws.source_art_sha256[:12]}</p></main>"
        )
        ws.record(
            "image_baseline_frozen",
            variant=normalized,
            art_sha256=ws.source_art_sha256,
        )
        return {
            "candidate": normalized,
            "observed_description": description,
            "art_sha256": ws.source_art_sha256,
            "width": ws.source_width,
            "height": ws.source_height,
            "cubes": len(ws.source_art),
            "source_occupied_share": round(
                len(converted.picture) / (converted.width * converted.height), 4
            ),
            "required_occupied_floor": round(ws.context.target_band["complete_fill"].low, 4),
            "artifacts": ["frozen_art.json", "source-art-report.html"],
        }

    def campaign_context() -> dict[str, Any]:
        """Study the reviewed campaign context before drawing.

        Returns:
            L151's static windows, allowed level-to-level changes, recent visual subjects,
            difficulty cadence, and the deliberately narrow mechanic scope of this slice.
        """
        context = ws.context.to_tool_result()
        context["confirmed_palette"] = dict(sorted(render.CONFIRMED_HEX.items()))
        source_fill = (
            len(ws.source_art) / (ws.source_width * ws.source_height)
            if ws.source_art is not None
            and ws.source_width is not None
            and ws.source_height is not None
            else None
        )
        context["art_contract"] = {
            "source": "reviewer-approved PNG; deterministic grid selection occurs in this run",
            "rule": (
                "Start exactly from the source, then treat it as the semantic foreground. "
                "Extend the board when required; preserve identity cues rather than empty "
                "source pixels. Mechanics may replace cells only as coherent visual features."
            ),
            "source_occupied_share": None if source_fill is None else round(source_fill, 4),
            "required_occupied_floor": round(ws.context.target_band["complete_fill"].low, 4),
            "density_methods": (
                "test an ornamental frame and a background field independently before "
                "combining them; source scaling and blanket edge growth are not admitted"
            ),
            "admitted_mechanics": (
                "up to six 2x2 keys with separate queue-lock blockers; lane-aligned "
                "connected shooters; surprise shooters; at most one Pixel Pipe; and up to two "
                "compact four-egg trays"
            ),
            "approved_description": ws.source_description or None,
        }
        return context

    def look_source_art() -> str:
        """See the approved pixelized image before changing it.

        Returns:
            The source grid as a PNG data URI. Describe what it visibly depicts before
            choosing where a mechanic should become part of the composition.
        """
        if ws.source_art is None or ws.source_width is None or ws.source_height is None:
            raise ValueError("this request did not include an approved pixelized source")
        ws.record("source_art_viewed", art_sha256=ws.source_art_sha256)
        return render.board_image_uri(
            _art_raw(ws.source_width, ws.source_height, ws.source_art), scale=6
        )

    def inspect_source_coordinates() -> str:
        """Inspect exact source coordinates and materials for deliberate mechanic placement.

        Returns:
            A top-first coordinate map. Each two-character token is a material id in hex;
            ``..`` is empty. Native y increases upward, as labelled on every row.
        """
        if ws.source_art is None or ws.source_width is None or ws.source_height is None:
            raise ValueError("this request did not include an approved pixelized source")
        rows = ["     " + " ".join(f"{x:02d}" for x in range(ws.source_width))]
        for y in range(ws.source_height - 1, -1, -1):
            tokens = [
                ".." if (x, y) not in ws.source_art else f"{ws.source_art[(x, y)]:02x}"
                for x in range(ws.source_width)
            ]
            rows.append(f"y={y:02d} " + " ".join(tokens))
        ws.record("source_coordinates_inspected")
        return "\n".join(rows)

    def start_from_source(
        observed_description: str,
        discard_unsaved: bool = False,
    ) -> dict[str, Any]:
        """Start the playable candidate from the exact approved pixel grid.

        Args:
            observed_description: Honest description of what the source visibly depicts,
                written after looking at it rather than copied from a filename or prompt.
            discard_unsaved: Explicitly replace an existing unsaved draft.

        Returns:
            Exact source dimensions and proof that no pixel changed during handoff.
        """
        if ws.source_art is None or ws.source_width is None or ws.source_height is None:
            raise ValueError("this request did not include an approved pixelized source")
        if ws.design is not None and not ws.saved and not discard_unsaved:
            raise ValueError(
                "the current candidate is unsaved — pass discard_unsaved=True to restart"
            )
        ws.design = Design(
            target_level=ws.context.next_level,
            width=ws.source_width,
            height=ws.source_height,
            picture=dict(ws.source_art),
            difficulty=ws.context.recommended_difficulty,
        )
        ws.subject = observed_description.strip()
        ws.revision += 1
        ws.saved = False
        ws.last_result = None
        ws.queues = None
        ws.patch_calls = 0
        ws.patch_cells = 0
        ws.looked_at_revision = None
        ws.visual_review_revision = None
        ws.visual_review = None
        ws.composition_school = None
        ws.composition_anchors = ()
        ws.record(
            "source_candidate_started",
            width=ws.source_width,
            height=ws.source_height,
            cubes=len(ws.source_art),
            art_sha256=ws.source_art_sha256,
        )
        ws.snapshot()
        return {
            "subject": ws.subject,
            "approved_description": ws.source_description,
            "width": ws.source_width,
            "height": ws.source_height,
            "cubes": len(ws.source_art),
            "materials": len(set(ws.source_art.values())),
            "source_cells_changed": 0,
            "occupied_share": round(len(ws.source_art) / (ws.source_width * ws.source_height), 4),
            "next": "raise physical density when below the campaign floor, then place mechanics",
        }

    def apply_composition_school(
        school: str,
        palette: list[int],
        seed: int = 151,
    ) -> dict[str, Any]:
        """Arrange gameplay pixels around the frozen source using one corpus-derived school.

        The source must be freshly restored before every ablation. The operation never changes
        a frozen source pixel and returns source-safe anchor rectangles for later mechanics.

        Args:
            school: One of segmented-squeeze, scenic-field, layered-inset-frame or
                peripheral-compartments.
            palette: At least four screenshot-confirmed materials for the surrounding field.
            seed: Deterministic within-school variation.

        Returns:
            Added cells, occupancy, school and source-safe mechanic anchors.
        """
        design = ws.need()
        if ws.source_art is None:
            raise ValueError("composition schools require an approved frozen source")
        if design.picture != ws.source_art or design.board or design.top:
            raise ValueError("restart from the exact source before applying one composition school")
        if any(material not in render.CONFIRMED_HEX for material in palette):
            raise ValueError("every composition material needs screenshot-confirmed colour")
        try:
            result = composition.apply(design, school, palette, seed=seed)
        except BuildError as exc:
            raise ValueError(str(exc)) from exc
        ws.composition_school = result.school
        ws.composition_anchors = result.mechanic_anchors
        ws.revision += 1
        ws.saved = False
        ws.last_result = None
        ws.queues = None
        ws.looked_at_revision = None
        ws.visual_review_revision = None
        ws.visual_review = None
        anchors = [asdict(anchor) for anchor in result.mechanic_anchors]
        ws.record(
            "composition_school_applied",
            school=result.school,
            seed=seed,
            palette=palette,
            added_cells=result.added_cells,
            occupied_share=round(result.occupied_share, 4),
            source_cells_changed=result.source_cells_changed,
            mechanic_anchors=anchors,
        )
        ws.snapshot()
        return {
            "school": result.school,
            "added_cells": result.added_cells,
            "occupied_share": round(result.occupied_share, 4),
            "source_cells_changed": result.source_cells_changed,
            "mechanic_anchors": anchors,
            "next": "look, then checkpoint only if the whole composition remains readable",
        }

    def add_ornamental_frame(materials: list[int]) -> dict[str, Any]:
        """Fill one to three empty border rings without overwriting source pixels.

        Args:
            materials: Screenshot-confirmed colours from outermost ring inward. The number of
                materials is the frame depth.

        Returns:
            Added cells, frame depth and resulting physical occupancy.
        """
        design = ws.need()
        if not 1 <= len(materials) <= 3:
            raise ValueError("an ornamental frame needs one to three material rings")
        if any(material not in render.CONFIRMED_HEX for material in materials):
            raise ValueError("every frame material must have a screenshot-confirmed colour")
        before = len(design.picture)
        try:
            builder.add_art_frame(design, tuple(materials))
        except BuildError as exc:
            raise ValueError(str(exc)) from exc
        added = len(design.picture) - before
        ws.revision += 1
        ws.saved = False
        ws.last_result = None
        ws.queues = None
        ws.looked_at_revision = None
        ws.visual_review_revision = None
        ws.visual_review = None
        occupancy = len(design.occupied()) / (design.width * design.height)
        ws.record(
            "ornamental_frame_added",
            materials=materials,
            added_cells=added,
            occupied_share=round(occupancy, 4),
        )
        ws.snapshot()
        return {
            "added_cells": added,
            "depth": len(materials),
            "occupied_share": round(occupancy, 4),
            "next": "look now; keep the frame only if it improves the whole composition",
        }

    def fill_background(material: int) -> dict[str, Any]:
        """Fill every remaining empty cell with one ordinary background material.

        This creates real cubes and therefore new ammo demand and peel structure. Use a colour
        that keeps the source foreground readable. A frame may be added first to segment the
        background and integrate exposed keys later.

        Args:
            material: Screenshot-confirmed background material.

        Returns:
            Added cells, background material and physical occupancy.
        """
        design = ws.need()
        if material not in render.CONFIRMED_HEX:
            raise ValueError("background material must have a screenshot-confirmed colour")
        before = len(design.picture)
        try:
            builder.fill_empty_art(design, material)
        except BuildError as exc:
            raise ValueError(str(exc)) from exc
        added = len(design.picture) - before
        ws.revision += 1
        ws.saved = False
        ws.last_result = None
        ws.queues = None
        ws.looked_at_revision = None
        ws.visual_review_revision = None
        ws.visual_review = None
        occupancy = len(design.occupied()) / (design.width * design.height)
        ws.record(
            "background_filled",
            material=material,
            added_cells=added,
            occupied_share=round(occupancy, 4),
        )
        ws.snapshot()
        return {
            "added_cells": added,
            "material": material,
            "occupied_share": round(occupancy, 4),
            "next": "look now; reject a background that hides the source or forms ugly noise",
        }

    def place_pixel_pipe(
        x: int,
        y: int,
        materials: list[int],
        counts: list[int],
        replace_art: bool = True,
    ) -> dict[str, Any]:
        """Replace one deliberate 3x3 image region with a functioning Pixel Pipe.

        A pipe revives one matching destroyed pixel at a time. Its displayed counter is the
        aggregate queue count; when one colour segment is depleted it changes to the next.
        This tool edits the image on purpose. Its numbered 3x3 footprint is usually too large
        for a compact eye, mouth or facial identity cue. Prefer a wing medallion, flower centre,
        tail marking, structural panel or other coherent larger feature—not an overlay chosen
        only because space happens to be free.

        Args:
            x: Leftmost native grid coordinate of the 3x3 footprint.
            y: Bottom native grid coordinate of the 3x3 footprint.
            materials: Ordered material segments regenerated by the pipe.
            counts: Segment counts matching ``materials`` one-for-one. Every value must be
                10, 20 or 50, the complete set observed in shipped L1-150 Pixel Pipes.
            replace_art: Remove source pixels under the footprint. Set false only for a
                deliberately empty 3x3 region.

        Returns:
            Footprint, removed source cells, aggregate counter and exact queue.
        """
        design = ws.need()
        if len(design.board.get("pixelPipes", [])) >= 1:
            raise ValueError("this first loop admits exactly one Pixel Pipe")
        if not materials or len(materials) != len(counts):
            raise ValueError("materials and counts must be non-empty lists of equal length")
        if any(material not in render.CONFIRMED_HEX for material in materials):
            raise ValueError("every pipe material must have a screenshot-confirmed colour")
        invalid_counts = [
            count for count in counts if count not in builder.PIXEL_PIPE_SEGMENT_COUNTS
        ]
        if invalid_counts:
            raise ValueError(
                "Pixel Pipe segment counts must be shipped L1-150 values "
                f"{builder.PIXEL_PIPE_SEGMENT_COUNTS}, got {invalid_counts}"
            )
        footprint = {(px, py) for px in range(x, x + 3) for py in range(y, y + 3)}
        if any(not (0 <= px < design.width and 0 <= py < design.height) for px, py in footprint):
            raise ValueError(f"3x3 Pixel Pipe at {(x, y)} leaves the board")
        covered = footprint & set(design.picture)
        if covered and not replace_art:
            raise ValueError(
                f"Pixel Pipe footprint covers {len(covered)} art cells; use replace_art=True"
            )
        remaining_picture = {
            cell: material
            for cell, material in design.picture.items()
            if not replace_art or cell not in footprint
        }
        trigger_materials = set(remaining_picture.values())
        missing_triggers = sorted(set(materials) - trigger_materials)
        if missing_triggers:
            raise ValueError(
                "every Pixel Pipe segment needs a matching live pixel outside its footprint; "
                f"no trigger remains for materials {missing_triggers}"
            )
        if replace_art:
            builder.erase(design, footprint)
        entry = {
            "GridPoints": [{"X": px, "Y": py} for px, py in sorted(footprint)],
            "Queue": [
                {"Material": material, "Count": count}
                for material, count in zip(materials, counts, strict=True)
            ],
        }
        try:
            builder.add_board_mechanic(design, "pixelPipes", entry)
        except BuildError as exc:
            raise ValueError(str(exc)) from exc
        ws.revision += 1
        ws.saved = False
        ws.last_result = None
        ws.queues = None
        ws.looked_at_revision = None
        ws.visual_review_revision = None
        ws.visual_review = None
        ws.record(
            "pixel_pipe_placed",
            x=x,
            y=y,
            removed_art_cells=len(covered),
            aggregate_count=sum(counts),
            queue=entry["Queue"],
        )
        ws.snapshot()
        return {
            "footprint": sorted(footprint),
            "removed_art_cells": len(covered),
            "aggregate_count": sum(counts),
            "queue": entry["Queue"],
            "next": "call look and judge the complete image, not the pipe in isolation",
        }

    def place_egg_box(
        x: int,
        y: int,
        materials: list[int],
        count: int = 20,
        replace_art: bool = True,
    ) -> dict[str, Any]:
        """Place one compact four-egg tray as a meaningful 4x4 board feature.

        Args:
            x: Leftmost native grid coordinate of the tray.
            y: Bottom native grid coordinate of the tray.
            materials: Four visible egg colours in reading order from the lower-left.
            count: Matching shots per egg; use ten or twenty from the recent corpus.
            replace_art: Remove ordinary pixels under the tray before placement.

        Returns:
            Footprint, removed art count, egg colours and total egg-box count.
        """
        design = ws.need()
        if len(design.board.get("eggBoxes", [])) >= 2:
            raise ValueError("the L151 slice permits at most two compact egg boxes")
        if len(materials) != 4:
            raise ValueError("a compact 2x2 egg box needs exactly four materials")
        if any(material not in render.CONFIRMED_HEX for material in materials):
            raise ValueError("every egg material must have a screenshot-confirmed colour")
        footprint = {(px, py) for px in range(x, x + 4) for py in range(y, y + 4)}
        covered = footprint & set(design.picture)
        if covered and not replace_art:
            raise ValueError(
                f"egg-box footprint covers {len(covered)} art cells; use replace_art=True"
            )
        try:
            egg_materials = (materials[0], materials[1], materials[2], materials[3])
            builder.place_egg_box(
                design,
                (x, y),
                egg_materials,
                count=count,
                replace_art=replace_art,
            )
        except BuildError as exc:
            raise ValueError(str(exc)) from exc
        ws.revision += 1
        ws.saved = False
        ws.last_result = None
        ws.queues = None
        ws.looked_at_revision = None
        ws.visual_review_revision = None
        ws.visual_review = None
        ws.record(
            "egg_box_placed",
            x=x,
            y=y,
            removed_art_cells=len(covered),
            materials=materials,
            count=count,
            total_egg_boxes=len(design.board["eggBoxes"]),
        )
        ws.snapshot()
        return {
            "footprint": sorted(footprint),
            "removed_art_cells": len(covered),
            "materials": materials,
            "count": count,
            "total_egg_boxes": len(design.board["eggBoxes"]),
            "next": "call look and confirm the tray belongs to the scene before configuring play",
        }

    def place_key(x: int, y: int, replace_art: bool = True) -> dict[str, Any]:
        """Turn one repeated 2x2 visual feature into a functional key ornament.

        A key becomes available when shooter fire gains a clear path to it. It immediately opens the
        leftmost front queue lock, or remains a physical obstacle until such a lock appears. Use
        keys as eyespots, buttons, jewels or other repeated details, not arbitrary holes.

        Args:
            x: Leftmost native grid coordinate of the 2x2 key.
            y: Bottom native grid coordinate of the 2x2 key.
            replace_art: Explicitly replace source pixels under the key.

        Returns:
            Footprint, removed art count and the total key count.
        """
        design = ws.need()
        if len(design.board.get("keys", [])) >= 6:
            raise ValueError("the admitted L151 recipe permits at most six keys")
        footprint = {(px, py) for px in range(x, x + 2) for py in range(y, y + 2)}
        covered = footprint & set(design.picture)
        if covered and not replace_art:
            raise ValueError(f"key footprint covers {len(covered)} art cells; use replace_art=True")
        try:
            builder.place_key(design, (x, y), replace_art=replace_art)
        except BuildError as exc:
            raise ValueError(str(exc)) from exc
        ws.revision += 1
        ws.saved = False
        ws.last_result = None
        ws.queues = None
        ws.looked_at_revision = None
        ws.visual_review_revision = None
        ws.visual_review = None
        ws.record(
            "key_placed",
            x=x,
            y=y,
            removed_art_cells=len(covered),
            total_keys=len(design.board["keys"]),
        )
        ws.snapshot()
        return {
            "footprint": sorted(footprint),
            "removed_art_cells": len(covered),
            "total_keys": len(design.board["keys"]),
            "next": "place matching queue locks through configure_gameplay",
        }

    def configure_gameplay(
        lock_count: int,
        connected_groups: int,
        surprise_count: int = 0,
    ) -> dict[str, Any]:
        """Build four real lanes and measure one rich queue treatment immediately.

        This is the fast gameplay loop for source-backed levels. Locks are inserted blocker
        tokens, never converted funded shooters. Connected pairs are chosen only at matching depths
        in separate lanes, so every group can physically reach all lane fronts together.

        Args:
            lock_count: Queue blockers, exactly one per placed board key.
            connected_groups: Aligned two-shooter groups, from 1 to 18.
            surprise_count: Hidden queue shooters, from 0 to 30.

        Returns:
            Replay proof, mechanic activity trace, campaign pressure and the production gate.
        """
        design = ws.need()
        key_count = len(design.board.get("keys", []))
        if lock_count != key_count:
            raise ValueError(
                f"every key needs one queue lock: {key_count} keys, {lock_count} locks"
            )
        if not 1 <= connected_groups <= 18:
            raise ValueError(f"connected_groups must be 1..18, got {connected_groups}")
        if not 0 <= surprise_count <= 30:
            raise ValueError(f"surprise_count must be 0..30, got {surprise_count}")

        for container in ("Locks", "ConnectedShooters", "SurpriseShooters"):
            design.top.pop(container, None)
        ws.queues = builder.blocker_first_queues(design, queues=4)
        try:
            treatment = builder.apply_queue_treatment(
                design,
                ws.queues,
                lock_count=lock_count,
                connected_groups=connected_groups,
                surprise_count=surprise_count,
            )
        except BuildError as exc:
            raise ValueError(str(exc)) from exc

        started = time.perf_counter()
        ws.last_result = _serialized_certify_fast(ws.raw(), ws.context)
        trace = (
            certification.mechanic_trace(ws.last_result.raw, ws.last_result.certificate)
            if ws.last_result.certificate is not None
            else None
        )
        activity_violations = (
            list(certification.mechanic_activity_violations(ws.last_result.raw, trace))
            if trace is not None
            else ["candidate has no replayed winning trace"]
        )
        campaign_fit = ws.last_result.campaign
        meets_gate = bool(
            campaign_fit is not None
            and _meets_save_gate(ws, campaign_fit)
            and not activity_violations
        )
        ws.saved = False
        ws.record(
            "gameplay_configured",
            duration_seconds=round(time.perf_counter() - started, 3),
            treatment=treatment,
            result=ws.last_result.summary(),
            mechanic_trace=trace,
            activity_violations=activity_violations,
            meets_save_gate=meets_gate,
        )
        return {
            **ws.last_result.summary(),
            **treatment,
            "lane_sizes": [len(lane) for lane in ws.queues],
            "mechanic_trace": trace,
            "activity_violations": activity_violations,
            "meets_save_gate": meets_gate,
        }

    def compare_to_source() -> dict[str, Any]:
        """Measure literal source changes without pretending to measure recognizability."""
        if ws.source_art is None:
            raise ValueError("this request did not include an approved pixelized source")
        design = ws.need()
        shared = set(ws.source_art) & set(design.picture)
        removed = set(ws.source_art) - set(design.picture)
        added = set(design.picture) - set(ws.source_art)
        recoloured = {cell for cell in shared if ws.source_art[cell] != design.picture[cell]}
        mechanic_cells = sum(
            len(grid_cells(entry.get("GridPoints") or entry.get("MainGridPoints") or []))
            for entries in design.board.values()
            for entry in entries
        )
        comparison = {
            "source_cells": len(ws.source_art),
            "unchanged_cells": len(shared - recoloured),
            "removed_cells": len(removed),
            "added_cells": len(added),
            "recoloured_cells": len(recoloured),
            "mechanic_cells": mechanic_cells,
            "literal_change_share": round(
                (len(removed) + len(added) + len(recoloured)) / len(ws.source_art), 4
            ),
            "warning": "pixel difference is diagnostic; only visual review judges meaning",
        }
        ws.record("source_compared", **comparison)
        return comparison

    def review_visual_retention(
        observed_description: str,
        preserved_features: list[str],
        mechanic_role: str,
    ) -> dict[str, Any]:
        """Record the final visual judgment after viewing the playable board.

        Args:
            observed_description: What the current render visibly depicts, without relying
                on the source filename or intended label.
            preserved_features: At least two concrete silhouette or identity cues still visible.
            mechanic_role: How the mechanic participates in the composition rather than merely
                avoiding it.

        Returns:
            The recorded visual review and its candidate revision.
        """
        if ws.source_art is None:
            raise ValueError("visual retention review is for source-backed candidates")
        if ws.looked_at_revision != ws.revision:
            raise ValueError("call look on the current playable board before reviewing it")
        clean_features = [feature.strip() for feature in preserved_features if feature.strip()]
        if len(clean_features) < 2:
            raise ValueError("name at least two concrete preserved visual features")
        if not observed_description.strip() or not mechanic_role.strip():
            raise ValueError("observed description and mechanic role must be explicit")
        ws.visual_review = {
            "observed_description": observed_description.strip(),
            "preserved_features": clean_features,
            "mechanic_role": mechanic_role.strip(),
        }
        ws.visual_review_revision = ws.revision
        ws.record("visual_retention_reviewed", **ws.visual_review)
        return {"revision": ws.revision, **ws.visual_review}

    def checkpoint_variant(
        name: str,
        concept: str,
        gameplay_hypothesis: str,
        decision: str,
    ) -> dict[str, Any]:
        """Preserve one visually inspected branch without promoting it as the final level.

        Args:
            name: Stable lowercase slug for this branch.
            concept: The level's concrete visual and experiential idea.
            gameplay_hypothesis: What the current or next mechanic edit is expected to change.
            decision: Why this branch is being kept, rejected or used as the next working branch.

        Returns:
            Immutable branch metadata and artifact paths for later restoration or review.
        """
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name):
            raise ValueError("variant name must be a lowercase slug of at most 40 characters")
        if name in ws.variants:
            raise ValueError(f"variant {name!r} already exists; checkpoint names are immutable")
        if ws.looked_at_revision != ws.revision:
            raise ValueError("call look on the current branch before checkpointing it")
        if not concept.strip() or not gameplay_hypothesis.strip() or not decision.strip():
            raise ValueError("concept, gameplay_hypothesis and decision must be explicit")

        raw = ws.raw()
        metadata: dict[str, Any] = {
            "schema": "conveyor-variant-checkpoint-v1",
            "name": name,
            "parent_revision": ws.revision,
            "subject": ws.subject,
            "concept": concept.strip(),
            "gameplay_hypothesis": gameplay_hypothesis.strip(),
            "decision": decision.strip(),
            "certification": ws.last_result.summary() if ws.last_result is not None else None,
        }
        ws.variants[name] = {
            "design": deepcopy(ws.need()),
            "queues": deepcopy(ws.queues),
            "subject": ws.subject,
            "visual_review": deepcopy(ws.visual_review),
            "composition_school": ws.composition_school,
            "composition_anchors": deepcopy(ws.composition_anchors),
            "metadata": metadata,
        }
        directory = ws.out_dir / "variants" / name
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "level.json").write_text(json.dumps(raw, separators=(",", ":")) + "\n")
        (directory / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        (directory / "review.html").write_text(
            "<!doctype html><meta charset=utf-8><title>conveyor shooter variant</title>"
            "<style>body{margin:0;background:#111117;color:#eee;font:16px system-ui;padding:24px}"
            "main{max-width:760px;margin:auto}svg{width:100%;height:auto;background:#0b0b10}"
            "p{color:#bbb}</style><main>"
            f"<h1>{html.escape(name)}</h1>{render.svg_board(raw, 16)}"
            f"<p><strong>Concept:</strong> {html.escape(metadata['concept'])}</p>"
            "<p><strong>Gameplay hypothesis:</strong> "
            f"{html.escape(metadata['gameplay_hypothesis'])}</p>"
            f"<p><strong>Decision:</strong> {html.escape(metadata['decision'])}</p></main>\n"
        )
        ws.record("variant_checkpointed", checkpoint=metadata)
        return {
            "name": name,
            "revision": ws.revision,
            "artifacts": ["level.json", "metadata.json", "review.html"],
            "variants_kept": sorted(ws.variants),
        }

    def restore_variant(name: str) -> dict[str, Any]:
        """Restore an immutable checkpoint as a new working revision.

        Args:
            name: Existing checkpoint slug returned by ``checkpoint_variant``.

        Returns:
            The restored concept, new revision and available checkpoint names.
        """
        if name not in ws.variants:
            raise ValueError(f"unknown variant {name!r}; available: {sorted(ws.variants)}")
        variant = ws.variants[name]
        ws.design = deepcopy(variant["design"])
        ws.queues = deepcopy(variant["queues"])
        ws.subject = str(variant["subject"])
        ws.visual_review = deepcopy(variant["visual_review"])
        ws.composition_school = variant["composition_school"]
        ws.composition_anchors = deepcopy(variant["composition_anchors"])
        ws.revision += 1
        ws.saved = False
        ws.last_result = None
        ws.looked_at_revision = None
        ws.visual_review_revision = None
        ws.record("variant_restored", name=name)
        ws.snapshot()
        return {
            "name": name,
            "revision": ws.revision,
            "concept": variant["metadata"]["concept"],
            "variants_kept": sorted(ws.variants),
            "next": "look again, then make one local gameplay or paint change",
        }

    def list_variants() -> dict[str, Any]:
        """List preserved visual seeds and gameplay iterations in this run."""
        return {
            name: {
                "concept": variant["metadata"]["concept"],
                "gameplay_hypothesis": variant["metadata"]["gameplay_hypothesis"],
                "decision": variant["metadata"]["decision"],
            }
            for name, variant in sorted(ws.variants.items())
        }

    def inspect_art() -> dict[str, Any]:
        """Measure visual richness before spending time on gameplay search.

        This is a fast opening-board check. It reports the candidate's exact values beside
        the observed L131-150 bands and gives deterministic advice for every miss. It does
        not solve the level and it does not decide whether the subject is recognizable.

        Returns:
            Six structured-richness comparisons, readiness for gameplay configuration and
            actionable advice. Treat them as diagnostics before configure_gameplay.
        """
        result = _art_feedback(ws.raw(), ws.context)
        ws.record("art_inspected", **result)
        return result

    def paint_cells(material: int, cells: list[list[int]], erase: bool = False) -> dict[str, Any]:
        """Make a small native-grid correction after looking at the rendered board.

        Args:
            material: Screenshot-confirmed material id to paint. Ignored when erasing.
            cells: Explicit ``[x, y]`` native grid cells. Keep this to a small feature or
                cleanup patch; reject the source conversion when the silhouette itself is wrong.
            erase: Remove the listed cells instead of recolouring them.

        Returns:
            Updated cube and material counts.
        """
        design = ws.need()
        patch = {(int(cell[0]), int(cell[1])) for cell in cells}
        if ws.patch_calls >= 2 or ws.patch_cells + len(patch) > 16:
            raise ValueError(
                "paint_cells is limited to two calls and 16 cells per authored revision; "
                "reject the source conversion instead of metric-patching the board"
            )
        if erase:
            builder.erase(design, patch)
        else:
            try:
                builder.paint(design, patch, material)
            except BuildError as exc:
                raise ValueError(str(exc)) from exc
        ws.revision += 1
        ws.saved = False
        ws.last_result = None
        ws.queues = None
        ws.patch_calls += 1
        ws.patch_cells += len(patch)
        ws.looked_at_revision = None
        ws.visual_review_revision = None
        ws.visual_review = None
        ws.record(
            "artwork_patched",
            material=material,
            cells=len(patch),
            erase=erase,
        )
        ws.snapshot()
        return {"cubes": len(design.picture), "materials": len(design.colours())}

    def look() -> str:
        """See the exact candidate render as an image.

        Returns:
            A PNG data URI. State what the image depicts and identify any weak feature before
            certifying; do not infer the image from the subject label you supplied.
        """
        ws.record("artwork_viewed")
        ws.looked_at_revision = ws.revision
        return render.board_image_uri(ws.raw(), scale=6)

    def certify_candidate() -> dict[str, Any]:
        """Run the new engine, replay its proof, and measure L151 context.

        Returns:
            Strict validity/solvability findings plus separate static and temporal campaign
            comparisons. An outside contextual band is visible but not mislabeled as a
            universal failure; lack of a replayed win is a hard failure.
        """
        started = time.perf_counter()
        ws.record("certification_started")
        if ws.source_art is not None and ws.queues is None:
            ws.queues = builder.blocker_first_queues(ws.need())
            ws.record("blocker_first_queues_built", lanes=len(ws.queues))
        ws.last_result = _serialized_certify(ws.raw(), ws.context)
        ws.record(
            "certification_completed",
            duration_seconds=round(time.perf_counter() - started, 3),
            result=ws.last_result.summary(),
        )
        return ws.last_result.summary()

    def save_candidate() -> dict[str, Any]:
        """Re-certify and save the native level, proof, metrics and visual report.

        Returns:
            Artifact names and the final certification summary.

        Raises:
            ValueError: If the authoritative engine cannot replay a winning certificate.
        """
        if ws.saved:
            raise ValueError("this candidate was already saved")
        if ws.looked_at_revision != ws.revision:
            raise ValueError("refusing to save: call look after the final artwork edit")
        if ws.source_art is not None and ws.visual_review_revision != ws.revision:
            raise ValueError(
                "refusing to save: review visual retention after the final source-backed edit"
            )
        current = ws.raw()
        if ws.last_result is not None and ws.last_result.raw == current:
            result = ws.last_result
            ws.record("save_reused_current_certificate")
        else:
            result = _serialized_certify(current, ws.context)
        if not result.solved:
            raise ValueError("refusing to save: " + "; ".join(result.violations))
        mechanic_trace = (
            certification.mechanic_trace(result.raw, result.certificate)
            if result.certificate is not None
            else None
        )
        activity_violations = (
            certification.mechanic_activity_violations(result.raw, mechanic_trace)
            if mechanic_trace is not None
            else ("candidate has no replayed winning trace",)
        )
        if activity_violations:
            raise ValueError("refusing to save inert mechanics: " + "; ".join(activity_violations))
        assert result.campaign is not None
        if not _meets_save_gate(ws, result.campaign):
            if ws.source_art is not None:
                raise ValueError(
                    "refusing to save: source-backed campaign fit requires relaunch and tray "
                    "pressure inside their bands, available actions at or above its corpus "
                    "floor, and at least 2/4 batch-reset temporal axes; got "
                    f"{result.campaign.temporal_inside}/{result.campaign.temporal_total} temporal"
                )
            raise ValueError(
                "refusing to save: campaign fit must reach at least 10/13 static, 2/4 "
                "batch-reset temporal, and 5/6 structured visual richness; got "
                f"{result.campaign.static_inside}/{result.campaign.static_total}, "
                f"{result.campaign.temporal_inside}/{result.campaign.temporal_total}, and "
                f"{result.campaign.richness_inside}/{result.campaign.richness_total}"
            )
        stem = f"level_{ws.context.next_level}_1"
        artifacts = certification.save_certified(result, ws.out_dir, stem=stem)
        mechanic_trace_name = f"{stem}.mechanic-trace.json"
        (ws.out_dir / mechanic_trace_name).write_text(json.dumps(mechanic_trace, indent=2) + "\n")
        artifacts[mechanic_trace_name] = ws.out_dir / mechanic_trace_name
        if ws.source_art is not None:
            comparison = compare_to_source()
            comparison_name = "source-comparison.html"
            (ws.out_dir / comparison_name).write_text(
                _source_comparison_html(ws, comparison) + "\n"
            )
            artifacts[comparison_name] = ws.out_dir / comparison_name
        ws.last_result = result
        ws.saved = True
        ws.record("candidate_saved", artifacts=sorted(artifacts))
        return {
            "artifacts": sorted(artifacts),
            "subject": ws.subject,
            **result.summary(),
        }

    tools = [
        campaign_context,
        look_source_image,
        extract_image_variants,
        look_image_variant,
        compare_image_variants,
        freeze_image_baseline,
        look_source_art,
        inspect_source_coordinates,
        start_from_source,
        apply_composition_school,
        add_ornamental_frame,
        fill_background,
        paint_cells,
        inspect_art,
        place_key,
        place_pixel_pipe,
        place_egg_box,
        compare_to_source,
        checkpoint_variant,
        restore_variant,
        list_variants,
        configure_gameplay,
        look,
        review_visual_retention,
        certify_candidate,
        save_candidate,
    ]
    return tools


__all__ = ["Workspace", "make_tools"]
