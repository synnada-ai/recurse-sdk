"""The agent's tools enforce consistency, budgets, measured selection, and receipts."""

import inspect
import json
import runpy
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TypeAliasType, cast, get_args, get_type_hints, is_typeddict

import joblib
import pandas as pd
import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError
from modeler import state, worker
from modeler.contracts import ModelerError

from recurse import _activate as activate
from recurse import _deactivate as deactivate

ROOT = Path(__file__).parents[1]


def test_final_response_rejects_empty_artifact_placeholders(run: Path, tools: Any) -> None:
    """A rejected run must omit unavailable artifacts rather than invent empty paths."""
    tools.review_inputs("Conflicting objectives.", {}, ["Choose precision or recall."], [])
    receipt = tools.finish_run("inconsistent_inputs", "Resolve the objective conflict.")
    manifest = yaml.safe_load((ROOT / "agent.yaml").read_text())
    validator = Draft202012Validator(manifest["outputs"])
    validator.validate(receipt)
    for name in manifest["outputs"]["properties"]["artifacts"]["properties"]:
        invalid = receipt | {"artifacts": receipt["artifacts"] | {name: ""}}
        with pytest.raises(ValidationError):
            validator.validate(invalid)


def test_agent_inputs_never_require_opaque_any_values(tools: Any) -> None:
    """Every agent-authored value has a concrete annotation, including nested dictionary values."""

    def check(annotation: Any) -> None:
        """Follow aliases and record fields to detect an opaque leaf."""
        assert annotation is not Any, "Any becomes a mandatory storage reference in strict mode"
        if isinstance(annotation, TypeAliasType):
            check(annotation.__value__)
        elif is_typeddict(annotation):
            for value in get_type_hints(annotation, include_extras=True).values():
                check(value)
        else:
            for value in get_args(annotation):
                check(value)

    manifest = yaml.safe_load((ROOT / "agent.yaml").read_text())
    for name in manifest["tools"]["register"]:
        function = getattr(tools, name)
        hints = get_type_hints(function)
        for parameter in inspect.signature(function).parameters:
            check(hints[parameter])


@pytest.fixture
def run(
    tmp_path: Path, tools: Any, frame: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Activate a run with isolated state and a local table instead of a network download."""
    activate(
        {"dataset": "example:unit-test", "budget": {"max_trials": 2, "max_training_seconds": 60}},
        tmp_path,
    )
    monkeypatch.setattr(tools.datasets, "load_dataset", lambda handle, cache: frame.copy())
    yield tmp_path
    deactivate()


def _ready(tools: Any) -> dict[str, Any]:
    """Complete the same required gate and resolution path used by the agent."""
    tools.review_inputs("Predict the binary y target using x and category.", {}, [], [])
    tools.inspect_dataset()
    return cast(
        dict[str, Any],
        tools.resolve_problem({"kind": "binary", "targets": ["y"], "features": ["x", "category"]}),
    )


def _inline(tools: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fit local fixture candidates directly while preserving the worker job boundary."""

    def execute(identifier: int, remaining: float) -> tuple[str, str, float]:
        """Use the real fitting worker with a deterministic measured duration."""
        worker.run(tools._root(), identifier)
        return "trained", "", 0.1

    monkeypatch.setattr(tools, "_execute", execute)


def test_complete_run_saves_best_measured_pipeline(
    run: Path, tools: Any, monkeypatch: pytest.MonkeyPatch, frame: pd.DataFrame
) -> None:
    """A regression in the last trial never replaces the better validated candidate."""
    contract = _ready(tools)
    (run / ".modeler" / "downloads").mkdir()
    _inline(tools, monkeypatch)
    for family in ["linear", "baseline"]:
        trial = tools.train_candidate({"family": family}, f"Measure {family} behavior.")
        tools.evaluate_candidate(trial["id"])
    receipt = tools.finish_run(
        "budget_exhausted", "Linear features separated the target; baseline regressed."
    )
    assert receipt["status"] == "succeeded"
    assert receipt == tools.finish_run("diminishing_returns", "Ignored repeat finalization.")
    manifest = yaml.safe_load((ROOT / "agent.yaml").read_text())
    Draft202012Validator(manifest["outputs"]).validate(receipt)
    evaluation = json.loads((run / "evaluation.json").read_text())
    assert evaluation == {
        "selected_trial": 1,
        "validation": {'f1:{"average": "binary", "positive_label": "1"}': 1.0},
        "final_test": {'f1:{"average": "binary", "positive_label": "1"}': 1.0},
        "final_test_passed": True,
    }
    assert json.loads((run / "resolved-contract.json").read_text()) == contract
    assert len(tools.experiment_history()) == 2
    assert sorted(path.name for path in (run / ".modeler").iterdir()) == ["state.sqlite"]

    with zipfile.ZipFile(run / "model-bundle.zip") as archive:
        archive.extractall(run / "bundle")
    model = joblib.load(run / "bundle/model.joblib")
    assert model.predict(frame).tolist() == frame["y"].tolist()
    assert "Linear features" in (run / "report.md").read_text()
    with pytest.raises(ModelerError, match="finalized"):
        tools.train_candidate({}, "Should not fit after finalization.")


@pytest.mark.parametrize(
    ("conflicts", "questions", "outcome"),
    [
        (["The requested outcome disagrees."], [], "inconsistent_inputs"),
        ([], ["Which target column should be predicted?"], "needs_clarification"),
    ],
)
def test_rejected_review_prevents_dataset_access(
    run: Path, tools: Any, conflicts: list[str], questions: list[str], outcome: str
) -> None:
    """Input contradictions and ambiguities stop before spending a training budget."""
    review = tools.review_inputs("A task with unresolved semantics.", {}, conflicts, questions)
    assert review["status"] == outcome
    with pytest.raises(ModelerError, match="review"):
        tools.inspect_dataset()
    with pytest.raises(ModelerError, match="must match"):
        tools.finish_run("unsupported_task", "Wrong reason.")
    result = tools.finish_run(outcome, "Correct the inputs before retrying.")
    assert result["status"] == outcome
    assert result["questions"] == questions
    assert result["conflicts"] == conflicts
    assert tools.experiment_history() == []
    assert json.loads((run / result["artifacts"]["review"]).read_text()) == review


@pytest.mark.parametrize("stage", ["review", "dataset", "trained", "evaluated"])
def test_tool_failure_preserves_evidence_without_claiming_a_model(
    run: Path, tools: Any, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """An operational blocker retains its diagnostic and trials without claiming unsupported ML."""
    review = tools.review_inputs("Predict y.", {}, [], [])
    if stage != "review":
        tools.inspect_dataset()
    if stage in {"trained", "evaluated"}:
        tools.resolve_problem({"kind": "binary", "targets": ["y"], "features": ["x"]})
        _inline(tools, monkeypatch)
        trial = tools.train_candidate({"family": "linear"}, "Measure separability.")
        if stage == "evaluated":
            tools.evaluate_candidate(trial["id"])
    history = tools.experiment_history()
    explanation = (
        "Resolver argument validation blocked progress after correcting the specification."
    )
    receipt = tools.finish_run("tool_error", explanation)
    assert receipt == {
        "status": "tool_error",
        "stop_reason": "tool_error",
        "summary": explanation,
        "questions": [],
        "conflicts": [],
        "artifacts": {"report": "report.md", "review": "review.json", "trials": "trials.jsonl"},
    }
    manifest = yaml.safe_load((ROOT / "agent.yaml").read_text())
    Draft202012Validator(manifest["outputs"]).validate(receipt)
    assert tools.experiment_history() == history
    assert json.loads((run / "review.json").read_text()) == review
    assert json.loads((run / "evaluation.json").read_text()) == {}
    assert not (run / "model-bundle.zip").exists()
    assert explanation in (run / "report.md").read_text()
    assert receipt == tools.finish_run("tool_error", "Idempotent repeat.")


def test_review_compares_independent_prose_requirements(run: Path, tools: Any) -> None:
    """Objective and threshold contradictions are recorded by the consistency gate."""
    deactivate()
    activate(
        {
            "dataset": "unused",
            "quality": {
                "objective": {"metric": "precision", "direction": "maximize"},
                "constraints": [{"metric": "recall", "operator": ">=", "value": 0.8}],
            },
        },
        run,
    )
    result = tools.review_inputs(
        "Maximize recall with recall >= .9.",
        {
            "objective": {"metric": "recall"},
            "constraints": [{"metric": "recall", "operator": ">=", "value": 0.9}],
        },
        [],
        [],
    )
    assert result["status"] == "inconsistent_inputs"
    assert len(result["conflicts"]) == 2


@pytest.mark.parametrize(
    "quality",
    [
        {"metric": "rmse", "direction": "minimize"},
        {"objective": "mae"},
        {"objective": {"metrics": "mae"}},
        {"constraints": {}},
        {"constraints": ["mae"]},
        {"constraints": [{}]},
        {"constraints": [{"metric": "recall", "operator": ">=", "value": 0.5, "extra": 1}]},
    ],
)
def test_malformed_prose_quality_can_be_corrected_before_review_is_frozen(
    run: Path, tools: Any, quality: dict[str, Any]
) -> None:
    """Malformed extractions get an actionable error rather than an immutable accepted review."""
    with pytest.raises(ModelerError, match="Extract these from the prose"):
        tools.review_inputs("Minimize MAE.", quality, [], [])
    with state.transaction(run / ".modeler") as connection:
        assert state.get(connection, "review") is None
    review = tools.review_inputs("Minimize MAE.", {"objective": {"metric": "mae"}}, [], [])
    assert review["status"] == "aligned"


def test_gate_is_immutable_and_prerequisites_are_enforced(run: Path, tools: Any) -> None:
    """Training cannot bypass or revise its input review and frozen contract."""
    with pytest.raises(ModelerError, match="review"):
        tools.inspect_dataset()
    with pytest.raises(ModelerError, match="interpretation"):
        tools.review_inputs("", {}, [], [])
    result = tools.review_inputs("Predict y.", {}, [], [])
    assert tools.review_inputs("Predict y.", {}, [], []) == result
    with pytest.raises(ModelerError, match="immutable"):
        tools.review_inputs("Predict something else.", {}, [], [])
    with pytest.raises(ModelerError, match="dataset"):
        tools.resolve_problem({})
    profile = tools.inspect_dataset()
    assert profile == tools.inspect_dataset()
    with pytest.raises(ModelerError, match="contract"):
        tools.train_candidate({}, "Baseline.")
    tools.resolve_problem({"kind": "binary", "targets": ["y"], "features": ["x"]})
    with pytest.raises(ModelerError, match="frozen"):
        tools.resolve_problem({})


def test_prose_constraints_survive_structured_additions(run: Path, tools: Any) -> None:
    """A missing structured objective does not discard an explicit prose requirement."""
    tools.review_inputs(
        "Minimize MAE, require MAE <= 10.",
        {
            "objective": {"metric": "mae", "direction": "minimize"},
            "constraints": [{"metric": "mae", "operator": "<=", "value": 10}],
        },
        [],
        [],
    )
    tools.inspect_dataset()
    contract = tools.resolve_problem(
        {"kind": "regression", "targets": ["value"], "features": ["x"]}
    )
    assert contract["quality"] == {
        "objective": {"metric": "mae", "direction": "minimize", "parameters": {}},
        "constraints": [{"metric": "mae", "operator": "<=", "value": 10, "parameters": {}}],
    }


def test_duplicate_trials_and_budget_overruns_fail(
    run: Path, tools: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated configurations do not consume extra trials and budget limits are authoritative."""
    _ready(tools)
    _inline(tools, monkeypatch)
    with pytest.raises(ModelerError, match="hypothesis"):
        tools.train_candidate({}, "")
    trial = tools.train_candidate({}, "Fit a linear candidate.")
    with pytest.raises(ModelerError, match="already attempted"):
        tools.train_candidate({}, "Repeat.")
    with pytest.raises(ModelerError, match="Unknown candidate"):
        tools.evaluate_candidate(999)
    with pytest.raises(ModelerError, match="Evaluate every"):
        tools.finish_run("diminishing_returns", "Premature.")
    measured = tools.evaluate_candidate(trial["id"])
    assert tools.evaluate_candidate(trial["id"]) == measured
    tools.train_candidate({"family": "baseline"}, "Baseline.")
    with pytest.raises(ModelerError, match="budget exhausted"):
        tools.train_candidate({"family": "extra_trees"}, "Budget overrun.")


def test_failed_fits_remain_in_history(
    run: Path, tools: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout consumes elapsed time and cannot be scored as a trained candidate."""
    _ready(tools)
    monkeypatch.setattr(
        tools, "_execute", lambda identifier, remaining: ("timeout", "Too slow.", 61.0)
    )
    trial = tools.train_candidate({}, "Try a candidate.")
    assert trial["status"] == "timeout"
    with pytest.raises(ModelerError, match="Only successfully"):
        tools.evaluate_candidate(trial["id"])
    with pytest.raises(ModelerError, match="budget exhausted"):
        tools.train_candidate({"family": "baseline"}, "No time remains.")
    result = tools.finish_run("budget_exhausted", "No model completed within the training budget.")
    assert result["status"] == "no_feasible_model"
    assert "model" not in result["artifacts"]


@pytest.mark.parametrize("final_failure", [False, True])
def test_unmet_constraints_never_return_an_accepted_model(
    run: Path, tools: Any, monkeypatch: pytest.MonkeyPatch, final_failure: bool
) -> None:
    """Both validation infeasibility and final-test failure retain truthful evidence."""
    _ready(tools)
    _inline(tools, monkeypatch)
    tools.train_candidate({}, "Linear candidate.")
    if not final_failure:
        monkeypatch.setattr(tools.contracts, "feasible", lambda spec, metrics: False)
    tools.evaluate_candidate(1)
    monkeypatch.setattr(tools.contracts, "feasible", lambda spec, metrics: False)
    receipt = tools.finish_run("diminishing_returns", "No feasible candidate established.")
    assert receipt["status"] == "no_feasible_model"
    assert not (run / "model-bundle.zip").exists()


def test_finish_requires_supported_evidence_backed_reason(run: Path, tools: Any) -> None:
    """A task cannot claim exhausted budget or convergence before experiments."""
    _ready(tools)
    for reason, message in [
        ("bad", "supported stop"),
        ("inconsistent_inputs", "No inconsistent"),
        ("budget_exhausted", "not exhausted"),
        ("diminishing_returns", "baseline"),
    ]:
        with pytest.raises(ModelerError, match=message):
            tools.finish_run(reason, "Test explanation.")
    assert (
        tools.finish_run("unsupported_task", "The requested extension is unsupported.")["status"]
        == "unsupported_task"
    )


@pytest.mark.parametrize("outcome", ["trained", "failed", "timeout"])
def test_training_subprocess_enforces_timeout(
    run: Path, tools: Any, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Subprocess completion, errors, and timeouts return bounded diagnostic evidence."""

    def execute(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Return the process outcome without launching a slow model."""
        assert kwargs["timeout"] == 2.0
        if outcome == "timeout":
            raise subprocess.TimeoutExpired("worker", 2)
        return subprocess.CompletedProcess([], 0 if outcome == "trained" else 1, "", "diagnostic")

    monkeypatch.setattr(tools.subprocess, "run", execute)
    status, diagnostic, seconds = tools._execute(1, 2.0)
    assert status == outcome
    assert diagnostic
    assert seconds >= 0


def test_training_worker_inherits_parent_application_dependencies(
    run: Path, tools: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runtime-injected dependency path survives launching a fresh Python interpreter."""
    _ready(tools)
    execute = subprocess.run
    monkeypatch.delenv("PYTHONPATH", raising=False)

    def without_site_packages(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        """Disable automatic site setup to reproduce a harness-injected import environment."""
        return execute([command[0], "-S", *command[1:]], **kwargs)

    monkeypatch.setattr(tools.subprocess, "run", without_site_packages)
    trial = tools.train_candidate({"family": "linear"}, "Measure linear separability.")
    assert trial["status"] == "trained", trial["diagnostic"]
    assert tools.evaluate_candidate(trial["id"])["status"] == "evaluated"


def test_worker_module_runs_the_recorded_job(
    run: Path, tools: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The standalone worker entrypoint loads only its internal fitting specification."""
    _ready(tools)
    _inline(tools, monkeypatch)
    tools.train_candidate({}, "Prepare a worker job.")
    monkeypatch.setattr(sys, "argv", ["worker", str(tools._root()), "1"])
    monkeypatch.delitem(sys.modules, "modeler.worker")
    runpy.run_module("modeler.worker", run_name="__main__")
    assert (tools._root() / "candidate-1.joblib").is_file()


def test_state_transactions_roll_back_on_failure(run: Path) -> None:
    """Partial state updates cannot escape an exception."""
    with pytest.raises(ModelerError), state.transaction(run) as connection:
        state.put(connection, "temporary", 1)
        raise ModelerError("rollback")
    with state.transaction(run) as connection:
        assert state.get(connection, "temporary") is None


def test_matching_prose_constraint_is_not_duplicated(run: Path, tools: Any) -> None:
    """Identical requirements from both sources become one enforced constraint."""
    quality = {"constraints": [{"metric": "recall", "operator": ">=", "value": 0.5}]}
    deactivate()
    activate({"dataset": "fixture", "quality": quality}, run)
    tools.review_inputs("Predict y with recall >= .5.", quality, [], [])
    tools.inspect_dataset()
    contract = tools.resolve_problem({"kind": "binary", "targets": ["y"], "features": ["x"]})
    assert len(contract["quality"]["constraints"]) == 1


def test_additional_metric_options_are_compatible_and_prose_options_survive(
    run: Path, tools: Any
) -> None:
    """Structured averaging can augment prose without dropping the prose's positive label."""
    deactivate()
    activate(
        {
            "dataset": "fixture",
            "quality": {
                "objective": {
                    "metric": "recall",
                    "direction": "maximize",
                    "parameters": {"average": "binary"},
                }
            },
        },
        run,
    )
    review = tools.review_inputs(
        "Maximize recall for label 0.",
        {
            "objective": {
                "metric": "recall",
                "direction": "maximize",
                "parameters": {"positive_label": "0"},
            }
        },
        [],
        [],
    )
    assert review["status"] == "aligned"
    tools.inspect_dataset()
    contract = tools.resolve_problem({"kind": "binary", "targets": ["y"], "features": ["category"]})
    assert contract["quality"]["objective"]["parameters"] == {
        "positive_label": "0",
        "average": "binary",
    }


@pytest.mark.parametrize(
    "name", ["binary", "multiclass", "multilabel", "regression", "forecast", "panel-forecast"]
)
def test_each_real_task_has_a_rejected_contradictory_request(
    run: Path, tools: Any, name: str
) -> None:
    """Each real-data task has a concrete mismatch case for future cloud-agent validation."""
    original = json.loads((ROOT / "inputs" / f"{name}.json").read_text())
    inconsistent = json.loads((ROOT / "tests/inconsistent-inputs" / f"{name}.json").read_text())
    schema = yaml.safe_load((ROOT / "agent.yaml").read_text())
    Draft202012Validator(schema["inputs"]).validate(inconsistent)
    assert inconsistent["dataset"] == original["dataset"]
    assert inconsistent["task"] == original["task"]
    deactivate()
    activate({key: value for key, value in inconsistent.items() if key != "task"}, run)
    review = tools.review_inputs(original["task"], original["quality"], [], [])
    assert review["status"] == "inconsistent_inputs"
    receipt = tools.finish_run("inconsistent_inputs", "The task and quality requirements disagree.")
    assert receipt["status"] == "inconsistent_inputs"
    assert not (run / "resolved-contract.json").exists()
    assert tools.experiment_history() == []
