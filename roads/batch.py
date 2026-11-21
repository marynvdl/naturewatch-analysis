#!/usr/bin/env python3
"""The global roads seed, sprout and tiles as Batch jobs in europe-west1.

The 648 blocks of 10° are split into at most 20 runs of similar weight, one Batch task each.
Each block writes its output, then a JSON record, so a rerun skips finished blocks.

  python roads/batch.py submit seed --pilot 30S_020E            print the pilot job, submit nothing
  python roads/batch.py submit seed --pilot 30S_020E --approved submit it
  python roads/batch.py submit seed --approved                  every block
  python roads/batch.py submit sprout --approved                every block, after every seed block has a record
  python roads/batch.py collect                                 the VRT and STAC files, after every sprout block has a record
  python roads/batch.py records seed                            totals from the records written so far
  python roads/batch.py submit tiles --pilot 5-18-17 [--approved] one shard of the tiles
  python roads/batch.py submit tiles --approved                 every shard, one task each
  python roads/batch.py submit merge --approved                 after every shard has a record: merge, verify, publish

A seed pilot writes to the seed path, as the full run would. A sprout pilot writes to the
scratch prefix, because its neighbours' seed blocks may not exist yet.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.append(str(ROOT / "tiles"))  # after roads/, so `tiles` stays roads/tiles.py


def _load_sprout_helpers():
    """sprout/cropland.py: GCS helpers, project, region, image. Loaded under its own name, so that
    tiles/cropland.py, which roads/tiles.py imports as cropland, does not shadow it."""
    spec = importlib.util.spec_from_file_location("sprout_cropland", ROOT / "sprout" / "cropland.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["sprout_cropland"] = module
    spec.loader.exec_module(module)
    return module


gcs = _load_sprout_helpers()

import blocks  # noqa: E402
import seed as seedmod  # noqa: E402
import shards  # noqa: E402  tiles/shards.py: the Batch bootstrap, job spec and pmtiles wheel

TASKS = 20  # the project allows 24 VMs at once in europe-west1
DUCKDB_VERSION = "2.0.0"
CONTAINER_PYTHON = "312"  # ghcr.io/osgeo/gdal:ubuntu-small-3.11.4 is Ubuntu 24.04
CODE_FILES = ("roads/batch.py", "roads/blocks.py", "roads/seed.py", "roads/sprout.py", "sprout/cropland.py")
TILE_CODE_FILES = (
    "roads/blocks.py", "roads/seed.py", "roads/sprout.py", "roads/tiles.py",
    "tiles/cropland.py", "tiles/global_cropland.py", "tiles/pyramid.py", "tiles/shards.py", "tiles/transcode.py",
)
TILE_PARALLELISM = 12  # 24 E2 CPUs in the region, 2 a machine
SHAPES = {
    "seed": {
        "machine": "e2-standard-4",
        "cpu_milli": "4000",
        "memory_mib": "14336",
        "duckdb_memory": "10GB",
        "disk_gb": 50,
        "max_run_seconds": 6 * 3600,
        "max_retries": 3,
    },
    "sprout": {
        "machine": "e2-highmem-2",  # 16 GB: the densest block, 40N_000E, reads 62 million rows with its neighbours
        "cpu_milli": "2000",
        "memory_mib": "14336",
        "duckdb_memory": "6GB",
        "disk_gb": 100,
        "disk_type": "pd-standard",  # pd-balanced counts against the region's 500 GB SSD quota
        "max_run_seconds": 6 * 3600,
        "max_retries": 3,
    },
    "tiles": {  # one shard a task: a few block COGs and one root-to-leaf path of 512 px tiles in memory
        "machine": "e2-standard-2",
        "cpu_milli": "2000",
        "memory_mib": "6144",
        "disk_gb": 30,
        "disk_type": "pd-standard",
        "max_run_seconds": 2 * 3600,
        "max_retries": 3,
    },
    "merge": {  # every shard on disk, the archive and the writer's spool, then the sample rebuilds from the sprout
        "machine": "e2-standard-4",
        "cpu_milli": "4000",
        "memory_mib": "14336",
        "disk_gb": 60,
        "disk_type": "pd-standard",
        "max_run_seconds": 6 * 3600,
        "max_retries": 1,
    },
}


# ---- paths ---------------------------------------------------------------------------


def seed_object(recipe: dict, block: str, suffix: str) -> str:
    return gcs.gs_join(recipe["seed"]["path"], f"{block}.{suffix}")


def sprout_object(recipe: dict, block: str, suffix: str, pilot: bool = False) -> str:
    prefix = gcs.gs_join(scratch(recipe), "pilot/") if pilot else gcs.gs_join(recipe["sprout"]["path"], "blocks/")
    return gcs.gs_join(prefix, f"{block}.{suffix}")


def scratch(recipe: dict) -> str:
    return f"gs://nature-watch-bucket/scratch/roads/{recipe['id']}/"


def stem(recipe: dict) -> str:
    """roads-2026-10 from sprout id roads-2026-10-v1: the VRT is <stem>.vrt."""
    return recipe["sprout"]["id"].rsplit("-", 1)[0]


# ---- records -------------------------------------------------------------------------


def read_record(path: str) -> dict | None:
    text = gcs.vsi_read_text(gcs.gs_to_vsi(path))
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def is_complete(record: dict | None, data_path: str) -> bool:
    """Written last, so a record means the block is done. A record with bytes must match the object."""
    if record is None:
        return False
    if record.get("bytes", 0) == 0:
        return True
    stat = gcs.gdal.VSIStatL(gcs.gs_to_vsi(data_path))
    return stat is not None and stat.size == record["bytes"]


def upload(local: Path, dest: str) -> None:
    gcs.vsi_copy(str(local), gcs.gs_to_vsi(dest))
    stat = gcs.gdal.VSIStatL(gcs.gs_to_vsi(dest))
    if stat is None or stat.size != local.stat().st_size:
        raise OSError(f"{dest} is not {local.stat().st_size} bytes after upload")


def write_record(dest: str, record: dict) -> None:
    gcs.vsi_write_text(gcs.gs_to_vsi(dest), json.dumps(record, indent=1) + "\n")


def require_batch() -> None:
    if os.environ.get(gcs.BATCH_ENV) != "1":
        raise SystemExit("This runs as a Batch task in europe-west1. From this computer: python roads/batch.py submit ...")


def task_blocks(plan: dict) -> list[str]:
    index = int(os.environ.get("BATCH_TASK_INDEX", "0"))
    return plan["groups"][index]


# ---- seed task -----------------------------------------------------------------------


def run_seed(recipe: dict, plan: dict, work: Path) -> None:
    require_batch()
    work.mkdir(parents=True, exist_ok=True)
    con = seedmod.connect(work, SHAPES["seed"]["duckdb_memory"])
    con.execute(f"SET s3_region='{recipe['source']['s3_region']}'")
    for block in task_blocks(plan):
        data = seed_object(recipe, block, "parquet")
        record_path = seed_object(recipe, block, "json")
        if is_complete(read_record(record_path), data):
            print(f"skip {block}: already written", flush=True)
            continue
        local = work / f"{block}.parquet"
        result = seedmod.write_block(con, recipe, blocks.bounds(block), local)
        if result["rows"]:
            upload(local, data)
            local.unlink()
        record = {
            "block": block,
            "release": recipe["source"]["release"],
            **result,
            "peak_memory_bytes": seedmod.peak_memory_bytes(),
            "written": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        write_record(record_path, record)
        print(json.dumps(record), flush=True)


# ---- sprout task ---------------------------------------------------------------------


def neighbour_seeds(recipe: dict, block: str, pilot: bool) -> tuple[list[tuple[str, dict]], list[str]]:
    """Seed records of the block and its neighbours. Without pilot, every one must exist."""
    found, missing = [], []
    for name in [block, *blocks.neighbours(block)]:
        record = read_record(seed_object(recipe, name, "json"))
        if record is None:
            missing.append(name)
        elif record["rows"]:
            found.append((name, record))
    if missing and not pilot:
        raise SystemExit(f"{block}: seed blocks without a record: {', '.join(missing)}")
    return found, missing


def fetch_seed(recipe: dict, name: str, record: dict, cache: Path) -> Path:
    local = cache / f"{name}.parquet"
    if not (local.exists() and local.stat().st_size == record["bytes"]):
        gcs.vsi_copy(gcs.gs_to_vsi(seed_object(recipe, name, "parquet")), str(local))
    return local


def run_sprout(recipe: dict, plan: dict, work: Path, pilot: bool) -> None:
    import sprout as sproutmod

    require_batch()
    cache = work / "seed"
    cache.mkdir(parents=True, exist_ok=True)
    con = seedmod.connect(work, SHAPES["sprout"]["duckdb_memory"])
    gcs.gdal.SetCacheMax(256 * 1024 * 1024)
    order = task_blocks(plan)
    for position, block in enumerate(order):
        data = sprout_object(recipe, block, "tif", pilot)
        record_path = sprout_object(recipe, block, "json", pilot)
        if is_complete(read_record(record_path), data):
            print(f"skip {block}: already written", flush=True)
            continue
        started = time.perf_counter()
        found, missing = neighbour_seeds(recipe, block, pilot)
        # Keep the seed files this block and the next one need; drop the rest.
        keep = {name for name, _ in found}
        if position + 1 < len(order):
            keep |= {order[position + 1], *blocks.neighbours(order[position + 1])}
        for path in cache.glob("*.parquet"):
            if path.stem not in keep:
                path.unlink()
        paths = [str(fetch_seed(recipe, name, record, cache)) for name, record in found]
        fetched = time.perf_counter() - started
        bounds = blocks.bounds(block)
        features = 0
        result = {"cells": 0, "road_cells": 0, "bytes": 0}
        if paths:
            source = work / f"{block}.fgb"
            features = sproutmod.write_burn_source(con, paths, bounds, recipe, source)
            if features:
                codes = sproutmod.burn_source(source, bounds, recipe)
                local = work / f"{block}.tif"
                result = sproutmod.write_codes_cog(codes, recipe, local, work)
                codes = None
                if result["bytes"]:
                    upload(local, data)
                    local.unlink()
            source.unlink(missing_ok=True)
        pixel = recipe["grid"]["pixel_size_deg"]
        west, north, cols, rows = sproutmod.snapped(bounds, pixel, tuple(recipe["grid"]["origin"]))
        record = {
            "block": block,
            "features": features,
            **result,
            "width": cols,
            "height": rows,
            "transform": [west, pixel, 0, north, 0, -pixel],
            "seed_blocks": [name for name, _ in found],
            "seed_missing": missing,
            "fetch_seconds": round(fetched, 1),
            "seconds": round(time.perf_counter() - started, 1),
            "peak_memory_bytes": seedmod.peak_memory_bytes(),
            "written": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        write_record(record_path, record)
        print(json.dumps(record), flush=True)


# ---- collect -------------------------------------------------------------------------


def vrt_text(recipe: dict, records: list[dict]) -> str:
    """Two bands over the global grid. No nodata: a block without a COG reads 0."""
    pixel = recipe["grid"]["pixel_size_deg"]
    west, north = recipe["grid"]["origin"]
    width = round(360 / pixel)
    height = round(180 / pixel)
    srs = gcs.srs_wkt(recipe["grid"]["crs"])
    bands = []
    for index, label in enumerate(("level", "surface"), start=1):
        sources = []
        for record in records:
            if not record["bytes"]:
                continue
            xoff = round((record["transform"][0] - west) / pixel)
            yoff = round((north - record["transform"][3]) / pixel)
            sources.append(
                f"""    <SimpleSource>
      <SourceFilename relativeToVRT="1">blocks/{record['block']}.tif</SourceFilename>
      <SourceBand>{index}</SourceBand>
      <SrcRect xOff="0" yOff="0" xSize="{record['width']}" ySize="{record['height']}"/>
      <DstRect xOff="{xoff}" yOff="{yoff}" xSize="{record['width']}" ySize="{record['height']}"/>
    </SimpleSource>"""
            )
        values = json.dumps({str(k): v for k, v in recipe["sprout"]["bands"][label].items()})
        bands.append(
            f"""  <VRTRasterBand dataType="Byte" band="{index}">
    <Description>{label}</Description>
    <Metadata><MDI key="values">{gcs.xml_escape(values)}</MDI></Metadata>
{chr(10).join(sources)}
  </VRTRasterBand>"""
        )
    return f"""<VRTDataset rasterXSize="{width}" rasterYSize="{height}">
  <SRS dataAxisToSRSAxisMapping="2,1">{gcs.xml_escape(srs)}</SRS>
  <GeoTransform>{west}, {pixel}, 0, {north}, 0, {-pixel}</GeoTransform>
  <Metadata>
    <MDI key="version">{recipe['sprout']['id']}</MDI>
    <MDI key="source_release">{recipe['source']['release']}</MDI>
  </Metadata>
{chr(10).join(bands)}
</VRTDataset>
"""


def stac_documents(recipe: dict, records: list[dict]) -> tuple[dict, dict]:
    sprout_path = recipe["sprout"]["path"]
    vrt_name = f"{stem(recipe)}.vrt"
    item_name = f"{recipe['sprout']['id']}.json"
    day = f"{recipe['period']['start']}T00:00:00Z"
    bbox = [-180.0, -90.0, 180.0, 90.0]
    links = [{"rel": "root", "href": "./collection.json", "type": "application/json"}]
    classes = {
        label: [{"value": int(k), "name": v} for k, v in recipe["sprout"]["bands"][label].items()]
        for label in ("level", "surface")
    }
    collection = {
        "stac_version": "1.0.0",
        "type": "Collection",
        "id": recipe["sprout"]["id"],
        "title": recipe["title"],
        "description": (
            f"Generated from cookbook/recipes/{recipe['id']}.yaml. "
            f"{recipe['source']['attribution']}. Licence {recipe['source']['licence']}."
        ),
        "license": recipe["source"]["licence"],
        "extent": {"spatial": {"bbox": [bbox]}, "temporal": {"interval": [[day, day]]}},
        "links": [
            *links,
            {"rel": "self", "href": "./collection.json", "type": "application/json"},
            {"rel": "item", "href": f"./{item_name}", "type": "application/json"},
        ],
    }
    item = {
        "stac_version": "1.0.0",
        "type": "Feature",
        "stac_extensions": [
            "https://stac-extensions.github.io/raster/v1.1.0/schema.json",
            "https://stac-extensions.github.io/classification/v1.1.0/schema.json",
        ],
        "id": recipe["sprout"]["id"],
        "collection": recipe["sprout"]["id"],
        "bbox": bbox,
        "geometry": {"type": "Polygon", "coordinates": [[[-180, -90], [180, -90], [180, 90], [-180, 90], [-180, -90]]]},
        "properties": {"datetime": day, "title": recipe["title"]},
        "assets": {
            "data": {
                "href": gcs.gs_join(sprout_path, vrt_name),
                "type": gcs.VRT_TYPE,
                "roles": ["data"],
                "title": recipe["title"],
                "raster:bands": [
                    {
                        "name": label,
                        "data_type": recipe["grid"]["dtype"],
                        "spatial_resolution": float(recipe["grid"]["pixel_size_deg"]),
                        "classification:classes": classes[label],
                    }
                    for label in ("level", "surface")
                ],
            },
            **{
                f"block-{r['block']}": {
                    "href": sprout_object(recipe, r["block"], "tif"),
                    "type": gcs.COG_TYPE,
                    "roles": ["data"],
                }
                for r in records
                if r["bytes"]
            },
        },
        "links": [
            *links,
            {"rel": "collection", "href": "./collection.json", "type": "application/json"},
            {"rel": "self", "href": f"./{item_name}", "type": "application/json"},
        ],
    }
    return collection, item


def fetch_records(prefix: str, dest: Path, key: str = "block") -> dict[str, dict]:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    listing = subprocess.run(
        ["gcloud", "storage", "ls", gcs.gs_join(prefix, "*.json"), f"--project={gcs.PROJECT}"], text=True, capture_output=True
    )
    if listing.returncode == 0 and listing.stdout.strip():
        gcs.gcloud(["storage", "cp", gcs.gs_join(prefix, "*.json"), str(dest)])
    out = {}
    for path in sorted(dest.glob("*.json")):
        record = json.loads(path.read_text())
        if key in record:
            out[record[key]] = record
    return out


def command_collect(recipe: dict) -> None:
    work = ROOT / "data" / "roads" / "collect"
    records = fetch_records(gcs.gs_join(recipe["sprout"]["path"], "blocks/"), work / "records")
    missing = [b for b in blocks.all_blocks() if b not in records]
    if missing:
        raise SystemExit(f"{len(missing)} sprout blocks have no record, for example {missing[0]}")
    ordered = [records[b] for b in blocks.all_blocks()]
    out = work / "collection"
    out.mkdir(parents=True, exist_ok=True)
    vrt = out / f"{stem(recipe)}.vrt"
    vrt.write_text(vrt_text(recipe, ordered))
    collection, item = stac_documents(recipe, ordered)
    (out / "collection.json").write_text(json.dumps(collection, indent=2) + "\n")
    (out / f"{recipe['sprout']['id']}.json").write_text(json.dumps(item, indent=2) + "\n")
    for path in (vrt, out / "collection.json", out / f"{recipe['sprout']['id']}.json"):
        gcs.upload_with_gcloud(path, gcs.gs_join(recipe["sprout"]["path"], path.name))
    print(json.dumps(totals(ordered)))


def totals(records) -> dict:
    records = list(records)
    out = {"blocks": len(records), "with_data": sum(1 for r in records if r.get("bytes"))}
    for key in (
        "rows", "features", "road_cells", "level_1", "level_2", "level_3", "level_4", "paved", "unpaved", "tiles", "bytes",
        "seconds", "wall_seconds", "fetch_seconds",
    ):
        if any(key in r for r in records):
            out[key] = round(sum(r.get(key, 0) for r in records), 1)
    out["peak_memory_bytes_max"] = max((r.get("peak_memory_bytes", 0) for r in records), default=0)
    return out


# ---- submit --------------------------------------------------------------------------


def tile_plan(recipe: dict, pilot: str | None) -> dict:
    """Shards over the sprout blocks that have a COG. A pilot plan holds one shard; it writes where the full run would."""
    import tiles as roadtiles

    records = fetch_records(gcs.gs_join(recipe["sprout"]["path"], "blocks/"), ROOT / "data" / "roads" / "collect" / "sprout-records")
    missing = [b for b in blocks.all_blocks() if b not in records]
    if missing:
        raise SystemExit(f"{len(missing)} sprout blocks have no record, for example {missing[0]}")
    shards = roadtiles.shard_plan(sorted(b for b, r in records.items() if r["bytes"]))
    if pilot:
        shards = [s for s in shards if s["id"] == pilot]
        if not shards:
            raise SystemExit(f"no shard {pilot}")
    return {"kind": "tiles", "shards": shards, "pilot": bool(pilot)}


def make_plan(recipe: dict, kind: str, pilot: str | None) -> dict:
    if kind in ("tiles", "merge"):
        if kind == "merge" and pilot:
            raise SystemExit("the merge has no pilot")
        return tile_plan(recipe, pilot)
    if pilot:
        blocks.corner(pilot)
        return {"kind": kind, "groups": [[pilot]], "pilot": True}
    if kind == "seed":
        weights = blocks.footer_weights(json.loads((seedmod.DATA / "footers.json").read_text()))
        minimum = 20_000.0  # a block with no footer still lists the footers and runs a query
    else:
        seeds = fetch_records(recipe["seed"]["path"], ROOT / "data" / "roads" / "collect" / "seed-records")
        missing = [b for b in blocks.all_blocks() if b not in seeds]
        if missing:
            raise SystemExit(f"{len(missing)} seed blocks have no record, for example {missing[0]}: run the seed job first")
        # A sprout block reads its neighbours too, and writes 1.6 billion cells even when sparse.
        weights = {b: sum(seeds[n]["rows"] for n in [b, *blocks.neighbours(b)]) for b in blocks.all_blocks()}
        minimum = 50_000.0
    return {"kind": kind, "groups": blocks.groups(weights, TASKS, minimum), "pilot": False}


def batch_job(prefix: str, kind: str, tasks: int, wheel: str, pilot: bool, plan_name: str = "plan.json") -> dict:
    shape = SHAPES[kind]
    if kind == "tiles":
        command = f"python3 /tmp/in/roads/tiles.py shard --recipe /tmp/in/recipe.json --plan /tmp/in/{plan_name} --work /tmp/work"
    elif kind == "merge":
        command = (
            f"python3 /tmp/in/roads/tiles.py merge --recipe /tmp/in/recipe.json --plan /tmp/in/{plan_name} --work /tmp/work"
            " --verify-samples 16"
        )
    else:
        command = (
            f"python3 /tmp/in/roads/batch.py run-{kind} --recipe /tmp/in/recipe.json --plan /tmp/in/{plan_name} --work /tmp/work"
            + (" --pilot-run" if pilot else "")
        )
    code_files = TILE_CODE_FILES if kind in ("tiles", "merge") else CODE_FILES
    parallelism = TILE_PARALLELISM if kind == "tiles" else TASKS
    script = shards.bootstrap(prefix, wheel, code_files, plan_name) + command + "\n"
    return shards.job_spec(shape, script, tasks, parallelism, gcs.BATCH_ENV)


def duckdb_wheel(work: Path) -> Path:
    found = list(work.glob(f"duckdb-{DUCKDB_VERSION}-*.whl"))
    if not found:
        subprocess.run(
            [
                sys.executable, "-m", "pip", "download", f"duckdb=={DUCKDB_VERSION}", "--no-deps", "--only-binary=:all:",
                "--python-version", CONTAINER_PYTHON, "--platform", "manylinux_2_27_x86_64",
                "--platform", "manylinux2014_x86_64", "-d", str(work),
            ],
            check=True,
        )
        found = list(work.glob(f"duckdb-{DUCKDB_VERSION}-*.whl"))
    return found[0]


def job_name(recipe: dict, kind: str, pilot: str | None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    label = f"-pilot-{pilot.lower().replace('_', '-')}" if pilot else ""
    return f"{recipe['id']}-{kind}{label}-{stamp}"


def command_submit(recipe_path: Path, kind: str, pilot: str | None, approved: bool) -> None:
    recipe = seedmod.load_recipe(recipe_path)
    plan = make_plan(recipe, kind, pilot)
    work = ROOT / "data" / "roads" / "batch"
    work.mkdir(parents=True, exist_ok=True)
    tiled = kind in ("tiles", "merge")
    wheel = shards.pmtiles_wheel(work) if tiled else duckdb_wheel(work)
    prefix = scratch(recipe)
    name = job_name(recipe, kind, pilot)
    plan_name = f"{name}.plan.json" if tiled else "plan.json"
    tasks = 1 if kind == "merge" else len(plan["shards"]) if tiled else len(plan["groups"])
    config = batch_job(prefix, kind, tasks, wheel.name, bool(pilot), plan_name)
    (work / "recipe.json").write_text(json.dumps(recipe, default=str) + "\n")
    (work / plan_name).write_text(json.dumps(plan) + "\n")
    config_path = work / f"{name}.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    shape = SHAPES[kind]
    if tiled:
        print(f"job {name}: {tasks} task(s), {len(plan['shards'])} shards, spot {shape['machine']}, parallelism {config['taskGroups'][0]['parallelism']}")
    else:
        sizes = [len(g) for g in plan["groups"]]
        print(f"job {name}: {len(sizes)} task(s), {sum(sizes)} blocks, {min(sizes)}–{max(sizes)} a task, spot {shape['machine']}")
    print(f"config {config_path}")
    print(f"status: gcloud batch jobs describe {name} --location={gcs.REGION} --project={gcs.PROJECT}")
    print(f"tasks:  gcloud batch tasks list --job={name} --location={gcs.REGION} --project={gcs.PROJECT}")
    print(f"cancel: gcloud batch jobs delete {name} --location={gcs.REGION} --project={gcs.PROJECT}")
    if kind == "seed":
        print(f"output: {recipe['seed']['path']}")
    elif kind == "tiles":
        print(f"output: {gcs.gs_join(scratch(recipe), 'tiles/shards/')}")
    elif kind == "merge":
        print(f"output: {recipe['tiles']['path']}")
    else:
        print(f"output: {gcs.gs_join(scratch(recipe), 'pilot/') if pilot else gcs.gs_join(recipe['sprout']['path'], 'blocks/')}")
    if not approved:
        print("not submitted: pass --approved once the cost is approved")
        return
    for relative in TILE_CODE_FILES if tiled else CODE_FILES:
        gcs.upload_with_gcloud(ROOT / relative, prefix + "code/" + relative)
    for path in (work / "recipe.json", work / plan_name, wheel):
        gcs.upload_with_gcloud(path, prefix + "code/" + path.name)
    gcs.gcloud(["services", "enable", "batch.googleapis.com"])
    gcs.gcloud(["batch", "jobs", "submit", name, f"--location={gcs.REGION}", f"--config={config_path}"])
    print(f"submitted {name}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("submit", "run-seed", "run-sprout", "collect", "records"))
    parser.add_argument("kind", nargs="?", choices=("seed", "sprout", "tiles", "merge"))
    parser.add_argument("--recipe", type=Path, default=seedmod.DEFAULT_RECIPE)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--work", type=Path, default=Path("/tmp/work"))
    parser.add_argument("--pilot", help="one block, for example 30S_020E, or one tiles shard, for example 5-18-17")
    parser.add_argument("--pilot-run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args()
    recipe = seedmod.load_recipe(args.recipe)
    if args.command == "submit":
        if not args.kind:
            parser.error("submit needs seed, sprout, tiles or merge")
        command_submit(args.recipe, args.kind, args.pilot, args.approved)
    elif args.command == "run-seed":
        run_seed(recipe, json.loads(args.plan.read_text()), args.work)
    elif args.command == "run-sprout":
        run_sprout(recipe, json.loads(args.plan.read_text()), args.work, args.pilot_run)
    elif args.command == "collect":
        command_collect(recipe)
    else:
        if args.kind == "tiles":
            prefix, key = gcs.gs_join(scratch(recipe), "tiles/shards/"), "shard"
        else:
            prefix, key = (recipe["seed"]["path"] if args.kind == "seed" else gcs.gs_join(recipe["sprout"]["path"], "blocks/")), "block"
        records = fetch_records(prefix, ROOT / "data" / "roads" / "collect" / f"{args.kind}-records", key)
        print(json.dumps(totals(records.values())))


if __name__ == "__main__":
    main()
