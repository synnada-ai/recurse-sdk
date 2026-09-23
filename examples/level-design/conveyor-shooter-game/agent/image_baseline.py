# Copyright (C) Synnada, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Deterministically convert normalized PNG artwork into frozen conveyor shooter grids."""

import hashlib
import json
import math
import struct
import zlib
from dataclasses import dataclass
from typing import Any, Literal

import audit_dimensions
import render

Variant = Literal["crisp", "balanced", "edge", "contrast", "detail", "colour-mix"]
VARIANTS: tuple[Variant, ...] = (
    "crisp",
    "balanced",
    "edge",
    "contrast",
    "detail",
    "colour-mix",
)
SELECTION_VARIANTS: tuple[Variant, ...] = ("crisp", "balanced", "edge", "contrast")
RGBA = tuple[int, int, int, int]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_SOURCE_BYTES = 9_000_000
MAX_SOURCE_DIMENSION = 4096
# A decoded pixel uses several Python objects. Keep four concurrent workers comfortably below
# the production service's 2 GiB memory envelope, even before accounting for solver state.
MAX_SOURCE_PIXELS = 1_000_000


#: PNG structure: bytes of chunk framing, IHDR payload size, and the supported encodings.
_PNG_CHUNK_OVERHEAD = 12
_IHDR_SIZE = 13
_SUPPORTED_BIT_DEPTH = 8
_COLOUR_TYPE_RGB = 2
_COLOUR_TYPE_GREY_ALPHA = 4
#: Alpha at or above which a sampled pixel becomes a cube, and below which it is invisible.
_OPAQUE_ALPHA = 96
_VISIBLE_ALPHA = 32
#: Colour-distance thresholds for contrast boosting and background detection.
_HIGH_CONTRAST = 105
_UNIFORM_CORNER_DISTANCE = 58
_BACKGROUND_DISTANCE = 36
#: A conversion grid needs at least this many cells per side.
_MIN_GRID_SIDE = 2


@dataclass(frozen=True)
class Raster:
    """A small dependency-free RGBA image."""

    width: int
    height: int
    pixels: tuple[RGBA, ...]

    def get(self, x: int, y: int) -> RGBA:
        """Return a clamped pixel."""
        x = min(self.width - 1, max(0, x))
        y = min(self.height - 1, max(0, y))
        return self.pixels[y * self.width + x]


@dataclass(frozen=True)
class PreparedImage:
    """One validated source decoded once for all deterministic candidates."""

    raster: Raster
    source_sha256: str


@dataclass(frozen=True)
class Conversion:
    """One immutable image-to-grid result."""

    width: int
    height: int
    variant: Variant
    picture: dict[tuple[int, int], int]
    source_sha256: str
    crop: tuple[int, int, int, int]

    def raw_level(self) -> dict[str, Any]:
        """Return the minimal level surface used by renderers and art metrics."""
        return {
            "PixelImageData": {
                "width": self.width,
                "height": self.height,
                "pixels": [
                    {"x": x, "y": y, "material": material, "areaX": 1, "areaY": 1}
                    for (x, y), material in sorted(self.picture.items())
                ],
            }
        }

    def frozen_artifact(self) -> dict[str, Any]:
        """Return a content-addressed visual baseline suitable for later gameplay work."""
        raw = self.raw_level()
        pixels = raw["PixelImageData"]["pixels"]
        canonical = json.dumps(pixels, separators=(",", ":"), sort_keys=True).encode()
        return {
            "schema": "conveyor-frozen-art-v1",
            "source_sha256": self.source_sha256,
            "art_sha256": hashlib.sha256(canonical).hexdigest(),
            "variant": self.variant,
            "crop": list(self.crop),
            "PixelImageData": raw["PixelImageData"],
            "artwork": audit_dimensions.artwork_metrics(raw),
        }


def _paeth(left: int, up: int, upper_left: int) -> int:
    """PNG Paeth predictor."""
    estimate = left + up - upper_left
    distances = (abs(estimate - left), abs(estimate - up), abs(estimate - upper_left))
    return (left, up, upper_left)[distances.index(min(distances))]


def _decode_png_payload(  # noqa: PLR0912, PLR0915 - sequential PNG chunk validation
    source: bytes,
) -> tuple[int, int, int, int, bytes]:
    """Validate PNG structure and return its bounded decompressed scanlines."""
    if len(source) > MAX_SOURCE_BYTES:
        raise ValueError(f"source PNG is {len(source)} bytes; maximum is {MAX_SOURCE_BYTES} bytes")
    if not source.startswith(PNG_SIGNATURE):
        raise ValueError("source image must be normalized to PNG before conversion")
    offset = 8
    width = height = depth = colour_type = interlace = -1
    compressed = bytearray()
    saw_header = saw_data = saw_end = False
    chunk_index = 0
    while offset < len(source):
        if len(source) - offset < _PNG_CHUNK_OVERHEAD:
            raise ValueError("PNG ends inside a chunk header")
        size = struct.unpack(">I", source[offset : offset + 4])[0]
        kind = source[offset + 4 : offset + 8]
        payload_start = offset + 8
        payload_end = payload_start + size
        chunk_end = payload_end + 4
        if chunk_end > len(source):
            raise ValueError(f"PNG chunk {kind!r} exceeds the payload boundary")
        payload = source[payload_start:payload_end]
        expected_crc = struct.unpack(">I", source[payload_end:chunk_end])[0]
        actual_crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise ValueError(f"PNG chunk {kind!r} has an invalid CRC")
        offset = chunk_end
        if kind == b"IHDR":
            if saw_header or chunk_index != 0 or size != _IHDR_SIZE:
                raise ValueError("PNG must begin with exactly one 13-byte IHDR chunk")
            width, height, depth, colour_type, _compression, _filter, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if depth != _SUPPORTED_BIT_DEPTH:
                raise ValueError(f"PNG bit depth must be 8, got {depth}")
            if _compression != 0 or _filter != 0:
                raise ValueError("PNG uses unsupported compression or filtering methods")
            saw_header = True
        elif kind == b"IDAT":
            if not saw_header or saw_end:
                raise ValueError("PNG IDAT appears outside the image data section")
            compressed.extend(payload)
            saw_data = True
        elif kind == b"IEND":
            if size != 0 or not saw_header or not saw_data:
                raise ValueError("PNG has an invalid IEND chunk")
            saw_end = True
            break
        elif kind[0] & 32 == 0:
            raise ValueError(f"PNG critical chunk {kind!r} is unsupported")
        chunk_index += 1
    if not saw_end:
        raise ValueError("PNG is missing its IEND chunk")
    if offset != len(source):
        raise ValueError("PNG contains trailing bytes after IEND")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(colour_type)
    if width <= 0 or height <= 0 or channels is None or interlace != 0:
        raise ValueError(
            f"unsupported PNG: {width}x{height}, colour type {colour_type}, interlace {interlace}"
        )
    if width > MAX_SOURCE_DIMENSION or height > MAX_SOURCE_DIMENSION:
        raise ValueError(
            f"source PNG dimensions {width}x{height} exceed {MAX_SOURCE_DIMENSION} per axis"
        )
    if width * height > MAX_SOURCE_PIXELS:
        raise ValueError(f"source PNG has {width * height} pixels; maximum is {MAX_SOURCE_PIXELS}")
    expected_size = (width * channels + 1) * height
    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(bytes(compressed), expected_size + 1)
    except zlib.error as exc:
        raise ValueError(f"PNG image data is not valid zlib: {exc}") from exc
    if len(raw) != expected_size or not decompressor.eof:
        raise ValueError(
            f"PNG expands to an invalid scanline size; expected exactly {expected_size} bytes"
        )
    if decompressor.unused_data or decompressor.unconsumed_tail:
        raise ValueError("PNG image data contains trailing or overlong compressed bytes")
    return width, height, colour_type, channels, raw


def validate_png(source: bytes) -> None:
    """Reject unsafe or structurally malformed normalized PNG bytes."""
    _decode_png_payload(source)


def png_dimensions(source: bytes) -> tuple[int, int]:
    """Return dimensions after applying the complete safe-PNG validation boundary."""
    width, height, _colour_type, _channels, _raw = _decode_png_payload(source)
    return width, height


def decode_png(source: bytes) -> Raster:
    """Decode a bounded non-interlaced 8-bit RGB/RGBA PNG using only stdlib."""
    width, height, colour_type, channels, raw = _decode_png_payload(source)
    stride = width * channels
    previous = bytearray(stride)
    rows: list[bytearray] = []
    position = 0
    for _y in range(height):
        filter_type = raw[position]
        encoded = raw[position + 1 : position + 1 + stride]
        position += stride + 1
        row = bytearray(stride)
        for index, value in enumerate(encoded):
            left = row[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            predictor = {
                0: 0,
                1: left,
                2: up,
                3: (left + up) // 2,
                4: _paeth(left, up, upper_left),
            }.get(filter_type)
            if predictor is None:
                raise ValueError(f"unsupported PNG row filter {filter_type}")
            row[index] = (value + predictor) & 255
        rows.append(row)
        previous = row
    pixels: list[RGBA] = []
    for row in rows:
        for x in range(width):
            values = row[x * channels : (x + 1) * channels]
            if colour_type == 0:
                pixels.append((values[0], values[0], values[0], 255))
            elif colour_type == _COLOUR_TYPE_RGB:
                pixels.append((values[0], values[1], values[2], 255))
            elif colour_type == _COLOUR_TYPE_GREY_ALPHA:
                pixels.append((values[0], values[0], values[0], values[1]))
            else:
                pixels.append((values[0], values[1], values[2], values[3]))
    return Raster(width, height, tuple(pixels))


def prepare_png(source: bytes) -> PreparedImage:
    """Decode one immutable request image for reuse across crop and sampling variants."""
    return PreparedImage(decode_png(source), hashlib.sha256(source).hexdigest())


def _distance(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    """Perceptually weighted distance between two RGB colours."""
    return math.sqrt(
        2 * (left[0] - right[0]) ** 2
        + 4 * (left[1] - right[1]) ** 2
        + 3 * (left[2] - right[2]) ** 2
    )


def _sample(raster: Raster, x: float, y: float, variant: Variant) -> RGBA:
    """Sample the raster at a fractional position using the variant's filter."""
    if variant == "crisp":
        return raster.get(round(x), round(y))
    x0, y0 = math.floor(x), math.floor(y)
    dx, dy = x - x0, y - y0
    neighbours = (
        (raster.get(x0, y0), (1 - dx) * (1 - dy)),
        (raster.get(x0 + 1, y0), dx * (1 - dy)),
        (raster.get(x0, y0 + 1), (1 - dx) * dy),
        (raster.get(x0 + 1, y0 + 1), dx * dy),
    )
    blended = (
        round(sum(pixel[0] * weight for pixel, weight in neighbours)),
        round(sum(pixel[1] * weight for pixel, weight in neighbours)),
        round(sum(pixel[2] * weight for pixel, weight in neighbours)),
        round(sum(pixel[3] * weight for pixel, weight in neighbours)),
    )
    if variant == "edge":
        colours = [pixel for pixel, _weight in neighbours]
        contrast = max(_distance(left, right) for left in colours for right in colours)
        if contrast > _HIGH_CONTRAST:
            return raster.get(round(x), round(y))
    return blended


def _cluster_materials(
    sampled: Raster,
    removed: set[tuple[int, int]],
    allowed: list[int],
) -> tuple[tuple[tuple[int, int, int], int], ...]:
    """Map deterministic source-colour clusters to distinct game materials."""
    colours = [
        sampled.get(x, y)[:3]
        for y in range(sampled.height)
        for x in range(sampled.width)
        if (x, y) not in removed and sampled.get(x, y)[3] >= _OPAQUE_ALPHA
    ]
    if not colours:
        return ()
    centres: list[tuple[int, int, int]] = [
        min(colours, key=sum),
        max(colours, key=sum),
    ]
    while len(centres) < min(8, len(set(colours))):
        centres.append(
            max(colours, key=lambda colour: min(_distance(colour, centre) for centre in centres))
        )
    for _iteration in range(8):
        groups: list[list[tuple[int, int, int]]] = [[] for _centre in centres]
        for colour in colours:
            index = min(range(len(centres)), key=lambda item: _distance(colour, centres[item]))
            groups[index].append(colour)
        centres = [
            (
                round(sum(colour[0] for colour in group) / len(group)),
                round(sum(colour[1] for colour in group) / len(group)),
                round(sum(colour[2] for colour in group) / len(group)),
            )
            if group
            else centre
            for centre, group in zip(centres, groups, strict=True)
        ]
    palette = [_rgb(value) for value in render.palette()]
    unused = set(allowed)
    mapping: list[tuple[tuple[int, int, int], int]] = []
    for centre in sorted(centres, key=lambda colour: (sum(colour), colour)):
        pool = unused or set(allowed)
        material = min(pool, key=lambda index: _distance(centre, palette[index]))
        mapping.append((centre, material))
        unused.discard(material)
    return tuple(mapping)


def _content_bbox(raster: Raster) -> tuple[int, int, int, int]:
    """Return visible bounds in one pass without materializing coordinate lists."""
    left, top = raster.width, raster.height
    right = bottom = -1
    for y in range(raster.height):
        for x in range(raster.width):
            if raster.get(x, y)[3] < _VISIBLE_ALPHA:
                continue
            left = min(left, x)
            top = min(top, y)
            right = max(right, x)
            bottom = max(bottom, y)
    if right < 0:
        raise ValueError("source PNG has no visible pixels")
    return left, top, right + 1, bottom + 1


def content_bbox(source: bytes) -> tuple[int, int, int, int]:
    """Return the smallest source rectangle containing non-transparent artwork."""
    return _content_bbox(decode_png(source))


def crop_variants(
    source: bytes | PreparedImage,
) -> dict[str, tuple[int, int, int, int]]:
    """Return deterministic tight, balanced and full-field composition windows."""
    prepared = source if isinstance(source, PreparedImage) else prepare_png(source)
    raster = prepared.raster
    left, top, right, bottom = _content_bbox(raster)

    def expand(fraction: float) -> tuple[int, int, int, int]:
        """Grow the content box by a fraction of its size, clamped to the image."""
        dx = round((right - left) * fraction)
        dy = round((bottom - top) * fraction)
        return (
            max(0, left - dx),
            max(0, top - dy),
            min(raster.width, right + dx),
            min(raster.height, bottom + dy),
        )

    return {
        "tight": expand(0.04),
        "balanced": expand(0.15),
        "full": (0, 0, raster.width, raster.height),
    }


def _resample(
    raster: Raster, box: tuple[int, int, int, int], width: int, height: int, variant: Variant
) -> Raster:
    """Resample a crop of the raster to the target grid."""
    left, top, right, bottom = box
    pixels: list[RGBA] = []
    for y in range(height):
        source_y = top + (y + 0.5) * (bottom - top) / height - 0.5
        for x in range(width):
            source_x = left + (x + 0.5) * (right - left) / width - 0.5
            pixels.append(_sample(raster, source_x, source_y, variant))
    sampled = Raster(width, height, tuple(pixels))
    if variant != "detail":
        return sampled
    sharpened: list[RGBA] = []
    for y in range(height):
        for x in range(width):
            centre = sampled.get(x, y)
            neighbours = [
                sampled.get(x - 1, y),
                sampled.get(x + 1, y),
                sampled.get(x, y - 1),
                sampled.get(x, y + 1),
            ]
            sharpened.append(
                tuple(
                    max(
                        0,
                        min(
                            255,
                            round(
                                centre[channel] * 1.7
                                - sum(pixel[channel] for pixel in neighbours) * 0.175
                            ),
                        ),
                    )
                    for channel in range(4)
                )  # type: ignore[arg-type]
            )
    return Raster(width, height, tuple(sharpened))


def _background(raster: Raster) -> tuple[int, int, int] | None:
    """Detect a uniform background colour from the corners, if any."""
    corners = [
        raster.get(0, 0),
        raster.get(raster.width - 1, 0),
        raster.get(0, raster.height - 1),
        raster.get(raster.width - 1, raster.height - 1),
    ]
    if max(_distance(corners[0], colour) for colour in corners[1:]) > _UNIFORM_CORNER_DISTANCE:
        return None
    return tuple(round(sum(colour[channel] for colour in corners) / 4) for channel in range(3))  # type: ignore[return-value]


def _connected_background(
    raster: Raster, background: tuple[int, int, int] | None
) -> set[tuple[int, int]]:
    """Pixels of the background colour connected to the image border."""
    if background is None:
        return set()
    pending = [(x, y) for x in range(raster.width) for y in (0, raster.height - 1)]
    pending.extend((x, y) for y in range(raster.height) for x in (0, raster.width - 1))
    removed: set[tuple[int, int]] = set()
    while pending:
        cell = pending.pop()
        if cell in removed:
            continue
        colour = raster.get(*cell)
        if colour[3] >= _OPAQUE_ALPHA and _distance(colour, background) >= _BACKGROUND_DISTANCE:
            continue
        removed.add(cell)
        x, y = cell
        pending.extend(
            neighbour
            for neighbour in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1))
            if 0 <= neighbour[0] < raster.width and 0 <= neighbour[1] < raster.height
        )
    return removed


def _rgb(hexcode: str) -> tuple[int, int, int]:
    """Parse a #rrggbb colour."""
    value = hexcode.removeprefix("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _material(
    colour: RGBA,
    x: int,
    y: int,
    variant: Variant,
    allowed: list[int],
) -> int:
    """Map one sampled colour to the nearest palette material."""
    palette = [_rgb(value) for value in render.palette()]
    ranked = sorted(allowed, key=lambda index: _distance(colour, palette[index]))
    if variant != "colour-mix":
        return ranked[0]
    first, second = ranked[:2]
    first_distance = _distance(colour, palette[first])
    second_distance = _distance(colour, palette[second])
    second_share = first_distance / max(first_distance + second_distance, 1e-9)
    bayer = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))
    return second if second_share > (bayer[y % 4][x % 4] + 0.5) / 16 else first


def convert(  # noqa: PLR0912, PLR0913 - one deterministic conversion pipeline
    source: bytes | PreparedImage,
    *,
    width: int,
    height: int,
    crop: tuple[int, int, int, int] | None = None,
    variant: Variant = "balanced",
    remove_background: bool = True,
) -> Conversion:
    """Convert normalized PNG bytes into game materials without model-authored pixels."""
    if width < _MIN_GRID_SIDE or height < _MIN_GRID_SIDE:
        raise ValueError(f"grid must be at least 2x2, got {width}x{height}")
    prepared = source if isinstance(source, PreparedImage) else prepare_png(source)
    opened = prepared.raster
    box = crop or (0, 0, opened.width, opened.height)
    left, top, right, bottom = box
    if not (0 <= left < right <= opened.width and 0 <= top < bottom <= opened.height):
        raise ValueError(f"crop {box!r} falls outside {opened.width}x{opened.height} source")
    sampled = _resample(opened, box, width, height, variant)
    background = _background(sampled) if remove_background else None
    removed = _connected_background(sampled, background)
    palette = [_rgb(value) for value in render.palette()]
    confirmed = sorted(render.CONFIRMED_HEX)
    nearest_counts: dict[int, int] = {}
    for y in range(height):
        for x in range(width):
            colour = sampled.get(x, y)
            if (x, y) in removed or colour[3] < _OPAQUE_ALPHA:
                continue
            nearest = min(confirmed, key=lambda index: _distance(colour, palette[index]))
            nearest_counts[nearest] = nearest_counts.get(nearest, 0) + 1
    allowed = [
        material
        for material, _count in sorted(
            nearest_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[:8]
    ]
    if variant == "contrast":
        allowed = confirmed
        cluster_mapping = _cluster_materials(sampled, removed, allowed)
    else:
        cluster_mapping = ()
    picture: dict[tuple[int, int], int] = {}
    for y in range(height):
        for x in range(width):
            colour = sampled.get(x, y)
            if (x, y) in removed or colour[3] < _OPAQUE_ALPHA:
                continue
            if cluster_mapping:
                material = min(cluster_mapping, key=lambda item: _distance(colour, item[0]))[1]
                picture[(x, height - 1 - y)] = material
            else:
                picture[(x, height - 1 - y)] = _material(colour, x, y, variant, allowed)
    if not picture:
        raise ValueError(
            "conversion removed every pixel; disable background removal or change crop"
        )
    return Conversion(width, height, variant, picture, prepared.source_sha256, box)


__all__ = [
    "SELECTION_VARIANTS",
    "VARIANTS",
    "Conversion",
    "PreparedImage",
    "Variant",
    "content_bbox",
    "convert",
    "crop_variants",
    "decode_png",
    "png_dimensions",
    "prepare_png",
    "validate_png",
]
