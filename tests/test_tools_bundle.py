"""Tools-only bundles preserve the standard sdist without agent artifacts."""

import copy
import hashlib
import io
import tarfile
from pathlib import Path

import pytest

import recurse
from recurse import BundleError, ManifestError
from tests.test_tools_manifest import TOOLS_MANIFEST, write_tools_manifest

TOOL_SOURCE = '''"""JSON fixture tools."""


def echo(message: str) -> dict[str, str]:
    """Return a message.

    Args:
        message: Message to return.

    Returns:
        The supplied message.
    """
    return {"message": message}
'''


def write_tools_service(directory: Path) -> Path:
    """Create an independently buildable tools-only service."""
    write_tools_manifest(directory, TOOLS_MANIFEST)
    (directory / "tools.py").write_text(TOOL_SOURCE)
    (directory / "uv.lock").write_text("version = 1\n")
    (directory / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n'
        '[project]\nname = "fixture-tools"\nversion = "0.1.0"\n'
        'description = "JSON fixture tools"\n\n'
        "[tool.hatch.build.targets.sdist]\n"
        'include = ["tools.yaml", "tools.py", "uv.lock"]\n'
    )
    return directory


@pytest.fixture
def service(tmp_path: Path) -> Path:
    """Provide a complete tools service using a real standard build backend."""
    return write_tools_service(tmp_path / "tools-service")


def test_build_tools_bundle_needs_no_prompt_and_does_not_import_source(service: Path) -> None:
    """A tools bundle cannot execute tool code on the author's machine."""
    with (service / "tools.py").open("a") as source:
        source.write('\nraise RuntimeError("client must never execute this module")\n')

    assert hasattr(recurse, "build_tools_bundle"), "missing public tools bundle builder"
    artifacts, record = recurse.build_tools_bundle(service)

    assert set(artifacts) == {"source"}
    assert record == {
        "apiVersion": "recurse.tools.application/v1alpha1",
        "source": {
            "sha256": hashlib.sha256(artifacts["source"]).hexdigest(),
            "size_bytes": len(artifacts["source"]),
        },
    }
    contents = {}
    with tarfile.open(fileobj=io.BytesIO(artifacts["source"]), mode="r:gz") as archive:
        for member in archive:
            if member.isfile():
                file = archive.extractfile(member)
                assert file is not None
                contents[member.name.partition("/")[2]] = file.read()
    assert {"tools.yaml", "tools.py", "pyproject.toml", "uv.lock", "PKG-INFO"} <= set(contents)
    assert "agent.yaml" not in contents
    assert "prompt.md" not in contents
    assert contents["uv.lock"] == b"version = 1\n"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            TOOL_SOURCE.replace("def echo(message: str)", "def echo(message: str, /)"),
            "named arguments",
        ),
        (TOOL_SOURCE.replace("def echo(message: str)", "def echo(*message: str)"), "variadic"),
        (TOOL_SOURCE.replace("def echo(message: str)", "def echo(**message: str)"), "variadic"),
        (
            TOOL_SOURCE.replace("def echo(message: str)", "def echo(other: str)"),
            "properties.*signature",
        ),
        (
            TOOL_SOURCE.replace("def echo(message: str)", 'def echo(message: str = "default")'),
            "required.*signature",
        ),
        (TOOL_SOURCE.replace("def echo(message: str)", "def echo(message)"), "annotation"),
        (
            TOOL_SOURCE.replace(
                "def echo(message: str) -> dict[str, str]", "def echo(message: str)"
            ),
            "return type",
        ),
        (TOOL_SOURCE.replace("def echo", "def private_echo"), "registered tool does not exist"),
        (
            TOOL_SOURCE + '\n\ndef extra() -> None:\n    """Unregistered function."""\n',
            "public tool is not registered",
        ),
        (TOOL_SOURCE + TOOL_SOURCE, "duplicate public tool"),
        ("[", "valid Python"),
    ],
)
def test_bad_tools_source_fails_before_backend_build(
    service: Path, source: str, message: str
) -> None:
    """Incorrect registrations and signatures are rejected without executing code."""
    (service / "tools.py").write_text(source)

    with pytest.raises(ManifestError, match=message):
        recurse.build_tools_bundle(service)


def test_async_and_keyword_only_defaults_are_supported(service: Path) -> None:
    """Named calls can omit a literal optional value that Python supplies."""
    manifest = copy.deepcopy(TOOLS_MANIFEST)
    schema = manifest["tools"]["register"]["echo"]["input_schema"]
    schema["properties"]["message"]["default"] = "hello"
    schema["required"] = []
    write_tools_manifest(service, manifest)
    (service / "tools.py").write_text(
        TOOL_SOURCE.replace("def echo(message: str)", 'async def echo(*, message: str = "hello")')
    )

    artifacts, _record = recurse.build_tools_bundle(service)

    assert artifacts["source"]


@pytest.mark.parametrize(
    ("default", "required", "schema_default", "message"),
    [
        ('"hello"', [], "different", "default.*Python"),
        ('"hello"', [], 3, "default.*schema"),
        ("object()", [], "hello", "literal JSON"),
        ("{1, 2}", [], "hello", "literal JSON"),
        (None, ["message"], "hello", "required.*default"),
    ],
)
def test_input_defaults_must_agree_with_python_and_schema(
    service: Path, default: str | None, required: list[str], schema_default: object, message: str
) -> None:
    """Omitted input must not produce a value different from the inspected contract."""
    manifest = copy.deepcopy(TOOLS_MANIFEST)
    schema = manifest["tools"]["register"]["echo"]["input_schema"]
    schema["properties"]["message"]["default"] = schema_default
    schema["required"] = required
    write_tools_manifest(service, manifest)
    if default is not None:
        (service / "tools.py").write_text(
            TOOL_SOURCE.replace("def echo(message: str)", f"def echo(message: str = {default})")
        )

    with pytest.raises(ManifestError, match=message):
        recurse.build_tools_bundle(service)


def test_missing_tools_directory_is_rejected(tmp_path: Path) -> None:
    """An absent tools bundle target is reported before reading the manifest."""
    with pytest.raises(BundleError, match="is not a directory"):
        recurse.build_tools_bundle(tmp_path / "missing")


def test_tools_backend_must_include_declared_source(service: Path) -> None:
    """Successful build output must contain the tool module that was validated."""
    (service / "pyproject.toml").write_text(
        (service / "pyproject.toml")
        .read_text()
        .replace('"tools.yaml", "tools.py",', '"tools.yaml",')
    )

    with pytest.raises(BundleError, match=r"omitted required file: tools\.py"):
        recurse.build_tools_bundle(service)


def test_tools_builder_rejects_changed_lockfile(
    service: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The immutable artifact cannot carry dependencies different from validation."""
    original = recurse._build_sdist

    def change_lockfile(directory: Path, required_paths: set[str]) -> bytes:
        """Represent a backend modifying a required file before building it."""
        (directory / "uv.lock").write_text("version = 2\n")
        return original(directory, required_paths)

    monkeypatch.setattr(recurse, "_build_sdist", change_lockfile)
    with pytest.raises(BundleError, match=r"build backend changed uv\.lock"):
        recurse.build_tools_bundle(service)


def test_tools_builder_rejects_oversized_lockfile(
    service: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lockfile bounds apply to tools artifacts as well as agents."""
    monkeypatch.setattr(recurse, "_MAX_LOCKFILE_BYTES", 1)
    with pytest.raises(BundleError, match="lockfile exceeds 1 bytes"):
        recurse.build_tools_bundle(service)


def test_tools_lockfile_read_error_is_reported(
    service: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disappeared or unreadable lockfile cannot silently reach publication."""
    original = Path.read_bytes

    def fail_read(path: Path) -> bytes:
        """Fail only the validated dependency lockfile."""
        if path.name == "uv.lock":
            raise OSError("blocked")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    with pytest.raises(BundleError, match=r"runtime\.lockfile could not be read"):
        recurse.build_tools_bundle(service)


@pytest.mark.parametrize("default", ['"hello"', "3"])
def test_python_defaults_are_validated_when_schema_omits_default(
    service: Path, default: str
) -> None:
    """An optional argument uses Python's validated default without duplicating it."""
    manifest = copy.deepcopy(TOOLS_MANIFEST)
    manifest["tools"]["register"]["echo"]["input_schema"]["required"] = []
    write_tools_manifest(service, manifest)
    (service / "tools.py").write_text(
        TOOL_SOURCE.replace("def echo(message: str)", f"def echo(message: str = {default})")
    )

    if default == '"hello"':
        artifacts, _record = recurse.build_tools_bundle(service)
        assert artifacts["source"]
    else:
        with pytest.raises(ManifestError, match=r"Python default.*schema"):
            recurse.build_tools_bundle(service)


def test_tools_source_read_error_is_reported(
    service: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vanished source file cannot be silently omitted after file validation."""
    original = Path.read_bytes

    def fail_read(path: Path) -> bytes:
        """Represent an operating-system failure for the tool module."""
        if path.name == "tools.py":
            raise OSError("blocked")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    with pytest.raises(ManifestError, match=r"tools\.source could not be read"):
        recurse.build_tools_bundle(service)
