"""Behavioral tests for the Tiny Tuner example application."""

import importlib.util
import json
import sys
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from recurse import _activate as activate
from recurse import _deactivate as deactivate

_SOURCE = Path(__file__).parents[1] / "examples" / "tiny-tuner" / "tools.py"
_MANIFEST = _SOURCE.with_name("agent.yaml")


def _load_tools() -> ModuleType:
    """Import the Tiny Tuner tool module under a collision-free name."""
    spec = importlib.util.spec_from_file_location("tiny_tuner_tools", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous_dont_write_bytecode
        del sys.modules[spec.name]
    return module


@pytest.fixture
def run(tmp_path: Path) -> Iterator[Path]:
    """An active run context with default Tiny Tuner inputs and a workspace."""
    activate({"target_f1": 0.8, "max_trials": 3, "optimizer_momentum": 0.8}, tmp_path)
    yield tmp_path
    deactivate()


def test_tuning_chain_measures_trials_and_saves_the_best_model(run: Path) -> None:
    """The full extract-train-validate-save chain is deterministic end to end."""
    tools = _load_tools()
    trials = [
        (1, False, False, 0.05, 0.0, 20, 1),
        (1, True, True, 0.1, 0.0, 50, 4),
        (2, True, True, 0.1, 0.01, 50, 2),
    ]
    history: tuple[Any, ...] = ()
    candidates = []
    for degree, interaction, standardize, rate, penalty, epochs, seed in trials:
        features = tools.extract_features(degree, interaction, standardize)
        candidate = tools.train_model(features, rate, penalty, epochs, seed)
        candidates.append(candidate)
        history = tools.validate_model(candidate, history)

    assert [round(validated.f1, 6) for validated in history] == [0.307692, 0.875, 0.823529]
    assert history[1].model["seed"] == 4
    assert history[1].model["optimizer_momentum"] == 0.8
    assert history[1].precision == pytest.approx(0.875)
    assert history[1].recall == pytest.approx(0.875)
    assert history[1].weights == pytest.approx(candidates[1].weights)
    assert history[1].bias == pytest.approx(candidates[1].bias)

    tools.save_best_model(history)
    artifact = json.loads((run / "best-model.json").read_text())
    assert artifact == json.loads(json.dumps(asdict(history[1])))


def test_tiny_tuner_declares_its_successful_output() -> None:
    """Tiny Tuner's declared output accepts the result its agent must return."""
    manifest = yaml.safe_load(_MANIFEST.read_text())
    Draft202012Validator(manifest["outputs"]).validate({"best_f1": 0.875, "target_reached": True})


def test_training_reproduces_weights_and_momentum_changes_them(tmp_path: Path) -> None:
    """A saved configuration is reproducible and momentum affects learned weights."""
    tools = _load_tools()
    features = tools.extract_features(2, True, True)

    activate({"optimizer_momentum": 0.0}, tmp_path)
    try:
        without_momentum = tools.train_model(features, 0.1, 0.01, 500, 1)
    finally:
        deactivate()

    activate({"optimizer_momentum": 0.8}, tmp_path)
    try:
        with_momentum = tools.train_model(features, 0.1, 0.01, 500, 1)
        replay = tools.train_model(features, 0.1, 0.01, 500, 1)
    finally:
        deactivate()

    assert with_momentum.weights == pytest.approx(replay.weights)
    assert with_momentum.bias == pytest.approx(replay.bias)
    assert without_momentum.weights != pytest.approx(with_momentum.weights)


def test_feature_configuration_controls_separability(run: Path) -> None:
    """Interaction features are what make the dataset separable."""
    tools = _load_tools()
    without = tools.extract_features(2, False, True)
    with_interaction = tools.extract_features(2, True, True)
    assert len(with_interaction.train_features[0]) == len(without.train_features[0]) + 1
    assert without.validation_labels == with_interaction.validation_labels
    assert 1 in without.validation_labels
    assert 0 in without.validation_labels


def test_training_rejects_a_non_numeric_momentum_input(tmp_path: Path) -> None:
    """A malformed optimizer_momentum run input is rejected explicitly."""
    tools = _load_tools()
    activate({"optimizer_momentum": "fast"}, tmp_path)
    try:
        features = tools.extract_features(1, True, False)
        with pytest.raises(TypeError, match="optimizer_momentum must be a number"):
            tools.train_model(features, 0.1, 0.0, 1, 1)
    finally:
        deactivate()
