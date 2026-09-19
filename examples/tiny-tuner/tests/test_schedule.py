"""Specifications for learning-rate decay without changing the evaluation protocol."""

import math
from dataclasses import replace
from typing import Any, Literal

import pytest
import torch

import tools


@pytest.mark.parametrize(
    ("schedule", "epochs", "expected"),
    [
        ("constant", 4, [0.001] * 8),
        ("cosine", 4, [0.001] * 2 + [0.000775] * 2 + [0.000325] * 2 + [0.0001] * 2),
        ("cosine", 1, [0.001] * 2),
    ],
)
def test_schedule_applies_expected_rate_to_every_update_and_resets_for_each_fold(
    monkeypatch: pytest.MonkeyPatch,
    schedule: Literal["constant", "cosine"],
    epochs: int,
    expected: list[float],
) -> None:
    """Epoch decay reaches both endpoints, holds within epochs, and restarts per fold."""
    rates: list[float] = []
    original_step = torch.optim.Adam.step

    def record_step(optimizer: torch.optim.Adam, *args: Any, **kwargs: Any) -> Any:
        rates.append(float(optimizer.param_groups[0]["lr"]))
        return original_step(optimizer, *args, **kwargs)

    monkeypatch.setattr(torch.optim.Adam, "step", record_step)
    candidate = tools.design_network("mlp", (4,), epochs=epochs, batch_size=16, schedule=schedule)
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
    with pytest.raises(ValueError, match="schedule must be constant or cosine"):
        tools._check(replace(candidate, schedule="linear"))  # type: ignore[arg-type]
