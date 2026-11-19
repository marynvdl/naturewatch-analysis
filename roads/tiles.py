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
  python roads/tiles.py local --sources DIR --work DIR      the sharded global flow on a folder of block COGs
  python roads/tiles.py shard | merge --plan plan.json      Batch tasks, submitted by roads/batch.py

The global archive is built in shards, one per zoom 5 tile that meets a block with roads, as cropland
version 3 is (tiles/global_cropland.py, tiles/pyramid.py). A shard writes its archive, its unquantised
root share and a record, last, so a rerun skips it. The merge builds zooms 0–4 from the root shares,
streams the shards in tile id order, rebuilds sample subtrees from the sprout and publishes.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple

import numpy as np
from osgeo import gdal

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tiles"))

import blocks  # noqa: E402
import cropland  # noqa: E402  tiles/cropland.py
import global_cropland as store  # noqa: E402  storage, upload and Range check helpers
import pyramid  # noqa: E402
import seed as seedmod  # noqa: E402
import shards  # noqa: E402  tiles/shards.py
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


def tile_bounds_deg(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """West, south, east, north of an XYZ tile in degrees."""
    n = 2**z

    def lat(index: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * index / n))))

    return x / n * 360.0 - 180.0, lat(y + 1), (x + 1) / n * 360.0 - 180.0, lat(y)


def codes_of(level: np.ndarray, surface: np.ndarray) -> np.ndarray:
    """Burn codes 1–12 from the two sprout bands, ordered so that max picks the highest level, then paved."""
    rank = np.select([surface == 2, surface == 1], [1, 2], 0)
    return np.where(level > 0, (level.astype(np.int16) - 1) * 3 + rank + 1, 0).astype(np.uint8)


MARGIN = 2  # sprout cells read around a tile, so the warp sees every cell a tile pixel overlaps


def layer(sprout_path: str | Path):
    """render(z, x, y) for the top zoom, from a sprout with the two bands (a COG or a VRT over blocks).

    Each tile reads the sprout cells under it with a small margin, derives road (0/1) and the burn code
    in memory, and warps them to the tile: average for the share, max for the code.
    """
    source = gdal.Open(str(sprout_path))
    west0, pixel, _, north0, _, neg = source.GetGeoTransform()
    width, height = source.RasterXSize, source.RasterYSize
    srs = source.GetProjection()
    level_band, surface_band = source.GetRasterBand(1), source.GetRasterBand(2)  # render keeps `source` alive below

    def mem(array: np.ndarray, col: int, row: int, dtype) -> gdal.Dataset:
        rows, cols = array.shape
        out = gdal.GetDriverByName("MEM").Create("", cols, rows, 1, dtype)
        out.SetGeoTransform((west0 + col * pixel, pixel, 0, north0 + row * neg, 0, neg))
        out.SetProjection(srs)
        out.GetRasterBand(1).WriteArray(array)
        return out

    def render(z: int, x: int, y: int) -> RoadTile | None:
        west, south, east, north = tile_bounds_deg(z, x, y)
        col0 = max(0, math.floor((west - west0) / pixel) - MARGIN)
        col1 = min(width, math.ceil((east - west0) / pixel) + MARGIN)
        row0 = max(0, math.floor((north0 - north) / -neg) - MARGIN)
        row1 = min(height, math.ceil((north0 - south) / -neg) + MARGIN)
        if col1 <= col0 or row1 <= row0:
            return None
        level = level_band.ReadAsArray(col0, row0, col1 - col0, row1 - row0)
        if not level.any():
            return None
        surface = surface_band.ReadAsArray(col0, row0, col1 - col0, row1 - row0)
        bounds = cropland.tile_bounds_3857(z, x, y)
        road = gdal.Warp(
            "", mem((level > 0).astype(np.uint8), col0, row0, gdal.GDT_Byte), format="MEM", outputBounds=bounds,
            width=SIZE, height=SIZE, dstSRS="EPSG:3857", resampleAlg="average", outputType=gdal.GDT_Float32,
            srcNodata=255, dstNodata=-1,
        )
        share = road.GetRasterBand(1).ReadAsArray().astype(np.float32)
        share = np.where(share > 0, share, 0).astype(np.float32)
        if not np.any(share > 0):
            return None
        warped = gdal.Warp(
            "", mem(codes_of(level, surface), col0, row0, gdal.GDT_Byte), format="MEM", outputBounds=bounds,
            width=SIZE, height=SIZE, dstSRS="EPSG:3857", resampleAlg="max", outputType=gdal.GDT_Byte,
            warpOptions=["INIT_DEST=0"],
        )
        return RoadTile(share, warped.GetRasterBand(1).ReadAsArray(), z)

    render.source = source

    return render


def build(sprout_path: Path, bounds, top_zoom: int, steps: int, dest: Path, full: float = 1.0) -> dict:
    started = time.perf_counter()
    render = layer(sprout_path)
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


# ---- the global archive in shards -------------------------------------------------------

CODE_VERSION = "1"
MERCATOR_LAT = 85.0511287798  # Web Mercator stops here; roads beyond it are not drawn
BATCH_ENV = "NATUREWATCH_SPROUT_BATCH"  # set by roads/batch.py on every roads job


def block_cells(names) -> dict[str, pyramid.Bounds]:
    """Bounds of each block, clipped to Web Mercator's latitudes."""
    out = {}
    for name in names:
        west, south, east, north = blocks.bounds(name)
        south, north = max(south, -MERCATOR_LAT), min(north, MERCATOR_LAT)
        if south < north:
            out[name] = (west, south, east, north)
    return out


def shard_plan(names) -> list[dict]:
    """One shard per zoom 5 tile that meets a block, in tile id order, each listing the blocks it reads."""
    return pyramid.plan_shards(block_cells(names), SHARD_ZOOM, cropland.tiles_covering)


def tiles_scratch(recipe: dict) -> str:
    return f"gs://nature-watch-bucket/scratch/roads/{recipe['id']}/tiles/"


def settings(recipe: dict, top: int | None = None) -> dict:
    spec = recipe["tiles"]
    return {
        "code": CODE_VERSION,
        "recipe": recipe["id"],
        "sprout": recipe["sprout"]["path"],
        "top_zoom": top or spec["max_zoom"],
        "share_steps": spec["share_steps"],
        "share_full": spec["share_full"],
        "tile_size": SIZE,
        "shard_zoom": SHARD_ZOOM,
    }


def archive_metadata(recipe: dict, config: dict) -> dict:
    return {
        "name": "roads",
        "format": "png",
        "type": "overlay",
        "version": recipe["id"],
        "release": recipe["source"]["release"],
        "attribution": recipe["source"]["attribution"],
        "licence": recipe["source"]["licence"],
        "description": __doc__.split("\n\n")[1],
        "tile_size": config["tile_size"],
        "share_steps": config["share_steps"],
        "share_full": config["share_full"],
        "levels": recipe["sprout"]["bands"]["level"],
        "surfaces": recipe["sprout"]["bands"]["surface"],
    }


def coders(config: dict):
    steps, full = config["share_steps"], config["share_full"]
    return lambda tile: encode(tile, steps, full)


def shard_layer(recipe: dict, top: int | None = None) -> shards.ShardLayer:
    """Roads for tiles/shards.py: the root tile is kept as its share alone."""
    config = settings(recipe, top)
    return shards.ShardLayer(
        name="roads",
        config=config,
        open_render=layer,
        reduce=reduce,
        encode=coders(config),
        save_root=lambda tile: {"share": tile.share.astype(np.float32)},
        load_root=lambda arrays: RoadTile(arrays["share"], None, SHARD_ZOOM),
        metadata=archive_metadata(recipe, config),
        source_bounds=lambda name: block_cells([name])[name],
        shard_zoom=SHARD_ZOOM,
        top_zoom=config["top_zoom"],
    )


def sprout_vrt(recipe: dict) -> str:
    return store.vsi(recipe["sprout"]["path"].rstrip("/") + "/" + recipe["sprout"]["id"].rsplit("-", 1)[0] + ".vrt")


def run_shard(recipe: dict, shard: dict, work: Path, scratch: str, sources: str, top: int | None = None) -> dict:
    return shards.run_shard(shard_layer(recipe, top), shard, work, scratch, sources)


def run_merge(
    recipe: dict, plan: list[dict], work: Path, scratch: str, dest: str, top: int | None = None,
    verify_samples: int = 0, sprout: str | None = None, keep_scratch: bool = False, publish: bool = True,
) -> dict:
    return shards.run_merge(
        shard_layer(recipe, top), plan, work, scratch, dest, sprout or sprout_vrt(recipe), verify_samples,
        keep_scratch=keep_scratch, publish=publish,
    )


def command_local(recipe: dict, sources: Path, work: Path, top: int | None) -> dict:
    """The whole sharded flow on a local folder of block COGs named like 30S_020E.tif. Nothing is published."""
    plan = shard_plan(sorted(path.stem for path in sources.glob("*.tif")))
    return shards.command_local(shard_layer(recipe, top), plan, sources, work)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("window", "local", "shard", "merge"))
    parser.add_argument("name", nargs="?")
    parser.add_argument("--plan", type=Path, help="shard, merge: the plan written by roads/batch.py submit")
    parser.add_argument("--work", type=Path, default=Path("/tmp/work"))
    parser.add_argument("--sources", type=Path, help="local: folder of block COGs")
    parser.add_argument("--verify-samples", type=int, default=0)
    parser.add_argument("--recipe", type=Path, default=seedmod.DEFAULT_RECIPE)
    parser.add_argument("--top-zoom", type=int, help="default tiles.max_zoom")
    parser.add_argument("--steps", type=int, help="default tiles.share_steps")
    parser.add_argument("--full", type=float, help="share drawn with the darkest shade, default tiles.share_full")
    parser.add_argument("--sprout", type=Path, help="default data/roads/sprout/<name>.tif")
    args = parser.parse_args()
    recipe = seedmod.load_recipe(args.recipe)
    if args.command == "local":
        print(json.dumps(command_local(recipe, args.sources, args.work, args.top_zoom)))
        return
    if args.command in ("shard", "merge"):
        if os.environ.get(BATCH_ENV) != "1":
            raise SystemExit("shard and merge run as Batch tasks. From this computer: python roads/batch.py submit tiles")
        plan = json.loads(args.plan.read_text())
        if args.command == "shard":
            shard = plan["shards"][int(os.environ.get("BATCH_TASK_INDEX", "0"))]
            run_shard(recipe, shard, args.work, tiles_scratch(recipe), recipe["sprout"]["path"].rstrip("/") + "/blocks/")
        else:
            run_merge(recipe, plan["shards"], args.work, tiles_scratch(recipe), recipe["tiles"]["path"], verify_samples=args.verify_samples)
        return
    spec = recipe["tiles"]
    args.top_zoom = args.top_zoom or spec["max_zoom"]
    args.steps = args.steps or spec["share_steps"]
    args.full = args.full or spec["share_full"]
    sprout_path = args.sprout or seedmod.DATA / "sprout" / f"{args.name}.tif"
    suffix = f"-full{args.full:g}"
    dest = seedmod.DATA / "tiles" / f"{sprout_path.stem}-z{args.top_zoom}{suffix}.pmtiles"
    record = build(sprout_path, sproutmod.window_bounds(args.name), args.top_zoom, args.steps, dest, args.full)
    print(json.dumps(record))


if __name__ == "__main__":
    main()
