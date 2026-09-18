# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""End-to-end checks for the minimal slide-and-collect design loop (no cloud run needed).

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

AGENT = Path(__file__).resolve().parents[1] / "agent"

# shape_id, x, y, color, rotation
SEATS = [
    (1, 0, 7, 0, 0),
    (6, 4, 6, 1, 0),
    (1, 0, 1, 2, 1),
    (2, 2, 3, 3, 0),
    (0, 5, 1, 4, 0),
    (1, 3, 0, 5, 0),
]


@pytest.fixture
def tools(tmp_path: Path) -> Any:
    """Load the adapter against a stubbed Recurse run context for slot 34."""
    sys.path.insert(0, str(AGENT))
    sys.modules["recurse"] = SimpleNamespace(  # type: ignore[assignment]
        context=lambda: SimpleNamespace(inputs={"slot": 34, "brief": ""}, workspace=tmp_path)
    )
    for name in ("tools", "toolkit"):
        sys.modules.pop(name, None)
    import tools as module  # noqa: PLC0415 - imported after the recurse stub is installed

    return module


def test_every_public_tool_is_registered(tools: Any) -> None:
    """Every public adapter function is registered in agent.yaml, and nothing else."""
    registered = set(yaml.safe_load((AGENT / "agent.yaml").read_text())["tools"]["register"])
    public = {
        name
        for name, value in vars(tools).items()
        if callable(value) and not name.startswith("_") and value.__module__ == "tools"
    } - {"DesignSession"}
    assert public == registered


def test_window_reports_gated_bands_for_the_slot(tools: Any) -> None:
    """The window lists gated feature bands for the requested slot."""
    report = tools.window(tools.begin_design())
    assert "target slot: 34" in report
    assert "headroom" in report and "(gates)" in report


def test_layered_design_is_certified_and_saved(tools: Any, tmp_path: Path) -> None:
    """A solvable layered design certifies, saves, and leaves a call history."""
    session = tools.begin_design()
    tools.new_board(session, 6, 8)
    for seat in SEATS:
        tools.place_seat(session, *seat)
    tools.set_inner(session, 0, 7)
    tools.populate(session, 1, 0.5)
    critique = tools.critique_design(session)
    assert "FAIL" not in critique and "INFO: solve" in critique
    assert tools.save_design(session, "layered domino").startswith("saved Gen_34.json")
    level = json.loads((tmp_path / "Gen_34.json").read_text())
    assert sum(seat["InnerBrickColorIndex"] not in (None, -1) for seat in level["Seats"]) == 1
    assert (tmp_path / "report.html").is_file()
    history = [json.loads(line) for line in (tmp_path / "history.jsonl").read_text().splitlines()]
    assert [entry["tool"] for entry in history][-2:] == ["critique_design", "save_design"]


def test_save_refuses_a_design_with_failures(tools: Any, tmp_path: Path) -> None:
    """A landscape board fails the critique and the save is refused."""
    session = tools.begin_design()
    tools.new_board(session, 8, 4)  # landscape boards always fail
    tools.place_seat(session, 1, 0, 0, 0, 0)
    tools.populate(session, 0, 0.4)
    assert "FAIL" in tools.critique_design(session)
    with pytest.raises(ValueError, match="cannot save"):
        tools.save_design(session, "should be refused")
    last = json.loads((tmp_path / "history.jsonl").read_text().splitlines()[-1])
    assert last["tool"] == "save_design" and last["result"].startswith("ERROR: cannot save")
