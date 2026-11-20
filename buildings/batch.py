#!/usr/bin/env python3
"""The global Buildings 2023 archive as Batch jobs in europe-west1 (task 05), from the sprout's block COGs.

  python buildings/batch.py plan                                  the shard plan, data/buildings/batch/plan.json
  python buildings/batch.py local <window> [...]                  the shard and merge code on a task 04 window cut,
                                                                  compared with the task 04 window archive
  python buildings/batch.py submit tiles --pilot 5-15-14 [--approved]   one shard; prints the job, submits only with --approved
  python buildings/batch.py submit tiles --approved              every shard, one task each
  python buildings/batch.py records                               totals from the shard records written so far
  python buildings/batch.py submit merge --approved              after every shard has a record: merge, verify, publish
  python buildings/batch.py shard | merge --plan plan.json        the Batch tasks themselves

The layer is buildings/tiles.py with the settings on the recipe's `tiles`: the top zoom is warped from the
sprout with `average`, Google's speckle rule applied to what each tile reads, shaded in √(share / full),
a parent the mean of its children, and a tile with no building not stored. tiles/shards.py does the shard,
merge and sample check, as for roads.

One shard per zoom 5 tile that meets a sprout block with a COG. A shard also copies every block within
MARGIN + SEED_RADIUS cells of its edge, so the 2 km speckle rule sees past the shard and shards meet without a seam.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tiles"))

import cropland  # noqa: E402  tiles/cropland.py: tile maths, gcloud
import pyramid  # noqa: E402
import shards  # noqa: E402  tiles/shards.py

_spec = importlib.util.spec_from_file_location("buildings_tiles", HERE / "tiles.py")
btiles = importlib.util.module_from_spec(_spec)
sys.modules["buildings_tiles"] = btiles
_spec.loader.exec_module(btiles)

CODE_VERSION = "2"
SHARD_ZOOM = 5
BLOCK_DEG = 10
MERCATOR_LAT = 85.0511287798
CELL = 0.00025
MARGIN_DEG = (btiles.MARGIN + btiles.SEED_RADIUS) * CELL  # what a top-zoom tile reads past its edge
BATCH_ENV = "NATUREWATCH_TILES_BATCH"
PROJECT = cropland.PROJECT
REGION = shards.REGION
WORK = ROOT / "data" / "buildings" / "batch"
PARALLELISM = 12  # 24 E2 CPUs in europe-west1, two a machine
CODE_FILES = (
    "buildings/batch.py", "buildings/tiles.py",
    "tiles/cropland.py", "tiles/global_cropland.py", "tiles/pyramid.py", "tiles/shards.py", "tiles/transcode.py",
)
SHAPES = {
    "tiles": {  # one shard a task: its block COGs and one root-to-leaf path of 512 px tiles in memory
        "machine": "e2-standard-2",
        "cpu_milli": "2000",
        "memory_mib": "6144",
        "disk_gb": 30,
        "disk_type": "pd-standard",  # pd-balanced counts against the region's 500 GB SSD quota
        "max_run_seconds": 4 * 3600,
        "max_retries": 3,
    },
    "merge": {  # every shard and root on disk, the archive and the writer's spool, then the sample rebuilds
        "machine": "e2-standard-4",
        "cpu_milli": "4000",
        "memory_mib": "14336",
        "disk_gb": 150,
        "disk_type": "pd-standard",
        "max_run_seconds": 6 * 3600,
        "max_retries": 1,
    },
}


# ---- the layer ------------------------------------------------------------------------


def settings(recipe: dict) -> dict:
    """Everything that changes a shard's bytes. Refuses a recipe whose settings this code does not build."""
    spec = recipe["tiles"]
    wanted = {"encoding": "share-palette", "resampling": "share", "share_curve": "sqrt", "top_rule": "graded",
              "parent": "mean", "empty": "drop", "tile_size": btiles.SIZE}
    wrong = {key: spec.get(key) for key, value in wanted.items() if spec.get(key) != value}
    if wrong:
        raise SystemExit(f"recipe tiles settings this code does not build: {wrong}")
    speckle = spec["google_speckle"]
    if speckle["seed_radius_cells"] != btiles.SEED_RADIUS:
        raise SystemExit(f"google_speckle.seed_radius_cells is {speckle['seed_radius_cells']}, the code reads {btiles.SEED_RADIUS}")
    return {
        "code": CODE_VERSION,
        "recipe": recipe["id"],
        "sprout": recipe["sprout"]["path"],
        "top_zoom": spec["max_zoom"],
        "share_steps": spec["share_steps"],
        "share_full": spec["share_full"],
        "top_rule": spec["top_rule"],
        "parent": spec["parent"],
        "empty": spec["empty"],
        "google_floor": speckle["floor"],
        "google_seed": speckle["seed_share"],
        "seed_radius_cells": speckle["seed_radius_cells"],
        "tile_size": btiles.SIZE,
        "shard_zoom": SHARD_ZOOM,
    }


def encoder(config: dict):
    top, steps, full = config["top_zoom"], config["share_steps"], config["share_full"]

    def encode(tile) -> bytes:
        if not (tile.share > 0).any():  # `empty: drop`: a tile with no building is not stored
            return b""
        return btiles.encode(tile, top, "graded", full, steps)

    return encode


def archive_metadata(recipe: dict, config: dict) -> dict:
    google, tempo = recipe["sources"]["google"], recipe["sources"]["tempo"]
    return {
        "name": "buildings",
        "format": "png",
        "type": "overlay",
        "version": recipe["id"],
        "year": recipe["year"],
        "attribution": f"{google['name']} 2023 ({google['licence']}); {tempo['name']} {tempo['quarter']} ({tempo['licence']})",
        "sources": {
            "google": {"name": google["name"], "url": google["url"], "licence": google["licence"]},
            "tempo": {"name": tempo["name"], "url": tempo["url"], "licence": tempo["licence"], "quarter": tempo["quarter"]},
        },
        "description": (
            "Building share: the fraction of each pixel covered by buildings. Index 0 is nodata (alpha 0); "
            "index k + 1 is shade k of share_steps, red = 255 × (k / steps)², k = round(√(share / share_full) × steps), "
            "and any share above 0 keeps shade 1. A parent's share is the mean of its four children. "
            "A tile with no building is not stored."
        ),
        "tile_size": config["tile_size"],
        "share_steps": config["share_steps"],
        "share_full": config["share_full"],
        "google_speckle": {"floor": config["google_floor"], "seed_share": config["google_seed"],
                           "seed_radius_cells": config["seed_radius_cells"]},
    }


def block_bounds(name: str) -> pyramid.Bounds:
    """A sprout block's bounds, clipped to Web Mercator's latitudes. Names as the Google seed: W010N00."""
    west = int(name[1:4]) * (1 if name[0] == "E" else -1)
    south = int(name[5:7]) * (1 if name[4] == "N" else -1)
    return west, max(south, -MERCATOR_LAT), west + BLOCK_DEG, min(south + BLOCK_DEG, MERCATOR_LAT)


def shard_layer(recipe: dict, bounds_of=block_bounds) -> shards.ShardLayer:
    config = settings(recipe)

    def open_render(path: str):
        return btiles.layer(path, 0.0, config["google_floor"], config["google_seed"], config["seed_radius_cells"])

    return shards.ShardLayer(
        name="buildings",
        config=config,
        open_render=open_render,
        reduce=lambda children: btiles.reduce(children, "mean"),
        encode=encoder(config),
        save_root=lambda tile: {"share": tile.share.astype(np.float32), "valid": tile.valid.astype(np.float32),
                                "zoom": np.array(tile.zoom)},
        load_root=lambda arrays: btiles.Tile(arrays["share"], arrays["valid"], int(arrays["zoom"])),
        metadata=archive_metadata(recipe, config),
        source_bounds=bounds_of,
        shard_zoom=SHARD_ZOOM,
        top_zoom=config["top_zoom"],
    )


# ---- the plan -----------------------------------------------------------------------------


def overlaps(a: pyramid.Bounds, b: pyramid.Bounds) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def shard_plan(cells: dict[str, pyramid.Bounds]) -> list[dict]:
    """One shard per zoom 5 tile that meets a source with data, in tile id order. Its sources are every source
    that meets the shard widened by MARGIN_DEG, so a tile on the shard's edge reads what it would from the VRT."""
    plan = pyramid.plan_shards(cells, SHARD_ZOOM, cropland.tiles_covering)
    for shard in plan:
        west, south, east, north = btiles.tile_bounds_deg(SHARD_ZOOM, shard["x"], shard["y"])
        wide = (west - MARGIN_DEG, south - MARGIN_DEG, east + MARGIN_DEG, north + MARGIN_DEG)
        shard["sources"] = sorted(name for name, bounds in cells.items() if overlaps(bounds, wide))
    return plan


def sprout_blocks(recipe: dict) -> list[str]:
    """Blocks of the published sprout that have a COG, from their records."""
    import shutil
    import subprocess

    prefix = cropland.gs_join(recipe["sprout"]["path"], "blocks/")
    dest = WORK / "sprout-records"
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    subprocess.run(["gcloud", "storage", "cp", cropland.gs_join(prefix, "*.json"), str(dest), f"--project={PROJECT}"],
                   check=True, capture_output=True)
    records = [json.loads(path.read_text()) for path in sorted(dest.glob("*.json"))]
    names = sorted(r["block"] for r in records if r.get("bytes"))
    if len(records) != 488:
        raise SystemExit(f"{len(records)} sprout block records, expected 488")
    return names


def make_plan(recipe: dict, pilot: str | None = None) -> dict:
    names = sprout_blocks(recipe)
    plan = shard_plan({name: block_bounds(name) for name in names})
    if pilot:
        plan = [shard for shard in plan if shard["id"] == pilot]
        if not plan:
            raise SystemExit(f"no shard {pilot}")
    return {"kind": "tiles", "shards": plan, "pilot": bool(pilot), "blocks": len(names)}


def scratch(recipe: dict) -> str:
    return f"gs://nature-watch-bucket/scratch/buildings/{recipe['id']}/tiles/"


def sprout_vrt(recipe: dict) -> str:
    return btiles_vsi(cropland.gs_join(recipe["sprout"]["path"], recipe["sprout"]["vrt"]))


def btiles_vsi(path: str) -> str:
    return shards.store.vsi(path)


# ---- the local check on a task 04 window ----------------------------------------------------


def window_tiles(name: str, top: int) -> tuple[set, set]:
    """The tiles the task 04 window archive holds in full: every tile at zoom 11 or more under a zoom 11 tile the
    window meets, and every lower tile all of whose zoom 11 descendants are among them."""
    fetch = set(cropland.tiles_covering(btiles.window_bounds(btiles.windows()[name]), btiles.FETCH_ZOOM))
    inside = set()
    for zoom in range(btiles.FETCH_ZOOM - 1, -1, -1):
        span = 2 ** (btiles.FETCH_ZOOM - zoom)
        for x, y in {(fx // span, fy // span) for fx, fy in fetch}:
            if all((x * span + i, y * span + j) in fetch for i in range(span) for j in range(span)):
                inside.add((zoom, x, y))
    return fetch, inside


def command_local(recipe: dict, name: str) -> dict:
    """Shards and merge over the task 04 cut of one window, compared with the task 04 archive of the approved settings."""
    cut = btiles.sprout_path(name)
    if not cut.exists():
        raise SystemExit(f"{cut} is missing: python buildings/tiles.py fetch {name}")
    ds = btiles.gdal.Open(str(cut))
    west, cell, _, north, _, neg = ds.GetGeoTransform()
    cut_bounds = (west, north + neg * ds.RasterYSize, west + cell * ds.RasterXSize, north)
    ds = None
    layer = shard_layer(recipe, bounds_of=lambda _name: cut_bounds)
    config = layer.config
    work = WORK / "local" / name
    shards.store.copy(cut, work / "sources" / f"{name}.tif")
    plan = pyramid.plan_shards({name: btiles.window_bounds(btiles.windows()[name])}, SHARD_ZOOM, cropland.tiles_covering)
    scratch_dir = str(work / "scratch")
    for shard in plan:
        shards.run_shard(layer, shard, work / "tmp", scratch_dir, str(work / "sources"))
    stats = shards.run_merge(
        layer, plan, work / "merge", scratch_dir, str(work / "buildings.pmtiles"), sprout=str(cut),
        verify_samples=4, edge_samples=2, keep_scratch=True, publish=False,
    )
    sample_settings = {"top_zoom": config["top_zoom"], "top_rule": config["top_rule"], "full": config["share_full"],
                       "parent": config["parent"], "empty": config["empty"],
                       "google_floor": config["google_floor"], "google_seed": config["google_seed"]}
    reference = btiles.DATA / f"{name}-{btiles.settings_name(sample_settings)}.pmtiles"
    sharded = {zxy: data for zxy, data in pyramid.read_archive(work / "merge" / "buildings.pmtiles")}
    window = {zxy: data for zxy, data in pyramid.read_archive(reference)} if reference.exists() else {}
    fetch, inside = window_tiles(name, config["top_zoom"])

    def compared(zxy) -> bool:
        z, x, y = zxy
        if z >= btiles.FETCH_ZOOM:
            return pyramid.ancestor(z, x, y, btiles.FETCH_ZOOM) in fetch
        return zxy in inside

    keys = {k for k in set(sharded) | set(window) if compared(k)}
    same = sum(1 for k in keys if sharded.get(k) == window.get(k))
    differ = sorted(k for k in keys if sharded.get(k) != window.get(k))
    return {
        "window": name, "reference": reference.name, "reference_tiles": len(window), "shards": len(plan),
        "archive_tiles": stats["tiles"], "compared": len(keys), "same": same, "different": len(differ),
        "first_different": ["/".join(map(str, k)) for k in differ[:8]], "verify": stats.get("verify"),
        "shard_seconds": stats["shard_wall_seconds_total"], "peak_memory_bytes": stats["shard_peak_memory_bytes_max"],
    }


# ---- Batch ------------------------------------------------------------------------------------


def require_batch() -> None:
    if os.environ.get(BATCH_ENV) != "1":
        raise SystemExit("shard and merge run as Batch tasks. From this computer: python buildings/batch.py submit tiles")


def job_name(recipe: dict, kind: str, pilot: str | None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    label = f"-pilot-{pilot}" if pilot else ""
    return f"{recipe['id']}-{kind}{label}-{stamp}"


def command_submit(recipe: dict, kind: str, pilot: str | None, approved: bool) -> None:
    if kind == "merge" and pilot:
        raise SystemExit("the merge has no pilot")
    settings(recipe)
    plan = make_plan(recipe, pilot)
    WORK.mkdir(parents=True, exist_ok=True)
    wheel = shards.pmtiles_wheel(WORK)
    prefix = scratch(recipe)
    name = job_name(recipe, kind, pilot)
    plan_name = f"{name}.plan.json"
    tasks = 1 if kind == "merge" else len(plan["shards"])
    command = f"python3 /tmp/in/buildings/batch.py {'shard' if kind == 'tiles' else 'merge'} --recipe /tmp/in/recipe.json --plan /tmp/in/{plan_name} --work /tmp/work"
    script = shards.bootstrap(prefix, wheel.name, CODE_FILES, plan_name) + command + "\n"
    config = shards.job_spec(SHAPES[kind], script, tasks, PARALLELISM if kind == "tiles" else 1, BATCH_ENV)
    (WORK / "recipe.json").write_text(json.dumps(recipe, default=str) + "\n")
    (WORK / plan_name).write_text(json.dumps(plan) + "\n")
    config_path = WORK / f"{name}.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    shape = SHAPES[kind]
    sources = [len(s["sources"]) for s in plan["shards"]]
    print(f"job {name}: {tasks} task(s), {len(plan['shards'])} shards over {plan['blocks']} blocks, "
          f"{min(sources)}–{max(sources)} blocks a shard, spot {shape['machine']}, {shape['disk_gb']} GB {shape['disk_type']}, "
          f"parallelism {config['taskGroups'][0]['parallelism']}")
    print(f"config {config_path}")
    print(f"status: gcloud batch jobs describe {name} --location={REGION} --project={PROJECT}")
    print(f"tasks:  gcloud batch tasks list --job={name} --location={REGION} --project={PROJECT}")
    print(f"cancel: gcloud batch jobs delete {name} --location={REGION} --project={PROJECT}")
    print(f"output: {cropland.gs_join(prefix, 'shards/') if kind == 'tiles' else recipe['tiles']['path']}")
    if not approved:
        print("not submitted: pass --approved once the cost is approved")
        return
    for relative in CODE_FILES:
        cropland.upload_with_gcloud(ROOT / relative, prefix + "code/" + relative)
    for path in (WORK / "recipe.json", WORK / plan_name, wheel):
        cropland.upload_with_gcloud(path, prefix + "code/" + path.name)
    cropland.gcloud(["batch", "jobs", "submit", name, f"--location={REGION}", f"--config={config_path}"])
    print(f"submitted {name}", flush=True)


def command_records(recipe: dict) -> dict:
    import shutil
    import subprocess

    dest = WORK / "shard-records"
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    listing = subprocess.run(["gcloud", "storage", "ls", cropland.gs_join(scratch(recipe), "shards/*.json"),
                              f"--project={PROJECT}"], text=True, capture_output=True)
    if listing.returncode == 0 and listing.stdout.strip():
        cropland.gcloud(["storage", "cp", cropland.gs_join(scratch(recipe), "shards/*.json"), str(dest)])
    records = [json.loads(path.read_text()) for path in sorted(dest.glob("*.json"))]
    out = {"shards": len(records), "with_tiles": sum(1 for r in records if r["tiles"])}
    for key in ("tiles", "bytes", "png_bytes", "wall_seconds", "fetch_seconds"):
        out[key] = round(sum(r.get(key, 0) for r in records), 1)
    out["wall_seconds_max"] = max((r.get("wall_seconds", 0) for r in records), default=0)
    out["peak_memory_bytes_max"] = max((r.get("peak_memory_bytes", 0) for r in records), default=0)
    return out


def load_recipe(path: Path) -> dict:
    if path.suffix == ".json":
        return json.loads(path.read_text())
    import yaml

    return yaml.safe_load(path.read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("plan", "local", "submit", "records", "shard", "merge"))
    parser.add_argument("args", nargs="*", help="local: window names; submit: tiles or merge")
    parser.add_argument("--recipe", type=Path, default=btiles.RECIPE)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--work", type=Path, default=Path("/tmp/work"))
    parser.add_argument("--pilot", help="one shard, for example 5-15-14")
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args()
    recipe = load_recipe(args.recipe)
    if args.command == "plan":
        plan = make_plan(recipe, args.pilot)
        WORK.mkdir(parents=True, exist_ok=True)
        (WORK / "plan.json").write_text(json.dumps(plan) + "\n")
        sources = [len(s["sources"]) for s in plan["shards"]]
        print(json.dumps({"blocks": plan["blocks"], "shards": len(plan["shards"]), "sources_min": min(sources),
                          "sources_max": max(sources), "plan": str(WORK / "plan.json")}))
    elif args.command == "local":
        for name in args.args or ["kocho-window", "edge-window"]:
            print(json.dumps(command_local(recipe, name)), flush=True)
    elif args.command == "submit":
        if args.args not in (["tiles"], ["merge"]):
            parser.error("submit tiles | submit merge")
        command_submit(recipe, args.args[0], args.pilot, args.approved)
    elif args.command == "records":
        print(json.dumps(command_records(recipe)))
    else:
        require_batch()
        plan = json.loads(args.plan.read_text())
        layer = shard_layer(recipe)
        if args.command == "shard":
            shard = plan["shards"][int(os.environ.get("BATCH_TASK_INDEX", "0"))]
            shards.run_shard(layer, shard, args.work, scratch(recipe), cropland.gs_join(recipe["sprout"]["path"], "blocks"))
        else:
            shards.run_merge(
                layer, plan["shards"], args.work, scratch(recipe), recipe["tiles"]["path"], sprout=sprout_vrt(recipe),
                verify_samples=12, edge_samples=4,
            )


if __name__ == "__main__":
    main()
