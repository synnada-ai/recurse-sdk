"""Protect the hosted SDK quality gate."""

import tomllib
from pathlib import Path

import yaml

REPOSITORY = Path(__file__).parents[1]


def test_ci_runs_the_locked_sdk_quality_and_build_gates() -> None:
    """Pull requests and main pushes run every documented SDK check."""
    workflow_path = REPOSITORY / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    job = workflow["jobs"]["checks"]
    steps = job["steps"]
    commands = [step["run"] for step in steps if "run" in step]

    assert set(triggers) == {"pull_request", "push"}
    assert triggers["push"]["branches"] == ["main"]
    assert workflow["permissions"] == {"contents": "read"}
    assert job["runs-on"] == "ubuntu-latest"
    assert any(
        step.get("uses") == "actions/setup-python@v6"
        and step.get("with") == {"python-version": "3.14"}
        for step in steps
    )
    assert commands == [
        "python -m pip install uv==0.9.16",
        "./check.sh",
    ]
    hooks = yaml.safe_load((REPOSITORY / ".pre-commit-config.yaml").read_text())["repos"]
    assert hooks[0]["hooks"][0]["entry"] == "./check.sh"
    assert hooks[0]["hooks"][0]["pass_filenames"] is False
    assert hooks[0]["hooks"][0]["always_run"] is True


def test_tiny_tuner_remains_in_coverage_and_type_checks() -> None:
    """Adding a separately checked example must not drop Tiny Tuner's quality gates."""
    configuration = tomllib.loads(
        (REPOSITORY / "examples/tiny-tuner/tests/pyproject.toml").read_text()
    )
    assert configuration["tool"]["coverage"]["run"]["source"] == [".."]
    assert configuration["tool"]["coverage"]["report"]["fail_under"] == 100
    script = (REPOSITORY / "check.sh").read_text()
    assert "uv run --directory examples/tiny-tuner/tests --locked" in script
    assert "pytest -n auto --maxprocesses=2 --cov --cov-branch" in script
    assert "mypy --config-file pyproject.toml ../tools.py ." in script
