#!/usr/bin/env python3
"""The Buildings 2023 sprout: Google Temporal where it has data, otherwise TEMPO after the seed rule.

The recipe is cookbook/recipes/buildings-2023-v1.yaml. One COG per 10 degree block on the sprout grid,
with two bands: share (uint8, value / 250, nodata 255) and source (0 none, 1 Google, 2 TEMPO).
Each block writes its COG, then a JSON record, so a rerun skips finished blocks.

  python sprout/buildings.py plan                              blocks and runs, from the seed summaries
  python sprout/buildings.py submit --pilot E000N20,E000N40    print the pilot job, submit nothing
  python sprout/buildings.py submit --pilot E000N20,E000N40 --approved
  python sprout/buildings.py submit --approved                 every block
  python sprout/buildings.py records [--pilot]                 totals from the records written so far
  python sprout/buildings.py collect                           the VRT and STAC files, after every block has a record
  python sprout/buildings.py check [--pilot]                   the comparison boxes against buildings/README.md

A pilot writes to the scratch prefix. The full run writes to sprout.path/blocks/.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import resource
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import cropland as gcs  # noqa: E402  GCS helpers, project, region, image, BATCH_ENV
from cropland import gdal  # noqa: E402

DEFAULT_RECIPE = ROOT / "cookbook" / "recipes" / "buildings-2023-v1.yaml"
GOOGLE_BLOCKS = ROOT / "gee" / "buildings" / "google-2023-blocks.json"
WORK = ROOT / "data" / "buildings" / "sprout"
CODE_FILES = ("sprout/buildings.py", "sprout/cropland.py")
TASKS = 24
PARALLEL = 12  # 24 E2_CPUS in europe-west1, two a machine
BLOCK_DEG = 10
STRIP = 1024  # rows per strip, a multiple of the 512 px COG block
HALF = 20037508.342789244  # Web Mercator half width, metres
MAX_LAT = math.degrees(math.atan(math.sinh(math.pi)))
SHAPE = {
    "machine": "e2-standard-2",
    "cpu_milli": "2000",
    "memory_mib": "7000",
    "disk_gb": 50,
    "disk_type": "pd-standard",  # pd-balanced counts against the region's 500 GB SSD quota
    "max_run_seconds": 8 * 3600,
    "max_retries": 3,
}


# --------------------------------------------------------------------------
# Pure helpers: no network. These are what the tests cover.
# --------------------------------------------------------------------------


def load_recipe(path: Path) -> dict:
    text = Path(path).read_text()
    if Path(path).suffix == ".json":
        return json.loads(text)
    import yaml

    return yaml.safe_load(text)


def block_name(west: int, south: int) -> str:
    """W010N00 for 10 W to 0, 0 to 10 N: the Google seed's names."""
    return f"{'E' if west >= 0 else 'W'}{abs(west):03d}{'N' if south >= 0 else 'S'}{abs(south):02d}"


def block_corner(name: str) -> tuple[int, int]:
    """West, south of a block name."""
    if len(name) != 7 or name[0] not in "EW" or name[4] not in "NS":
        raise ValueError(f"not a block name: {name}")
    west = int(name[1:4]) * (1 if name[0] == "E" else -1)
    south = int(name[5:7]) * (1 if name[4] == "N" else -1)
    if west % BLOCK_DEG or south % BLOCK_DEG or not -180 <= west < 180 or not -90 <= south < 90:
        raise ValueError(f"not a block name: {name}")
    return west, south


def all_blocks() -> list[str]:
    """Every block, north to south, then west to east."""
    return [block_name(w, s) for s in range(80, -100, -BLOCK_DEG) for w in range(-180, 180, BLOCK_DEG)]


def block_grid(name: str, recipe: dict) -> dict:
    west, south = block_corner(name)
    cell = recipe["grid"]["pixel_size_deg"]
    side = round(BLOCK_DEG / cell)
    return {"west": float(west), "north": float(south + BLOCK_DEG), "cell": cell, "cols": side, "rows": side}


def mosaic_lonlat(x: int, y: int, zoom: int) -> tuple[float, float, float, float]:
    """West, south, east, north in degrees of XYZ tile x, y."""
    n = 2**zoom

    def lat(row: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row / n))))

    return x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y)


def parse_mosaic(name: str) -> tuple[int, int]:
    """x, y of z4-<x>-<y>.tif."""
    _, x, y = name.removesuffix(".tif").split("-")
    return int(x), int(y)


def mosaics_meeting(bounds: tuple[float, float, float, float], mosaics: list[str], zoom: int) -> list[str]:
    """The mosaics whose extent overlaps west, south, east, north (degrees)."""
    west, south, east, north = bounds
    out = []
    for name in mosaics:
        mw, ms, me, mn = mosaic_lonlat(*parse_mosaic(name), zoom)
        if mw < east and me > west and ms < north and mn > south:
            out.append(name)
    return out


def plan_blocks(google: list[str], mosaics: list[str], zoom: int, recipe: dict) -> list[str]:
    """Blocks that meet a Google block or a TEMPO mosaic, in all_blocks order."""
    wanted = set(google)
    for name in all_blocks():
        west, south = block_corner(name)
        if mosaics_meeting((west, south, west + BLOCK_DEG, south + BLOCK_DEG), mosaics, zoom):
            wanted.add(name)
    return [b for b in all_blocks() if b in wanted]


def groups(names: list[str], weights: dict[str, float], count: int) -> list[list[str]]:
    """Split names, in order, into at most `count` runs of similar total weight."""
    total = sum(weights[n] for n in names)
    target = total / count
    out: list[list[str]] = [[]]
    running = 0.0
    for name in names:
        w = weights[name]
        if out[-1] and running + w / 2 > target * len(out) and len(out) < count:
            out.append([])
        out[-1].append(name)
        running += w
    return out


def seed_radius_cells(recipe: dict) -> int:
    """72 cells either side for 2 km, as buildings/compare.py tempo_kept counts it (27.8 m a cell)."""
    return int(round(recipe["sprout"]["tempo"]["seed_radius_m"] / 27.8))


def dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """True where a True cell lies within `radius` cells either side, in a square window.

    Cells outside the array count as False, like scipy's maximum_filter with mode="constant".
    """
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


def merge(google: np.ndarray | None, tempo: np.ndarray, recipe: dict, margin: int) -> dict:
    """Share and source for the centre of a strip.

    tempo is float32 TEMPO band 1 on the sprout grid, NaN where TEMPO has no tile, with `margin` cells
    on every side so seeds beyond the strip count. google is the uint8 Google seed for the centre
    only, or None outside Google's blocks.
    """
    sprout = recipe["sprout"]
    scale = sprout["bands"]["share"]["scale"]
    nodata = sprout["bands"]["share"]["nodata"]
    gnodata = recipe["seeds"]["google"]["nodata"]
    src = sprout["bands"]["source"]["values"]
    t = np.nan_to_num(tempo, nan=0.0)
    seeds = dilate(t >= sprout["tempo"]["seed_share"], seed_radius_cells(recipe))
    rows, cols = tempo.shape[0] - 2 * margin, tempo.shape[1] - 2 * margin
    inner = (slice(margin, margin + rows), slice(margin, margin + cols))
    t_valid = ~np.isnan(tempo[inner])
    t_share = np.clip(np.rint(t[inner] * scale), 0, scale).astype(np.uint8)
    kept = seeds[inner]
    t_out = np.where(kept, t_share, 0).astype(np.uint8)
    g_valid = np.zeros((rows, cols), bool) if google is None else google != gnodata
    share = np.full((rows, cols), nodata, np.uint8)
    source = np.full((rows, cols), src["none"], np.uint8)
    use_t = t_valid & ~g_valid
    share[use_t] = t_out[use_t]
    source[use_t] = src["tempo"]
    if google is not None:
        share[g_valid] = google[g_valid]
        source[g_valid] = src["google"]
    counts = {
        "google_cells": int(g_valid.sum()),
        "tempo_cells": int(use_t.sum()),
        "google_built": int((g_valid & (share > 0)).sum()),
        "tempo_built": int((use_t & (share > 0)).sum()),
        "tempo_dropped": int((use_t & (t_share > 0) & ~kept).sum()),
        "google_share_sum": int(share[g_valid].sum(dtype=np.int64)),
        "tempo_share_sum": int(share[use_t].sum(dtype=np.int64)),
    }
    return {"share": share, "source": source, "counts": counts}


def add_counts(total: dict, part: dict) -> dict:
    for key, value in part.items():
        total[key] = total.get(key, 0) + value
    return total


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def scratch(recipe: dict) -> str:
    return f"gs://nature-watch-bucket/scratch/sprout/{recipe['sprout']['id']}/"


def block_object(recipe: dict, block: str, suffix: str, pilot: bool = False) -> str:
    prefix = gcs.gs_join(scratch(recipe), "pilot/") if pilot else gcs.gs_join(recipe["sprout"]["path"], "blocks/")
    return gcs.gs_join(prefix, f"{block}.{suffix}")


# --------------------------------------------------------------------------
# Batch task
# --------------------------------------------------------------------------


def peak_memory_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


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
    stat = gdal.VSIStatL(gcs.gs_to_vsi(data_path))
    return stat is not None and stat.size == record["bytes"]


def tempo_strip(sources: list[str], west: float, north: float, cell: float, cols: int, rows: int):
    """TEMPO band 1 averaged onto the sprout grid, as buildings/compare.py tempo_box. NaN where no tile."""
    if not sources:
        return np.full((rows, cols), np.nan, np.float32)
    opts = gdal.WarpOptions(
        format="MEM",
        dstSRS="EPSG:4326",
        outputBounds=(west, north - rows * cell, west + cols * cell, north),
        width=cols,
        height=rows,
        resampleAlg="average",
        srcBands=[1],
        srcNodata=-1.0,
        dstNodata=np.nan,
        outputType=gdal.GDT_Float32,
        multithread=True,
        warpMemoryLimit=256,
    )
    ds = gdal.Warp("", sources, options=opts)
    return ds.GetRasterBand(1).ReadAsArray()


def tempo_sources(recipe: dict, mosaics: list[str], bounds) -> list[str]:
    tempo = recipe["seeds"]["tempo"]
    west, south, east, north = bounds
    south, north = max(south, -MAX_LAT), min(north, MAX_LAT)
    if south >= north:
        return []
    names = mosaics_meeting((west, south, east, north), mosaics, tempo["mosaic_zoom"])
    return [gcs.gs_to_vsi(gcs.gs_join(tempo["path"], n)) for n in names]


def build_block(recipe: dict, block: str, mosaics: list[str], google: bool, work: Path) -> dict:
    """Write work/<block>.tif. Returns the counts and the COG path, or None for an empty block."""
    grid = block_grid(block, recipe)
    cell, cols, rows = grid["cell"], grid["cols"], grid["rows"]
    margin = seed_radius_cells(recipe)
    west, north = grid["west"], grid["north"]
    m = margin * cell
    sources = tempo_sources(recipe, mosaics, (west - m, north - BLOCK_DEG - m, west + BLOCK_DEG + m, north + m))
    gds = None
    if google:
        gpath = gcs.gs_to_vsi(gcs.gs_join(recipe["seeds"]["google"]["path"], f"{block}.tif"))
        gds = gdal.Open(gpath)
    nodata = recipe["sprout"]["bands"]["share"]["nodata"]
    raw = work / f"{block}.raw.tif"
    out = gdal.GetDriverByName("GTiff").Create(
        str(raw), cols, rows, 2, gdal.GDT_Byte, ["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512", "COMPRESS=DEFLATE", "ZLEVEL=1"]
    )
    out.SetGeoTransform((west, cell, 0.0, north, 0.0, -cell))
    out.SetProjection(gcs.srs_wkt(recipe["grid"]["crs"]))
    counts: dict = {}
    for r0 in range(0, rows, STRIP):
        n = min(STRIP, rows - r0)
        top = north - r0 * cell
        t = tempo_strip(sources, west - m, top + m, cell, cols + 2 * margin, n + 2 * margin)
        g = gds.GetRasterBand(1).ReadAsArray(0, r0, cols, n) if gds is not None else None
        result = merge(g, t, recipe, margin)
        out.GetRasterBand(1).WriteArray(result["share"], 0, r0)
        out.GetRasterBand(2).WriteArray(result["source"], 0, r0)
        add_counts(counts, result["counts"])
        print(f"{block} rows {r0 + n}/{rows}", flush=True)
    for index, label in ((1, "share"), (2, "source")):
        out.GetRasterBand(index).SetDescription(label)
        out.GetRasterBand(index).SetNoDataValue(nodata)
    out.SetMetadata({"version": recipe["sprout"]["id"], "share_scale": str(recipe["sprout"]["bands"]["share"]["scale"])})
    out = None
    gds = None
    counts["tempo_mosaics"] = len(sources)
    if counts.get("google_cells", 0) + counts.get("tempo_cells", 0) == 0:
        raw.unlink()
        return {"counts": counts, "cog": None}
    comp = recipe["sprout"]["compression"]
    cog = work / f"{block}.tif"
    gdal.Translate(
        str(cog),
        str(raw),
        options=gdal.TranslateOptions(
            format="COG",
            creationOptions=[
                f"COMPRESS={comp['method'].upper()}",
                f"LEVEL={comp['level']}",
                "PREDICTOR=NO",
                f"BLOCKSIZE={comp['blocksize']}",
                "OVERVIEWS=NONE",
                "NUM_THREADS=2",
            ],
        ),
    )
    raw.unlink()
    return {"counts": counts, "cog": cog}


def upload(local: Path, dest: str) -> None:
    gcs.vsi_copy(str(local), gcs.gs_to_vsi(dest))
    stat = gdal.VSIStatL(gcs.gs_to_vsi(dest))
    if stat is None or stat.size != local.stat().st_size:
        raise OSError(f"{dest} is not {local.stat().st_size} bytes after upload")


def run_task(recipe: dict, plan: dict, work: Path) -> None:
    if os.environ.get(gcs.BATCH_ENV) != "1":
        raise SystemExit("This runs as a Batch task in europe-west1. From this computer: python sprout/buildings.py submit ...")
    work.mkdir(parents=True, exist_ok=True)
    gdal.SetCacheMax(256 * 1024 * 1024)
    pilot = plan["pilot"]
    google = set(plan["google"])
    index = int(os.environ.get("BATCH_TASK_INDEX", "0"))
    for block in plan["groups"][index]:
        data = block_object(recipe, block, "tif", pilot)
        record_path = block_object(recipe, block, "json", pilot)
        if is_complete(read_record(record_path), data):
            print(f"skip {block}: already written", flush=True)
            continue
        started = time.perf_counter()
        built = build_block(recipe, block, plan["mosaics"], block in google, work)
        size = 0
        if built["cog"] is not None:
            size = built["cog"].stat().st_size
            upload(built["cog"], data)
            built["cog"].unlink()
        grid = block_grid(block, recipe)
        record = {
            "block": block,
            "bytes": size,
            "width": grid["cols"],
            "height": grid["rows"],
            "transform": [grid["west"], grid["cell"], 0, grid["north"], 0, -grid["cell"]],
            "google_seed": block in google,
            **built["counts"],
            "seconds": round(time.perf_counter() - started, 1),
            "peak_memory_bytes": peak_memory_bytes(),
            "written": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        gcs.vsi_write_text(gcs.gs_to_vsi(record_path), json.dumps(record, indent=1) + "\n")
        print(json.dumps(record), flush=True)


# --------------------------------------------------------------------------
# Laptop: plan, submit, records, collect, check
# --------------------------------------------------------------------------


def seed_summary(recipe: dict, which: str) -> dict:
    out = subprocess.run(
        ["gcloud", "storage", "cat", gcs.gs_join(recipe["seeds"][which]["path"], "summary.json"), f"--project={gcs.PROJECT}"],
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(out.stdout)


def make_plan(recipe: dict, pilot: list[str] | None) -> dict:
    google = [block_name(w, s) for w, s in json.loads(GOOGLE_BLOCKS.read_text())["blocks"]]
    mosaics = sorted(seed_summary(recipe, "tempo")["per_mosaic"])
    if pilot:
        for name in pilot:
            block_corner(name)
        return {"pilot": True, "groups": [pilot], "google": google, "mosaics": mosaics}
    names = plan_blocks(google, mosaics, recipe["seeds"]["tempo"]["mosaic_zoom"], recipe)
    # Every block warps 1.6 billion cells; a Google block also reads its seed.
    weights = {n: 1.0 + (0.3 if n in google else 0.0) for n in names}
    return {"pilot": False, "groups": groups(names, weights, TASKS), "google": google, "mosaics": mosaics}


def bootstrap(prefix: str) -> str:
    bucket_path = prefix.removeprefix("gs://").rstrip("/")
    names = [*CODE_FILES, "recipe.json", "plan.json"]
    downloads = "\n".join(f'download("{n}", folder / "{n}")' for n in names)
    return f"""python3 - << 'PY'
import json
import urllib.request
from pathlib import Path
request = urllib.request.Request(
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
    headers={{"Metadata-Flavor": "Google"}},
)
token = json.load(urllib.request.urlopen(request))["access_token"]

def download(name, dest):
    url = "https://storage.googleapis.com/{bucket_path}/code/" + name
    body = urllib.request.Request(url, headers={{"Authorization": "Bearer " + token}})
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(urllib.request.urlopen(body).read())

folder = Path("/tmp/in")
{downloads}
PY
python3 -c "import numpy" 2>/dev/null || (apt-get update -qq && apt-get install -y -qq python3-numpy)
"""


def batch_job(prefix: str, tasks: int) -> dict:
    command = "python3 /tmp/in/sprout/buildings.py run --recipe /tmp/in/recipe.json --plan /tmp/in/plan.json --work /tmp/work"
    spec = {
        "maxRunDuration": f"{SHAPE['max_run_seconds']}s",
        "maxRetryCount": SHAPE["max_retries"],
        "lifecyclePolicies": [{"action": "RETRY_TASK", "actionCondition": {"exitCodes": [50001]}}],
        "computeResource": {"cpuMilli": SHAPE["cpu_milli"], "memoryMib": SHAPE["memory_mib"]},
        "environment": {"variables": {"CPL_MACHINE_IS_GCE": "YES", gcs.BATCH_ENV: "1", "GDAL_CACHEMAX": "256"}},
        "runnables": [
            {"container": {"imageUri": gcs.IMAGE, "entrypoint": "sh", "commands": ["-c", bootstrap(prefix) + command + "\n"]}}
        ],
    }
    return {
        "taskGroups": [{"taskCount": str(tasks), "parallelism": str(min(tasks, PARALLEL)), "taskSpec": spec}],
        "allocationPolicy": {
            "instances": [
                {
                    "policy": {
                        "machineType": SHAPE["machine"],
                        "provisioningModel": "SPOT",
                        "bootDisk": {"sizeGb": SHAPE["disk_gb"], "type": SHAPE["disk_type"]},
                    }
                }
            ],
            "location": {"allowedLocations": [f"regions/{gcs.REGION}"]},
            "serviceAccount": {"email": gcs.SERVICE_ACCOUNT},
        },
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


def job_name(recipe: dict, pilot: bool) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    return f"sprout-{recipe['sprout']['id']}{'-pilot' if pilot else ''}-{stamp}"


def command_plan(recipe: dict) -> dict:
    plan = make_plan(recipe, None)
    sizes = [len(g) for g in plan["groups"]]
    names = [b for g in plan["groups"] for b in g]
    google = set(plan["google"])
    print(
        f"{len(names)} blocks ({sum(1 for n in names if n in google)} with a Google seed, "
        f"{sum(1 for n in names if n not in google)} TEMPO only), {len(sizes)} tasks of {min(sizes)}–{max(sizes)} blocks"
    )
    return plan


def command_submit(recipe_path: Path, pilot: list[str] | None, approved: bool) -> None:
    recipe = load_recipe(recipe_path)
    plan = make_plan(recipe, pilot)
    WORK.mkdir(parents=True, exist_ok=True)
    prefix = scratch(recipe)
    name = job_name(recipe, bool(pilot))
    config = batch_job(prefix, len(plan["groups"]))
    (WORK / "recipe.json").write_text(json.dumps(recipe, default=str) + "\n")
    (WORK / "plan.json").write_text(json.dumps(plan) + "\n")
    config_path = WORK / f"{name}.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    sizes = [len(g) for g in plan["groups"]]
    print(f"job {name}: {len(sizes)} task(s), {sum(sizes)} blocks, {min(sizes)}–{max(sizes)} a task, spot {SHAPE['machine']}")
    print(f"config {config_path}")
    print(f"status: gcloud batch jobs describe {name} --location={gcs.REGION} --project={gcs.PROJECT}")
    print(f"tasks:  gcloud batch tasks list --job={name} --location={gcs.REGION} --project={gcs.PROJECT}")
    print(f"cancel: gcloud batch jobs delete {name} --location={gcs.REGION} --project={gcs.PROJECT}")
    print(f"output: {gcs.gs_join(prefix, 'pilot/') if pilot else gcs.gs_join(recipe['sprout']['path'], 'blocks/')}")
    if not approved:
        print("not submitted: pass --approved once the cost is approved")
        return
    for relative in CODE_FILES:
        gcs.upload_with_gcloud(ROOT / relative, prefix + "code/" + relative)
    for path in (WORK / "recipe.json", WORK / "plan.json"):
        gcs.upload_with_gcloud(path, prefix + "code/" + path.name)
    gcs.gcloud(["batch", "jobs", "submit", name, f"--location={gcs.REGION}", f"--config={config_path}"])
    print(f"submitted {name}", flush=True)


def fetch_records(prefix: str, dest: Path) -> dict[str, dict]:
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
        if "block" in record:
            out[record["block"]] = record
    return out


def totals(records) -> dict:
    records = list(records)
    out = {"blocks": len(records), "with_data": sum(1 for r in records if r.get("bytes"))}
    keys = ("bytes", "google_cells", "tempo_cells", "google_built", "tempo_built", "tempo_dropped",
            "google_share_sum", "tempo_share_sum", "seconds")
    for key in keys:
        out[key] = round(sum(r.get(key, 0) for r in records), 1)
    out["seconds_max"] = max((r.get("seconds", 0) for r in records), default=0)
    out["peak_memory_bytes_max"] = max((r.get("peak_memory_bytes", 0) for r in records), default=0)
    return out


def vrt_text(recipe: dict, records: list[dict]) -> str:
    """Two bands over the global grid. A block without a COG reads share 255 and source 0."""
    pixel = recipe["grid"]["pixel_size_deg"]
    west, north = recipe["grid"]["origin"]
    width, height = round(360 / pixel), round(180 / pixel)
    srs = gcs.srs_wkt(recipe["grid"]["crs"])
    bands_cfg = recipe["sprout"]["bands"]
    bands = []
    for index, label in enumerate(("share", "source"), start=1):
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
        if label == "share":
            extra = f"    <NoDataValue>{bands_cfg['share']['nodata']}</NoDataValue>\n"
            meta = {"scale": bands_cfg["share"]["scale"], "nodata": bands_cfg["share"]["nodata"]}
        else:
            extra = ""
            meta = {str(v): k for k, v in bands_cfg["source"]["values"].items()}
        bands.append(
            f"""  <VRTRasterBand dataType="Byte" band="{index}">
    <Description>{label}</Description>
{extra}    <Metadata><MDI key="values">{gcs.xml_escape(json.dumps(meta))}</MDI></Metadata>
{chr(10).join(sources)}
  </VRTRasterBand>"""
        )
    return f"""<VRTDataset rasterXSize="{width}" rasterYSize="{height}">
  <SRS dataAxisToSRSAxisMapping="2,1">{gcs.xml_escape(srs)}</SRS>
  <GeoTransform>{west}, {pixel}, 0, {north}, 0, {-pixel}</GeoTransform>
  <Metadata>
    <MDI key="version">{recipe['sprout']['id']}</MDI>
  </Metadata>
{chr(10).join(bands)}
</VRTDataset>
"""


def stac_documents(recipe: dict, records: list[dict]) -> tuple[dict, dict]:
    sprout_path = recipe["sprout"]["path"]
    vrt_name = recipe["sprout"]["vrt"]
    item_name = f"{recipe['sprout']['id']}.json"
    day = f"{recipe['year']}-06-30T00:00:00Z"
    bbox = [-180.0, -90.0, 180.0, 90.0]
    links = [{"rel": "root", "href": "./collection.json", "type": "application/json"}]
    google, tempo = recipe["sources"]["google"], recipe["sources"]["tempo"]
    credits = (
        f"{google['name']} 2023 ({google['licence']}) where Google has data, "
        f"otherwise {tempo['name']} {tempo['quarter']} ({tempo['licence']})"
    )
    source_classes = [{"value": v, "name": k} for k, v in recipe["sprout"]["bands"]["source"]["values"].items()]
    collection = {
        "stac_version": "1.0.0",
        "type": "Collection",
        "id": recipe["sprout"]["id"],
        "title": recipe["title"],
        "description": f"Generated from cookbook/recipes/{recipe['id']}.yaml. Building share: {credits}.",
        "license": "various",
        "extent": {"spatial": {"bbox": [bbox]}, "temporal": {"interval": [[day, day]]}},
        "links": [
            *links,
            {"rel": "self", "href": "./collection.json", "type": "application/json"},
            {"rel": "item", "href": f"./{item_name}", "type": "application/json"},
            {"rel": "license", "href": google["url"], "title": google["licence"]},
            {"rel": "license", "href": tempo["url"], "title": tempo["licence"]},
        ],
    }
    share = recipe["sprout"]["bands"]["share"]
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
                        "name": "share",
                        "data_type": "uint8",
                        "nodata": share["nodata"],
                        "scale": 1 / share["scale"],
                        "offset": 0,
                        "spatial_resolution": float(recipe["grid"]["pixel_size_deg"]),
                    },
                    {
                        "name": "source",
                        "data_type": "uint8",
                        "spatial_resolution": float(recipe["grid"]["pixel_size_deg"]),
                        "classification:classes": source_classes,
                    },
                ],
            },
            **{
                f"block-{r['block']}": {"href": block_object(recipe, r["block"], "tif"), "type": gcs.COG_TYPE, "roles": ["data"]}
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


def command_collect(recipe: dict) -> None:
    work = WORK / "collect"
    plan = make_plan(recipe, None)
    records = fetch_records(gcs.gs_join(recipe["sprout"]["path"], "blocks/"), work / "records")
    names = [b for g in plan["groups"] for b in g]
    missing = [b for b in names if b not in records]
    if missing:
        raise SystemExit(f"{len(missing)} blocks have no record, for example {missing[0]}")
    ordered = [records[b] for b in names]
    out = work / "collection"
    out.mkdir(parents=True, exist_ok=True)
    vrt = out / recipe["sprout"]["vrt"]
    vrt.write_text(vrt_text(recipe, ordered))
    collection, item = stac_documents(recipe, ordered)
    (out / "collection.json").write_text(json.dumps(collection, indent=2) + "\n")
    (out / f"{recipe['sprout']['id']}.json").write_text(json.dumps(item, indent=2) + "\n")
    for path in (vrt, out / "collection.json", out / f"{recipe['sprout']['id']}.json"):
        gcs.upload_with_gcloud(path, gcs.gs_join(recipe["sprout"]["path"], path.name))
    print(json.dumps(totals(ordered)))


# --------------------------------------------------------------------------
# Box check, read from the bucket with the gcloud account's token
# --------------------------------------------------------------------------

CHECK_BOXES = {
    "google": ["kongoussi", "mopti", "timia", "bardai", "mueda", "bria", "in-salah-south"],
    "tempo": ["saint-flour", "kansas", "ti-tree", "korgalzhyn", "lairg"],
}


def authorise_reads() -> None:
    """Let GDAL read the private bucket over HTTPS with the gcloud account's token. Kept in memory only."""
    token = subprocess.run(["gcloud", "auth", "print-access-token"], text=True, capture_output=True, check=True).stdout.strip()
    gdal.SetConfigOption("GDAL_HTTP_HEADERS", f"Authorization: Bearer {token}")
    gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")


def https(gs_path: str) -> str:
    return "/vsicurl/https://storage.googleapis.com/" + gs_path.removeprefix("gs://")


def read_box(ds, grid, origin_col: int, origin_row: int) -> tuple[np.ndarray, np.ndarray]:
    c, r = grid.col0 - origin_col, grid.row0 - origin_row
    share = ds.GetRasterBand(1).ReadAsArray(c, r, grid.cols, grid.rows)
    source = ds.GetRasterBand(2).ReadAsArray(c, r, grid.cols, grid.rows)
    return share, source


def command_check(recipe: dict, pilot: list[str] | None) -> None:
    sys.path.insert(0, str(ROOT / "buildings"))
    import compare

    authorise_reads()
    boxes = compare.load_boxes()
    cell = recipe["grid"]["pixel_size_deg"]
    scale = recipe["sprout"]["bands"]["share"]["scale"]
    vrt = None if pilot else gdal.Open(https(gcs.gs_join(recipe["sprout"]["path"], recipe["sprout"]["vrt"])))
    print("| Box | Source cells (none, Google, TEMPO) | Sprout km² | Task 01 km² | Reference |")
    print("| --- | --- | ---: | ---: | --- |")
    for kind, names in CHECK_BOXES.items():
        for name in names:
            box = boxes[name]
            grid = compare.box_grid(box["lat"], box["lon"], box["size_km"])
            if vrt is None:
                west = math.floor(grid.west / BLOCK_DEG) * BLOCK_DEG
                south = math.floor(grid.south / BLOCK_DEG) * BLOCK_DEG
                block = block_name(west, south)
                if block not in pilot:
                    continue
                ds = gdal.Open(https(block_object(recipe, block, "tif", pilot=True)))
                share, source = read_box(ds, grid, round((west + 180) / cell), round((90 - south - BLOCK_DEG) / cell))
            else:
                share, source = read_box(vrt, grid, 0, 0)
            data = compare.load_box(name)
            area = data["area"]
            valid = share != recipe["sprout"]["bands"]["share"]["nodata"]
            km2 = float((np.where(valid, share, 0) / scale * area).sum())
            if kind == "google":
                ee = compare.CACHE / f"{name}.eemean.npy"
                ref = float((np.nan_to_num(np.load(ee)) * area).sum()) if ee.exists() else float("nan")
                label = "Google mean presence (ee_box.py)" if ee.exists() else "no ee_box.py cache"
            else:
                rule = recipe["sprout"]["tempo"]
                kept = compare.tempo_kept(data["tempo"], 0.0, rule["seed_share"], rule["seed_radius_m"])
                ref = float((kept * area).sum())
                label = "TEMPO after the seed rule (compare.py)"
            cells = [int((source == v).sum()) for v in (0, 1, 2)]
            print(f"| {name} | {cells[0]:,}, {cells[1]:,}, {cells[2]:,} | {km2:.4f} | {ref:.4f} | {label} |")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("plan", "submit", "run", "records", "collect", "check"))
    parser.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--work", type=Path, default=Path("/tmp/work"))
    parser.add_argument("--pilot", nargs="?", const="E000N20,E000N40", help="blocks, comma separated")
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args()
    recipe = load_recipe(args.recipe)
    pilot = args.pilot.split(",") if args.pilot else None
    if args.command == "plan":
        command_plan(recipe)
    elif args.command == "submit":
        command_submit(args.recipe, pilot, args.approved)
    elif args.command == "run":
        run_task(recipe, json.loads(args.plan.read_text()), args.work)
    elif args.command == "collect":
        command_collect(recipe)
    elif args.command == "records":
        prefix = gcs.gs_join(scratch(recipe), "pilot/") if pilot else gcs.gs_join(recipe["sprout"]["path"], "blocks/")
        records = fetch_records(prefix, WORK / ("pilot-records" if pilot else "records"))
        for record in records.values():
            print(json.dumps(record))
        print(json.dumps(totals(records.values())))
    else:
        command_check(recipe, pilot)


if __name__ == "__main__":
    main()
