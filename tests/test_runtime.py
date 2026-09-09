"""Tests for the public runtime context."""

from pathlib import Path
from typing import cast

import pytest

import recurse
from recurse import RunContext, RunContextError
from recurse import _activate as activate
from recurse import _deactivate as deactivate


def test_context_outside_a_run_raises_a_clear_public_error() -> None:
    """context() outside a run raises the documented no-active-run error."""
    with pytest.raises(RunContextError, match=r"^no active Recurse run: "):
        recurse.context()


def test_context_returns_the_active_run_context(tmp_path: Path) -> None:
    """An activated run exposes its inputs and workspace, then deactivates cleanly."""
    activate({"target": 0.9, "labels": {"tags": ["a"]}}, tmp_path)
    try:
        active = recurse.context()
        assert isinstance(active, RunContext)
        assert active.workspace == tmp_path
        assert active.inputs["target"] == 0.9
    finally:
        deactivate()
    with pytest.raises(RunContextError, match="no active Recurse run"):
        recurse.context()


def test_active_inputs_are_deeply_read_only(tmp_path: Path) -> None:
    """Nested inputs are frozen and detached from the caller's mutable data."""
    supplied = {"labels": {"tags": ["a"]}}
    activate(supplied, tmp_path)
    try:
        inputs = recurse.context().inputs
        with pytest.raises(TypeError):
            inputs["labels"] = None  # type: ignore[index]
        labels = cast("dict[str, object]", inputs["labels"])
        with pytest.raises(TypeError):
            labels["tags"] = None
        assert labels["tags"] == ("a",)
        supplied["labels"]["tags"].append("mutated-after-activation")
        active_labels = cast("dict[str, object]", recurse.context().inputs["labels"])
        assert active_labels["tags"] == ("a",)
    finally:
        deactivate()


def test_activation_rejects_a_second_concurrent_run(tmp_path: Path) -> None:
    """Activating over an active run context fails."""
    activate({}, tmp_path)
    try:
        with pytest.raises(RunContextError, match="already active"):
            activate({}, tmp_path)
    finally:
        deactivate()


def test_public_api_is_exactly_the_declared_surface() -> None:
    """__all__ lists exactly the supported public names, all importable."""
    assert recurse.__all__ == [
        "BundleError",
        "ManifestError",
        "RecurseError",
        "RunContext",
        "RunContextError",
        "build_bundle",
        "context",
    ]
    for name in recurse.__all__:
        assert getattr(recurse, name) is not None


def test_error_hierarchy_is_rooted_at_recurse_error() -> None:
    """Every public error derives from RecurseError."""
    assert issubclass(recurse.ManifestError, recurse.RecurseError)
    assert issubclass(recurse.BundleError, recurse.RecurseError)
    assert issubclass(recurse.RunContextError, recurse.RecurseError)
    assert issubclass(recurse.RecurseError, Exception)
