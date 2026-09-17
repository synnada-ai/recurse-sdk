"""Independent checks of trusted benchmark model artifacts and recorded selection."""

import json
import zipfile
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
import pytest
from benchmarks.assessment import assess
from modeler import contracts, learning


def _fixture(frame: pd.DataFrame, root: Path, subset: bool = False) -> dict[str, Any]:
    """Create known accepted artifacts from deterministic fixture observations."""
    quality = {
        "objective": {"metric": "model_bytes", "direction": "minimize"},
        "constraints": [{"metric": "f1", "operator": ">=", "value": 0.9}],
    }
    proposal = {"kind": "binary", "targets": ["y"], "features": ["category", "x"]}
    case = {"request": {"quality": quality, "budget": {"max_trials": 2}}, "specification": proposal}
    spec = contracts.resolve(proposal, frame, quality)
    parts = learning.partition(frame, spec)
    config = learning.validate_configuration(
        {"family": "linear", "feature_subset": ["category"] if subset else None}, spec
    )
    model = learning.fit(frame.iloc[parts[0]], spec, config)
    path = root / "model.joblib"
    joblib.dump(model, path, compress=0, protocol=5)
    validation = learning.measure(model, frame.iloc[parts[0]], frame.iloc[parts[1]], path)
    final = learning.measure(model, frame.iloc[parts[0]], frame.iloc[parts[2]], path)
    with zipfile.ZipFile(root / "model-bundle.zip", "w") as archive:
        archive.write(path, "model.joblib")
    values = {
        "receipt.json": {"status": "succeeded"},
        "resolved-contract.json": spec,
        "splits.json": dict(
            zip(["train", "validation", "test"], [part.tolist() for part in parts], strict=True)
        ),
        "evaluation.json": {
            "selected_trial": 1,
            "validation": validation,
            "final_test": final,
            "final_test_passed": True,
        },
        "trials.jsonl": {
            "id": 1,
            "configuration": config,
            "status": "evaluated",
            "validation": validation,
        },
    }
    for name, value in values.items():
        (root / name).write_text(json.dumps(value))
    return case


@pytest.mark.parametrize("subset", [False, True])
def test_audit_reproduces_complete_pipeline_and_feature_subset(
    frame: pd.DataFrame, tmp_path: Path, subset: bool
) -> None:
    """Exact model bytes and predictive metrics reproduce for either feature selection."""
    case = _fixture(frame, tmp_path, subset)
    assert assess(case, tmp_path, frame) == {"verified": True, "issues": []}


@pytest.mark.parametrize(
    "mutation",
    [
        ("resolved-contract.json", ["seed"], 17, "Resolved task or quality differs"),
        ("splits.json", ["train"], [], "Saved splits differ"),
        ("trials.jsonl", ["status"], "trained", "A trained candidate was not evaluated"),
        ("receipt.json", ["status"], "no_feasible_model", "No accepted model"),
        ("evaluation.json", ["selected_trial"], 2, "Selected trial is not the best"),
        ("evaluation.json", ["final_test_passed"], False, "Accepted result reports a failed"),
        ("evaluation.json", ["validation"], {}, "validation scores do not reproduce"),
        (
            "evaluation.json",
            ["final_test", "model_bytes:{}"],
            None,
            "final_test scores do not reproduce",
        ),
        (
            "evaluation.json",
            ["final_test", "model_bytes:{}"],
            1.0,
            "final_test scores do not reproduce",
        ),
        (
            "trials.jsonl",
            ["validation", "model_bytes:{}"],
            1.0,
            "Selected validation scores differ",
        ),
    ],
)
def test_changed_records_fail_audit(
    frame: pd.DataFrame,
    tmp_path: Path,
    mutation: tuple[str, list[str], Any, str],
) -> None:
    """The audit identifies the changed contract, split, selection or measurement."""
    filename, path, replacement, message = mutation
    case = _fixture(frame, tmp_path)
    file = tmp_path / filename
    content = json.loads(file.read_text())
    parent = content
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = replacement
    file.write_text(json.dumps(content))
    result = assess(case, tmp_path, frame)
    assert not result["verified"]
    assert any(message in issue for issue in result["issues"])


@pytest.mark.parametrize(
    "mutation",
    [
        ("configuration", "trees", 42, "Saved model configuration differs"),
        ("specification", "seed", 17, "Saved model semantics differ"),
        ("specification", "features", ["category"], "Saved model features differ"),
    ],
)
def test_changed_model_fails_audit(
    frame: pd.DataFrame, tmp_path: Path, mutation: tuple[str, str, Any, str]
) -> None:
    """Recorded evidence cannot substitute a different fitted predictor."""
    field, key, value, message = mutation
    case = _fixture(frame, tmp_path)
    path = tmp_path / "model.joblib"
    model = joblib.load(path)
    getattr(model, field)[key] = value
    joblib.dump(model, path, compress=0, protocol=5)
    with zipfile.ZipFile(tmp_path / "model-bundle.zip", "w") as archive:
        archive.write(path, "model.joblib")
    result = assess(case, tmp_path, frame)
    assert not result["verified"]
    assert any(message in issue for issue in result["issues"])


def test_missing_evidence_and_exceeded_budget_fail(frame: pd.DataFrame, tmp_path: Path) -> None:
    """Audit failures remain evidence, rather than dropping unsuccessful benchmark attempts."""
    case = _fixture(frame, tmp_path)
    case["request"]["budget"]["max_trials"] = 0
    (tmp_path / "model-bundle.zip").unlink()
    result = assess(case, tmp_path, frame)
    assert not result["verified"]
    assert result["issues"][0] == "Trial count exceeds the requested budget."
    assert "FileNotFoundError" in result["issues"][1]


def test_reproduced_constraint_failure_cannot_pass(frame: pd.DataFrame, tmp_path: Path) -> None:
    """An optimistic reported F1 does not override independent final-test measurement."""
    case = _fixture(frame, tmp_path)
    case["request"]["quality"]["constraints"][0]["value"] = 1.1
    for name in ["trials.jsonl", "evaluation.json"]:
        path = tmp_path / name
        record = json.loads(path.read_text())
        record["validation"]['f1:{"average": "binary", "positive_label": "1"}'] = 1.2
        path.write_text(json.dumps(record))
    result = assess(case, tmp_path, frame)
    assert not result["verified"]
    assert "Reproduced final-test constraints fail." in result["issues"]
