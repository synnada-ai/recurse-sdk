"""Tests for manifest parsing and validation."""

import json
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

import recurse
from recurse import ManifestError, load_manifest

Edit = Callable[[Callable[[dict[str, Any]], None]], Path]


def test_valid_manifest_round_trips_the_authored_document(app: Path) -> None:
    """Loading returns exactly what the author wrote."""
    manifest = load_manifest(app)
    assert manifest == yaml.safe_load((app / "agent.yaml").read_text())


@pytest.mark.parametrize("model", ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"])
def test_model_selection_survives_manifest_loading(edit_manifest: Edit, model: str) -> None:
    """The declared specialist model is retained without importing application code."""
    app = edit_manifest(lambda manifest: manifest["agent"].__setitem__("model", model))
    (app / "tools.py").write_text("raise RuntimeError('must not import')\n")
    assert load_manifest(app)["agent"]["model"] == model


def test_manifest_is_read_without_importing_tool_code(app: Path) -> None:
    """Manifest parsing never executes the tool module."""
    (app / "tools.py").write_text("raise RuntimeError('tools were imported')\n")
    assert load_manifest(app)["metadata"]["name"] == "receipt-writer"


def test_missing_manifest_names_the_expected_file(tmp_path: Path) -> None:
    """A directory without agent.yaml is reported by file name."""
    with pytest.raises(ManifestError, match=r"agent\.yaml was not found"):
        load_manifest(tmp_path)


def test_unparseable_yaml_is_an_authoring_error(app: Path) -> None:
    """Broken YAML is a manifest error, not a parser traceback."""
    (app / "agent.yaml").write_text("[broken")
    with pytest.raises(ManifestError, match=r"agent\.yaml is not valid YAML"):
        load_manifest(app)


def test_non_utf8_manifest_is_an_authoring_error(app: Path) -> None:
    """Invalid UTF-8 bytes are reported without leaking a decoder traceback."""
    (app / "agent.yaml").write_bytes(b"metadata:\n  summary: caf\xe9\n")

    with pytest.raises(ManifestError, match="UTF-8"):
        load_manifest(app)


def test_duplicate_yaml_keys_are_rejected(app: Path) -> None:
    """A repeated mapping key is refused instead of silently overwritten."""
    (app / "agent.yaml").write_text("kind: Agent\nkind: Agent\n")
    with pytest.raises(ManifestError, match="duplicate key"):
        load_manifest(app)


def test_non_mapping_document_is_rejected(app: Path) -> None:
    """A YAML list at the top level is refused."""
    (app / "agent.yaml").write_text("- item\n")
    with pytest.raises(ManifestError, match=r"agent\.yaml must be a mapping"):
        load_manifest(app)


def test_surrogate_text_is_rejected_as_an_encoding_problem(app: Path) -> None:
    """Lone surrogates are reported as a UTF-8 problem before field checks."""
    (app / "agent.yaml").write_text(
        (app / "agent.yaml").read_text().replace("Writes one receipt file.", '"\\uD800"')
    )
    with pytest.raises(ManifestError, match="UTF-8"):
        load_manifest(app)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda m: m.pop("metadata"), r"^metadata is required$"),
        (lambda m: m.pop("outputs"), r"^outputs is required$"),
        (lambda m: m.__setitem__("extra", 1), r"^agent\.yaml has an unknown field: extra$"),
        (lambda m: m["metadata"].pop("name"), r"^metadata\.name is required$"),
        (
            lambda m: m["metadata"].__setitem__("owner", "x"),
            r"^metadata has an unknown field: owner$",
        ),
        (lambda m: m["metadata"].__setitem__("name", ""), r"^metadata\.name must be a non-empty"),
        (lambda m: m["metadata"].__setitem__("name", 3), r"^metadata\.name must be a string$"),
        (lambda m: m.__setitem__("metadata", []), r"^metadata must be a mapping$"),
        (
            lambda m: m.__setitem__("api_version", "recurse.run/v2"),
            r"^api_version must be recurse\.run/v1alpha1$",
        ),
        (lambda m: m.__setitem__("kind", "Robot"), r"^kind must be Agent$"),
        (
            lambda m: m["runtime"].__setitem__("python", "3.13"),
            r"^runtime\.python must be one of: 3\.14$",
        ),
        (
            lambda m: m["runtime"].__setitem__("lockfile", "other.lock"),
            r"^runtime\.lockfile must be uv\.lock$",
        ),
        (
            lambda m: m["agent"].__setitem__("model", ""),
            r"^agent\.model must be a non-empty string$",
        ),
        (lambda m: m["agent"].__setitem__("model", None), r"^agent\.model must be a string$"),
        (lambda m: m["agent"].__setitem__("model", 3), r"^agent\.model must be a string$"),
        (
            lambda m: m["agent"].__setitem__("parameters", {}),
            r"^agent has an unknown field: parameters$",
        ),
        (
            lambda m: m["agent"].pop("prompt"),
            r"^agent\.prompt is required$",
        ),
        (
            lambda m: m["inputs"].__setitem__("rounds", {1, 2}),
            r"^inputs must contain only JSON-portable values$",
        ),
        (
            lambda m: m["inputs"].__setitem__(3, "x"),
            r"^inputs must contain only JSON-portable values$",
        ),
        (lambda m: m.__setitem__("inputs", "schema"), r"^inputs must be a mapping$"),
        (
            lambda m: m["inputs"].__setitem__("type", b"bytes"),
            r"^inputs must contain only JSON-portable values$",
        ),
        (
            lambda m: m["inputs"].__setitem__("type", "not-a-type"),
            r"^inputs is not a valid Draft 2020-12 schema$",
        ),
        (
            lambda m: m["outputs"].__setitem__("type", b"bytes"),
            r"^outputs must contain only JSON-portable values$",
        ),
        (lambda m: m.__setitem__("outputs", "schema"), r"^outputs must be a mapping$"),
        (
            lambda m: m["outputs"].__setitem__("type", "array"),
            r"^outputs\.type must be object$",
        ),
        (
            lambda m: m["outputs"]["properties"]["receipt"].__setitem__("type", "not-a-type"),
            r"^outputs is not a valid Draft 2020-12 schema$",
        ),
        (lambda m: m["tools"].__setitem__("register", {}), r"^tools\.register must not be empty$"),
        (
            lambda m: m["tools"].__setitem__("register", ["write_receipt"]),
            r"^tools\.register must be a mapping$",
        ),
        (
            lambda m: m["tools"].__setitem__("register", {"write_receipt": []}),
            r"^tools\.register\.write_receipt must be a mapping or null$",
        ),
        (
            lambda m: m["tools"].__setitem__("register", {"write_receipt": {"volatile": 1}}),
            r"^tools\.register\.write_receipt\.volatile must be a boolean$",
        ),
        (
            lambda m: m["tools"].__setitem__("register", {"write_receipt": {"speed": 1}}),
            r"^tools\.register\.write_receipt has an unknown field: speed$",
        ),
        (
            lambda m: m["tools"].__setitem__("built-in", "yes"),
            r"^tools\.built-in must be a boolean$",
        ),
        (
            lambda m: m["tools"].__setitem__("defaults", {"storable": None}),
            r"^tools\.defaults\.storable must be a boolean$",
        ),
        (
            lambda m: m["tools"].__setitem__("defaults", {"cache": True}),
            r"^tools\.defaults has an unknown field: cache$",
        ),
        (
            lambda m: m["tools"].__setitem__("register", {3: None}),
            r"^tools\.register keys must be strings$",
        ),
        (
            lambda m: m["tools"].__setitem__("register", {"": None}),
            r"^tools\.register keys must be non-empty strings$",
        ),
    ],
)
def test_invalid_manifests_name_the_offending_field(
    edit_manifest: Edit, mutation: Callable[[dict[str, Any]], None], message: str
) -> None:
    """Every contract violation names the exact offending field."""
    app = edit_manifest(mutation)
    with pytest.raises(ManifestError, match=message):
        load_manifest(app)


@pytest.mark.parametrize("model", ["gpt-5.6", "astra", "unknown-model"])
def test_unsupported_model_is_rejected_locally(edit_manifest: Edit, model: str) -> None:
    """Reject unsupported selections before building or uploading an application."""
    app = edit_manifest(lambda m: m["agent"].update(model=model))
    with pytest.raises(recurse.ManifestError, match=r"agent\.model must be one of"):
        load_manifest(app)


@pytest.mark.parametrize("pure_args", [None, [], ["item"]])
def test_storage_settings_survive_loading(edit_manifest: Edit, pure_args: object) -> None:
    """Keep omission distinct from the all-pure null shorthand."""
    settings = {"no_storage": ["count"], "pure_args": pure_args}
    app = edit_manifest(lambda m: m["tools"].update(defaults=settings))
    assert load_manifest(app)["tools"]["defaults"] == settings


def test_optional_tool_settings_are_accepted(edit_manifest: Edit) -> None:
    """built-in, defaults, and per-tool settings are all accepted together."""
    app = edit_manifest(
        lambda m: m["tools"].update(
            {
                "built-in": True,
                "defaults": {"volatile": False},
                "register": {"write_receipt": {"storable": False, "volatile": True}},
            }
        )
    )
    manifest = load_manifest(app)
    assert manifest["tools"]["built-in"] is True


@pytest.mark.parametrize(
    "key,value",
    [("pure_args", True), ("pure_args", [3]), ("no_storage", None), ("no_storage", [""])],
)
def test_invalid_storage_settings_are_authoring_errors(
    edit_manifest: Edit, key: str, value: object
) -> None:
    """Malformed parameter lists produce a manifest error, not an internal exception."""
    app = edit_manifest(lambda m: m["tools"].update(defaults={key: value}))
    with pytest.raises(recurse.ManifestError, match=key):
        load_manifest(app)


def test_manifest_schema_is_a_valid_standalone_package_resource(app: Path) -> None:
    """The installed SDK carries the same JSON Schema used for validation."""
    resource = files(recurse).joinpath("agent.schema.json")
    schema = json.loads(resource.read_text(encoding="utf-8"))

    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(load_manifest(app))


def test_schema_documents_every_public_property() -> None:
    """Every public manifest property is described in the canonical schema."""
    schema = json.loads(files(recurse).joinpath("agent.schema.json").read_text(encoding="utf-8"))

    def check(node: object, path: str) -> None:
        """Recursively assert descriptions on declared properties."""
        if not isinstance(node, dict):
            return
        properties = node.get("properties", {})
        if isinstance(properties, dict):
            for name, field in properties.items():
                field_path = f"{path}.{name}" if path else name
                assert isinstance(field, dict)
                assert field.get("description"), field_path
                check(field, field_path)
        for definition in node.get("$defs", {}).values():
            check(definition, path)

    check(schema, "")


def test_non_finite_numbers_are_not_json_portable(app: Path) -> None:
    """An .inf value inside inputs is rejected as non-portable."""
    (app / "agent.yaml").write_text(
        (app / "agent.yaml").read_text().replace("type: object", "budget: .inf\n  type: object")
    )
    with pytest.raises(ManifestError, match=r"^inputs must contain only JSON-portable values$"):
        load_manifest(app)


def test_non_finite_output_schema_values_are_not_json_portable(app: Path) -> None:
    """An .inf value inside outputs is rejected as non-portable."""
    (app / "agent.yaml").write_text(
        (app / "agent.yaml").read_text().replace("outputs:\n", "outputs:\n  budget: .inf\n", 1)
    )
    with pytest.raises(ManifestError, match=r"^outputs must contain only JSON-portable values$"):
        load_manifest(app)


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
def test_output_schema_references_must_stay_inside_the_manifest(app: Path, keyword: str) -> None:
    """A frozen output declaration cannot depend on an external schema."""
    manifest = yaml.safe_load((app / "agent.yaml").read_text())
    manifest["outputs"][keyword] = "https://example.com/output.json"
    (app / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    with pytest.raises(ManifestError, match=r"^outputs must be self-contained$"):
        load_manifest(app)


def test_cyclic_list_values_are_not_json_portable(app: Path) -> None:
    """A self-referencing list inside inputs is rejected without recursing forever."""
    (app / "agent.yaml").write_text(
        (app / "agent.yaml")
        .read_text()
        .replace("type: object", "loop: &loop [*loop]\n  type: object", 1)
    )
    with pytest.raises(ManifestError, match=r"^inputs must contain only JSON-portable values$"):
        load_manifest(app)


def test_cyclic_values_are_a_clear_authoring_error(app: Path) -> None:
    """A self-referencing mapping fails as invalid YAML, not a crash."""
    text = (app / "agent.yaml").read_text()
    (app / "agent.yaml").write_text(
        text.replace("inputs:", "inputs: &loop {self: *loop}\nunused:", 1)
    )
    with pytest.raises(ManifestError, match=r"agent\.yaml is not valid YAML"):
        load_manifest(app)


def test_unreadable_manifest_is_a_manifest_error(app: Path) -> None:
    """Permission failures reading agent.yaml surface as a manifest error."""
    (app / "agent.yaml").chmod(0)
    try:
        with pytest.raises(ManifestError, match=r"agent\.yaml could not be read"):
            load_manifest(app)
    finally:
        (app / "agent.yaml").chmod(0o644)
