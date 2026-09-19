"""Specifications for capacity-limited sampling and final-scoring deadlines."""

import json
import time
from pathlib import Path
from typing import Any

import pytest
import torch

import tools
from recurse import _activate as activate
from recurse import _deactivate as deactivate


def test_large_subset_redistributes_unavailable_class_quotas_exactly() -> None:
    """Minority classes are capped while remaining examples are allocated fairly."""
    capacities = [5, 7, 20, 20, 20, 20, 20, 20, 20, 20]
    labels = torch.cat([torch.full((size,), label) for label, size in enumerate(capacities)])
    folds = tools._folds(labels, 3, 100, 7)
    selected = torch.cat(folds)
    assert selected.numel() == selected.unique().numel() == 100
    assert torch.bincount(labels[selected], minlength=10).tolist() == [5, 7, *([11] * 8)]
    replay = tools._folds(labels, 3, 100, 7)
    assert all(torch.equal(first, second) for first, second in zip(folds, replay, strict=True))


def test_redistribution_assigns_remainder_deterministically() -> None:
    """Uneven leftover quotas differ by at most one among uncapped classes."""
    labels = torch.cat([torch.full((5 if label == 0 else 20,), label) for label in range(10)])
    selected = torch.cat(tools._folds(labels, 3, 100, 7))
    assert torch.bincount(labels[selected], minlength=10).tolist() == [5, *([11] * 5), *([10] * 4)]
    assert selected.unique().numel() == 100


def test_requested_subset_cannot_exceed_available_population() -> None:
    """Unavailable examples produce an actionable failure before partition construction."""
    with pytest.raises(ValueError, match="enough examples for the requested sample count"):
        tools._folds(torch.arange(10).repeat_interleave(5), 3, 100, 7)


def test_final_scoring_crossing_deadline_cannot_qualify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even perfect completed folds are rejected when the last scoring operation is late."""
    settings = {
        "samples": 100,
        "cv_folds": 2,
        "cv_method": "stratified_kfold",
        "cv_repeats": 1,
        "cv_seed": 7,
        "seed": 7,
        "max_epochs": 2,
        "max_trials": 3,
        "max_seconds": 300,
        "target_accuracy": 0.99,
    }
    labels = torch.arange(10).repeat_interleave(10)
    monkeypatch.setattr(tools, "_data", lambda: (torch.zeros(100, 28, 28), labels))
    monkeypatch.setattr(tools, "_train", lambda *args: torch.nn.Linear(1, 10))
    monkeypatch.setattr(time, "monotonic", lambda: 1.0)
    calls = 0

    def score(*_: Any) -> float:
        """Exhaust the allowance only in the last held-out scoring operation."""
        nonlocal calls
        calls += 1
        if calls == 2:
            monkeypatch.setattr(time, "monotonic", lambda: 302.0)
        return 1.0

    monkeypatch.setattr(tools, "_accuracy", score)
    activate(settings, tmp_path)
    try:
        trial = tools.evaluate_network(tools.design_network("mlp", (2,), epochs=1))
        assert calls == 2
        assert trial["status"] == "timed_out"
        assert "cv_accuracy" not in trial
        assert not list(tmp_path.glob("*.pt"))
        receipt = tools.finish_search("wall_clock")
        assert receipt == {
            "target_reached": False,
            "cv_accuracy": None,
            "parameter_count": None,
            "trials_attempted": 1,
            "stop_reason": "wall_clock",
        }
        assert json.loads((tmp_path / "trials.json").read_text()) == [trial]
    finally:
        deactivate()
