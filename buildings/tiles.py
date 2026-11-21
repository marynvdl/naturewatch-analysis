#!/usr/bin/env python3
"""Tile sample for the Buildings 2023 layer: the share pyramid on small windows, built several ways (task 04).

  python buildings/tiles.py fetch [name ...]        cut each window from the published sprout VRT
  python buildings/tiles.py window <name> [flags]   one archive: data/buildings/tiles/<name>-<settings>.pmtiles
  python buildings/tiles.py sample [name ...]       the baseline and every variant, for every window
  python buildings/tiles.py speckle [name ...]      the recommended settings with each Google speckle rule
  python buildings/tiles.py compare                 the comparison tables, from the records and archives

A tile is a 512 px palette PNG, laid out as cropland version 3 (tiles/transcode.py share_palette_from_gray):
index 0 is nodata (tRNS alpha 0), index k + 1 is shade k of `steps`, red = 255 × (k / steps)². Shade k is
round(√(share / full) × steps), and any share above 0 keeps shade 1. `full` is the share drawn darkest.

The settings a sample compares, each a flag:

  --top-zoom        the archive's top zoom. A 512 px tile at zoom 11 is about 38 m a pixel at the equator.
  --top-rule        `graded`, as below the top zoom, or `solid<t>`: index 2 (red 255) where share >= t,
                    index 1 (red 0) below it, as cropland's solid top zoom with t = 0.5.
  --full            the share drawn with the darkest shade, 1 as cropland, 0.25 as roads.
  --parent          `mean`: a parent's share is the mean of its four children, nodata and absent children
                    counting as 0, so it is the built fraction of the parent pixel. `max`: the largest child.
  --empty           `keep`: every tile with a valid cell is stored, so nodata and share 0 stay apart.
                    `drop`: a tile with no building is not stored, as roads.
  --floor           a sprout cell whose share is below it is read as 0. 0 keeps every cell. Google's mean presence
                    leaves cells of 1 or 2 (of 250) on empty land, 3–6 m² in a 28 m cell, smaller than a hut.
  --google-floor    as --floor, on Google cells only (source 1). TEMPO cells already passed the sprout's seed rule.
  --google-seed     a Google cell is kept only if a Google cell within SEED_RADIUS cells (2 km) reaches this share.
                    It clears Google's speckle on empty bush. 0 turns it off. Both run on the window cut, before
                    the top zoom is warped, so `fetch` reads SEED_RADIUS more cells around each window.

The top zoom is warped from the sprout with `average`, the share with nodata as 0 and a valid mask beside it,
so the share is the built fraction of the whole pixel and the area is kept. A pixel is nodata when no cell
under it has a source. Reads are window sized, with the gcloud account's token. Nothing is written to the cloud.
"""
from __future__ import annotations

import argparse
import json
import math
import resource
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tiles"))

try:
    import compare  # noqa: E402  the windows and their areas; needs yaml and pyproj, which the Batch container lacks
except ImportError:
    compare = None  # a Batch shard only needs the layer maths below
import cropland  # noqa: E402  tiles/cropland.py: tile maths
import pyramid  # noqa: E402
import transcode  # noqa: E402
from osgeo import gdal  # noqa: E402

gdal.UseExceptions()

RECIPE = ROOT / "cookbook" / "recipes" / "buildings-2023-v1.yaml"
DATA = ROOT / "data" / "buildings" / "tiles"
SIZE = 512
STEPS = 7
MARGIN = 2  # sprout cells read around a top-zoom tile
SEED_RADIUS = 72  # sprout cells, about 2 km: the sprout's TEMPO seed rule radius
SOURCE_GOOGLE = 1
FETCH_ZOOM = 11  # a window reads the sprout under every tile at this zoom that meets it
SHARE_SCALE = 250
SHARE_NODATA = 255
NODATA_KEY = transcode.NODATA_KEY
SHADE_ZOOMS = (4, 6, 8, 10)
THRESHOLDS = (0.5, 0.25, 0.1, 0.05)

BASELINE = {"top_zoom": 11, "top_rule": "solid0.5", "full": 1.0, "parent": "mean", "empty": "keep", "floor": 0.0}
# The settings recommended before the pause, and the Google speckle rules compared on them (resumed 19 November 2026).
RECOMMENDED = {"top_zoom": 12, "top_rule": "graded", "full": 0.25, "parent": "mean", "empty": "drop", "floor": 0.0}
SPECKLE_VARIANTS = [
    {},
    {"floor": 0.01},
    {"google_floor": 0.008, "google_seed": 0.15},
    {"google_floor": 0.012},
    {"google_floor": 0.012, "google_seed": 0.15},
    {"google_floor": 0.016, "google_seed": 0.15},
]
VARIANTS = [
    {"top_zoom": 12},
    {"top_zoom": 13},
    {"top_rule": "graded"},
    {"top_rule": "solid0.25"},
    {"top_rule": "solid0.1"},
    {"top_rule": "solid0.05"},
    {"full": 0.5},
    {"full": 0.25},
    {"full": 0.1},
    {"parent": "max"},
    {"empty": "drop"},
    {"floor": 0.01},
    {"floor": 0.02},
]


class Tile(NamedTuple):
    share: np.ndarray  # built fraction of each pixel, 0..1, float32
    valid: np.ndarray  # fraction of each pixel with a source, 0..1, float32
    zoom: int


# ---- windows ------------------------------------------------------------------------------


def windows() -> dict[str, dict]:
    return {name: box for name, box in compare.load_boxes().items() if box["kind"] == "window"}


def window_bounds(box: dict) -> tuple[float, float, float, float]:
    half = box["size_deg"] / 2
    return box["lon"] - half, box["lat"] - half, box["lon"] + half, box["lat"] + half


def fetch_grid(box: dict) -> compare.Grid:
    """Sprout cells under every FETCH_ZOOM tile that meets the window, with MARGIN cells more on each side."""
    tiles = cropland.tiles_covering(window_bounds(box), FETCH_ZOOM)
    xs, ys = [x for x, _ in tiles], [y for _, y in tiles]
    west, _, _, north = tile_bounds_deg(FETCH_ZOOM, min(xs), min(ys))
    _, south, east, _ = tile_bounds_deg(FETCH_ZOOM, max(xs), max(ys))
    margin = MARGIN + SEED_RADIUS
    col0 = math.floor((west + 180) / compare.CELL) - margin
    col1 = math.ceil((east + 180) / compare.CELL) + margin
    row0 = math.floor((90 - north) / compare.CELL) - margin
    row1 = math.ceil((90 - south) / compare.CELL) + margin
    return compare.Grid(col0, row0, col1 - col0, row1 - row0)


def tile_extent(box: dict) -> tuple[float, float, float, float]:
    """West, south, east, north of the FETCH_ZOOM tiles that meet the window, in degrees."""
    tiles = cropland.tiles_covering(window_bounds(box), FETCH_ZOOM)
    xs, ys = [x for x, _ in tiles], [y for _, y in tiles]
    west, _, _, north = tile_bounds_deg(FETCH_ZOOM, min(xs), min(ys))
    _, south, east, _ = tile_bounds_deg(FETCH_ZOOM, max(xs), max(ys))
    return west, south, east, north


def sprout_path(name: str) -> Path:
    return DATA / "sprout" / f"{name}.tif"


def fetch(recipe: dict, name: str) -> dict:
    """Cut one window from the published VRT to a local GeoTIFF. Returns bytes read and the cut's size."""
    started = time.perf_counter()
    token = subprocess.run(["gcloud", "auth", "print-access-token"], text=True, capture_output=True, check=True).stdout.strip()
    for key, value in {
        "GDAL_HTTP_HEADERS": f"Authorization: Bearer {token}",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.vrt",
        "CPL_VSIL_NETWORK_STATS_ENABLED": "YES",
    }.items():
        gdal.SetConfigOption(key, value)
    gdal.NetworkStatsReset()
    sprout = recipe["sprout"]
    vrt = "/vsicurl/https://storage.googleapis.com/" + (sprout["path"].rstrip("/") + "/" + sprout["vrt"]).removeprefix("gs://")
    grid = fetch_grid(windows()[name])
    dest = sprout_path(name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    gdal.Translate(
        str(dest), vrt, srcWin=[grid.col0, grid.row0, grid.cols, grid.rows],
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512"],
    )
    stats = json.loads(gdal.NetworkStatsGetAsSerializedJSON() or "{}")
    gdal.SetConfigOption("GDAL_HTTP_HEADERS", None)
    downloaded = stats.get("methods", {}).get("GET", {}).get("downloaded_bytes", 0)
    return {"window": name, "cells": [grid.cols, grid.rows], "bytes_read": downloaded, "bytes_local": dest.stat().st_size,
            "wall_seconds": round(time.perf_counter() - started, 1)}


# ---- tile maths -----------------------------------------------------------------------------


def tile_bounds_deg(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2**z

    def lat(index: float) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * index / n))))

    return x / n * 360.0 - 180.0, lat(y + 1), (x + 1) / n * 360.0 - 180.0, lat(y)


def pixel_lats(z: int, y: int) -> np.ndarray:
    """Latitude of the SIZE + 1 row edges of tile row y, north first."""
    n = 2**z * SIZE
    rows = y * SIZE + np.arange(SIZE + 1)
    return np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * rows / n))))


@lru_cache(maxsize=4096)
def pixel_area_km2(z: int, y: int) -> np.ndarray:
    """Area of one pixel per row of tile row y, km², on the WGS84 spheroid. Shape (SIZE, 1)."""
    lats = pixel_lats(z, y)
    width = 360.0 / (2**z * SIZE)
    out = np.empty((SIZE, 1))
    for r in range(SIZE):
        n, s = lats[r], lats[r + 1]
        area, _ = compare.GEOD.polygon_area_perimeter([0, width, width, 0], [s, s, n, n])
        out[r, 0] = abs(area) / 1e6
    return out


def overlap(edges: np.ndarray, low: float, high: float) -> np.ndarray:
    """Fraction of each interval between consecutive edges (either order) that lies in [low, high]."""
    a, b = np.minimum(edges[:-1], edges[1:]), np.maximum(edges[:-1], edges[1:])
    return np.clip((np.minimum(b, high) - np.maximum(a, low)) / (b - a), 0, 1)


def km2_in(tile: Tile, x: int, y: int, bounds) -> float:
    """Built km² of a top-zoom tile inside bounds (west, south, east, north), by fractional pixel overlap."""
    west, south, east, north = bounds
    z = tile.zoom
    lon_edges = (x * SIZE + np.arange(SIZE + 1)) / (2**z * SIZE) * 360.0 - 180.0
    wx = overlap(lon_edges, west, east)
    wy = overlap(pixel_lats(z, y), south, north)
    return float((tile.share * pixel_area_km2(z, y) * wy[:, None] * wx[None, :]).sum())


# ---- the layer ------------------------------------------------------------------------------


def speckle_rule(share: np.ndarray, source: np.ndarray, floor: float = 0.0, seed: float = 0.0,
                 radius: int = SEED_RADIUS) -> np.ndarray:
    """Sprout share values (uint8, nodata 255) with Google's speckle set to 0. On Google cells only: a value
    below floor × 250 is 0, and with a seed, a cell is 0 unless a Google cell within radius cells reaches it.
    Cells outside the arrays are not seeds."""
    google = (source == SOURCE_GOOGLE) & (share != SHARE_NODATA)
    out = share.copy()
    if floor > 0:
        out[google & (share < floor * SHARE_SCALE)] = 0
    if seed > 0:
        near = dilate(google & (share >= seed * SHARE_SCALE), radius)
        out[google & ~near] = 0
    return out


def dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """True where a True cell lies within `radius` cells either side, in a square window, by running sums.
    Cells outside the array count as False. The same as sprout/buildings.py dilate, without scipy."""
    out = mask.astype(np.int32)
    for axis in (0, 1):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (radius + 1, radius)
        c = np.cumsum(np.pad(out, pad), axis=axis, dtype=np.int32)
        n = out.shape[axis]
        hi = np.take(c, np.arange(2 * radius + 1, 2 * radius + 1 + n), axis=axis)
        lo = np.take(c, np.arange(0, n), axis=axis)
        out = (hi - lo > 0).astype(np.int32)
    return out.astype(bool)


def cleaned_path(name: str, floor: float, seed: float) -> Path:
    """The window cut with speckle_rule applied, written once beside the cut."""
    path = sprout_path(name)
    if not floor and not seed:
        return path
    dest = path.with_name(f"{name}-gfloor{floor:g}-gseed{seed:g}.tif")
    if not dest.exists():
        ds = gdal.Open(str(path))
        share = ds.GetRasterBand(1).ReadAsArray()
        source = ds.GetRasterBand(2).ReadAsArray()
        out = gdal.GetDriverByName("GTiff").CreateCopy(str(dest), ds, options=["COMPRESS=DEFLATE", "TILED=YES"])
        out.GetRasterBand(1).WriteArray(speckle_rule(share, source, floor, seed))
        out = None
    return dest


def layer(path: Path | str, floor: float = 0.0, google_floor: float = 0.0, google_seed: float = 0.0,
          radius: int = SEED_RADIUS):
    """render(z, x, y) for the top zoom from a window cut, a block COG or a VRT with the two sprout bands.

    With a Google rule, each tile reads `radius` more cells on every side and applies speckle_rule to what
    it read, so the result does not depend on where the source is cut. The window flow instead applies the
    rule once to its cut (cleaned_path) and renders without it.
    """
    source = gdal.Open(str(path))
    west0, cell, _, north0, _, neg = source.GetGeoTransform()
    width, height = source.RasterXSize, source.RasterYSize
    srs = source.GetProjection()
    band = source.GetRasterBand(1)
    source_band = source.GetRasterBand(2)
    ruled = google_floor > 0 or google_seed > 0
    pad = radius if ruled else 0
    # Windows and the warp origin are worked out on the global cell grid (180° W, 90° N), not from this raster's
    # corner, so a shard's block mosaic and the global VRT round alike and give the same bytes.
    grid_col, grid_row = round((west0 + 180) / cell), round((90 - north0) / -neg)

    def render(z: int, x: int, y: int) -> Tile | None:
        west, south, east, north = tile_bounds_deg(z, x, y)
        col0 = max(0, math.floor((west + 180) / cell) - MARGIN - grid_col)
        col1 = min(width, math.ceil((east + 180) / cell) + MARGIN - grid_col)
        row0 = max(0, math.floor((90 - north) / -neg) - MARGIN - grid_row)
        row1 = min(height, math.ceil((90 - south) / -neg) + MARGIN - grid_row)
        if col1 <= col0 or row1 <= row0:
            return None
        if ruled:
            c0, c1 = max(0, col0 - pad), min(width, col1 + pad)
            r0, r1 = max(0, row0 - pad), min(height, row1 + pad)
            wide = band.ReadAsArray(c0, r0, c1 - c0, r1 - r0)
            if (wide == SHARE_NODATA).all():
                return None
            wide = speckle_rule(wide, source_band.ReadAsArray(c0, r0, c1 - c0, r1 - r0), google_floor, google_seed, radius)
            raw = wide[row0 - r0 : row1 - r0, col0 - c0 : col1 - c0]
        else:
            raw = band.ReadAsArray(col0, row0, col1 - col0, row1 - row0)
        valid = raw != SHARE_NODATA
        if floor > 0:
            raw = np.where(valid & (raw < floor * SHARE_SCALE), 0, raw)
        if not valid.any():
            return None
        mem = gdal.GetDriverByName("MEM").Create("", col1 - col0, row1 - row0, 2, gdal.GDT_Float32)
        mem.SetGeoTransform((-180 + (grid_col + col0) * cell, cell, 0, 90 + (grid_row + row0) * neg, 0, neg))
        mem.SetProjection(srs)
        mem.GetRasterBand(1).WriteArray(np.where(valid, raw / SHARE_SCALE, 0).astype(np.float32))
        mem.GetRasterBand(2).WriteArray(valid.astype(np.float32))
        warped = gdal.Warp(
            "", mem, format="MEM", outputBounds=cropland.tile_bounds_3857(z, x, y), width=SIZE, height=SIZE,
            dstSRS="EPSG:3857", resampleAlg="average", outputType=gdal.GDT_Float32, warpOptions=["INIT_DEST=0"],
        )
        share = warped.GetRasterBand(1).ReadAsArray().astype(np.float32)
        valid_px = warped.GetRasterBand(2).ReadAsArray().astype(np.float32)
        if not (valid_px > 0).any():
            return None
        return Tile(np.clip(share, 0, 1), valid_px, z)

    render.source = source  # keep the dataset open while render lives
    return render


def reduce(children: dict, parent: str) -> Tile | None:
    share = np.zeros((SIZE * 2, SIZE * 2), dtype=np.float32)
    valid = np.zeros((SIZE * 2, SIZE * 2), dtype=np.float32)
    zoom = None
    for (dx, dy), child in children.items():
        share[dy * SIZE : (dy + 1) * SIZE, dx * SIZE : (dx + 1) * SIZE] = child.share
        valid[dy * SIZE : (dy + 1) * SIZE, dx * SIZE : (dx + 1) * SIZE] = child.valid
        zoom = child.zoom - 1
    blocks = share.reshape(SIZE, 2, SIZE, 2)
    out = blocks.max(axis=(1, 3)) if parent == "max" else blocks.mean(axis=(1, 3))
    valid_out = valid.reshape(SIZE, 2, SIZE, 2).mean(axis=(1, 3))
    if not (valid_out > 0).any():
        return None
    return Tile(out.astype(np.float32), valid_out.astype(np.float32), zoom)


def shade_index(share: np.ndarray, steps: int = STEPS, full: float = 1.0) -> np.ndarray:
    """Shade 0..steps, evenly spaced in √(share / full). Any share above 0 is at least shade 1."""
    k = np.rint(np.sqrt(np.clip(share / full, 0, 1)) * steps).astype(np.int32)
    return np.where(share > 0, np.maximum(k, 1), 0)


def palette_png(index: np.ndarray, reds: list[int]) -> bytes:
    """Index 0 is the nodata key with alpha 0. Only the entries the tile uses are written."""
    used = int(index.max()) + 1
    palette = np.zeros((used, 3), dtype=np.uint8)
    palette[0] = NODATA_KEY
    palette[1:used, 0] = reds[1:used]
    depth = 2 if len(reds) <= 4 else 4 if len(reds) <= 16 else 8
    return transcode._png(transcode._pack(index.astype(np.uint8), depth), depth, 3, [(b"PLTE", palette.tobytes()), (b"tRNS", b"\x00")])


def share_reds(steps: int = STEPS) -> list[int]:
    return [0] + [int(round((k / steps) ** 2 * 255)) for k in range(steps + 1)]


SOLID_REDS = [0, 0, 255]


def encode(tile: Tile, top_zoom: int, top_rule: str, full: float, steps: int = STEPS) -> bytes:
    nodata = tile.valid <= 0
    if tile.zoom == top_zoom and top_rule.startswith("solid"):
        threshold = float(top_rule.removeprefix("solid"))
        index = np.where(nodata, 0, 1 + (tile.share >= threshold)).astype(np.uint8)
        return palette_png(index, SOLID_REDS)
    index = np.where(nodata, 0, shade_index(tile.share, steps, full) + 1).astype(np.uint8)
    return palette_png(index, share_reds(steps))


# ---- one archive ----------------------------------------------------------------------------


def settings_name(settings: dict) -> str:
    name = f"z{settings['top_zoom']}-{settings['top_rule']}-full{settings['full']:g}-{settings['parent']}-{settings['empty']}"
    name += f"-floor{settings['floor']:g}" if settings.get("floor") else ""
    name += f"-gfloor{settings['google_floor']:g}" if settings.get("google_floor") else ""
    return name + (f"-gseed{settings['google_seed']:g}" if settings.get("google_seed") else "")


def peak_memory_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def build(name: str, settings: dict, dest: Path | None = None) -> dict:
    """Build one window archive under these settings and write its record beside it."""
    started = time.perf_counter()
    box = windows()[name]
    dest = dest or DATA / f"{name}-{settings_name(settings)}.pmtiles"
    top = settings["top_zoom"]
    sprout = cleaned_path(name, settings.get("google_floor", 0.0), settings.get("google_seed", 0.0))
    render_tile = layer(sprout, settings.get("floor", 0.0))
    boxes = compare.load_boxes()
    inner = boxes.get(box.get("box", ""))
    inner_bounds = compare.box_grid(inner["lat"], inner["lon"], inner["size_km"]).bounds() if inner else None
    extent = tile_extent(box)
    top_stats = {"built_km2": 0.0, "box_km2": 0.0, "pixels_built": 0,
                 "solid": {f"{t:g}": {"built_km2_kept": 0.0, "painted_km2": 0.0} for t in THRESHOLDS}}

    def render(z: int, x: int, y: int) -> Tile | None:
        tile = render_tile(z, x, y)
        if tile is not None:
            area = pixel_area_km2(z, y)
            top_stats["built_km2"] += float((tile.share * area).sum())
            top_stats["pixels_built"] += int((tile.share > 0).sum())
            for t in THRESHOLDS:
                drawn = tile.share >= t
                top_stats["solid"][f"{t:g}"]["built_km2_kept"] += float((tile.share * area * drawn).sum())
                top_stats["solid"][f"{t:g}"]["painted_km2"] += float((area * drawn).sum())
            if inner_bounds:
                top_stats["box_km2"] += km2_in(tile, x, y, inner_bounds)
        return tile

    drop = settings["empty"] == "drop"

    def enc(tile: Tile) -> bytes:
        if drop and not (tile.share > 0).any():
            return b""
        return encode(tile, top, settings["top_rule"], settings["full"])

    def red(children: dict) -> Tile | None:
        return reduce(children, settings["parent"])

    entries: list[pyramid.Entry] = []
    roots = {}
    for x, y in cropland.tiles_covering(window_bounds(box), FETCH_ZOOM):
        value = pyramid.build_subtree((FETCH_ZOOM, x, y), top, render, red, enc, entries)
        if value is not None:
            roots[(x, y)] = value
    entries += pyramid.upper_levels(roots, FETCH_ZOOM, red, enc)
    entries = sorted((e for e in entries if e[1]), key=lambda e: e[0])
    metadata = {"name": "buildings", "format": "png", "type": "overlay", "settings": settings, "steps": STEPS}
    if entries:
        written = pyramid.write_archive(entries, dest, pyramid.archive_header(window_bounds(box), 0, top), metadata)
    else:  # `drop` on a window with no building: nothing to store
        dest.unlink(missing_ok=True)
        written = {"tiles": 0, "bytes": 0, "distinct_tiles": 0}
    by_zoom: dict[int, dict] = {}
    for tile_id, data in entries:
        z = pyramid.tileid_to_zxy(tile_id)[0]
        stat = by_zoom.setdefault(z, {"tiles": 0, "bytes": 0})
        stat["tiles"] += 1
        stat["bytes"] += len(data)
    record = {
        "window": name, "settings": settings, "archive": dest.name, **written,
        "zooms": {str(z): by_zoom[z] for z in sorted(by_zoom)},
        "top": top_stats,
        "sprout_km2": sprout_km2(name, extent),
        "rule_km2": sprout_km2(name, extent, sprout),
        "window_cells": cells_built(name),
        "rule_cells": cells_built(name, sprout),
        "sprout_box_km2": sprout_box_km2(name, inner) if inner else None,
        "wall_seconds": round(time.perf_counter() - started, 1),
        "peak_memory_bytes": peak_memory_bytes(),
    }
    dest.with_suffix(".json").write_text(json.dumps(record, indent=1))
    return record


def read_cut(name: str, path: Path | None = None) -> tuple[np.ndarray, compare.Grid]:
    ds = gdal.Open(str(path or sprout_path(name)))
    west, cell, _, north, _, _ = ds.GetGeoTransform()
    grid = compare.Grid(round((west + 180) / cell), round((90 - north) / cell), ds.RasterXSize, ds.RasterYSize)
    return ds.GetRasterBand(1).ReadAsArray(), grid


def sprout_km2(name: str, bounds, path: Path | None = None) -> float:
    """Built km² of the sprout inside bounds, edge cells weighted by their overlap."""
    raw, grid = read_cut(name, path)
    share = np.where(raw == SHARE_NODATA, 0, raw / SHARE_SCALE)
    west, south, east, north = bounds
    wx = overlap(grid.west + np.arange(grid.cols + 1) * compare.CELL, west, east)
    wy = overlap(grid.north - np.arange(grid.rows + 1) * compare.CELL, south, north)
    area = compare.cell_area_km2(grid)
    return float((share * area * wy[:, None] * wx[None, :]).sum())


def cells_built(name: str, path: Path | None = None) -> int:
    """Sprout cells above 0 inside the window itself."""
    raw, grid = read_cut(name, path)
    west, south, east, north = window_bounds(windows()[name])
    c0, c1 = round((west - grid.west) / compare.CELL), round((east - grid.west) / compare.CELL)
    r0, r1 = round((grid.north - north) / compare.CELL), round((grid.north - south) / compare.CELL)
    part = raw[r0:r1, c0:c1]
    return int(((part > 0) & (part != SHARE_NODATA)).sum())


def sprout_box_km2(name: str, box: dict) -> float:
    """Built km² of a comparison box, read from the window cut, as `sprout/buildings.py check` reads it."""
    raw, grid = read_cut(name)
    inner = compare.box_grid(box["lat"], box["lon"], box["size_km"])
    r, c = inner.row0 - grid.row0, inner.col0 - grid.col0
    part = raw[r : r + inner.rows, c : c + inner.cols]
    return float((np.where(part == SHARE_NODATA, 0, part / SHARE_SCALE) * compare.cell_area_km2(inner)).sum())


# ---- compare --------------------------------------------------------------------------------


def shade_counts(path: Path, zoom: int, steps: int = STEPS) -> np.ndarray:
    """Pixels per shade 1..steps at one zoom of an archive below its top zoom."""
    counts = np.zeros(steps + 1, dtype=np.int64)
    if not path.exists():
        return counts[1:]
    for (z, _x, _y), data in pyramid.read_archive(path):
        if z != zoom:
            continue
        rgba = transcode.decode_rgba(data)
        red = np.where(rgba[..., 3] == 0, 0, rgba[..., 0]).astype(np.float64)
        k = np.rint(np.sqrt(red / 255) * steps).astype(np.int64)
        k = np.where(red > 0, np.maximum(k, 1), 0)
        counts += np.bincount(k.ravel(), minlength=steps + 1)
    return counts[1:]


def records() -> dict[tuple[str, str], dict]:
    out = {}
    for path in sorted(DATA.glob("*.json")):
        record = json.loads(path.read_text())
        out[(record["window"], settings_name(record["settings"]))] = record
    return out


def compare_tables() -> str:
    recs = records()
    names = list(windows())
    base = settings_name(BASELINE)
    lines = []

    lines += ["### Archives", "", "| Window | Settings | Tiles | Bytes | Wall s | Peak memory MB |", "| --- | --- | ---: | ---: | ---: | ---: |"]
    for name in names:
        for change in [{}] + VARIANTS:
            key = (name, settings_name({**BASELINE, **change}))
            if key in recs:
                r = recs[key]
                label = "baseline" if not change else ", ".join(f"{k} {v}" for k, v in change.items())
                lines.append(f"| {name} | {label} | {r['tiles']:,} | {r['bytes']:,} | {r['wall_seconds']} | {r['peak_memory_bytes'] / 1e6:.0f} |")

    lines += ["", "### Built area at the top zoom against the sprout", "",
              "| Window | Sprout km² under the tiles | Tiles km² at z11, z12, z13 | Box | Sprout box km² | Tiles box km² at z11 |",
              "| --- | ---: | --- | --- | ---: | ---: |"]
    for name in names:
        r = recs.get((name, base))
        if not r:
            continue
        zs = [recs.get((name, settings_name({**BASELINE, "top_zoom": z}))) for z in (11, 12, 13)]
        km = ", ".join(f"{x['top']['built_km2']:.4f}" if x else "–" for x in zs)
        box = windows()[name].get("box", "")
        sb = f"{r['sprout_box_km2']:.4f}" if r["sprout_box_km2"] is not None else ""
        tb = f"{r['top']['box_km2']:.4f}" if box else ""
        lines.append(f"| {name} | {r['sprout_km2']:.4f} | {km} | {box} | {sb} | {tb} |")

    lines += ["", "### Solid top zoom: built area kept, and area painted, by threshold (top zoom 11)", "",
              "| Window | Built km² | " + " | ".join(f"≥ {t:g}: kept, painted" for t in THRESHOLDS) + " |",
              "| --- | ---: |" + " --- |" * len(THRESHOLDS)]
    for name in names:
        r = recs.get((name, base))
        if not r or r["top"]["built_km2"] <= 0:
            continue
        built = r["top"]["built_km2"]
        cells = [f"{s['built_km2_kept'] / built:.0%}, {s['painted_km2']:.3f} km²" for s in (r["top"]["solid"][f"{t:g}"] for t in THRESHOLDS)]
        lines.append(f"| {name} | {built:.4f} | " + " | ".join(cells) + " |")

    lines += ["", "### Built pixels per shade 1–7", "", "| Window | Setting | Zoom | Shades 1–7 | Share in shades 1–2 |", "| --- | --- | ---: | --- | ---: |"]
    for name in names:
        for change in ({}, {"full": 0.5}, {"full": 0.25}, {"full": 0.1}, {"parent": "max"}, {"floor": 0.01}, {"floor": 0.02}):
            r = recs.get((name, settings_name({**BASELINE, **change})))
            if not r:
                continue
            label = "baseline" if not change else ", ".join(f"{k} {v}" for k, v in change.items())
            for zoom in (4, 8, 10):
                counts = shade_counts(DATA / r["archive"], zoom)
                total = counts.sum()
                faint = f"{counts[:2].sum() / total:.0%}" if total else "–"
                lines.append(f"| {name} | {label} | {zoom} | {'; '.join(f'{c:,}' for c in counts)} | {faint} |")
    return "\n".join(lines)


# ---- main -----------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("fetch", "window", "sample", "speckle", "compare"))
    parser.add_argument("names", nargs="*")
    parser.add_argument("--top-zoom", type=int, default=BASELINE["top_zoom"])
    parser.add_argument("--top-rule", default=BASELINE["top_rule"])
    parser.add_argument("--full", type=float, default=BASELINE["full"])
    parser.add_argument("--parent", choices=("mean", "max"), default=BASELINE["parent"])
    parser.add_argument("--empty", choices=("keep", "drop"), default=BASELINE["empty"])
    parser.add_argument("--floor", type=float, default=BASELINE["floor"])
    parser.add_argument("--google-floor", type=float, default=0.0)
    parser.add_argument("--google-seed", type=float, default=0.0)
    args = parser.parse_args()
    import yaml

    recipe = yaml.safe_load(RECIPE.read_text())
    names = args.names or list(windows())
    if args.command == "fetch":
        for name in names:
            print(json.dumps(fetch(recipe, name)), flush=True)
    elif args.command == "window":
        settings = {"top_zoom": args.top_zoom, "top_rule": args.top_rule, "full": args.full, "parent": args.parent, "empty": args.empty,
                    "floor": args.floor, "google_floor": args.google_floor, "google_seed": args.google_seed}
        for name in names:
            record = build(name, settings)
            print(json.dumps({k: record[k] for k in ("archive", "tiles", "bytes", "wall_seconds")}), flush=True)
    elif args.command == "sample":
        for name in names:
            for change in [{}] + VARIANTS:
                record = build(name, {**BASELINE, **change})
                print(json.dumps({k: record[k] for k in ("archive", "tiles", "bytes", "wall_seconds")}), flush=True)
    elif args.command == "speckle":
        for name in names:
            for change in SPECKLE_VARIANTS:
                record = build(name, {**RECOMMENDED, **change})
                print(json.dumps({k: record[k] for k in ("archive", "tiles", "bytes", "wall_seconds")}), flush=True)
    else:
        print(compare_tables())


if __name__ == "__main__":
    main()
