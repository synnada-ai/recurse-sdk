"""Freeze isolated baseline/contender applications and public requests for the pilot."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import yaml

from .runner import ROOT, fingerprint, load_cases

__all__ = ["main", "prepare"]


def prepare(destination: Path) -> dict[str, Any]:
    """Create a reviewable eight-run plan without submitting runs or spending credits.

    Applications contain only runtime files. Evaluator specifications remain outside
    each application and the public request contains only the agent input contract.
    Existing destinations are never overwritten.
    """
    destination.mkdir(parents=True, exist_ok=False)
    cases = {case["id"]: case for case in load_cases(Path(__file__).with_name("cases.json"))}
    appendix = Path(__file__).with_name("complexity-aware.md").read_text()
    files = ["agent.yaml", "prompt.md", "tools.py", "pyproject.toml", "uv.lock", "README.md"]
    applications = {}
    for variant in ["baseline", "complexity-aware"]:
        app = destination / variant
        app.mkdir()
        for name in files:
            shutil.copy2(ROOT / name, app / name)
        shutil.copytree(
            ROOT / "modeler", app / "modeler", ignore=shutil.ignore_patterns("__pycache__")
        )
        manifest_path = app / "agent.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["agent"]["model"] = "gpt-5.6-luna"
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
        if variant == "complexity-aware":
            with (app / "prompt.md").open("a") as stream:
                stream.write(appendix)
        applications[variant] = fingerprint(
            {
                str(path.relative_to(app)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(app.rglob("*"))
                if path.is_file()
            }
        )
    requests = destination / "requests"
    requests.mkdir()
    for name in ["penguins", "mpg"]:
        (requests / f"{name}.json").write_text(json.dumps(cases[name]["request"], indent=2) + "\n")
    # Alternate order across repeats to reduce systematic execution-order effects.
    schedule = [
        {"case": name, "repeat": repeat, "variant": variant}
        for repeat in range(2)
        for name in ["penguins", "mpg"]
        for variant in (
            ["baseline", "complexity-aware"] if repeat == 0 else ["complexity-aware", "baseline"]
        )
    ]
    plan = {
        "applications": applications,
        "cases": {name: cases[name] for name in ["penguins", "mpg"]},
        "schedule": schedule,
        "model": "gpt-5.6-luna",
        "cpu": 1,
        "memory_mib": 2048,
        "parallelism": 1,
        "cloud_runs_launched": 0,
    }
    (destination / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    return plan


def main() -> None:
    """Write the prepared pilot into a new directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.destination), indent=2))


if __name__ == "__main__":
    main()
