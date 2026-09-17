"""Isolated data fixtures and tool imports for Predictive Modeler."""

import importlib.util
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from modeler.contracts import resolve  # noqa: E402 - example import path


def _load_tools() -> Any:
    """Import the example without colliding with the SDK's tools directory."""
    spec = importlib.util.spec_from_file_location("predictive_tools", ROOT / "tools.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tools() -> Any:
    """Expose the public agent actions."""
    return _load_tools()


@pytest.fixture
def frame() -> pd.DataFrame:
    """Small generated unit-test fixture; user-facing examples use real observations."""
    values = np.arange(150)
    return pd.DataFrame(
        {
            "x": values.astype(float),
            "category": np.where(values % 2, "odd", "even"),
            "text": np.where(values % 2, "warm orange sunny", "cold blue rainy"),
            "y": (values % 2).astype(str),
            "a": values % 2,
            "b": (values // 2) % 2,
            "value": values * 0.5 + np.sin(values),
            "group": values // 10,
            "date": pd.date_range("2020-01-01", periods=150),
            "_split": ["train"] * 90 + ["validation"] * 30 + ["test"] * 30,
        }
    )


@pytest.fixture
def spec(frame: pd.DataFrame) -> dict[str, Any]:
    """A resolved binary classification contract."""
    return resolve(
        {"kind": "binary", "targets": ["y"], "features": ["x", "category"], "split": "random"},
        frame,
        {},
    )
