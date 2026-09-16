"""Fit a fixed candidate in a subprocess so training deadlines can be enforced."""

import json
import sys
from pathlib import Path

import joblib
import pandas as pd
from threadpoolctl import threadpool_limits

from .learning import fit

__all__ = ["run"]


def run(root: Path, identifier: int) -> None:
    """Read a trusted internal job and write its fitted model."""
    job = json.loads((root / f"job-{identifier}.json").read_text())
    data = pd.read_parquet(root / "data.parquet")
    with threadpool_limits(limits=1):
        model = fit(data.iloc[job["train"]], job["specification"], job["configuration"])
        joblib.dump(model, root / f"candidate-{identifier}.joblib", compress=0, protocol=5)


if __name__ == "__main__":
    run(Path(sys.argv[1]), int(sys.argv[2]))
