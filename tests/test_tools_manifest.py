"""Tools-only manifests validate without prompts or executable imports."""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

import recurse
from recurse import ManifestError

TOOLS_MANIFEST: dict[str, Any] = {
    "apiVersion": "recurse.tools/v1alpha1",
    "kind": "ToolService",
    "metadata": {"name": "fixture-tools", "title": "Fixture Tools", "summary": "JSON fixtures."},
    "runtime": {"python": "3.14", "lockfile": "uv.lock"},
    "tools": {
        "source": "tools.py",
        "register": {
            "echo": {
                "description": "Return a message.",
                "input_schema": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
                "pricing": {
                    "provider_basis_micro_usd": 0,
                    "compute_basis_micro_usd": 0,
                    "provider_multiplier": 1,
                    "compute_multiplier": 4,
                },
            }
        },
    },
}


def write_tools_manifest(directory: Path, manifest: dict[str, Any]) -> Path:
    """Write a supplied manifest and return its service directory."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tools.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return directory


def test_load_tools_manifest_accepts_explicit_free_tools_without_agent(tmp_path: Path) -> None:
    """A tools-only author can validate a complete declaration without a prompt."""
    service = write_tools_manifest(tmp_path, TOOLS_MANIFEST)

    assert hasattr(recurse, "load_tools_manifest"), "missing public tools manifest loader"
    result = recurse.load_tools_manifest(service)

    assert result == TOOLS_MANIFEST
    assert "agent" not in result
    assert not (service / "agent.yaml").exists()


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("apiVersion",), "recurse.run/v1alpha1", "apiVersion"),
        (("kind",), "Agent", "kind"),
        (("agent",), {"prompt": "prompt.md"}, "unknown field"),
        (("runtime", "python"), "3.13", "runtime.python"),
        (("runtime", "lockfile"), "requirements.txt", "runtime.lockfile"),
        (("tools", "built_in"), True, "unknown field"),
        (("tools", "register", "echo", "volatile"), True, "unknown field"),
        (("tools", "register", "echo", "description"), "", "description"),
        (("tools", "register", "echo", "pricing", "provider_basis_micro_usd"), 1, "pricing"),
        (("tools", "register", "echo", "pricing", "compute_basis_micro_usd"), 1, "pricing"),
        (("tools", "register", "echo", "pricing", "provider_multiplier"), 2, "pricing"),
        (("tools", "register", "echo", "pricing", "compute_multiplier"), 1, "pricing"),
        (("tools", "register", "echo", "input_schema", "type"), "array", "input_schema"),
        (
            ("tools", "register", "echo", "input_schema", "additionalProperties"),
            True,
            "input_schema",
        ),
        (("tools", "register", "echo", "output_schema", "type"), "unknown", "Draft 2020-12"),
        (("tools", "register", "echo", "output_schema", "$ref"), "other.json", "self-contained"),
        (
            ("tools", "register", "echo", "input_schema", "$dynamicRef"),
            "https://example.com/schema",
            "self-contained",
        ),
    ],
)
def test_invalid_tools_declarations_are_rejected(
    tmp_path: Path, path: tuple[str, ...], value: object, message: str
) -> None:
    """Invalid fields cannot be mistaken for a deployable tools-only contract."""
    manifest = copy.deepcopy(TOOLS_MANIFEST)
    parent = manifest
    for part in path[:-1]:
        parent = parent[part]
    parent[path[-1]] = value
    write_tools_manifest(tmp_path, manifest)

    with pytest.raises(ManifestError, match=message):
        recurse.load_tools_manifest(tmp_path)


def test_schema_allows_self_contained_local_references(tmp_path: Path) -> None:
    """A declared schema can reuse a local definition without network discovery."""
    manifest = copy.deepcopy(TOOLS_MANIFEST)
    schema = manifest["tools"]["register"]["echo"]["input_schema"]
    schema["$defs"] = {"message": {"type": "string"}}
    schema["properties"]["message"] = {"$ref": "#/$defs/message"}
    write_tools_manifest(tmp_path, manifest)

    assert (
        recurse.load_tools_manifest(tmp_path)["tools"]["register"]["echo"]["input_schema"] == schema
    )


@pytest.mark.parametrize(
    "apis",
    [
        [],
        ["exa.web.search", "firecrawl.web.scrape"],
        [f"provider.read_{index}" for index in range(20)],
    ],
)
def test_optional_read_api_declarations_preserve_exact_ids(tmp_path: Path, apis: list[str]) -> None:
    """Functions declare bounded API authority without authoring credentials or prices."""
    manifest = copy.deepcopy(TOOLS_MANIFEST)
    manifest["tools"]["register"]["echo"]["apis"] = apis
    write_tools_manifest(tmp_path, manifest)
    assert recurse.load_tools_manifest(tmp_path)["tools"]["register"]["echo"]["apis"] == apis


@pytest.mark.parametrize(
    "apis",
    [
        "exa.web.search",
        [1],
        [""],
        ["a" * 129],
        ["exa.web.search", "exa.web.search"],
        [f"provider.read_{index}" for index in range(21)],
        ["exa.web.search\n"],
        ["provider/read"],
        ["provider.read?key=secret"],
        ["próvider.read"],
        ["_provider.read"],
        [".provider.read"],
        ["-provider.read"],
        [{"id": "exa.web.search", "price": 0}],
    ],
)
def test_invalid_api_declarations_cannot_reach_preparation(tmp_path: Path, apis: Any) -> None:
    """Duplicate, unbounded or non-identifier authority is rejected in the public manifest."""
    manifest = copy.deepcopy(TOOLS_MANIFEST)
    manifest["tools"]["register"]["echo"]["apis"] = apis
    write_tools_manifest(tmp_path, manifest)
    with pytest.raises(ManifestError, match="apis"):
        recurse.load_tools_manifest(tmp_path)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("apiVersion: [", "valid YAML"),
        ("[]", "mapping"),
        ("kind: ToolService\nkind: Agent\n", "tools.yaml has a duplicate key"),
        ("metadata: {summary: 2026-10-13}\n", "JSON-portable"),
        ("metadata: {summary: .nan}\n", "JSON-portable"),
        ('metadata: {summary: "\\uD800"}\n', "UTF-8 encodable"),
        ("&cycle {metadata: *cycle}\n", "valid YAML"),
    ],
)
def test_malformed_tools_yaml_is_rejected(tmp_path: Path, text: str, message: str) -> None:
    """Malformed or nonportable YAML cannot reach the service builder."""
    (tmp_path / "tools.yaml").write_text(text)

    with pytest.raises(ManifestError, match=message):
        recurse.load_tools_manifest(tmp_path)


def test_missing_tools_manifest_is_a_named_error(tmp_path: Path) -> None:
    """Authors receive the tools filename when the manifest is absent."""
    with pytest.raises(ManifestError, match=r"tools\.yaml was not found"):
        recurse.load_tools_manifest(tmp_path)


def test_non_utf8_tools_manifest_is_rejected(tmp_path: Path) -> None:
    """Invalid input bytes never produce a replacement declaration."""
    (tmp_path / "tools.yaml").write_bytes(b"\xff")
    with pytest.raises(ManifestError, match="valid UTF-8"):
        recurse.load_tools_manifest(tmp_path)


def test_unreadable_tools_manifest_is_a_named_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operating-system read failure identifies the tools manifest."""
    write_tools_manifest(tmp_path, TOOLS_MANIFEST)

    def fail_read(path: Path, *args: object, **kwargs: object) -> str:
        """Represent a filesystem read error at the file boundary."""
        raise OSError("blocked")

    monkeypatch.setattr(Path, "read_text", fail_read)
    with pytest.raises(ManifestError, match=r"tools\.yaml could not be read"):
        recurse.load_tools_manifest(tmp_path)


def test_yaml_with_an_unhashable_mapping_key_is_rejected(tmp_path: Path) -> None:
    """Invalid YAML mapping keys produce the SDK's author-facing error type."""
    (tmp_path / "tools.yaml").write_text("? [invalid, key]\n: value\n")
    with pytest.raises(ManifestError, match="valid YAML"):
        recurse.load_tools_manifest(tmp_path)
