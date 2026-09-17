"""Opt-in verification of the full tool chain on all six real datasets."""

import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
import pytest
import yaml
from jsonschema import Draft202012Validator

from recurse import _activate as activate
from recurse import _deactivate as deactivate

ROOT = Path(__file__).parents[1]


def _configurations(name: str) -> list[dict[str, Any]]:
    """Choose fixed probes for infrastructure verification, not an agent search policy."""
    if name == "binary":
        return [
            {"family": "baseline"},
            {"family": "linear", "class_weight": "balanced"},
            {"family": "extra_trees", "trees": 50},
        ]
    if name == "multilabel":
        return [
            {"family": "baseline"},
            {"family": "linear", "threshold": 0.2},
            {"family": "linear", "threshold": 0.35},
        ]
    if name in {"forecast", "panel-forecast"}:
        return [
            {"family": "baseline"},
            {"family": "seasonal", "seasonal_period": 12 if name == "panel-forecast" else 7},
            {"family": "linear", "lags": [1, 2, 3, 12] if name == "panel-forecast" else [1, 7, 14]},
        ]
    return [{"family": "baseline"}, {"family": "linear"}, {"family": "extra_trees", "trees": 50}]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("name", "rows"),
    [
        ("binary", 41188),
        ("multiclass", 13611),
        ("multilabel", 54263),
        ("regression", 1030),
        ("forecast", 731),
        ("panel-forecast", 109280),
    ],
)
def test_real_dataset_tool_chain(name: str, rows: int, tools: Any, tmp_path: Path) -> None:
    """Real source data reaches a reloadable accepted artifact through actual subprocess fits."""
    request = json.loads((ROOT / "inputs" / f"{name}.json").read_text())
    schema = yaml.safe_load((ROOT / "agent.yaml").read_text())
    Draft202012Validator(schema["inputs"]).validate(request)
    specification = json.loads((ROOT / "tests/specifications.json").read_text())[name]
    request["budget"]["max_trials"] = 3
    workspace = tmp_path / name
    workspace.mkdir()
    cache = ROOT.parents[1] / ".dataset-cache"
    if cache.exists():
        shutil.copytree(cache, workspace / ".modeler/downloads")
    activate({key: value for key, value in request.items() if key != "task"}, workspace)
    try:
        tools.review_inputs(request["task"], request["quality"], [], [])
        assert tools.inspect_dataset()["rows"] == rows
        tools.resolve_problem(specification)
        data = pd.read_parquet(workspace / ".modeler/data.parquet")
        splits = json.loads((workspace / "splits.json").read_text())
        for config in _configurations(name):
            trial = tools.train_candidate(config, f"Local verification probe: {config}.")
            assert trial["status"] == "trained", trial["diagnostic"]
            tools.evaluate_candidate(trial["id"])
        receipt = tools.finish_run(
            "budget_exhausted",
            "Three fixed local probes; this verifies tools, not autonomous agent behavior.",
        )
        Draft202012Validator(schema["outputs"]).validate(receipt)
        assert receipt["status"] == "succeeded"
        with zipfile.ZipFile(workspace / "model-bundle.zip") as archive:
            archive.extractall(tmp_path / "bundle")
        model = joblib.load(tmp_path / "bundle/model.joblib")
        inputs = (
            data.iloc[splits["fit"]]
            if specification["kind"] == "forecast"
            else data.iloc[splits["test"]]
        )
        prediction = model.predict(inputs)
        assert len(prediction) == len(splits["test"])
        destination = os.environ.get("MODELER_EVIDENCE_DIR")
        if destination:
            output = Path(destination) / name
            output.mkdir(parents=True, exist_ok=True)
            for path in workspace.iterdir():
                if path.is_file():
                    shutil.copy2(path, output / path.name)
    finally:
        deactivate()


@pytest.mark.integration
@pytest.mark.parametrize("name", ["compact-regression", "feature-limited-multiclass"])
def test_real_complexity_requirements(name: str, tools: Any, tmp_path: Path) -> None:
    """Complexity examples reach an accepted, independently sized model on real data."""
    request = json.loads((ROOT / "inputs" / f"{name}.json").read_text())
    schema = yaml.safe_load((ROOT / "agent.yaml").read_text())
    Draft202012Validator(schema["inputs"]).validate(request)
    regression = name == "compact-regression"
    specification = json.loads((ROOT / "tests/specifications.json").read_text())[
        "regression" if regression else "multiclass"
    ]
    configs = (
        [{"family": "baseline"}, {"family": "linear"}, {"family": "extra_trees", "trees": 50}]
        if regression
        else [
            {"family": "baseline", "feature_subset": ["Area"]},
            {
                "family": "linear",
                "feature_subset": ["Area", "Perimeter", "roundness", "ShapeFactor1"],
            },
            {
                "family": "extra_trees",
                "trees": 50,
                "feature_subset": ["Area", "Perimeter", "roundness", "ShapeFactor1"],
            },
        ]
    )
    cache = ROOT.parents[1] / ".dataset-cache"
    if cache.exists():
        shutil.copytree(cache, tmp_path / ".modeler/downloads")
    activate({key: value for key, value in request.items() if key != "task"}, tmp_path)
    try:
        tools.review_inputs(request["task"], request["quality"], [], [])
        tools.inspect_dataset()
        tools.resolve_problem(specification)
        for config in configs:
            trial = tools.train_candidate(
                config, "Fixed comparison of size and predictive quality."
            )
            assert trial["status"] == "trained", trial["diagnostic"]
            tools.evaluate_candidate(trial["id"])
        receipt = tools.finish_run("budget_exhausted", "Local fixed-policy complexity check.")
        assert receipt["status"] == "succeeded"
        evaluation = json.loads((tmp_path / "evaluation.json").read_text())
        if regression:
            with zipfile.ZipFile(tmp_path / "model-bundle.zip") as archive:
                assert (
                    evaluation["final_test"]["model_bytes:{}"]
                    == archive.getinfo("model.joblib").file_size
                )
            assert evaluation["final_test"]["mae:{}"] <= 10
        else:
            assert evaluation["final_test"]["input_feature_count:{}"] <= 4
    finally:
        deactivate()
