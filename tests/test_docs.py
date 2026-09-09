"""Documentation verification: generated references, docstrings, examples."""

import re
import runpy
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import check_docstrings  # noqa: E402 - local documentation tool after path setup
import generate_docs  # noqa: E402 - local documentation tool after path setup

import recurse  # noqa: E402 - installed SDK loaded after local tool path setup


def test_generated_references_are_not_stale() -> None:
    """The committed generated references match a fresh generation."""
    assert generate_docs.main(["--check"]) == 0


def test_readme_uses_uv_for_normal_installation() -> None:
    """Public setup distinguishes CLI and library installation with uv."""
    readme = (ROOT / "README.md").read_text()
    guide = (ROOT / "docs" / "guide.md").read_text()

    for document in (readme, guide):
        assert "uv tool install recurse-sdk" in document
        assert "uv add recurse-sdk" in document
        assert "pip install recurse-sdk" not in document


def test_guide_explains_how_to_shape_a_specialist_loop() -> None:
    """Authoring guidance covers the decisions that make recursion useful."""
    guide = (ROOT / "docs" / "guide.md").read_text()

    for phrase in (
        "## Designing a specialist loop",
        "## Outer-agent workflow",
        "bounded candidate-validator loop",
        "Building tools",
        "Validator tools",
        "construct, measure, and revise",
        "authoritative receipt",
        "mutable module globals",
        "Establish a direct baseline",
        "Deploy as MCP only",
    ):
        assert phrase in guide


def test_every_example_has_a_public_readme() -> None:
    """Each runnable example explains its problem and verified loop."""
    examples = ROOT / "examples"

    for application in sorted(path for path in examples.iterdir() if path.is_dir()):
        readme = (application / "README.md").read_text()
        for heading in (
            "## When to use it",
            "## How the loop works",
            "## Run it",
            "## Result",
            "## Limitations",
        ):
            assert heading in readme

    tiny_tuner = " ".join((examples / "tiny-tuner" / "README.md").read_text().split())
    rna_fold_lab = " ".join((examples / "rna-fold-lab" / "README.md").read_text().split())
    assert "A direct implementation is simpler" in tiny_tuner
    assert "first candidate do not justify Recurse" in rna_fold_lab


def test_lint_config_keeps_only_justified_test_pattern_exceptions() -> None:
    """Broad lint suppression cannot silently hide unrelated test findings."""
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert configuration["tool"]["ruff"]["lint"]["per-file-ignores"] == {
        "tests/**": ["S101", "PLR2004"],
        "examples/*/tests/**": ["S101", "PLR2004"],
    }


def test_generation_reports_stale_and_writes_fresh_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--check fails on drift; a write pass produces complete, fresh references."""
    monkeypatch.setattr(generate_docs, "DOCS", tmp_path / "reference")

    assert generate_docs.main(["--check"]) == 1
    assert "stale generated documentation" in capsys.readouterr().err
    assert generate_docs.main([]) == 0
    assert generate_docs.main(["--check"]) == 0
    api = (tmp_path / "reference" / "api.md").read_text()
    manifest = (tmp_path / "reference" / "manifest.md").read_text()
    for name in ["build_bundle", "context", "RunContext", "RecurseError"]:
        assert f"recurse.{name}" in api
    for field in ["api_version", "metadata.name", "tools.register", "inputs"]:
        assert f"`{field}`" in manifest
    assert "Default: `True`" in manifest
    assert "*literal, required*" in manifest
    assert "*choice, required*" in manifest
    assert manifest.count("*path, required*") == 2
    assert "Always `recurse.run/v1alpha1`." in manifest
    assert "keyed by public function name in `tools.source`" in manifest


def test_every_public_member_requires_a_docstring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rendering aborts when a public member loses its docstring."""
    monkeypatch.setattr(recurse.RunContext, "__doc__", None)
    with pytest.raises(SystemExit, match=r"recurse\.RunContext has no docstring"):
        generate_docs.render_api_reference()


def test_guide_has_only_the_two_ordinary_local_mcp_configuration_commands() -> None:
    """The guide configures hosts with the CLI bridge and no embedded credential."""
    guide = (ROOT / "docs" / "guide.md").read_text()
    commands = [line for line in guide.splitlines() if "mcp add recurse" in line]

    assert commands == [
        "codex mcp add recurse -- recurse mcp serve <deployment-id>",
        "claude mcp add recurse -- recurse mcp serve <deployment-id>",
    ]
    assert all("--env" not in command and "token" not in command for command in commands)
    codex_blocks = [
        block
        for block in re.findall(r"```toml\n(.*?)```", guide, re.S)
        if "[mcp_servers.recurse]" in block
    ]
    assert len(codex_blocks) == 1
    recurse_server = tomllib.loads(codex_blocks[0])["mcp_servers"]["recurse"]
    assert recurse_server == {
        "command": "recurse",
        "args": ["mcp", "serve", "<deployment-id>"],
        "startup_timeout_sec": 180,
        "tool_timeout_sec": 1140,
    }


def test_guide_python_examples_run_against_the_installed_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every runnable guide example executes against the installed package."""
    guide = (ROOT / "docs" / "guide.md").read_text()
    blocks = re.findall(r"```python\n(.*?)```", guide, re.S)
    assert blocks
    monkeypatch.chdir(ROOT)
    executed = 0
    for block in blocks:
        if "path/to/app" in block:
            continue
        exec(  # noqa: S102 - runnable public documentation is the behavior under test
            compile(block, "<guide>", "exec"), {}
        )
        executed += 1
    assert executed >= 2


def test_tools_run_as_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documentation tools run directly and exit with their check status."""
    monkeypatch.setattr(sys, "argv", ["generate_docs.py", "--check"])
    with pytest.raises(SystemExit) as generate_exit:
        runpy.run_path(str(ROOT / "tools" / "generate_docs.py"), run_name="__main__")
    assert generate_exit.value.code == 0

    monkeypatch.setattr(sys, "argv", ["check_docstrings.py"])
    with pytest.raises(SystemExit) as docstring_exit:
        runpy.run_path(str(ROOT / "tools" / "check_docstrings.py"), run_name="__main__")
    assert docstring_exit.value.code == 0


def test_the_whole_python_tree_has_complete_docstrings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every definition exists and the exported API documents its contract."""
    assert check_docstrings.main() == 0
    output = capsys.readouterr().out
    assert "0 definitions" in output
    assert "0 public API" in output


def test_public_api_docstrings_report_signature_and_behavior_gaps(tmp_path: Path) -> None:
    """Public diagnostics identify missing, stale, return, yield, and error documentation."""
    module = tmp_path / "public.py"
    module.write_text(
        '''"""Example public module."""

__all__ = ["PublicError", "render", "stream", "undocumented"]

class PublicError(Exception):
    """A public failure."""

def render(value: str, *, mode: str) -> str:
    """Render a value.

    Args:
        value: Value to render.
        stale: No longer accepted.
    """
    if not value:
        raise PublicError("empty")
    if mode == "invalid":
        raise ValueError("internal")
    return mode

def stream(value: str, *items: str, **options: str):
    """Yield a value.

    Args:
        value: Value to yield.
        *items: Additional values.
        **options: Stream options.
    """
    yield value

def undocumented() -> None:
    return None
'''
    )

    assert check_docstrings._public_api_errors(module) == [
        f"{module}:8: render: Args must document parameter mode",
        f"{module}:8: render: Args documents unknown parameter stale",
        f"{module}:8: render: Returns must describe the return value",
        f"{module}:8: render: Raises must document PublicError",
        f"{module}:21: stream: Yields must describe yielded values",
    ]

    private_module = tmp_path / "private.py"
    private_module.write_text('''"""Private module."""\n''')
    assert check_docstrings._public_api_errors(private_module) == []


def test_docstring_check_includes_public_api_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command fails with an exact public symbol diagnostic."""
    for name in ("src", "tests", "tools", "examples"):
        (tmp_path / name).mkdir()
    (tmp_path / "src" / "recurse.py").write_text(
        '''"""Example SDK."""
__all__ = ["render"]

def render(value: str) -> str:
    """Render a value."""
    return value
'''
    )

    assert check_docstrings.main(tmp_path) == 1
    error = capsys.readouterr().err
    assert f"{tmp_path / 'src' / 'recurse.py'}:4: render" in error
    assert "Args must document parameter value" in error
    assert "Returns must describe the return value" in error


def test_public_api_docstrings_ignore_nested_function_behavior(tmp_path: Path) -> None:
    """A private nested helper cannot change its exported function's documentation contract."""
    module = tmp_path / "nested.py"
    module.write_text(
        '''"""Example SDK."""
__all__ = ["PublicError", "configure"]

class PublicError(Exception):
    """A public failure."""

def configure() -> None:
    """Configure the SDK."""
    def nested():
        raise PublicError("private")
        yield "private"
'''
    )

    assert check_docstrings._public_api_errors(module) == []


def test_docstring_check_reports_each_omission(tmp_path: Path) -> None:
    """Missing module, class, method, and nested docstrings are each located exactly."""
    bare = tmp_path / "bare.py"
    bare.write_text("class Widget:\n    def spin(self):\n        def inner():\n            pass\n")

    missing = check_docstrings._missing_in_module(bare)

    names = sorted(entry.rsplit(": ", 1)[1] for entry in missing)
    assert names == ["Widget", "inner", "module", "spin"]
    assert all(str(bare) in entry for entry in missing)

    tree = tmp_path / "tree"
    (tree / "src").mkdir(parents=True)
    bare.replace(tree / "src" / "bare.py")
    for name in ("tests", "tools", "examples"):
        (tree / name).mkdir()
    assert check_docstrings.main(tree) == 1


def test_docstring_check_ignores_example_virtual_environments(tmp_path: Path) -> None:
    """Installed third-party modules cannot become SDK documentation obligations."""
    dependency = tmp_path / "examples" / "app" / ".venv" / "dependency.py"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("class Dependency: pass\n")
    assert check_docstrings.main(tmp_path) == 0


def test_public_fields_require_descriptions_and_reject_stale_names(tmp_path: Path) -> None:
    """Exported fields must agree with non-empty Google Attributes entries."""
    source = tmp_path / "public.py"
    source.write_text('''"""Example SDK."""
__all__ = ["Record", "Undocumented"]
class Record:
    """A public record.

    Attributes:
        documented: Meaningful field description.
        empty:
        stale: Removed field.
    """
    documented: str
    missing: int
    empty: str
    _internal: str
    setting = True

class Undocumented:
    value: str

class Private:
    value: str
''')
    assert check_docstrings._public_api_errors(source) == [
        f"{source}:3: Record: Attributes must document field empty",
        f"{source}:3: Record: Attributes must document field missing",
        f"{source}:3: Record: Attributes documents unknown field stale",
        f"{source}:17: Undocumented: Attributes must document field value",
    ]
