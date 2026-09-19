"""Specifications for removing convolution offsets while retaining learned normalization."""

from dataclasses import replace

import pytest
import torch
from torch import nn

import tools


def test_bias_removal_preserves_all_other_initial_parameters() -> None:
    """A controlled size reduction removes only four convolution bias vectors."""
    ordinary = tools.design_network(
        "cnn", (8, 12), normalization="batch", convs_per_stage=2, head_size=3
    )
    with torch.random.fork_rng(devices=[]), tools._cpu():
        torch.manual_seed(7)
        baseline = tools._network(ordinary)
        torch.manual_seed(7)
        compact = tools._network(replace(ordinary, family="biasfree"))
        assert sum(p.numel() for p in baseline.parameters()) == 4018
        assert sum(p.numel() for p in compact.parameters()) == 3978
        removed = set(baseline.state_dict()) - set(compact.state_dict())
        assert removed == {f"{i}.bias" for i in (0, 3, 7, 10)}
        for name, value in compact.state_dict().items():
            assert torch.equal(value, baseline.state_dict()[name])
        for layer in compact.modules():
            if isinstance(layer, nn.Conv2d):
                assert layer.bias is None
            elif isinstance(layer, (nn.BatchNorm2d, nn.Linear)):
                assert layer.bias is not None


def test_biasfree_rejects_spatial_head_overflow_and_layer_norm() -> None:
    """The extra family observes the same convolutional shape and normalization bounds."""
    with pytest.raises(ValueError, match="final spatial width"):
        tools.design_network("biasfree", (2, 3, 4), head_size=4)
    with pytest.raises(ValueError, match="incompatible"):
        tools.design_network("biasfree", (4,), normalization="layer")
