"""Benchmark integrity, failure accounting, and bounded fixed-search behavior."""

import copy
import hashlib
import importlib.util
import io
import json
import runpy
import sys
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pandas as pd
import pytest
from benchmarks import runner

from recurse import build_bundle

ROOT = Path(__file__).parents[1]


def _case() -> dict[str, Any]:
    """Return one frozen public-data pilot request."""
    return runner.load_cases(ROOT / "benchmarks/cases.json")[-1]


def test_manifest_and_search_policies() -> None:
    """Each modality has a bounded policy and suite fingerprints ignore JSON key order."""
    cases = runner.load_cases(ROOT / "benchmarks/cases.json")
    assert len(cases) == 8
    assert {case["partition"] for case in cases} == {"development"}
    for case in cases:
        assert len(runner._configurations(case)) == 3
    assert runner.fingerprint({"a": 1, "b": 2}) == runner.fingerprint({"b": 2, "a": 1})
    assert callable(runner._tools().train_candidate)


@pytest.mark.parametrize("change", ["version", "protocol", "partition", "duplicate", "family"])
def test_manifest_rejects_invalid_partitions_and_versions(tmp_path: Path, change: str) -> None:
    """A family cannot cross partitions, and duplicate IDs cannot conceal cases."""
    manifest = json.loads((ROOT / "benchmarks/cases.json").read_text())
    if change == "version":
        manifest["version"] = 2
    elif change == "protocol":
        manifest["measurement_protocol"] = "unknown"
    elif change == "partition":
        manifest["cases"][0]["partition"] = "unknown"
    elif change == "duplicate":
        manifest["cases"].append(manifest["cases"][0])
    else:
        duplicate = copy.deepcopy(manifest["cases"][0])
        duplicate.update(id="alternate", partition="holdout")
        manifest["cases"].append(duplicate)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        runner.load_cases(path)


def test_missing_tools_module_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken tool location produces an explicit import failure."""
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *args: None)
    with pytest.raises(RuntimeError, match="Cannot load"):
        runner._tools()


@pytest.mark.parametrize("path", ["", "missing", "../outside", "."])
def test_artifact_audit_rejects_bad_paths(tmp_path: Path, path: str) -> None:
    """Only existing files inside the result workspace are accepted."""
    with pytest.raises(ValueError, match="Invalid or missing"):
        runner.audit(tmp_path, {"artifacts": {"model": path}})


def test_artifact_hashes_cover_bytes(tmp_path: Path) -> None:
    """Artifact receipts include deterministic byte checksums."""
    (tmp_path / "model").write_bytes(b"test")
    assert runner.audit(tmp_path, {"artifacts": {"model": "model"}}) == {
        "model": hashlib.sha256(b"test").hexdigest()
    }


@pytest.mark.parametrize(
    "scenario",
    ["success", "download_failure", "changed", "no_pin", "loaded_changed", "time_exhausted"],
)
def test_fixed_search_preserves_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """Failed attempts retain their identity and result record; successful fits are evaluated."""
    case = _case()
    case["source_sha256"] = hashlib.sha256(b"x\n1\n2\n").hexdigest()
    if scenario == "no_pin":
        del case["source_sha256"]
    download = Mock(return_value=b"changed" if scenario == "changed" else b"x\n1\n2\n")
    if scenario == "download_failure":
        download.side_effect = TimeoutError("source timeout")
    monkeypatch.setattr(runner, "_download", download)
    monkeypatch.setattr(runner, "assess", lambda *args: {"verified": True, "issues": []})
    actions = Mock()
    actions.train_candidate.side_effect = [
        {"status": "trained", "id": 1, "seconds": 1},
        {"status": "failed", "id": 2, "seconds": 2},
        {"status": "trained", "id": 3, "seconds": 3},
    ]
    if scenario == "time_exhausted":
        actions.train_candidate.side_effect = [{"status": "trained", "id": 1, "seconds": 60}]
    actions.evaluate_candidate.side_effect = [
        {"status": "evaluated", "id": 1, "seconds": 60 if scenario == "time_exhausted" else 4},
        {"status": "evaluated", "id": 3, "seconds": 6},
    ]
    actions.finish_run.return_value = {"status": "succeeded", "artifacts": {"model": "model"}}
    monkeypatch.setattr(runner, "_tools", lambda: actions)
    monkeypatch.setattr(
        pd,
        "read_parquet",
        lambda _: pd.DataFrame({"x": [1, 3 if scenario == "loaded_changed" else 2]}),
    )
    workspace = tmp_path / "attempt"

    def inspect() -> None:
        """Save the controlled source observations at the real tool boundary."""
        (workspace / "evaluation.json").write_text('{"final_test": {"mae:{}": 2}}')
        (workspace / "model").write_bytes(b"model")
        (workspace / ".modeler").mkdir(exist_ok=True)
        (workspace / ".modeler/data.parquet").write_bytes(b"frame")

    actions.inspect_dataset.side_effect = inspect
    result = runner.run(case, workspace, "test-source")
    assert json.loads((workspace / "benchmark-result.json").read_text()) == result
    assert result["request_hash"] == runner.fingerprint(case["request"])
    assert result["mode"] == "fixed-search-with-ground-truth"
    if scenario in {"success", "no_pin", "time_exhausted"}:
        assert result["status"] == "succeeded"
        assert result["diagnostic"] is None
        assert result["training_seconds"] == (60 if scenario == "time_exhausted" else 12)
        assert result["evaluation"] == {"final_test": {"mae:{}": 2}}
        assert actions.evaluate_candidate.call_count == (1 if scenario == "time_exhausted" else 2)
    else:
        assert result["status"] == "failed"
        assert result["data_hash"] is None
        assert result["diagnostic"]
        assert not actions.train_candidate.called
    with pytest.raises(FileExistsError):
        runner.run(case, workspace, "test-source")


def test_fixed_search_rejects_excess_budget(tmp_path: Path) -> None:
    """The fixed policy cannot silently spend or truncate an unbounded search budget."""
    case = _case()
    case["request"]["budget"]["max_trials"] = 4
    with pytest.raises(ValueError, match="one to three"):
        runner.run(case, tmp_path / "attempt", "source")


def test_pairing_keeps_missing_failed_and_incompatible_results() -> None:
    """Comparison never drops failures or treats changed problem definitions as matched."""
    row = dict.fromkeys(
        [
            "request_hash",
            "specification_hash",
            "budget_hash",
            "protocol",
            "partition",
            "dataset_family",
            "data_hash",
            "verifier_hash",
            "environment",
        ],
        "same",
    ) | {"case_id": "a", "repeat": 0, "status": "failed"}
    changed = row | {"budget_hash": "changed", "data_hash": None}
    rows = runner.compare([row, row | {"case_id": "missing"}], [changed])
    assert rows[0]["paired"]
    assert rows[0]["mismatch"] == ["budget_hash", "data_hash"]
    assert rows[0]["left"]["status"] == "failed"
    assert not rows[1]["paired"]
    assert runner.compare([row], [row])[0]["mismatch"] == []
    accepted = row | {"status": "succeeded", "assessment": {"verified": True}}
    assert runner.compare([accepted], [accepted])[0]["comparable"]
    assert not runner.compare([accepted], [accepted | {"assessment": {"verified": False}}])[0][
        "comparable"
    ]
    assert not runner.compare([row | {"data_hash": None}], [row | {"data_hash": None}])[0][
        "comparable"
    ]
    assert not runner.compare([], [row])[0]["comparable"]
    with pytest.raises(ValueError, match="Duplicate"):
        runner.compare([row, row], [])


def test_cli_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """Both entry points serialize machine-readable results."""
    monkeypatch.setattr(runner, "run", lambda *args: {"status": "failed"})
    runner.main(["run", "mpg", str(tmp_path / "run"), "--design", "test"])
    assert json.loads(capsys.readouterr().out) == {"status": "failed"}
    path = tmp_path / "results.json"
    path.write_text("[]")
    monkeypatch.setattr(sys, "argv", ["benchmarks", "compare", str(path), str(path)])
    runpy.run_module("benchmarks", run_name="__main__")
    assert json.loads(capsys.readouterr().out) == []


def test_benchmark_ground_truth_is_excluded_from_deployment() -> None:
    """Expected interpretations and evaluation cases never reach the agent bundle."""
    artifacts, _ = build_bundle(ROOT)
    with tarfile.open(fileobj=io.BytesIO(artifacts["source"]), mode="r:gz") as archive:
        assert not any("benchmarks" in Path(name).parts for name in archive.getnames())
