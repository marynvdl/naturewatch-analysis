#!/usr/bin/env python3
"""Download and check the GLAD cropland tiles named in a cookbook recipe.

Open one tile and record its source codes in the recipe before fetching the
rest. This script does not edit the recipe. It refuses a full download while
any seed value is still "confirm-from-one-tile".

The South Africa clip keeps GLAD's codes inside the mainland boundary and
writes the clip's nodata value outside it. The sprout step remaps those codes.
That laptop path is the version 1 command. It refuses a recipe with no clip.

The global recipe is two other commands. `transfer` writes a URL list from the
recipe and submits a Storage Transfer Service job into `seed.path`. The service
first writes each object as the source host plus the URL path. `check` submits
a GDAL check on a spot machine in europe-west1. A tile that passes is moved to
the flat file name, and the host path is deleted. Neither command downloads the
tile set to the laptop, and neither writes a clip.

Examples
  python seed/cropland.py sample
  python seed/cropland.py run
  python seed/cropland.py transfer --recipe cookbook/recipes/cropland-2024-v2.yaml
  python seed/cropland.py check --recipe cookbook/recipes/cropland-2024-v2.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

from osgeo import gdal, ogr, osr

gdal.UseExceptions()
ogr.UseExceptions()

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECIPE = ROOT / "cookbook" / "recipes" / "cropland-2024.yaml"
DEFAULT_WORK = ROOT / "data" / "seed" / "cropland-2024"
PROJECT = "nature-watch-387210"
REGION = "europe-west1"
IMAGE = "ghcr.io/osgeo/gdal:ubuntu-small-3.11.4"
MACHINE = "e2-standard-2"
SERVICE_ACCOUNT = "637146989512-compute@developer.gserviceaccount.com"
TRANSFER_AGENT = "project-637146989512@storage-transfer-service.iam.gserviceaccount.com"
V1_PREFIX = "gs://nature-watch-bucket/seeds/cropland/glad/2024/v1/"
SCRATCH = "gs://nature-watch-bucket/scratch/seed/cropland-2024-v2/"
TRANSFER_JOB = "cropland-2024-v2-seed"
CHECK_JOB = "seed-cropland-glad-2024-v2"
BATCH_ENV = "NATUREWATCH_SEED_BATCH"
# 223 exact histograms. A short limit would stop a slow tile.
CHECK_RUN_SECONDS = "43200s"
PENDING = "confirm-from-one-tile"
CHUNK = 2 * 1024 * 1024
USER_AGENT = "naturewatch-analysis"
# Inside 20S_020E and well inside South Africa. The clip must keep this pixel.
CHECK_LON = 28.0
CHECK_LAT = -26.2

DTYPE_NAME = {gdal.GDT_Byte: "uint8"}


@dataclass(frozen=True)
class RasterInfo:
    """Grid and pixel codes read from one GeoTIFF."""

    width: int
    height: int
    crs: str
    pixel_size_deg: float
    origin_x: float
    origin_y: float
    dtype: str
    nodata: int | None
    counts: dict[int, int]


def load_recipe(path: Path) -> dict:
    text = path.read_text()
    if path.suffix == ".json":
        return json.loads(text)
    import yaml

    return yaml.safe_load(text)


def seed_prefix(path: str) -> str:
    return path if path.endswith("/") else path + "/"


def require_laptop_recipe(recipe: dict) -> None:
    """The ranged download and the clip belong to the version 1 recipe."""
    if "clip" not in recipe["seed"]:
        raise SystemExit(
            "This recipe has no clip, so the laptop download does not run. "
            "The global commands are transfer and check."
        )


def require_transfer_recipe(recipe: dict) -> None:
    """The global commands must not write the version 1 seed."""
    path = seed_prefix(recipe["seed"]["path"])
    if "clip" in recipe["seed"] or path.startswith(V1_PREFIX):
        raise SystemExit(
            f"{path} is the version 1 seed. The transfer command does not write there."
        )


def pending_values(recipe: dict) -> list[str]:
    """Seed codes that still wait for a tile to be opened."""
    return [key for key, value in recipe["seed"]["values"].items() if value == PENDING]


def content_length(url: str) -> int:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return int(response.headers["Content-Length"])


def fetch_range(url: str, start: int, end: int) -> bytes:
    """Download one byte range. GLAD closes long connections, so the range stays small."""
    expected = end - start + 1
    last_error = None
    for attempt in range(8):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end}"},
            )
            with urllib.request.urlopen(request, timeout=90) as response:
                if response.status != 206:
                    raise OSError(f"expected HTTP 206, got {response.status}")
                data = response.read()
            if len(data) != expected:
                raise OSError(f"short read {len(data)} != {expected}")
            return data
        except Exception as exc:  # noqa: BLE001 - retry any network failure
            last_error = exc
            print(f"retry {attempt} at byte {start}: {exc}", flush=True)
            time.sleep(2 + attempt)
    raise OSError(f"download failed at byte {start}: {last_error}")


def download(url: str, dest: Path) -> Path:
    """Resume a file until it matches the server's content length."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = content_length(url)
    have = dest.stat().st_size if dest.exists() else 0
    if have > total:
        dest.unlink()
        have = 0
    if have == total:
        print(f"already complete {dest.name} ({total} bytes)", flush=True)
        return dest
    print(f"download {dest.name} from byte {have} of {total}", flush=True)
    while have < total:
        end = min(have + CHUNK, total) - 1
        data = fetch_range(url, have, end)
        with dest.open("ab") as handle:
            handle.write(data)
        have += len(data)
        print(f"{dest.name} {have}/{total}", flush=True)
    return dest


def tile_url(recipe: dict, name: str) -> str:
    base = recipe["source"]["download"]
    if not base.endswith("/"):
        base += "/"
    return base + name


def spatial_reference(wkt: str) -> str:
    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)
    name = srs.GetAuthorityName(None) or srs.GetAuthorityName("GEOGCS")
    code = srs.GetAuthorityCode(None) or srs.GetAuthorityCode("GEOGCS")
    if not name or not code:
        raise ValueError(f"raster CRS has no EPSG code: {wkt}")
    return f"{name}:{code}"


def inspect_raster(path: Path) -> RasterInfo:
    """Read the grid and the exact pixel-value counts. A short file fails here."""
    dataset = gdal.Open(str(path))
    band = dataset.GetRasterBand(1)
    transform = dataset.GetGeoTransform()
    if transform[2] != 0 or transform[4] != 0:
        raise ValueError(f"{path.name} is rotated or sheared")
    if abs(abs(transform[1]) - abs(transform[5])) > 1e-15:
        raise ValueError(f"{path.name} has unequal pixel width and height")
    histogram = band.GetHistogram(
        min=-0.5, max=255.5, buckets=256, include_out_of_range=True, approx_ok=False
    )
    counts = {value: int(count) for value, count in enumerate(histogram) if count}
    nodata = band.GetNoDataValue()
    info = RasterInfo(
        width=dataset.RasterXSize,
        height=dataset.RasterYSize,
        crs=spatial_reference(dataset.GetProjection()),
        pixel_size_deg=abs(transform[1]),
        origin_x=transform[0],
        origin_y=transform[3],
        dtype=DTYPE_NAME.get(band.DataType, gdal.GetDataTypeName(band.DataType)),
        nodata=None if nodata is None else int(nodata),
        counts=counts,
    )
    dataset = None
    return info


def _aligned(origin: float, pixel: float) -> bool:
    return abs(origin / pixel - round(origin / pixel)) < 1e-6


def source_codes(recipe: dict) -> set[int]:
    values = recipe["seed"]["values"]
    return {int(values["cropland"]), int(values["other"])}


def check_tile(info: RasterInfo, recipe: dict) -> list[str]:
    """A source tile must match the recipe grid and use only the recorded codes."""
    problems = _grid_problems(info, recipe)
    values = recipe["seed"]["values"]
    if info.nodata != values["nodata"]:
        problems.append(f"nodata is {info.nodata}, recipe says {values['nodata']}")
    extra = set(info.counts) - source_codes(recipe)
    if extra:
        problems.append(f"unexpected values {sorted(extra)}")
    counted = sum(info.counts.values())
    if info.nodata is None and counted != info.width * info.height:
        problems.append(f"counted {counted} pixels of {info.width * info.height}")
    span = info.width * info.pixel_size_deg
    if abs(span - 10) > 1e-6 or abs(info.height * info.pixel_size_deg - 10) > 1e-6:
        problems.append(f"tile spans {span}°, expected 10°")
    return problems


def check_tiles(infos: list[RasterInfo], recipe: dict) -> list[str]:
    problems = []
    for info in infos:
        problems.extend(check_tile(info, recipe))
    cropland = int(recipe["seed"]["values"]["cropland"])
    if not any(cropland in info.counts for info in infos):
        problems.append(f"value {cropland} does not appear in the downloaded tiles")
    return problems


def check_clip(info: RasterInfo, recipe: dict) -> list[str]:
    """The clip uses the same grid. Outside the boundary is the clip nodata value."""
    problems = _grid_problems(info, recipe)
    nodata = int(recipe["seed"]["clip"]["nodata"])
    if info.nodata != nodata:
        problems.append(f"clip nodata is {info.nodata}, recipe says {nodata}")
    allowed = source_codes(recipe)
    extra = set(info.counts) - allowed
    if extra:
        problems.append(f"clip has values outside the source codes: {sorted(extra)}")
    cropland = int(recipe["seed"]["values"]["cropland"])
    if cropland not in info.counts:
        problems.append(f"clip does not contain value {cropland}")
    if nodata in allowed:
        problems.append(f"clip nodata {nodata} collides with a GLAD source code")
    return problems


def _grid_problems(info: RasterInfo, recipe: dict) -> list[str]:
    grid = recipe["grid"]
    problems = []
    if info.crs != grid["crs"]:
        problems.append(f"CRS is {info.crs}, recipe says {grid['crs']}")
    pixel = float(grid["pixel_size_deg"])
    if abs(info.pixel_size_deg - pixel) > 1e-12:
        problems.append(f"pixel size is {info.pixel_size_deg}, recipe says {pixel}")
    if info.dtype != grid["dtype"]:
        problems.append(f"dtype is {info.dtype}, recipe says {grid['dtype']}")
    if not _aligned(info.origin_x, pixel) or not _aligned(info.origin_y, pixel):
        problems.append("origin is not on the recipe grid")
    return problems


def envelope(dataset_path: Path) -> tuple[float, float, float, float]:
    dataset = gdal.Open(str(dataset_path))
    transform = dataset.GetGeoTransform()
    minx = transform[0]
    maxy = transform[3]
    maxx = minx + dataset.RasterXSize * transform[1]
    miny = maxy + dataset.RasterYSize * transform[5]
    dataset = None
    return minx, maxx, miny, maxy


def union_envelope(paths: list[Path]) -> tuple[float, float, float, float]:
    boxes = [envelope(path) for path in paths]
    return (
        min(box[0] for box in boxes),
        max(box[1] for box in boxes),
        min(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def explode_polygons(geometry) -> list:
    kind = ogr.GT_Flatten(geometry.GetGeometryType())
    if kind == ogr.wkbPolygon:
        return [geometry.Clone()]
    if kind in (ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
        parts = []
        for index in range(geometry.GetGeometryCount()):
            parts.extend(explode_polygons(geometry.GetGeometryRef(index)))
        return parts
    return []


def envelope_polygon(box: tuple[float, float, float, float]):
    minx, maxx, miny, maxy = box
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)):
        ring.AddPoint(x, y)
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    return polygon


def polygons_meeting(geometries, box: tuple[float, float, float, float]) -> list:
    """Keep polygons that meet the tile footprint. Distant islands drop out."""
    footprint = envelope_polygon(box)
    kept = []
    for geometry in geometries:
        for part in explode_polygons(geometry):
            if part.Intersects(footprint):
                kept.append(part)
    return kept


def read_geometries(path: Path) -> list:
    dataset = ogr.Open(str(path))
    layer = dataset.GetLayer()
    geometries = []
    for feature in layer:
        geometries.append(feature.GetGeometryRef().Clone())
    dataset = None
    return geometries


def write_polygons(path: Path, polygons: list) -> None:
    if path.exists():
        path.unlink()
    driver = ogr.GetDriverByName("GeoJSON")
    dataset = driver.CreateDataSource(str(path))
    layer = dataset.CreateLayer("mainland", geom_type=ogr.wkbMultiPolygon)
    feature = ogr.Feature(layer.GetLayerDefn())
    multi = ogr.Geometry(ogr.wkbMultiPolygon)
    for polygon in polygons:
        multi.AddGeometry(polygon)
    feature.SetGeometry(multi)
    layer.CreateFeature(feature)
    dataset = None


def pixel_index(path: Path, lon: float, lat: float) -> tuple[gdal.Dataset, int, int]:
    dataset = gdal.Open(str(path))
    transform = dataset.GetGeoTransform()
    column = int((lon - transform[0]) / transform[1])
    row = int((lat - transform[3]) / transform[5])
    if not (0 <= column < dataset.RasterXSize and 0 <= row < dataset.RasterYSize):
        dataset = None
        raise ValueError(f"{lon}, {lat} is outside {path.name}")
    return dataset, column, row


def read_pixel(path: Path, lon: float, lat: float) -> int:
    dataset, column, row = pixel_index(path, lon, lat)
    value = dataset.GetRasterBand(1).ReadRaster(column, row, 1, 1)
    dataset = None
    return value[0]


def read_block(path: Path, lon: float, lat: float, size: int = 64) -> bytes:
    """A square of source pixels centred on a point. Used to see a grid shift."""
    dataset, column, row = pixel_index(path, lon, lat)
    column -= size // 2
    row -= size // 2
    if column < 0 or row < 0 or column + size > dataset.RasterXSize or row + size > dataset.RasterYSize:
        dataset = None
        raise ValueError(f"block at {lon}, {lat} does not fit in {path.name}")
    block = dataset.GetRasterBand(1).ReadRaster(column, row, size, size)
    dataset = None
    return block


def clip_tiles(tile_paths: list[Path], boundary: Path, dest: Path, nodata: int, pixel: float) -> None:
    work = dest.parent
    cutline = work / "mainland.geojson"
    kept = polygons_meeting(read_geometries(boundary), union_envelope(tile_paths))
    if not kept:
        raise ValueError("no boundary polygon meets the tiles")
    write_polygons(cutline, kept)
    vrt = work / "tiles.vrt"
    gdal.BuildVRT(str(vrt), [str(path) for path in tile_paths])
    gdal.SetCacheMax(512 * 1024 * 1024)
    if dest.exists():
        dest.unlink()
    last_percent = {"value": -1}

    def progress(complete, _message, _data):
        percent = int(complete * 20) * 5
        if percent != last_percent["value"]:
            last_percent["value"] = percent
            print(f"warp {percent}%", flush=True)
        return 1

    gdal.Warp(
        str(dest),
        str(vrt),
        format="GTiff",
        cutlineDSName=str(cutline),
        cropToCutline=True,
        dstNodata=nodata,
        resampleAlg="near",
        xRes=pixel,
        yRes=pixel,
        targetAlignedPixels=True,
        outputType=gdal.GDT_Byte,
        multithread=True,
        creationOptions=[
            "COMPRESS=LZW",
            "TILED=YES",
            "BIGTIFF=IF_SAFER",
            "NUM_THREADS=ALL_CPUS",
        ],
        warpOptions=["NUM_THREADS=ALL_CPUS"],
        callback=progress,
    )


def gs_join(prefix: str, name: str) -> str:
    if not prefix.endswith("/"):
        prefix += "/"
    return prefix + name


def upload(local: Path, destination: str) -> None:
    subprocess.run(
        ["gcloud", "storage", "cp", str(local), destination, f"--project={PROJECT}"],
        check=True,
    )


def peak_memory_bytes() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss
    return rss * 1024


def print_raster(name: str, info: RasterInfo) -> None:
    codes = ", ".join(f"{value} ({count})" for value, count in sorted(info.counts.items()))
    print(
        f"{name}: {info.crs} {info.pixel_size_deg}° {info.dtype} "
        f"nodata={info.nodata} origin=({info.origin_x}, {info.origin_y})"
    )
    print(f"  values: {codes}")


def require_confirmed(recipe: dict) -> None:
    pending = pending_values(recipe)
    if pending:
        joined = ", ".join(pending)
        raise SystemExit(
            f"Recipe still has {joined} = {PENDING}. "
            "Run sample, record those codes in the recipe, then fetch the rest."
        )


def command_sample(recipe: dict, work: Path) -> RasterInfo:
    name = recipe["seed"]["tiles"][0]
    path = download(tile_url(recipe, name), work / name)
    info = inspect_raster(path)
    print_raster(name, info)
    pending = pending_values(recipe)
    if pending:
        print(f"Record seed.values for {', '.join(pending)} from this tile before downloading the rest.")
    return info


def command_fetch(recipe: dict, work: Path) -> list[Path]:
    require_confirmed(recipe)
    paths = []
    infos = []
    for name in recipe["seed"]["tiles"]:
        path = download(tile_url(recipe, name), work / name)
        info = inspect_raster(path)
        problems = check_tile(info, recipe)
        print_raster(name, info)
        if problems:
            raise SystemExit(f"{name} failed the recipe check:\n" + "\n".join(problems))
        paths.append(path)
        infos.append(info)
    problems = check_tiles(infos, recipe)
    if problems:
        raise SystemExit("Downloaded tiles failed the recipe check:\n" + "\n".join(problems))
    print(f"checked {len(paths)} tiles")
    return paths


def command_clip(recipe: dict, work: Path, tiles: list[Path]) -> Path:
    require_confirmed(recipe)
    clip = recipe["seed"]["clip"]
    boundary = download(clip["boundary"]["url"], work / "boundary.geojson")
    dest = work / clip["file"]
    pixel = float(recipe["grid"]["pixel_size_deg"])
    print(f"clipping to {clip['boundary']['name']}", flush=True)
    clip_tiles(tiles, boundary, dest, int(clip["nodata"]), pixel)
    info = inspect_raster(dest)
    print_raster(dest.name, info)
    problems = check_clip(info, recipe)
    if problems:
        raise SystemExit("Clip failed the recipe check:\n" + "\n".join(problems))
    # CHECK_LON/CHECK_LAT sit in this tile, inland, so the block is not on the cutline.
    source = work / "20S_020E.tif"
    source_block = read_block(source, CHECK_LON, CHECK_LAT)
    clip_block = read_block(dest, CHECK_LON, CHECK_LAT)
    if source_block != clip_block:
        raise SystemExit(
            f"64×64 block at {CHECK_LON}, {CHECK_LAT} changed between the tile and the clip"
        )
    print(f"64×64 block at {CHECK_LON}, {CHECK_LAT} matches the source tile")
    return dest


def command_upload(recipe: dict, tiles: list[Path], clip_path: Path) -> None:
    prefix = recipe["seed"]["path"]
    for path in tiles:
        upload(path, gs_join(prefix, path.name))
    upload(clip_path, gs_join(prefix, clip_path.name))
    print("uploaded", flush=True)
    subprocess.run(
        ["gcloud", "storage", "ls", "-l", prefix, f"--project={PROJECT}"],
        check=True,
    )


@dataclass(frozen=True)
class TilePlan:
    """Where to read one tile, and how to leave it as a flat file name."""

    tile: str
    read_name: str
    move_to: str | None
    delete_name: str | None


def transferred_object_name(url: str) -> str:
    """Temporary object name Storage Transfer writes for one URL-list entry.

    The service names the object as the host plus the URL path. The check
    moves a tile that passes to the file name alone and deletes this path.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or not parsed.path:
        raise ValueError(f"not an http url: {url}")
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return f"{host}/{parsed.path.lstrip('/')}"


def transfer_names(recipe: dict) -> set[str]:
    return {transferred_object_name(tile_url(recipe, name)) for name in recipe["seed"]["tiles"]}


def tile_plan(tile: str, url: str, present: set[str]) -> TilePlan:
    """Read the host path until the flat file exists. A failed tile is not moved."""
    if "/" in tile or tile in ("", ".", ".."):
        raise ValueError(f"tile name is not a flat file name: {tile}")
    nested = transferred_object_name(url)
    has_flat = tile in present
    has_nested = nested in present
    if not has_flat and not has_nested:
        raise SystemExit(f"missing {tile}")
    if has_flat and has_nested:
        return TilePlan(tile, tile, None, nested)
    if has_flat:
        return TilePlan(tile, tile, None, None)
    return TilePlan(tile, nested, tile, None)


def missing_tiles(recipe: dict, present: set[str]) -> list[str]:
    missing = []
    for tile in recipe["seed"]["tiles"]:
        nested = transferred_object_name(tile_url(recipe, tile))
        if tile not in present and nested not in present:
            missing.append(tile)
    return missing


def blocking_names(recipe: dict, present: set[str]) -> list[str]:
    """Flat or host-path copies that should stop a second transfer."""
    found = []
    for tile in recipe["seed"]["tiles"]:
        nested = transferred_object_name(tile_url(recipe, tile))
        if tile in present:
            found.append(tile)
        if nested in present:
            found.append(nested)
    return found


def url_list_tsv(recipe: dict) -> str:
    """URL list for Storage Transfer. URLs are in UTF-8 lexicographic order."""
    urls = sorted(tile_url(recipe, name) for name in recipe["seed"]["tiles"])
    return "TsvHttpData-1.0\n" + "\n".join(urls) + "\n"


def relative_names(listing: str, prefix: str) -> set[str]:
    prefix = seed_prefix(prefix)
    names = set()
    for line in listing.splitlines():
        url = line.strip()
        if url.startswith(prefix) and not url.endswith("/"):
            names.add(url[len(prefix) :])
    return names


def already_stored(listing: str, prefix: str, names: set[str]) -> list[str]:
    return sorted(relative_names(listing, prefix) & names)


def list_prefix(prefix: str) -> str:
    result = subprocess.run(
        ["gcloud", "storage", "ls", "--recursive", seed_prefix(prefix), f"--project={PROJECT}"],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        if "matched no objects" in result.stderr:
            return ""
        raise SystemExit(result.stderr.strip() or f"could not list {prefix}")
    return result.stdout


def transfer_create_args(list_object: str, destination: str) -> list[str]:
    return [
        "transfer",
        "jobs",
        "create",
        list_object,
        seed_prefix(destination),
        f"--name={TRANSFER_JOB}",
        "--description=GLAD cropland 2024 global seed from cropland-2024-v2",
        "--overwrite-when=never",
    ]


def transfer_agent_grant_args() -> list[str]:
    """Let the transfer agent read the URL list and write this bucket.

    The job destination is the version 2 prefix. The command refuses the
    version 1 path before it grants this role.
    """
    return [
        "storage",
        "buckets",
        "add-iam-policy-binding",
        "gs://nature-watch-bucket",
        f"--member=serviceAccount:{TRANSFER_AGENT}",
        "--role=roles/storage.legacyBucketWriter",
    ]


def print_transfer_followup() -> None:
    print(
        f"status: gcloud transfer jobs describe {TRANSFER_JOB} --project={PROJECT}",
        flush=True,
    )
    print(
        "operations: gcloud transfer operations list "
        f"--job-names={TRANSFER_JOB} --project={PROJECT}",
        flush=True,
    )
    print(
        "cancel operation: gcloud transfer operations cancel OPERATION "
        f"--project={PROJECT}",
        flush=True,
    )
    print(
        f"delete job: gcloud transfer jobs delete {TRANSFER_JOB} --project={PROJECT}",
        flush=True,
    )


def print_check_followup() -> None:
    print(
        "status: gcloud batch jobs describe "
        f"{CHECK_JOB} --location={REGION} --project={PROJECT}",
        flush=True,
    )
    print(
        "cancel: gcloud batch jobs delete "
        f"{CHECK_JOB} --location={REGION} --project={PROJECT}",
        flush=True,
    )


def split_gs(gs_path: str) -> tuple[str, str]:
    if not gs_path.startswith("gs://"):
        raise ValueError(f"not a gs path: {gs_path}")
    bucket, _, name = gs_path[len("gs://") :].partition("/")
    if not bucket or not name:
        raise ValueError(f"not a gs object: {gs_path}")
    return bucket, name


def rewrite_url(bucket: str, source: str, dest: str, rewrite_token: str | None) -> str:
    """Same-bucket rewrite that fails if the flat name already exists."""
    url = (
        "https://storage.googleapis.com/storage/v1/b/"
        + quote(bucket, safe="")
        + "/o/"
        + quote(source, safe="")
        + "/rewriteTo/b/"
        + quote(bucket, safe="")
        + "/o/"
        + quote(dest, safe="")
        + "?ifGenerationMatch=0"
    )
    if rewrite_token:
        url += "&rewriteToken=" + quote(rewrite_token, safe="")
    return url


def delete_url(bucket: str, name: str) -> str:
    return (
        "https://storage.googleapis.com/storage/v1/b/"
        + quote(bucket, safe="")
        + "/o/"
        + quote(name, safe="")
    )


def list_page_url(bucket: str, prefix: str, page_token: str | None) -> str:
    url = (
        "https://storage.googleapis.com/storage/v1/b/"
        + quote(bucket, safe="")
        + "/o?prefix="
        + quote(prefix, safe="")
        + "&fields=items/name,nextPageToken"
    )
    if page_token:
        url += "&pageToken=" + quote(page_token, safe="")
    return url


def names_from_list_page(payload: dict, prefix: str) -> set[str]:
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    names = set()
    for item in payload.get("items") or []:
        name = item["name"]
        if name.startswith(prefix):
            names.add(name[len(prefix) :])
    return names


def metadata_token() -> str:
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["access_token"]


def gcs_api(method: str, url: str, token: str, body: bytes | None = None) -> dict:
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = response.read()
    if not payload:
        return {}
    return json.loads(payload)


def list_gcs_names(gs_prefix: str, token: str) -> set[str]:
    bucket, prefix = split_gs(seed_prefix(gs_prefix))
    names: set[str] = set()
    page = None
    while True:
        payload = gcs_api("GET", list_page_url(bucket, prefix, page), token)
        names.update(names_from_list_page(payload, prefix))
        page = payload.get("nextPageToken")
        if not page:
            return names


def delete_gcs_object(gs_path: str, token: str) -> None:
    bucket, name = split_gs(gs_path)
    gcs_api("DELETE", delete_url(bucket, name), token)


def move_gcs_object(source: str, dest: str, token: str) -> None:
    """Rewrite one object onto its flat name, then delete the host path."""
    bucket, source_name = split_gs(source)
    dest_bucket, dest_name = split_gs(dest)
    if bucket != dest_bucket:
        raise ValueError(f"refusing to move across buckets: {source} -> {dest}")
    rewrite_token = None
    while True:
        payload = gcs_api(
            "POST",
            rewrite_url(bucket, source_name, dest_name, rewrite_token),
            token,
            body=b"{}",
        )
        if payload.get("done"):
            break
        rewrite_token = payload.get("rewriteToken")
        if not rewrite_token:
            raise OSError(f"rewrite of {source} did not finish")
    delete_gcs_object(source, token)


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


def batch_job(script_prefix: str) -> dict:
    """One spot VM in europe-west1. Batch deletes it when the task ends.

    The script and recipe are copied to the scratch prefix before submit.
    The container downloads them and checks one tile at a time. A Cloud
    Storage mount is not used: the Batch host's root is read-only.
    """
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
python3 /tmp/in/cropland.py verify --recipe /tmp/in/recipe.json --work /tmp/work
"""
    return {
        "taskGroups": [
            {
                "taskCount": "1",
                "taskSpec": {
                    "maxRunDuration": CHECK_RUN_SECONDS,
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


def command_transfer(recipe: dict, work: Path) -> None:
    require_transfer_recipe(recipe)
    work.mkdir(parents=True, exist_ok=True)
    tsv_path = work / "urls.tsv"
    tsv_path.write_text(url_list_tsv(recipe))
    destination = seed_prefix(recipe["seed"]["path"])
    present = relative_names(list_prefix(destination), destination)
    stored = blocking_names(recipe, present)
    if stored:
        preview = ", ".join(stored[:3])
        raise SystemExit(
            f"{len(stored)} tiles are already in {destination} ({preview}). "
            "The transfer does not overwrite them."
        )
    gcloud(["services", "enable", "storagetransfer.googleapis.com"])
    gcloud(transfer_agent_grant_args())
    list_object = gs_join(SCRATCH, "urls.tsv")
    upload(tsv_path, list_object)
    print(" ".join(["gcloud", *transfer_create_args(list_object, destination)]), flush=True)
    gcloud(transfer_create_args(list_object, destination))
    print(f"submitted {TRANSFER_JOB}", flush=True)
    print(f"url list {list_object}", flush=True)
    print(f"destination {destination}", flush=True)
    print_transfer_followup()


def command_verify(recipe: dict, work: Path) -> None:
    """Check every listed tile. The first failure stops the run. No clip is written."""
    if os.environ.get(BATCH_ENV) != "1":
        raise SystemExit(
            "The check runs next to the bucket in europe-west1. "
            "From this computer, use: python seed/cropland.py check"
        )
    require_transfer_recipe(recipe)
    work.mkdir(parents=True, exist_ok=True)
    gdal.SetCacheMax(512 * 1024 * 1024)
    infos = []
    destination = recipe["seed"]["path"]
    tiles = recipe["seed"]["tiles"]
    present = list_gcs_names(destination, metadata_token())
    plans = [tile_plan(name, tile_url(recipe, name), present) for name in tiles]
    for index, plan in enumerate(plans, start=1):
        source = gs_join(destination, plan.read_name)
        dest = work / plan.tile
        try:
            print(f"read {index}/{len(tiles)} {source}", flush=True)
            vsi_copy(gs_to_vsi(source), str(dest))
            info = inspect_raster(dest)
        finally:
            if dest.exists():
                dest.unlink()
        problems = check_tile(info, recipe)
        print_raster(plan.tile, info)
        if problems:
            raise SystemExit(f"{plan.tile} failed the recipe check:\n" + "\n".join(problems))
        if plan.move_to or plan.delete_name:
            token = metadata_token()
            if plan.move_to:
                flat = gs_join(destination, plan.move_to)
                move_gcs_object(source, flat, token)
                print(f"moved {plan.tile} to {flat}", flush=True)
            else:
                delete_gcs_object(gs_join(destination, plan.delete_name), token)
                print(f"removed host path for {plan.tile}", flush=True)
        infos.append(info)
    problems = check_tiles(infos, recipe)
    if problems:
        raise SystemExit("Tiles failed the recipe check:\n" + "\n".join(problems))
    print(f"checked {len(infos)} tiles", flush=True)


def command_check(recipe: dict) -> None:
    require_transfer_recipe(recipe)
    destination = seed_prefix(recipe["seed"]["path"])
    present = relative_names(list_prefix(destination), destination)
    missing = missing_tiles(recipe, present)
    if missing:
        raise SystemExit(
            f"{len(missing)} tiles are not in {destination} yet. "
            "Wait for the transfer to finish before the check."
        )
    gcloud(["services", "enable", "batch.googleapis.com"])
    work = ROOT / "data" / "seed" / recipe["id"]
    work.mkdir(parents=True, exist_ok=True)
    recipe_json = work / "recipe.json"
    recipe_json.write_text(json.dumps(recipe) + "\n")
    upload(Path(__file__).resolve(), gs_join(SCRATCH, "cropland.py"))
    upload(recipe_json, gs_join(SCRATCH, "recipe.json"))
    config = batch_job(SCRATCH)
    config_path = work / "batch_job.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    print(config_path.read_text(), flush=True)
    gcloud(["batch", "jobs", "submit", CHECK_JOB, f"--location={REGION}", f"--config={config_path}"])
    print(f"submitted {CHECK_JOB} in {REGION}", flush=True)
    print_check_followup()


def gcloud(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gcloud", *args, f"--project={PROJECT}"], check=True, text=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("sample", "fetch", "clip", "upload", "run", "transfer", "check", "verify"),
    )
    parser.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
    parser.add_argument("--work", type=Path, default=None)
    args = parser.parse_args()
    started = time.perf_counter()
    recipe = load_recipe(args.recipe)
    if args.command in ("sample", "fetch", "clip", "upload", "run"):
        require_laptop_recipe(recipe)
    if args.work is not None:
        work = args.work
    elif args.command in ("transfer", "check", "verify"):
        work = ROOT / "data" / "seed" / recipe["id"]
    else:
        work = DEFAULT_WORK

    if args.command == "sample":
        work.mkdir(parents=True, exist_ok=True)
        command_sample(recipe, work)
    elif args.command == "fetch":
        work.mkdir(parents=True, exist_ok=True)
        command_fetch(recipe, work)
    elif args.command == "clip":
        work.mkdir(parents=True, exist_ok=True)
        tiles = [work / name for name in recipe["seed"]["tiles"]]
        missing = [path.name for path in tiles if not path.exists()]
        if missing:
            raise SystemExit(f"missing tiles, run fetch first: {', '.join(missing)}")
        command_clip(recipe, work, tiles)
    elif args.command == "upload":
        tiles = [work / name for name in recipe["seed"]["tiles"]]
        clip_path = work / recipe["seed"]["clip"]["file"]
        command_upload(recipe, tiles, clip_path)
    elif args.command == "transfer":
        command_transfer(recipe, work)
    elif args.command == "check":
        command_check(recipe)
    elif args.command == "verify":
        command_verify(recipe, work)
    else:
        work.mkdir(parents=True, exist_ok=True)
        tiles = command_fetch(recipe, work)
        clip_path = command_clip(recipe, work, tiles)
        command_upload(recipe, tiles, clip_path)

    elapsed = time.perf_counter() - started
    print(f"wall_seconds {elapsed:.1f}")
    print(f"peak_memory_bytes {peak_memory_bytes()}")


if __name__ == "__main__":
    main()
