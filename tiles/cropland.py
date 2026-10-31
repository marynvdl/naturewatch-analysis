#!/usr/bin/env python3
"""Build class-value map tiles from the cropland sprout.

The recipe is the catalog. This script reads it and does not edit it.
Pixels stay 0 (not cropland) or 1 (cropland). Nodata stays transparent.
Colour is applied in the client. The published run is a Batch job in
europe-west1. The laptop only submits that job.

  python tiles/cropland.py submit
  python tiles/cropland.py build --recipe cookbook/recipes/cropland-2024-v2.yaml \
      --source <vrt or tif> --dest cropland.pmtiles --resampling max|q3|nearest \
      [--window WEST SOUTH EAST NORTH] [--stats stats.json]
  python tiles/cropland.py preview --dest <folder with .pmtiles files>

A recipe with tiles.scheme "xyz" writes a folder of PNG files (version 1).
A recipe with tiles.scheme "pmtiles" writes one raster PMTiles archive. The
top zoom is rendered from the source. Every lower zoom is built from its
four child tiles.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import math
import os
import resource
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

import numpy as np
from osgeo import gdal

gdal.UseExceptions()

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECIPE = ROOT / "cookbook" / "recipes" / "cropland-2024.yaml"
PROJECT = "nature-watch-387210"
REGION = "europe-west1"
IMAGE = "ghcr.io/osgeo/gdal:ubuntu-small-3.11.4"
MACHINE = "e2-standard-2"
SERVICE_ACCOUNT = "637146989512-compute@developer.gserviceaccount.com"
SCRATCH = "gs://nature-watch-bucket/scratch/tiles/cropland-2024/"
BATCH_ENV = "NATUREWATCH_TILES_BATCH"
TILE_SIZE = 256
EARTH_RADIUS = 6378137.0


def load_recipe(path: Path) -> dict:
    text = path.read_text()
    if path.suffix == ".json":
        return json.loads(text)
    import yaml

    return yaml.safe_load(text)


def published_values(recipe: dict) -> dict[str, int]:
    return {name: int(value) for name, value in recipe["sprout"]["values"].items()}


RESAMPLINGS = ("max", "q3", "nearest")


def tile_contract(recipe: dict, resampling: str | None = None) -> dict:
    """The recipe's tile block. A test can lower max_zoom; the file says 12.

    A pmtiles recipe may leave resampling unset until the sample is approved.
    Then the caller names one of RESAMPLINGS for a local sample.
    """
    tiles = recipe.get("tiles")
    if not isinstance(tiles, dict):
        raise ValueError("recipe has no tiles block")
    values = published_values(recipe)
    scheme = tiles.get("scheme")
    problems = []
    if scheme not in ("xyz", "pmtiles"):
        problems.append("tiles.scheme must be xyz or pmtiles")
    if int(tiles.get("tile_size", TILE_SIZE)) != TILE_SIZE:
        problems.append(f"tiles.tile_size must be {TILE_SIZE}")
    chosen = tiles.get("resampling")
    if scheme == "xyz":
        if chosen != "max":
            problems.append("tiles.resampling must be max")
        if int(tiles.get("tile_size", 0)) != TILE_SIZE:
            problems.append(f"tiles.tile_size must be {TILE_SIZE}")
    else:
        if chosen is not None and chosen not in RESAMPLINGS:
            problems.append(f"tiles.resampling must be one of {', '.join(RESAMPLINGS)} or unset")
        if resampling is not None:
            if resampling not in RESAMPLINGS:
                problems.append(f"resampling {resampling} must be one of {', '.join(RESAMPLINGS)}")
            elif chosen is not None and chosen != resampling:
                problems.append(f"recipe resampling is {chosen}, not {resampling}")
            chosen = resampling
        if chosen is None:
            problems.append("tiles.resampling is unset. Name one with --resampling for a local sample")
    if tiles.get("encoding") != "class-value":
        problems.append("tiles.encoding must be class-value")
    if int(tiles.get("min_zoom", -1)) != 0:
        problems.append("tiles.min_zoom must be 0")
    max_zoom = int(tiles.get("max_zoom", -1))
    if max_zoom < 0:
        problems.append("tiles.max_zoom must be at least 0")
    path = str(tiles.get("path", ""))
    if not path.startswith("gs://nature-watch-tiles/"):
        problems.append("tiles.path must be on nature-watch-tiles")
    if values.get("cropland") != 1 or values.get("not_cropland") != 0 or values.get("nodata") != 255:
        problems.append("sprout classes must be cropland 1, not_cropland 0, nodata 255")
    if problems:
        raise ValueError("tile recipe failed the check:\n" + "\n".join(problems))
    return {
        "scheme": scheme,
        "path": path if scheme == "pmtiles" or path.endswith("/") else path + "/",
        "min_zoom": 0,
        "max_zoom": max_zoom,
        "nodata": 255,
        "classes": (0, 1),
        "resampling": chosen,
    }


def mercator_x(lon: float) -> float:
    return EARTH_RADIUS * math.radians(lon)


def mercator_y(lat: float) -> float:
    lat_r = math.radians(lat)
    return EARTH_RADIUS * math.log(math.tan(math.pi / 4 + lat_r / 2))


def tile_index(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """XYZ tile containing a longitude and latitude. y grows southward."""
    n = 2**zoom
    x = math.floor((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    merc = math.log(math.tan(lat_r) + 1 / math.cos(lat_r))
    y = math.floor((1 - merc / math.pi) / 2 * n)
    return x, y


def tiles_covering(bounds: tuple[float, float, float, float], zoom: int) -> list[tuple[int, int]]:
    west, south, east, north = bounds
    n = 2**zoom
    x0, y_north = tile_index(west, north, zoom)
    x1, y_south = tile_index(east - 1e-12, south + 1e-12, zoom)
    x0 = max(0, min(n - 1, x0))
    x1 = max(0, min(n - 1, x1))
    y0 = max(0, min(n - 1, y_north))
    y1 = max(0, min(n - 1, y_south))
    if x1 < x0 or y1 < y0:
        return []
    return [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


def tile_bounds_3857(zoom: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2**zoom

    def lon(index: int) -> float:
        return index / n * 360.0 - 180.0

    def lat(index: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * index / n))))

    return (mercator_x(lon(x)), mercator_y(lat(y + 1)), mercator_x(lon(x + 1)), mercator_y(lat(y)))


def raster_bounds(path: Path) -> tuple[float, float, float, float]:
    dataset = gdal.Open(str(path))
    transform = dataset.GetGeoTransform()
    width = dataset.RasterXSize
    height = dataset.RasterYSize
    dataset = None
    west = transform[0]
    north = transform[3]
    east = west + transform[1] * width
    south = north + transform[5] * height
    return west, south, east, north


def write_class_png(path: Path, gray: np.ndarray, alpha: np.ndarray) -> None:
    path.write_bytes(encode_class_png(gray, alpha))


def encode_class_png(gray: np.ndarray, alpha: np.ndarray) -> bytes:
    """8-bit gray plus alpha. The gray value is the class. Alpha 0 is nodata."""
    height, width = gray.shape
    paired = np.stack([gray, alpha], axis=-1)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + paired[row].tobytes() for row in range(height))
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 4, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    return png


def render_tile(source: Path, zoom: int, x: int, y: int, nodata: int, classes: tuple[int, ...]) -> np.ndarray | None:
    """One XYZ tile, or None when every pixel is nodata.

    Maximum resampling ignores nodata, so one cropland pixel keeps the cell.
    """
    dataset = gdal.Warp(
        "",
        str(source),
        format="MEM",
        outputBounds=tile_bounds_3857(zoom, x, y),
        width=TILE_SIZE,
        height=TILE_SIZE,
        dstSRS="EPSG:3857",
        resampleAlg="max",
        srcNodata=nodata,
        dstNodata=nodata,
    )
    band = dataset.GetRasterBand(1)
    if band.XSize != TILE_SIZE or band.YSize != TILE_SIZE:
        raise ValueError(f"tile {zoom}/{x}/{y} is {band.XSize}x{band.YSize}")
    values = band.ReadAsArray()
    dataset = None
    allowed = set(classes) | {nodata}
    stray = sorted({int(value) for value in np.unique(values)} - allowed)
    if stray:
        raise ValueError(f"tile {zoom}/{x}/{y} has values {stray}")
    if not np.any(np.isin(values, classes)):
        return None
    return values


def build_tiles(source: Path, dest: Path, recipe: dict) -> dict:
    contract = tile_contract(recipe)
    bounds = raster_bounds(source)
    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped = 0
    total_bytes = 0
    cropland_pixels = 0
    for zoom in range(contract["min_zoom"], contract["max_zoom"] + 1):
        zoom_written = 0
        for x, y in tiles_covering(bounds, zoom):
            values = render_tile(source, zoom, x, y, contract["nodata"], contract["classes"])
            if values is None:
                skipped += 1
                continue
            gray = np.zeros(values.shape, dtype=np.uint8)
            alpha = np.zeros(values.shape, dtype=np.uint8)
            data = np.isin(values, contract["classes"])
            gray[data] = values[data]
            alpha[data] = 255
            path = dest / str(zoom) / str(x) / f"{y}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            write_class_png(path, gray, alpha)
            written += 1
            zoom_written += 1
            total_bytes += path.stat().st_size
            cropland_pixels += int(np.count_nonzero(values == 1))
        print(f"zoom {zoom} tiles {zoom_written}", flush=True)
    if written and cropland_pixels == 0:
        raise ValueError("tiles contain no cropland pixel")
    return {
        "min_zoom": contract["min_zoom"],
        "max_zoom": contract["max_zoom"],
        "tiles": written,
        "skipped_empty": skipped,
        "bytes": total_bytes,
        "cropland_pixels": cropland_pixels,
        "path": contract["path"],
    }


# ---- Raster PMTiles: a pyramid built from the top zoom down -----------------

NODATA = 255


def reduce_children(children: dict[tuple[int, int], np.ndarray], resampling: str) -> np.ndarray | None:
    """One parent tile from up to four child tiles.

    children is keyed by (dx, dy), each 0 or 1. A missing child is nodata.
    Nodata never counts. A cell with no valid child pixel stays nodata.

    max      the largest valid class in the 2 x 2 block. Exact.
    q3       the third quartile of the valid classes in the block, by nearest rank:
             the value at rank ceil(0.75 n) of n sorted values. With classes 0 and 1
             that is cropland when more than a quarter of the valid pixels are
             cropland, since the value at that rank is 1 exactly when
             cropland > n - ceil(0.75 n) = floor(n / 4).
    nearest  the top left pixel of the block, valid or not.
    """
    if not children:
        return None
    canvas = np.full((TILE_SIZE * 2, TILE_SIZE * 2), NODATA, dtype=np.uint8)
    for (dx, dy), values in children.items():
        canvas[dy * TILE_SIZE : (dy + 1) * TILE_SIZE, dx * TILE_SIZE : (dx + 1) * TILE_SIZE] = values
    if resampling == "nearest":
        out = canvas[0::2, 0::2].copy()
    else:
        blocks = canvas.reshape(TILE_SIZE, 2, TILE_SIZE, 2).transpose(0, 2, 1, 3).reshape(TILE_SIZE, TILE_SIZE, 4)
        valid = blocks != NODATA
        count = valid.sum(axis=-1)
        cropland = (blocks == 1).sum(axis=-1)
        if resampling == "max":
            picked = cropland > 0
        elif resampling == "q3":
            picked = cropland > count // 4
        else:
            raise ValueError(f"unknown resampling {resampling}")
        out = np.where(count == 0, NODATA, picked.astype(np.uint8)).astype(np.uint8)
    if not np.any(out != NODATA):
        return None
    return out


def build_pyramid(source: Path, bounds: tuple[float, float, float, float], contract: dict) -> dict[int, dict]:
    """Zoom max_zoom is warped from the source. Each lower zoom comes from its four children.

    Returns {zoom: {(x, y): 256 x 256 uint8 of 0, 1, 255}}. An all-nodata tile is left out.
    The top zoom uses maximum resampling for every choice, so the three choices share one
    base. At zoom 12 a tile pixel is within about 20% of a source pixel.
    """
    top = contract["max_zoom"]
    levels: dict[int, dict] = {top: {}}
    for x, y in tiles_covering(bounds, top):
        values = render_tile(source, top, x, y, contract["nodata"], contract["classes"])
        if values is not None:
            levels[top][(x, y)] = values
    for zoom in range(top - 1, contract["min_zoom"] - 1, -1):
        groups: dict[tuple[int, int], dict] = {}
        for (x, y), values in levels[zoom + 1].items():
            groups.setdefault((x // 2, y // 2), {})[(x % 2, y % 2)] = values
        level = {}
        for key, children in groups.items():
            reduced = reduce_children(children, contract["resampling"])
            if reduced is not None:
                level[key] = reduced
        levels[zoom] = level
    return levels


def tile_png(values: np.ndarray) -> bytes:
    data = values != NODATA
    gray = np.where(data, values, 0).astype(np.uint8)
    alpha = np.where(data, 255, 0).astype(np.uint8)
    return encode_class_png(gray, alpha)


def write_pmtiles(levels: dict[int, dict], dest: Path, contract: dict, bounds: tuple[float, float, float, float]) -> dict:
    """One raster archive, one PNG per tile, written in tile id order."""
    from pmtiles.tile import Compression, TileType, zxy_to_tileid
    from pmtiles.writer import Writer

    entries = sorted(
        (zxy_to_tileid(zoom, x, y), values)
        for zoom, level in levels.items()
        for (x, y), values in level.items()
    )
    if not entries:
        raise ValueError("no tiles to write")
    dest.parent.mkdir(parents=True, exist_ok=True)
    png_bytes = 0
    with open(dest, "wb") as handle:
        writer = Writer(handle)
        for tile_id, values in entries:
            png = tile_png(values)
            png_bytes += len(png)
            writer.write_tile(tile_id, png)
        west, south, east, north = bounds
        used = [zoom for zoom, level in levels.items() if level]
        writer.finalize(
            {
                "tile_type": TileType.PNG,
                "tile_compression": Compression.NONE,
                "min_zoom": min(used),
                "max_zoom": max(used),
                "min_lon_e7": int(west * 1e7),
                "min_lat_e7": int(south * 1e7),
                "max_lon_e7": int(east * 1e7),
                "max_lat_e7": int(north * 1e7),
                "center_zoom": min(max(used), 10),
                "center_lon_e7": int((west + east) / 2 * 1e7),
                "center_lat_e7": int((south + north) / 2 * 1e7),
            },
            {
                "name": "cropland class values",
                "format": "png",
                "description": "Gray 1 is cropland, gray 0 is other land, alpha 0 is nodata",
                "type": "overlay",
                "resampling": contract["resampling"],
            },
        )
    return {"tiles": len(entries), "png_bytes": png_bytes, "bytes": dest.stat().st_size}


def count_class(level: dict, value: int = 1) -> int:
    return int(sum(np.count_nonzero(values == value) for values in level.values()))


def zoom_visibility(levels: dict[int, dict], top: int) -> dict:
    """How much zoom-12 cropland a coarser zoom still shows.

    cells         cropland cells at that zoom
    area_kept     cells * 4**(top - zoom) / zoom 12 cropland pixels. Each cell is exactly that
                  many zoom 12 pixels, so 1.0 is the same cropland area. Over 1.0 is overdraw.
    pixels_seen   share of zoom 12 cropland pixels whose cell at that zoom is cropland.
    """
    base = count_class(levels[top])
    rows = {}
    for zoom in sorted(levels, reverse=True):
        k = top - zoom
        cells = count_class(levels[zoom])
        row = {"cells": cells, "area_kept": cells * 4**k / base if base else None}
        if 0 <= k <= 8:
            seen = 0
            span = TILE_SIZE >> k
            for (x, y), values in levels[top].items():
                parent = levels[zoom].get((x >> k, y >> k))
                if parent is None:
                    continue
                sub = parent[(y % 2**k) * span : (y % 2**k + 1) * span, (x % 2**k) * span : (x % 2**k + 1) * span]
                up = np.repeat(np.repeat(sub, 2**k, axis=0), 2**k, axis=1)
                seen += int(np.count_nonzero((values == 1) & (up == 1)))
            row["pixels_seen"] = seen / base if base else None
        rows[zoom] = row
    return {"zoom12_cropland_pixels": base, "zooms": rows}


def q3_from_source(source: Path, zoom: int, tiles: list[tuple[int, int]], nodata: int) -> dict:
    """Third quartile straight from source pixels, for the tiles given.

    The cell is cropland when more than a quarter of the valid source pixels under it are
    cropland. GDAL's average resampling weights each source pixel by the overlap.
    """
    out = {}
    for x, y in tiles:
        dataset = gdal.Warp(
            "",
            str(source),
            format="MEM",
            outputBounds=tile_bounds_3857(zoom, x, y),
            width=TILE_SIZE,
            height=TILE_SIZE,
            dstSRS="EPSG:3857",
            resampleAlg="average",
            outputType=gdal.GDT_Float32,
            srcNodata=nodata,
            dstNodata=-1,
        )
        share = dataset.GetRasterBand(1).ReadAsArray()
        dataset = None
        values = np.where(share < 0, NODATA, (share > 0.25).astype(np.uint8)).astype(np.uint8)
        if np.any(values != NODATA):
            out[(x, y)] = values
    return out


def compare_q3_with_source(source: Path, levels: dict[int, dict], zooms: tuple[int, ...], nodata: int) -> dict:
    """A q3 of q3 values against a q3 taken from the source pixels, tile by tile."""
    rows = {}
    for zoom in zooms:
        pyramid = levels.get(zoom, {})
        direct = q3_from_source(source, zoom, sorted(pyramid), nodata)
        keys = set(pyramid) | set(direct)
        empty = np.full((TILE_SIZE, TILE_SIZE), NODATA, dtype=np.uint8)
        differ = 0
        valid_both = 0
        for key in keys:
            a = pyramid.get(key, empty)
            b = direct.get(key, empty)
            both = (a != NODATA) & (b != NODATA)
            valid_both += int(both.sum())
            differ += int(((a != b) & both).sum())
        rows[zoom] = {
            "pyramid_cropland_cells": count_class(pyramid),
            "source_cropland_cells": count_class(direct),
            "cells_compared": valid_both,
            "cells_differ": differ,
            "share_differ": differ / valid_both if valid_both else None,
        }
    return rows


def crop_window(source: str, window: tuple[float, float, float, float], dest: Path) -> Path:
    """Cut a small window out of the sprout (a VRT or a GeoTIFF) into one local file."""
    west, south, east, north = window
    dest.parent.mkdir(parents=True, exist_ok=True)
    dataset = gdal.Translate(
        str(dest),
        source,
        projWin=(west, north, east, south),
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
    )
    dataset = None
    return dest


def build_pmtiles(source: Path, dest: Path, recipe: dict, resampling: str | None = None) -> dict:
    contract = tile_contract(recipe, resampling)
    if contract["scheme"] != "pmtiles":
        raise ValueError("build_pmtiles needs a pmtiles recipe")
    bounds = raster_bounds(source)
    started = time.perf_counter()
    levels = build_pyramid(source, bounds, contract)
    built = time.perf_counter()
    archive = write_pmtiles(levels, dest, contract, bounds)
    top = contract["max_zoom"]
    stats = {
        "resampling": contract["resampling"],
        "min_zoom": contract["min_zoom"],
        "max_zoom": top,
        "bounds": bounds,
        "tiles_by_zoom": {zoom: len(level) for zoom, level in sorted(levels.items())},
        "pyramid_seconds": round(built - started, 2),
        "write_seconds": round(time.perf_counter() - built, 2),
        **archive,
        "visibility": zoom_visibility(levels, top),
    }
    if stats["visibility"]["zoom12_cropland_pixels"] == 0 and contract["max_zoom"] == 12:
        raise ValueError("tiles contain no cropland pixel")
    stats["_levels"] = levels
    stats["_source"] = source
    stats["_contract"] = contract
    return stats


PREVIEW_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Cropland PMTiles sample: max, q3, nearest</title>
  <link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css"/>
  <style>
    html, body { margin: 0; height: 100%; font: 13px system-ui, sans-serif; }
    #grid { display: grid; grid-template-columns: repeat(3, 1fr); height: 100%; gap: 2px; }
    .pane { position: relative; }
    .pane .map { position: absolute; inset: 0; }
    .label { position: absolute; z-index: 1; left: 8px; top: 8px; background: #fff; padding: 4px 8px; box-shadow: 0 1px 4px rgba(0,0,0,.2); }
    #legend { position: absolute; z-index: 2; left: 8px; bottom: 8px; background: #fff; padding: 4px 8px; box-shadow: 0 1px 4px rgba(0,0,0,.2); }
  </style>
</head>
<body>
  <div id="grid"></div>
  <div id="legend">Orange: cropland (class 1). Faint green: other land (class 0). Clear: nodata. Colour is applied here, in the page.</div>
  <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
  <script src="https://unpkg.com/pmtiles@3.2.0/dist/pmtiles.js"></script>
  <script>
    const names = ["max", "q3", "nearest"];
    const center = __CENTER__;
    const protocol = new pmtiles.Protocol();
    // The archive holds class values. This protocol reads a tile by byte range, then colours it.
    maplibregl.addProtocol("crop", async (params, abort) => {
      const inner = await protocol.tile({ ...params, url: params.url.replace("crop://", "pmtiles://") }, abort);
      if (!inner.data || inner.data.byteLength === 0) return inner;
      const bitmap = await createImageBitmap(new Blob([inner.data], { type: "image/png" }));
      const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
      const ctx = canvas.getContext("2d");
      ctx.drawImage(bitmap, 0, 0);
      const image = ctx.getImageData(0, 0, bitmap.width, bitmap.height);
      const px = image.data;
      for (let i = 0; i < px.length; i += 4) {
        if (px[i + 3] === 0) continue;
        if (px[i] === 1) { px[i] = 229; px[i + 1] = 145; px[i + 2] = 57; px[i + 3] = 255; }
        else { px[i] = 80; px[i + 1] = 140; px[i + 2] = 90; px[i + 3] = 40; }
      }
      ctx.putImageData(image, 0, 0);
      const blob = await canvas.convertToBlob({ type: "image/png" });
      return { data: await blob.arrayBuffer() };
    });
    const maps = [];
    let syncing = false;
    names.forEach((name) => {
      const pane = document.createElement("div");
      pane.className = "pane";
      pane.innerHTML = '<div class="label">' + name + ' <span class="z"></span></div><div class="map" id="m-' + name + '"></div>';
      document.getElementById("grid").appendChild(pane);
      const url = new URL(name + ".pmtiles", window.location.href).href;
      const map = new maplibregl.Map({
        container: "m-" + name,
        style: { version: 8, sources: {
          base: { type: "raster", tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}"], tileSize: 256 },
          crop: { type: "raster", tiles: ["crop://" + url + "/{z}/{x}/{y}"], tileSize: 256, minzoom: 0, maxzoom: 12 }
        }, layers: [
          { id: "base", type: "raster", source: "base" },
          { id: "crop", type: "raster", source: "crop", paint: { "raster-resampling": "nearest" } }
        ] },
        center: center, zoom: 9
      });
      maps.push(map);
      map.on("move", () => {
        pane.querySelector(".z").textContent = "zoom " + map.getZoom().toFixed(1);
        if (syncing) return;
        syncing = true;
        maps.forEach((other) => { if (other !== map) other.jumpTo({ center: map.getCenter(), zoom: map.getZoom() }); });
        syncing = false;
      });
    });
  </script>
</body>
</html>
"""


def parse_byte_range(header: str | None, size: int) -> tuple[int, int, bool]:
    """Inclusive start and end, and whether the client asked for a range."""
    if not header:
        return 0, size - 1, False
    if not header.startswith("bytes="):
        raise ValueError(f"unsupported range: {header}")
    spec = header.removeprefix("bytes=").split(",", 1)[0]
    start_text, _, end_text = spec.partition("-")
    if start_text == "":
        start = max(0, size - int(end_text))
        end = size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    if start < 0 or end < start or start >= size:
        raise ValueError(f"unsatisfiable range: {header}")
    return start, min(end, size - 1), True


class ByteRangeHandler(http.server.SimpleHTTPRequestHandler):
    """Serve files with HTTP Range. Python's default handler returns the whole file."""

    def end_headers(self) -> None:
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Access-Control-Expose-Headers", "Accept-Ranges, Content-Length, Content-Range")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_head(self):
        path = Path(self.translate_path(self.path.split("?", 1)[0]))
        if not path.is_file():
            self.send_error(404, "File not found")
            return None
        size = path.stat().st_size
        try:
            start, end, ranged = parse_byte_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_error(416, "Requested range not satisfiable")
            return None
        handle = path.open("rb")
        handle.seek(start)
        if ranged:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        handle.length = end - start + 1  # type: ignore[attr-defined]
        return handle

    def copyfile(self, source, outputfile) -> None:
        remaining = getattr(source, "length", None)
        if remaining is None:
            super().copyfile(source, outputfile)
            return
        while remaining > 0:
            chunk = source.read(min(65536, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)


def write_preview(folder: Path, center: tuple[float, float]) -> Path:
    html = folder / "preview.html"
    html.write_text(PREVIEW_HTML.replace("__CENTER__", json.dumps(list(center))))
    return html


def command_preview(folder: Path, port: int) -> None:
    missing = [name for name in RESAMPLINGS if not (folder / f"{name}.pmtiles").exists()]
    if missing:
        raise SystemExit(f"no archive for {', '.join(missing)} in {folder}. Run build for each first.")
    handler = functools.partial(ByteRangeHandler, directory=str(folder))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"preview http://127.0.0.1:{port}/preview.html", flush=True)
    print("cancel: Ctrl+C", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped", flush=True)
    finally:
        server.server_close()


def gs_join(prefix: str, name: str) -> str:
    if not prefix.endswith("/"):
        prefix += "/"
    return prefix + name


def gs_to_vsi(gs_path: str) -> str:
    if not gs_path.startswith("gs://"):
        raise ValueError(f"not a gs path: {gs_path}")
    return "/vsigs/" + gs_path[len("gs://") :]


def vsi_copy(source: str, dest: str) -> None:
    incoming = gdal.VSIFOpenL(source, "rb")
    if incoming is None:
        raise OSError(f"cannot read {source}")
    parent = dest.rsplit("/", 1)[0]
    if parent.startswith("/vsi"):
        gdal.MkdirRecursive(parent, 0o755)
    outgoing = gdal.VSIFOpenL(dest, "wb")
    if outgoing is None:
        gdal.VSIFCloseL(incoming)
        raise OSError(f"cannot write {dest}")
    try:
        while True:
            chunk = gdal.VSIFReadL(1, 8 * 1024 * 1024, incoming)
            if not chunk:
                break
            written = gdal.VSIFWriteL(chunk, 1, len(chunk), outgoing)
            if written != len(chunk):
                raise OSError(f"short write to {dest}")
    finally:
        gdal.VSIFCloseL(incoming)
        gdal.VSIFCloseL(outgoing)


def sync_tree(source: Path, destination: str) -> None:
    """Copy the tiles into the destination prefix, without an extra folder."""
    target = gs_to_vsi(destination) if destination.startswith("gs://") else destination
    if not gdal.Sync(str(source).rstrip("/") + "/", target.rstrip("/"), options=["NUM_THREADS=8"]):
        raise OSError(f"could not sync tiles to {destination}")


def batch_job(script_prefix: str) -> dict:
    """One spot VM in europe-west1. Batch deletes it when the task ends."""
    prefix = script_prefix.removeprefix("gs://").rstrip("/")
    fetch = f"""python3 - << 'PY'
import json
import urllib.request
from pathlib import Path
request = urllib.request.Request(
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
    headers={{"Metadata-Flavor": "Google"}},
)
token = json.load(urllib.request.urlopen(request))["access_token"]

def download(name, dest):
    url = "https://storage.googleapis.com/{prefix}/" + name
    body = urllib.request.Request(url, headers={{"Authorization": "Bearer " + token}})
    dest.write_bytes(urllib.request.urlopen(body).read())

folder = Path("/tmp/in")
folder.mkdir(exist_ok=True)
download("cropland.py", folder / "cropland.py")
download("recipe.json", folder / "recipe.json")
PY
python3 /tmp/in/cropland.py run --recipe /tmp/in/recipe.json --work /tmp/work --upload --scratch {script_prefix}
"""
    return {
        "taskGroups": [
            {
                "taskCount": "1",
                "taskSpec": {
                    "maxRunDuration": "10800s",
                    "maxRetryCount": 1,
                    "computeResource": {"cpuMilli": "2000", "memoryMib": "6144"},
                    "environment": {"variables": {"CPL_MACHINE_IS_GCE": "YES", BATCH_ENV: "1"}},
                    "runnables": [
                        {
                            "container": {
                                "imageUri": IMAGE,
                                "entrypoint": "sh",
                                "commands": ["-c", fetch],
                            }
                        }
                    ],
                },
            }
        ],
        "allocationPolicy": {
            "instances": [
                {
                    "policy": {
                        "machineType": MACHINE,
                        "provisioningModel": "SPOT",
                        "bootDisk": {"sizeGb": 30, "type": "pd-balanced"},
                    }
                }
            ],
            "location": {"allowedLocations": [f"regions/{REGION}"]},
            "serviceAccount": {"email": SERVICE_ACCOUNT},
        },
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


def gcloud(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gcloud", *args, f"--project={PROJECT}"], check=True, text=True)


def upload_with_gcloud(local: Path, destination: str) -> None:
    gcloud(["storage", "cp", str(local), destination])


def materialize_sprout(recipe: dict, work: Path) -> Path:
    dest = work / Path(recipe["sprout"]["path"]).name
    if dest.exists() and dest.stat().st_size > 0:
        print(f"using {dest}", flush=True)
        return dest
    print(f"copy {recipe['sprout']['path']}", flush=True)
    vsi_copy(gs_to_vsi(recipe["sprout"]["path"]), str(dest))
    return dest


def command_build_pmtiles(
    recipe: dict,
    source: str,
    dest: Path,
    resampling: str | None,
    window: tuple[float, float, float, float] | None,
    stats_path: Path | None,
) -> dict:
    started = time.perf_counter()
    work_source = Path(source)
    if window is not None:
        work_source = crop_window(source, window, dest.with_suffix(".window.tif"))
    stats = build_pmtiles(work_source, dest, recipe, resampling)
    levels = stats.pop("_levels")
    source_path = stats.pop("_source")
    contract = stats.pop("_contract")
    if contract["resampling"] == "q3":
        stats["q3_against_source"] = compare_q3_with_source(
            source_path, levels, (8, 6, 4), contract["nodata"]
        )
    stats["bounds_note"] = "west, south, east, north in degrees"
    stats["wall_seconds"] = round(time.perf_counter() - started, 2)
    stats["peak_memory_bytes"] = peak_memory_bytes()
    stats["dest"] = str(dest)
    if window is not None:
        center = ((window[0] + window[2]) / 2, (window[1] + window[3]) / 2)
        write_preview(dest.parent, center)
    if stats_path is not None:
        stats_path.write_text(json.dumps(stats, indent=2) + "\n")
    print(
        f"{contract['resampling']} tiles {stats['tiles']} bytes {stats['bytes']} "
        f"wall {stats['wall_seconds']}s peak {stats['peak_memory_bytes']}",
        flush=True,
    )
    return stats


def command_build(recipe: dict, source: Path, dest: Path) -> dict:
    stats = build_tiles(source, dest, recipe)
    print(
        f"tiles {stats['tiles']} skipped_empty {stats['skipped_empty']} bytes {stats['bytes']}",
        flush=True,
    )
    print(f"zooms {stats['min_zoom']}-{stats['max_zoom']}", flush=True)
    return stats


def command_run(recipe: dict, work: Path, upload: bool, scratch: str | None) -> None:
    if os.environ.get(BATCH_ENV) != "1":
        raise SystemExit(
            "The tile run is the Batch job in europe-west1. "
            "From this computer, use: python tiles/cropland.py submit"
        )
    if recipe.get("tiles", {}).get("scheme") == "pmtiles":
        raise SystemExit("The global PMTiles run is task 05. It is not wired to Batch yet.")
    work.mkdir(parents=True, exist_ok=True)
    source = materialize_sprout(recipe, work)
    stats = command_build(recipe, source, work / "tiles")
    if upload:
        sync_tree(work / "tiles", tile_contract(recipe)["path"])
        print(f"uploaded {tile_contract(recipe)['path']}", flush=True)
    if scratch:
        delete_scratch(scratch)
    print(f"tile_bytes {stats['bytes']}", flush=True)


def delete_scratch(prefix: str) -> None:
    folder = gs_to_vsi(prefix).rstrip("/")
    names = gdal.ReadDir(folder) or []
    for name in names:
        if gdal.Unlink(f"{folder}/{name}") != 0:
            print(f"scratch object left in place: {gs_join(prefix, name)}", flush=True)


def command_submit(recipe_path: Path) -> None:
    recipe = load_recipe(recipe_path)
    if recipe.get("tiles", {}).get("scheme") == "pmtiles":
        raise SystemExit("The global PMTiles run is task 05. It is not wired to Batch yet.")
    contract = tile_contract(recipe)
    job_id = f"tiles-{recipe['sprout']['id']}"
    gcloud(["services", "enable", "batch.googleapis.com"])
    existing = subprocess.run(
        ["gcloud", "storage", "ls", contract["path"], f"--project={PROJECT}"],
        text=True,
        capture_output=True,
    )
    if existing.returncode == 0 and existing.stdout.strip():
        raise SystemExit(
            f"{contract['path']} already has objects. Delete that prefix before submitting again."
        )
    work = ROOT / "data" / "tiles" / recipe["id"]
    work.mkdir(parents=True, exist_ok=True)
    recipe_json = work / "recipe.json"
    recipe_json.write_text(json.dumps(recipe) + "\n")
    upload_with_gcloud(Path(__file__).resolve(), gs_join(SCRATCH, "cropland.py"))
    upload_with_gcloud(recipe_json, gs_join(SCRATCH, "recipe.json"))
    config = batch_job(SCRATCH)
    config_path = work / "batch_job.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    print(config_path.read_text(), flush=True)
    gcloud(["batch", "jobs", "submit", job_id, f"--location={REGION}", f"--config={config_path}"])
    print(f"submitted {job_id} in {REGION}", flush=True)


def peak_memory_bytes() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss
    return rss * 1024


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "run", "submit", "preview"))
    parser.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--dest", type=Path)
    parser.add_argument("--work", type=Path, default=ROOT / "data" / "tiles" / "cropland-2024")
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--resampling", choices=RESAMPLINGS)
    parser.add_argument("--window", type=float, nargs=4, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    parser.add_argument("--stats", type=Path)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--scratch", default=None)
    args = parser.parse_args()
    started = time.perf_counter()
    recipe = load_recipe(args.recipe)
    if args.command == "submit":
        command_submit(args.recipe)
    elif args.command == "preview":
        if args.dest is None:
            raise SystemExit("preview needs --dest, the folder with max, q3 and nearest .pmtiles files")
        command_preview(args.dest, args.port)
        return
    elif args.command == "build":
        if args.source is None or args.dest is None:
            raise SystemExit("build needs --source and --dest")
        if recipe.get("tiles", {}).get("scheme") == "pmtiles":
            command_build_pmtiles(
                recipe, str(args.source), args.dest, args.resampling, args.window, args.stats
            )
            return
        command_build(recipe, args.source, args.dest)
    else:
        command_run(recipe, args.work, args.upload, args.scratch)
    elapsed = time.perf_counter() - started
    print(f"wall_seconds {elapsed:.1f}")
    print(f"peak_memory_bytes {peak_memory_bytes()}")


if __name__ == "__main__":
    main()
