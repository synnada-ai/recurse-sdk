"""Frozen pilot applications separate agent instructions from evaluator answers."""

import io
import json
import runpy
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from benchmarks.prepare import prepare

from recurse import build_bundle


def test_prepare_freezes_equal_requests_and_isolates_only_prompt_change(tmp_path: Path) -> None:
    """Both applications package, and neither receives the private case specifications."""
    root = tmp_path / "pilot"
    plan = prepare(root)
    assert plan["cloud_runs_launched"] == 0
    assert len(plan["schedule"]) == 8
    assert plan["applications"]["baseline"] != plan["applications"]["complexity-aware"]
    assert plan["schedule"][0] == {"case": "penguins", "repeat": 0, "variant": "baseline"}
    assert plan["schedule"][4] == {"case": "penguins", "repeat": 1, "variant": "complexity-aware"}
    for name, case in plan["cases"].items():
        request = json.loads((root / "requests" / f"{name}.json").read_text())
        assert request == case["request"]
        assert set(request) == {"dataset", "task", "quality", "budget"}
    for variant in plan["applications"]:
        assert (
            yaml.safe_load((root / variant / "agent.yaml").read_text())["agent"]["model"]
            == "gpt-5.6-luna"
        )
        artifacts, _ = build_bundle(root / variant)
        with tarfile.open(fileobj=io.BytesIO(artifacts["source"]), mode="r:gz") as archive:
            names = [Path(name).parts[1:] for name in archive.getnames()]
        assert all("benchmarks" not in name and "tests" not in name for name in names)
    for path in (root / "baseline").rglob("*"):
        if path.is_file() and path.name != "prompt.md":
            assert (
                path.read_bytes()
                == (root / "complexity-aware" / path.relative_to(root / "baseline")).read_bytes()
            )
    assert (
        (root / "complexity-aware/prompt.md")
        .read_text()
        .startswith((root / "baseline/prompt.md").read_text())
    )
    with pytest.raises(FileExistsError):
        prepare(root)


def test_prepare_command_writes_plan_without_launching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Preparation is a local operation that emits the same reviewable plan it saves."""
    root = tmp_path / "command"
    monkeypatch.setattr(sys, "argv", ["prepare", str(root)])
    monkeypatch.delitem(sys.modules, "benchmarks.prepare")
    runpy.run_module("benchmarks.prepare", run_name="__main__")
    result = json.loads(capsys.readouterr().out)
    assert result == json.loads((root / "plan.json").read_text())
    assert result["cloud_runs_launched"] == 0
