"""Verify the published source archive and the wheel built from it."""

import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path


def test_distributions_contain_only_runtime_and_package_metadata(tmp_path: Path) -> None:
    """A lean sdist must still build a wheel containing the CLI, SDK, and schema."""
    root = Path(__file__).parents[1]
    uv = shutil.which("uv")
    assert uv is not None
    subprocess.run(  # noqa: S603 - fixed build command against this repository
        [uv, "build", "--no-sources", "--out-dir", str(tmp_path)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )

    with tarfile.open(next(tmp_path.glob("*.tar.gz"))) as archive:
        files = {
            str(Path(member.name).relative_to(Path(member.name).parts[0]))
            for member in archive.getmembers()
            if member.isfile()
        }
    assert files == {
        ".gitignore",  # Hatch always includes the VCS ignore file in source archives.
        "src/recurse.py",
        "src/_recurse_cli.py",
        "src/agent.schema.json",
        "README.md",
        "LICENSE",
        "pyproject.toml",
        "PKG-INFO",
    }

    with zipfile.ZipFile(next(tmp_path.glob("*.whl"))) as wheel:
        runtime_files = {name for name in wheel.namelist() if ".dist-info/" not in name}
        assert runtime_files == {"recurse.py", "_recurse_cli.py", "agent.schema.json"}
        for name in runtime_files:
            assert wheel.read(name) == (root / "src" / name).read_bytes()
