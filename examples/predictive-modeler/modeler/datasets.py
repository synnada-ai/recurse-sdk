"""Bounded dataset loading and reproducible real-data example conversions."""

import hashlib
import io
import json
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, cast

import pandas as pd

from .contracts import ModelerError

__all__ = ["load_dataset", "profile"]
_MAX_BYTES = 32 * 1024 * 1024


def _download(url: str) -> bytes:
    """Read a bounded HTTPS response without allowing a scheme downgrade."""
    if urllib.parse.urlparse(url).scheme != "https":
        raise ModelerError("Dataset downloads require HTTPS.")
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - HTTPS checked above
        if urllib.parse.urlparse(response.url).scheme != "https":
            raise ModelerError("Dataset redirects must remain on HTTPS.")
        data = response.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise ModelerError("Dataset download exceeds the 32 MiB example limit.")
    return cast(bytes, data)


def _source(name: str, cache: Path) -> bytes:
    """Verify pinned source bytes whether cached or freshly downloaded."""
    sources = json.loads(Path(__file__).with_name("sources.json").read_text())
    source = sources[name]
    path = cache / source["sha256"]
    data = path.read_bytes() if path.exists() else _download(source["url"])
    if hashlib.sha256(data).hexdigest() != source["sha256"]:
        raise ModelerError(f"Dataset checksum mismatch for {name}; do not use changed data.")
    cache.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def _member(data: bytes, suffix: str) -> bytes:
    """Read one bounded archive member without extracting files."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        matches = [name for name in archive.namelist() if name.endswith(suffix)]
        if len(matches) != 1:
            raise ModelerError(f"Expected exactly one archive member ending in {suffix!r}.")
        if archive.getinfo(matches[0]).file_size > _MAX_BYTES:
            raise ModelerError("Expanded dataset exceeds 32 MiB.")
        return archive.read(matches[0])


def _emotions(cache: Path) -> pd.DataFrame:
    """Preserve all emotion labels and the official three-way partition."""
    labels = _source("emotions-labels", cache).decode().splitlines()
    frames = []
    for split in ["train", "dev", "test"]:
        frame = pd.read_csv(
            io.BytesIO(_source(f"emotions-{split}", cache)),
            sep="\t",
            names=["text", "labels", "comment_id"],
            dtype=str,
        )
        labelsets = frame.pop("labels").str.split(",")
        for index, label in enumerate(labels):
            frame[label] = labelsets.map(lambda values, i=index: int(str(i) in values))
        frame["_split"] = "validation" if split == "dev" else split
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def _tourism(data: bytes) -> pd.DataFrame:
    """Convert published monthly TSF observations to a long-form table."""
    text = _member(data, ".tsf").decode("cp1252")
    frames = []
    for line in text.split("@data", 1)[1].strip().splitlines():
        name, start, values = line.split(":")
        observations = [float(value) for value in values.split(",")]
        frames.append(
            pd.DataFrame(
                {
                    "series": name,
                    "date": pd.date_range(
                        pd.Timestamp(start.replace("-00-00-00", "")),
                        periods=len(observations),
                        freq="MS",
                    ),
                    "value": observations,
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def load_dataset(handle: str, cache: Path) -> pd.DataFrame:  # noqa: PLR0911 - format dispatch
    """Load an example handle or HTTPS CSV/Parquet URI without changing target values.

    Args:
        handle: example:bank-marketing, dry-bean, goemotions, concrete, bike-sharing,
            tourism-monthly (each with the example: prefix), or an HTTPS URI.
        cache: Directory for checksummed source downloads.

    Returns:
        A table with normalized column names for the bundled examples.
    """
    if handle == "example:bank-marketing":
        data = _member(_source("bank", cache), "bank-additional.zip")
        return pd.read_csv(io.BytesIO(_member(data, "bank-additional-full.csv")), sep=";")
    if handle == "example:dry-bean":
        data = _member(_source("beans", cache), "Dry_Bean_Dataset.xlsx")
        return pd.read_excel(io.BytesIO(data))
    if handle == "example:concrete":
        data = _member(_source("concrete", cache), "Concrete_Data.xls")
        frame = pd.read_excel(io.BytesIO(data))
        frame.columns = [
            "cement",
            "slag",
            "fly_ash",
            "water",
            "superplasticizer",
            "coarse_aggregate",
            "fine_aggregate",
            "age",
            "strength",
        ]
        return frame
    if handle == "example:bike-sharing":
        return pd.read_csv(io.BytesIO(_member(_source("bikes", cache), "day.csv")))
    if handle == "example:goemotions":
        return _emotions(cache)
    if handle == "example:tourism-monthly":
        return _tourism(_source("tourism", cache))
    extension = Path(urllib.parse.urlparse(handle).path).suffix.lower()
    if extension not in {".csv", ".parquet"}:
        raise ModelerError("Use a supported example: handle or HTTPS .csv/.parquet URI.")
    buffer = io.BytesIO(_download(handle))
    return pd.read_csv(buffer) if extension == ".csv" else pd.read_parquet(buffer)


def profile(data: pd.DataFrame) -> dict[str, Any]:
    """Describe schema and missingness without exposing held-out target observations."""
    return {
        "rows": len(data),
        "columns": {
            str(column): {
                "dtype": str(data[column].dtype),
                "missing": int(data[column].isna().sum()),
                "distinct": int(data[column].nunique()),
            }
            for column in data.columns
        },
    }
