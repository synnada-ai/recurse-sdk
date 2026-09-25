"""Specifications for compact networks with repeated convolutions before pooling."""

import math
from dataclasses import asdict, replace
from pathlib import Path
from typing import Literal

import pytest
import torch
from torch import nn

import tools


@pytest.mark.parametrize(
    ("family", "convs_per_stage", "parameters", "convolutions"),
    [("cnn", 1, 690, 2), ("cnn", 2, 1446, 4), ("separable", 1, 452, 4), ("separable", 2, 688, 8)],
)
def test_extra_convolutions_preserve_spatial_resolution_and_count_all_parameters(
    family: Literal["cnn", "separable"],
    convs_per_stage: int,
    parameters: int,
    convolutions: int,
) -> None:
    """Two stages retain 7x7 features regardless of the number of convolution blocks."""
    candidate = tools.design_network(
        family, (4, 8), normalization="batch", convs_per_stage=convs_per_stage
    )
    with tools._cpu():
        model = tools._network(candidate)
        # Counts include convolution biases, BN affine terms and the 330-parameter head.
        assert sum(parameter.numel() for parameter in model.parameters()) == parameters
        modules = list(model.modules())
        assert sum(isinstance(module, nn.Conv2d) for module in modules) == convolutions
        assert sum(isinstance(module, nn.BatchNorm2d) for module in modules) == 2 * convs_per_stage
        assert sum(isinstance(module, nn.ReLU) for module in modules) == 2 * convs_per_stage
        assert sum(isinstance(module, nn.MaxPool2d) for module in modules) == 2
        # Inspect the feature map immediately before adaptive pooling and the classifier.
        features = nn.Sequential(*list(model.children())[:-3])
        assert features(torch.zeros(2, 1, 28, 28)).shape == (2, 8, 7, 7)
        assert model(torch.zeros(2, 1, 28, 28)).shape == (2, 10)


@pytest.mark.parametrize("family", ["cnn", "separable"])
def test_repeated_convolution_training_is_reproducible_and_checkpoint_replays(
    family: Literal["cnn", "separable"], tmp_path: Path
) -> None:
    """The saved recipe rebuilds extra blocks and reloads their complete trained state."""
    candidate = tools.design_network(
        family, (4, 8), normalization="batch", convs_per_stage=2, epochs=2, batch_size=16
    )
    generator = torch.Generator().manual_seed(12)
    images = torch.randint(0, 256, (32, 28, 28), generator=generator, dtype=torch.uint8)
    labels = torch.arange(32) % 10
    indices = torch.arange(32)
    with tools._cpu():
        first = tools._train(candidate, images, labels, indices, 7, math.inf)
        second = tools._train(candidate, images, labels, indices, 7, math.inf)
        assert all(
            torch.equal(value, second.state_dict()[key])
            for key, value in first.state_dict().items()
        )
        checkpoint = tmp_path / "model.pt"
        torch.save(first.state_dict(), checkpoint)
        replay = tools._network(tools.Candidate(**asdict(candidate)))
        replay.load_state_dict(torch.load(checkpoint, weights_only=True))
        first.eval()
        replay.eval()
        pixels = tools._pixels(images, indices)
        torch.testing.assert_close(first(pixels), replay(pixels))


def test_default_convolution_count_is_one() -> None:
    """Recipes that omit the new dimension retain the single-block architecture."""
    default = tools.design_network("cnn", (4, 8))
    assert default.convs_per_stage == 1
    assert default == tools.design_network("cnn", (4, 8), convs_per_stage=1)


@pytest.mark.parametrize("invalid", [0, 3, True, 1.5])
def test_invalid_convolution_counts_are_rejected(invalid: int | float) -> None:
    """Only bounded integer counts are accepted before network allocation."""
    candidate = tools.design_network("cnn", (4,))
    with pytest.raises(ValueError, match="convs_per_stage must be an integer, 1 or 2"):
        tools._check(replace(candidate, convs_per_stage=invalid))  # type: ignore[arg-type]
