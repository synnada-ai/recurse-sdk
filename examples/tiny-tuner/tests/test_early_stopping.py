"""Specifications for inner-only epoch selection and independent cosine horizons."""

import json
import math
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import torch
import yaml
from jsonschema import Draft202012Validator
from torch import Tensor, nn

import tools
from recurse import _activate as activate
from recurse import _deactivate as deactivate


@pytest.fixture
def early_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Activate a small balanced run with inner stopping and fresh temporary evidence."""
    manifest = yaml.safe_load((Path(__file__).parents[1] / "agent.yaml").read_text())
    settings = {name: field["default"] for name, field in manifest["inputs"]["properties"].items()}
    settings.update(
        samples=100,
        cv_folds=2,
        max_epochs=8,
        stopping_method="early_stopping",
        stopping_validation_fraction=0.2,
    )
    activate(settings, tmp_path)
    generator = torch.Generator().manual_seed(12)
    images = torch.randint(0, 256, (100, 28, 28), generator=generator, dtype=torch.uint8)
    labels = torch.arange(10).repeat_interleave(10)
    monkeypatch.setattr(tools, "_data", lambda: (images, labels))
    try:
        with tools._cpu():
            yield tmp_path
    finally:
        deactivate()


def test_inner_split_is_stratified_reproducible_and_ignores_outer_observations() -> None:
    """Private randomness selects only supplied training indices, preserving both classes."""
    labels = torch.arange(10).repeat_interleave(10)
    indices = torch.arange(0, 100, 2)
    state = torch.get_rng_state().clone()
    training, validation = tools._inner_split(labels, indices, 0.2, 7)
    assert torch.equal(torch.get_rng_state(), state)
    assert torch.bincount(labels[training]).tolist() == [4] * 10
    assert torch.bincount(labels[validation]).tolist() == [1] * 10
    assert set(training.tolist()).isdisjoint(validation.tolist())
    assert set(torch.cat((training, validation)).tolist()) == set(indices.tolist())
    changed = labels.clone()
    changed[1::2] = -1
    replay = tools._inner_split(changed, indices, 0.2, 7)
    assert all(
        torch.equal(first, second)
        for first, second in zip((training, validation), replay, strict=True)
    )
    assert not torch.equal(validation, tools._inner_split(labels, indices, 0.2, 8)[1])


@pytest.mark.parametrize("class_size", [0, 1])
def test_inner_split_rejects_classes_without_two_training_examples(class_size: int) -> None:
    """A missing or singleton class cannot silently leak or disappear from inner validation."""
    labels = torch.cat((torch.zeros(class_size, dtype=torch.long), torch.arange(1, 10).repeat(2)))
    with pytest.raises(ValueError, match="class 0; increase samples"):
        tools._inner_split(labels, torch.arange(len(labels)), 0.1, 7)


@pytest.mark.parametrize(
    ("losses", "patience", "min_epochs", "min_delta", "best_epoch", "reason"),
    [
        ([1.0, 0.8, 0.79, 0.81], 2, 1, 0.1, 3, "patience"),
        ([1.0] * 5, 1, 5, 0.01, 1, "patience"),
        ([1.0, 0.9, 0.8], 3, 1, 0.01, 3, "epoch_ceiling"),
    ],
)
def test_best_loss_restore_is_independent_of_significant_improvement_and_minimum_epochs(  # noqa: PLR0913, PLR0917 - independent stopping controls and expectations
    early_run: Path,
    monkeypatch: pytest.MonkeyPatch,
    losses: list[float],
    patience: int,
    min_epochs: int,
    min_delta: float,
    best_epoch: int,
    reason: str,
) -> None:
    """Restore actual minimum-loss weights and BN buffers even for sub-delta improvements."""
    images, labels = tools._data()
    snapshots: list[dict[str, Tensor]] = []
    original_validation = tools._validation

    def validation(model: nn.Module, *args: Any) -> tuple[float, float]:
        """Score normally, asserting held-out inference leaves normalization statistics alone."""
        before = {key: value.clone() for key, value in model.state_dict().items()}
        _, accuracy = original_validation(model, *args)
        assert not model.training
        assert all(torch.equal(value, model.state_dict()[key]) for key, value in before.items())
        snapshots.append(before)
        return losses[len(snapshots) - 1], accuracy

    monkeypatch.setattr(tools, "_validation", validation)
    candidate = tools.design_network(
        "cnn", (4,), normalization="batch", epochs=len(losses), batch_size=16
    )
    diagnostics: dict[str, Any] = {}
    model = tools._train_early(
        candidate,
        images,
        labels,
        torch.arange(100),
        7,
        math.inf,
        {"patience": patience, "min_epochs": min_epochs, "min_delta": min_delta},
        7,
        diagnostics,
    )
    assert diagnostics["actual_epochs"] == len(losses)
    assert diagnostics["best_epoch"] == best_epoch
    assert diagnostics["stop_reason"] == reason
    assert diagnostics["training_examples"] == 90
    assert diagnostics["inner_validation_examples"] == 10
    assert [entry["inner_validation_loss"] for entry in diagnostics["learning_curve"]] == losses
    assert all(entry["training_loss"] > 0 for entry in diagnostics["learning_curve"])
    assert all(
        torch.equal(value, snapshots[best_epoch - 1][key])
        for key, value in model.state_dict().items()
    )
    tracked = [
        value for key, value in model.state_dict().items() if key.endswith("num_batches_tracked")
    ]
    assert all(int(value) == best_epoch * 6 for value in tracked)


def test_early_stopping_scores_each_outer_fold_once_and_records_reduced_fit_count(
    early_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outer validation is unseen during epoch selection; checkpoint metadata counts fit data."""
    original_early, original_accuracy = tools._train_early, tools._accuracy
    outer_training: list[set[int]] = []
    split_seeds: list[int] = []

    def train(*args: Any) -> nn.Module:
        """Capture outer training ownership and independent inner split seeds."""
        outer_training.append(set(args[3].tolist()))
        split_seeds.append(args[7])
        return original_early(*args)

    def accuracy(
        model: nn.Module, images: Tensor, labels: Tensor, indices: Tensor, deadline: float
    ) -> float:
        """Assert outer scoring receives only the disjoint held-out partition."""
        assert outer_training[-1].isdisjoint(indices.tolist())
        return original_accuracy(model, images, labels, indices, deadline)

    monkeypatch.setattr(tools, "_train_early", train)
    monkeypatch.setattr(tools, "_accuracy", accuracy)
    trial = tools.evaluate_network(tools.design_network("mlp", (4,), epochs=2))
    assert trial["status"] == "completed"
    assert split_seeds == [7, 8]
    assert len(trial["fold_accuracies"]) == len(outer_training) == 2
    assert trial["training_examples"] == 40
    assert all(
        fold["training_examples"] == 40 and fold["inner_validation_examples"] == 10
        for fold in trial["fold_training"]
    )
    assert all(fold["actual_epochs"] == 2 for fold in trial["fold_training"])
    tools.finish_search("diminishing_returns")
    metadata = json.loads((early_run / "best-model.json").read_text())
    assert metadata["protocol_version"] == 4
    assert metadata["training_examples"] == 40
    assert json.loads((early_run / "search.json").read_text())["protocol_version"] == 4


def test_early_training_replays_and_restores_caller_random_state(early_run: Path) -> None:
    """Fresh runs with the same inner and training seeds reproduce curves and checkpoints."""
    images, labels = tools._data()
    candidate = tools.design_network("mlp", (4,), epochs=2, schedule="cosine")
    state = torch.get_rng_state().clone()
    first_info: dict[str, Any] = {}
    second_info: dict[str, Any] = {}
    first = tools._train_early(
        candidate, images, labels, torch.arange(100), 7, math.inf, {}, 9, first_info
    )
    second = tools._train_early(
        candidate, images, labels, torch.arange(100), 7, math.inf, {}, 9, second_info
    )
    assert first_info == second_info
    assert torch.equal(torch.get_rng_state(), state)
    assert all(
        torch.equal(value, second.state_dict()[key]) for key, value in first.state_dict().items()
    )


def test_inner_scoring_timeout_preserves_curve_without_partial_cv_qualification(
    early_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed inner epoch survives timeout as diagnostics, never as target evidence."""
    original_validation = tools._validation
    monkeypatch.setattr(time, "monotonic", lambda: 1.0)

    def expire(*args: Any) -> tuple[float, float]:
        """Expire after inner scoring so the cooperative post-check rejects the trial."""
        result = original_validation(*args)
        monkeypatch.setattr(time, "monotonic", lambda: 302.0)
        return result

    monkeypatch.setattr(tools, "_validation", expire)
    trial = tools.evaluate_network(tools.design_network("mlp", (4,), epochs=2))
    assert trial["status"] == "timed_out"
    assert "cv_accuracy" not in trial and "fold_accuracies" not in trial
    fold = trial["fold_training"][0]
    assert fold["actual_epochs"] == 1
    assert fold["stop_reason"] == "wall_clock"
    assert len(fold["learning_curve"]) == 1
    assert json.loads((early_run / "trials.json").read_text()) == [trial]
    assert tools.finish_search("wall_clock")["cv_accuracy"] is None


def test_inner_validation_rejects_nonfinite_loss_and_checks_final_deadline(
    early_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid loss and a late last batch cannot produce a selectable inner checkpoint."""
    images, labels = tools._data()
    model = tools._network(tools.design_network("mlp", (4,)))
    monkeypatch.setattr(
        nn.functional, "cross_entropy", lambda *args, **kwargs: torch.tensor(math.nan)
    )
    with pytest.raises(ValueError, match="Inner validation loss is non-finite"):
        tools._validation(model, images, labels, torch.arange(100), math.inf)
    monkeypatch.setattr(time, "monotonic", lambda: 1.0)

    def delayed_loss(*args: Any, **kwargs: Any) -> Tensor:
        """Make the final scoring operation finish beyond the deadline."""
        monkeypatch.setattr(time, "monotonic", lambda: 3.0)
        return torch.tensor(1.0)

    monkeypatch.setattr(nn.functional, "cross_entropy", delayed_loss)
    with pytest.raises(TimeoutError):
        tools._validation(model, images, labels, torch.arange(100), 2.0)


def test_unfinished_inner_scoring_retains_completed_training_epoch(
    early_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Training that finished before scoring timed out is counted with null validation metrics."""

    def timeout(*args: Any) -> tuple[float, float]:
        """Simulate inner scoring failing to complete before the allowance expires."""
        raise TimeoutError("inner scoring exceeded deadline")

    monkeypatch.setattr(tools, "_validation", timeout)
    trial = tools.evaluate_network(tools.design_network("mlp", (4,), epochs=2))
    assert trial["status"] == "timed_out"
    fold = trial["fold_training"][0]
    assert fold["actual_epochs"] == 1
    assert fold["best_epoch"] is None
    assert fold["stop_reason"] == "wall_clock"
    assert fold["learning_curve"][0]["inner_validation_loss"] is None
    assert fold["learning_curve"][0]["inner_validation_accuracy"] is None
    assert fold["learning_curve"][0]["training_loss"] > 0


def test_checkpoint_restore_crossing_deadline_cannot_qualify(
    early_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restoring an otherwise valid inner winner remains inside the cooperative allowance."""
    original_load = nn.Module.load_state_dict
    monkeypatch.setattr(time, "monotonic", lambda: 1.0)

    def delayed_restore(model: nn.Module, *args: Any, **kwargs: Any) -> Any:
        """Complete restoration late to exercise its post-operation deadline check."""
        result = original_load(model, *args, **kwargs)
        monkeypatch.setattr(time, "monotonic", lambda: 302.0)
        return result

    monkeypatch.setattr(nn.Module, "load_state_dict", delayed_restore)
    trial = tools.evaluate_network(tools.design_network("mlp", (4,), epochs=1))
    assert trial["status"] == "timed_out"
    assert trial["fold_training"][0]["best_epoch"] == 1
    assert trial["fold_training"][0]["stop_reason"] == "wall_clock"
    assert "cv_accuracy" not in trial


def test_inner_validation_error_records_failed_training_diagnostics(
    early_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed validation retains preceding training loss without suggesting convergence."""

    def failure(*args: Any) -> tuple[float, float]:
        """Inject a scoring failure after a complete training epoch."""
        raise ValueError("invalid inner loss")

    monkeypatch.setattr(tools, "_validation", failure)
    with pytest.raises(ValueError, match="invalid inner loss"):
        tools.evaluate_network(tools.design_network("mlp", (4,), epochs=1))
    trial = tools._ledger(early_run)[0]
    assert trial["status"] == "failed"
    assert trial["fold_training"][0]["stop_reason"] == "failed"
    assert trial["fold_training"][0]["actual_epochs"] == 1


@pytest.mark.parametrize(
    ("horizon", "expected"),
    [(2, [0.001, 0.0001, 0.0001, 0.0001]), (1, [0.001, 0.0001, 0.0001, 0.0001])],
)
def test_independent_cosine_horizon_reaches_floor_then_holds(
    horizon: int, expected: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Additional epochs never restart cosine decay or raise its terminal learning rate."""
    rates: list[float] = []
    original_step = torch.optim.Adam.step

    def step(optimizer: torch.optim.Adam, *args: Any, **kwargs: Any) -> Any:
        """Observe actual updates rather than only the schedule helper's arithmetic."""
        rates.append(float(optimizer.param_groups[0]["lr"]))
        return original_step(optimizer, *args, **kwargs)

    monkeypatch.setattr(torch.optim.Adam, "step", step)
    candidate = tools.design_network(
        "mlp", (4,), epochs=4, schedule="cosine", schedule_epochs=horizon
    )
    with tools._cpu():
        tools._train(
            candidate,
            torch.zeros(16, 28, 28, dtype=torch.uint8),
            torch.arange(16) % 10,
            torch.arange(16),
            7,
            math.inf,
        )
    assert rates == pytest.approx(expected)


@pytest.mark.parametrize("invalid", [-1, 101, True, 1.5])
def test_schedule_horizon_requires_bounded_integer(invalid: int | float) -> None:
    """Malformed horizons cannot silently change the training schedule."""
    with pytest.raises(ValueError, match="schedule_epochs must be an integer"):
        tools.design_network("mlp", (4,), schedule_epochs=invalid)  # type: ignore[arg-type]


def test_explicit_horizon_versions_fixed_training_without_changing_legacy_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new scheduling rule upgrades recorded protocol while untouched fixed recipes remain v3."""
    activate({}, tmp_path)
    try:
        legacy = tools.design_network("mlp", (4,))
        assert tools._protocol_version({}, legacy) == 3
        explicit = tools.design_network("mlp", (4,), schedule_epochs=10)
        assert tools._protocol_version({}, explicit) == 4
        monkeypatch.setattr(time, "monotonic", lambda: 1.0)
        assert tools._deadline(tmp_path, 300, 3) == 301.0
        assert tools._deadline(tmp_path, 300, 4) == 301.0
        assert tools._deadline(tmp_path, 300, 3) == 301.0
        assert json.loads((tmp_path / "search.json").read_text())["protocol_version"] == 4
    finally:
        deactivate()


def test_stopping_input_schema_has_fixed_defaults_and_rejects_invalid_controls() -> None:
    """Run-level stopping controls remain bounded and preserve fixed training by default."""
    manifest = yaml.safe_load((Path(__file__).parents[1] / "agent.yaml").read_text())
    schema = manifest["inputs"]
    assert schema["properties"]["stopping_method"]["default"] == "fixed"
    validator = Draft202012Validator(schema)
    for value in (
        {"patience": 0},
        {"min_epochs": 0},
        {"min_delta": -0.1},
        {"min_delta": 1.1},
        {"stopping_validation_fraction": 0},
        {"stopping_method": "outer_validation"},
    ):
        assert not validator.is_valid(value)
