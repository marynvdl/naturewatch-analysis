#!/usr/bin/env python3
"""Checks on the cropland v3 share archive before it is published (task 09b, step 6).

  ids     every v3 tile has v2 tiles under it, every v2 tile sits in a v3 tile
  slice   nodata of each v3 tile in a slice equals the nodata of its four v2 tiles, exactly
  sprout  a seeded sample of subtrees rebuilt from the sprout: bytes equal, and the opacity error of the shades

A v3 tile at zoom z (512 px) is the four v2 tiles (256 px) at zoom z + 1, so its pixels are the same size as v2's
at that zoom. Run as Batch tasks from `submit` (one task per slice, one for `ids`, one for `sprout`), or locally on
small archives. Nothing here writes to v2 or v3.

  python tiles/verify_v3.py submit --recipe cookbook/recipes/cropland-2024-v3.yaml --scratch-archive gs://... [--approved]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

import cropland  # noqa: E402
import global_cropland as gc  # noqa: E402
import pyramid  # noqa: E402
import transcode  # noqa: E402
from pmtiles.tile import tileid_to_zxy, zxy_to_tileid  # noqa: E402

V2_ARCHIVE = "gs://nature-watch-tiles/cropland/2024/v2/cropland.pmtiles"
SLICES = 8
SOLID_ZOOM = 11


def verify_prefix(recipe: dict) -> str:
    return gc.scratch_prefix(recipe) + "verify/"


def read_table(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, "callable", dict]:
    """Tile id, body offset and body length of every tile in an archive, sorted by id, and a byte reader."""
    get_bytes = transcode.file_source(path)
    header = transcode.read_header(get_bytes)
    table, _stats = transcode.directory_entries(get_bytes, header)
    ids, offsets, lengths = transcode._addressed(table)
    order = np.argsort(ids, kind="stable")
    base = header["tile_data_offset"]
    return ids[order], offsets[order].astype(np.int64) + base, lengths[order].astype(np.int64), get_bytes, header


def zxy_arrays(ids: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    zxy = np.array([tileid_to_zxy(int(i)) for i in ids], dtype=np.int64).reshape(-1, 3)
    return zxy[:, 0], zxy[:, 1], zxy[:, 2]


def check_tile_ids(v3_ids: np.ndarray, v2_ids: np.ndarray) -> dict:
    """Every v3 tile has at least one v2 tile under it, and every v2 tile at zoom 1 or more is under a v3 tile.

    v2 zoom 0 is not under any 512 px tile: a 512 px tile at zoom 0 is v2 zoom 1.
    """
    z3, x3, y3 = zxy_arrays(v3_ids)
    z2, x2, y2 = zxy_arrays(v2_ids)
    keep = z2 >= 1
    parents = {(int(z) - 1, int(x) // 2, int(y) // 2) for z, x, y in zip(z2[keep], x2[keep], y2[keep])}
    mine = {(int(z), int(x), int(y)) for z, x, y in zip(z3, x3, y3)}
    return {
        "v3_tiles": len(mine),
        "v2_tiles_at_zoom_1_up": int(keep.sum()),
        "v3_without_a_v2_tile": len(mine - parents),
        "v2_without_a_v3_tile": len(parents - mine),
        "v3_zoom_range": [int(z3.min()), int(z3.max())],
    }


def nodata_mask(data: bytes) -> np.ndarray:
    return transcode.decode_rgba(data)[..., 3] == 0


def v2_nodata_mosaic(v2_lookup, get_v2, cache: dict, z: int, x: int, y: int) -> np.ndarray:
    """512 px nodata mask of v3 tile (z, x, y) from the v2 tiles at z + 1. A missing v2 tile is all nodata."""
    out = np.ones((512, 512), dtype=bool)
    for dy in (0, 1):
        for dx in (0, 1):
            found = v2_lookup.get(zxy_to_tileid(z + 1, x * 2 + dx, y * 2 + dy))
            if found is None:
                continue
            offset, length = found
            if offset not in cache:
                if len(cache) > 20000:
                    cache.clear()
                cache[offset] = transcode.decode_v2(get_v2(offset, length)) == transcode.NODATA
            out[dy * 256 : (dy + 1) * 256, dx * 256 : (dx + 1) * 256] = cache[offset]
    return out


def check_slice(v3_path: Path, v2_path: Path, index: int, of: int) -> dict:
    started = time.perf_counter()
    ids3, off3, len3, get3, _ = read_table(v3_path)
    ids2, off2, len2, get2, _ = read_table(v2_path)
    v2_lookup = {int(i): (int(o), int(n)) for i, o, n in zip(ids2.tolist(), off2.tolist(), len2.tolist())}
    bounds = np.linspace(0, len(ids3), of + 1).astype(int)
    lo, hi = int(bounds[index]), int(bounds[index + 1])
    results: dict[tuple, bool] = {}
    cache: dict[int, np.ndarray] = {}
    mismatched: list[list[int]] = []
    pixels_wrong = 0
    for i in range(lo, hi):
        z, x, y = tileid_to_zxy(int(ids3[i]))
        children = tuple(
            v2_lookup.get(zxy_to_tileid(z + 1, x * 2 + dx, y * 2 + dy), (None,))[0] for dy in (0, 1) for dx in (0, 1)
        )
        key = (int(off3[i]), children)
        if key not in results:
            mine = nodata_mask(get3(int(off3[i]), int(len3[i])))
            theirs = v2_nodata_mosaic(v2_lookup, lambda o, n: get2(o, n), cache, z, x, y)
            results[key] = int((mine != theirs).sum())
        if results[key]:
            pixels_wrong += results[key]
            if len(mismatched) < 20:
                mismatched.append([z, x, y])
    return {
        "check": "nodata",
        "index": index,
        "of": of,
        "tiles_checked": hi - lo,
        "distinct_pairs": len(results),
        "pixels_wrong": pixels_wrong,
        "first_mismatches": mismatched,
        "wall_seconds": round(time.perf_counter() - started, 1),
        "peak_memory_bytes": cropland.peak_memory_bytes(),
    }


def opacity(share: np.ndarray) -> np.ndarray:
    """The paint's opacity: 0.2 + 0.75 sqrt(share) where there is any cropland, 0 elsewhere."""
    return np.where(share > 0, 0.2 + 0.75 * np.sqrt(np.clip(share, 0, 1)), 0.0)


def check_sprout(recipe: dict, v3_path: Path, count: int, seed: int, min_body: int = 2000, root_zoom: int = 8) -> dict:
    """Rebuild `count` subtrees from the sprout. Bytes must be equal. Report the shades' opacity error at graded zooms."""
    started = time.perf_counter()
    contract = gc.require_contract(recipe)
    top, solid = contract["max_zoom"], contract["solid_zoom"]
    ids, offsets, lengths, _get, _ = read_table(v3_path)
    z, x, y = zxy_arrays(ids)
    candidates = np.flatnonzero((z == root_zoom) & (lengths > min_body))
    rng = random.Random(seed)
    chosen = sorted(rng.sample(candidates.tolist(), min(count, len(candidates))))
    ops = gc.layer(contract, gc.vsi(recipe["sprout"]["path"]))
    stats: dict[int, list] = {}  # zoom -> [sum error, max error, cells, tiles]
    compared = 0
    roots = []
    for i in chosen:
        root = (root_zoom, int(x[i]), int(y[i]))
        roots.append(list(root))
        captured: dict[int, bytes] = {}

        def encode(value, ops=ops, captured=captured):
            data = ops["encode"](value)
            if value.zoom < solid:
                q = transcode.share_values(data)
                valid = value.share >= 0
                err = np.abs(opacity(np.where(valid, value.share, 0)) - opacity(np.where(q >= 0, q, 0) / 255.0))
                graded = valid & ((value.share > 0) | (q > 0))
                entry = stats.setdefault(value.zoom, [0.0, 0.0, 0, 0])
                if graded.any():
                    entry[0] += float(err[graded].sum())
                    entry[1] = max(entry[1], float(err[graded].max()))
                    entry[2] += int(graded.sum())
                    entry[3] += 1
            return data

        entries: list = []
        pyramid.build_subtree(root, top, ops["render"], ops["reduce"], encode, entries)
        expected = dict(entries)
        found = gc.archive_lookup(v3_path, root, top)
        if expected != found:
            raise pyramid.MergeError(
                f"sample under {root} disagrees with the sprout: {len(set(expected) - set(found))} missing, "
                f"{len(set(found) - set(expected))} extra, {sum(1 for k in set(expected) & set(found) if expected[k] != found[k])} different"
            )
        compared += len(expected)
    cells = sum(v[2] for v in stats.values())
    return {
        "check": "sprout",
        "roots": roots,
        "seed": seed,
        "min_body_bytes": min_body,
        "tiles_compared": compared,
        "bytes_equal": True,
        "opacity_error_graded_zooms": {
            "cells": cells,
            "mean": round(sum(v[0] for v in stats.values()) / cells, 5) if cells else None,
            "max": round(max((v[1] for v in stats.values()), default=0.0), 5),
            "by_zoom": {
                str(zoom): {"mean": round(v[0] / v[2], 5) if v[2] else None, "max": round(v[1], 5), "cells": v[2], "tiles": v[3]}
                for zoom, v in sorted(stats.items())
            },
        },
        "wall_seconds": round(time.perf_counter() - started, 1),
        "peak_memory_bytes": cropland.peak_memory_bytes(),
    }


# ---- Batch ----------------------------------------------------------------------

VERIFY_SHAPE = {**gc.SHARD_SHAPE, "parallelism": SLICES + 2, "max_run_seconds": 14400, "max_retries": 1, "disk_gb": 40}
VERIFY_COMMAND = (
    "python3 /tmp/in/verify_v3.py run --recipe /tmp/in/recipe.json --work /tmp/work "
    "--v3 {archive} --slices " + str(SLICES)
)


def run_task(recipe: dict, index: int, work: Path, v3_remote: str, slices: int) -> dict:
    """Task 0..slices-1 check a slice, task `slices` the ids, task `slices + 1` the sprout sample."""
    v3_path, v2_path = work / "v3.pmtiles", work / "v2.pmtiles"
    gc.copy(v3_remote, v3_path)
    if index <= slices:
        gc.copy(V2_ARCHIVE, v2_path)
    if index < slices:
        result = check_slice(v3_path, v2_path, index, slices)
    elif index == slices:
        ids3, *_ = read_table(v3_path)
        ids2, *_ = read_table(v2_path)
        result = {"check": "tile ids", **check_tile_ids(ids3, ids2)}
    else:
        result = check_sprout(recipe, v3_path, 16, 20261108)
    gc.write_json(verify_prefix(recipe) + f"{index}.json", result)
    print(json.dumps(result), flush=True)
    return result


def command_submit(recipe_path: Path, archive: str, approved: bool) -> None:
    recipe = cropland.load_recipe(recipe_path)
    gc.require_contract(recipe)
    prefix = gc.scratch_prefix(recipe)
    work = gc.ROOT / "data" / "tiles" / recipe["id"] / "global"
    wheel = next(work.glob("pmtiles-*.whl")).name
    config = gc.batch_job(prefix, VERIFY_SHAPE, VERIFY_COMMAND.format(archive=archive), SLICES + 2, wheel, "verify")
    name = f"tiles-{recipe['id']}-verify"
    (work / "recipe.json").write_text(json.dumps(recipe) + "\n")
    config_path = work / f"{name}.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"job {name}: {SLICES + 2} tasks, parallelism {VERIFY_SHAPE['parallelism']}, results under {verify_prefix(recipe)}")
    print(f"status: gcloud batch jobs describe {name} --location={cropland.REGION} --project={cropland.PROJECT}")
    print(f"tasks:  gcloud batch tasks list --job={name} --location={cropland.REGION} --project={cropland.PROJECT}")
    print(f"cancel: gcloud batch jobs delete {name} --location={cropland.REGION} --project={cropland.PROJECT}")
    if not approved:
        print("not submitted: the expected cost is not approved (pass --approved after approval)")
        return
    for file_name in gc.CODE_FILES:
        cropland.upload_with_gcloud(Path(__file__).resolve().parent / file_name, prefix + "code/" + file_name)
    cropland.upload_with_gcloud(work / "recipe.json", prefix + "code/recipe.json")
    cropland.upload_with_gcloud(work / wheel, prefix + "code/" + wheel)
    cropland.gcloud(["batch", "jobs", "submit", name, f"--location={cropland.REGION}", f"--config={config_path}"])
    print(f"submitted {name}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("submit", "run"))
    parser.add_argument("--recipe", type=Path, default=cropland.ROOT / "cookbook" / "recipes" / "cropland-2024-v3.yaml")
    parser.add_argument("--work", type=Path, default=Path("/tmp/work"))
    parser.add_argument("--v3", help="the v3 archive to check, a path or gs://")
    parser.add_argument("--scratch-archive", help="submit: where the merge wrote the v3 archive")
    parser.add_argument("--slices", type=int, default=SLICES)
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args()
    if args.command == "submit":
        command_submit(args.recipe, args.scratch_archive, args.approved)
        return
    recipe = cropland.load_recipe(args.recipe)
    index = int(os.environ.get("BATCH_TASK_INDEX", "-1"))
    if args.command == "run":
        if os.environ.get(cropland.BATCH_ENV) != "1" or index < 0:
            raise SystemExit("run is a Batch task. From this computer use: submit")
        run_task(recipe, index, args.work, args.v3, args.slices)


if __name__ == "__main__":
    main()
