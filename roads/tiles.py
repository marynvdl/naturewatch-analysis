#!/usr/bin/env python3
"""Raster PMTiles for roads from the roads sprout: share below the top zoom, level and surface at it.

A tile is a 512 px palette PNG. Red carries the value. Index 0 is transparent.

  below the top zoom   red = round((k / steps)² × 255) for shade k = 1..steps, k = max(1, round(√share × steps)).
                       share is the fraction of sprout cells under the pixel that are road cells.
                       Any road keeps at least shade 1, so a lone track does not vanish.
  at the top zoom      red = level × 4 + surface (level 1–4, surface 0 unknown, 1 paved, 2 unpaved),
                       the highest level under the pixel and the surface of a road of that level.

A tile with no road is not stored. A parent's share is the mean of its four children's,
an absent child counting as 0, so it equals the share of top-zoom pixels under it.

  python roads/tiles.py window chinko --top-zoom 11         data/roads/tiles/chinko-z11.pmtiles
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import NamedTuple

import numpy as np
from osgeo import gdal

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tiles"))

import cropland  # noqa: E402
import pyramid  # noqa: E402
import seed as seedmod  # noqa: E402
import sprout as sproutmod  # noqa: E402
import transcode  # noqa: E402

gdal.UseExceptions()

SIZE = 512
SHARD_ZOOM = 5
TRANSPARENT = (0, 0, 1)  # index 0, alpha 0 by tRNS. The cropland archives use the same colour key.


class RoadTile(NamedTuple):
    share: np.ndarray  # float32, 0–1
    code: np.ndarray | None  # burn codes 0–12 at the top zoom, else None
    zoom: int


def red_of_code(code: int) -> int:
    level, surface = sproutmod.decode(np.array([code]))
    return int(level[0]) * 4 + int(surface[0])


def shade_index(share: np.ndarray, steps: int, full: float = 1.0) -> np.ndarray:
    """Shade 1..steps, evenly spaced in √(share / full). A share of `full` or more is the darkest shade."""
    k = np.rint(np.sqrt(np.clip(share / full, 0, 1)) * steps).astype(np.int32)
    return np.where(share > 0, np.maximum(k, 1), 0).astype(np.uint8)


def palette_png(index: np.ndarray, reds: list[int]) -> bytes:
    palette = np.zeros((len(reds), 3), dtype=np.uint8)
    palette[0] = TRANSPARENT
    for i, red in enumerate(reds[1:], start=1):
        palette[i, 0] = red
    depth = 4 if len(reds) <= 16 else 8
    return transcode._png(transcode._pack(index, depth), depth, 3, [(b"PLTE", palette.tobytes()), (b"tRNS", b"\x00")])


def share_reds(steps: int) -> list[int]:
    return [0] + [int(round((k / steps) ** 2 * 255)) for k in range(1, steps + 1)]


TOP_REDS = [0] + [red_of_code(c) for c in range(1, 13)]


def encode(tile: RoadTile, steps: int, full: float = 1.0) -> bytes:
    if tile.code is not None:
        return palette_png(tile.code.astype(np.uint8), TOP_REDS)
    return palette_png(shade_index(tile.share, steps, full), share_reds(steps))


def reduce(children: dict) -> RoadTile | None:
    big = np.zeros((SIZE * 2, SIZE * 2), dtype=np.float32)
    zoom = None
    for (dx, dy), child in children.items():
        big[dy * SIZE : (dy + 1) * SIZE, dx * SIZE : (dx + 1) * SIZE] = child.share
        zoom = child.zoom - 1
    share = big.reshape(SIZE, 2, SIZE, 2).mean(axis=(1, 3))
    if not np.any(share > 0):
        return None
    return RoadTile(share, None, zoom)


def derived_rasters(sprout_path: Path, work: Path) -> tuple[Path, Path]:
    """road (0/1) and burn code rasters beside the sprout, for the warps."""
    work.mkdir(parents=True, exist_ok=True)
    source = gdal.Open(str(sprout_path))
    level = source.GetRasterBand(1).ReadAsArray()
    surface = source.GetRasterBand(2).ReadAsArray()
    rank = np.select([surface == 2, surface == 1], [1, 2], 0)
    codes = np.where(level > 0, (level.astype(np.int16) - 1) * 3 + rank + 1, 0).astype(np.uint8)
    paths = []
    for name, array in (("road", (level > 0).astype(np.uint8)), ("code", codes)):
        path = work / f"{sprout_path.stem}-{name}.tif"
        out = gdal.GetDriverByName("GTiff").Create(str(path), source.RasterXSize, source.RasterYSize, 1, gdal.GDT_Byte, ["COMPRESS=DEFLATE", "TILED=YES"])
        out.SetGeoTransform(source.GetGeoTransform())
        out.SetProjection(source.GetProjection())
        out.GetRasterBand(1).WriteArray(array)
        out = None
        paths.append(path)
    return paths[0], paths[1]


def layer(road: Path, codes: Path):
    def render(z: int, x: int, y: int) -> RoadTile | None:
        share = cropland.fraction_tile(road, z, x, y, 255, SIZE)
        if share is None:
            return None
        share = np.where(share > 0, share, 0).astype(np.float32)
        if not np.any(share > 0):
            return None
        warped = gdal.Warp(
            "", str(codes), format="MEM", outputBounds=cropland.tile_bounds_3857(z, x, y), width=SIZE, height=SIZE,
            dstSRS="EPSG:3857", resampleAlg="max", outputType=gdal.GDT_Byte, warpOptions=["INIT_DEST=0"],
        )
        code = warped.GetRasterBand(1).ReadAsArray()
        return RoadTile(share, code, z)

    return render


def build(sprout_path: Path, bounds, top_zoom: int, steps: int, dest: Path, full: float = 1.0) -> dict:
    started = time.perf_counter()
    road, codes = derived_rasters(sprout_path, dest.parent / "work")
    render = layer(road, codes)
    entries: list[pyramid.Entry] = []
    roots = {}
    for x, y in cropland.tiles_covering(bounds, SHARD_ZOOM):
        value = pyramid.build_subtree((SHARD_ZOOM, x, y), top_zoom, render, reduce, lambda t: encode(t, steps, full), entries)
        if value is not None:
            roots[(x, y)] = value
    entries += pyramid.upper_levels(roots, SHARD_ZOOM, reduce, lambda t: encode(t, steps, full))
    entries.sort(key=lambda entry: entry[0])
    metadata = {"name": "roads", "format": "png", "type": "overlay", "description": __doc__.split("\n\n")[1]}
    written = pyramid.write_archive(entries, dest, pyramid.archive_header(bounds, 0, top_zoom), metadata)
    by_zoom: dict[int, dict] = {}
    for tile_id, data in entries:
        z = pyramid.tileid_to_zxy(tile_id)[0]
        stat = by_zoom.setdefault(z, {"tiles": 0, "bytes": 0, "largest": 0})
        stat["tiles"] += 1
        stat["bytes"] += len(data)
        stat["largest"] = max(stat["largest"], len(data))
    return {
        "archive": dest.name,
        **written,
        "zooms": {str(z): by_zoom[z] for z in sorted(by_zoom)},
        "wall_seconds": round(time.perf_counter() - started, 1),
        "peak_memory_bytes": seedmod.peak_memory_bytes(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("window",))
    parser.add_argument("name")
    parser.add_argument("--top-zoom", type=int, default=11)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--full", type=float, default=1.0, help="share drawn with the darkest shade")
    parser.add_argument("--sprout", type=Path, help="default data/roads/sprout/<name>.tif")
    args = parser.parse_args()
    sprout_path = args.sprout or seedmod.DATA / "sprout" / f"{args.name}.tif"
    suffix = "" if args.full == 1.0 else f"-full{args.full:g}"
    dest = seedmod.DATA / "tiles" / f"{sprout_path.stem}-z{args.top_zoom}{suffix}.pmtiles"
    record = build(sprout_path, sproutmod.window_bounds(args.name), args.top_zoom, args.steps, dest, args.full)
    print(json.dumps(record))


if __name__ == "__main__":
    main()
