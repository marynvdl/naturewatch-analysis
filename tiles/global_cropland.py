#!/usr/bin/env python3
"""Global cropland PMTiles in shards: plan, build one shard, merge, submit.

The recipe is the catalog and this file does not edit it. The layer maths
(render, reduce, encode) is `cropland.py`; the partitioning and merge are
`pyramid.py`. This file binds the two to a recipe, to storage and to Batch.

  python tiles/global_cropland.py plan   --recipe cookbook/recipes/cropland-2024-v2.yaml
  python tiles/global_cropland.py submit --recipe ... [--pilot N] [--merge] [--approved]
  python tiles/global_cropland.py shard  --recipe ... --index N        (a Batch task)
  python tiles/global_cropland.py merge  --recipe ...                  (a Batch task)
  python tiles/global_cropland.py local  --recipe ... --sources DIR --work DIR --top-zoom 9

A recipe whose tiles.resampling is share (cookbook/recipes/cropland-2024-v3.yaml, task 09b) builds the version 3
archive: 512 px palette tiles, zoom 0 to 11, cropland/2024/v3. The shades, curve and solid zoom are in the recipe.

`plan` and `local` start nothing billable. `submit` writes the job files and
prints them; it calls gcloud only with --approved, after the expected cost has
been approved. Colour stays in the client.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from osgeo import gdal  # noqa: E402

import cropland  # noqa: E402
import pyramid  # noqa: E402
import transcode  # noqa: E402

gdal.UseExceptions()

ROOT = Path(__file__).resolve().parents[1]
SHARD_ZOOM = 5
CODE_VERSION = "1"
CLASS_RESAMPLINGS = ("max", "q3", "q2", "nearest")
CACHE_CONTROL = "public, max-age=3600"  # what the version 1 tile objects serve
CACHE_CONTROL_V3 = "public, max-age=31536000, immutable"
CODE_FILES = ("cropland.py", "pyramid.py", "transcode.py", "global_cropland.py", "verify_v3.py")
SHARE_TILE_SIZE = cropland.SHARE_TILE_SIZE  # version 3 (task 09b): the recipe sets the shades, the curve and the solid zoom
SHARD_SHAPE = {
    "machine": "e2-standard-2",
    "cpu_milli": "2000",
    "memory_mib": "6144",
    "disk_gb": 30,
    "max_run_seconds": 7200,
    "parallelism": 8,
    "max_retries": 3,
}
MERGE_SHAPE = {
    "machine": "e2-standard-4",
    "cpu_milli": "4000",
    "memory_mib": "12288",
    "disk_gb": 60,
    "max_run_seconds": 14400,
    "parallelism": 1,
    "max_retries": 1,
}


# ---- recipe and plan --------------------------------------------------------


def is_share(recipe: dict) -> bool:
    return recipe["tiles"].get("resampling") == "share"


def require_contract(recipe: dict) -> dict:
    """The tile contract, or a refusal. An unset resampling stops here."""
    contract = cropland.tile_contract(recipe)
    if contract["scheme"] != "pmtiles":
        raise SystemExit("this run needs a recipe with tiles.scheme pmtiles")
    if contract["resampling"] == "share":
        tiles = recipe["tiles"]
        missing = [key for key in ("share_steps", "share_curve", "solid_zoom") if key not in tiles]
        if missing:
            raise SystemExit(f"share needs tiles.{', tiles.'.join(missing)}. Use cookbook/recipes/cropland-2024-v3.yaml")
        contract.update(
            share_steps=int(tiles["share_steps"]), share_curve=tiles["share_curve"], solid_zoom=int(tiles["solid_zoom"])
        )
        if contract["tile_size"] != SHARE_TILE_SIZE:
            raise SystemExit(f"share tiles are {SHARE_TILE_SIZE} px")
        if not 1 <= contract["solid_zoom"] <= contract["max_zoom"]:
            raise SystemExit("solid_zoom must be from 1 to max_zoom")
    elif contract["resampling"] not in CLASS_RESAMPLINGS:
        raise SystemExit(f"resampling {contract['resampling']} is not a class-value method")
    elif contract["tile_encoding"] != "class-value":
        raise SystemExit("this run writes class-value tiles only")
    if contract["max_zoom"] <= SHARD_ZOOM:
        raise SystemExit(f"tiles.max_zoom must be above the shard zoom {SHARD_ZOOM}")
    return contract


def cell_bounds(name: str) -> pyramid.Bounds:
    """Bounds of a GLAD 10 degree tile from its name. The name is the top left corner."""
    match = re.fullmatch(r"(\d+)([NS])_(\d+)([EW])(?:\.tif)?", name)
    if not match:
        raise ValueError(f"not a GLAD tile name: {name}")
    lat = int(match[1]) * (1 if match[2] == "N" else -1)
    lon = int(match[3]) * (1 if match[4] == "E" else -1)
    return lon, lat - 10, lon + 10, lat


def cells_of(recipe: dict, only: set[str] | None = None) -> dict[str, pyramid.Bounds]:
    names = [Path(name).stem for name in recipe["seed"]["tiles"]]
    if only is not None:
        names = [name for name in names if name in only]
    return {name: cell_bounds(name) for name in names}


def make_plan(recipe: dict, only: set[str] | None = None) -> list[dict]:
    return pyramid.plan_shards(cells_of(recipe, only), SHARD_ZOOM, cropland.tiles_covering)


def config_of(recipe: dict, contract: dict) -> dict:
    config = {
        "code": CODE_VERSION,
        "recipe": recipe["id"],
        "resampling": contract["resampling"],
        "max_zoom": contract["max_zoom"],
        "shard_zoom": SHARD_ZOOM,
        "sprout": recipe["sprout"]["path"],
    }
    if contract["resampling"] == "share":  # an old shard is never reused after any of these change
        config.update(
            share_steps=contract["share_steps"], share_curve=contract["share_curve"],
            solid_zoom=contract["solid_zoom"], tile_size=contract["tile_size"],
        )
    return config


def scratch_prefix(recipe: dict) -> str:
    return f"gs://nature-watch-bucket/scratch/tiles/{recipe['id']}/"


# ---- storage: gs:// through GDAL, anything else as a path ------------------------


def vsi(path: str | Path) -> str:
    text = str(path)
    return cropland.gs_to_vsi(text) if text.startswith("gs://") else text


def exists(path: str | Path) -> bool:
    return gdal.VSIStatL(vsi(path)) is not None


def size_of(path: str | Path) -> int | None:
    stat = gdal.VSIStatL(vsi(path))
    return None if stat is None else stat.size


def read_json(path: str | Path) -> dict | None:
    if not exists(path):
        return None
    handle = gdal.VSIFOpenL(vsi(path), "rb")
    try:
        data = gdal.VSIFReadL(1, size_of(path) or 0, handle)
    finally:
        gdal.VSIFCloseL(handle)
    return json.loads(data)


def copy(source: str | Path, dest: str | Path) -> None:
    """Copy one object. A local destination appears under its final name only when whole."""
    src, dst = vsi(source), vsi(dest)
    if not str(dest).startswith("gs://"):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        part = Path(dst).with_name(Path(dst).name + ".part")
        cropland.vsi_copy(src, str(part))
        os.replace(part, dst)
    else:
        cropland.vsi_copy(src, dst)


def write_json(path: str | Path, payload: dict) -> None:
    text = json.dumps(payload, indent=2) + "\n"
    if str(path).startswith("gs://"):
        local = Path("/tmp") / ("record-" + Path(path).name)
        local.write_text(text)
        copy(local, path)
        local.unlink()
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        part = Path(str(path) + ".part")
        part.write_text(text)
        os.replace(part, path)


def delete_prefix(prefix: str) -> None:
    folder = vsi(prefix).rstrip("/")
    for name in gdal.ReadDir(folder) or []:
        if name in (".", ".."):
            continue
        if gdal.Unlink(f"{folder}/{name}") != 0:
            print(f"left in place: {prefix.rstrip('/')}/{name}", flush=True)


def shard_paths(scratch: str, shard: dict) -> tuple[str, str]:
    base = scratch.rstrip("/") + "/shards/" + shard["id"]
    return base + ".pmtiles", base + ".json"


def root_path(scratch: str, shard: dict) -> str:
    """A share shard's root tile, unquantised, for the upper zooms. The palette PNG has too few shades for that."""
    return scratch.rstrip("/") + "/shards/" + shard["id"] + ".root.npz"


def save_root(root: "Share", scratch: str, shard: dict) -> None:
    local = Path("/tmp") / f"root-{os.getpid()}-{shard['id']}.npz"
    np.savez(local, share=root.share, weight=root.weight.astype(np.float32), zoom=root.zoom)
    try:
        copy(local, root_path(scratch, shard))
    finally:
        local.unlink()


def load_root(path: Path) -> "Share":
    with np.load(path) as data:
        return Share(data["share"], data["weight"].astype(np.float64), int(data["zoom"]))


# ---- the layer ----------------------------------------------------------------


def decode_png(data: bytes) -> np.ndarray:
    """A class tile as an array: gray where alpha is set, nodata 255 elsewhere."""
    name = f"/vsimem/decode-{os.getpid()}.png"
    gdal.FileFromMemBuffer(name, data)
    try:
        dataset = gdal.Open(name)
        gray = dataset.GetRasterBand(1).ReadAsArray()
        alpha = dataset.GetRasterBand(2).ReadAsArray()
        dataset = None
    finally:
        gdal.Unlink(name)
    return np.where(alpha == 0, cropland.NODATA, gray).astype(np.uint8)


class Share(NamedTuple):
    """A share tile as the pyramid carries it: share 0 to 1 (-1 nodata), valid zoom 12 pixel count, tile zoom."""

    share: np.ndarray
    weight: np.ndarray
    zoom: int


def share_layer(contract: dict, source: str):
    nodata, size = contract["nodata"], contract["tile_size"]

    def render(z: int, x: int, y: int):
        share = cropland.fraction_tile(Path(source), z, x, y, nodata, size)
        return None if share is None else Share(share, (share >= 0).astype(np.float64), z)

    def reduce(children: dict):
        pairs = {key: (value.share, value.weight) for key, value in children.items()}
        reduced = cropland.reduce_fraction(pairs, size)
        zoom = next(iter(children.values())).zoom - 1
        return None if reduced is None else Share(reduced[0], reduced[1], zoom)

    def encode(value: Share) -> bytes:
        gray, alpha = cropland.share_gray_alpha(value.share)
        solid = value.zoom >= contract["solid_zoom"]
        return transcode.share_palette_from_gray(gray, alpha, contract["share_steps"], contract["share_curve"], solid)

    return {"render": render, "reduce": reduce, "encode": encode, "decode": None}


def layer(contract: dict, source: str):
    if contract["resampling"] == "share":
        return share_layer(contract, source)
    resampling = contract["resampling"]
    return {
        "render": lambda z, x, y: cropland.render_tile(Path(source), z, x, y, contract["nodata"], contract["classes"]),
        "reduce": lambda children: cropland.reduce_children(children, resampling),
        "encode": cropland.tile_png,
        "decode": decode_png,
    }


def archive_metadata(recipe: dict, contract: dict) -> dict:
    if contract["resampling"] == "share":
        return {
            "name": "cropland share",
            "format": "png",
            "type": "overlay",
            "description": (
                f"Palette. Red is the cropland share on a 0-255 scale, {contract['share_steps'] + 1} shades ({contract['share_curve']}), "
                f"from zoom {contract['solid_zoom']} two shades. A transparent entry is nodata"
            ),
            "encoding": "cropland-share-palette",
            "resampling": "share",
            "share_steps": contract["share_steps"],
            "share_curve": contract["share_curve"],
            "solid_zoom": contract["solid_zoom"],
            "tile_size": contract["tile_size"],
            "recipe": recipe["id"],
            "sprout": recipe["sprout"]["id"],
            "version": "3",
        }
    return {
        "name": "cropland class values",
        "format": "png",
        "type": "overlay",
        "description": "Gray 1 is cropland, gray 0 is other land, alpha 0 is nodata",
        "encoding": "class-value",
        "resampling": contract["resampling"],
        "recipe": recipe["id"],
        "sprout": recipe["sprout"]["id"],
        "version": "2",
    }


def union(bounds: list[pyramid.Bounds]) -> pyramid.Bounds:
    return (min(b[0] for b in bounds), min(b[1] for b in bounds), max(b[2] for b in bounds), max(b[3] for b in bounds))


# ---- one shard ----------------------------------------------------------------


def run_shard(
    recipe: dict, index: int, work: Path, scratch: str, sources: str | None, plan: list[dict] | None = None,
    top_zoom: int | None = None,
) -> dict:
    """Build shard `index` of the plan. Skips when its record is current and its file is whole."""
    contract = require_contract(recipe)
    top = top_zoom or contract["max_zoom"]
    plan = plan if plan is not None else make_plan(recipe)
    shard = plan[index]
    config = {**config_of(recipe, contract), "max_zoom": top}
    digest = pyramid.config_digest(config)
    file_path, record_path = shard_paths(scratch, shard)
    record = read_json(record_path)
    whole = record and (
        record["tiles"] == 0
        or (size_of(file_path) == record["bytes"] and (contract["resampling"] != "share" or exists(root_path(scratch, shard))))
    )
    if record and record.get("config") == digest and whole:
        print(f"shard {shard['id']} is done, skipping", flush=True)
        return record
    tiles_path = sources or recipe["sprout"]["tiles_path"]
    local_sources = work / shard["id"] / "src"
    for name in shard["sources"]:
        copy(tiles_path.rstrip("/") + f"/{name}.tif", local_sources / f"{name}.tif")
    vrt = work / shard["id"] / "shard.vrt"
    gdal.BuildVRT(
        str(vrt), [str(local_sources / f"{name}.tif") for name in shard["sources"]], srcNodata=contract["nodata"], VRTNodata=contract["nodata"]
    ).FlushCache()
    ops = layer(contract, str(vrt))
    out = work / shard["id"] / f"{shard['id']}.pmtiles"
    bounds = union([cell_bounds(name) for name in shard["sources"]])
    roots: list = []
    result = pyramid.build_shard(
        shard, top, ops["render"], ops["reduce"], ops["encode"], out, bounds, archive_metadata(recipe, contract), roots
    )
    result.update(config=digest, sources=shard["sources"], peak_memory_bytes=cropland.peak_memory_bytes())
    if result["tiles"]:
        copy(out, file_path)
        if size_of(file_path) != result["bytes"]:
            raise OSError(f"{file_path} is {size_of(file_path)} bytes, expected {result['bytes']}")
        if contract["resampling"] == "share":
            save_root(roots[0], scratch, shard)
    write_json(record_path, result)  # last, so a record means a finished shard
    shutil.rmtree(work / shard["id"], ignore_errors=True)
    print(
        f"shard {shard['id']} tiles {result['tiles']} bytes {result['bytes']} wall {result['wall_seconds']}s "
        f"peak {result['peak_memory_bytes']}",
        flush=True,
    )
    return result


# ---- the merge ----------------------------------------------------------------


def run_merge(
    recipe: dict, work: Path, scratch: str, dest: str, plan: list[dict] | None = None,
    top_zoom: int | None = None, verify_samples: int = 0, keep_scratch: bool = False, publish: bool = True,
) -> dict:
    contract = require_contract(recipe)
    top = top_zoom or contract["max_zoom"]
    plan = plan if plan is not None else make_plan(recipe)
    config = {**config_of(recipe, contract), "max_zoom": top}
    digest = pyramid.config_digest(config)
    started = time.perf_counter()
    if publish and exists(dest):
        raise SystemExit(f"{dest} already exists. Remove it on purpose before merging again.")

    records: dict[str, dict] = {}
    missing = []
    for shard in plan:
        file_path, record_path = shard_paths(scratch, shard)
        record = read_json(record_path)
        if not record or record.get("config") != digest or (record["tiles"] and size_of(file_path) != record["bytes"]):
            missing.append(shard["id"])
        else:
            records[shard["id"]] = record
    if missing:
        raise SystemExit(f"{len(missing)} of {len(plan)} shards are missing or not current: {', '.join(missing[:12])}")

    local_files: dict[str, Path | None] = {}
    shard_bytes = 0
    for shard in plan:
        file_path, _record = shard_paths(scratch, shard)
        if records[shard["id"]]["tiles"] == 0:
            local_files[shard["id"]] = None
            continue
        target = work / "shards" / f"{shard['id']}.pmtiles"
        copy(file_path, target)
        local_files[shard["id"]] = target
        shard_bytes += target.stat().st_size
        if contract["resampling"] == "share":
            copy(root_path(scratch, shard), work / "shards" / f"{shard['id']}.root.npz")
    fetched = time.perf_counter()

    ops = layer(contract, "")
    roots: dict[tuple[int, int], object] = {}
    for shard in plan:
        path = local_files[shard["id"]]
        if path is None:
            continue
        if contract["resampling"] == "share":
            roots[(shard["x"], shard["y"])] = load_root(work / "shards" / f"{shard['id']}.root.npz")
            continue
        first = next(iter(pyramid.read_archive(path)), None)
        if first is not None and first[0] == (shard["z"], shard["x"], shard["y"]):
            roots[(shard["x"], shard["y"])] = ops["decode"](first[1])
    upper = pyramid.upper_levels(roots, SHARD_ZOOM, ops["reduce"], ops["encode"])

    bounds = union([cell_bounds(name) for shard in plan for name in shard["sources"]])
    archive = work / "cropland.pmtiles"
    stream = pyramid.merge_entries(plan, local_files, records, SHARD_ZOOM, top, upper)
    stats = pyramid.write_archive(
        stream, archive, pyramid.archive_header(bounds, contract["min_zoom"], top), archive_metadata(recipe, contract)
    )
    expected = len(upper) + sum(record["tiles"] for record in records.values())
    if stats["tiles"] != expected:
        raise pyramid.MergeError(f"wrote {stats['tiles']} tiles, shards and upper zooms give {expected}")
    built = time.perf_counter()

    stats.update(
        shards=len(plan),
        shard_bytes=shard_bytes,
        upper_tiles=len(upper),
        min_zoom=contract["min_zoom"],
        max_zoom=top,
        resampling=contract["resampling"],
        shard_wall_seconds_total=round(sum(r["wall_seconds"] for r in records.values()), 1),
        shard_peak_memory_bytes_max=max(r["peak_memory_bytes"] for r in records.values()),
    )
    if verify_samples:
        stats["verify"] = verify_against_sprout(recipe, contract, archive, plan, verify_samples, top)
    if publish:
        upload_archive(archive, dest)
        cache = CACHE_CONTROL_V3 if "/v3/" in dest else CACHE_CONTROL
        set_cache_control(dest, cache)
        if dest.startswith("gs://nature-watch-tiles/"):  # a scratch dest is not public
            stats["range_check"] = check_range(dest)
        stats["cache_control"] = cache
    if not keep_scratch:
        delete_prefix(scratch.rstrip("/") + "/shards")
    stats["merge_wall_seconds"] = round(time.perf_counter() - started, 1)
    stats["fetch_seconds"] = round(fetched - started, 1)
    stats["build_seconds"] = round(built - fetched, 1)
    stats["peak_memory_bytes"] = cropland.peak_memory_bytes()
    print("merge " + json.dumps(stats), flush=True)
    return stats


def verify_against_sprout(recipe: dict, contract: dict, archive: Path, plan: list[dict], samples: int, top: int, root_zoom: int = 8) -> dict:
    """Rebuild a few subtrees straight from the sprout and compare every tile with the archive.

    Each sample is a zoom 8 tile (or the top zoom, whichever is lower) and its descendants.
    Both missing and present tiles must agree, byte for byte.
    """
    source = vsi(recipe["sprout"]["path"])
    ops = layer(contract, source)
    rng = random.Random(20241102)
    root_zoom = min(root_zoom, top)
    checked = 0
    for _ in range(samples):
        shard = rng.choice(plan)
        span = 2 ** (root_zoom - SHARD_ZOOM)
        root = (root_zoom, shard["x"] * span + rng.randrange(span), shard["y"] * span + rng.randrange(span))
        entries: list[pyramid.Entry] = []
        pyramid.build_subtree(root, top, ops["render"], ops["reduce"], ops["encode"], entries)
        expected = dict(entries)
        found = archive_lookup(archive, root, top)
        if expected != found:
            only_expected = len(set(expected) - set(found))
            only_found = len(set(found) - set(expected))
            differing = sum(1 for key in set(expected) & set(found) if expected[key] != found[key])
            raise pyramid.MergeError(
                f"sample under {root} disagrees with the sprout: {only_expected} missing, {only_found} extra, {differing} different"
            )
        checked += len(expected)
    return {"samples": samples, "root_zoom": root_zoom, "tiles_compared": checked}


def archive_lookup(archive: Path, root: tuple[int, int, int], top: int) -> dict[int, bytes]:
    """Tiles of a local archive under a root tile, by tile id."""
    from pmtiles.reader import MmapSource, Reader
    from pmtiles.tile import zxy_to_tileid

    found: dict[int, bytes] = {}
    with open(archive, "rb") as handle:
        reader = Reader(MmapSource(handle))
        stack = [root]
        while stack:
            z, x, y = stack.pop()
            data = reader.get(z, x, y)
            if data is not None:
                found[zxy_to_tileid(z, x, y)] = data
            if z < top:
                stack.extend((z + 1, x * 2 + dx, y * 2 + dy) for dy in (0, 1) for dx in (0, 1))
    return found


# ---- upload and the Range check ---------------------------------------------------


def metadata_token() -> str | None:
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        return json.load(urllib.request.urlopen(request, timeout=2))["access_token"]
    except OSError:
        return None


def set_cache_control(dest: str, value: str) -> None:
    bucket, name = dest.removeprefix("gs://").split("/", 1)
    token = metadata_token()
    if token is None:
        cropland.gcloud(["storage", "objects", "update", dest, f"--cache-control={value}"])
        return
    url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{urllib.request.quote(name, safe='')}"
    request = urllib.request.Request(
        url,
        data=json.dumps({"cacheControl": value}).encode(),
        method="PATCH",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    urllib.request.urlopen(request).read()


def upload_archive(archive: Path, dest: str) -> None:
    copy(archive, dest)
    if size_of(dest) != archive.stat().st_size:
        raise OSError(f"{dest} is {size_of(dest)} bytes, expected {archive.stat().st_size}")


def check_range(dest: str) -> dict:
    """A Range request on the public object must answer 206 and start with the PMTiles magic."""
    bucket, name = dest.removeprefix("gs://").split("/", 1)
    request = urllib.request.Request(
        f"https://storage.googleapis.com/{bucket}/{name}", headers={"Range": "bytes=0-126"}
    )
    with urllib.request.urlopen(request) as response:
        body = response.read()
        result = {
            "status": response.status,
            "content_range": response.headers.get("Content-Range"),
            "cache_control": response.headers.get("Cache-Control"),
        }
    if result["status"] != 206 or not body.startswith(b"PMTiles") or len(body) != 127:
        raise OSError(f"Range request on {dest} did not answer 206 with the PMTiles header: {result}")
    return result


# ---- Batch --------------------------------------------------------------------------

SHARD_COMMAND = "python3 /tmp/in/global_cropland.py shard --recipe /tmp/in/recipe.json --work /tmp/work"
MERGE_COMMAND = "python3 /tmp/in/global_cropland.py merge --recipe /tmp/in/recipe.json --work /tmp/work --verify-samples 12"


def bootstrap(prefix: str, wheel: str) -> str:
    """Download the code and recipe, make numpy and pmtiles importable."""
    bucket_path = prefix.removeprefix("gs://").rstrip("/")
    files = [*CODE_FILES, "recipe.json", wheel]
    downloads = "\n".join(f'download("{name}", folder / "{name}")' for name in files)
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
    dest.write_bytes(urllib.request.urlopen(body).read())

folder = Path("/tmp/in")
folder.mkdir(exist_ok=True)
{downloads}
PY
python3 -c "import zipfile; zipfile.ZipFile('/tmp/in/{wheel}').extractall('/tmp/in/lib')"
python3 -c "import numpy" 2>/dev/null || (apt-get update -qq && apt-get install -y -qq python3-numpy)
export PYTHONPATH=/tmp/in/lib
"""


def batch_job(prefix: str, shape: dict, command: str, tasks: int, wheel: str, run_label: str) -> dict:
    spec = {
        "maxRunDuration": f"{shape['max_run_seconds']}s",
        "maxRetryCount": shape["max_retries"],
        "lifecyclePolicies": [{"action": "RETRY_TASK", "actionCondition": {"exitCodes": [50001]}}],
        "computeResource": {"cpuMilli": shape["cpu_milli"], "memoryMib": shape["memory_mib"]},
        "environment": {
            "variables": {"CPL_MACHINE_IS_GCE": "YES", cropland.BATCH_ENV: "1", "GDAL_CACHEMAX": "512", "RUN": run_label}
        },
        "runnables": [
            {
                "container": {
                    "imageUri": cropland.IMAGE,
                    "entrypoint": "sh",
                    "commands": ["-c", bootstrap(prefix, wheel) + command + "\n"],
                }
            }
        ],
    }
    return {
        "taskGroups": [{"taskCount": str(tasks), "parallelism": str(shape["parallelism"]), "taskSpec": spec}],
        "allocationPolicy": {
            "instances": [
                {
                    "policy": {
                        "machineType": shape["machine"],
                        "provisioningModel": "SPOT",
                        "bootDisk": {"sizeGb": shape["disk_gb"], "type": "pd-balanced"},
                    }
                }
            ],
            "location": {"allowedLocations": [f"regions/{cropland.REGION}"]},
            "serviceAccount": {"email": cropland.SERVICE_ACCOUNT},
        },
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


def job_name(recipe: dict, kind: str, pilot: int | None = None) -> str:
    return f"tiles-{recipe['id']}-{kind}" + (f"-pilot{pilot}" if pilot is not None else "")


def command_submit(recipe_path: Path, pilot: int | None, merge: bool, approved: bool, dest: str | None = None) -> None:
    recipe = cropland.load_recipe(recipe_path)
    require_contract(recipe)
    plan = make_plan(recipe)
    prefix = scratch_prefix(recipe)
    work = ROOT / "data" / "tiles" / recipe["id"] / "global"
    work.mkdir(parents=True, exist_ok=True)
    wheel_files = list(work.glob("pmtiles-*.whl"))
    if not wheel_files:
        subprocess.run(
            [sys.executable, "-m", "pip", "download", "pmtiles", "--no-deps", "-d", str(work)], check=True
        )
        wheel_files = list(work.glob("pmtiles-*.whl"))
    wheel = wheel_files[0].name
    if merge:
        command = MERGE_COMMAND + (f" --dest {dest} --keep-scratch" if dest else "")
        config = batch_job(prefix, MERGE_SHAPE, command, 1, wheel, "merge")
        name = job_name(recipe, "merge")
    elif pilot is not None:
        config = batch_job(prefix, SHARD_SHAPE, SHARD_COMMAND.replace("shard --", f"shard --index {pilot} --"), 1, wheel, "pilot")
        name = job_name(recipe, "shards", pilot)
    else:
        config = batch_job(prefix, SHARD_SHAPE, SHARD_COMMAND, len(plan), wheel, "shards")
        name = job_name(recipe, "shards")
    (work / "recipe.json").write_text(json.dumps(recipe) + "\n")
    config_path = work / f"{name}.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"job {name}: {config['taskGroups'][0]['taskCount']} task(s), parallelism {config['taskGroups'][0]['parallelism']}")
    print(f"config {config_path}")
    print(f"status: gcloud batch jobs describe {name} --location={cropland.REGION} --project={cropland.PROJECT}")
    print(f"tasks:  gcloud batch tasks list --job={name} --location={cropland.REGION} --project={cropland.PROJECT}")
    print(f"cancel: gcloud batch jobs delete {name} --location={cropland.REGION} --project={cropland.PROJECT}")
    if not approved:
        print("not submitted: the expected cost is not approved (pass --approved after approval)")
        return
    for file_name in CODE_FILES:
        cropland.upload_with_gcloud(Path(__file__).resolve().parent / file_name, prefix + "code/" + file_name)
    cropland.upload_with_gcloud(work / "recipe.json", prefix + "code/recipe.json")
    cropland.upload_with_gcloud(work / wheel, prefix + "code/" + wheel)
    cropland.gcloud(["services", "enable", "batch.googleapis.com"])
    cropland.gcloud(["batch", "jobs", "submit", name, f"--location={cropland.REGION}", f"--config={config_path}"])
    print(f"submitted {name}", flush=True)


# ---- local, plan, main --------------------------------------------------------------


def command_plan(recipe: dict) -> None:
    contract = require_contract(recipe)
    plan = make_plan(recipe)
    top = contract["max_zoom"]
    z12_per_shard = 4 ** (top - SHARD_ZOOM)
    print(f"resampling {contract['resampling']} zooms {contract['min_zoom']}-{top} shard zoom {SHARD_ZOOM}")
    print(f"shards {len(plan)} (one Batch task each), source tiles {len(recipe['seed']['tiles'])}")
    print(f"zoom {top} tiles per shard at most {z12_per_shard}")
    print(f"most source tiles read by one shard {max(len(s['sources']) for s in plan)}")
    print(f"parallelism {SHARD_SHAPE['parallelism']} on {SHARD_SHAPE['machine']} = {SHARD_SHAPE['parallelism'] * 2} vCPU")


def command_local(recipe: dict, sources: str, work: Path, top_zoom: int, only: set[str] | None) -> dict:
    """The whole sharded flow on a local folder of sprout tiles. For tests and cropped samples."""
    present = {path.stem for path in Path(sources).glob("*.tif")}
    names = present if only is None else present & only
    plan = make_plan(recipe, names)
    scratch = str(work / "scratch")
    for index in range(len(plan)):
        run_shard(recipe, index, work / "tmp", scratch, sources, plan, top_zoom)
    stats = run_merge(
        recipe, work / "merge", scratch, str(work / "cropland.pmtiles"), plan=plan, top_zoom=top_zoom,
        verify_samples=0, keep_scratch=True, publish=False,
    )
    copy(work / "merge" / "cropland.pmtiles", work / "cropland.pmtiles")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "submit", "shard", "merge", "local"))
    parser.add_argument("--recipe", type=Path, default=cropland.ROOT / "cookbook" / "recipes" / "cropland-2024-v2.yaml")
    parser.add_argument("--work", type=Path, default=Path("/tmp/work"))
    parser.add_argument("--index", type=int)
    parser.add_argument("--pilot", type=int, help="submit one shard task, by plan index")
    parser.add_argument("--merge", action="store_true", help="submit the merge job")
    parser.add_argument("--approved", action="store_true", help="the expected cost is approved: really submit")
    parser.add_argument("--verify-samples", type=int, default=0)
    parser.add_argument("--sources", help="folder or prefix of sprout COGs, instead of sprout.tiles_path")
    parser.add_argument("--only", help="comma-separated tile names, for a cropped local run")
    parser.add_argument("--top-zoom", type=int)
    parser.add_argument("--keep-scratch", action="store_true", help="merge: keep the shard files, so a failed check does not cost a rebuild")
    parser.add_argument("--dest", help="merge: write the archive here instead of tiles.path, to check it before it is published")
    args = parser.parse_args()
    recipe = cropland.load_recipe(args.recipe)
    if args.command == "plan":
        command_plan(recipe)
    elif args.command == "submit":
        command_submit(args.recipe, args.pilot, args.merge, args.approved, args.dest)
    elif args.command == "local":
        only = set(args.only.split(",")) if args.only else None
        command_local(recipe, args.sources, args.work, args.top_zoom or recipe["tiles"]["max_zoom"], only)
    else:
        if os.environ.get(cropland.BATCH_ENV) != "1":
            raise SystemExit("shard and merge run as Batch tasks. From this computer use: submit")
        scratch = scratch_prefix(recipe)
        if args.command == "shard":
            index = args.index if args.index is not None else int(os.environ.get("BATCH_TASK_INDEX", "-1"))
            if index < 0:
                raise SystemExit("shard needs --index or BATCH_TASK_INDEX")
            run_shard(recipe, index, args.work, scratch, args.sources)
        else:
            run_merge(
                recipe, args.work, scratch, args.dest or recipe["tiles"]["path"], verify_samples=args.verify_samples,
                keep_scratch=args.keep_scratch
            )


if __name__ == "__main__":
    main()
