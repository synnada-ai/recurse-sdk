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
        expected_splits = dict(
            zip(["train", "validation", "test"], [part.tolist() for part in parts], strict=True)
        )
        splits = json.loads((workspace / "splits.json").read_text())
        check(splits == expected_splits, "Saved splits differ from the frozen evaluation policy.")
        history = [
            json.loads(line) for line in (workspace / "trials.jsonl").read_text().splitlines()
        ]
        budget = case["request"]["budget"]
        check(len(history) <= budget["max_trials"], "Trial count exceeds the requested budget.")
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
            reproduced = {
                "validation": learning.measure(
                    model, data.iloc[parts[0]], data.iloc[parts[1]], model_path
                ),
                "final_test": learning.measure(
                    model, data.iloc[np.concatenate(parts[:2])], data.iloc[parts[2]], model_path
                ),
            }
        for name, values in reproduced.items():
            recorded = evaluation[name]
            check(
                values.keys() == recorded.keys()
                and all(
                    value is not None
                    and recorded[key] is not None
                    and bool(np.isclose(value, recorded[key], rtol=1e-9, atol=1e-9))
                    for key, value in values.items()
                ),
                f"{name} scores do not reproduce.",
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
