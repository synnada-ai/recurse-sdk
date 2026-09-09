"""Construction and forward-fold validation tools for RNA Fold Lab."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import cast

import RNA  # type: ignore[import-untyped]

import recurse


def _target_structure() -> str:
    """Return the schema-validated target for the active run."""
    target = cast("str", recurse.context().inputs["target_structure"])
    balance = 0
    for symbol in target:
        if symbol == "(":
            balance += 1
        elif symbol == ")":
            balance -= 1
        elif symbol != ".":
            raise ValueError("target_structure must be balanced dot-bracket notation")
        if balance < 0:
            raise ValueError("target_structure must be balanced dot-bracket notation")
    if balance:
        raise ValueError("target_structure must be balanced dot-bracket notation")
    return target


def _max_trials() -> int:
    """Return the schema-validated measurement budget for the active run."""
    return cast("int", recurse.context().inputs["max_trials"])


def _sequence_template() -> str:
    """Return the schema-validated fixed-base template for the active run."""
    return cast("str", recurse.context().inputs["sequence_template"])


@dataclass(frozen=True)
class SequenceCandidate:
    """An RNA sequence proposed by the specialist.

    Attributes:
        sequence: Uppercase RNA nucleotide sequence.
    """

    sequence: str


@dataclass(frozen=True, repr=False)
class EvaluatedSequence:
    """One sequence measured by the frozen forward-folding oracle.

    Attributes:
        trial: One-based oracle measurement number.
        sequence: RNA sequence submitted to the oracle.
        predicted_structure: Minimum-free-energy fold in dot-bracket notation.
        minimum_free_energy: Predicted fold energy in kcal/mol.
        distance: Base-pair distance from the requested target.
        gc_fraction: Fraction of nucleotides that are G or C.
    """

    trial: int
    sequence: str
    predicted_structure: str
    minimum_free_energy: float
    distance: int
    gc_fraction: float

    def __repr__(self) -> str:
        """Keep the search-relevant measurement visible in bounded previews."""
        return (
            f"EvaluatedSequence(trial={self.trial}, distance={self.distance}, "
            f"structure={self.predicted_structure!r}, "
            f"energy={self.minimum_free_energy}, gc={self.gc_fraction:.3f})"
        )


def create_sequence(sequence: str) -> SequenceCandidate:
    """Construct an RNA sequence candidate chosen by the specialist.

    Args:
        sequence: Candidate using the RNA alphabet ``A``, ``C``, ``G``, and ``U``.

    Returns:
        The candidate ready for independent measurement.

    Raises:
        ValueError: If the sequence is malformed or does not match the target length.
    """
    if not sequence or set(sequence) - set("ACGU"):
        raise ValueError("sequence must use only A, C, G, and U")
    target = _target_structure()
    if len(sequence) != len(target):
        raise ValueError(f"sequence must contain exactly {len(target)} nucleotides")
    template = _sequence_template()
    if len(template) != len(target):
        raise ValueError(f"sequence_template must contain exactly {len(target)} positions")
    for position, (base, required) in enumerate(zip(sequence, template, strict=True), start=1):
        if required not in ("N", base):
            raise ValueError(f"position {position} must be {required}")
    gc_fraction = sum(base in "GC" for base in sequence) / len(sequence)
    minimum = float(cast("int | float", recurse.context().inputs["gc_min_fraction"]))
    maximum = float(cast("int | float", recurse.context().inputs["gc_max_fraction"]))
    if minimum > maximum:
        raise ValueError("gc_min_fraction must not exceed gc_max_fraction")
    if gc_fraction < minimum:
        raise ValueError(f"GC fraction {gc_fraction:.3f} must be at least {minimum:.3f}")
    if gc_fraction > maximum:
        raise ValueError(f"GC fraction {gc_fraction:.3f} must be at most {maximum:.3f}")
    max_homopolymer = cast("int", recurse.context().inputs["max_homopolymer"])
    if any(base * (max_homopolymer + 1) in sequence for base in "ACGU"):
        raise ValueError(f"sequence may not repeat one base more than {max_homopolymer} times")
    return SequenceCandidate(sequence=sequence)


def evaluate_sequence(
    candidate: SequenceCandidate, previous: tuple[EvaluatedSequence, ...]
) -> tuple[EvaluatedSequence, ...]:
    """Forward-fold one candidate and extend the stored measurement history.

    Args:
        candidate: Sequence selected by the specialist.
        previous: Prior measurements loaded from the stable agent storage key.

    Returns:
        Complete state ordered with the best result first and newest others next.

    Raises:
        ValueError: If the candidate is a duplicate or the trial limit is exhausted.
    """
    if any(item.sequence == candidate.sequence for item in previous):
        raise ValueError("sequence has already been evaluated; construct a different candidate")
    max_trials = _max_trials()
    if len(previous) >= max_trials:
        noun = "trial" if max_trials == 1 else "trials"
        raise ValueError(f"maximum of {max_trials} measured {noun} has been reached")
    predicted_structure, energy = RNA.fold(candidate.sequence)
    target = _target_structure()
    measured = EvaluatedSequence(
        trial=len(previous) + 1,
        sequence=candidate.sequence,
        predicted_structure=predicted_structure,
        minimum_free_energy=float(energy),
        distance=int(RNA.bp_distance(predicted_structure, target)),
        gc_fraction=sum(base in "GC" for base in candidate.sequence) / len(candidate.sequence),
    )
    history = (*previous, measured)
    best = min(history, key=lambda item: (item.distance, item.trial))
    recent = sorted(
        (item for item in history if item is not best),
        key=lambda item: item.trial,
        reverse=True,
    )
    return (best, *recent)


def save_best_sequence(evaluated_sequences: tuple[EvaluatedSequence, ...]) -> str:
    """Persist the best measured candidate and complete search history.

    Args:
        evaluated_sequences: Complete history loaded from agent storage.

    Returns:
        JSON receipt containing the exact measured facts written to the artifacts.

    Raises:
        ValueError: If no candidate has been measured.
    """
    if not evaluated_sequences:
        raise ValueError("measure at least one sequence before saving")
    chronological = tuple(sorted(evaluated_sequences, key=lambda item: item.trial))
    best = min(chronological, key=lambda item: item.distance)
    payload = {
        "target_structure": _target_structure(),
        "constraints": {
            "sequence_template": _sequence_template(),
            "gc_min_fraction": recurse.context().inputs["gc_min_fraction"],
            "gc_max_fraction": recurse.context().inputs["gc_max_fraction"],
            "max_homopolymer": recurse.context().inputs["max_homopolymer"],
        },
        "best": asdict(best),
        "history": [asdict(item) for item in chronological],
    }
    artifact = recurse.context().workspace / "best-sequence.json"
    artifact.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return json.dumps(
        {
            "sequence": best.sequence,
            "predicted_structure": best.predicted_structure,
            "minimum_free_energy": best.minimum_free_energy,
            "distance": best.distance,
            "gc_fraction": best.gc_fraction,
            "measured_trials": len(chronological),
        },
        sort_keys=True,
    )
