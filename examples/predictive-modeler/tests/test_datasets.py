"""Dataset adapters verify integrity and preserve real-data labels and split membership."""

import hashlib
import io
import json
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd
import pytest
from modeler import datasets
from modeler.contracts import ModelerError


def _archive(name: str, content: bytes) -> bytes:
    """Create one in-memory archive member for parser tests."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, content)
    return buffer.getvalue()


def test_download_requires_https_and_bounds_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """URI scheme and response size are checked before dataset parsing."""
    with pytest.raises(ModelerError, match="HTTPS"):
        datasets._download("file:///tmp/data.csv")

    class Response(io.BytesIO):
        """A bounded network response with observable redirect URL."""

        url = "https://example.org/data.csv"

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(b"a,b\n1,2\n"))
    assert datasets._download("https://example.org/data.csv") == b"a,b\n1,2\n"
    monkeypatch.setattr(datasets, "_MAX_BYTES", 2)
    with pytest.raises(ModelerError, match="32 MiB"):
        datasets._download("https://example.org/data.csv")
    Response.url = "http://example.org/data.csv"
    with pytest.raises(ModelerError, match="redirects"):
        datasets._download("https://example.org/data.csv")


def test_sources_verify_cached_and_downloaded_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached file is never trusted without comparing its recorded checksum."""
    content = b"real pinned source fixture"
    digest = hashlib.sha256(content).hexdigest()
    registry = tmp_path / "sources.json"
    registry.write_text(json.dumps({"case": {"sha256": digest, "url": "https://example.org/data"}}))
    monkeypatch.setattr(datasets, "__file__", str(tmp_path / "datasets.py"))
    monkeypatch.setattr(datasets, "_download", lambda url: content)
    cache = tmp_path / "cache"
    assert datasets._source("case", cache) == content
    assert datasets._source("case", cache) == content
    (cache / digest).write_bytes(b"changed")
    with pytest.raises(ModelerError, match="checksum"):
        datasets._source("case", cache)


def test_archives_reject_missing_members_and_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Archive access selects a single member and never extracts arbitrary paths."""
    data = _archive("data.csv", b"1234")
    assert datasets._member(data, "data.csv") == b"1234"
    with pytest.raises(ModelerError, match="exactly one"):
        datasets._member(data, "absent.csv")
    monkeypatch.setattr(datasets, "_MAX_BYTES", 1)
    with pytest.raises(ModelerError, match="Expanded"):
        datasets._member(data, "data.csv")


@pytest.mark.parametrize(
    "handle",
    ["bank-marketing", "dry-bean", "concrete", "bike-sharing", "tourism-monthly", "goemotions"],
)
def test_example_adapters_preserve_observations(
    handle: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Format conversion preserves targets and official multilabel partitions."""
    sources = {
        "bank": _archive(
            "bank-additional.zip", _archive("bank-additional-full.csv", b"x;y\n1;yes\n")
        ),
        "beans": _archive("Dry_Bean_Dataset.xlsx", b"excel"),
        "concrete": _archive("Concrete_Data.xls", b"excel"),
        "bikes": _archive("day.csv", b"dteday,cnt\n2020-01-01,12\n"),
        "tourism": _archive("data.tsf", b"@data\nT1:2000-01-01 00-00-00:1,2,3\n"),
        "emotions-labels": b"happy\nsad\n",
        "emotions-train": b"hello\t0,1\ta\n",
        "emotions-dev": b"world\t0\tb\n",
        "emotions-test": b"other\t1\tc\n",
    }
    # Match the published TSF timestamp spelling.
    sources["tourism"] = _archive("data.tsf", b"@data\nT1:2000-01-01 00-00-00:1,2,3\n")
    monkeypatch.setattr(datasets, "_source", lambda name, cache: sources[name])
    monkeypatch.setattr(pd, "read_excel", lambda source: pd.DataFrame([[1] * 9]))
    frame = datasets.load_dataset("example:" + handle, tmp_path)
    assert len(frame) == (3 if handle in {"tourism-monthly", "goemotions"} else 1)
    if handle == "goemotions":
        assert frame[["happy", "sad", "_split"]].to_dict("records") == [
            {"happy": 1, "sad": 1, "_split": "train"},
            {"happy": 1, "sad": 0, "_split": "validation"},
            {"happy": 0, "sad": 1, "_split": "test"},
        ]
    assert datasets.profile(frame)["rows"] == len(frame)


@pytest.mark.parametrize("extension", ["csv", "parquet"])
def test_https_tables_are_loaded_without_a_catalog(
    extension: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A user's own HTTPS table works with the same input contract."""
    expected = pd.DataFrame({"x": [1, 2], "y": [3, 4]})
    buffer = io.BytesIO()
    if extension == "csv":
        buffer.write(expected.to_csv(index=False).encode())
    else:
        expected.to_parquet(buffer, index=False)
    monkeypatch.setattr(datasets, "_download", lambda url: buffer.getvalue())
    actual = datasets.load_dataset(f"https://example.org/data.{extension}", tmp_path)
    pd.testing.assert_frame_equal(actual, expected)
    with pytest.raises(ModelerError, match="supported"):
        datasets.load_dataset("example:missing", tmp_path)
