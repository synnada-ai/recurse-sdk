"""Behavioral tests for the RNA Fold Lab example application."""

import importlib.util
import inspect
import json
import reprlib
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from jsonschema import Draft202012Validator

import recurse
from recurse import _activate as activate
from recurse import _deactivate as deactivate

_SOURCE = Path(__file__).parents[1] / "rna_fold_tools.py"
_APP = _SOURCE.parent


def _load_tools() -> ModuleType:
    """Import the RNA Fold Lab tool module under a collision-free name."""
    spec = importlib.util.spec_from_file_location("rna_fold_lab_tools", _SOURCE)
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


def test_prompt_guides_pairwise_local_repair() -> None:
    """The specialist preserves useful pairs instead of restarting its search."""
    prompt = (_APP / "prompt.md").read_text().lower()

    assert (
        "translate the requested dot-bracket target into explicit paired position numbers" in prompt
    )
    assert "change as few implicated bases as practical" in prompt


@pytest.fixture
def run(tmp_path: Path) -> Iterator[Path]:
    """An active run context with a small exact-fold target."""
    activate(
        {
            "target_structure": "((((....))))",
            "sequence_template": "NNNNNNNNNNNN",
            "gc_min_fraction": 0.25,
            "gc_max_fraction": 0.75,
            "max_homopolymer": 8,
            "max_trials": 8,
        },
        tmp_path,
    )
    yield tmp_path
    deactivate()


def test_candidate_is_measured_and_saved_with_real_fold_evidence(run: Path) -> None:
    """The constructor, oracle, and artifact form one reproducible inner loop."""
    tools = _load_tools()

    candidate = tools.create_sequence("GGGGAAAACCCC")
    history = tools.evaluate_sequence(candidate, ())
    tools.save_best_sequence(history)

    assert history == (
        tools.EvaluatedSequence(
            trial=1,
            sequence="GGGGAAAACCCC",
            predicted_structure="((((....))))",
            minimum_free_energy=pytest.approx(-5.4),
            distance=0,
            gc_fraction=pytest.approx(2 / 3),
        ),
    )
    artifact = json.loads((run / "best-sequence.json").read_text())
    assert artifact["target_structure"] == "((((....))))"
    assert artifact["best"] == {
        "distance": 0,
        "gc_fraction": pytest.approx(2 / 3),
        "minimum_free_energy": pytest.approx(-5.4),
        "predicted_structure": "((((....))))",
        "sequence": "GGGGAAAACCCC",
        "trial": 1,
    }
    assert artifact["history"] == [artifact["best"]]
    assert artifact["constraints"] == {
        "gc_max_fraction": 0.75,
        "gc_min_fraction": 0.25,
        "max_homopolymer": 8,
        "sequence_template": "NNNNNNNNNNNN",
    }
    assert {path.name for path in run.iterdir()} == {"best-sequence.json"}


def test_constructor_enforces_fixed_sequence_positions(tmp_path: Path) -> None:
    """A candidate that changes a fixed base never reaches fold measurement."""
    tools = _load_tools()
    activate(
        {
            "target_structure": "((((....))))",
            "sequence_template": "NNNNGNNNNNNN",
            "gc_min_fraction": 0.0,
            "gc_max_fraction": 1.0,
            "max_homopolymer": 12,
            "max_trials": 1,
        },
        tmp_path,
    )
    try:
        with pytest.raises(ValueError, match="position 5 must be G"):
            tools.create_sequence("GGGGAAAACCCC")
    finally:
        deactivate()


@pytest.mark.parametrize(
    ("sequence", "minimum", "maximum", "message"),
    [
        ("AAAAAAAAAAAA", 0.25, 1.0, "GC fraction 0.000 must be at least 0.250"),
        ("GGGGAAAACCCC", 0.0, 0.6, "GC fraction 0.667 must be at most 0.600"),
    ],
)
def test_constructor_enforces_gc_content_band(
    tmp_path: Path,
    sequence: str,
    minimum: float,
    maximum: float,
    message: str,
) -> None:
    """Candidates outside the declared GC band are rejected before measurement."""
    tools = _load_tools()
    activate(
        {
            "target_structure": "((((....))))",
            "sequence_template": "NNNNNNNNNNNN",
            "gc_min_fraction": minimum,
            "gc_max_fraction": maximum,
            "max_homopolymer": 12,
            "max_trials": 1,
        },
        tmp_path,
    )
    try:
        with pytest.raises(ValueError, match=message):
            tools.create_sequence(sequence)
    finally:
        deactivate()


def test_constructor_enforces_homopolymer_ceiling(tmp_path: Path) -> None:
    """A base run longer than the declared ceiling is rejected before measurement."""
    tools = _load_tools()
    activate(
        {
            "target_structure": "((((....))))",
            "sequence_template": "NNNNNNNNNNNN",
            "gc_min_fraction": 0.0,
            "gc_max_fraction": 1.0,
            "max_homopolymer": 3,
            "max_trials": 1,
        },
        tmp_path,
    )
    try:
        with pytest.raises(ValueError, match="may not repeat one base more than 3 times"):
            tools.create_sequence("GGGGAAAACCCC")
    finally:
        deactivate()


def test_constructor_rejects_a_template_with_the_wrong_length(tmp_path: Path) -> None:
    """A fixed-base template must describe every target position exactly once."""
    tools = _load_tools()
    activate(
        {
            "target_structure": "((((....))))",
            "sequence_template": "NNNNNNNNNNN",
            "gc_min_fraction": 0.0,
            "gc_max_fraction": 1.0,
            "max_homopolymer": 12,
            "max_trials": 1,
        },
        tmp_path,
    )
    try:
        with pytest.raises(ValueError, match="sequence_template must contain exactly 12 positions"):
            tools.create_sequence("GGGGAAAACCCC")
    finally:
        deactivate()


@pytest.mark.parametrize(
    ("sequence", "message"),
    [
        ("GGGGAAAATTTT", "sequence must use only A, C, G, and U"),
        ("GGGGAAAACCC", "sequence must contain exactly 12 nucleotides"),
    ],
)
def test_constructor_rejects_invalid_candidates(run: Path, sequence: str, message: str) -> None:
    """Malformed candidates receive a correction before oracle measurement."""
    tools = _load_tools()

    with pytest.raises(ValueError, match=message):
        tools.create_sequence(sequence)


def test_duplicate_candidate_does_not_consume_a_trial(run: Path) -> None:
    """A repeated sequence is rejected without extending measured history."""
    tools = _load_tools()
    candidate = tools.create_sequence("GGGGAAAACCCC")
    history = tools.evaluate_sequence(candidate, ())

    with pytest.raises(ValueError, match="sequence has already been evaluated"):
        tools.evaluate_sequence(candidate, history)

    assert len(history) == 1


def test_trial_limit_is_enforced_at_the_validator_boundary(tmp_path: Path) -> None:
    """The oracle cannot measure more candidates than the declared run budget."""
    tools = _load_tools()
    activate(
        {
            "target_structure": "((((....))))",
            "sequence_template": "NNNNNNNNNNNN",
            "gc_min_fraction": 0.0,
            "gc_max_fraction": 1.0,
            "max_homopolymer": 12,
            "max_trials": 1,
        },
        tmp_path,
    )
    try:
        first = tools.create_sequence("GGGGAAAACCCC")
        history = tools.evaluate_sequence(first, ())
        second = tools.create_sequence("CCCCAAAAGGGG")
        with pytest.raises(ValueError, match="maximum of 1 measured trial has been reached"):
            tools.evaluate_sequence(second, history)
    finally:
        deactivate()


def test_plural_trial_limit_error_is_clear(tmp_path: Path) -> None:
    """The exhausted multi-trial budget reports the correct limit."""
    tools = _load_tools()
    activate(
        {
            "target_structure": "((((....))))",
            "sequence_template": "NNNNNNNNNNNN",
            "gc_min_fraction": 0.0,
            "gc_max_fraction": 1.0,
            "max_homopolymer": 12,
            "max_trials": 2,
        },
        tmp_path,
    )
    try:
        history = tools.evaluate_sequence(tools.create_sequence("GGGGAAAACCCC"), ())
        history = tools.evaluate_sequence(tools.create_sequence("CCCCAAAAGGGG"), history)
        with pytest.raises(ValueError, match="maximum of 2 measured trials has been reached"):
            tools.evaluate_sequence(tools.create_sequence("GCGCAAAAGCGC"), history)
    finally:
        deactivate()


@pytest.mark.parametrize("target", ["(((...))", "((..x..))", ")("])
def test_constructor_rejects_malformed_target_structure(tmp_path: Path, target: str) -> None:
    """Malformed dot-bracket targets are rejected before a candidate is accepted."""
    tools = _load_tools()
    activate({"target_structure": target, "max_trials": 1}, tmp_path)
    try:
        with pytest.raises(ValueError, match="target_structure must be balanced dot-bracket"):
            tools.create_sequence("A" * len(target))
    finally:
        deactivate()


def test_example_builds_as_a_complete_locked_application() -> None:
    """The public bundler accepts the exact application sent to Recurse."""
    artifacts, identity = recurse.build_bundle(_APP)

    assert set(artifacts) == {"source"}
    assert identity["api_version"] == "recurse.application/v1alpha1"
    assert b'name = "viennarna"' in recurse._sdist_file(artifacts["source"], "uv.lock").lower()
    manifest = yaml.safe_load((_APP / "agent.yaml").read_text())
    Draft202012Validator(manifest["outputs"]).validate(
        {
            "sequence": "GGGGAAAACCCC",
            "predicted_structure": "((((....))))",
            "minimum_free_energy": -5.4,
            "distance": 0,
            "gc_fraction": 2 / 3,
            "measured_trials": 1,
        }
    )
    assert manifest["tools"]["register"]["evaluate_sequence"] == {"volatile": True}


def test_measurement_state_keeps_best_and_latest_visible_after_six_trials(
    run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bounded state preview retains both useful ends of a long search."""
    tools = _load_tools()
    monkeypatch.setattr(tools.RNA, "fold", lambda _sequence: ("((........))", -8.0))

    state = ()
    for trial in range(1, 9):
        bits = format(trial, "012b")
        sequence = bits.translate(str.maketrans("01", "AC"))
        state = tools.evaluate_sequence(tools.SequenceCandidate(sequence), state)

    rendered = reprlib.Repr(maxstring=128, maxother=128).repr(state)
    assert "trial=1" in rendered
    assert "trial=8" in rendered
    assert "((........))" in rendered


def test_save_returns_the_authoritative_result_receipt(run: Path) -> None:
    """Final prose can copy measured facts instead of reconstructing them."""
    tools = _load_tools()
    history = tools.evaluate_sequence(tools.create_sequence("GGGGAAAACCCC"), ())

    receipt = tools.save_best_sequence(history)

    assert json.loads(receipt) == {
        "sequence": "GGGGAAAACCCC",
        "predicted_structure": "((((....))))",
        "minimum_free_energy": pytest.approx(-5.4),
        "distance": 0,
        "gc_fraction": pytest.approx(2 / 3),
        "measured_trials": 1,
    }


def test_measurement_history_flows_through_typed_tool_values(run: Path) -> None:
    """Each history is explicit and no hidden module state crosses search branches."""
    tools = _load_tools()
    candidate = tools.create_sequence("GGGGAAAACCCC")

    first_history = tools.evaluate_sequence(candidate, ())
    independent_history = tools.evaluate_sequence(candidate, ())

    assert first_history[0].trial == 1
    assert independent_history[0].trial == 1
    assert tuple(inspect.signature(tools.evaluate_sequence).parameters) == (
        "candidate",
        "previous",
    )
    assert tuple(inspect.signature(tools.save_best_sequence).parameters) == ("evaluated_sequences",)


def test_constructor_rejects_an_inverted_gc_band(tmp_path: Path) -> None:
    """An impossible GC interval is rejected before measurement."""
    tools = _load_tools()
    activate(
        {
            "target_structure": "((((....))))",
            "sequence_template": "NNNNNNNNNNNN",
            "gc_min_fraction": 0.7,
            "gc_max_fraction": 0.3,
            "max_homopolymer": 8,
            "max_trials": 1,
        },
        tmp_path,
    )
    try:
        with pytest.raises(ValueError, match="gc_min_fraction must not exceed gc_max_fraction"):
            tools.create_sequence("GGGGAAAACCCC")
    finally:
        deactivate()


def test_save_rejects_an_empty_measurement_ledger(run: Path) -> None:
    """Saving before measurement returns a precise corrective error."""
    tools = _load_tools()

    with pytest.raises(ValueError, match="measure at least one sequence before saving"):
        tools.save_best_sequence(())
