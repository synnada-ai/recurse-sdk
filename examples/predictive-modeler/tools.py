"""Predictive Modeler's consistency gate, experiments, independent scorer, and receipt."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

import joblib
import pandas as pd
from modeler import contracts, datasets, learning, state
from modeler.proposals import ProposalValue, ProseQuality

import recurse

__all__ = [
    "evaluate_candidate",
    "experiment_history",
    "finish_run",
    "get_request",
    "inspect_dataset",
    "resolve_problem",
    "review_inputs",
    "train_candidate",
]


def _root() -> Path:
    """Locate private working state inside this run's workspace."""
    return Path(recurse.context().workspace) / ".modeler"


def _write(path: Path, value: Any) -> None:
    """Write finite structured evidence."""
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def get_request() -> dict[str, Any]:
    """Read structured requirements before assessing the natural-language task.

    Returns:
        Dataset, quality, and budget. The natural-language task is already in the agent prompt.
    """
    return cast(
        dict[str, Any], json.loads(json.dumps(dict(recurse.context().inputs), default=dict))
    )


def review_inputs(
    task_summary: str, task_quality: ProseQuality, conflicts: list[str], questions: list[str]
) -> dict[str, Any]:
    """Record the first semantic review before downloading data or fitting anything.

    Args:
        task_summary: Faithful interpretation of the caller's task, including prediction time.
        task_quality: Only objective/constraints explicitly stated in the prose, using the same
            shape as quality. Do not copy structured quality into this independent interpretation.
        conflicts: Explain any semantic contradictions between the prose and structured quality.
        questions: Material ambiguities preventing a defensible task interpretation.

    Returns:
        Review status, conflicts, and questions. Rejected reviews require finish_run immediately.
    """
    if not task_summary.strip():
        raise contracts.ModelerError("A meaningful task interpretation is required.")
    # Direct Python callers also get validation; the harness checks the TypedDict schema first.
    quality = cast(dict[str, Any], task_quality)
    objective = quality.get("objective", {})
    constraints = quality.get("constraints", [])
    if (
        quality.keys() - {"objective", "constraints"}
        or not isinstance(objective, dict)
        or objective.keys() - {"metric", "direction", "parameters"}
        or not isinstance(constraints, list)
        or any(
            not isinstance(item, dict)
            or not {"metric", "operator", "value"} <= item.keys()
            or item.keys() - {"metric", "operator", "value", "parameters"}
            for item in constraints
        )
    ):
        raise contracts.ModelerError(
            "task_quality must contain an objective object and/or a constraints list of metric, "
            "operator, value objects. Extract these from the prose; do not pass quality.objective "
            "as task_quality. Omit requirements not stated in the prose."
        )
    supplied = get_request().get("quality", {})
    detected = list(conflicts)
    for key, value in objective.items():
        actual = supplied.get("objective", {}).get(key)
        if actual is not None and _disagrees(value, actual):
            detected.append(
                f"Prose objective {key}={value!r} conflicts with quality value {actual!r}."
            )
    for constraint in constraints:
        matching = [
            item
            for item in supplied.get("constraints", [])
            if item["metric"] == constraint["metric"]
            and item.get("parameters", {}) == constraint.get("parameters", {})
        ]
        if matching and constraint not in matching:
            detected.append(f"Prose constraint conflicts with quality: {constraint!r}.")
    outcome = (
        "inconsistent_inputs" if detected else "needs_clarification" if questions else "aligned"
    )
    review = {
        "status": outcome,
        "summary": task_summary,
        "task_quality": task_quality,
        "conflicts": detected,
        "questions": questions,
    }
    with state.transaction(_root()) as connection:
        previous = state.get(connection, "review")
        if previous is not None:
            if previous != review:
                raise contracts.ModelerError(
                    "The input review is immutable; start a new run to revise it."
                )
            return cast(dict[str, Any], previous)
        state.put(connection, "review", review)
    return review


def inspect_dataset() -> dict[str, Any]:
    """Download and profile the dataset only after an aligned input review.

    Returns:
        Column names, types, missingness, distinct counts, and dataset fingerprint.
    """
    with state.transaction(_root()) as connection:
        review = state.require(connection, "review")
        if review["status"] != "aligned":
            raise contracts.ModelerError("Resolve the input review before loading data.")
        previous = state.get(connection, "dataset")
        if previous:
            return cast(dict[str, Any], previous)
        data = datasets.load_dataset(str(get_request()["dataset"]), _root() / "downloads")
        data = data.reset_index(drop=True)
        path = _root() / "data.parquet"
        data.to_parquet(path, index=False)
        result = datasets.profile(data) | {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        state.put(connection, "dataset", result)
    return result


def resolve_problem(specification: dict[str, ProposalValue]) -> dict[str, Any]:
    """Freeze targets, prediction semantics, quality, and split membership before search.

    Args:
        specification: kind (binary/multiclass/multilabel/regression/forecast), targets (list),
            features (list), optional text_features (list), group/time (column names), split
            (random/group/temporal/official), seed (integer); forecasting also needs frequency
            (pandas offset, e.g. D or MS) and horizon (positive integer). Forecast features must
            be empty: this version supports observed history and calendar features only.

    Returns:
        The normalized immutable contract, including split sizes and dataset fingerprint.
    """
    with state.transaction(_root()) as connection:
        dataset = state.require(connection, "dataset")
        if state.get(connection, "contract"):
            raise contracts.ModelerError("The contract is already frozen.")
        review = state.get(connection, "review")
        supplied = get_request().get("quality", {})
        quality = _merge(review["task_quality"], supplied)
        # Prose-only constraints still apply when structured quality adds other requirements.
        quality["constraints"] = list(supplied.get("constraints", []))
        for item in review["task_quality"].get("constraints", []):
            if item not in quality["constraints"]:
                quality["constraints"].append(item)
        data = pd.read_parquet(_root() / "data.parquet")
        contract = contracts.resolve(specification, data, quality)
        parts = learning.partition(data, contract)
        splits = dict(
            zip(["train", "validation", "test"], [part.tolist() for part in parts], strict=True)
        )
        contract["split_sizes"] = {name: len(rows) for name, rows in splits.items()}
        contract["dataset"] = {"handle": get_request()["dataset"], "sha256": dataset["sha256"]}
        state.put(connection, "contract", contract)
        state.put(connection, "splits", splits)
        _write(recurse.context().workspace / "resolved-contract.json", contract)
        _write(recurse.context().workspace / "splits.json", splits)
    return contract


def _execute(identifier: int, remaining: float) -> tuple[str, str, float]:
    """Run one isolated fit with a hard deadline and one native compute thread."""
    started = time.monotonic()
    environment = os.environ | {
        # Preserve dependency paths injected by the harness into the parent interpreter.
        "PYTHONPATH": os.pathsep.join(dict.fromkeys([str(Path(__file__).parent), *sys.path])),
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }
    try:
        result = subprocess.run(  # noqa: S603 - fixed module, integer ID, authority-owned paths
            [sys.executable, "-m", "modeler.worker", str(_root()), str(identifier)],
            cwd=Path(__file__).parent,
            env=environment,
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
        status = "trained" if result.returncode == 0 else "failed"
        diagnostic = result.stderr[-4000:]
    except subprocess.TimeoutExpired:
        status, diagnostic = (
            "timeout",
            "Training exceeded the remaining cumulative training budget.",
        )
    return status, diagnostic, time.monotonic() - started


def train_candidate(configuration: dict[str, ProposalValue], hypothesis: str) -> dict[str, Any]:
    """Fit a proposed candidate without revealing validation or final-test measurements.

    Args:
        configuration: family (baseline/linear/extra_trees; seasonal for forecasts), scale,
            regularization, trees, max_depth, min_samples_leaf, class_weight (null/balanced),
            threshold, multilabel strategy (independent/chain), forecast lags, seasonal_period,
            text max_features and ngram_max, and feature_subset (eligible tabular columns).
            Omitted values use documented defaults.
        hypothesis: Why this experiment should improve the frozen objective or feasibility.

    Returns:
        Candidate ID, normalized configuration, hypothesis, training duration, and fit status.
        Failed and timed-out attempts consume the budget and remain in history.
    """
    if not hypothesis.strip():
        raise contracts.ModelerError("Describe the hypothesis motivating this experiment.")
    with state.transaction(_root()) as connection:
        contract = state.require(connection, "contract")
        config = learning.validate_configuration(configuration, contract)
        history = state.trials(connection)
        budget = {"max_trials": 20, "max_training_seconds": 600} | get_request().get("budget", {})
        remaining = budget["max_training_seconds"] - sum(item["seconds"] for item in history)
        if len(history) >= budget["max_trials"] or remaining <= 0:
            raise contracts.ModelerError("Experiment budget exhausted; finalize the run.")
        if any(item["configuration"] == config for item in history):
            raise contracts.ModelerError(
                "This configuration was already attempted; inspect history."
            )
        identifier = len(history) + 1
        job = {
            "train": state.get(connection, "splits")["train"],
            "specification": contract,
            "configuration": config,
        }
        _write(_root() / f"job-{identifier}.json", job)
        status, diagnostic, seconds = _execute(identifier, remaining)
        trial = {
            "id": identifier,
            "configuration": config,
            "hypothesis": hypothesis,
            "status": status,
            "diagnostic": diagnostic,
            "seconds": seconds,
        }
        connection.execute("INSERT INTO trials VALUES (?, ?)", (identifier, json.dumps(trial)))
    return trial


def evaluate_candidate(candidate_id: int) -> dict[str, Any]:
    """Independently measure a trained candidate on the frozen validation observations.

    Args:
        candidate_id: Integer ID returned by train_candidate.

    Returns:
        Recorded validation metrics and feasibility, or the existing result for a repeat call.
    """
    with state.transaction(_root()) as connection:
        contract = state.require(connection, "contract")
        history = state.trials(connection)
        matches = [item for item in history if item["id"] == candidate_id]
        if not matches:
            raise contracts.ModelerError(f"Unknown candidate ID: {candidate_id}")
        trial = matches[0]
        if trial["status"] == "evaluated":
            return trial
        if trial["status"] != "trained":
            raise contracts.ModelerError("Only successfully trained candidates can be evaluated.")
        splits = state.get(connection, "splits")
        data = pd.read_parquet(_root() / "data.parquet")
        model = joblib.load(_root() / f"candidate-{candidate_id}.joblib")
        measurements = learning.measure(
            model,
            data.iloc[splits["train"]],
            data.iloc[splits["validation"]],
            _root() / f"candidate-{candidate_id}.joblib",
        )
        trial.update(
            status="evaluated",
            validation=measurements,
            feasible=contracts.feasible(contract, measurements),
        )
        connection.execute(
            "UPDATE trials SET value = ? WHERE id = ?", (json.dumps(trial), candidate_id)
        )
    return trial


def experiment_history() -> list[dict[str, Any]]:
    """Inspect complete experiment evidence to decide which hypotheses deserve follow-up.

    Returns:
        All trials, including failures, with authoritative measurements when evaluated.
    """
    with state.transaction(_root()) as connection:
        return state.trials(connection)


def _package(model_path: Path, contract: dict[str, Any]) -> None:
    """Bundle the fitted pipeline with importable prediction code and exact dependency versions."""
    versions = {
        name: version(name) for name in ["numpy", "pandas", "scikit-learn", "joblib", "scipy"]
    }
    workspace = recurse.context().workspace
    _write(workspace / "environment.json", {"python": sys.version, "packages": versions})
    with zipfile.ZipFile(workspace / "model-bundle.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(model_path, "model.joblib")
        archive.writestr(
            "requirements.txt", "\n".join(f"{name}=={value}" for name, value in versions.items())
        )
        archive.writestr("contract.json", json.dumps(contract, indent=2))
        for path in (Path(__file__).parent / "modeler").glob("*.py"):
            if path.name in {"__init__.py", "contracts.py", "learning.py"}:
                archive.write(path, f"modeler/{path.name}")
        archive.writestr(
            "predict.py",
            '"""Load this trusted model bundle and predict CSV rows/history."""\n'
            "import sys\nimport joblib\nimport pandas as pd\n"
            'model = joblib.load("model.joblib")\n'
            "print(model.predict(pd.read_csv(sys.argv[1])))\n",
        )


def _report(
    receipt: dict[str, Any],
    history: list[dict[str, Any]],
    review: dict[str, Any],
    evaluation: dict[str, Any],
) -> str:
    """Render prose from recorded evidence; keep the agent's rationale clearly attributed."""
    sections = [
        "# Predictive Modeler report",
        "## Task",
        review["summary"],
        "## Outcome",
        f"Status: {receipt['status']}. Stop reason: {receipt['stop_reason']}.",
        "## Agent rationale",
        receipt["summary"],
        "## Evaluation",
        "```json\n" + json.dumps(evaluation, indent=2) + "\n```",
        "## Measurement protocol (v3)",
        "model_bytes is the exact uncompressed model.joblib file size (joblib, pickle protocol 5), "
        "including preprocessing, configuration and every label/series estimator; excluding "
        "prediction code, dependency packages, reports and caller-supplied forecast history. "
        "input_feature_count counts distinct raw columns required by the saved prediction "
        "interface: selected tabular features, or forecast target history, time and series ID. "
        "It does not count encoded columns, lags, training rows or inferred feature importance. "
        "Dependency versions are pinned in the model bundle. "
        "Complexity metrics take no parameters.",
        "## Experiments",
    ]
    for item in history:
        sections += [
            f"### Trial {item['id']}: {item['status']}",
            item["hypothesis"],
            "```json\n" + json.dumps(item, indent=2) + "\n```",
        ]
    sections += [
        "## Limitations",
        "The winner is the best feasible evaluated candidate, not a global optimum. "
        "The saved pipeline retains its training-only fit; no untested refit is substituted. "
        "Validation guided selection; final-test measurements are reported once. "
        "Thresholds apply to positive-class probability (binary) or each label (multilabel). "
        "Forecast metrics average equally across series and forecast origins. "
        "Partitions, fitting, predictions and historical error scales "
        "use parsed chronological dates. "
        "Only load joblib artifacts you trust, using the bundled dependency versions.",
    ]
    return "\n\n".join(sections) + "\n"


def finish_run(stop_reason: str, explanation: str) -> dict[str, Any]:
    """Finalize once, select the measured winner, and return an authoritative artifact receipt.

    Args:
        stop_reason: budget_exhausted, diminishing_returns, inconsistent_inputs,
            needs_clarification, unsupported_task, or tool_error. Explain untried approaches
            for early stops. Tool failures do not establish that the task is unsupported.
        explanation: Evidence-backed selection rationale or actionable diagnostic. For tool_error,
            identify the failing tool, observed error, attempted correction, and unfinished work.

    Returns:
        Final status, stop reason, summary, conflicts/questions, and relative artifact paths.
        A failed final acceptance yields no_feasible_model and no accepted model bundle.
        Copy this entire receipt unchanged into the final JSON, including all conflicts and
        exactly the artifact keys returned. Do not paraphrase or add empty optional paths.
    """
    with state.transaction(_root()) as connection:
        previous = state.get(connection, "receipt")
        if previous:
            return cast(dict[str, Any], previous)
        review = state.require(connection, "review")
        history = state.trials(connection)
        receipt = _finalize(connection, review, history, stop_reason, explanation)
        state.put(connection, "receipt", receipt)
    # Keep only the receipt database for idempotence; intermediate fits/data are not deliverables.
    for path in _root().iterdir():
        if path.name != "state.sqlite":
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    return receipt


def _finalize(
    connection: Any,
    review: dict[str, Any],
    history: list[dict[str, Any]],
    reason: str,
    explanation: str,
) -> dict[str, Any]:
    """Enforce stop semantics and save results using only authority-owned measurements."""
    allowed = {
        "budget_exhausted",
        "diminishing_returns",
        "inconsistent_inputs",
        "needs_clarification",
        "unsupported_task",
        "tool_error",
    }
    if reason not in allowed or not explanation.strip():
        raise contracts.ModelerError("Supply a supported stop reason and substantive explanation.")
    if review["status"] != "aligned" and reason != review["status"]:
        raise contracts.ModelerError("Stop reason must match the rejected input review.")
    if reason == "inconsistent_inputs" and review["status"] == "aligned":
        raise contracts.ModelerError("No inconsistent-input finding was recorded.")
    status = (
        reason
        if reason
        in {"inconsistent_inputs", "needs_clarification", "unsupported_task", "tool_error"}
        else "no_feasible_model"
    )
    evaluation: dict[str, Any] = {}
    artifacts: dict[str, str] = {
        "report": "report.md",
        "review": "review.json",
        "trials": "trials.jsonl",
    }
    if reason in {"budget_exhausted", "diminishing_returns"}:
        contract = state.require(connection, "contract")
        if any(item["status"] == "trained" for item in history):
            raise contracts.ModelerError(
                "Evaluate every successfully trained candidate before finalizing."
            )
        budget = {"max_trials": 20, "max_training_seconds": 600} | get_request().get("budget", {})
        if (
            reason == "budget_exhausted"
            and len(history) < budget["max_trials"]
            and sum(item["seconds"] for item in history) < budget["max_training_seconds"]
        ):
            raise contracts.ModelerError("The budget is not exhausted.")
        if not history:
            raise contracts.ModelerError("Run a baseline before claiming diminishing returns.")
        candidates = [item for item in history if item.get("feasible")]
        if candidates:
            status, evaluation = _select(connection, contract, candidates)
            if status == "succeeded":
                artifacts["model"] = "model-bundle.zip"
        artifacts.update(
            resolved_contract="resolved-contract.json",
            evaluation="evaluation.json",
            splits="splits.json",
        )
    workspace = recurse.context().workspace
    _write(workspace / "review.json", review)
    _write(workspace / "evaluation.json", evaluation)
    (workspace / "trials.jsonl").write_text("".join(json.dumps(item) + "\n" for item in history))
    receipt = {
        "status": status,
        "stop_reason": reason,
        "summary": explanation,
        "questions": review["questions"],
        "conflicts": review["conflicts"],
        "artifacts": artifacts,
    }
    (workspace / "report.md").write_text(_report(receipt, history, review, evaluation))
    _write(workspace / "receipt.json", receipt)
    return receipt


def _select(
    connection: Any, contract: dict[str, Any], candidates: list[dict[str, Any]]
) -> tuple[str, Any]:
    """Select on validation and test exactly that winner, without test-driven retries."""
    objective = contract["quality"]["objective"]
    direction = -1 if objective["direction"] == "maximize" else 1
    winner = min(
        candidates, key=lambda item: direction * item["validation"][contracts.metric_key(objective)]
    )
    data = pd.read_parquet(_root() / "data.parquet")
    splits = state.get(connection, "splits")
    path = _root() / f"candidate-{winner['id']}.joblib"
    model = joblib.load(path)
    measurements = learning.measure(
        model, data.iloc[splits["train"] + splits["validation"]], data.iloc[splits["test"]], path
    )
    passed = contracts.feasible(contract, measurements)
    evaluation = {
        "selected_trial": winner["id"],
        "validation": winner["validation"],
        "final_test": measurements,
        "final_test_passed": passed,
    }
    if passed:
        _package(path, contract)
    return ("succeeded" if passed else "no_feasible_model"), evaluation


def _disagrees(prose: Any, structured: Any) -> bool:
    """Compare shared requirements while permitting compatible additional metric options."""
    if isinstance(prose, dict) and isinstance(structured, dict):
        return any(
            _disagrees(prose[key], structured[key]) for key in prose.keys() & structured.keys()
        )
    return bool(prose != structured)


def _merge(prose: dict[str, Any], structured: dict[str, Any]) -> dict[str, Any]:
    """Preserve nested prose requirements after the consistency check has passed."""
    result = prose | structured
    for key in prose.keys() & structured.keys():
        if isinstance(prose[key], dict) and isinstance(structured[key], dict):
            result[key] = _merge(prose[key], structured[key])
    return result
