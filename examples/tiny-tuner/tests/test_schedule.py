"""Specifications for learning-rate decay without changing the evaluation protocol."""

import math
from dataclasses import replace
from typing import Any, Literal

import pytest
import torch

import tools


@pytest.mark.parametrize(
    ("schedule", "epochs", "min_lr_ratio", "expected"),
    [
        ("constant", 4, 0.1, [0.001] * 8),
        ("constant", 4, 0, [0.001] * 8),
        ("cosine", 4, 0.1, [0.001] * 2 + [0.000775] * 2 + [0.000325] * 2 + [0.0001] * 2),
        ("cosine", 1, 0.1, [0.001] * 2),
        ("cosine", 3, 0, [0.001] * 2 + [0.0005] * 2 + [0] * 2),
        ("cosine", 3, 0.01, [0.001] * 2 + [0.000505] * 2 + [0.00001] * 2),
        ("cosine", 3, 1, [0.001] * 6),
        ("cosine", 1, 0, [0.001] * 2),
        ("cosine", 1, 0.01, [0.001] * 2),
        ("cosine", 1, 1, [0.001] * 2),
    ],
)
def test_schedule_applies_expected_rate_to_every_update_and_resets_for_each_fold(
    monkeypatch: pytest.MonkeyPatch,
    schedule: Literal["constant", "cosine"],
    epochs: int,
    min_lr_ratio: float,
    expected: list[float],
) -> None:
    """Epoch decay reaches both endpoints, holds within epochs, and restarts per fold."""
    rates: list[float] = []
    original_step = torch.optim.Adam.step

    def record_step(optimizer: torch.optim.Adam, *args: Any, **kwargs: Any) -> Any:
        """Observe the rate used by each actual optimizer update."""
        rates.append(float(optimizer.param_groups[0]["lr"]))
        return original_step(optimizer, *args, **kwargs)

    monkeypatch.setattr(torch.optim.Adam, "step", record_step)
    candidate = tools.design_network(
        "mlp", (4,), epochs=epochs, batch_size=16, schedule=schedule, min_lr_ratio=min_lr_ratio
    )
    images = torch.zeros((32, 28, 28), dtype=torch.uint8)
    labels = torch.arange(32) % 10
    with tools._cpu():
        first = tools._train(candidate, images, labels, torch.arange(32), 7, math.inf)
        second = tools._train(candidate, images, labels, torch.arange(32), 7, math.inf)
    assert rates == pytest.approx(expected * 2)
    assert all(
        torch.equal(value, second.state_dict()[key]) for key, value in first.state_dict().items()
    )


def test_default_schedule_is_constant_and_invalid_schedule_is_rejected() -> None:
    """Old recipes retain constant learning rates and unknown strategies fail validation."""
    candidate = tools.design_network("mlp", (4,))
    assert candidate.schedule == "constant"
    assert candidate.min_lr_ratio == 0.1
    with pytest.raises(ValueError, match="schedule must be constant or cosine"):
        tools._check(replace(candidate, schedule="linear"))  # type: ignore[arg-type]


@pytest.mark.parametrize("ratio", [math.nan, math.inf, -0.01, 1.01])
def test_invalid_minimum_learning_rate_ratios_are_rejected(ratio: float) -> None:
    """Reject non-finite ratios and values outside the bounded decay range."""
    with pytest.raises(ValueError, match=r"min_lr_ratio must be finite and in \[0, 1\]"):
        tools.design_network("mlp", (4,), min_lr_ratio=ratio)
