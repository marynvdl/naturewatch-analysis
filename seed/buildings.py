#!/usr/bin/env python3
"""Write the TEMPO seed and check both seeds of the Buildings 2023 layer, in one Batch job.

The recipe is cookbook/recipes/buildings-2023-v1.yaml. The Google seed is written by
gee/buildings/export_google.py first. Then `submit` starts one spot machine in
europe-west1 that runs `run`:

1. TEMPO. Download the 2023 Q4 tile index, rebuild the URL list and stop if its SHA-256
   differs from the recipe. Download the tiles of one zoom 4 Web Mercator tile at a time,
   check each one, and write band 1 as one COG per zoom 4 tile to the TEMPO seed path.
   A mosaic already in the seed path is skipped, so a preempted job resumes.
2. Google. Check every block COG for the grid, uint8, nodata 255 and values 0-250 or 255.
3. Write summary.json to each seed path.

Nothing is downloaded to the laptop.

Examples
  python seed/buildings.py plan
  python seed/buildings.py submit
  python seed/buildings.py status
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECIPE = ROOT / "cookbook" / "recipes" / "buildings-2023-v1.yaml"
BLOCKS = ROOT / "gee" / "buildings" / "google-2023-blocks.json"
PROJECT = "nature-watch-387210"
REGION = "europe-west1"
IMAGE = "ghcr.io/osgeo/gdal:ubuntu-small-3.11.4"
MACHINE = "e2-standard-2"
SERVICE_ACCOUNT = "637146989512-compute@developer.gserviceaccount.com"
SCRATCH = "gs://nature-watch-bucket/scratch/seed/buildings-2023-v1/"
JOB = "seed-buildings-2023-v1"
BATCH_ENV = "NATUREWATCH_SEED_BATCH"
RUN_SECONDS = "43200s"
# Each request takes about 1.2 s from Microsoft's server. 64 at a time gave 49 tiles a second
# from the laptop on 17 November 2026, so 822,199 tiles take about 4.7 hours.
WORKERS = 64
USER_AGENT = "naturewatch-analysis"

# Web Mercator
HALF = 20037508.342789244
TILE_PX = 256
NAME = re.compile(r"L15-(\d{4})E-(\d{4})N\.tif$")


# --------------------------------------------------------------------------
# Pure helpers: no network. These are what the tests cover.
# --------------------------------------------------------------------------


def load_recipe(path: Path) -> dict:
    text = Path(path).read_text()
    if path.suffix == ".json":
        return json.loads(text)
    import yaml

    return yaml.safe_load(text)


def url_list_sha256(urls: list[str]) -> str:
    """SHA-256 of the URLs sorted bytewise, one per line with a trailing newline."""
    text = "".join(u + "\n" for u in sorted(urls, key=lambda u: u.encode()))
    return hashlib.sha256(text.encode()).hexdigest()


def tile_xy(url: str, zoom: int) -> tuple[int, int]:
    """XYZ x, y of a TEMPO tile. The file name counts rows from the south (TMS)."""
    match = NAME.search(url)
    if not match:
        raise ValueError(f"not a TEMPO tile name: {url}")
    x, y_tms = int(match.group(1)), int(match.group(2))
    return x, (2**zoom - 1) - y_tms


def group_by_mosaic(urls: list[str], tile_zoom: int, mosaic_zoom: int) -> dict[tuple[int, int], list[str]]:
    shift = tile_zoom - mosaic_zoom
    groups: dict[tuple[int, int], list[str]] = defaultdict(list)
    for url in urls:
        x, y = tile_xy(url, tile_zoom)
        groups[(x >> shift, y >> shift)].append(url)
    return dict(sorted(groups.items()))


def tile_bounds(x: int, y: int, zoom: int) -> tuple[float, float, float, float]:
    """West, south, east, north in EPSG:3857 metres of XYZ tile x, y."""
    size = 2 * HALF / 2**zoom
    west = -HALF + x * size
    north = HALF - y * size
    return west, north - size, west + size, north


def pixel_size(zoom: int) -> float:
    return 2 * HALF / 2**zoom / TILE_PX


def mosaic_name(recipe: dict, x: int, y: int) -> str:
    return recipe["seeds"]["tempo"]["object"].format(x=x, y=y)


def block_name(west: int, south: int) -> str:
    ew = "E" if west >= 0 else "W"
    ns = "N" if south >= 0 else "S"
    return f"{ew}{abs(west):03d}{ns}{abs(south):02d}"


def google_names(blocks: list[list[int]]) -> list[str]:
    return [block_name(w, s) + ".tif" for w, s in blocks]


def tempo_problems(ds, x: int, y: int, zoom: int, nodata: float) -> list[str]:
    """Grid, type, nodata and band 1 values of one source tile."""
    import numpy as np
    from osgeo import gdal

    problems = []
    srs = ds.GetSpatialRef()
    if srs is None or srs.GetAuthorityCode(None) != "3857":
        problems.append("crs is not EPSG:3857")
    if (ds.RasterXSize, ds.RasterYSize) != (TILE_PX, TILE_PX):
        problems.append(f"size {ds.RasterXSize} x {ds.RasterYSize}")
    west, _, _, north = tile_bounds(x, y, zoom)
    gt = ds.GetGeoTransform()
    res = pixel_size(zoom)
    expected = (west, res, 0.0, north, 0.0, -res)
    if any(abs(a - b) > 0.01 for a, b in zip(gt, expected)):
        problems.append(f"geotransform {gt}, expected {expected}")
    band = ds.GetRasterBand(1)
    if band.DataType != gdal.GDT_Float32:
        problems.append(f"band 1 type {gdal.GetDataTypeName(band.DataType)}")
    if band.GetNoDataValue() != nodata:
        problems.append(f"band 1 nodata {band.GetNoDataValue()}")
    values = band.ReadAsArray()
    good = (values == nodata) | ((values >= 0) & (values <= 1))
    if not good.all():
        bad = values[~good]
        problems.append(f"{bad.size} values outside 0-1 and nodata, e.g. {bad[:3].tolist()}")
    return problems


def google_problems(ds, west: int, south: int, recipe: dict) -> tuple[list[str], list[int]]:
    """Grid, type, nodata and the histogram of one Google block."""
    from osgeo import gdal

    seed = recipe["seeds"]["google"]
    cell = recipe["grid"]["pixel_size_deg"]
    side = int(round(seed["blocks_deg"] / cell))
    problems = []
    srs = ds.GetSpatialRef()
    if srs is None or srs.GetAuthorityCode(None) != "4326":
        problems.append("crs is not EPSG:4326")
    if (ds.RasterXSize, ds.RasterYSize) != (side, side):
        problems.append(f"size {ds.RasterXSize} x {ds.RasterYSize}, expected {side}")
    expected = (west, cell, 0.0, south + seed["blocks_deg"], 0.0, -cell)
    if any(abs(a - b) > 1e-9 for a, b in zip(ds.GetGeoTransform(), expected)):
        problems.append(f"geotransform {ds.GetGeoTransform()}, expected {expected}")
    band = ds.GetRasterBand(1)
    if band.DataType != gdal.GDT_Byte:
        problems.append(f"type {gdal.GetDataTypeName(band.DataType)}")
    if band.GetNoDataValue() != seed["nodata"]:
        problems.append(f"nodata {band.GetNoDataValue()}")
    histogram = band.GetHistogram(-0.5, 255.5, 256, include_out_of_range=False, approx_ok=False)
    # GDAL leaves nodata cells out of the histogram. Count them as the rest of the block.
    histogram[seed["nodata"]] += ds.RasterXSize * ds.RasterYSize - sum(histogram)
    top = seed["scale"]
    stray = sum(histogram[top + 1:seed["nodata"]])
    if stray:
        problems.append(f"{stray} cells between {top + 1} and {seed['nodata'] - 1}")
    return problems, histogram


# --------------------------------------------------------------------------
# Cloud. Runs in the Batch container, or submits from the laptop.
# --------------------------------------------------------------------------


def split_gs(path: str) -> tuple[str, str]:
    bucket, _, name = path[len("gs://"):].partition("/")
    return bucket, name


def metadata_token() -> str:
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["access_token"]


def list_names(prefix: str) -> dict[str, int]:
    """Object names under a gs:// prefix, relative to it, with their sizes. Uses the VM's token."""
    from urllib.parse import quote

    bucket, name = split_gs(prefix)
    out: dict[str, int] = {}
    page = None
    while True:
        url = (f"https://storage.googleapis.com/storage/v1/b/{bucket}/o?prefix={quote(name, safe='')}"
               "&fields=items(name,size),nextPageToken")
        if page:
            url += "&pageToken=" + quote(page, safe="")
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + metadata_token()})
        payload = json.load(urllib.request.urlopen(request, timeout=120))
        for item in payload.get("items") or []:
            out[item["name"][len(name):]] = int(item["size"])
        page = payload.get("nextPageToken")
        if not page:
            return out


def http_get(url: str, tries: int = 5) -> bytes:
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def download_file(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=600) as response, dest.open("wb") as out:
        shutil.copyfileobj(response, out, 8 * 1024 * 1024)


def upload(local: Path, gs_path: str) -> None:
    from osgeo import gdal

    dest = "/vsigs/" + gs_path[len("gs://"):]
    if gdal.CopyFile(str(local), dest) != 0:
        raise OSError(f"upload failed: {dest}")


def index_urls(recipe: dict, work: Path) -> list[str]:
    from osgeo import ogr

    tempo = recipe["sources"]["tempo"]
    path = work / "tile_index.gpkg"
    if not path.exists():
        download_file(tempo["index"], path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f"index sha256 {digest}", flush=True)
    ds = ogr.Open(str(path))
    layer = ds.GetLayerByName(tempo["index_layer"])
    urls = [f.GetField(tempo["index_column"]) for f in layer]
    urls = [u for u in urls if u]
    if len(urls) != tempo["tile_count"]:
        raise SystemExit(f"the index lists {len(urls)} tiles, the recipe says {tempo['tile_count']}")
    if url_list_sha256(urls) != tempo["url_list_sha256"]:
        raise SystemExit("the URL list differs from the recipe's url_list_sha256")
    return urls


def write_mosaic(recipe: dict, x: int, y: int, urls: list[str], work: Path) -> dict:
    from osgeo import gdal

    tempo_source = recipe["sources"]["tempo"]
    seed = recipe["seeds"]["tempo"]
    zoom, mzoom, nodata = tempo_source["tile_zoom"], seed["mosaic_zoom"], seed["nodata"]
    folder = work / f"z{mzoom}-{x}-{y}"
    folder.mkdir(parents=True, exist_ok=True)

    def fetch(url: str) -> Path:
        path = folder / url.rsplit("/", 1)[1]
        path.write_bytes(http_get(url))
        tx, ty = tile_xy(url, zoom)
        ds = gdal.Open(str(path))
        problems = tempo_problems(ds, tx, ty, zoom, nodata)
        ds = None
        if problems:
            raise SystemExit(f"{url} failed the check:\n" + "\n".join(problems))
        return path

    with ThreadPoolExecutor(WORKERS) as pool:
        paths = list(pool.map(fetch, urls))
    source_bytes = sum(p.stat().st_size for p in paths)
    west, south, east, north = tile_bounds(x, y, mzoom)
    res = pixel_size(zoom)
    vrt = folder / "mosaic.vrt"
    gdal.BuildVRT(str(vrt), [str(p) for p in paths], options=gdal.BuildVRTOptions(
        bandList=[1], outputBounds=(west, south, east, north), xRes=res, yRes=res,
        srcNodata=nodata, VRTNodata=nodata))
    out = work / mosaic_name(recipe, x, y)
    gdal.Translate(str(out), str(vrt), options=gdal.TranslateOptions(
        format="COG", creationOptions=[
            "COMPRESS=DEFLATE", "PREDICTOR=YES", "BLOCKSIZE=512", "OVERVIEWS=NONE", "NUM_THREADS=ALL_CPUS",
        ]))
    ds = gdal.Open(str(out))
    size = (ds.RasterXSize, ds.RasterYSize)
    ds = None
    expected = TILE_PX * 2 ** (zoom - mzoom)
    if size != (expected, expected):
        raise SystemExit(f"{out.name} is {size}, expected {expected} square")
    upload(out, seed["path"] + out.name)
    stored = out.stat().st_size
    out.unlink()
    shutil.rmtree(folder)
    return {"tiles": len(urls), "source_bytes": source_bytes, "bytes": stored}


def command_run(recipe: dict, blocks: list[list[int]], work: Path) -> None:
    if os.environ.get(BATCH_ENV) != "1":
        raise SystemExit("`run` runs on the Batch machine. From this computer, use: python seed/buildings.py submit")
    from osgeo import gdal

    gdal.UseExceptions()
    gdal.SetConfigOption("CPL_MACHINE_IS_GCE", "YES")
    work.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    # TEMPO
    tempo_source = recipe["sources"]["tempo"]
    seed = recipe["seeds"]["tempo"]
    urls = index_urls(recipe, work)
    groups = group_by_mosaic(urls, tempo_source["tile_zoom"], seed["mosaic_zoom"])
    present = list_names(seed["path"])
    mosaics = {}
    for i, ((x, y), group) in enumerate(groups.items(), start=1):
        name = mosaic_name(recipe, x, y)
        if name in present:
            print(f"{i}/{len(groups)} {name} already stored, skipped", flush=True)
            mosaics[name] = {"tiles": len(group), "bytes": present[name]}
            continue
        t0 = time.perf_counter()
        mosaics[name] = write_mosaic(recipe, x, y, group, work)
        print(f"{i}/{len(groups)} {name} {mosaics[name]} {time.perf_counter() - t0:.0f} s", flush=True)
    tempo_summary = {
        "id": seed["id"], "mosaics": len(mosaics), "tiles": sum(m["tiles"] for m in mosaics.values()),
        "bytes": sum(m["bytes"] for m in mosaics.values()), "per_mosaic": mosaics,
    }
    if tempo_summary["tiles"] != tempo_source["tile_count"]:
        raise SystemExit(f"mosaics hold {tempo_summary['tiles']} tiles, the recipe says {tempo_source['tile_count']}")
    summary_path = work / "tempo-summary.json"
    summary_path.write_text(json.dumps(tempo_summary, indent=1) + "\n")
    upload(summary_path, seed["path"] + "summary.json")
    tempo_seconds = time.perf_counter() - started
    print(f"TEMPO: {len(mosaics)} mosaics, {tempo_summary['bytes']} bytes, {tempo_seconds:.0f} s", flush=True)

    # Google
    gseed = recipe["seeds"]["google"]
    present = list_names(gseed["path"])
    tifs = sorted(n for n in present if n.endswith(".tif"))
    expected = sorted(google_names(blocks))
    if tifs != expected:
        raise SystemExit(f"Google seed objects differ from the block list: "
                         f"missing {sorted(set(expected) - set(tifs))[:5]}, extra {sorted(set(tifs) - set(expected))[:5]}")
    total = [0] * 256
    per_block = {}
    for i, (west, south) in enumerate(blocks, start=1):
        name = block_name(west, south) + ".tif"
        ds = gdal.Open("/vsigs/" + gseed["path"][len("gs://"):] + name)
        problems, histogram = google_problems(ds, west, south, recipe)
        ds = None
        if problems:
            raise SystemExit(f"{name} failed the check:\n" + "\n".join(problems))
        total = [a + b for a, b in zip(total, histogram)]
        covered = sum(histogram) - histogram[gseed["nodata"]]
        per_block[name] = {"bytes": present[name], "covered_cells": covered, "nonzero_cells": covered - histogram[0]}
        print(f"{i}/{len(blocks)} {name} {per_block[name]}", flush=True)
    google_summary = {
        "id": gseed["id"], "blocks": len(per_block), "bytes": sum(b["bytes"] for b in per_block.values()),
        "histogram": total, "per_block": per_block,
    }
    summary_path = work / "google-summary.json"
    summary_path.write_text(json.dumps(google_summary, indent=1) + "\n")
    upload(summary_path, gseed["path"] + "summary.json")
    print(f"Google: {len(per_block)} blocks checked, {google_summary['bytes']} bytes", flush=True)
    print(f"wall_seconds {time.perf_counter() - started:.1f}")
    print(f"peak_memory_bytes {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}")


def batch_job(scratch: str) -> dict:
    """One spot VM in europe-west1. Batch deletes it when the task ends.

    The script, the recipe and the block list are copied to the scratch prefix first.
    A retry after preemption skips the mosaics already stored.
    """
    prefix = scratch.removeprefix("gs://").rstrip("/")
    fetch = f"""python3 - << 'PY'
import json
import urllib.request
from pathlib import Path
request = urllib.request.Request(
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
    headers={{"Metadata-Flavor": "Google"}},
)
token = json.load(urllib.request.urlopen(request))["access_token"]
folder = Path("/tmp/in")
folder.mkdir(exist_ok=True)
for name in ("buildings.py", "recipe.json", "blocks.json"):
    body = urllib.request.Request("https://storage.googleapis.com/{prefix}/" + name,
                                  headers={{"Authorization": "Bearer " + token}})
    (folder / name).write_bytes(urllib.request.urlopen(body).read())
PY
python3 /tmp/in/buildings.py run --recipe /tmp/in/recipe.json --blocks /tmp/in/blocks.json --work /tmp/work
"""
    return {
        "taskGroups": [{
            "taskCount": "1",
            "taskSpec": {
                "maxRunDuration": RUN_SECONDS,
                "maxRetryCount": 3,
                "computeResource": {"cpuMilli": "2000", "memoryMib": "6144"},
                "environment": {"variables": {"CPL_MACHINE_IS_GCE": "YES", BATCH_ENV: "1"}},
                "runnables": [{"container": {"imageUri": IMAGE, "entrypoint": "sh", "commands": ["-c", fetch]}}],
            },
        }],
        "allocationPolicy": {
            "instances": [{"policy": {
                "machineType": MACHINE,
                "provisioningModel": "SPOT",
                "bootDisk": {"sizeGb": 50, "type": "pd-balanced"},
            }}],
            "location": {"allowedLocations": [f"regions/{REGION}"]},
            "serviceAccount": {"email": SERVICE_ACCOUNT},
        },
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


def gcloud(args: list[str]) -> None:
    subprocess.run(["gcloud", *args, f"--project={PROJECT}"], check=True, text=True)


def command_plan(recipe: dict, blocks: list[list[int]]) -> None:
    tempo = recipe["seeds"]["tempo"]
    print(f"Batch job {JOB}: one spot {MACHINE} in {REGION}, {WORKERS} downloads at a time")
    print(f"  TEMPO: {recipe['sources']['tempo']['tile_count']} tiles into zoom {tempo['mosaic_zoom']} "
          f"mosaics at {tempo['path']}{tempo['object']}")
    print(f"  Google: check {len(blocks)} blocks at {recipe['seeds']['google']['path']}")
    print(f"  scratch: {SCRATCH}")


def command_submit(recipe: dict, recipe_path: Path, blocks_path: Path) -> None:
    work = ROOT / "data" / "seed" / recipe["id"]
    work.mkdir(parents=True, exist_ok=True)
    recipe_json = work / "recipe.json"
    recipe_json.write_text(json.dumps(recipe) + "\n")
    gcloud(["services", "enable", "batch.googleapis.com"])
    for local, name in ((Path(__file__).resolve(), "buildings.py"), (recipe_json, "recipe.json"), (blocks_path, "blocks.json")):
        subprocess.run(["gcloud", "storage", "cp", str(local), SCRATCH + name, f"--project={PROJECT}"], check=True)
    config = work / "batch_job.json"
    config.write_text(json.dumps(batch_job(SCRATCH), indent=2) + "\n")
    gcloud(["batch", "jobs", "submit", JOB, f"--location={REGION}", f"--config={config}"])
    print(f"submitted {JOB} in {REGION}")
    print(f"status: gcloud batch jobs describe {JOB} --location={REGION} --project={PROJECT}")
    print(f"cancel: gcloud batch jobs delete {JOB} --location={REGION} --project={PROJECT}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("plan", "submit", "status", "run"))
    parser.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
    parser.add_argument("--blocks", type=Path, default=BLOCKS)
    parser.add_argument("--work", type=Path, default=Path("/tmp/work"))
    args = parser.parse_args(argv)
    recipe = load_recipe(args.recipe)
    blocks = json.loads(args.blocks.read_text())["blocks"]
    if args.command == "plan":
        command_plan(recipe, blocks)
    elif args.command == "submit":
        command_submit(recipe, args.recipe, args.blocks)
    elif args.command == "status":
        gcloud(["batch", "jobs", "describe", JOB, f"--location={REGION}", "--format=value(status.state)"])
    else:
        command_run(recipe, blocks, args.work)


if __name__ == "__main__":
    main()
