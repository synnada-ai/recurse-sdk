"""Fit deployment or cross-validation models under an enforceable subprocess deadline."""

import json
import sys
from pathlib import Path

import joblib
import pandas as pd
from threadpoolctl import threadpool_limits

from .learning import cross_validate, fit

__all__ = ["run"]


def run(root: Path, identifier: int, phase: str = "fit") -> None:
    """Read a trusted job and write its deployment model or cross-validation scores."""
    job = json.loads((root / f"job-{identifier}.json").read_text())
    data = pd.read_parquet(root / "data.parquet")
    with threadpool_limits(limits=1):
        if phase == "validate":
            result = cross_validate(data, job["specification"], job["configuration"], job["plan"])
            (root / f"validation-{identifier}.json").write_text(json.dumps(result, allow_nan=False))
        else:
            model = fit(data.iloc[job["plan"]["fit"]], job["specification"], job["configuration"])
            joblib.dump(model, root / f"candidate-{identifier}.joblib", compress=0, protocol=5)


if __name__ == "__main__":
    run(Path(sys.argv[1]), int(sys.argv[2]), next(iter(sys.argv[3:]), "fit"))
