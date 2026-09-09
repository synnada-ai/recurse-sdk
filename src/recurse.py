"""Recurse SDK: author, package, and run Recurse applications.

The public surface is intentionally small:

- :func:`build_bundle` validates an application directory and produces one
  standard source distribution containing its exact dependency lockfile.
- :func:`context` gives application tool code access to the active run's
  validated inputs and writable workspace.
- :class:`RecurseError` and its subclasses report every SDK failure.

The ``recurse`` console command that ships with this distribution handles
login, deployment, and billing; see the package documentation.
"""

import ast
import copy
import hashlib
import io
import json
import math
import re
import subprocess
import tarfile
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import Any, cast

import jsonschema
import yaml

__all__ = [
    "BundleError",
    "ManifestError",
    "RecurseError",
    "RunContext",
    "RunContextError",
    "build_bundle",
    "context",
]


class RecurseError(Exception):
    """Base class for every error raised by the Recurse SDK."""


class ManifestError(RecurseError):
    """The application manifest or declared application content is invalid.

    The message names the offending ``agent.yaml`` field or declared file so
    the author can correct it directly.
    """


class BundleError(RecurseError):
    """The application cannot be built into a valid source distribution.

    Raised when project metadata, the selected build backend, or the resulting
    source archive does not satisfy the Recurse application contract.
    """


class RunContextError(RecurseError):
    """The Recurse run context was used outside an active run."""


@dataclass(frozen=True)
class RunContext:
    """A read-only view of the active Recurse run.

    Attributes:
        inputs: The validated run inputs, deeply read-only. Mappings are
            immutable mappings and lists are tuples.
        workspace: The writable run output directory. Every file written under
            it is collected as a run artifact when the run finishes.
    """

    inputs: Mapping[str, object]
    workspace: Path


_active: RunContext | None = None


def context() -> RunContext:
    """Return the run context of the active Recurse run.

    Returns:
        The active :class:`RunContext`.

    Raises:
        RunContextError: If no Recurse run is active, for example when the
            application module is imported or executed outside the platform.
    """
    if _active is None:
        raise RunContextError(
            "no active Recurse run: recurse.context() is only available while "
            "a deployed application is executing"
        )
    return _active


def _freeze(value: object) -> object:
    """Return a deeply read-only copy of a JSON-portable value."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _activate(inputs: Mapping[str, object], workspace: Path) -> None:
    """Activate the run context for one run.

    Args:
        inputs: Validated run inputs to expose read-only.
        workspace: The run output directory.

    Raises:
        RunContextError: If a run context is already active.
    """
    global _active  # noqa: PLW0603 - the context is one process-local active run
    if _active is not None:
        raise RunContextError("a Recurse run context is already active")
    frozen = cast("Mapping[str, object]", _freeze(copy.deepcopy(dict(inputs))))
    _active = RunContext(inputs=frozen, workspace=workspace)


def _deactivate() -> None:
    """Deactivate the run context after a run finishes."""
    global _active  # noqa: PLW0603 - teardown clears that same active-run context
    _active = None


_MANIFEST_SCHEMA = json.loads(
    files(__name__).joinpath("agent.schema.json").read_text(encoding="utf-8")
)
jsonschema.Draft202012Validator.check_schema(_MANIFEST_SCHEMA)
_MANIFEST_VALIDATOR = jsonschema.Draft202012Validator(_MANIFEST_SCHEMA)


class _UniqueKeyLoader(yaml.SafeLoader):
    """A YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: yaml.Loader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    """Build a mapping while rejecting duplicate keys.

    Args:
        loader: The active YAML loader.
        node: The mapping node being constructed.
        deep: Whether nested objects are constructed eagerly.

    Returns:
        The constructed mapping.

    Raises:
        ManifestError: If the mapping repeats a key.
    """
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ManifestError(f"agent.yaml has a duplicate key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _is_json_portable(value: object) -> bool:
    """Report whether a value survives JSON serialization unchanged.

    Args:
        value: The authored value to inspect.

    Returns:
        True when the value is built only from mappings with string keys,
        lists, strings, booleans, finite numbers, and null.
    """
    return _is_portable_value(value, ())


def _is_portable_value(value: object, active: tuple[int, ...]) -> bool:
    """Recursively check portability while refusing cyclic containers.

    Args:
        value: The value to inspect.
        active: Identities of the containers currently being visited; a value
            appearing inside itself is a cycle and is not portable.

    Returns:
        True when the value is acyclic and JSON-portable.
    """
    if isinstance(value, dict | list):
        if id(value) in active:
            return False
        active = (*active, id(value))
        if isinstance(value, dict):
            return all(
                isinstance(key, str) and _is_portable_value(item, active)
                for key, item in value.items()
            )
        return all(_is_portable_value(item, active) for item in value)
    if isinstance(value, float):
        return math.isfinite(value)
    return value is None or isinstance(value, str | int | bool)


def _error_path(error: jsonschema.ValidationError) -> str:
    """Return the public dotted field name for a schema error."""
    return ".".join(str(part) for part in error.absolute_path)


def _format_manifest_error(error: jsonschema.ValidationError) -> str:
    """Translate a JSON Schema failure into one concise author-facing message."""
    path = _error_path(error)
    validator = cast("str", error.validator)
    if "propertyNames" in error.absolute_schema_path:
        return f"{path} keys must be {'strings' if validator == 'type' else 'non-empty strings'}"
    if validator in {"required", "additionalProperties", "anyOf"}:
        return _format_manifest_structure_error(error, validator, path)
    return _format_manifest_value_error(error, validator, path)


def _format_manifest_structure_error(
    error: jsonschema.ValidationError, validator: str, path: str
) -> str:
    """Format missing, extra, and union-shaped manifest failures."""
    if validator == "required":
        required = cast("list[str]", error.validator_value)
        instance = cast("Mapping[str, object]", error.instance)
        missing = next(name for name in required if name not in instance)
        return f"{path + '.' if path else ''}{missing} is required"
    if validator == "additionalProperties":
        schema = cast("Mapping[str, object]", error.schema)
        allowed = cast("Mapping[str, object]", schema.get("properties", {}))
        instance = cast("Mapping[str, object]", error.instance)
        unknown = next(name for name in instance if name not in allowed)
        return f"{path or 'agent.yaml'} has an unknown field: {unknown}"
    if not isinstance(error.instance, dict):
        return f"{path} must be a mapping or null"
    nested = max(
        error.context,
        key=lambda item: (len(item.absolute_path), item.validator != "type"),
    )
    return _format_manifest_error(nested)


def _format_manifest_value_error(
    error: jsonschema.ValidationError, validator: str, path: str
) -> str:
    """Format one scalar or collection constraint failure."""
    if validator == "type":
        expected_type = cast("str", error.validator_value)
        expected = {"object": "mapping", "boolean": "boolean", "string": "string"}.get(
            expected_type, expected_type
        )
        return f"{path} must be a {expected}"
    if validator == "enum":
        choices = ", ".join(str(choice) for choice in cast("list[object]", error.validator_value))
        return f"{path} must be one of: {choices}"
    messages = {
        "minLength": f"{path} must be a non-empty string",
        "minProperties": f"{path} must not be empty",
        "const": f"{path} must be {error.validator_value}",
    }
    return messages[validator]


def _validate_manifest(manifest: dict[str, Any]) -> None:
    """Validate one portable manifest against the packaged public schema."""
    error = next(_MANIFEST_VALIDATOR.iter_errors(manifest), None)
    if error is not None:
        raise ManifestError(_format_manifest_error(error))
    for field in ("inputs", "outputs"):
        try:
            jsonschema.Draft202012Validator.check_schema(manifest[field])
        except jsonschema.exceptions.SchemaError as error:
            raise ManifestError(f"{field} is not a valid Draft 2020-12 schema") from error
    if _has_external_schema_reference(manifest["outputs"]):
        raise ManifestError("outputs must be self-contained")


def _has_external_schema_reference(value: object) -> bool:
    """Report whether a schema depends on a document outside itself."""
    if isinstance(value, dict):
        return any(
            (
                key in {"$ref", "$dynamicRef"}
                and isinstance(item, str)
                and item != ""
                and not item.startswith("#")
            )
            or _has_external_schema_reference(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_external_schema_reference(item) for item in value)
    return False


def load_manifest(app_directory: str | Path) -> dict[str, Any]:
    """Read and validate an application's ``agent.yaml`` manifest.

    The manifest is validated purely from its text; application tool code is
    never imported. The returned mapping is exactly what the author wrote.

    Args:
        app_directory: The application directory containing ``agent.yaml``.

    Returns:
        The validated manifest as a plain mapping.

    Raises:
        ManifestError: If the manifest is missing, unparseable, or violates
            the manifest contract. The message names the offending field.
    """
    manifest_path = Path(app_directory) / "agent.yaml"
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ManifestError(f"agent.yaml was not found in {app_directory}") from error
    except UnicodeDecodeError as error:
        raise ManifestError("agent.yaml must contain valid UTF-8 text") from error
    except OSError as error:
        raise ManifestError("agent.yaml could not be read") from error
    try:
        manifest = yaml.load(
            text,
            Loader=_UniqueKeyLoader,  # noqa: S506 - subclass of yaml.SafeLoader
        )
    except yaml.YAMLError as error:
        raise ManifestError("agent.yaml is not valid YAML") from error
    if not isinstance(manifest, dict):
        raise ManifestError("agent.yaml must be a mapping")
    try:
        json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError as error:
        raise ManifestError("agent.yaml must contain only UTF-8 encodable text") from error
    except TypeError, ValueError:
        # Non-JSON values are reported by field validation with an exact path.
        pass
    for field in ("inputs", "outputs"):
        value = manifest.get(field)
        if isinstance(value, dict) and not _is_json_portable(value):
            raise ManifestError(f"{field} must contain only JSON-portable values")
    _validate_manifest(manifest)
    return manifest


_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_EXPANDED_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_LOCKFILE_BYTES = 64 * 1024 * 1024
_MAX_SOURCE_MEMBERS = 1024
_MAX_MEMBER_PATH_BYTES = 255


def _is_canonical_relative_path(value: str) -> bool:
    """Report whether a path is a canonical portable relative path."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not (
        not value
        or len(encoded) > _MAX_MEMBER_PATH_BYTES
        or value.startswith("/")
        or "\\" in value
        or unicodedata.normalize("NFC", value) != value
        or any(unicodedata.category(character) == "Cc" for character in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    )


def _declared_file(app_directory: Path, manifest: dict[str, Any], field: str) -> Path:
    """Resolve a manifest-declared file path and require that it exists.

    Args:
        app_directory: The application directory.
        manifest: The validated manifest.
        field: Dotted manifest field holding the path, e.g. ``agent.prompt``.

    Returns:
        The path of the declared file.

    Raises:
        ManifestError: If the declared path is not canonical or the file does
            not exist.
    """
    section, key = field.split(".")
    declared = str(manifest[section][key])
    if not _is_canonical_relative_path(declared):
        raise ManifestError(f"{field} is not a canonical path")
    path = app_directory / declared
    if not path.is_file():
        raise ManifestError(f"{field} does not exist")
    return path


def _docstring_sections(docstring: str) -> dict[str, list[str]]:
    """Split a Google-style docstring into its titled sections."""
    sections: dict[str, list[str]] = {}
    current = "summary"
    sections[current] = []
    for line in docstring.splitlines():
        heading = re.fullmatch(r"\s*(Args|Returns|Raises|Yields|Examples):\s*", line)
        if heading:
            current = heading.group(1)
            sections[current] = []
        else:
            sections[current].append(line)
    return sections


def _tool_parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    """Return every named parameter of a tool function definition."""
    arguments = function.args
    parameters = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
    for variadic in (arguments.vararg, arguments.kwarg):
        if variadic is not None:
            parameters.append(variadic)
    return parameters


def _validate_tool(function: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
    """Validate one registered tool definition without importing it.

    Args:
        function: The tool's parsed function definition.

    Raises:
        ManifestError: If the tool lacks annotations or a complete
            Google-style docstring.
    """
    name = function.name
    parameters = _tool_parameters(function)
    for parameter in parameters:
        if parameter.annotation is None:
            raise ManifestError(f"tool {name} parameter {parameter.arg} requires a type annotation")
    if function.returns is None:
        raise ManifestError(f"tool {name} requires a return type annotation")
    docstring = ast.get_docstring(function)
    if not docstring:
        raise ManifestError(f"tool {name} requires a docstring")
    sections = _docstring_sections(docstring)
    described = "\n".join(sections.get("Args", []))
    for parameter in parameters:
        if not re.search(rf"^\s*{re.escape(parameter.arg)}(?: \([^)]*\))?:\s*\S", described, re.M):
            raise ManifestError(f"tool {name} docstring must describe parameter {parameter.arg}")
    returns_none = isinstance(function.returns, ast.Constant) and function.returns.value is None
    if returns_none:
        if "Returns" in sections:
            raise ManifestError(
                f"tool {name} must not document a return value when it returns None"
            )
    elif not "".join(sections.get("Returns", [])).strip():
        raise ManifestError(f"tool {name} docstring must describe the return value")


def _validate_tools_source(source_path: Path, registered: dict[str, Any]) -> None:
    """Statically validate the tool module against the registration list.

    Args:
        source_path: The declared ``tools.source`` file.
        registered: The manifest's ``tools.register`` mapping.

    Raises:
        ManifestError: If the module does not parse, the registrations do not
            match its public functions, or a tool definition is incomplete.
    """
    try:
        source_bytes = source_path.read_bytes()
    except OSError as error:
        raise ManifestError("tools.source could not be read") from error
    try:
        module = ast.parse(source_bytes)
    except (SyntaxError, ValueError) as error:
        raise ManifestError("tools.source is not valid Python") from error
    public_functions = {
        node.name: node
        for node in module.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and not node.name.startswith("_")
    }
    missing = sorted(set(registered) - set(public_functions))
    if missing:
        raise ManifestError(f"registered tool does not exist: {missing[0]}")
    undeclared = sorted(set(public_functions) - set(registered))
    if undeclared:
        raise ManifestError(f"public tool is not registered: {undeclared[0]}")
    for name in registered:
        _validate_tool(public_functions[name])


def _project_metadata(app_directory: Path) -> None:
    """Require the project to select a standards-based build backend explicitly."""
    try:
        project = tomllib.loads((app_directory / "pyproject.toml").read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ManifestError("pyproject.toml does not exist") from error
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise BundleError("pyproject.toml could not be read") from error
    build_system = project.get("build-system")
    if not isinstance(build_system, dict):
        raise BundleError("pyproject.toml must declare an explicit build-system")
    backend = build_system.get("build-backend")
    requirements = build_system.get("requires")
    if (
        not isinstance(backend, str)
        or not backend
        or not isinstance(requirements, list)
        or not requirements
        or not all(isinstance(requirement, str) and requirement for requirement in requirements)
    ):
        raise BundleError("pyproject.toml must declare an explicit build-system")


def _inspect_sdist(source: bytes, required_paths: set[str]) -> None:
    """Require one bounded standard sdist root containing every runtime declaration."""
    if len(source) > _MAX_SOURCE_BYTES:
        raise BundleError(f"source distribution exceeds {_MAX_SOURCE_BYTES} bytes")
    try:
        with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as archive:
            members = archive.getmembers()
    except (tarfile.TarError, OSError) as error:
        raise BundleError("build backend did not produce a valid source distribution") from error
    if len(members) > _MAX_SOURCE_MEMBERS:
        raise BundleError(f"source distribution has more than {_MAX_SOURCE_MEMBERS} members")
    roots: set[str] = set()
    files_in_root: set[str] = set()
    seen: set[str] = set()
    expanded_size = 0
    for member in members:
        root, separator, relative_path = member.name.partition("/")
        if not separator and member.isdir() and _is_canonical_relative_path(member.name):
            roots.add(root)
            continue
        if not separator or not _is_canonical_relative_path(member.name):
            raise BundleError("source distribution must contain one standard root")
        roots.add(root)
        if member.name in seen:
            raise BundleError("source distribution contains a duplicate path")
        seen.add(member.name)
        if member.isdir():
            continue
        if not member.isfile():
            raise BundleError("source distribution contains a non-file member")
        expanded_size += member.size
        if expanded_size > _MAX_EXPANDED_SOURCE_BYTES:
            raise BundleError(
                f"source distribution expands beyond {_MAX_EXPANDED_SOURCE_BYTES} bytes"
            )
        files_in_root.add(relative_path)
    if len(roots) != 1:
        raise BundleError("source distribution must contain one standard root")
    missing = sorted(required_paths - files_in_root)
    if missing:
        raise BundleError(f"build backend omitted required file: {missing[0]}")


def _sdist_file(source: bytes, relative_path: str) -> bytes:
    """Read one already-validated regular file from a standard source distribution."""
    with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as archive:
        member = next(
            member
            for member in archive.getmembers()
            if member.name.partition("/")[2] == relative_path
        )
        extracted = archive.extractfile(member)
        if extracted is None:  # pragma: no cover - _inspect_sdist requires a regular file
            raise BundleError(f"source distribution could not read {relative_path}")
        return extracted.read()


def _build_sdist(app_directory: Path, required_paths: set[str]) -> bytes:
    """Run the declared build backend and return its single source distribution."""
    with TemporaryDirectory() as output_directory:
        try:
            result = subprocess.run(  # noqa: S603 - fixed uv operation; only app path varies
                [  # noqa: S607 - uv is the documented SDK prerequisite
                    "uv",
                    "build",
                    "--sdist",
                    "--no-create-gitignore",
                    "--out-dir",
                    output_directory,
                    str(app_directory),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise BundleError("uv is required to build the application") from error
        if result.returncode != 0:
            diagnostic = result.stderr.strip().splitlines()
            detail = f": {diagnostic[-1]}" if diagnostic else ""
            raise BundleError(f"source distribution build failed{detail}")
        outputs = list(Path(output_directory).iterdir())
        if len(outputs) != 1 or not outputs[0].name.endswith(".tar.gz"):
            raise BundleError("build backend must produce exactly one source distribution")
        try:
            source = outputs[0].read_bytes()
        except OSError as error:
            raise BundleError("source distribution could not be read") from error
    _inspect_sdist(source, required_paths)
    return source


def _build_bundle(
    app_directory: str | Path,
) -> tuple[dict[str, bytes], dict[str, Any], dict[str, Any]]:
    """Build application artifacts and retain their validated manifest.

    The manifest, declared files, and registered tools are validated first;
    tool code is parsed but never imported. The application files are then
    passed to the project's declared build backend. Its unchanged source
    distribution is returned as the complete immutable application artifact.

    Args:
        app_directory: The application directory containing ``agent.yaml``.

    Returns:
        Source-distribution bytes, their portable identity record, and the
        validated manifest.

    Raises:
        ManifestError: If the manifest, a declared file, or a registered tool
            is invalid.
        BundleError: If project metadata, the build, or its source distribution
            is invalid.
    """
    app = Path(app_directory)
    if not app.is_dir():
        raise BundleError(f"{app} is not a directory")
    manifest = load_manifest(app)
    _declared_file(app, manifest, "agent.prompt")
    _declared_file(app, manifest, "runtime.lockfile")
    source = _declared_file(app, manifest, "tools.source")
    _project_metadata(app)
    _validate_tools_source(source, manifest["tools"]["register"])
    try:
        lockfile_bytes = (app / "uv.lock").read_bytes()
    except OSError as error:
        raise BundleError("runtime.lockfile could not be read") from error
    if len(lockfile_bytes) > _MAX_LOCKFILE_BYTES:
        raise BundleError(f"lockfile exceeds {_MAX_LOCKFILE_BYTES} bytes")
    source_bytes = _build_sdist(
        app,
        {
            "agent.yaml",
            "pyproject.toml",
            "PKG-INFO",
            "uv.lock",
            manifest["agent"]["prompt"],
            manifest["tools"]["source"],
        },
    )
    if _sdist_file(source_bytes, "uv.lock") != lockfile_bytes:
        raise BundleError("build backend changed uv.lock")
    return (
        {"source": source_bytes},
        {
            "api_version": "recurse.application/v1alpha1",
            "source": {
                "sha256": hashlib.sha256(source_bytes).hexdigest(),
                "size_bytes": len(source_bytes),
            },
        },
        manifest,
    )


def build_bundle(app_directory: str | Path) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Validate an application and build its standard deployment artifacts.

    The manifest, declared files, and registered tools are validated first;
    tool code is parsed but never imported. The application files are then
    passed to its declared build backend. The resulting source distribution is
    returned unchanged and contains the exact dependency lockfile.

    Args:
        app_directory: The application directory containing ``agent.yaml``.

    Returns:
        A pair containing source-distribution bytes keyed by ``source`` and a
        portable record with its SHA-256 and byte size.

    Raises:
        ManifestError: If the manifest, a declared file, or a registered tool
            is invalid.
        BundleError: If project metadata, the build, or its source distribution
            is invalid.
    """
    bundle_bytes, record, _manifest = _build_bundle(app_directory)
    return bundle_bytes, record
