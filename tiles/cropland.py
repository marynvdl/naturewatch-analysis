#!/usr/bin/env python3
"""Build class-value map tiles from the cropland sprout.

The recipe is the catalog. This script reads it and does not edit it.
Pixels stay 0 (not cropland) or 1 (cropland). Nodata stays transparent.
Colour is applied in the client. The published run is a Batch job in
europe-west1. The laptop only submits that job.

  python tiles/cropland.py submit
"""
from __future__ import annotations

import argparse
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


def tile_contract(recipe: dict) -> dict:
    """The recipe's tile block. A test can lower max_zoom; the file says 12."""
    tiles = recipe.get("tiles")
    if not isinstance(tiles, dict):
        raise ValueError("recipe has no tiles block")
    values = published_values(recipe)
    problems = []
    if tiles.get("scheme") != "xyz":
        problems.append("tiles.scheme must be xyz")
    if int(tiles.get("tile_size", 0)) != TILE_SIZE:
        problems.append(f"tiles.tile_size must be {TILE_SIZE}")
    if tiles.get("resampling") != "max":
        problems.append("tiles.resampling must be max")
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
        "path": path if path.endswith("/") else path + "/",
        "min_zoom": 0,
        "max_zoom": max_zoom,
        "nodata": 255,
        "classes": (0, 1),
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
    path.write_bytes(png)


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
    parser.add_argument("command", choices=("build", "run", "submit"))
    parser.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--dest", type=Path)
    parser.add_argument("--work", type=Path, default=ROOT / "data" / "tiles" / "cropland-2024")
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--scratch", default=None)
    args = parser.parse_args()
    started = time.perf_counter()
    recipe = load_recipe(args.recipe)
    if args.command == "submit":
        command_submit(args.recipe)
    elif args.command == "build":
        if args.source is None or args.dest is None:
            raise SystemExit("build needs --source and --dest")
        command_build(recipe, args.source, args.dest)
    else:
        command_run(recipe, args.work, args.upload, args.scratch)
    elapsed = time.perf_counter() - started
    print(f"wall_seconds {elapsed:.1f}")
    print(f"peak_memory_bytes {peak_memory_bytes()}")


if __name__ == "__main__":
    main()
