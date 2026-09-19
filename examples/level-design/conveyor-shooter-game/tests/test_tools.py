# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""End-to-end checks for the minimal conveyor shooter design loop (no cloud run needed).

Run with: uv run --directory tests --locked pytest
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
SUBJECT = "a pixel dragon"
REVIEW = (SUBJECT, '["horned head", "spread wings"]', "keys sit as jewels on the body")


@pytest.fixture
def tools(tmp_path: Path) -> Any:
    """Load the adapter against a stubbed Recurse run context holding the dragon input."""
    sys.path.insert(0, str(AGENT))
    inputs = json.loads((ROOT / "inputs" / "dragon.json").read_text())
    sys.modules["recurse"] = SimpleNamespace(  # type: ignore[assignment]
        context=lambda: SimpleNamespace(inputs=inputs, workspace=tmp_path)
    )
    for name in ("tools", "toolkit"):
        sys.modules.pop(name, None)
    import tools as module  # noqa: PLC0415 - imported after the recurse stub is installed

    return module


def _composed(tools: Any, keys: list[tuple[int, int]]) -> Any:
    session = tools.begin_design()
    variant = json.loads(tools.extract_image_variants(session))[0]
    tools.freeze_image_baseline(session, variant, SUBJECT)
    tools.start_from_source(session, SUBJECT)
    tools.apply_composition_school(session, "layered-inset-frame", "[2,3,4,5]", 151)
    for x, y in keys:
        tools.place_key(session, x, y)
    tools.look(session)
    tools.review_visual_retention(session, *REVIEW)
    return session


def test_every_public_tool_is_registered(tools: Any) -> None:
    """Every public adapter function is registered in agent.yaml, and nothing else."""
    registered = set(yaml.safe_load((AGENT / "agent.yaml").read_text())["tools"]["register"])
    public = {
        name
        for name, value in vars(tools).items()
        if callable(value) and not name.startswith("_") and value.__module__ == "tools"
    } - {"DesignSession"}
    assert public == registered


def test_buried_keys_are_certified_and_saved(tools: Any, tmp_path: Path) -> None:
    """Keys buried inside the picture certify, save, and leave a call history."""
    session = _composed(tools, [(9, 30), (16, 29)])
    result = json.loads(tools.configure_gameplay(session, 2, 3, 0))
    assert result["solved"] and result["meets_save_gate"] and not result["activity_violations"]
    receipt = json.loads(tools.save_candidate(session))
    assert "level_151_1.json" in receipt["artifacts"]
    level = json.loads((tmp_path / "level_151_1.json").read_text())
    assert len(level["PixelImageData"]["keys"]["Keys"]) == len(level["Locks"]["Shooters"]) == 2
    history = [json.loads(line) for line in (tmp_path / "history.jsonl").read_text().splitlines()]
    assert [entry["tool"] for entry in history][-2:] == ["configure_gameplay", "save_candidate"]


def test_exposed_keys_are_rejected_and_cannot_be_saved(tools: Any) -> None:
    """Keys with a clear firing line are rejected and the save is refused."""
    session = _composed(tools, [(0, 24), (32, 0)])  # edge keys have a clear firing line
    result = json.loads(tools.configure_gameplay(session, 2, 2, 0))
    assert not result["solved"]
    assert any("exposed at initialization" in violation for violation in result["violations"])
    with pytest.raises(ValueError, match="refusing to save"):
        tools.save_candidate(session)
