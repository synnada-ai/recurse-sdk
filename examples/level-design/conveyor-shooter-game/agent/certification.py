# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Authoritative certification and campaign measurement for generated levels."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import audit_dimensions
import render
from conveyor_game import (
    ActionKind,
    Engine,
    GameRules,
    Level,
    Outcome,
    SolveResult,
    SolveStatus,
    replay,
    solve,
    solve_experimental_portfolio,
)
from conveyor_game.model import mechanics_used


@dataclass(frozen=True)
class Band:
    """One observed low/median/high campaign window."""

    low: float
    median: float
    high: float

    def contains(self, value: float) -> bool:
        """Return whether ``value`` lies inside the observed window."""
        return self.low <= value <= self.high


@dataclass(frozen=True)
class CampaignContext:
    """The compact evidence needed to design the next campaign level."""

    next_level: int
    target_window: tuple[int, int]
    next_batch_position: int
    recommended_difficulty: str
    same_position_difficulty_counts: dict[str, int]
    supported_design_mechanics: tuple[str, ...]
    supported_mechanics_note: str
    mechanic_first_seen: dict[str, int]
    target_band: dict[str, Band]
    previous_level: dict[str, Any]
    temporal_band: dict[str, Band]
    temporal_reference: str
    recent_levels: tuple[dict[str, Any], ...]

    def to_tool_result(self) -> dict[str, Any]:
        """Return compact JSON-compatible grounding for the design agent."""
        return {
            "next_level": self.next_level,
            "target_window": list(self.target_window),
            "next_batch_position": self.next_batch_position,
            "recommended_difficulty": self.recommended_difficulty,
            "same_position_difficulty_counts": self.same_position_difficulty_counts,
            "supported_design_mechanics": list(self.supported_design_mechanics),
            "supported_mechanics_note": self.supported_mechanics_note,
            "target_band": _bands_to_dict(self.target_band),
            "previous_level": self.previous_level,
            "temporal_band": _bands_to_dict(self.temporal_band),
            "temporal_reference": self.temporal_reference,
            "recent_levels": list(self.recent_levels),
            "note": (
                "These are contextual observations from certified shipped levels, not a "
                "single quality score. Static and temporal fit are reported separately."
            ),
        }


def load_context(path: Path) -> CampaignContext:
    """Load and validate the frozen L1-150 campaign context."""
    raw = json.loads(path.read_text())
    if raw.get("schema") != "conveyor-campaign-context-v1":
        raise ValueError(f"unsupported conveyor shooter campaign context in {path}")
    window = raw["target_window"]
    if len(window) != 2:
        raise ValueError(f"target_window must have two entries, got {window!r}")
    return CampaignContext(
        next_level=int(raw["next_level"]),
        target_window=(int(window[0]), int(window[1])),
        next_batch_position=int(raw["next_batch_position"]),
        recommended_difficulty=str(raw["recommended_difficulty"]),
        same_position_difficulty_counts={
            str(name): int(count) for name, count in raw["same_position_difficulty_counts"].items()
        },
        supported_design_mechanics=tuple(raw["supported_design_mechanics"]),
        supported_mechanics_note=str(raw["supported_mechanics_note"]),
        mechanic_first_seen={
            str(name): int(level) for name, level in raw["mechanic_first_seen"].items()
        },
        target_band=_parse_bands(raw["target_band"]),
        previous_level=dict(raw["previous_level"]),
        temporal_band=_parse_bands(raw["temporal_band"]),
        temporal_reference=str(raw["temporal_reference"]),
        recent_levels=tuple(dict(level) for level in raw["recent_levels"]),
    )


def _parse_bands(raw: dict[str, dict[str, float]]) -> dict[str, Band]:
    """Decode named percentile windows."""
    return {
        name: Band(
            low=float(values["low"]),
            median=float(values["median"]),
            high=float(values["high"]),
        )
        for name, values in raw.items()
    }


def _bands_to_dict(bands: dict[str, Band]) -> dict[str, dict[str, float]]:
    """Encode named percentile windows for a tool result."""
    return {
        name: {"low": band.low, "median": band.median, "high": band.high}
        for name, band in bands.items()
    }


RULES = GameRules(connected_gap=1)
RICHNESS_DIMENSIONS = frozenset(
    {
        "regions_per_100_cells",
        "local_colour_diversity_3x3",
        "structured_detail_share",
        "colour_boundary_share",
        "palette_entropy",
        "dominant_colour_share",
    }
)
PRESSURE_DIMENSIONS = frozenset(
    {
        "relaunch_share",
        "mean_tray",
        "mean_available_actions",
    }
)


@dataclass(frozen=True)
class BandValue:
    """One candidate value compared with one observed campaign window."""

    value: float
    low: float
    median: float
    high: float
    inside: bool

    @classmethod
    def compare(cls, value: float, band: Band) -> BandValue:
        """Create one explicit comparison."""
        return cls(
            value=value,
            low=band.low,
            median=band.median,
            high=band.high,
            inside=band.contains(value),
        )


@dataclass(frozen=True)
class CampaignMeasurement:
    """Static and temporal context, deliberately kept as separate axes."""

    static: dict[str, BandValue]
    temporal: dict[str, BandValue]

    @property
    def static_inside(self) -> int:
        """Count static dimensions inside their observed windows."""
        return sum(value.inside for value in self.static.values())

    @property
    def static_total(self) -> int:
        """Count measured static dimensions."""
        return len(self.static)

    @property
    def temporal_inside(self) -> int:
        """Count level-to-level changes inside their observed windows."""
        return sum(value.inside for value in self.temporal.values())

    @property
    def temporal_total(self) -> int:
        """Count measured temporal dimensions."""
        return len(self.temporal)

    @property
    def richness_inside(self) -> int:
        """Count structured-resolution dimensions inside the observed campaign bands."""
        return sum(self.static[name].inside for name in RICHNESS_DIMENSIONS)

    @property
    def richness_total(self) -> int:
        """Return the number of hard visual-richness dimensions."""
        return len(RICHNESS_DIMENSIONS)

    def to_dict(self) -> dict[str, Any]:
        """Return artifact-friendly measurements."""
        return {
            "static": {name: _band_value_dict(value) for name, value in self.static.items()},
            "temporal": {name: _band_value_dict(value) for name, value in self.temporal.items()},
            "static_fit": f"{self.static_inside}/{self.static_total}",
            "temporal_fit": f"{self.temporal_inside}/{self.temporal_total}",
            "richness_fit": f"{self.richness_inside}/{self.richness_total}",
        }


def campaign_gate(measurement: CampaignMeasurement) -> bool:
    """Return whether a batch-opening candidate clears contextual and richness floors.

    Temporal bands are calibrated on the 14 shipped transitions into batch position one. A
    batch reset is intentionally more discontinuous than an ordinary adjacent-level change,
    so two of four temporal axes are required while static gameplay and art retain stricter
    floors.
    """
    return bool(
        measurement.static_inside >= 10
        and measurement.temporal_inside >= 2
        and measurement.richness_inside >= 5
    )


def source_campaign_gate(measurement: CampaignMeasurement) -> bool:
    """Gate source-backed levels on physical density and play pressure.

    The source image is a semantic seed, not permission to leave a sparse board. Physical opening
    fill and occupied-box density must reach the shipped lower bounds; values above the 90th
    percentile remain allowed because full-field boards are present in the corpus. Relaunch and
    tray pressure must remain in band. Available actions is asymmetric for this easy batch opener:
    falling below the observed floor is dangerous, while extra safe choices are not a failure.
    """
    fill = measurement.static["complete_fill"]
    box_fill = measurement.static["bbox_fill"]
    relaunch = measurement.static["relaunch_share"]
    tray = measurement.static["mean_tray"]
    choices = measurement.static["mean_available_actions"]
    return bool(
        measurement.temporal_inside >= 2
        and fill.value >= fill.low
        and box_fill.value >= box_fill.low
        and relaunch.inside
        and tray.inside
        and choices.value >= choices.low
    )


@dataclass(frozen=True)
class CertificationResult:
    """Everything established about one candidate, including replay evidence."""

    raw: dict[str, Any]
    solved: bool
    violations: tuple[str, ...]
    strategy: str | None = None
    explored_states: int = 0
    certificate: dict[str, Any] | None = None
    campaign: CampaignMeasurement | None = None

    def summary(self) -> dict[str, Any]:
        """Return the model-facing certification result."""
        result: dict[str, Any] = {
            "solved": self.solved,
            "violations": list(self.violations),
            "strategy": self.strategy,
            "explored_states": self.explored_states,
        }
        if self.certificate is not None:
            result["certificate_actions"] = len(self.certificate["actions"])
        if self.campaign is not None:
            result["campaign"] = self.campaign.to_dict()
        return result


def certify(raw: dict[str, Any], context: CampaignContext) -> CertificationResult:
    """Parse, validate, solve, replay, then measure one candidate.

    A solved label is emitted only after the persisted primitive actions replay to ``won``
    through the same unrestricted engine. Corpus windows never substitute for that proof.
    """
    return _certify_with_solver(
        raw,
        context,
        _solve_authoritative,
    )


def _solve_authoritative(level: Level) -> SolveResult:
    """Take a cheap exact proof before escalating to the complementary portfolio."""
    probe = solve(level, RULES, max_states=5_000)
    if probe.status is not SolveStatus.INCOMPLETE:
        return probe
    return solve_experimental_portfolio(level, RULES)


def certify_fast(
    raw: dict[str, Any],
    context: CampaignContext,
    *,
    max_states: int = 5_000,
) -> CertificationResult:
    """Run a bounded exact preflight suitable for iterative queue experiments.

    An incomplete result is not evidence that a level is impossible. It tells the designer to
    prefer a queue hypothesis that the exact search can prove cheaply. Final certification and
    every saved artifact still use :func:`certify` and its complete strategy portfolio.
    """
    if max_states < 1:
        raise ValueError("max_states must be positive")
    return _certify_with_solver(
        raw,
        context,
        lambda level: solve(level, RULES, max_states=max_states),
        incomplete_message=f"fast preflight exhausted {max_states} exact states",
    )


def _certify_with_solver(
    raw: dict[str, Any],
    context: CampaignContext,
    solve_level: Callable[[Level], Any],
    *,
    incomplete_message: str | None = None,
) -> CertificationResult:
    """Apply shared parsing, validation, replay and measurement around one solver policy."""
    try:
        level = Level.from_dict(raw, number=context.next_level)
    except (KeyError, TypeError, ValueError) as exc:
        return CertificationResult(raw=raw, solved=False, violations=(f"schema: {exc}",))

    violations = _mechanical_violations(raw, level, context)
    if violations:
        return CertificationResult(raw=raw, solved=False, violations=tuple(violations))

    solved = solve_level(level)
    if solved.status is not SolveStatus.SOLVED:
        detail = (
            ", ".join(solved.unsupported)
            if solved.unsupported
            else (
                incomplete_message
                if solved.status is SolveStatus.INCOMPLETE and incomplete_message is not None
                else solved.status.value
            )
        )
        return CertificationResult(
            raw=raw,
            solved=False,
            violations=(f"authoritative solver did not certify the level: {detail}",),
            strategy=solved.strategy,
            explored_states=solved.explored_states,
        )

    final_state = replay(level, RULES, solved.actions)
    outcome = final_state.outcome(level)
    if outcome is not Outcome.WON:
        return CertificationResult(
            raw=raw,
            solved=False,
            violations=(f"certificate replay ended {outcome.value}",),
            strategy=solved.strategy,
            explored_states=solved.explored_states,
        )

    certificate = {
        "schema": "conveyor-primitive-certificate-v1",
        "level": context.next_level,
        "rules": {"connected_gap": RULES.connected_gap},
        "strategy": solved.strategy,
        "explored_states": solved.explored_states,
        "replay": outcome.value,
        "actions": [
            {"kind": action.kind.value, "index": action.index, "shooter_id": action.shooter_id}
            for action in solved.actions
        ],
    }
    campaign = measure_campaign(raw, certificate, context)
    return CertificationResult(
        raw=raw,
        solved=True,
        violations=(),
        strategy=solved.strategy,
        explored_states=solved.explored_states,
        certificate=certificate,
        campaign=campaign,
    )


def mechanic_trace(raw: dict[str, Any], certificate: dict[str, Any]) -> dict[str, Any]:
    """Replay a certificate and prove that every designed mechanic participates.

    Solvability alone can accept ornamental mechanics that never affect the winning path. This
    trace records actual key releases, lock openings, pipe regeneration, connected launches and
    hidden-shooter launches so production review can distinguish an active design from decoration.
    """
    level = Level.from_dict(raw, number=int(certificate["level"]))
    engine = Engine(level, RULES)
    state = engine.initial_state()
    connected_ids = {shooter_id for group in level.connections for shooter_id in group}
    hidden_ids = {shooter.id for lane in level.lanes for shooter in lane if shooter.hidden}
    connected_launches = 0
    hidden_launches = 0
    key_release_actions: list[int] = []
    lock_open_actions: list[int] = []
    keys_released = 0
    locks_opened = 0
    pipe_revives = 0

    for action_index, record in enumerate(certificate["actions"], start=1):
        kind = ActionKind(record["kind"])
        before = state
        if kind is ActionKind.ADVANCE:
            state, _event = engine.advance(state)
        elif kind is ActionKind.LAUNCH_LANE:
            lane_index = int(record["index"])
            lane_shooter = level.lanes[lane_index][state.lane_heads[lane_index]]
            connected_launches += int(lane_shooter.id in connected_ids)
            hidden_launches += int(lane_shooter.id in hidden_ids)
            state = engine.launch_lane(state, lane_index)
        else:
            slot_index = int(record["index"])
            tray_shooter = state.tray[slot_index]
            connected_launches += int(tray_shooter.id in connected_ids)
            hidden_launches += int(tray_shooter.id in hidden_ids)
            state = engine.launch_tray(state, slot_index)

        released = len(state.waiting_keys | state.freed_keys) - len(
            before.waiting_keys | before.freed_keys
        )
        opened = len(before.locked) - len(state.locked)
        if released:
            keys_released += released
            key_release_actions.append(action_index)
        if opened:
            locks_opened += opened
            lock_open_actions.append(action_index)
        pipe_revives += sum(
            after - prior
            for after, prior in zip(state.pipe_progress, before.pipe_progress, strict=True)
        )

    if state.outcome(level) is not Outcome.WON:
        raise ValueError(f"mechanic trace ended {state.outcome(level).value}")
    return {
        "keys_released": keys_released,
        "key_release_actions": key_release_actions,
        "locks_opened": locks_opened,
        "lock_open_actions": lock_open_actions,
        "pipe_revives": pipe_revives,
        "connected_launches": connected_launches,
        "hidden_launches": hidden_launches,
        "mechanics_used": mechanics_used(raw),
    }


def mechanic_activity_violations(raw: dict[str, Any], trace: dict[str, Any]) -> tuple[str, ...]:
    """Return designed mechanics that were inert in the certified winning trace."""
    used = mechanics_used(raw)
    violations: list[str] = []
    if "keys" in used and int(trace["keys_released"]) != int(used["keys"]):
        violations.append(
            f"only {trace['keys_released']}/{used['keys']} keys participate in the win"
        )
    if "Locks" in used and int(trace["locks_opened"]) != int(used["Locks"]):
        violations.append(f"only {trace['locks_opened']}/{used['Locks']} locks open in the win")
    pipe_demand = sum(
        int(segment["Count"])
        for pipe in raw["PixelImageData"].get("pixelPipes", {}).get("Pipes", [])
        for segment in pipe.get("Queue", [])
    )
    if "pixelPipes" in used and int(trace["pipe_revives"]) != pipe_demand:
        violations.append(
            f"Pixel Pipes perform {trace['pipe_revives']}/{pipe_demand} required revivals"
        )
    if "ConnectedShooters" in used and not int(trace["connected_launches"]):
        violations.append("connected shooters never launch in the winning trace")
    if "SurpriseShooters" in used and not int(trace["hidden_launches"]):
        violations.append("surprise shooters never launch in the winning trace")
    return tuple(violations)


def measure_campaign(
    raw: dict[str, Any], certificate: dict[str, Any], context: CampaignContext
) -> CampaignMeasurement:
    """Measure static L151 fit and change from L150 on separate axes."""
    artwork = audit_dimensions.artwork_metrics(raw)
    play = audit_dimensions.replay_profile(raw, certificate).metrics
    picture = audit_dimensions.physical_opening_cells(raw)
    width = int(raw["PixelImageData"]["width"])
    height = int(raw["PixelImageData"]["height"])
    xs = [cell[0] for cell in picture]
    ys = [cell[1] for cell in picture]
    perimeter = sum(
        neighbour not in picture
        for x, y in picture
        for neighbour in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1))
    )
    values = {
        "complete_fill": len(picture) / (width * height),
        "bbox_fill": len(picture) / ((max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1)),
        "perimeter_per_cell": perimeter / len(picture),
        "regions_per_100_cells": artwork["regions_per_100_cells"],
        "colours": artwork["colours"],
        "local_colour_diversity_3x3": artwork["local_colour_diversity_3x3"],
        "structured_detail_share": artwork["structured_detail_share"],
        "colour_boundary_share": artwork["colour_boundary_share"],
        "palette_entropy": artwork["palette_entropy"],
        "dominant_colour_share": artwork["dominant_colour_share"],
        "relaunch_share": play["relaunch_share"],
        "mean_tray": play["mean_tray"],
        "mean_available_actions": play["mean_available_actions"],
    }
    previous = context.previous_level
    deltas = {
        "delta_complete_fill": values["complete_fill"] - float(previous["complete_fill"]),
        "delta_regions_per_100_cells": values["regions_per_100_cells"]
        - float(previous["regions_per_100_cells"]),
        "delta_relaunch_share": values["relaunch_share"] - float(previous["relaunch_share"]),
        "delta_mean_tray": values["mean_tray"] - float(previous["mean_tray"]),
    }
    return CampaignMeasurement(
        static={
            name: BandValue.compare(values[name], band)
            for name, band in context.target_band.items()
        },
        temporal={
            name: BandValue.compare(deltas[name], band)
            for name, band in context.temporal_band.items()
        },
    )


def save_certified(result: CertificationResult, directory: Path, *, stem: str) -> dict[str, Path]:
    """Write one proven level and its independent evidence artifacts."""
    if not result.solved or result.certificate is None or result.campaign is None:
        raise ValueError("cannot save a candidate without a replayed winning certificate")
    directory.mkdir(parents=True, exist_ok=True)
    artifacts = {
        f"{stem}.json": directory / f"{stem}.json",
        f"{stem}.certificate.json": directory / f"{stem}.certificate.json",
        f"{stem}.metrics.json": directory / f"{stem}.metrics.json",
        "report.html": directory / "report.html",
    }
    artifacts[f"{stem}.json"].write_text(json.dumps(result.raw, separators=(",", ":")) + "\n")
    artifacts[f"{stem}.certificate.json"].write_text(
        json.dumps(result.certificate, indent=2) + "\n"
    )
    artifacts[f"{stem}.metrics.json"].write_text(
        json.dumps(result.campaign.to_dict(), indent=2) + "\n"
    )
    artifacts["report.html"].write_text(_report(stem, result))
    return artifacts


def _mechanical_violations(
    raw: dict[str, Any], level: Level, context: CampaignContext
) -> list[str]:
    """Return strict pre-search failures under the admitted L151 scope."""
    violations: list[str] = []
    if level.unsupported:
        violations.append("unsupported transition mechanics: " + ", ".join(level.unsupported))
    elif level.keys:
        initial = Engine(level, RULES).initial_state()
        initially_exposed = initial.waiting_keys | initial.freed_keys
        if initially_exposed:
            violations.append(
                f"{len(initially_exposed)} key(s) are exposed at initialization; "
                "bury each key behind an intentional opening blocker"
            )
    used = set(mechanics_used(raw))
    outside_slice = used - set(context.supported_design_mechanics)
    if outside_slice:
        violations.append(
            "mechanics outside the admitted L151 construction slice: "
            + ", ".join(sorted(outside_slice))
        )
    shortages = {
        material: level.ammo_by_material.get(material, 0) - demand
        for material, demand in level.demand_by_material.items()
        if level.ammo_by_material.get(material, 0) < demand
    }
    if shortages:
        violations.append(f"per-colour ammo does not cover demand: {shortages}")
    ordinary_materials = {
        int(pixel["material"]) for pixel in raw["PixelImageData"].get("pixels", [])
    }
    missing_pipe_triggers = sorted(
        {
            int(segment["Material"])
            for pipe in raw["PixelImageData"].get("pixelPipes", {}).get("Pipes", [])
            for segment in pipe.get("Queue", [])
            if int(segment["Material"]) not in ordinary_materials
        }
    )
    if missing_pipe_triggers:
        violations.append(
            f"Pixel Pipe segment has no matching live trigger pixel: {missing_pipe_triggers}"
        )
    if raw.get("SlotCount") != 5:
        violations.append(f"SlotCount must be 5, got {raw.get('SlotCount')}")
    if raw.get("ConveyorLimit") not in {4, 5}:
        violations.append(
            f"ConveyorLimit must be a shipped L1-150 value (4 or 5), got {raw.get('ConveyorLimit')}"
        )
    return violations


def _band_value_dict(value: BandValue) -> dict[str, float | bool]:
    """Encode one band comparison."""
    return {
        "value": round(value.value, 6),
        "low": value.low,
        "median": value.median,
        "high": value.high,
        "inside": value.inside,
    }


def _report(stem: str, result: CertificationResult) -> str:
    """Render a compact visual and metric review artifact."""
    assert result.campaign is not None and result.certificate is not None
    rows = []
    for axis, values in (
        ("static", result.campaign.static),
        ("change from L150", result.campaign.temporal),
    ):
        for name, value in values.items():
            state = "inside" if value.inside else "outside"
            rows.append(
                f"<tr><td>{axis}</td><td>{name}</td><td>{value.value:.3f}</td>"
                f"<td>{value.low:.3f}-{value.high:.3f}</td><td class={state}>{state}</td></tr>"
            )
    style = (
        "body{background:#131319;color:#eee;font:14px/1.45 system-ui;margin:32px}"
        "main{max-width:1000px;margin:auto}section{background:#1c1c24;padding:20px;"
        "border-radius:12px;margin:18px 0}svg{max-width:420px;height:auto;"
        "image-rendering:pixelated}"
        "table{border-collapse:collapse;width:100%}td,th{padding:8px;border-bottom:1px solid #333;"
        "text-align:left}.inside{color:#65d98b}.outside{color:#ff8c72}code{color:#68d9f4}"
    )
    return (
        "<!doctype html><meta charset=utf-8><title>conveyor shooter L151 candidate</title>"
        f"<style>{style}</style><main><h1>{stem} · replay-certified</h1>"
        f"<p><code>{result.strategy}</code> · {len(result.certificate['actions'])} primitive "
        f"actions · {result.explored_states} explored states</p><section>"
        f"{render.svg_board(result.raw, 12)}"
        "</section><section><h2>Campaign context</h2>"
        f"<p>{result.campaign.static_inside}/{result.campaign.static_total} static · "
        f"{result.campaign.temporal_inside}/{result.campaign.temporal_total} temporal</p>"
        "<table><thead><tr><th>axis</th>"
        "<th>dimension</th><th>candidate</th><th>observed band</th><th>status</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></section></main>"
    )


__all__ = [
    "BandValue",
    "CampaignMeasurement",
    "CertificationResult",
    "certify",
    "measure_campaign",
    "save_certified",
]
