"""Offline behavioral specifications for compact MNIST architecture search."""

import json
import math
import tempfile
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import torch
import yaml
from jsonschema import Draft202012Validator

import tools
from recurse import _activate as activate
from recurse import _deactivate as deactivate

_MANIFEST = Path(__file__).parents[1] / "agent.yaml"


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Activate defaults with balanced synthetic image fixtures and an isolated workspace."""
    manifest = yaml.safe_load(_MANIFEST.read_text())
    settings = {key: value["default"] for key, value in manifest["inputs"]["properties"].items()}
    settings.update(samples=100, cv_folds=2, max_trials=3, max_epochs=2, target_accuracy=0.95)
    activate(settings, tmp_path)
    generator = torch.Generator().manual_seed(12)
    images = torch.randint(0, 256, (100, 28, 28), generator=generator, dtype=torch.uint8)
    labels = torch.arange(10).repeat_interleave(10)
    monkeypatch.setattr(tools, "_data", lambda: (images, labels))
    previous = torch.get_num_threads()
    torch.set_num_threads(1)
    try:
        yield tmp_path
    finally:
        torch.set_num_threads(previous)
        deactivate()


@pytest.mark.parametrize(
    ("family", "normalization", "activation", "pooling"),
    [
        ("mlp", "none", "relu", "max"),
        ("mlp", "batch", "gelu", "max"),
        ("mlp", "layer", "relu", "max"),
        ("cnn", "none", "relu", "max"),
        ("cnn", "batch", "gelu", "average"),
        ("cnn", "group", "relu", "max"),
        ("separable", "group", "gelu", "average"),
        ("attention", "none", "relu", "max"),
        ("attention", "layer", "gelu", "max"),
    ],
)
def test_supported_blocks_train_score_and_save_replayable_weights(
    run: Path,
    family: Any,
    normalization: Any,
    activation: Any,
    pooling: Any,
) -> None:
    """Every block family produces ten-class logits and a reloadable measured checkpoint."""
    candidate = tools.design_network(
        family,
        (4,),
        epochs=1,
        batch_size=16,
        normalization=normalization,
        activation=activation,
        pooling=pooling,
    )
    trial = tools.evaluate_network(candidate)
    replay = tools.evaluate_network(candidate)
    assert trial == replay
    assert trial["status"] == "completed"
    assert trial["cv_accuracy"] == sum(trial["fold_accuracies"]) / 2
    assert trial["training_examples"] == 50
    model = tools._network(candidate)
    assert trial["parameter_count"] == sum(p.numel() for p in model.parameters())
    receipt = tools.finish_search("diminishing_returns")
    assert tools.finish_search("diminishing_returns") == receipt
    Draft202012Validator(yaml.safe_load(_MANIFEST.read_text())["outputs"]).validate(receipt)
    model.load_state_dict(torch.load(run / "best-model.pt", weights_only=True))
    model.eval()
    assert model(torch.zeros(2, 1, 28, 28)).shape == (2, 10)
    saved = json.loads((run / "best-model.json").read_text())
    assert saved["candidate"] == trial["candidate"]
    assert saved["checkpoint_fold"] == 1
    assert json.loads((run / "receipt.json").read_text()) == receipt
    with pytest.raises(ValueError, match="finalized"):
        tools.evaluate_network(candidate)


def test_folds_are_disjoint_stratified_reproducible_and_use_all_selected_examples() -> None:
    """Every selected observation is held out exactly once and splits do not depend on recipes."""
    labels = torch.arange(10).repeat_interleave(12)
    folds = tools._folds(labels, 3, 100, 7)
    assert [torch.bincount(labels[fold], minlength=10).tolist() for fold in folds] == [
        [4] * 10,
        [3] * 10,
        [3] * 10,
    ]
    assert len(torch.cat(folds).unique()) == 100
    assert all(
        torch.equal(a, b) for a, b in zip(folds, tools._folds(labels, 3, 100, 7), strict=True)
    )
    assert not torch.equal(folds[0], tools._folds(labels, 3, 100, 8)[0])
    assert len(torch.cat(tools._folds(labels, 3, 60000, 7))) == len(labels)
    with pytest.raises(ValueError, match="enough examples"):
        tools._folds(labels[:10], 3, 100, 7)
    with pytest.raises(ValueError, match="enough examples"):
        tools._folds(labels, 3, 10, 7)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"family": "rnn"}, "family"),
        ({"widths": ()}, "widths"),
        ({"widths": (0,)}, "widths"),
        ({"widths": (True,)}, "widths"),
        ({"widths": (257,)}, "widths"),
        ({"widths": (1, 2, 3, 4)}, "widths"),
        ({"learning_rate": math.nan}, "learning_rate"),
        ({"learning_rate": 0}, "learning_rate"),
        ({"weight_decay": math.inf}, "weight_decay"),
        ({"weight_decay": -1}, "weight_decay"),
        ({"epochs": 0}, "epochs"),
        ({"epochs": True}, "epochs"),
        ({"batch_size": 1}, "batch_size"),
        ({"batch_size": False}, "batch_size"),
        ({"normalization": "group"}, "normalization"),
        ({"activation": "sigmoid"}, "activation"),
        ({"pooling": "median"}, "pooling"),
        ({"family": "attention", "widths": (4, 4)}, "exactly one"),
    ],
)
def test_invalid_recipes_are_rejected(changes: dict[str, Any], message: str) -> None:
    """Reject incompatible blocks and unbounded numeric inputs before training."""
    candidate = tools.design_network("mlp", (4,))
    with pytest.raises(ValueError, match=message):
        tools._check(replace(candidate, **changes))


def test_training_is_reproducible_and_restores_random_state(run: Path) -> None:
    """Every fold begins from fresh weights without changing the caller's RNG state."""
    images, labels = tools._data()
    candidate = tools.design_network("mlp", (4, 3), epochs=1)
    indices = torch.arange(100)
    initial = torch.get_rng_state().clone()
    first = tools._train(candidate, images, labels, indices, 7, math.inf)
    second = tools._train(candidate, images, labels, indices, 7, math.inf)
    assert torch.equal(initial, torch.get_rng_state())
    assert all(
        torch.equal(a, b) for a, b in zip(first.parameters(), second.parameters(), strict=True)
    )


def test_nonfinite_training_loss_is_rejected(run: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Numerical failure cannot be counted as a completed accuracy measurement."""
    monkeypatch.setattr(torch.nn.functional, "cross_entropy", lambda *_: torch.tensor(math.nan))
    with pytest.raises(ValueError, match="non-finite"):
        tools.evaluate_network(tools.design_network("mlp", (4,), epochs=1))
    assert tools._ledger(run)[0]["status"] == "failed"


def test_smallest_feasible_wins_over_accuracy_and_infeasible_smaller_models(
    run: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Size matters only after target feasibility, and all candidates remain in the ledger."""
    scores = iter([0.96, 0.96, 0.99, 0.99, 0.94, 0.94])
    monkeypatch.setattr(tools, "_accuracy", lambda *_: next(scores))
    small = tools.evaluate_network(tools.design_network("mlp", (4,), epochs=1))
    tools.evaluate_network(tools.design_network("mlp", (8,), epochs=1))
    tools.evaluate_network(tools.design_network("mlp", (2,), epochs=1))
    receipt = tools.finish_search("trial_budget")
    assert receipt == {
        "target_reached": True,
        "cv_accuracy": 0.96,
        "parameter_count": small["parameter_count"],
        "trials_attempted": 3,
        "stop_reason": "trial_budget",
    }
    assert len(tools._ledger(run)) == 3


def test_ranking_breaks_ties_and_reports_no_feasible_candidate() -> None:
    """Accuracy breaks size ties, and diagnostic ranking favors accuracy before size."""
    trials = [
        {
            "status": "completed",
            "parameter_count": size,
            "cv_accuracy": score,
            "target_reached": feasible,
            "trial": index,
        }
        for index, (size, score, feasible) in enumerate(
            [(20, 0.96, True), (20, 0.97, True), (20, 0.97, True)], 1
        )
    ]
    assert tools._winner(trials) == trials[1]
    for trial in trials:
        trial["target_reached"] = False
    assert tools._winner(trials) == trials[1]
    trials[2]["parameter_count"] = 10
    assert tools._winner(trials) == trials[2]
    assert tools._winner([]) is None


def test_limits_are_enforced_and_cached_trials_do_not_consume_budget(run: Path) -> None:
    """The evaluator owns epoch/trial limits and verifies claimed stopping reasons."""
    with pytest.raises(ValueError, match="No attempted"):
        tools.finish_search("diminishing_returns")
    with pytest.raises(ValueError, match="reason must"):
        tools.finish_search("target_reached")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_epochs"):
        tools.evaluate_network(tools.design_network("mlp", (4,), epochs=3))
    for width in (2, 3, 4):
        tools.evaluate_network(tools.design_network("mlp", (width,), epochs=1))
        if width == 2:
            with pytest.raises(ValueError, match="Trial budget remains"):
                tools.finish_search("trial_budget")
            with pytest.raises(ValueError, match="Wall-clock budget remains"):
                tools.finish_search("wall_clock")
    assert tools.evaluate_network(tools.design_network("mlp", (2,), epochs=1))["trial"] == 1
    with pytest.raises(ValueError, match="max_trials exhausted"):
        tools.evaluate_network(tools.design_network("mlp", (5,), epochs=1))


def test_partial_cv_timeout_never_qualifies_and_previous_winner_survives(
    run: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deadline between folds preserves completed evidence and discards the partial score."""
    first = tools.evaluate_network(tools.design_network("mlp", (4,), epochs=1))
    monkeypatch.setattr(time, "monotonic", lambda: 1.0)
    tools._write(run / "search.json", {"deadline": 2.0})

    def expire(*_: Any) -> float:
        """Expire the run after the first held-out fold has been measured."""
        monkeypatch.setattr(time, "monotonic", lambda: 3.0)
        return 1.0

    monkeypatch.setattr(tools, "_accuracy", expire)
    timed_out = tools.evaluate_network(tools.design_network("mlp", (2,), epochs=1))
    assert timed_out["status"] == "timed_out"
    assert "cv_accuracy" not in timed_out
    assert tools.finish_search("wall_clock")["parameter_count"] == first["parameter_count"]


def test_timeout_without_completed_trials_returns_null_metrics(
    run: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhaustion during data loading returns an honest no-model receipt."""
    original = tools._data
    monkeypatch.setattr(time, "monotonic", lambda: 1.0)

    def delayed_data() -> tuple[torch.Tensor, torch.Tensor]:
        """Simulate data setup consuming the entire wall-clock allowance."""
        monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
        return original()

    monkeypatch.setattr(tools, "_data", delayed_data)
    candidate = tools.design_network("mlp", (4,), epochs=1)
    trial = tools.evaluate_network(candidate)
    assert trial["status"] == "timed_out"
    assert tools.evaluate_network(candidate) == trial
    with pytest.raises(TimeoutError, match="wall-clock"):
        tools.evaluate_network(replace(candidate, widths=(8,)))
    receipt = tools.finish_search("wall_clock")
    assert receipt == {
        "target_reached": False,
        "cv_accuracy": None,
        "parameter_count": None,
        "trials_attempted": 1,
        "stop_reason": "wall_clock",
    }
    assert not (run / "best-model.pt").exists()


def test_scoring_honors_deadline(run: Path) -> None:
    """Held-out scoring also stops rather than running beyond the search allowance."""
    images, labels = tools._data()
    with pytest.raises(TimeoutError):
        tools._accuracy(
            tools._network(tools.design_network("mlp", (4,))), images, labels, torch.arange(100), 0
        )


def test_download_uses_training_split_and_cache_outside_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loader never reads official test observations for architecture selection."""
    calls = []
    data, targets = torch.zeros(2, 28, 28, dtype=torch.uint8), torch.tensor([0, 1])

    def dataset(root: str, *, train: bool, download: bool) -> SimpleNamespace:
        """Record the real loader's requested dataset split."""
        calls.append((root, train, download))
        return SimpleNamespace(data=data, targets=targets)

    monkeypatch.setattr(tools, "MNIST", dataset)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    actual_data, actual_targets = tools._data()
    assert actual_data is data and actual_targets is targets
    assert calls == [(str(tmp_path / "recurse-mnist"), True, True)]


def test_manifest_defaults_match_requested_search_contract() -> None:
    """The runnable manifest defaults to 99% CV accuracy and a five-minute allowance."""
    manifest = yaml.safe_load(_MANIFEST.read_text())
    inputs = manifest["inputs"]
    defaults = {key: value["default"] for key, value in inputs["properties"].items()}
    Draft202012Validator(inputs).validate(defaults)
    assert defaults["target_accuracy"] == 0.99
    assert defaults["max_seconds"] == 300
    assert defaults["samples"] == 60000
    assert set(manifest["tools"]["register"]) == {
        "design_network",
        "evaluate_network",
        "finish_search",
    }


@pytest.mark.parametrize("method", ["stratified_kfold", "kfold", "stratified_holdout"])
def test_cv_methods_repeat_on_identical_samples_without_train_validation_overlap(
    method: str,
) -> None:
    """Every method preserves the selected cohort and yields disjoint, reproducible splits."""
    labels = torch.arange(10).repeat_interleave(20)
    settings = {
        "cv_method": method,
        "cv_folds": 3,
        "cv_repeats": 2,
        "cv_seed": 7,
        "samples": 100,
        "validation_fraction": 0.2,
    }
    splits = tools._splits(labels, settings)
    replay = tools._splits(labels, settings)
    assert len(splits) == (2 if method == "stratified_holdout" else 6)
    cohort = set(torch.cat(splits[0]).tolist())
    for (training, validation), (again_train, again_validation) in zip(splits, replay, strict=True):
        assert set(training.tolist()).isdisjoint(validation.tolist())
        assert set(torch.cat((training, validation)).tolist()) == cohort
        assert torch.equal(training, again_train) and torch.equal(validation, again_validation)
        assert len(cohort) == 100
        if method == "stratified_holdout":
            assert torch.bincount(labels[validation]).tolist() == [2] * 10
    if method != "stratified_holdout":
        assert len(torch.cat([validation for _, validation in splits[:3]]).unique()) == 100
    assert not torch.equal(splits[0][1], splits[len(splits) // 2][1])


def test_default_cv_preserves_baseline_folds() -> None:
    """Parameterization does not silently change the baseline evaluation partitions."""
    labels = torch.arange(10).repeat_interleave(20)
    settings = {
        "cv_method": "stratified_kfold",
        "cv_folds": 3,
        "cv_repeats": 1,
        "cv_seed": 7,
        "samples": 100,
        "validation_fraction": 0.2,
    }
    assert all(
        torch.equal(old, new)
        for old, (_, new) in zip(
            tools._folds(labels, 3, 100, 7), tools._splits(labels, settings), strict=True
        )
    )


def test_holdout_ignores_kfold_count() -> None:
    """Changing an irrelevant K-fold parameter cannot change holdout membership."""
    labels = torch.arange(10).repeat_interleave(20)
    settings = {
        "cv_method": "stratified_holdout",
        "cv_folds": 2,
        "cv_repeats": 2,
        "cv_seed": 7,
        "samples": 100,
        "validation_fraction": 0.2,
    }
    first = tools._splits(labels, settings)
    settings["cv_folds"] = 5
    second = tools._splits(labels, settings)
    assert all(
        torch.equal(a, b)
        for split_a, split_b in zip(first, second, strict=True)
        for a, b in zip(split_a, split_b, strict=True)
    )
