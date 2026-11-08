"""Rewrite a class-value PMTiles archive in a PNG encoding that every browser decodes exactly.

The version 2 cropland archive stores 8-bit gray plus alpha PNGs. WebKit converts gray
PNGs through a gray colour profile, so class 0 comes back as 1 in places. Truecolour and
palette PNGs are read exactly. This module reads the version 2 tiles, keeps their class
values and nodata, and writes them in another encoding with the same tile ids. It does not
warp the source again.

A tile array holds the class (0 or 1) and NODATA (255) where alpha is 0.

  python tiles/transcode.py inventory --out data/tiles/v3/inventory      writes inventory.npy and inventory.json
  python tiles/transcode.py sample --inventory data/tiles/v3/inventory.npy --out <dir>
  python tiles/transcode.py views --inventory data/tiles/v3/inventory.npy --dest <file> --box W,S,E,N
  python tiles/transcode.py transcode --src v2.pmtiles --dest v3.pmtiles --encoding palette-r1 [--tile-size 512]
  python tiles/transcode.py verify --a v2.pmtiles --b v3.pmtiles --encoding palette-r1
  python tiles/transcode.py bench --src <sample dir>/v2-sample.pmtiles --encoding rgb-r1
"""
from __future__ import annotations

import argparse
import concurrent.futures
import functools
import gzip
import json
import mmap
import os
import resource
import struct
import sys
import time
import urllib.request
import zlib
from pathlib import Path
from typing import Callable, Iterator

import numpy as np
from osgeo import gdal

sys.path.insert(0, str(Path(__file__).parent))

import pyramid  # noqa: E402
from pmtiles.tile import Compression, deserialize_directory, deserialize_header, tileid_to_zxy, zxy_to_tileid  # noqa: E402

gdal.UseExceptions()

NODATA = 255
SIZE = 256
V2_URL = "https://storage.googleapis.com/nature-watch-tiles/cropland/2024/v2/cropland.pmtiles"
ZAKOUMA = (10.02, 22.63)
NODATA_KEY = (0, 0, 1)  # an RGB colour no class uses; red 0 so a decoder that drops tRNS paints class 0


# ---- PNG writing --------------------------------------------------------------


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _png(rows: np.ndarray, bit_depth: int, colour_type: int, chunks: list[tuple[bytes, bytes]] = ()) -> bytes:
    """rows: one already packed row of bytes per row of a square image. Filter 0 on every row, as version 2."""
    height = rows.shape[0]
    raw = np.concatenate([np.zeros((height, 1), dtype=np.uint8), rows], axis=1).tobytes()
    out = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", height, height, bit_depth, colour_type, 0, 0, 0))
    for tag, data in chunks:
        out += _chunk(tag, data)
    return out + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b"")


def _check(values: np.ndarray) -> np.ndarray:
    nodata = values == NODATA
    bad = ~nodata & (values > 1)
    if bad.any():
        raise ValueError(f"class values other than 0, 1 and nodata: {np.unique(values[bad]).tolist()}")
    return nodata


def encode_gray_alpha(values: np.ndarray) -> bytes:
    nodata = _check(values)
    pixels = np.stack([np.where(nodata, 0, values), np.where(nodata, 0, 255)], axis=-1).astype(np.uint8)
    return _png(pixels.reshape(len(values), -1), 8, 4)


def _rgb(values: np.ndarray, scale: int) -> bytes:
    nodata = _check(values)
    pixels = np.zeros(values.shape + (3,), dtype=np.uint8)
    pixels[..., 0] = np.where(nodata, NODATA_KEY[0], values * scale)
    pixels[nodata] = NODATA_KEY
    chunks = [(b"tRNS", struct.pack(">HHH", *NODATA_KEY))] if nodata.any() else []
    return _png(pixels.reshape(len(values), -1), 8, 2, chunks)


def _rgba(values: np.ndarray, scale: int) -> bytes:
    nodata = _check(values)
    pixels = np.zeros(values.shape + (4,), dtype=np.uint8)
    pixels[..., 0] = np.where(nodata, 0, values * scale)
    pixels[..., 3] = np.where(nodata, 0, 255)
    return _png(pixels.reshape(len(values), -1), 8, 6)


def _rgb_nodata_class(values: np.ndarray) -> bytes:
    """RGB, no transparency. Nodata is red 2, a class the data does not use."""
    nodata = _check(values)
    pixels = np.zeros(values.shape + (3,), dtype=np.uint8)
    pixels[..., 0] = np.where(nodata, 2, values)
    return _png(pixels.reshape(len(values), -1), 8, 2)


def _palette(values: np.ndarray, scale: int) -> bytes:
    """Index 0 is class 0, 1 is class 1, 2 is nodata (tRNS alpha 0). 1 bit a pixel without nodata, 2 with."""
    nodata = _check(values)
    if nodata.any():
        index = np.where(nodata, 2, values).astype(np.uint8).reshape(len(values), -1, 4)
        rows = (index[..., 0] << 6) | (index[..., 1] << 4) | (index[..., 2] << 2) | index[..., 3]
        chunks = [(b"PLTE", bytes([0, 0, 0, scale, 0, 0, *NODATA_KEY])), (b"tRNS", b"\xff\xff\x00")]
        return _png(rows.astype(np.uint8), 2, 3, chunks)
    rows = np.packbits(values.astype(np.uint8), axis=1)
    return _png(rows, 1, 3, [(b"PLTE", bytes([0, 0, 0, scale, 0, 0]))])


def _pack(index: np.ndarray, depth: int) -> np.ndarray:
    height, width = index.shape
    if depth == 8:
        return index
    per = 8 // depth
    grouped = index.reshape(height, width // per, per).astype(np.uint8)
    rows = np.zeros((height, width // per), dtype=np.uint8)
    for i in range(per):
        rows |= grouped[..., i] << (8 - depth * (i + 1))
    return rows


def share_palette_png(data: bytes, steps: int = 14, curve: str = "linear", solid: bool = False) -> bytes:
    """A cropland-share tile (gray 0-255 plus alpha, as `cropland.py --resampling share` writes) as a palette PNG.

    Palette index 0 is nodata (tRNS alpha 0). Index k + 1 is shade k, k = 0..steps, and its red value is the
    share on a 0-255 scale: 255 * (k / steps) for curve "linear", 255 * (k / steps)^2 for "sqrt", which spaces
    the shades evenly in the square root of the share, so they are evenly spaced in opacity when opacity follows
    the square root. Up to 2, 14 or 254 steps fit 2, 4 or 8 bits. Any share above 0 keeps at least shade 1, so sparse
    cropland does not disappear at coarse zooms. Only the palette entries a tile uses are written.

    solid: two shades, share below 0.5 is 0 and from 0.5 is 255. For the zoom where the map draws no shading.
    """
    rgba = decode_rgba(data)
    gray, alpha = rgba[..., 0].astype(np.int32), rgba[..., 3]
    if solid:
        steps, curve = 1, "linear"
        level = (gray >= 128).astype(np.int32)
    else:
        x = np.sqrt(gray / 255) if curve == "sqrt" else gray / 255
        level = np.rint(x * steps).astype(np.int32)
        level = np.where(gray > 0, np.maximum(level, 1), 0)
    depth = 2 if steps + 2 <= 4 else 4 if steps + 2 <= 16 else 8
    index = np.where(alpha == 0, 0, level + 1).astype(np.uint8)
    used = int(index.max()) + 1
    palette = np.zeros((used, 3), dtype=np.uint8)
    for k in range(1, used):
        share = (k - 1) / steps
        palette[k, 0] = int(round((share**2 if curve == "sqrt" else share) * 255))
    palette[0] = NODATA_KEY
    return _png(_pack(index, depth), depth, 3, [(b"PLTE", palette.tobytes()), (b"tRNS", b"\x00")])


def share_values(data: bytes) -> np.ndarray:
    """Red value of each pixel (share on a 0-255 scale) and -1 for nodata, from any share PNG."""
    rgba = decode_rgba(data)
    return np.where(rgba[..., 3] == 0, -1, rgba[..., 0].astype(np.int32))


ENCODINGS: dict[str, Callable[[np.ndarray], bytes]] = {
    "gray-alpha": encode_gray_alpha,
    "rgb-r1": lambda values: _rgb(values, 1),
    "rgb-r255": lambda values: _rgb(values, 255),
    "rgba-r1": lambda values: _rgba(values, 1),
    "rgba-r255": lambda values: _rgba(values, 255),
    "palette-r1": lambda values: _palette(values, 1),
    "palette-r255": lambda values: _palette(values, 255),
    "rgb-r1-nodata2": _rgb_nodata_class,
}

DESCRIPTIONS = {
    "gray-alpha": "Gray 1 is cropland, gray 0 is other land, alpha 0 is nodata",
    "rgb-r1": "RGB. Red 1 is cropland, red 0 is other land. Nodata is RGB 0,0,1, transparent by tRNS",
    "rgb-r255": "RGB. Red 255 is cropland, red 0 is other land. Nodata is RGB 0,0,1, transparent by tRNS",
    "rgba-r1": "RGBA. Red 1 is cropland, red 0 is other land, alpha 0 is nodata",
    "rgba-r255": "RGBA. Red 255 is cropland, red 0 is other land, alpha 0 is nodata",
    "palette-r1": "Palette. Red 1 is cropland, red 0 is other land, a transparent entry is nodata",
    "palette-r255": "Palette. Red 255 is cropland, red 0 is other land, a transparent entry is nodata",
    "rgb-r1-nodata2": "RGB, opaque. Red 1 is cropland, red 0 is other land, red 2 is nodata",
}


# ---- PNG reading --------------------------------------------------------------


def _unfilter(data: bytes, height: int, stride: int, bpp: int) -> np.ndarray:
    raw = np.frombuffer(data, dtype=np.uint8).reshape(height, stride + 1)
    kinds = raw[:, 0]
    rows = raw[:, 1:].astype(np.int32)
    if not kinds.any():
        return rows.astype(np.uint8)
    out = np.zeros((height, stride), dtype=np.int32)
    for y in range(height):
        row, above = rows[y], out[y - 1] if y else np.zeros(stride, dtype=np.int32)
        kind = kinds[y]
        if kind == 0:
            out[y] = row
        elif kind == 2:
            out[y] = (row + above) & 0xFF
        else:
            line = np.zeros(stride, dtype=np.int32)
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up_left = above[i - bpp] if i >= bpp else 0
                if kind == 1:
                    predict = left
                elif kind == 3:
                    predict = (left + above[i]) >> 1
                elif kind == 4:
                    p = left + above[i] - up_left
                    pa, pb, pc = abs(p - left), abs(p - above[i]), abs(p - up_left)
                    predict = left if pa <= pb and pa <= pc else (above[i] if pb <= pc else up_left)
                else:
                    raise ValueError(f"PNG filter {kind}")
                line[i] = (row[i] + predict) & 0xFF
            out[y] = line
    return out.astype(np.uint8)


def decode_rgba(data: bytes) -> np.ndarray:
    """A PNG as straight RGBA, the way a browser hands it to WebGL, with no colour management."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    position, idat, palette, trns = 8, [], None, None
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        tag = data[position + 4 : position + 8]
        body = data[position + 8 : position + 8 + length]
        position += 12 + length
        if tag == b"IHDR":
            width, height, depth, colour, _c, _f, interlace = struct.unpack(">IIBBBBB", body)
            if depth not in (1, 2, 4, 8) or interlace:
                raise ValueError(f"unsupported PNG: depth {depth}, interlace {interlace}")
        elif tag == b"PLTE":
            palette = np.frombuffer(body, dtype=np.uint8).reshape(-1, 3)
        elif tag == b"tRNS":
            trns = body
        elif tag == b"IDAT":
            idat.append(body)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour]
    stride = (width * channels * depth + 7) // 8
    rows = _unfilter(zlib.decompress(b"".join(idat)), height, stride, max(1, channels * depth // 8))
    if depth < 8:
        bits = np.unpackbits(rows, axis=1).reshape(height, -1, depth)
        weights = 1 << np.arange(depth - 1, -1, -1)
        samples = (bits * weights).sum(axis=2)[:, : width * channels].astype(np.uint8)
    else:
        samples = rows
    samples = samples.reshape(height, width, channels)
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    if colour == 3:
        alpha = np.full(len(palette), 255, dtype=np.uint8)
        if trns:
            alpha[: len(trns)] = np.frombuffer(trns, dtype=np.uint8)
        index = samples[..., 0]
        rgba[..., :3], rgba[..., 3] = palette[index], alpha[index]
        return rgba
    if colour in (0, 4):
        rgba[..., :3] = samples[..., :1]
    else:
        rgba[..., :3] = samples[..., :3]
    rgba[..., 3] = samples[..., -1] if colour in (4, 6) else 255
    if trns and colour in (0, 2):
        key = np.array(struct.unpack(">" + "H" * (len(trns) // 2), trns), dtype=np.uint16)
        rgba[(samples.astype(np.uint16) == key).all(axis=-1), 3] = 0
    return rgba


def values_from_rgba(rgba: np.ndarray, encoding: str) -> np.ndarray:
    """Class values and NODATA from decoded RGBA, inverting the encoding."""
    channel = rgba[..., 0]
    if encoding == "rgb-r1-nodata2":
        return np.where(channel == 2, NODATA, channel).astype(np.uint8)
    scale = 255 if encoding.endswith("r255") else 1
    values = np.where(rgba[..., 3] == 0, NODATA, channel // scale).astype(np.uint8)
    opaque = rgba[..., 3] != 0
    if not np.isin(rgba[..., 3], (0, 255)).all() or not np.isin(channel[opaque], (0, scale)).all():
        raise ValueError(f"tile is not a valid {encoding} class tile")
    return values


def decode_values(data: bytes, encoding: str) -> np.ndarray:
    return values_from_rgba(decode_rgba(data), encoding)


def gdal_values(data: bytes, encoding: str) -> np.ndarray:
    """The same as decode_values, through GDAL's PNG driver: an independent decoder for checks."""
    name = f"/vsimem/check-{os.getpid()}-{id(data)}.png"
    gdal.FileFromMemBuffer(name, data)
    try:
        dataset = gdal.Open(name)
        bands = [dataset.GetRasterBand(i + 1) for i in range(dataset.RasterCount)]
        arrays = [band.ReadAsArray() for band in bands]
        rgba = np.zeros((dataset.RasterYSize, dataset.RasterXSize, 4), dtype=np.uint8)
        table = bands[0].GetColorTable()
        if table is not None:
            entries = np.array([table.GetColorEntry(i) for i in range(table.GetCount())], dtype=np.uint8)
            rgba[:] = entries[arrays[0]]
        elif len(arrays) in (1, 2):
            rgba[..., :3] = arrays[0][..., None]
            rgba[..., 3] = arrays[1] if len(arrays) == 2 else 255
        else:
            for i, band in enumerate(arrays):
                rgba[..., i] = band
            if len(arrays) == 3:
                rgba[..., 3] = 255
                keys = [band.GetNoDataValue() for band in bands]
                if all(key is not None for key in keys):
                    rgba[np.all([a == k for a, k in zip(arrays, keys)], axis=0), 3] = 0
        dataset = None
    finally:
        gdal.Unlink(name)
    return values_from_rgba(rgba, encoding)


# ---- version 2 tiles ----------------------------------------------------------


def decode_v2(data: bytes) -> np.ndarray:
    """A version 2 tile (8-bit gray plus alpha, filter 0) as class values. Refuses anything else."""
    if data[25] != 4 or data[24] != 8:
        raise ValueError("not an 8-bit gray plus alpha PNG")
    rgba = decode_rgba(data)
    alpha, gray = rgba[..., 3], rgba[..., 0]
    if not np.isin(alpha, (0, 255)).all():
        raise ValueError("alpha other than 0 and 255")
    if (gray[alpha == 0] != 0).any():
        raise ValueError("gray under alpha 0 is not 0, so nodata would lose information")
    return values_from_rgba(rgba, "gray-alpha")


def transcoder(encoding: str, cache_size: int = 65536) -> Callable[[bytes], bytes]:
    encode = ENCODINGS[encoding]

    @functools.lru_cache(maxsize=cache_size)
    def convert(data: bytes) -> bytes:
        return encode(decode_v2(data))

    return convert


def category(values: np.ndarray) -> str:
    nodata = values == NODATA
    if nodata.all():
        return "all-nodata"
    if nodata.any():
        return "partly-nodata"
    ones = int((values == 1).sum())
    return "all-zero" if ones == 0 else "all-one" if ones == values.size else "mixed"


# ---- reading archives ---------------------------------------------------------


class HttpSource:
    """get_bytes over HTTP Range. Counts requests and bytes."""

    def __init__(self, url: str):
        self.url, self.requests, self.bytes = url, 0, 0

    def __call__(self, offset: int, length: int) -> bytes:
        request = urllib.request.Request(self.url, headers={"Range": f"bytes={offset}-{offset + length - 1}"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    if response.status != 206:
                        raise RuntimeError(f"expected 206, got {response.status}")
                    data = response.read()
                break
            except OSError:
                if attempt == 3:
                    raise
                time.sleep(2**attempt)
        self.requests += 1
        self.bytes += len(data)
        return data


def file_source(path: Path):
    handle = open(path, "rb")
    mapped = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
    return lambda offset, length: bytes(mapped[offset : offset + length])


def read_header(get_bytes) -> dict:
    return deserialize_header(get_bytes(0, 127))


def read_metadata(get_bytes, header: dict) -> dict:
    data = get_bytes(header["metadata_offset"], header["metadata_length"])
    if header["internal_compression"] == Compression.GZIP:
        data = gzip.decompress(data)
    return json.loads(data)


def _directory(data: bytes, header: dict):
    if header["internal_compression"] != Compression.GZIP:
        raise ValueError("pmtiles reads gzip directories only")
    return deserialize_directory(data)


def directory_entries(get_bytes, header: dict) -> tuple[np.ndarray, dict]:
    """Every tile entry (tile_id, offset, length, run_length) of an archive, and directory stats.

    Reads the root and the whole leaf section in two requests.
    """
    root_raw = get_bytes(header["root_offset"], header["root_length"])
    leaves_raw = get_bytes(header["leaf_directory_offset"], header["leaf_directory_length"]) if header["leaf_directory_length"] else b""
    rows, leaf_sizes = [], []

    def walk(entries):
        for entry in entries:
            if entry.run_length:
                rows.append((entry.tile_id, entry.offset, entry.length, entry.run_length))
            else:
                leaf_sizes.append(entry.length)
                walk(_directory(leaves_raw[entry.offset : entry.offset + entry.length], header))

    walk(_directory(root_raw, header))
    table = np.array(rows, dtype=np.uint64)
    stats = {
        "root_bytes": len(root_raw),
        "leaf_section_bytes": len(leaves_raw),
        "leaves": len(leaf_sizes),
        "leaf_bytes_min": min(leaf_sizes, default=0),
        "leaf_bytes_median": int(np.median(leaf_sizes)) if leaf_sizes else 0,
        "leaf_bytes_max": max(leaf_sizes, default=0),
        "entries": len(rows),
    }
    return table, stats


def tiles_in_order(get_bytes) -> Iterator[tuple[int, bytes]]:
    """(tile_id, bytes) for every addressed tile, in tile id order."""
    header = read_header(get_bytes)
    table, _stats = directory_entries(get_bytes, header)
    base = header["tile_data_offset"]
    for tile_id, offset, length, run in table.tolist():
        data = get_bytes(base + offset, length)
        for i in range(run):
            yield tile_id + i, data


def zoom_base(zoom: int) -> int:
    return (4**zoom - 1) // 3


def tile_at(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    import math

    n = 2**zoom
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


# ---- commands -----------------------------------------------------------------


def peak_memory() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return usage if sys.platform == "darwin" else usage * 1024


def command_inventory(url: str, out: Path) -> dict:
    source = HttpSource(url)
    started = time.time()
    header = read_header(source)
    metadata = read_metadata(source, header)
    table, stats = directory_entries(source, header)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.save(out.with_suffix(".npy"), table)
    tile_ids, offsets, lengths, runs = (table[:, i] for i in range(4))
    zooms = np.searchsorted(np.array([zoom_base(z) for z in range(32)], dtype=np.uint64), tile_ids, side="right") - 1
    first = np.unique(offsets, return_index=True)[1]
    per_zoom = {}
    for zoom in range(int(header["max_zoom"]) + 1):
        here = zooms == zoom
        distinct_here = np.zeros(len(table), dtype=bool)
        distinct_here[first] = True
        distinct_here &= here
        per_zoom[zoom] = {
            "addressed": int(runs[here].sum()),
            "distinct_bodies": int(distinct_here.sum()),
            "distinct_bytes": int(lengths[distinct_here].sum()),
        }
    summary = {
        "url": url,
        "header": {key: (value.name if hasattr(value, "name") else value) for key, value in header.items()},
        "metadata": metadata,
        "directories": stats,
        "addressed_tiles": int(runs.sum()),
        "distinct_bodies": len(first),
        "per_zoom": per_zoom,
        "requests": source.requests,
        "bytes_read": source.bytes,
        "wall_seconds": round(time.time() - started, 1),
    }
    out.with_suffix(".json").write_text(json.dumps(summary, indent=2))
    return summary


def _addressed(table: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Expand runs: tile id, body offset and length for every addressed tile."""
    tile_ids, offsets, lengths, runs = (table[:, i] for i in range(4))
    runs_i = runs.astype(np.int64)
    starts = np.repeat(tile_ids, runs_i)
    step = np.arange(int(runs_i.sum()), dtype=np.uint64) - np.repeat(np.cumsum(runs_i) - runs_i, runs_i).astype(np.uint64)
    return starts + step, np.repeat(offsets, runs_i), np.repeat(lengths, runs_i)


def choose_sample(table: np.ndarray, max_zoom: int, per_zoom: int, seed: int) -> dict[int, list[str]]:
    """Tile ids to check, with the reason each was picked.

    Per zoom: random addressed tiles (mostly uniform), random distinct bodies (mostly
    mixed), and tiles with a missing neighbour (likely partly nodata). Plus the most
    shared bodies and the Zakouma column.
    """
    rng = np.random.default_rng(seed)
    ids, offsets, _lengths = _addressed(table)
    picked: dict[int, list[str]] = {}

    def add(tile_id: int, reason: str) -> None:
        picked.setdefault(int(tile_id), []).append(reason)

    def present(tile_id: int) -> bool:
        i = np.searchsorted(ids, tile_id)
        return i < len(ids) and int(ids[i]) == tile_id

    for zoom in range(max_zoom + 1):
        lo, hi = np.searchsorted(ids, [zoom_base(zoom), zoom_base(zoom + 1)])
        if hi == lo:
            continue
        here = np.arange(lo, hi)
        for i in rng.choice(here, min(per_zoom, len(here)), replace=False):
            add(ids[i], "random")
        _bodies, first = np.unique(offsets[lo:hi], return_index=True)
        for i in rng.choice(first + lo, min(per_zoom, len(first)), replace=False):
            add(ids[i], "distinct")
        edges = 0
        for i in rng.choice(here, min(len(here), per_zoom * 40), replace=False):
            z, x, y = tileid_to_zxy(int(ids[i]))
            n = 2**z
            neighbours = [((x + dx) % n, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)) if 0 <= y + dy < n]
            if any(not present(zxy_to_tileid(z, nx, ny)) for nx, ny in neighbours):
                add(ids[i], "edge")
                edges += 1
                if edges >= per_zoom:
                    break
    bodies, counts = np.unique(offsets, return_counts=True)
    for body in bodies[np.argsort(counts)[::-1][:10]]:
        add(ids[np.argmax(offsets == body)], "shared")
    lat, lon = ZAKOUMA
    for zoom in range(max_zoom + 1):
        x, y = tile_at(lat, lon, zoom)
        span = 2 ** max(0, zoom - 10)
        for dx in range(span):
            for dy in range(span):
                tile_id = zxy_to_tileid(zoom, x - x % span + dx, y - y % span + dy)
                if present(tile_id):
                    add(tile_id, "zakouma")
    return picked


def fetch_bodies(source: HttpSource, base: int, spans: list[tuple[int, int]], gap: int = 16384, limit: int = 1 << 20, workers: int = 8) -> dict[int, bytes]:
    """Bodies by offset, coalescing close ones into one Range request."""
    spans = sorted(set(spans))
    groups, current = [], []
    for offset, length in spans:
        if current and (offset - (current[-1][0] + current[-1][1]) > gap or offset + length - current[0][0] > limit):
            groups.append(current)
            current = []
        current.append((offset, length))
    if current:
        groups.append(current)

    def get(group):
        start = group[0][0]
        end = max(offset + length for offset, length in group)
        data = source(base + start, end - start)
        return {offset: data[offset - start : offset - start + length] for offset, length in group}

    bodies: dict[int, bytes] = {}
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        for part in pool.map(get, groups):
            bodies.update(part)
    return bodies


def extract(url: str, table: np.ndarray, tile_ids: list[int]) -> tuple[list[tuple[int, bytes]], HttpSource, dict]:
    source = HttpSource(url)
    header = read_header(source)
    ids, offsets, lengths = _addressed(table)
    where = np.searchsorted(ids, np.array(sorted(tile_ids), dtype=np.uint64))
    spans = [(int(offsets[i]), int(lengths[i])) for i in where]
    bodies = fetch_bodies(source, header["tile_data_offset"], spans)
    return [(int(ids[i]), bodies[int(offsets[i])]) for i in where], source, header


def view_tiles(table: np.ndarray, boxes: list[tuple[float, float, float, float]], full_zoom: int, max_zoom: int) -> list[int]:
    """Every present tile at zooms 0 to full_zoom, and inside the boxes (west, south, east, north) above it."""
    ids = _addressed(table)[0]
    keep = set(ids[ids < zoom_base(full_zoom + 1)].tolist())
    for west, south, east, north in boxes:
        for zoom in range(full_zoom + 1, max_zoom + 1):
            x0, y0 = tile_at(north, west, zoom)
            x1, y1 = tile_at(south, east, zoom)
            wanted = np.array([zxy_to_tileid(zoom, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)], dtype=np.uint64)
            found = np.searchsorted(ids, wanted)
            found = np.minimum(found, len(ids) - 1)
            keep.update(wanted[ids[found] == wanted].tolist())
    return sorted(keep)


def write_tiles(entries: list[tuple[int, bytes]], dest: Path, header: dict, metadata: dict) -> dict:
    keys = ("min_zoom", "max_zoom", "min_lon_e7", "min_lat_e7", "max_lon_e7", "max_lat_e7", "center_zoom", "center_lon_e7", "center_lat_e7")
    out_header = pyramid.archive_header((0, 0, 0, 0), 0, 0)
    out_header.update({key: header[key] for key in keys})
    return pyramid.write_archive(entries, dest, out_header, metadata)


def command_sample(url: str, inventory: Path, out: Path, per_zoom: int, seed: int) -> dict:
    """A stratified sample of tiles for the exactness check and the benchmark."""
    table = np.load(inventory)
    summary = json.loads(inventory.with_suffix(".json").read_text())
    out.mkdir(parents=True, exist_ok=True)
    picked = choose_sample(table, summary["header"]["max_zoom"], per_zoom, seed)
    entries, source, header = extract(url, table, list(picked))
    rows = []
    for tile_id, data in entries:
        z, x, y = tileid_to_zxy(tile_id)
        rows.append({"tile_id": tile_id, "z": z, "x": x, "y": y, "bytes": len(data), "reasons": picked[tile_id], "category": category(decode_v2(data))})
    report = write_tiles(entries, out / "v2-sample.pmtiles", header, summary["metadata"])
    report.update({"requests": source.requests, "bytes_read": source.bytes, "per_zoom": per_zoom, "seed": seed})
    (out / "sample.json").write_text(json.dumps(rows))
    (out / "sample-report.json").write_text(json.dumps(report, indent=2))
    return report


def command_views(url: str, inventory: Path, dest: Path, boxes: list[tuple[float, ...]], full_zoom: int) -> dict:
    """Every tile at zooms 0 to full_zoom and every tile inside the boxes above it, for the browser checks."""
    table = np.load(inventory)
    summary = json.loads(inventory.with_suffix(".json").read_text())
    wanted = view_tiles(table, boxes, full_zoom, summary["header"]["max_zoom"])
    entries, source, header = extract(url, table, wanted)
    report = write_tiles(entries, dest, header, summary["metadata"])
    report.update({"requests": source.requests, "bytes_read": source.bytes, "boxes": boxes, "full_zoom": full_zoom})
    return report


def mosaics(get_bytes) -> Iterator[tuple[int, np.ndarray]]:
    """512 px tiles from 256 px tiles: tile (z, x, y) at 512 is the four tiles under it at z + 1.

    A missing child is nodata. Yields in 512 tile id order.
    """
    header = read_header(get_bytes)
    table, _stats = directory_entries(get_bytes, header)
    ids, offsets, lengths = _addressed(table)
    base = header["tile_data_offset"]
    parents = sorted({zxy_to_tileid(z - 1, x // 2, y // 2) for z, x, y in map(tileid_to_zxy, ids[ids >= 1].tolist())})
    decode = functools.lru_cache(maxsize=4096)(lambda offset, length: decode_v2(get_bytes(base + offset, length)))
    for parent in parents:
        z, x, y = tileid_to_zxy(parent)
        out = np.full((2 * SIZE, 2 * SIZE), NODATA, dtype=np.uint8)
        for dy in (0, 1):
            for dx in (0, 1):
                child = zxy_to_tileid(z + 1, 2 * x + dx, 2 * y + dy)
                i = np.searchsorted(ids, child)
                if i < len(ids) and ids[i] == child:
                    out[dy * SIZE : (dy + 1) * SIZE, dx * SIZE : (dx + 1) * SIZE] = decode(int(offsets[i]), int(lengths[i]))
        yield parent, out


def command_transcode(src: Path, dest: Path, encoding: str, version: str, tile_size: int = 256) -> dict:
    get_bytes = file_source(src)
    header = read_header(get_bytes)
    metadata = read_metadata(get_bytes, header)
    metadata.update({"description": DESCRIPTIONS[encoding], "encoding": f"class-value-{encoding}", "version": version})
    started = time.time()
    if tile_size == 512:
        encode = ENCODINGS[encoding]
        entries = ((tile_id, encode(values)) for tile_id, values in mosaics(get_bytes))
        header = {**header, "max_zoom": header["max_zoom"] - 1, "center_zoom": max(0, header["center_zoom"] - 1)}
        metadata["tile_size"] = 512
    else:
        convert = transcoder(encoding)
        entries = ((tile_id, convert(data)) for tile_id, data in tiles_in_order(get_bytes))
    result = write_tiles(entries, dest, header, metadata)
    result.update({"encoding": encoding, "tile_size": tile_size, "wall_seconds": round(time.time() - started, 2), "peak_memory": peak_memory()})
    return result


def compare(a: Path, b: Path, encoding: str, decoder: str = "gdal") -> dict:
    """Every tile id in a is in b and no more, and every tile decodes to the same class values and nodata."""
    read_b = gdal_values if decoder == "gdal" else decode_values
    get_a, get_b = file_source(a), file_source(b)
    tiles_a, tiles_b = tiles_in_order(get_a), tiles_in_order(get_b)
    checked = differ = 0
    missing, extra, mismatched = [], [], []
    cache: dict[bytes, np.ndarray] = {}
    pending_b = next(tiles_b, None)
    for tile_id, data_a in tiles_a:
        while pending_b is not None and pending_b[0] < tile_id:
            extra.append(pending_b[0])
            pending_b = next(tiles_b, None)
        if pending_b is None or pending_b[0] != tile_id:
            missing.append(tile_id)
            continue
        if data_a not in cache:
            if len(cache) > 4096:
                cache.clear()
            cache[data_a] = gdal_values(data_a, "gray-alpha")
        checked += 1
        if not np.array_equal(cache[data_a], read_b(pending_b[1], encoding)):
            differ += 1
            mismatched.append(tile_id)
        pending_b = next(tiles_b, None)
    while pending_b is not None:
        extra.append(pending_b[0])
        pending_b = next(tiles_b, None)
    header_a, header_b = read_header(get_a), read_header(get_b)
    same_header = {key: header_a[key] == header_b[key] for key in ("addressed_tiles_count", "min_zoom", "max_zoom", "tile_type", "tile_compression", "min_lon_e7", "max_lat_e7")}
    return {"checked": checked, "differ": differ, "missing": len(missing), "extra": len(extra), "first_mismatches": mismatched[:10], "header_fields_equal": same_header}


def command_bench(src: Path, encodings: list[str], rows_path: Path | None) -> dict:
    """Bytes per tile, time per 1,000 tiles and peak memory for each encoding over every tile in src."""
    get_bytes = file_source(src)
    tiles = list(tiles_in_order(get_bytes))
    arrays = [(tile_id, decode_v2(data)) for tile_id, data in tiles]
    by_id = {row["tile_id"]: row for row in json.loads(rows_path.read_text())} if rows_path else {}
    result = {"tiles": len(tiles), "v2_bytes": sum(len(data) for _id, data in tiles), "encodings": {}}
    for encoding in encodings:
        encode = ENCODINGS[encoding]
        started = time.perf_counter()
        encoded = [(tile_id, encode(values)) for tile_id, values in arrays]
        encode_s = time.perf_counter() - started
        started = time.perf_counter()
        for _tile_id, data in encoded:
            decode_rgba(data)
        decode_s = time.perf_counter() - started
        total = sum(len(data) for _id, data in encoded)
        groups: dict[str, list[int]] = {}
        for (tile_id, data), (_same, v2) in zip(encoded, tiles):
            zoom = tileid_to_zxy(tile_id)[0]
            reasons = by_id.get(tile_id, {}).get("reasons", [])
            keys = [f"z{zoom}", by_id.get(tile_id, {}).get("category", "?")] + [f"why:{r}" for r in reasons] + [f"z{zoom}:{r}" for r in reasons]
            for key in keys:
                groups.setdefault(key, [0, 0, 0])
                groups[key][0] += 1
                groups[key][1] += len(data)
                groups[key][2] += len(v2)
        result["encodings"][encoding] = {
            "bytes": total,
            "ratio_to_v2": round(total / result["v2_bytes"], 4),
            "encode_s_per_1000": round(encode_s / len(tiles) * 1000, 3),
            "python_decode_s_per_1000": round(decode_s / len(tiles) * 1000, 3),
            "groups": {key: {"tiles": n, "bytes": b, "v2_bytes": v} for key, (n, b, v) in sorted(groups.items())},
        }
    result["peak_memory"] = peak_memory()
    return result


def extrapolate(bench: dict, inventory: dict) -> dict:
    """Archive tile bytes per encoding: each zoom's distinct v2 bytes times the ratio in that zoom's distinct-body stratum."""
    out = {}
    for encoding, row in bench["encodings"].items():
        total = 0.0
        for zoom, counts in inventory["per_zoom"].items():
            group = row["groups"].get(f"z{zoom}:distinct") or row["groups"][f"z{zoom}"]
            total += counts["distinct_bytes"] * group["bytes"] / group["v2_bytes"]
        out[encoding] = int(total)
    return out


def command_share(src: Path, dest: Path, steps: int, curve: str, solid_zoom: int | None) -> dict:
    """Rewrite a cropland-share archive (gray) as a palette archive with `steps` + 1 shades.

    From solid_zoom up the tiles are two-shade (see share_palette_png solid), the zoom the map draws solid.
    Zooms below carry the shades. The paint reads the settings from the archive metadata.
    """
    get_bytes = file_source(src)
    header = read_header(get_bytes)
    metadata = read_metadata(get_bytes, header)
    metadata.update({
        "description": f"Palette. Red is the cropland share on a 0-255 scale, {steps + 1} shades ({curve}), from zoom {solid_zoom} two shades. A transparent entry is nodata",
        "encoding": "cropland-share-palette", "share_steps": steps, "share_curve": curve, "solid_zoom": solid_zoom, "version": "3-share-sample"})
    started = time.time()
    entries = ((tile_id, share_palette_png(data, steps, curve, solid=solid_zoom is not None and tileid_to_zxy(tile_id)[0] >= solid_zoom))
               for tile_id, data in tiles_in_order(get_bytes))
    result = write_tiles(entries, dest, header, metadata)
    result.update({"steps": steps, "curve": curve, "solid_zoom": solid_zoom, "wall_seconds": round(time.time() - started, 2), "peak_memory": peak_memory()})
    return result


def parse_box(text: str) -> tuple[float, float, float, float]:
    west, south, east, north = (float(part) for part in text.split(","))
    return west, south, east, north


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory")
    inventory.add_argument("--url", default=V2_URL)
    inventory.add_argument("--out", type=Path, required=True)
    sample = commands.add_parser("sample")
    sample.add_argument("--url", default=V2_URL)
    sample.add_argument("--inventory", type=Path, required=True)
    sample.add_argument("--out", type=Path, required=True)
    sample.add_argument("--per-zoom", type=int, default=100)
    sample.add_argument("--seed", type=int, default=9)
    views = commands.add_parser("views")
    views.add_argument("--url", default=V2_URL)
    views.add_argument("--inventory", type=Path, required=True)
    views.add_argument("--dest", type=Path, required=True)
    views.add_argument("--box", type=parse_box, action="append", required=True, help="west,south,east,north")
    views.add_argument("--full-zoom", type=int, default=6)
    transcode = commands.add_parser("transcode")
    transcode.add_argument("--src", type=Path, required=True)
    transcode.add_argument("--dest", type=Path, required=True)
    transcode.add_argument("--encoding", choices=sorted(ENCODINGS), required=True)
    transcode.add_argument("--version", default="3")
    transcode.add_argument("--tile-size", type=int, choices=(256, 512), default=256)
    verify = commands.add_parser("verify")
    verify.add_argument("--a", type=Path, required=True)
    verify.add_argument("--b", type=Path, required=True)
    verify.add_argument("--encoding", choices=sorted(ENCODINGS), required=True)
    verify.add_argument("--decoder", choices=("gdal", "own"), default="gdal")
    share = commands.add_parser("share")
    share.add_argument("--src", type=Path, required=True)
    share.add_argument("--dest", type=Path, required=True)
    share.add_argument("--steps", type=int, required=True, help="shades above zero, 1 to 254. Up to 2 fit 2 bits, up to 14 fit 4 bits")
    share.add_argument("--curve", choices=("linear", "sqrt"), default="sqrt")
    share.add_argument("--solid-zoom", type=int, default=None, help="two shades from this tile zoom up")
    bench = commands.add_parser("bench")
    bench.add_argument("--src", type=Path, required=True)
    bench.add_argument("--encoding", choices=sorted(ENCODINGS), action="append", required=True)
    bench.add_argument("--rows", type=Path)
    args = parser.parse_args()

    if args.command == "inventory":
        result = command_inventory(args.url, args.out)
        result.pop("metadata")
    elif args.command == "sample":
        result = command_sample(args.url, args.inventory, args.out, args.per_zoom, args.seed)
    elif args.command == "views":
        result = command_views(args.url, args.inventory, args.dest, args.box, args.full_zoom)
    elif args.command == "transcode":
        result = command_transcode(args.src, args.dest, args.encoding, args.version, args.tile_size)
    elif args.command == "verify":
        result = compare(args.a, args.b, args.encoding, args.decoder)
    elif args.command == "share":
        result = command_share(args.src, args.dest, args.steps, args.curve, args.solid_zoom)
    else:
        result = command_bench(args.src, args.encoding, args.rows)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
