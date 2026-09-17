"""Local fixed-search baselines and failure-preserving benchmark comparisons.

This evaluator owns ground truth. It must never be included in the agent bundle.
"""

import argparse
import hashlib
import importlib.util
import io
import json
import platform
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pandas as pd
from modeler.datasets import _download

from recurse import _activate, _deactivate

from .assessment import assess

__all__ = ["audit", "compare", "fingerprint", "load_cases", "main", "run"]
ROOT = Path(__file__).parents[1]
PROTOCOL = "predictive-modeler/v2"
_MAX_PROBES = 3


def fingerprint(value: Any) -> str:
    """Hash canonical JSON for exact case and budget matching."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _provenance() -> dict[str, Any]:
    """Fingerprint runtime and verifier separately so prompt variants remain comparable."""
    runtime = [
        ROOT / name
        for name in [
            "agent.yaml",
            "prompt.md",
            "tools.py",
            "uv.lock",
            "pyproject.toml",
            "modeler/sources.json",
        ]
    ]
    runtime += sorted((ROOT / "modeler").rglob("*.py"))
    runtime += sorted(path for path in (ROOT / "modeler/data").iterdir() if path.is_file())
    verifier = [ROOT / "modeler" / name for name in ["contracts.py", "learning.py"]]
    verifier += sorted(Path(__file__).parent.glob("*.py"))

    def digest(paths: list[Path]) -> str:
        """Identify file contents and their runtime-relative locations."""
        return fingerprint(
            {
                str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in paths
            }
        )

    return {
        "source_hash": digest(runtime),
        "verifier_hash": digest(verifier),
        "environment": {
            "python": platform.python_version(),
            "packages": {
                name: version(name)
                for name in [
                    "numpy",
                    "pandas",
                    "scikit-learn",
                    "joblib",
                    "recurse-sdk",
                    "scipy",
                    "pyarrow",
                ]
            },
        },
    }


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Load a versioned suite and reject cross-partition dataset-family leakage."""
    manifest = json.loads(path.read_text())
    if manifest["version"] != 1 or manifest["measurement_protocol"] != PROTOCOL:
        raise ValueError("Unsupported benchmark version or measurement protocol.")
    families: dict[str, str] = {}
    ids: set[str] = set()
    cases: list[dict[str, Any]] = manifest["cases"]
    for case in cases:
        partition, family = case["partition"], case["dataset_family"]
        if partition not in {"development", "validation", "holdout"}:
            raise ValueError("Unknown benchmark partition.")
        if case["id"] in ids or families.setdefault(family, partition) != partition:
            raise ValueError("Duplicate case ID or dataset family crosses partitions.")
        ids.add(case["id"])
    return cases


def _configurations(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Use three predeclared probes; this is not an autonomous search."""
    kind = case["specification"]["kind"]
    if kind == "forecast":
        period = 12 if case["specification"]["frequency"] == "MS" else 7
        return [
            {"family": "baseline"},
            {"family": "seasonal", "seasonal_period": period},
            {"family": "linear", "lags": [1, period]},
        ]
    return [
        {"family": "baseline"},
        {"family": "linear", **({"threshold": 0.2} if kind == "multilabel" else {})},
        {"family": "extra_trees", "trees": 50},
    ]


def _tools() -> Any:
    """Load the exact example tools without a global module-name collision."""
    spec = importlib.util.spec_from_file_location("benchmark_modeler_tools", ROOT / "tools.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load modeler tools.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def audit(workspace: Path, receipt: dict[str, Any]) -> dict[str, str]:
    """Hash every declared artifact, rejecting paths outside the result directory.

    This is an integrity audit, not independent reproduction of model scores.
    It intentionally never unpickles downloaded artifacts.
    """
    hashes = {}
    for name, relative in receipt["artifacts"].items():
        path = (workspace / relative).resolve()
        if not relative or not path.is_relative_to(workspace.resolve()) or not path.is_file():
            raise ValueError(f"Invalid or missing artifact {name}: {relative!r}")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def run(case: dict[str, Any], workspace: Path, design: str, repeat: int = 0) -> dict[str, Any]:
    """Run fixed probes with evaluator-supplied truth and preserve failed attempts.

    Use a fresh output directory for every attempt. The design identifier must
    identify the source revision/environment; repetitions do not change the split.
    """
    request = case["request"]
    if not 1 <= request["budget"]["max_trials"] <= _MAX_PROBES:
        raise ValueError("Fixed-search baseline supports one to three trials.")
    workspace.mkdir(parents=True, exist_ok=False)
    configurations = _configurations(case)[: request["budget"]["max_trials"]]
    result: dict[str, Any] = {
        "case_id": case["id"],
        "dataset_family": case["dataset_family"],
        "partition": case["partition"],
        "repeat": repeat,
        "design": design,
        "mode": "fixed-search-with-ground-truth",
        "protocol": PROTOCOL,
        "request_hash": fingerprint(request),
        "specification_hash": fingerprint(case["specification"]),
        "budget_hash": fingerprint(request["budget"]),
        "policy_hash": fingerprint(configurations),
        "data_hash": None,
        "status": "failed",
        "diagnostic": None,
        "evaluation": {},
        "artifacts": {},
        "assessment": {"verified": False, "issues": ["Run has not produced an accepted model."]},
        "trial_count": 0,
        "training_seconds": 0.0,
        "monetary_cost": None,
        **_provenance(),
    }
    started = time.monotonic()
    _activate({key: value for key, value in request.items() if key != "task"}, workspace)
    try:
        pinned_data = None
        if "source_sha256" in case:
            pinned_data = _download(request["dataset"])
            actual = hashlib.sha256(pinned_data).hexdigest()
            if actual != case["source_sha256"]:
                raise ValueError("Pinned public dataset checksum mismatch.")
        actions = _tools()
        actions.review_inputs(request["task"], request["quality"], [], [])
        actions.inspect_dataset()
        actions.resolve_problem(case["specification"])
        frame = pd.read_parquet(workspace / ".modeler/data.parquet")
        if pinned_data is not None and not pd.read_csv(io.BytesIO(pinned_data)).equals(frame):
            raise ValueError("Loaded dataset differs from the pinned public source.")
        result["data_hash"] = hashlib.sha256(
            (workspace / ".modeler/data.parquet").read_bytes()
        ).hexdigest()
        for configuration in configurations:
            if result["training_seconds"] >= request["budget"]["max_training_seconds"]:
                break
            trial = actions.train_candidate(
                configuration, "Predeclared fixed-search baseline probe."
            )
            result["trial_count"] += 1
            result["training_seconds"] += trial["seconds"]
            if trial["status"] == "trained":
                actions.evaluate_candidate(trial["id"])
        receipt = actions.finish_run(
            "budget_exhausted",
            "All predeclared fixed-search probes attempted; no autonomous reasoning.",
        )
        result["status"] = receipt["status"]
        result["artifacts"] = audit(workspace, receipt)
        result["evaluation"] = json.loads((workspace / "evaluation.json").read_text())
        result["assessment"] = assess(case, workspace, frame)
    except Exception as error:  # preserve failures in the denominator, including tool exceptions
        result["status"] = "failed"
        result["diagnostic"] = f"{type(error).__name__}: {error}"
    finally:
        _deactivate()
        result["wall_seconds"] = time.monotonic() - started
        (workspace / "benchmark-result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def compare(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair identical requests and repeats, retaining unmatched and failed results.

    Return per-case measurements instead of averaging incompatible metric units.
    Unknown data hashes on failed runs remain visible, never treated as equality.
    """

    def indexed(results: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
        """Require one attempt per case and repeat on each side."""
        index = {(row["case_id"], row["repeat"]): row for row in results}
        if len(index) != len(results):
            raise ValueError("Duplicate case/repeat in comparison.")
        return index

    first, second = indexed(left), indexed(right)
    output = []
    for key in sorted(first.keys() | second.keys()):
        a, b = first.get(key), second.get(key)
        paired = a is not None and b is not None
        mismatch = []
        if a is not None and b is not None:
            mismatch = [
                field
                for field in (
                    "request_hash",
                    "specification_hash",
                    "budget_hash",
                    "protocol",
                    "partition",
                    "dataset_family",
                    "verifier_hash",
                    "environment",
                )
                if a[field] != b[field]
            ]
            if not a["data_hash"] or not b["data_hash"] or a["data_hash"] != b["data_hash"]:
                mismatch.append("data_hash")
        output.append(
            {
                "case_id": key[0],
                "repeat": key[1],
                "paired": paired,
                "comparable": a is not None
                and b is not None
                and not mismatch
                and a["status"] == b["status"] == "succeeded"
                and a["assessment"]["verified"]
                and b["assessment"]["verified"],
                "mismatch": mismatch,
                "left": a,
                "right": b,
            }
        )
    return output


def main(argv: list[str] | None = None) -> None:
    """Run one bounded local case or compare saved result files."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    local = sub.add_parser("run")
    local.add_argument("case")
    local.add_argument("output", type=Path)
    local.add_argument("--design", required=True)
    local.add_argument("--repeat", type=int, default=0)
    local.add_argument("--manifest", type=Path, default=Path(__file__).with_name("cases.json"))
    comparison = sub.add_parser("compare")
    comparison.add_argument("left", type=Path)
    comparison.add_argument("right", type=Path)
    args = parser.parse_args(argv)
    if args.command == "run":
        cases = {case["id"]: case for case in load_cases(args.manifest)}
        result = run(cases[args.case], args.output.resolve(), args.design, args.repeat)
        print(json.dumps(result, indent=2))
    else:
        print(
            json.dumps(
                compare(json.loads(args.left.read_text()), json.loads(args.right.read_text())),
                indent=2,
            )
        )
