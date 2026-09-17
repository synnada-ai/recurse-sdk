"""Reproduce accepted results from trusted benchmark runs, outside the deployed agent."""

import json
import shutil
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import joblib
import numpy as np
import pandas as pd
from modeler import contracts, learning

__all__ = ["assess"]

_CONTINUOUS = {"mae", "rmse", "mase", "absolute_bias"}
_COMPLEXITY = {"model_bytes", "input_feature_count"}


def assess(case: dict[str, Any], workspace: Path, data: pd.DataFrame) -> dict[str, Any]:
    """Audit a trusted accepted bundle against evaluator-owned semantics and observations.

    Only call on artifacts produced by your own runs: loading joblib executes pickle code.
    This checks recorded selection and reproduces the selected model's scores; it cannot
    prove that an LLM never observed holdout data or reproduce discarded candidate fits.
    The same current verifier/dependencies must be used for baseline and contender.
    """
    issues: list[str] = []

    def check(condition: bool, message: str) -> None:
        """Retain every independently observed discrepancy."""
        if not condition:
            issues.append(message)

    try:
        receipt = json.loads((workspace / "receipt.json").read_text())
        if receipt["status"] != "succeeded":
            return {"verified": False, "issues": ["No accepted model to reproduce."]}
        actual = json.loads((workspace / "resolved-contract.json").read_text())
        expected = contracts.resolve(case["specification"], data, case["request"]["quality"])
        check(
            all(actual.get(key) == value for key, value in expected.items()),
            "Resolved task or quality differs from the evaluator's expected contract.",
        )
        parts = learning.partition(data, expected)
        check(
            actual.get("validation_method") == parts["method"]
            and actual.get("split_sizes")
            == {
                "fit": len(parts["fit"]),
                "test": len(parts["test"]),
                "folds": [
                    {key: len(rows) for key, rows in fold.items()} for fold in parts["folds"]
                ],
            },
            "Resolved validation method or split sizes differ from the evaluation plan.",
        )
        splits = json.loads((workspace / "splits.json").read_text())
        check(splits == parts, "Saved splits differ from the frozen evaluation policy.")
        history = [
            json.loads(line) for line in (workspace / "trials.jsonl").read_text().splitlines()
        ]
        budget = case["request"]["budget"]
        check(len(history) <= budget["max_trials"], "Trial count exceeds the requested budget.")
        issues.extend(_audit_budget(history, budget))
        check(
            all(item["status"] != "trained" for item in history),
            "A trained candidate was not evaluated.",
        )
        candidates = [
            item
            for item in history
            if item["status"] == "evaluated" and contracts.feasible(expected, item["validation"])
        ]
        objective = expected["quality"]["objective"]
        direction = -1 if objective["direction"] == "maximize" else 1
        winner = min(
            candidates,
            key=lambda item: direction * item["validation"][contracts.metric_key(objective)],
        )
        evaluation = json.loads((workspace / "evaluation.json").read_text())
        check(
            winner["id"] == evaluation["selected_trial"],
            "Selected trial is not the best feasible recorded candidate.",
        )
        check(
            winner["validation"] == evaluation["validation"],
            "Selected validation scores differ from the trial history.",
        )
        with (
            TemporaryDirectory() as directory,
            zipfile.ZipFile(workspace / "model-bundle.zip") as archive,
        ):
            model_path = Path(directory) / "model.joblib"
            with archive.open("model.joblib") as source, model_path.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            model = joblib.load(model_path)
            check(
                model.configuration == winner["configuration"],
                "Saved model configuration differs from the selected candidate.",
            )
            model_spec = model.specification | {
                "features": actual["features"],
                "text_features": actual["text_features"],
            }
            check(model_spec == actual, "Saved model semantics differ from the resolved contract.")
            subset = winner["configuration"]["feature_subset"]
            check(
                model.specification["features"]
                == (actual["features"] if subset is None else subset),
                "Saved model features differ from the selected candidate.",
            )
            cross_validation = learning.cross_validate(
                data, expected, winner["configuration"], parts
            )
            issues.extend(_audit_folds(cross_validation, winner["cross_validation"]))
            final = learning.measure(
                model, data.iloc[parts["fit"]], data.iloc[parts["test"]], model_path
            )
            complexity = {
                key: value
                for key, value in final.items()
                if key in {"model_bytes:{}", "input_feature_count:{}"}
            }
            reproduced = {
                "validation": cross_validation["scores"] | complexity,
                "final_test": final,
            }
        for name, values in reproduced.items():
            recorded = evaluation[name]
            check(
                _scores_match(values, recorded, refitted=name == "validation"),
                f"{name} scores do not reproduce.",
            )
        check(
            contracts.feasible(expected, reproduced["validation"]),
            "Reproduced cross-validation constraints fail.",
        )
        check(
            contracts.feasible(expected, reproduced["final_test"]),
            "Reproduced final-test constraints fail.",
        )
        check(
            evaluation["final_test_passed"] is True, "Accepted result reports a failed final test."
        )
    except Exception as error:
        issues.append(f"Artifact audit failed: {type(error).__name__}: {error}")
    return {"verified": not issues, "issues": issues}


def _scores_match(
    actual: dict[str, Any], recorded: dict[str, Any], *, refitted: bool = False
) -> bool:
    """Allow numerical refit drift only for continuous metrics, never model complexity."""
    if actual.keys() != recorded.keys():
        return False
    for key, value in actual.items():
        saved = recorded[key]
        if value is None or saved is None or not np.isfinite(value) or not np.isfinite(saved):
            return False
        metric = key.split(":", 1)[0]
        if metric in _COMPLEXITY:
            matches = value == saved
        else:
            rtol, atol = (1e-6, 1e-8) if refitted and metric in _CONTINUOUS else (1e-9, 1e-9)
            matches = bool(np.isclose(value, saved, rtol=rtol, atol=atol))
        if not matches:
            return False
    return True


def _audit_folds(actual: dict[str, Any], recorded: dict[str, Any]) -> list[str]:
    """Reproduce every recorded fold and its equally weighted predictive mean."""
    issues = []
    if len(actual["folds"]) != len(recorded["folds"]):
        issues.append("Cross-validation fold count differs.")
    for fold, saved in zip(actual["folds"], recorded["folds"], strict=False):
        if (fold["train_rows"], fold["validation_rows"]) != (
            saved["train_rows"],
            saved["validation_rows"],
        ):
            issues.append("Cross-validation fold sizes differ.")
        if not _scores_match(fold["scores"], saved["scores"], refitted=True):
            issues.append("Cross-validation fold scores do not reproduce.")
    if not _scores_match(actual["scores"], recorded["scores"], refitted=True):
        issues.append("Cross-validation aggregate scores do not reproduce.")
    means = (
        {
            key: None
            if any(fold["scores"].get(key) is None for fold in recorded["folds"])
            else float(np.mean([fold["scores"][key] for fold in recorded["folds"]]))
            for key in recorded["scores"]
        }
        if recorded["folds"]
        else {}
    )
    if not _scores_match(means, recorded["scores"]):
        issues.append("Recorded cross-validation aggregate differs from its fold mean.")
    return issues


def _audit_budget(history: list[dict[str, Any]], budget: dict[str, Any]) -> list[str]:
    """Reject invalid durations and new trials after cumulative worker-time exhaustion."""
    issues = []
    elapsed = 0.0
    for item in history:
        if elapsed >= budget["max_training_seconds"]:
            issues.append("A trial started after the training budget was exhausted.")
        seconds = item["seconds"]
        if not np.isfinite(seconds) or seconds < 0:
            issues.append("Recorded trial duration is invalid.")
        elapsed += seconds
    return issues
