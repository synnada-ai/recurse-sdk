"""Public contract and deployment packaging checks for the Predictive Modeler example."""

import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

import recurse

ROOT = Path(__file__).parents[1] / "examples" / "predictive-modeler"


@pytest.mark.parametrize(
    "name", ["binary", "multiclass", "multilabel", "regression", "forecast", "panel-forecast"]
)
def test_real_dataset_requests_share_one_input_contract(name: str) -> None:
    """Every demonstration request validates against the same manifest."""
    manifest = recurse.load_manifest(ROOT)
    request = json.loads((ROOT / "inputs" / f"{name}.json").read_text())
    Draft202012Validator(manifest["inputs"]).validate(request)
    assert request["dataset"].startswith("example:")
    assert set(request) == {"dataset", "task", "quality", "budget"}


def test_modeler_bundle_contains_runtime_without_test_or_data_payloads() -> None:
    """The deployed archive contains its adapters and pinned sources, but no testing environment."""
    artifacts, _ = recurse.build_bundle(ROOT)
    with tarfile.open(fileobj=io.BytesIO(artifacts["source"]), mode="r:gz") as archive:
        paths = {"/".join(Path(name).parts[1:]) for name in archive.getnames()}
        member = next(item for item in archive.getmembers() if item.name.endswith("/agent.yaml"))
        source = archive.extractfile(member)
        assert source is not None
        with source:
            manifest = yaml.safe_load(source)
    # These proposals have no upstream object producer; requiring references prevents construction.
    settings = manifest["tools"]["register"]
    assert settings["review_inputs"]["no_storage"] == ["task_quality", "conflicts", "questions"]
    assert settings["resolve_problem"]["no_storage"] == ["specification"]
    assert settings["train_candidate"]["no_storage"] == ["configuration"]
    assert {
        "agent.yaml",
        "prompt.md",
        "tools.py",
        "uv.lock",
        "pyproject.toml",
        "modeler/learning.py",
        "modeler/sources.json",
        "modeler/worker.py",
    } <= paths
    assert not any(
        path.startswith(("tests/", ".venv/", ".dataset-cache/")) or "__pycache__" in path
        for path in paths
    )
