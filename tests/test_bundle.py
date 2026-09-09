"""Tests for authoring validation and standards-based application artifacts."""

import hashlib
import io
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import recurse
from recurse import BundleError, ManifestError, _is_canonical_relative_path, build_bundle

Edit = Callable[[Callable[[dict[str, Any]], None]], Path]


def _source_paths(source: bytes) -> set[str]:
    """Return paths below the standard root in one source distribution."""
    with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as archive:
        return {member.name.split("/", 1)[1] for member in archive if "/" in member.name}


def _sdist(
    files: dict[str, bytes] | None = None,
    *,
    extra_members: list[tarfile.TarInfo] | None = None,
) -> bytes:
    """Create a small source distribution for direct boundary tests."""
    contents = files or {
        "agent.yaml": b"manifest",
        "prompt.md": b"prompt",
        "pyproject.toml": b"project",
        "PKG-INFO": b"metadata",
        "tools.py": b"tools",
    }
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for path, data in contents.items():
            member = tarfile.TarInfo(f"application-0.1.0/{path}")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        for member in extra_members or []:
            archive.addfile(member)
    return stream.getvalue()


def _source_file(source: bytes, path: str) -> bytes:
    """Read one regular file below a source distribution's standard root."""
    with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as archive:
        member = next(member for member in archive if member.name.endswith(f"/{path}"))
        extracted = archive.extractfile(member)
        assert extracted is not None
        return extracted.read()


def test_build_uses_one_standard_sdist_containing_the_exact_lockfile(app: Path) -> None:
    """The backend-produced sdist is the complete immutable application artifact."""
    artifacts, record = build_bundle(app)

    assert set(artifacts) == {"source"}
    assert record == {
        "api_version": "recurse.application/v1alpha1",
        "source": {
            "sha256": hashlib.sha256(artifacts["source"]).hexdigest(),
            "size_bytes": len(artifacts["source"]),
        },
    }
    assert _source_file(artifacts["source"], "uv.lock") == (app / "uv.lock").read_bytes()
    assert {"PKG-INFO", "agent.yaml", "prompt.md", "pyproject.toml", "tools.py", "uv.lock"} <= (
        _source_paths(artifacts["source"])
    )


def test_build_backend_controls_source_membership(app: Path) -> None:
    """Unselected local junk never enters the backend-produced artifact."""
    (app / ".env").write_text("SECRET=do-not-package\n")
    (app / "notes.txt").write_text("not selected\n")

    artifacts, _record = build_bundle(app)

    paths = _source_paths(artifacts["source"])
    assert ".env" not in paths
    assert "notes.txt" not in paths


@pytest.mark.parametrize(
    "pyproject",
    [
        (
            '[build-system]\nrequires = ["hatchling"]\n'
            'build-backend = "hatchling.build"\n\n'
            '[project]\nname = "receipt-writer"\nversion = "0.1.0"\n'
            'description = "Fixture"\n\n'
            "[tool.hatch.build.targets.sdist]\n"
            'include = ["agent.yaml", "prompt.md", "tools.py", "uv.lock"]\n'
        ),
        (
            '[build-system]\nrequires = ["setuptools"]\n'
            'build-backend = "setuptools.build_meta"\n\n'
            '[project]\nname = "receipt-writer"\nversion = "0.1.0"\n'
            'description = "Fixture"\n\n'
            '[tool.setuptools]\npy-modules = ["tools"]\n'
            "[tool.setuptools.data-files]\n"
            '"." = ["agent.yaml", "prompt.md", "uv.lock"]\n'
        ),
        (
            '[build-system]\nrequires = ["flit_core>=3.12,<4"]\n'
            'build-backend = "flit_core.buildapi"\n\n'
            '[project]\nname = "receipt-writer"\nversion = "0.1.0"\n'
            'description = "Fixture"\nauthors = [{name = "Recurse"}]\n\n'
            '[tool.flit.module]\nname = "tools"\n'
            '[tool.flit.sdist]\ninclude = ["agent.yaml", "prompt.md", "uv.lock"]\n'
        ),
    ],
    ids=["hatchling", "setuptools", "flit"],
)
def test_supported_build_backends_select_complete_application(app: Path, pyproject: str) -> None:
    """Common standards-based backends can include every declared runtime file."""
    (app / "pyproject.toml").write_text(pyproject)

    artifacts, _record = build_bundle(app)

    assert {
        "agent.yaml",
        "prompt.md",
        "pyproject.toml",
        "PKG-INFO",
        "tools.py",
        "uv.lock",
    } <= (_source_paths(artifacts["source"]))
    assert _source_file(artifacts["source"], "uv.lock") == (app / "uv.lock").read_bytes()


@pytest.mark.parametrize(
    "pyproject",
    [
        '[project]\nname = "receipt-writer"\nversion = "0.1.0"\n',
        '[build-system]\nrequires = ["hatchling"]\n[project]\nname = "x"\nversion = "1"\n',
        (
            '[build-system]\nbuild-backend = "hatchling.build"\n'
            '[project]\nname = "x"\nversion = "1"\n'
        ),
    ],
)
def test_build_requires_an_explicit_build_backend(app: Path, pyproject: str) -> None:
    """The SDK never relies on uv's legacy backend fallback for file selection."""
    (app / "pyproject.toml").write_text(pyproject)

    with pytest.raises(BundleError, match="explicit build-system"):
        build_bundle(app)


def test_backend_must_include_every_runtime_file(app: Path) -> None:
    """A declared file omitted by backend configuration fails before upload."""
    (app / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n'
        '[project]\nname = "receipt-writer"\nversion = "0.1.0"\n'
        'description = "Fixture"\n\n'
        '[tool.hatch.build.targets.sdist]\ninclude = ["agent.yaml", "tools.py"]\n'
    )

    with pytest.raises(BundleError, match=r"omitted required file: prompt\.md"):
        build_bundle(app)


def test_missing_application_directory_is_rejected(tmp_path: Path) -> None:
    """A non-directory build target is a clear error."""
    with pytest.raises(BundleError, match="is not a directory"):
        build_bundle(tmp_path / "missing")


def test_missing_or_invalid_pyproject_is_rejected(app: Path) -> None:
    """Project metadata must exist and parse before the backend runs."""
    (app / "pyproject.toml").unlink()
    with pytest.raises(ManifestError, match=r"^pyproject\.toml does not exist$"):
        build_bundle(app)
    (app / "pyproject.toml").write_text("[")
    with pytest.raises(BundleError, match=r"^pyproject\.toml could not be read$"):
        build_bundle(app)


def test_unreadable_pyproject_is_a_build_error(app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An operating-system metadata read failure has a stable message."""
    original = Path.read_text

    def fail(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        """Fail only the project metadata read."""
        if path.name == "pyproject.toml":
            raise OSError("blocked")
        return original(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", fail)
    with pytest.raises(BundleError, match=r"^pyproject\.toml could not be read$"):
        build_bundle(app)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("prompt", "missing.md", r"^agent\.prompt does not exist$"),
        ("prompt", "../outside.md", r"^agent\.prompt is not a canonical path$"),
        ("prompt", "/etc/hosts", r"^agent\.prompt is not a canonical path$"),
        ("prompt", "./prompt.md", r"^agent\.prompt is not a canonical path$"),
        ("prompt", "prompt\\.md", r"^agent\.prompt is not a canonical path$"),
    ],
)
def test_declared_paths_must_be_canonical_and_present(
    edit_manifest: Edit, field: str, value: str, message: str
) -> None:
    """Traversal, absolute, dotted, and missing declared paths are refused."""
    app = edit_manifest(lambda manifest: manifest["agent"].__setitem__(field, value))
    with pytest.raises(ManifestError, match=message):
        build_bundle(app)


def test_declared_files_must_be_regular_files(app: Path) -> None:
    """A declared directory is not accepted as a runtime file."""
    (app / "prompt.md").unlink()
    (app / "prompt.md").mkdir()
    with pytest.raises(ManifestError, match=r"^agent\.prompt does not exist$"):
        build_bundle(app)


def test_tool_source_is_never_imported(app: Path) -> None:
    """Module-level side effects do not execute during authoring checks."""
    marker = app / "imported.marker"
    (app / "tools.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported')\n" + (app / "tools.py").read_text()
    )

    build_bundle(app)

    assert not marker.exists()


def test_unreadable_tool_source_is_an_authoring_error(
    app: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool source read failure is reported before the build backend runs."""
    original = Path.read_bytes

    def unreadable(path: Path) -> bytes:
        """Fail only the declared tool source read."""
        if path.name == "tools.py":
            raise OSError("blocked")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(ManifestError, match=r"tools\.source could not be read"):
        build_bundle(app)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("def broken(:\n", r"^tools\.source is not valid Python$"),
        ("x = 1\n", r"^registered tool does not exist: write_receipt$"),
        (
            "def write_receipt(item: str) -> str:\n"
            '    """Write.\n\n    Args:\n        item: Item.\n\n'
            '    Returns:\n        Text.\n    """\n    return item\n'
            "def stray(x: int) -> int:\n"
            '    """Stray.\n\n    Args:\n        x: Value.\n\n'
            '    Returns:\n        Value.\n    """\n    return x\n',
            r"^public tool is not registered: stray$",
        ),
        (
            "def write_receipt(item) -> str:\n"
            '    """Write.\n\n    Args:\n        item: Item.\n\n'
            '    Returns:\n        Text.\n    """\n    return item\n',
            r"^tool write_receipt parameter item requires a type annotation$",
        ),
        (
            "def write_receipt(item: str):\n"
            '    """Write.\n\n    Args:\n        item: Item.\n\n'
            '    Returns:\n        Text.\n    """\n    return item\n',
            r"^tool write_receipt requires a return type annotation$",
        ),
        (
            "def write_receipt(item: str) -> str:\n    return item\n",
            r"^tool write_receipt requires a docstring$",
        ),
        (
            "def write_receipt(item: str) -> str:\n"
            '    """Write.\n\n    Returns:\n        Text.\n    """\n    return item\n',
            r"^tool write_receipt docstring must describe parameter item$",
        ),
        (
            "def write_receipt(item: str) -> str:\n"
            '    """Write.\n\n    Args:\n        item: Item.\n    """\n    return item\n',
            r"^tool write_receipt docstring must describe the return value$",
        ),
        (
            "def write_receipt(item: str) -> None:\n"
            '    """Write.\n\n    Args:\n        item: Item.\n\n'
            '    Returns:\n        Nothing.\n    """\n',
            r"^tool write_receipt must not document a return value when it returns None$",
        ),
    ],
)
def test_tool_authoring_problems_are_reported(app: Path, source: str, message: str) -> None:
    """Each authoring defect names the tool and the problem."""
    (app / "tools.py").write_text(source)
    with pytest.raises(ManifestError, match=message):
        build_bundle(app)


def test_private_imported_and_none_returning_functions_are_supported(app: Path) -> None:
    """Only public authored callables are registered and None needs no Returns section."""
    (app / "tools.py").write_text(
        "from os.path import join\n_private = join\n"
        "def _helper(x: int) -> int:\n    return x\n"
        "def write_receipt(item: str) -> None:\n"
        '    """Write.\n\n    Args:\n        item: Item.\n    """\n'
    )

    build_bundle(app)


def test_async_public_functions_and_variadics_follow_tool_rules(app: Path) -> None:
    """Async and variadic public parameters receive the same authoring checks."""
    (app / "tools.py").write_text(
        (app / "tools.py").read_text() + "async def poll(delay: float) -> None:\n"
        '    """Poll.\n\n    Args:\n        delay: Seconds.\n    """\n'
    )
    with pytest.raises(ManifestError, match=r"^public tool is not registered: poll$"):
        build_bundle(app)
    (app / "tools.py").write_text(
        "def write_receipt(item: str, **labels) -> str:\n"
        '    """Write.\n\n    Args:\n        item: Item.\n        labels: Labels.\n\n'
        '    Returns:\n        Text.\n    """\n    return item\n'
    )
    with pytest.raises(ManifestError, match="parameter labels requires a type annotation"):
        build_bundle(app)


def test_canonical_path_helper_rejects_surrogates() -> None:
    """Surrogate-bearing names can never be canonical archive paths."""
    assert _is_canonical_relative_path("notes.txt")
    assert not _is_canonical_relative_path("bad\udc80name")


def test_sdist_boundary_rejects_malformed_oversized_and_multi_root_archives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed bytes, total limits, and ambiguous roots fail independently."""
    required = {"agent.yaml", "prompt.md", "pyproject.toml", "PKG-INFO", "tools.py"}
    with pytest.raises(BundleError, match="valid source distribution"):
        recurse._inspect_sdist(b"not gzip", required)
    monkeypatch.setattr(recurse, "_MAX_SOURCE_BYTES", 1)
    with pytest.raises(BundleError, match="source distribution exceeds 1 bytes"):
        recurse._inspect_sdist(_sdist(), required)
    monkeypatch.setattr(recurse, "_MAX_SOURCE_BYTES", 64 * 1024 * 1024)
    with pytest.raises(BundleError, match="one standard root"):
        recurse._inspect_sdist(_sdist({"../outside": b"x"}), set())
    second_root = tarfile.TarInfo("other-0.1.0/file.txt")
    with pytest.raises(BundleError, match="one standard root"):
        recurse._inspect_sdist(_sdist(extra_members=[second_root]), set())


def test_sdist_boundary_rejects_member_limits_and_missing_declarations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Member count and required application files are bounded before upload."""
    required = {"agent.yaml", "prompt.md", "pyproject.toml", "PKG-INFO", "tools.py"}
    monkeypatch.setattr(recurse, "_MAX_SOURCE_MEMBERS", 1)
    with pytest.raises(BundleError, match="more than 1 members"):
        recurse._inspect_sdist(_sdist(), required)
    monkeypatch.setattr(recurse, "_MAX_SOURCE_MEMBERS", 1024)
    with pytest.raises(BundleError, match=r"omitted required file: prompt\.md"):
        recurse._inspect_sdist(
            _sdist({name: b"x" for name in required if name != "prompt.md"}), required
        )


def test_sdist_boundary_rejects_links_duplicates_and_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archive structure and expanded bytes remain bounded despite compression."""
    link = tarfile.TarInfo("application-0.1.0/link")
    link.type = tarfile.SYMTYPE
    link.linkname = "agent.yaml"
    with pytest.raises(BundleError, match="non-file member"):
        recurse._inspect_sdist(_sdist(extra_members=[link]), set())

    duplicate = tarfile.TarInfo("application-0.1.0/agent.yaml")
    with pytest.raises(BundleError, match="duplicate path"):
        recurse._inspect_sdist(_sdist(extra_members=[duplicate]), set())

    monkeypatch.setattr(recurse, "_MAX_EXPANDED_SOURCE_BYTES", 1)
    with pytest.raises(BundleError, match="expands beyond 1 bytes"):
        recurse._inspect_sdist(_sdist(), set())


@pytest.mark.parametrize(
    ("returncode", "stderr", "message"),
    [
        (1, "backend exploded\n", "source distribution build failed: backend exploded"),
        (1, "", "source distribution build failed"),
    ],
)
def test_backend_failures_are_reported(
    app: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stderr: str,
    message: str,
) -> None:
    """Backend failures surface one bounded local diagnostic."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=returncode, stderr=stderr),
    )
    with pytest.raises(BundleError, match=f"^{message}$"):
        build_bundle(app)


def test_missing_uv_is_reported(app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing uv executable has a direct setup error."""

    def missing(*args: object, **kwargs: object) -> None:
        """Simulate subprocess executable lookup failure."""
        raise OSError("missing")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(BundleError, match="uv is required"):
        build_bundle(app)


def test_backend_cannot_change_the_lockfile(app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lockfile inside the backend artifact must match the reviewed local input."""

    def changed_lockfile(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        """Produce one otherwise valid sdist containing different lockfile bytes."""
        del kwargs
        output = Path(arguments[arguments.index("--out-dir") + 1]) / "app-0.1.0.tar.gz"
        output.write_bytes(
            _sdist(
                {
                    "agent.yaml": b"manifest",
                    "prompt.md": b"prompt",
                    "pyproject.toml": b"project",
                    "PKG-INFO": b"metadata",
                    "tools.py": b"tools",
                    "uv.lock": b"changed",
                }
            )
        )
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(subprocess, "run", changed_lockfile)
    with pytest.raises(BundleError, match=r"build backend changed uv\.lock"):
        build_bundle(app)


def test_backend_must_produce_exactly_one_tarball(
    app: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing, multiple, and non-tar outputs cannot choose an artifact ambiguously."""

    def no_output(*args: object, **kwargs: object) -> SimpleNamespace:
        """Report backend success without writing an artifact."""
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(subprocess, "run", no_output)
    with pytest.raises(BundleError, match="exactly one source distribution"):
        build_bundle(app)


def test_unreadable_backend_output_is_reported(app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable successful backend artifact has a stable packaging error."""

    def one_output(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        """Write one nominal output into the requested directory."""
        del kwargs
        output = Path(arguments[arguments.index("--out-dir") + 1]) / "app-0.1.0.tar.gz"
        output.write_bytes(b"archive")
        return SimpleNamespace(returncode=0, stderr="")

    original = Path.read_bytes

    def unreadable(path: Path) -> bytes:
        """Fail only the backend output read."""
        if path.name.endswith(".tar.gz"):
            raise OSError("blocked")
        return original(path)

    monkeypatch.setattr(subprocess, "run", one_output)
    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(BundleError, match="source distribution could not be read"):
        build_bundle(app)


def test_lockfile_read_and_size_failures_are_reported(
    app: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lockfile is readable and bounded before the backend packages it."""
    original = Path.read_bytes

    def unreadable(path: Path) -> bytes:
        """Fail only the lockfile read."""
        if path.name == "uv.lock":
            raise OSError("blocked")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(BundleError, match=r"runtime\.lockfile could not be read"):
        build_bundle(app)
    monkeypatch.setattr(Path, "read_bytes", original)
    monkeypatch.setattr(recurse, "_MAX_LOCKFILE_BYTES", 1)
    with pytest.raises(BundleError, match="lockfile exceeds 1 bytes"):
        build_bundle(app)
