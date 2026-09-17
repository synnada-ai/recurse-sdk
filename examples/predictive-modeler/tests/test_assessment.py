"""Independent checks of trusted benchmark model artifacts and recorded selection."""

import copy
import json
import zipfile
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
import pytest
from benchmarks.assessment import _audit_budget, _audit_folds, _scores_match, assess
from modeler import contracts, learning


def _fixture(
    frame: pd.DataFrame, root: Path, subset: bool = False, *, regression: bool = False
) -> dict[str, Any]:
    """Create known accepted artifacts from deterministic fixture observations."""
    quality = {
        "objective": {"metric": "model_bytes", "direction": "minimize"},
        "constraints": [{"metric": "f1", "operator": ">=", "value": 0.9}],
    }
    proposal = {"kind": "binary", "targets": ["y"], "features": ["category", "x"]}
    if regression:
        quality["constraints"] = [{"metric": "mae", "operator": "<=", "value": 2.0}]
        proposal.update(kind="regression", targets=["value"])
    case = {
        "request": {"quality": quality, "budget": {"max_trials": 2, "max_training_seconds": 120}},
        "specification": proposal,
    }
    spec = contracts.resolve(proposal, frame, quality)
    parts = learning.partition(frame, spec)
    spec["validation_method"] = parts["method"]
    spec["split_sizes"] = {
        "fit": len(parts["fit"]),
        "test": len(parts["test"]),
        "folds": [{key: len(rows) for key, rows in fold.items()} for fold in parts["folds"]],
    }
    config = learning.validate_configuration(
        {"family": "linear", "feature_subset": ["category"] if subset else None}, spec
    )
    model = learning.fit(frame.iloc[parts["fit"]], spec, config)
    path = root / "model.joblib"
    joblib.dump(model, path, compress=0, protocol=5)
    cv = learning.cross_validate(frame, spec, config, parts)
    final = learning.measure(model, frame.iloc[parts["fit"]], frame.iloc[parts["test"]], path)
    validation = cv["scores"] | {"model_bytes:{}": final["model_bytes:{}"]}
    with zipfile.ZipFile(root / "model-bundle.zip", "w") as archive:
        archive.write(path, "model.joblib")
    values = {
        "receipt.json": {"status": "succeeded"},
        "resolved-contract.json": spec,
        "splits.json": parts,
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
            "seconds": 1.0,
            "cross_validation": cv,
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
        ("resolved-contract.json", ["validation_method"], "unknown", "Resolved validation method"),
        ("resolved-contract.json", ["split_sizes", "fit"], 0, "Resolved validation method"),
        ("trials.jsonl", ["seconds"], -1, "Recorded trial duration is invalid"),
        ("trials.jsonl", ["cross_validation", "folds"], [], "Cross-validation fold count differs"),
        ("trials.jsonl", ["cross_validation", "scores"], {}, "Cross-validation aggregate scores"),
        ("splits.json", ["fit"], [], "Saved splits differ"),
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


@pytest.mark.parametrize("change", ["train_rows", "validation_rows", "scores"])
def test_changed_fold_evidence_fails_audit(
    frame: pd.DataFrame, tmp_path: Path, change: str
) -> None:
    """Fold scores and row counts must match independently replayed training."""
    case = _fixture(frame, tmp_path)
    path = tmp_path / "trials.jsonl"
    record = json.loads(path.read_text())
    record["cross_validation"]["folds"][0][change] = {} if change == "scores" else 0
    path.write_text(json.dumps(record))
    result = assess(case, tmp_path, frame)
    assert not result["verified"]
    assert any("Cross-validation fold" in issue for issue in result["issues"])


def test_training_budget_exhaustion_forbids_starting_another_trial(
    frame: pd.DataFrame, tmp_path: Path
) -> None:
    """An exhausted cumulative fit/CV budget cannot admit another candidate."""
    case = _fixture(frame, tmp_path)
    case["request"]["budget"]["max_training_seconds"] = 1
    path = tmp_path / "trials.jsonl"
    record = path.read_text()
    path.write_text(record + "\n" + record)
    result = assess(case, tmp_path, frame)
    assert not result["verified"]
    assert "A trial started after the training budget was exhausted." in result["issues"]


@pytest.mark.parametrize(
    "actual,recorded", [(None, 1), (1, None), (float("inf"), float("inf")), (1, float("nan"))]
)
def test_nonfinite_or_missing_scores_never_reproduce(actual: Any, recorded: Any) -> None:
    """Undefined metrics cannot support an accepted reproducibility claim."""
    assert not _scores_match({"mae:{}": actual}, {"mae:{}": recorded})


@pytest.mark.parametrize("drift,accepted", [(1e-7, True), (0.01, False)])
def test_refitted_continuous_scores_allow_only_small_numerical_drift(
    frame: pd.DataFrame, tmp_path: Path, drift: float, accepted: bool
) -> None:
    """Platform refits may differ slightly without weakening saved-model verification."""
    case = _fixture(frame, tmp_path, regression=True)
    path = tmp_path / "trials.jsonl"
    trial = json.loads(path.read_text())
    trial["cross_validation"]["scores"]["mae:{}"] += drift
    for fold in trial["cross_validation"]["folds"]:
        fold["scores"]["mae:{}"] += drift
    trial["validation"]["mae:{}"] += drift
    path.write_text(json.dumps(trial))
    path = tmp_path / "evaluation.json"
    evaluation = json.loads(path.read_text())
    evaluation["validation"] = trial["validation"]
    path.write_text(json.dumps(evaluation))
    assert assess(case, tmp_path, frame)["verified"] is accepted


def test_small_refit_drift_cannot_cross_hard_cv_constraint(
    frame: pd.DataFrame, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A score within reproducibility tolerance still must satisfy the original bound."""
    case = _fixture(frame, tmp_path, regression=True)
    path = tmp_path / "trials.jsonl"
    trial = json.loads(path.read_text())
    trial["cross_validation"]["scores"]["mae:{}"] = 2.0
    for fold in trial["cross_validation"]["folds"]:
        fold["scores"]["mae:{}"] = 2.0
    trial["validation"]["mae:{}"] = 2.0
    path.write_text(json.dumps(trial))
    path = tmp_path / "evaluation.json"
    evaluation = json.loads(path.read_text())
    evaluation["validation"] = trial["validation"]
    path.write_text(json.dumps(evaluation))
    replay = copy.deepcopy(trial["cross_validation"])
    replay["scores"]["mae:{}"] += 1e-7
    for fold in replay["folds"]:
        fold["scores"]["mae:{}"] += 1e-7
    monkeypatch.setattr(learning, "cross_validate", lambda *args: replay)
    assert assess(case, tmp_path, frame) == {
        "verified": False,
        "issues": ["Reproduced cross-validation constraints fail."],
    }


def test_recorded_cv_mean_is_not_given_refit_tolerance() -> None:
    """A small but inconsistent recorded mean is not a platform refit discrepancy."""
    actual: dict[str, Any] = {
        "scores": {"mae:{}": 2.0},
        "folds": [{"scores": {"mae:{}": 2.0}, "train_rows": 40, "validation_rows": 10}],
    }
    recorded = copy.deepcopy(actual)
    recorded["scores"]["mae:{}"] += 1e-7
    assert _audit_folds(actual, recorded) == [
        "Recorded cross-validation aggregate differs from its fold mean."
    ]


@pytest.mark.parametrize("refitted", [False, True])
@pytest.mark.parametrize("metric", ["model_bytes", "input_feature_count"])
def test_complexity_is_always_compared_exactly(refitted: bool, metric: str) -> None:
    """Even sub-tolerance differences in discrete artifact metrics are rejected."""
    key = metric + ":{}"
    assert not _scores_match({key: 100.0}, {key: 100.0 + 1e-8}, refitted=refitted)


@pytest.mark.parametrize("metric", ["mae", "f1"])
def test_saved_predictions_and_classification_keep_strict_tolerance(metric: str) -> None:
    """Only refitted continuous scores receive the wider numerical allowance."""
    key = metric + ":{}"
    assert not _scores_match({key: 1.0}, {key: 1.0 + 1e-7})
    assert _scores_match({key: 1.0}, {key: 1.0 + 1e-7}, refitted=True) is (metric == "mae")


def test_final_evaluated_trial_cannot_exceed_cumulative_time(
    frame: pd.DataFrame, tmp_path: Path
) -> None:
    """An accepted last candidate must obey the same cumulative budget as earlier trials."""
    case = _fixture(frame, tmp_path)
    path = tmp_path / "trials.jsonl"
    trial = json.loads(path.read_text())
    trial["seconds"] = 121.0
    path.write_text(json.dumps(trial))
    result = assess(case, tmp_path, frame)
    assert not result["verified"]
    assert "A successful trial exceeded the cumulative training budget." in result["issues"]


@pytest.mark.parametrize("status", ["failed", "timeout"])
def test_failed_final_worker_may_finish_cleanup_after_deadline(status: str) -> None:
    """Cleanup overrun preserves a failed trial without treating it as successful training."""
    history = [{"seconds": 1.0, "status": "evaluated"}, {"seconds": 120.0, "status": status}]
    assert _audit_budget(history, {"max_training_seconds": 120}) == []
