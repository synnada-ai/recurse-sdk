"""Recurse SDK: author, package, and run Recurse applications.

The public surface is intentionally small:

- :func:`build_bundle` validates an application directory and produces one
  standard source distribution containing its exact dependency lockfile.
- :func:`load_tools_manifest` validates a tools-only declaration.
- :func:`build_tools_bundle` builds tools-only source without a prompt or agent.
- :func:`context` gives application tool code access to the active run's
  validated inputs and writable workspace.
- :class:`RecurseError` and its subclasses report every SDK failure.

The ``recurse`` console command that ships with this distribution handles
login, deployment, and billing; see the package documentation.
"""

import ast
import base64
import copy
import hashlib
import http.client
import io
import json
import math
import os
import re
import subprocess
import tarfile
import time
import tomllib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import Any, cast, override

import jsonschema
import yaml

__all__ = [
    "BundleError",
    "ManifestError",
    "RecurseError",
    "RunContext",
    "RunContextError",
    "ToolsClient",
    "ToolsError",
    "build_bundle",
    "build_tools_bundle",
    "context",
    "load_tools_manifest",
]


class RecurseError(Exception):
    """Base class for every error raised by the Recurse SDK."""


class ManifestError(RecurseError):
    """The application manifest or declared application content is invalid.

    The message names the offending ``agent.yaml`` or ``tools.yaml`` field or file so
    the author can correct it directly.
    """


class BundleError(RecurseError):
    """The application cannot be built into a valid source distribution.

    Raised when project metadata, the selected build backend, or the resulting
    source archive does not satisfy the Recurse application contract.
    """


class RunContextError(RecurseError):
    """The Recurse run context was used outside an active run."""


class ToolsError(RecurseError):
    """A tools request, local login, source preparation or publication could not complete.

    Execution failures are returned as durable call receipts instead of exceptions.

    Attributes:
        status_code: HTTP rejection status, or ``None`` for a local/transport failure.
        version_id: Allocated source version to inspect or resume, when known.
    """

    status_code: int | None
    version_id: str | None

    def __init__(
        self, message: str, status_code: int | None = None, version_id: str | None = None
    ) -> None:
        """Retain a safe failure message and optional resumable source identity."""
        super().__init__(message)
        self.status_code = status_code
        self.version_id = version_id


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
        expected_type = cast("str | list[str]", error.validator_value)
        names = expected_type if isinstance(expected_type, list) else [expected_type]
        expected = " or ".join({"object": "mapping"}.get(name, name) for name in names)
        article = "an" if expected[0] in "aeiou" else "a"
        return f"{path} must be {article} {expected}"
    if validator == "enum":
        choices = ", ".join(str(choice) for choice in cast("list[object]", error.validator_value))
        return f"{path} must be one of: {choices}"
    messages = {
        "minLength": f"{path} must be a non-empty string",
        "minProperties": f"{path} must not be empty",
        "minimum": f"{path} must be at least {error.validator_value}",
        "const": f"{path} must be {error.validator_value}",
    }
    return messages[validator]


def _validate_manifest(manifest: dict[str, Any]) -> None:
    """Validate one portable manifest against the packaged public schema."""
    agent = manifest.get("agent")
    limits: dict[str, Any] = agent if isinstance(agent, dict) else {}
    for name in ("max_llm_errors", "max_tool_errors"):
        # JSON Schema treats 3.0 as an integer; the hosted service does not.
        if isinstance(limits.get(name), float):
            raise ManifestError(f"agent.{name} must be an integer")
    timeout = limits.get("timeout_tools")
    if isinstance(timeout, float) and not math.isfinite(timeout):
        raise ManifestError("agent.timeout_tools must be a finite number")
    error = next(_MANIFEST_VALIDATOR.iter_errors(manifest), None)
    if error is not None:
        raise ManifestError(_format_manifest_error(error))
    for field in ("inputs", "outputs"):
        try:
            jsonschema.Draft202012Validator.check_schema(manifest[field])
        except jsonschema.exceptions.SchemaError as error:
            raise ManifestError(f"{field} is not a valid Draft 2020-12 schema") from error
    _validate_task_contract(manifest["inputs"])
    if _has_external_schema_reference(manifest["outputs"]):
        raise ManifestError("outputs must be self-contained")


def _validate_task_contract(schema: dict[str, Any]) -> None:
    """Validate the supported declaration for the reserved run task."""
    properties = schema.get("properties")
    if not isinstance(properties, dict) or "task" not in properties:
        raise ManifestError("inputs.properties.task must be declared")
    task = properties["task"]
    if not isinstance(task, dict) or task.get("type") != "string":
        raise ManifestError("inputs.properties.task.type must be string")
    if "default" in task and (not isinstance(task["default"], str) or not task["default"]):
        raise ManifestError("inputs.properties.task.default must be a non-empty string")
    required = schema.get("required", [])
    if "default" not in task and "task" not in required:
        raise ManifestError(
            "inputs.properties.task must have a non-empty default or be listed in inputs.required"
        )


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


_TOOLS_MANIFEST_SCHEMA = json.loads(
    files(__name__).joinpath("tools.schema.json").read_text(encoding="utf-8")
)
jsonschema.Draft202012Validator.check_schema(_TOOLS_MANIFEST_SCHEMA)
_TOOLS_MANIFEST_VALIDATOR = jsonschema.Draft202012Validator(_TOOLS_MANIFEST_SCHEMA)


def load_tools_manifest(service_directory: str | Path) -> dict[str, Any]:
    """Read a tools-only declaration without importing its Python source.

    Args:
        service_directory: Directory containing ``tools.yaml``.

    Returns:
        The validated, JSON-portable manifest exactly as authored.

    Raises:
        ManifestError: If the manifest is unreadable, malformed, or violates
            the tools-only contract, including schema and fixture pricing.
    """
    try:
        text = (Path(service_directory) / "tools.yaml").read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ManifestError(f"tools.yaml was not found in {service_directory}") from error
    except UnicodeDecodeError as error:
        raise ManifestError("tools.yaml must contain valid UTF-8 text") from error
    except OSError as error:
        raise ManifestError("tools.yaml could not be read") from error
    try:
        manifest = yaml.load(
            text,
            Loader=_UniqueKeyLoader,  # noqa: S506 - subclass of yaml.SafeLoader
        )
    except ManifestError as error:
        raise ManifestError(str(error).replace("agent.yaml", "tools.yaml")) from error
    except (yaml.YAMLError, TypeError) as error:
        raise ManifestError("tools.yaml is not valid YAML") from error
    if not isinstance(manifest, dict):
        raise ManifestError("tools.yaml must be a mapping")
    if not _is_json_portable(manifest):
        raise ManifestError("tools.yaml must contain only JSON-portable values")
    try:
        json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError as error:
        raise ManifestError("tools.yaml must contain only UTF-8 encodable text") from error
    schema_error = next(_TOOLS_MANIFEST_VALIDATOR.iter_errors(manifest), None)
    if schema_error is not None:
        field = _error_path(schema_error) or "tools.yaml"
        detail = (
            "unknown field"
            if schema_error.validator == "additionalProperties"
            else schema_error.message
        )
        raise ManifestError(f"{field}: {detail}")
    _validate_tools_registration_schemas(manifest)
    return manifest


def _validate_tools_registration_schemas(manifest: dict[str, Any]) -> None:
    """Validate both explicit schemas of every tools registration."""
    for name, registration in manifest["tools"]["register"].items():
        for field in ("input_schema", "output_schema"):
            schema = registration[field]
            try:
                jsonschema.Draft202012Validator.check_schema(schema)
            except jsonschema.exceptions.SchemaError as error:
                raise ManifestError(
                    f"tools.register.{name}.{field} is not a valid Draft 2020-12 schema"
                ) from error
            if _has_external_schema_reference(schema):
                raise ManifestError(f"tools.register.{name}.{field} must be self-contained")


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
            "apiVersion": "recurse.application/v1alpha1",
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
        portable record with ``apiVersion`` set to
        ``recurse.application/v1alpha1`` and ``source`` containing the exact
        SHA-256 and byte size.

    Raises:
        ManifestError: If the manifest, a declared file, or a registered tool
            is invalid.
        BundleError: If project metadata, the build, or its source distribution
            is invalid.
    """
    bundle_bytes, record, _manifest = _build_bundle(app_directory)
    return bundle_bytes, record


def _validate_tools_signature(
    function: ast.FunctionDef | ast.AsyncFunctionDef, input_schema: dict[str, Any]
) -> None:
    """Require a closed named-input schema to agree with the Python signature."""
    name = function.name
    arguments = function.args
    if arguments.posonlyargs:
        raise ManifestError(f"tool {name} must accept named arguments")
    if arguments.vararg is not None or arguments.kwarg is not None:
        raise ManifestError(f"tool {name} must not have variadic arguments")
    parameters = [*arguments.args, *arguments.kwonlyargs]
    properties = input_schema["properties"]
    if set(properties) != {parameter.arg for parameter in parameters}:
        raise ManifestError(f"tool {name} input properties must match its Python signature")
    defaults: list[ast.expr | None] = [None] * (len(arguments.args) - len(arguments.defaults))
    defaults.extend(arguments.defaults)
    defaults.extend(arguments.kw_defaults)
    required = {
        parameter.arg
        for parameter, default in zip(parameters, defaults, strict=True)
        if default is None
    }
    if set(input_schema.get("required", [])) != required:
        raise ManifestError(f"tool {name} input required must match its Python signature")
    validator = jsonschema.Draft202012Validator(input_schema)
    for parameter, expression in zip(parameters, defaults, strict=True):
        _validate_tools_default(
            name, parameter.arg, expression, properties[parameter.arg], validator
        )


def _validate_tools_default(
    name: str,
    parameter: str,
    expression: ast.expr | None,
    declaration: Any,
    validator: jsonschema.Draft202012Validator,
) -> None:
    """Ensure omitted inputs have one literal value agreed by Python and schema."""
    prefix = f"tool {name} default for {parameter}"
    has_declared_default = isinstance(declaration, dict) and "default" in declaration
    if expression is None:
        if has_declared_default:
            raise ManifestError(
                f"tool {name} required parameter {parameter} must not have a default"
            )
        return
    try:
        default = ast.literal_eval(expression)
    except (ValueError, TypeError) as error:
        raise ManifestError(f"{prefix} must be literal JSON") from error
    if not _is_json_portable(default):
        raise ManifestError(f"{prefix} must be literal JSON")
    if has_declared_default:
        declared_default = declaration["default"]
        if not validator.evolve(schema=declaration).is_valid(declared_default):
            raise ManifestError(f"{prefix} must satisfy its schema")
        if json.dumps(declared_default, sort_keys=True) != json.dumps(default, sort_keys=True):
            raise ManifestError(f"{prefix} must match Python")
    if not validator.evolve(schema=declaration).is_valid(default):
        raise ManifestError(f"tool {name} Python default for {parameter} must satisfy its schema")


def _validate_tools_contract_source(source_path: Path, registered: dict[str, Any]) -> None:
    """Parse tool definitions without importing code or running its decorators."""
    try:
        source_bytes = source_path.read_bytes()
    except OSError as error:
        raise ManifestError("tools.source could not be read") from error
    try:
        module = ast.parse(source_bytes)
    except (SyntaxError, ValueError) as error:
        raise ManifestError("tools.source is not valid Python") from error
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in module.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not node.name.startswith(
            "_"
        ):
            if node.name in functions:
                raise ManifestError(f"duplicate public tool: {node.name}")
            functions[node.name] = node
    missing = sorted(set(registered) - set(functions))
    if missing:
        raise ManifestError(f"registered tool does not exist: {missing[0]}")
    undeclared = sorted(set(functions) - set(registered))
    if undeclared:
        raise ManifestError(f"public tool is not registered: {undeclared[0]}")
    for name, registration in registered.items():
        function = functions[name]
        _validate_tools_signature(function, registration["input_schema"])
        _validate_tool(function)


def build_tools_bundle(service_directory: str | Path) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Validate tools-only source and build one immutable standard distribution.

    Tools are parsed without importing them on the author's machine. The
    distribution contains ``tools.yaml``, its source, project metadata, and
    the exact dependency lockfile. No agent manifest or prompt is required.

    Args:
        service_directory: Directory containing ``tools.yaml`` and project files.

    Returns:
        Source-distribution bytes keyed by ``source`` and a portable record
        with ``apiVersion`` set to ``recurse.tools.application/v1alpha1``.
        The record's ``source`` contains the exact SHA-256 and byte size.

    Raises:
        ManifestError: If declarations, files, schemas, or signatures are invalid.
        BundleError: If the directory, build metadata, backend output, or
            dependency lockfile does not satisfy the tools artifact contract.
    """
    service = Path(service_directory)
    if not service.is_dir():
        raise BundleError(f"{service} is not a directory")
    manifest = load_tools_manifest(service)
    lockfile = _declared_file(service, manifest, "runtime.lockfile")
    source = _declared_file(service, manifest, "tools.source")
    _project_metadata(service)
    _validate_tools_contract_source(source, manifest["tools"]["register"])
    try:
        lockfile_bytes = lockfile.read_bytes()
    except OSError as error:
        raise BundleError("runtime.lockfile could not be read") from error
    if len(lockfile_bytes) > _MAX_LOCKFILE_BYTES:
        raise BundleError(f"lockfile exceeds {_MAX_LOCKFILE_BYTES} bytes")
    source_bytes = _build_sdist(
        service,
        {"tools.yaml", "pyproject.toml", "PKG-INFO", "uv.lock", manifest["tools"]["source"]},
    )
    if _sdist_file(source_bytes, "uv.lock") != lockfile_bytes:
        raise BundleError("build backend changed uv.lock")
    return (
        {"source": source_bytes},
        {
            "apiVersion": "recurse.tools.application/v1alpha1",
            "source": {
                "sha256": hashlib.sha256(source_bytes).hexdigest(),
                "size_bytes": len(source_bytes),
            },
        },
    )


_TOOLS_HTTP_TIMEOUT_SECONDS = 60
_TOOLS_MAX_RESPONSE_BYTES = 8_388_608


class _ToolsNoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects carrying a customer bearer or a Storage capability."""

    @override
    def redirect_request(
        self,
        request: urllib.request.Request,
        response: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        """Leave redirected requests rejected at their original HTTP boundary."""


def _tools_origin(base_url: str | None) -> str:
    """Resolve one HTTP origin without credentials, path, query, or fragment."""
    origin = (
        base_url
        if base_url is not None
        else os.environ.get("RECURSE_TOOLS_URL", "https://tools.recurse.run")
    )
    try:
        if not isinstance(origin, str) or any(character.isspace() for character in origin):
            raise ValueError("invalid origin")
        parsed = urllib.parse.urlsplit(origin)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not origin.isascii()
            or not origin.isprintable()
        ):
            raise ValueError("invalid origin")
        _port = parsed.port
    except ValueError as error:
        raise ToolsError(
            "tools base URL must be an http(s) origin without a path or credentials"
        ) from error
    return origin.rstrip("/")


def _tools_bearer(access_token: str) -> str:
    """Require an explicit nonempty bearer safe for one Authorization header."""
    if (
        not isinstance(access_token, str)
        or not access_token
        or not access_token.isascii()
        or not access_token.isprintable()
        or any(character.isspace() for character in access_token)
    ):
        raise ToolsError("a nonempty tools access token is required")
    return access_token


def _tools_json(payload: object) -> bytes:
    """Encode an ordinary finite JSON object without changing its value types."""
    try:
        if not isinstance(payload, dict) or not _is_json_portable(payload):
            raise ValueError("invalid JSON object")
        return json.dumps(
            payload, allow_nan=False, ensure_ascii=False, separators=(",", ":")
        ).encode()
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise ToolsError("tools requests must contain ordinary finite JSON objects") from error


def _tools_http_error(error: urllib.error.HTTPError) -> ToolsError:
    """Retain useful public rejection detail without printing raw transport data."""
    status = error.code
    message = f"the tools service rejected the request (HTTP {status})"
    if status >= http.client.INTERNAL_SERVER_ERROR:
        message = f"the tools service could not complete the request (HTTP {status})"
    elif status == http.client.UNAUTHORIZED:
        message = "the tools access token was rejected (HTTP 401)"
    else:
        try:
            payload = json.loads(error.read(_TOOLS_MAX_RESPONSE_BYTES + 1))
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if isinstance(detail, str) and detail:
                message = detail
        except ValueError, OSError, RecursionError:
            pass
    return ToolsError(message, status_code=status)


def _tools_field(payload: dict[str, Any], name: str) -> str:
    """Read one mandatory response string before authorizing the next step."""
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ToolsError(f"the tools service returned an invalid response: missing {name}")
    return value


def _private_limits(max_cost_micro_usd: int, timeout_seconds: float) -> None:
    """Validate explicit preparation limits before building source or authorizing charges."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ToolsError("preparation timeout must be a positive finite number")
    if (
        isinstance(max_cost_micro_usd, bool)
        or not isinstance(max_cost_micro_usd, int)
        or max_cost_micro_usd < 0
    ):
        raise ToolsError("maximum tools cost must be a nonnegative integer in micro USD")


class ToolsClient:
    """Call the tools service from a local login or an explicit server bearer.

    The explicit constructor never reads a keychain or opens a browser. Supply a
    short-lived bearer for the exact tools ``/mcp`` resource; renew it in the calling
    server process. Use :meth:`from_login` explicitly for the saved local CLI login.
    Methods return the service's ordinary JSON objects and durable call receipts.

    ``catalog_search`` pages the catalog; ``catalog_get`` inspects schemas, versions
    and price. ``call`` requires those versions, JSON inputs, a stable idempotency
    key and an explicit cost cap. ``get_call`` reads status/results/charges without
    another admission; ``cancel_call`` requests cancellation. Execution failures
    and unresolved billing remain receipt data. ``publish`` builds and publishes
    source for the authorized publisher. ``prepare_private`` prepares a customer's
    own project without publication; ``run`` prepares, inspects and admits a private
    call. ``get_version`` inspects publisher preparation and
    ``publish_version`` activates an already prepared version. ``from_login`` uses
    the saved local login and can refresh one rejected resource bearer. An explicit
    server bearer is never refreshed or read from local credentials automatically.
    """

    def __init__(self, access_token: str, *, base_url: str | None = None) -> None:
        """Construct a client without discovering or refreshing server credentials.

        Args:
            access_token: Explicit bearer for the tools resource.
            base_url: Tools HTTP origin; defaults to ``RECURSE_TOOLS_URL`` or
                ``https://tools.recurse.run``.

        Raises:
            ToolsError: If the bearer or HTTP origin is invalid.
        """
        self._access_token = _tools_bearer(access_token)
        self._base_url = _tools_origin(base_url)
        self._local_token: Callable[[str | None], str] | None = None
        self._opener = urllib.request.build_opener(_ToolsNoRedirect())

    @property
    def base_url(self) -> str:
        """Return the resolved tools HTTP origin."""
        return self._base_url

    @classmethod
    def from_login(cls, *, base_url: str | None = None) -> ToolsClient:
        """Use the saved CLI login with resource-scoped refresh and no browser prompt.

        Args:
            base_url: Tools HTTP origin; uses the same default as the constructor.

        Returns:
            A client that can refresh a rejected local bearer once per request.

        Raises:
            ToolsError: If the origin, saved login, keychain, or refresh grant is invalid.
        """
        from _recurse_cli import ServiceError, _access_token  # noqa: PLC0415 - explicit local login

        origin = _tools_origin(base_url)

        def token(rejected: str | None) -> str:
            """Obtain the exact Tools-resource bearer through the existing CLI lock."""
            try:
                return _tools_bearer(
                    _access_token(rejected_token=rejected, resource=origin + "/mcp")
                )
            except RecurseError as error:
                raise ToolsError(str(error)) from error
            except ServiceError as error:
                raise ToolsError(
                    "the local tools login could not be refreshed; run: recurse login",
                    error.status_code,
                ) from error

        client = cls(token(None), base_url=origin)
        client._local_token = token
        return client

    def _send(self, method: str, path: str, body: bytes | None) -> dict[str, Any]:
        """Send one bounded HTTP request with no transport retries or redirects."""
        headers = {
            "Authorization": "Bearer " + self._access_token,
            "Accept": "application/json",
            "User-Agent": "recurse-sdk",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
            if path == "/mcp":
                message = json.loads(body)
                headers["MCP-Protocol-Version"] = "2026-07-28"
                mcp_method = message.get("method")
                if isinstance(mcp_method, str):
                    headers["Mcp-Method"] = mcp_method
                params = message.get("params")
                name = params.get("name") if isinstance(params, dict) else None
                if mcp_method == "tools/call" and isinstance(name, str):
                    headers["Mcp-Name"] = name
        request = urllib.request.Request(  # noqa: S310 - validated Tools origin
            self._base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=_TOOLS_HTTP_TIMEOUT_SECONDS) as response:
                raw = response.read(_TOOLS_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise _tools_http_error(error) from error
        except (http.client.HTTPException, OSError, ValueError) as error:
            raise ToolsError(
                "the tools service could not be reached; check your connection and tools base URL"
            ) from error
        try:
            payload = json.loads(raw)
            if (
                len(raw) > _TOOLS_MAX_RESPONSE_BYTES
                or not isinstance(payload, dict)
                or not _is_json_portable(payload)
            ):
                raise ValueError("invalid JSON response")
        except (ValueError, UnicodeError, RecursionError) as error:
            raise ToolsError("the tools service returned an invalid response") from error
        return cast("dict[str, Any]", payload)

    def _request(
        self, method: str, path: str, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send one request, permitting only one explicit local authentication refresh."""
        body = _tools_json(json_body) if json_body is not None else None
        try:
            return self._send(method, path, body)
        except ToolsError as error:
            if error.status_code != http.client.UNAUTHORIZED or self._local_token is None:
                raise
        self._access_token = self._local_token(self._access_token)
        return self._send(method, path, body)

    def catalog_search(
        self, search: str = "", limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        """Search the active catalog and return one page with its opaque next cursor."""
        query: dict[str, str | int] = {"search": search, "limit": limit}
        if cursor is not None:
            query["cursor"] = cursor
        return self._request("GET", "/v1/catalog?" + urllib.parse.urlencode(query))

    def catalog_get(self, tool_id: str) -> dict[str, Any]:
        """Inspect one tool's immutable versions, schemas and published price."""
        return self._request("GET", "/v1/catalog/" + urllib.parse.quote(tool_id, safe=""))

    def call(  # noqa: PLR0913 - explicit inspected versions, retry identity and customer cap
        self,
        tool_id: str,
        *,
        tool_version: str,
        price_version: str,
        inputs: dict[str, Any],
        idempotency_key: str,
        max_cost_micro_usd: int,
    ) -> dict[str, Any]:
        """Admit one bounded call; retrieve pending results through ``get_call``.

        All versions, the retry key and maximum total customer cost are explicit.
        Reuse identical arguments and key if a transport outcome is unknown.
        """
        if not isinstance(inputs, dict):
            raise ToolsError("tools inputs must be a JSON object")
        return self._request(
            "POST",
            "/v1/calls",
            {
                "tool_id": tool_id,
                "tool_version": tool_version,
                "price_version": price_version,
                "inputs": inputs,
                "idempotency_key": idempotency_key,
                "max_cost_micro_usd": max_cost_micro_usd,
            },
        )

    def get_call(self, call_id: str) -> dict[str, Any]:
        """Read your stored call status, result and charges without another admission."""
        return self._request("GET", "/v1/calls/" + urllib.parse.quote(call_id, safe=""))

    def cancel_call(self, call_id: str) -> dict[str, Any]:
        """Request cancellation; execution and billing may close independently."""
        return self._request(
            "POST", "/v1/calls/" + urllib.parse.quote(call_id, safe="") + "/cancel", {}
        )

    def get_version(self, version_id: str) -> dict[str, Any]:
        """Read the publisher's stored preparation state for one source version."""
        return self._request("GET", "/v1/tool-versions/" + urllib.parse.quote(version_id, safe=""))

    def get_private_version(self, version_id: str) -> dict[str, Any]:
        """Read your private project's stored preparation state."""
        return self._request(
            "GET", "/v1/private-tool-versions/" + urllib.parse.quote(version_id, safe="")
        )

    def inspect_private(self, version_id: str, function_name: str) -> dict[str, Any]:
        """Inspect your prepared private function's schemas, versions and compute price."""
        return self._request(
            "GET",
            "/v1/private-tool-versions/"
            + urllib.parse.quote(version_id, safe="")
            + "/tools/"
            + urllib.parse.quote(function_name, safe=""),
        )

    def call_private(  # noqa: PLR0913 - explicit inspected versions, retry key and cost cap
        self,
        version_id: str,
        function_name: str,
        *,
        tool_version: str,
        price_version: str,
        inputs: dict[str, Any],
        idempotency_key: str,
        max_cost_micro_usd: int,
    ) -> dict[str, Any]:
        """Admit your prepared function with an explicit price, retry identity and cap.

        Reuse all arguments and the same key after an unknown transport outcome.
        Read results with ``get_call`` and request cancellation with ``cancel_call``.
        """
        if not isinstance(inputs, dict):
            raise ToolsError("tools inputs must be a JSON object")
        return self._request(
            "POST",
            "/v1/private-calls",
            {
                "version_id": version_id,
                "function_name": function_name,
                "tool_version": tool_version,
                "price_version": price_version,
                "inputs": inputs,
                "idempotency_key": idempotency_key,
                "max_cost_micro_usd": max_cost_micro_usd,
            },
        )

    def prepare_private(
        self,
        service_directory: str | Path,
        *,
        max_cost_micro_usd: int,
        timeout_seconds: float = 600,
    ) -> dict[str, Any]:
        """Build, upload and prepare your tools.yaml project without publishing it.

        Identical source reuses the owner's stored version. Preparation errors retain
        ``ToolsError.version_id`` for inspection with ``get_private_version``.
        The explicit total workflow cap includes preparation and execution charges.
        """
        _private_limits(max_cost_micro_usd, timeout_seconds)
        artifacts, record = build_tools_bundle(service_directory)
        manifest = load_tools_manifest(service_directory)
        return self._prepare_private_source(
            artifacts["source"],
            {
                "service_name": manifest["metadata"]["name"],
                "application_record": record,
                "max_cost_micro_usd": max_cost_micro_usd,
            },
            timeout_seconds,
        )

    def _prepare_private_source(
        self, source: bytes, registration: dict[str, Any], timeout_seconds: float
    ) -> dict[str, Any]:
        """Upload and prepare an already built private source without rebuilding it."""
        staged = self._request("POST", "/v1/private-tool-versions", registration)
        version_id = _tools_field(staged, "version_id")
        try:
            if _tools_field(staged, "status") == "staged":
                descriptor = staged.get("source_upload")
                if not isinstance(descriptor, dict):
                    raise ToolsError(
                        "the tools service returned an invalid source upload descriptor"
                    )
                self._upload_source(descriptor, source)
                self._request(
                    "POST",
                    "/v1/private-tool-versions/"
                    + urllib.parse.quote(version_id, safe="")
                    + "/complete",
                    {"source_upload_id": _tools_field(descriptor, "upload_id")},
                )
            deadline = time.monotonic() + timeout_seconds
            while True:
                version = self.get_private_version(version_id)
                status = _tools_field(version, "status")
                if status == "prepared":
                    return version
                if status in {
                    "failed",
                    "cancelled",
                    "timed_out",
                    "infrastructure_failed",
                    "retired",
                }:
                    raise ToolsError("tools preparation did not succeed: " + status)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ToolsError("tools preparation did not finish before the timeout")
                time.sleep(min(2, remaining))
        except ToolsError as error:
            raise ToolsError(
                f"{error}. Version: {version_id}; inspect with get_private_version.",
                error.status_code,
                version_id,
            ) from error

    def run(  # noqa: PLR0913 - explicit function, retry identity, cap and preparation bound
        self,
        service_directory: str | Path,
        function_name: str,
        *,
        inputs: dict[str, Any],
        idempotency_key: str,
        max_cost_micro_usd: int,
        timeout_seconds: float = 600,
    ) -> dict[str, Any]:
        """Prepare, inspect and admit a private project; return its durable call receipt.

        This waits for preparation. Poll ``get_call`` for execution results.
        Unchanged source, inputs, cap and key recover an admitted call; changes need a new key.
        Failed preparation is billable; inspect its version before an explicit retry.
        """
        _tools_json(inputs)
        _private_limits(max_cost_micro_usd, timeout_seconds)
        manifest = load_tools_manifest(service_directory)
        registered = manifest["tools"]["register"]
        if not isinstance(function_name, str) or function_name not in registered:
            raise ToolsError("function is not registered in tools.yaml")
        try:
            jsonschema.Draft202012Validator(registered[function_name]["input_schema"]).validate(
                inputs
            )
        except jsonschema.ValidationError as error:
            raise ToolsError("inputs do not match the private function schema") from error
        artifacts, record = build_tools_bundle(service_directory)
        replay = self._request(
            "POST",
            "/v1/private-calls/replay",
            {
                "service_name": manifest["metadata"]["name"],
                "source_sha256": record["source"]["sha256"],
                "function_name": function_name,
                "inputs": inputs,
                "idempotency_key": idempotency_key,
                "max_cost_micro_usd": max_cost_micro_usd,
            },
        )
        if "receipt" not in replay or (
            replay["receipt"] is not None and not isinstance(replay["receipt"], dict)
        ):
            raise ToolsError("the tools service returned an invalid private replay response")
        if replay["receipt"] is not None:
            return cast("dict[str, Any]", replay["receipt"])
        version = self._prepare_private_source(
            artifacts["source"],
            {
                "service_name": manifest["metadata"]["name"],
                "application_record": record,
                "max_cost_micro_usd": max_cost_micro_usd,
            },
            timeout_seconds,
        )
        version_id = _tools_field(version, "version_id")
        definition = self.inspect_private(version_id, function_name)
        return self.call_private(
            version_id,
            function_name,
            tool_version=_tools_field(definition, "version"),
            price_version=_tools_field(definition, "price_version"),
            inputs=inputs,
            idempotency_key=idempotency_key,
            max_cost_micro_usd=max_cost_micro_usd,
        )

    def publish_version(
        self, version_id: str, publication_config: dict[str, Any]
    ) -> dict[str, Any]:
        """Publish a known prepared version with its immutable publisher policy."""
        return self._request(
            "POST",
            "/v1/tool-versions/" + urllib.parse.quote(version_id, safe="") + "/publication",
            publication_config,
        )

    def _upload_source(self, descriptor: dict[str, Any], source: bytes) -> None:
        """Upload once; let completion prove storage after a lost transfer acknowledgment."""
        url = _tools_field(descriptor, "upload_url")
        try:
            parsed = urllib.parse.urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("invalid upload URL")
            prefix = base64.b64decode(_tools_field(descriptor, "upload_prefix"), validate=True)
            suffix = base64.b64decode(_tools_field(descriptor, "upload_suffix"), validate=True)
            request = urllib.request.Request(  # noqa: S310 - validated capability URL
                url,
                data=prefix + source + suffix,
                headers={
                    "Authorization": _tools_field(descriptor, "upload_authorization"),
                    "Content-Type": _tools_field(descriptor, "upload_content_type"),
                    "User-Agent": "recurse-sdk",
                },
                method="POST",
            )
        except ValueError as error:
            raise ToolsError(
                "the tools service returned an invalid source upload descriptor"
            ) from error
        try:
            with self._opener.open(request, timeout=_TOOLS_HTTP_TIMEOUT_SECONDS):
                return
        except urllib.error.HTTPError as error:
            if error.code >= http.client.INTERNAL_SERVER_ERROR:
                # A proxy may fail after Storage committed. The completion request
                # checks that exact uploaded object without repeating its transfer.
                error.close()
                return
            raise ToolsError("the tools source upload was rejected", error.code) from error
        except http.client.HTTPException, OSError:
            # A lost acknowledgment can follow a stored source. Completion checks its
            # durable upload identity; do not resend the source or allocate a new version.
            return
        except ValueError as error:
            raise ToolsError("the tools source upload could not complete") from error

    def publish(
        self,
        service_directory: str | Path,
        *,
        publication_config: dict[str, Any] | None = None,
        timeout_seconds: float = 600,
    ) -> dict[str, Any]:
        """Build, upload, prepare and publish one source version without restaging.

        Publisher policy is separate from ``tools.yaml``. Failures after staging
        retain ``ToolsError.version_id``; use ``get_version`` and ``publish_version``
        to inspect or publish that same prepared version.
        """
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ToolsError("publication timeout must be a positive finite number")
        config = publication_config if publication_config is not None else {"tools": {}}
        _tools_json(config)
        artifacts, record = build_tools_bundle(service_directory)
        manifest = load_tools_manifest(service_directory)
        staged = self._request(
            "POST",
            "/v1/tool-versions",
            {"service_name": manifest["metadata"]["name"], "application_record": record},
        )
        version_id = _tools_field(staged, "version_id")
        try:
            descriptor = staged.get("source_upload")
            if not isinstance(descriptor, dict):
                raise ToolsError("the tools service returned an invalid source upload descriptor")
            self._upload_source(descriptor, artifacts["source"])
            self._request(
                "POST",
                "/v1/tool-versions/" + urllib.parse.quote(version_id, safe="") + "/complete",
                {"source_upload_id": _tools_field(descriptor, "upload_id")},
            )
            deadline = time.monotonic() + timeout_seconds
            while True:
                version = self.get_version(version_id)
                status = _tools_field(version, "status")
                if status in {"prepared", "published"}:
                    return self.publish_version(version_id, config)
                if status in {
                    "failed",
                    "cancelled",
                    "timed_out",
                    "infrastructure_failed",
                    "retired",
                }:
                    raise ToolsError("tools preparation did not succeed: " + status)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ToolsError("tools preparation did not finish before the timeout")
                time.sleep(min(2, remaining))
        except ToolsError as error:
            raise ToolsError(
                f"{error}. Version: {version_id}; inspect with get_version and resume "
                "with publish_version when prepared.",
                error.status_code,
                version_id,
            ) from error
