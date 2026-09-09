"""Shared fixtures for the SDK test suite."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

VALID_MANIFEST: dict[str, Any] = {
    "api_version": "recurse.run/v1alpha1",
    "kind": "Agent",
    "metadata": {
        "name": "receipt-writer",
        "title": "Receipt Writer",
        "summary": "Writes one receipt file.",
    },
    "runtime": {"python": "3.14", "lockfile": "uv.lock"},
    "agent": {"prompt": "prompt.md"},
    "inputs": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["item"],
        "properties": {
            "item": {"type": "string"},
            "task": {"type": "string", "default": "Write a receipt for {{ input.item }}."},
        },
        "additionalProperties": False,
    },
    "outputs": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["receipt"],
        "properties": {"receipt": {"type": "string"}},
        "additionalProperties": False,
    },
    "tools": {"source": "tools.py", "register": {"write_receipt": None}},
}

VALID_TOOL_SOURCE = '''"""Receipt tools."""

import recurse


def write_receipt(item: str, count: int = 1) -> str:
    """Write a receipt file into the run workspace.

    Args:
        item: Item name to record.
        count: Number of items purchased.

    Returns:
        The receipt text.
    """
    text = f"{count} x {item}"
    (recurse.context().workspace / "receipt.txt").write_text(text)
    return text
'''


def write_app(directory: Path, manifest: dict[str, Any] | None = None) -> Path:
    """Write a complete valid application into ``directory`` and return it."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "agent.yaml").write_text(
        yaml.safe_dump(manifest or VALID_MANIFEST, sort_keys=False)
    )
    (directory / "prompt.md").write_text("Write exactly the requested receipt.\n")
    (directory / "uv.lock").write_text("version = 1\n")
    (directory / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n'
        '[project]\nname = "receipt-writer"\nversion = "0.1.0"\n'
        'description = "Receipt writer fixture"\n\n'
        "[tool.hatch.build.targets.sdist]\n"
        'include = ["agent.yaml", "prompt.md", "tools.py", "uv.lock"]\n'
    )
    (directory / "tools.py").write_text(VALID_TOOL_SOURCE)
    return directory


@pytest.fixture
def app(tmp_path: Path) -> Path:
    """A valid application directory."""
    return write_app(tmp_path / "app")


@pytest.fixture
def edit_manifest(app: Path) -> Callable[[Callable[[dict[str, Any]], None]], Path]:
    """Apply a mutation to the app manifest and return the app directory."""

    def apply(mutate: Callable[[dict[str, Any]], None]) -> Path:
        """Mutate the manifest in place and rewrite agent.yaml."""
        manifest = yaml.safe_load((app / "agent.yaml").read_text())
        mutate(manifest)
        (app / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
        return app

    return apply
