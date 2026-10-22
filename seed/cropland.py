#!/usr/bin/env python3
"""Download and check the GLAD cropland tiles named in a cookbook recipe.

Open one tile and record its source codes in the recipe before fetching the
rest. This script does not edit the recipe. It refuses a full download while
any seed value is still "confirm-from-one-tile".

The South Africa clip keeps GLAD's codes inside the mainland boundary and
writes the clip's nodata value outside it. The sprout step remaps those codes.

Examples
  python seed/cropland.py sample
  python seed/cropland.py run
"""
from __future__ import annotations

import argparse
import resource
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml
from osgeo import gdal, ogr, osr

gdal.UseExceptions()
ogr.UseExceptions()

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECIPE = ROOT / "cookbook" / "recipes" / "cropland-2024.yaml"
DEFAULT_WORK = ROOT / "data" / "seed" / "cropland-2024"
PROJECT = "nature-watch-387210"
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
    return yaml.safe_load(path.read_text())


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("sample", "fetch", "clip", "upload", "run"))
    parser.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    args = parser.parse_args()
    started = time.perf_counter()
    recipe = load_recipe(args.recipe)
    work = args.work
    work.mkdir(parents=True, exist_ok=True)

    if args.command == "sample":
        command_sample(recipe, work)
    elif args.command == "fetch":
        command_fetch(recipe, work)
    elif args.command == "clip":
        tiles = [work / name for name in recipe["seed"]["tiles"]]
        missing = [path.name for path in tiles if not path.exists()]
        if missing:
            raise SystemExit(f"missing tiles, run fetch first: {', '.join(missing)}")
        command_clip(recipe, work, tiles)
    elif args.command == "upload":
        tiles = [work / name for name in recipe["seed"]["tiles"]]
        clip_path = work / recipe["seed"]["clip"]["file"]
        command_upload(recipe, tiles, clip_path)
    else:
        tiles = command_fetch(recipe, work)
        clip_path = command_clip(recipe, work, tiles)
        command_upload(recipe, tiles, clip_path)

    elapsed = time.perf_counter() - started
    print(f"wall_seconds {elapsed:.1f}")
    print(f"peak_memory_bytes {peak_memory_bytes()}")


if __name__ == "__main__":
    main()
