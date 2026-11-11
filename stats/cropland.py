#!/usr/bin/env python3
"""Count cropland 2024 inside each South Africa conservation area and store the stats.

The recipe is the catalog. This script reads it and does not edit it.
Each place gets crops_area, crops_percent, and valid_coverage in place_stats,
at full precision, on its current boundary, in one stat_runs row.
The published run is a Batch job in europe-west1. The laptop only submits
that job. The Stats Pipeline is paused: do not submit until it resumes.

  python stats/cropland.py submit
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import resource
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import psycopg
from exactextract import exact_extract
from exactextract.feature import JSONFeatureSource
from exactextract.raster import NumPyRasterSource
from osgeo import gdal, osr
from psycopg.rows import dict_row
from pyproj import Geod
from shapely import from_wkb, make_valid, unary_union
from shapely.geometry import MultiPolygon, Point, box, mapping
from shapely.prepared import prep

gdal.UseExceptions()

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECIPE = ROOT / "cookbook" / "recipes" / "cropland-2024.yaml"
PROJECT = "nature-watch-387210"
REGION = "europe-west1"
IMAGE = "ghcr.io/osgeo/gdal:ubuntu-small-3.11.4"
MACHINE = "e2-standard-4"
SERVICE_ACCOUNT = "637146989512-compute@developer.gserviceaccount.com"
SCRATCH = "gs://nature-watch-bucket/scratch/stats/cropland-2024/"
SECRET = "naturewatch-facts-database-url"
BATCH_ENV = "NATUREWATCH_STATS_BATCH"
MAX_WINDOW_PIXELS = 8_000_000
INTERSECTION_TOLERANCE_KM2 = 0.01
PIXEL_CENTER_RELATIVE_TOLERANCE = 0.01
# The five largest places dominate when they take at least half the counting time.
DOMINANCE_SHARE = 0.5

METHOD_ID = "area-weighted-spheroid-v1"
METHOD_SUMMARY = (
    "Area-weighted count of the sprout's full-resolution pixels. "
    "A pixel contributes the fraction of its area inside the place, multiplied by the WGS84 spheroidal area of the whole pixel. "
    "area_km2 sums value 1. "
    "percent is 100 times area_km2 divided by the area of values 0 and 1, or 0 when that area is 0. "
    "valid_coverage is that valid area divided by the place area. "
    "Nodata and pixels outside the raster are not valid. "
    "Each place is counted alone, and ground shared by two places is kept in both. "
    "Stored numbers are full precision. "
    "Shown square kilometres use two decimal places (one hectare), percent uses one decimal place, and valid coverage uses three decimal places."
)

# The current boundary of each published conservation area listed for South Africa.
# listed_places is the app's list rule: terrestrial, at least 10 km2.
PILOT_SQL = """
select
  listed.id::text as id,
  boundary.id::text as boundary_id,
  listed.name,
  boundary.area_km2,
  extensions.ST_AsEWKB(
    extensions.ST_Multi(
      extensions.ST_CollectionExtract(
        extensions.ST_MakeValid(boundary.geom),
        3
      )
    )
  ) as geom
from public.listed_places as listed
join public.place_boundaries as boundary
  on boundary.place_id = listed.id
 and boundary.is_current
where listed.place_type_id = 'conservation_area'
  and listed.iso3 = 'ZAF'
order by boundary.area_km2, listed.id
"""

SELECTION = {"place_type": "conservation_area", "iso3": "ZAF", "list": "listed_places"}

# The sprout and the method are registered by the app's migrations, not here.
SPROUT_SQL = """
select period_start, period_end
from public.sprouts
where id = %s
"""

METHOD_SQL = """
select summary
from public.stat_methods
where id = %s
"""

INSERT_RUN = """
insert into public.stat_runs (
  sprout_id, stat_method_id, selection, boundary_count, code_version,
  batch_job_id, status, started_at
) values (
  %s, %s, %s::jsonb, %s, %s, %s, 'running', now()
)
returning id::text as id
"""

INSERT_STAT = """
insert into public.place_stats (
  stat_run_id, sprout_id, stat_method_id, place_id, boundary_id, stat_id,
  period_start, period_end, value, calculated_at
) values (
  %s::uuid, %s, %s, %s::uuid, %s::uuid, %s, %s, %s, %s, now()
)
"""

FINISH_RUN = """
update public.stat_runs
set status = %s, finished_at = now()
where id = %s::uuid
  and status = 'running'
"""

COUNT_STATS = """
select count(*) as stored
from public.place_stats
where stat_run_id = %s::uuid
"""

STAT_IDS = ("crops_area", "crops_percent", "valid_coverage")

_GEOD = Geod(ellps="WGS84")


@dataclass(frozen=True)
class ClassCodes:
    cropland: int
    nodata: int
    allowed: frozenset[int]


@dataclass(frozen=True)
class Place:
    id: str
    boundary_id: str
    name: str
    area_km2: float
    geom: MultiPolygon


@dataclass(frozen=True)
class PlaceResult:
    place: Place
    cropland_km2: float
    valid_km2: float
    seconds: float


def load_recipe(path: Path) -> dict:
    text = path.read_text()
    if path.suffix == ".json":
        return json.loads(text)
    import yaml

    return yaml.safe_load(text)


def version_of(recipe: dict) -> str:
    folder = recipe["sprout"]["path"].rstrip("/").split("/")[-2]
    if len(folder) < 2 or folder[0] != "v" or not folder[1:].isdigit():
        raise ValueError(f"sprout path has no version folder: {recipe['sprout']['path']}")
    return folder


def sprout_id(recipe: dict) -> str:
    sprout = recipe["sprout"]["id"]
    version = version_of(recipe)
    if not sprout.endswith(f"-{version}"):
        raise ValueError(f"sprout id {sprout} does not end with {version}")
    return sprout


def class_codes(recipe: dict) -> ClassCodes:
    values = {name: int(value) for name, value in recipe["sprout"]["values"].items()}
    return ClassCodes(values["cropland"], values["nodata"], frozenset(values.values()))


def parse_class_values(text: str) -> dict[str, int]:
    parsed = {}
    for part in text.split(","):
        name, value = part.split(":")
        parsed[name] = int(value)
    return parsed


def wgs84_wkt() -> str:
    spatial = osr.SpatialReference()
    spatial.ImportFromEPSG(4326)
    spatial.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return spatial.ExportToWkt()


def spatial_reference(wkt: str) -> str:
    spatial = osr.SpatialReference()
    spatial.ImportFromWkt(wkt)
    name = spatial.GetAuthorityName(None) or spatial.GetAuthorityName("GEOGCS")
    code = spatial.GetAuthorityCode(None) or spatial.GetAuthorityCode("GEOGCS")
    if not name or not code:
        raise ValueError(f"raster CRS has no EPSG code: {wkt}")
    return f"{name}:{code}"


def pixel_area_km2(lon_width: float, lat_south: float, lat_north: float) -> float:
    """WGS84 spheroidal area of one pixel. Longitude does not change the area."""
    area_m2, _perimeter = _GEOD.polygon_area_perimeter(
        [0.0, lon_width, lon_width, 0.0],
        [lat_south, lat_south, lat_north, lat_north],
    )
    return abs(area_m2) / 1e6


def stat_values(cropland_km2: float, valid_km2: float, place_area_km2: float) -> tuple[float, float, float]:
    """Full-precision area_km2, percent, and valid_coverage. Nothing is rounded."""
    percent = 0.0 if valid_km2 == 0 else 100.0 * cropland_km2 / valid_km2
    coverage = 0.0 if place_area_km2 == 0 else valid_km2 / place_area_km2
    return cropland_km2, percent, coverage


def polygonal(geom):
    geom = make_valid(geom)
    if geom.geom_type == "GeometryCollection":
        parts = [part for part in geom.geoms if part.geom_type in ("Polygon", "MultiPolygon")]
        geom = unary_union(parts) if parts else geom
    if geom.geom_type == "Polygon":
        geom = MultiPolygon([geom])
    if geom.geom_type != "MultiPolygon" or geom.is_empty:
        kind = "empty" if geom.is_empty else geom.geom_type
        raise ValueError(f"geometry is {kind}")
    return geom


def iter_strips(n_rows: int, width: int, max_pixels: int):
    if width > max_pixels:
        raise ValueError(f"window is {width} pixels wide, above the {max_pixels} pixel strip limit")
    step = max(1, max_pixels // width)
    start = 0
    while start < n_rows:
        take = min(step, n_rows - start)
        yield start, take
        start += take


def window_limits(dataset, polygon):
    transform = dataset.GetGeoTransform()
    if transform[2] != 0 or transform[4] != 0:
        raise ValueError("sprout raster is rotated or sheared")
    if transform[1] <= 0 or transform[5] >= 0:
        raise ValueError("sprout raster is not a north-up grid")
    minx, miny, maxx, maxy = polygon.bounds
    origin_x, pixel_x, _x_skew, origin_y, _y_skew, pixel_y = transform
    col0 = math.floor((minx - origin_x) / pixel_x)
    col1 = math.ceil((maxx - origin_x) / pixel_x)
    row0 = math.floor((maxy - origin_y) / pixel_y)
    row1 = math.ceil((miny - origin_y) / pixel_y)
    col0 = max(0, col0)
    row0 = max(0, row0)
    col1 = min(dataset.RasterXSize, col1)
    row1 = min(dataset.RasterYSize, row1)
    return col0, row0, col1, row1, transform


def strip_extent(transform, col: int, row: int, cols: int, rows: int):
    origin_x, pixel_x, _x_skew, origin_y, _y_skew, pixel_y = transform
    xmin = origin_x + col * pixel_x
    xmax = xmin + cols * pixel_x
    ymax = origin_y + row * pixel_y
    ymin = ymax + rows * pixel_y
    return xmin, ymin, xmax, ymax


def read_strip(band, col: int, row: int, cols: int, rows: int, nodata: int) -> np.ndarray:
    raw = band.ReadAsArray(col, row, cols, rows)
    if raw is None:
        raise RuntimeError("failed to read a sprout window")
    if np.ma.isMaskedArray(raw):
        raw = raw.filled(nodata)
    return np.asarray(raw)


def require_allowed(values: np.ndarray, codes: ClassCodes) -> None:
    allowed = np.array(sorted(codes.allowed), dtype=values.dtype)
    if np.any(~np.isin(values, allowed)):
        found = sorted(set(np.unique(values).tolist()) - set(codes.allowed))
        raise ValueError(f"sprout window has values {found}")


def make_stats(codes: ClassCodes):
    def cropland_km2(values, coverage, weights):
        data = np.ma.asarray(values)
        keep = np.asarray(data.filled(-1) == codes.cropland)
        return float(np.sum(np.asarray(coverage)[keep] * np.asarray(np.ma.asarray(weights))[keep]))

    def valid_km2(values, coverage, weights):
        data = np.ma.asarray(values)
        keep = ~np.ma.getmaskarray(data)
        return float(np.sum(np.asarray(coverage)[keep] * np.asarray(np.ma.asarray(weights))[keep]))

    return cropland_km2, valid_km2


def _stat_number(properties: dict, name: str) -> float:
    value = properties.get(name)
    if value is None:
        return 0.0
    return float(value)


def summarize_arrays(values: np.ndarray, extent, polygon, codes: ClassCodes, srs_wkt: str) -> tuple[float, float]:
    """Area-weighted sums for one north-up window. extent is xmin, ymin, xmax, ymax."""
    height, width = values.shape
    if height == 0 or width == 0:
        return 0.0, 0.0
    require_allowed(values, codes)
    xmin, ymin, xmax, ymax = extent
    pixel_x = (xmax - xmin) / width
    row_height = (ymax - ymin) / height
    weights = np.empty((height, width), dtype=np.float64)
    for row in range(height):
        north = ymax - row * row_height
        south = north - row_height
        weights[row, :] = pixel_area_km2(pixel_x, south, north)
    raster = NumPyRasterSource(values, xmin, ymin, xmax, ymax, nodata=codes.nodata, srs_wkt=srs_wkt)
    weight_raster = NumPyRasterSource(weights, xmin, ymin, xmax, ymax, srs_wkt=srs_wkt)
    feature = {"type": "Feature", "properties": {}, "geometry": mapping(polygon)}
    features = JSONFeatureSource([feature], srs_wkt=srs_wkt)
    cropland_stat, valid_stat = make_stats(codes)
    result = exact_extract(raster, features, [cropland_stat, valid_stat], weights=weight_raster)
    properties = result[0]["properties"]
    return _stat_number(properties, "cropland_km2"), _stat_number(properties, "valid_km2")


def summarize_reference(values: np.ndarray, extent, polygon, codes: ClassCodes) -> tuple[float, float]:
    """The same sums from polygon intersection, used to check exactextract."""
    height, width = values.shape
    if height == 0 or width == 0:
        return 0.0, 0.0
    require_allowed(values, codes)
    xmin, ymin, xmax, ymax = extent
    pixel_x = (xmax - xmin) / width
    row_height = (ymax - ymin) / height
    prepared = prep(polygon)
    cropland = 0.0
    valid = 0.0
    for row in range(height):
        north = ymax - row * row_height
        south = north - row_height
        area = pixel_area_km2(pixel_x, south, north)
        for col in range(width):
            value = int(values[row, col])
            if value == codes.nodata:
                continue
            west = xmin + col * pixel_x
            cell = box(west, south, west + pixel_x, north)
            if not prepared.intersects(cell):
                continue
            fraction = polygon.intersection(cell).area / cell.area
            fraction = min(1.0, max(0.0, fraction))
            if fraction == 0.0:
                continue
            part = fraction * area
            valid += part
            if value == codes.cropland:
                cropland += part
    return cropland, valid


def summarize_pixel_center(values: np.ndarray, extent, polygon, codes: ClassCodes) -> tuple[float, float]:
    """Full pixel area where the centre is inside the place."""
    height, width = values.shape
    if height == 0 or width == 0:
        return 0.0, 0.0
    require_allowed(values, codes)
    xmin, ymin, xmax, ymax = extent
    pixel_x = (xmax - xmin) / width
    row_height = (ymax - ymin) / height
    prepared = prep(polygon)
    cropland = 0.0
    valid = 0.0
    for row in range(height):
        north = ymax - row * row_height
        south = north - row_height
        area = pixel_area_km2(pixel_x, south, north)
        center_y = (south + north) / 2
        for col in range(width):
            value = int(values[row, col])
            if value == codes.nodata:
                continue
            west = xmin + col * pixel_x
            if not prepared.covers(Point(west + pixel_x / 2, center_y)):
                continue
            valid += area
            if value == codes.cropland:
                cropland += area
    return cropland, valid


def walk_windows(dataset, polygon, codes: ClassCodes, max_pixels: int, reduce_window):
    col0, row0, col1, row1, transform = window_limits(dataset, polygon)
    if col1 <= col0 or row1 <= row0:
        return 0.0, 0.0
    band = dataset.GetRasterBand(1)
    width = col1 - col0
    cropland = 0.0
    valid = 0.0
    for row, nrows in iter_strips(row1 - row0, width, max_pixels):
        values = read_strip(band, col0, row0 + row, width, nrows, codes.nodata)
        extent = strip_extent(transform, col0, row0 + row, width, nrows)
        part_cropland, part_valid = reduce_window(values, extent)
        cropland += part_cropland
        valid += part_valid
    return cropland, valid


def summarize_exact(dataset, polygon, codes: ClassCodes, srs_wkt: str, max_pixels: int = MAX_WINDOW_PIXELS):
    return walk_windows(
        dataset,
        polygon,
        codes,
        max_pixels,
        lambda values, extent: summarize_arrays(values, extent, polygon, codes, srs_wkt),
    )


def summarize_reference_dataset(dataset, polygon, codes: ClassCodes, max_pixels: int = MAX_WINDOW_PIXELS):
    return walk_windows(
        dataset,
        polygon,
        codes,
        max_pixels,
        lambda values, extent: summarize_reference(values, extent, polygon, codes),
    )


def summarize_pixel_center_dataset(dataset, polygon, codes: ClassCodes, max_pixels: int = MAX_WINDOW_PIXELS):
    return walk_windows(
        dataset,
        polygon,
        codes,
        max_pixels,
        lambda values, extent: summarize_pixel_center(values, extent, polygon, codes),
    )


def within_relative(left: float, right: float, tolerance: float) -> bool:
    scale = max(abs(left), abs(right), 1e-12)
    return abs(left - right) <= tolerance * scale


def choose_check_places(places: list[Place]) -> dict[str, list[Place]]:
    small = [place for place in places if 10 <= place.area_km2 <= 50]
    small.sort(key=lambda place: (place.area_km2, place.id))
    if len(small) < 2:
        raise ValueError(f"{len(small)} pilot places are between 10 and 50 km2; the check needs two")
    largest = max(places, key=lambda place: (place.area_km2, place.id))
    return {"intersection": small[:2], "pixel_center": [largest]}


def check_pair(label: str, name: str, stored: float, other: float, limit_km2: float | None, relative: float | None) -> None:
    difference = abs(stored - other)
    if limit_km2 is not None and difference > limit_km2:
        raise SystemExit(
            f"check failed {label} {name}: {stored:.6f} and {other:.6f} differ by {difference:.6f} km2"
        )
    if relative is not None and not within_relative(stored, other, relative):
        raise SystemExit(
            f"check failed {label} {name}: {stored:.6f} and {other:.6f} differ by more than {relative:.1%}"
        )
    print(f"check {label} {name} stored {stored:.6f} other {other:.6f} diff {difference:.6f}", flush=True)


def check_results(dataset, results: list[PlaceResult], codes: ClassCodes, srs_wkt: str) -> None:
    by_id = {item.place.id: item for item in results}
    chosen = choose_check_places([item.place for item in results])
    for place in chosen["intersection"]:
        cropland, valid = summarize_reference_dataset(dataset, place.geom, codes)
        stored = by_id[place.id]
        check_pair("intersection cropland", place.name, stored.cropland_km2, cropland, INTERSECTION_TOLERANCE_KM2, None)
        check_pair("intersection valid", place.name, stored.valid_km2, valid, INTERSECTION_TOLERANCE_KM2, None)
    for place in chosen["pixel_center"]:
        cropland, valid = summarize_pixel_center_dataset(dataset, place.geom, codes)
        stored = by_id[place.id]
        check_pair(
            "pixel-center cropland",
            place.name,
            stored.cropland_km2,
            cropland,
            None,
            PIXEL_CENTER_RELATIVE_TOLERANCE,
        )
        check_pair(
            "pixel-center valid",
            place.name,
            stored.valid_km2,
            valid,
            None,
            PIXEL_CENTER_RELATIVE_TOLERANCE,
        )


def largest_share(results: list[PlaceResult]) -> tuple[list[PlaceResult], float]:
    largest = sorted(results, key=lambda item: item.place.area_km2, reverse=True)[:5]
    counted = sum(item.seconds for item in results)
    top = sum(item.seconds for item in largest)
    share = 0.0 if counted == 0 else top / counted
    return largest, share


def report_dominance(results: list[PlaceResult], counted: float) -> None:
    largest, share = largest_share(results)
    print(f"count_seconds {counted:.1f}", flush=True)
    print(f"largest_five_time_share {share:.3f}", flush=True)
    print(f"large_polygons_dominate {'yes' if share >= DOMINANCE_SHARE else 'no'}", flush=True)
    for item in largest:
        print(f"large {item.place.name} {item.place.area_km2:.2f} km2 {item.seconds:.1f}s", flush=True)


def require_sprout(dataset, recipe: dict) -> None:
    problems = []
    transform = dataset.GetGeoTransform()
    if transform[2] != 0 or transform[4] != 0 or transform[1] <= 0 or transform[5] >= 0:
        problems.append("raster is not a north-up grid")
    try:
        crs = spatial_reference(dataset.GetProjection())
    except ValueError as exc:
        problems.append(str(exc))
    else:
        if crs != recipe["grid"]["crs"]:
            problems.append(f"CRS is {crs}")
    pixel = float(recipe["grid"]["pixel_size_deg"])
    if abs(abs(transform[1]) - pixel) > 1e-12 or abs(abs(transform[5]) - pixel) > 1e-12:
        problems.append(f"pixel size is {abs(transform[1])} by {abs(transform[5])}")
    band = dataset.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    expected_nodata = int(recipe["sprout"]["values"]["nodata"])
    if nodata is None or int(nodata) != expected_nodata:
        problems.append(f"nodata is {nodata}")
    if band.DataType != gdal.GDT_Byte:
        problems.append(f"dtype is {gdal.GetDataTypeName(band.DataType)}")
    version = dataset.GetMetadataItem("version")
    if version != version_of(recipe):
        problems.append(f"version is {version}")
    class_values = dataset.GetMetadataItem("class_values")
    try:
        parsed = parse_class_values(class_values or "")
    except ValueError:
        parsed = {}
    expected = {name: int(value) for name, value in recipe["sprout"]["values"].items()}
    if parsed != expected:
        problems.append(f"class values are {class_values}")
    if problems:
        raise ValueError("sprout raster does not match the recipe: " + "; ".join(problems))


def connect(database_url: str):
    try:
        return psycopg.connect(
            database_url,
            application_name="naturewatch-stats-cropland-2024",
            connect_timeout=30,
            row_factory=dict_row,
        )
    except psycopg.Error as exc:
        message = str(exc).replace(database_url, "[redacted]")
        raise SystemExit(f"database connection failed: {message}") from None


def load_places(database_url: str) -> list[Place]:
    with connect(database_url) as conn:
        rows = conn.execute(PILOT_SQL).fetchall()
    places = []
    for row in rows:
        try:
            geom = polygonal(from_wkb(bytes(row["geom"])))
        except Exception as exc:
            raise SystemExit(f"{row['name']} ({row['id']}) geometry cannot be used: {exc}") from None
        places.append(Place(row["id"], row["boundary_id"], row["name"], float(row["area_km2"]), geom))
    return places


def require_registered(database_url: str, recipe: dict) -> tuple:
    """The sprout's period. Stops unless the sprout and the method are registered."""
    sprout = sprout_id(recipe)
    with connect(database_url) as conn:
        period = conn.execute(SPROUT_SQL, (sprout,)).fetchone()
        method = conn.execute(METHOD_SQL, (METHOD_ID,)).fetchone()
    if period is None:
        raise SystemExit(f"sprout {sprout} is not in public.sprouts")
    if method is None:
        raise SystemExit(f"method {METHOD_ID} is not in public.stat_methods")
    if method["summary"] != METHOD_SUMMARY:
        raise SystemExit(f"method {METHOD_ID} in public.stat_methods has another summary")
    return period["period_start"], period["period_end"]


def start_run(database_url: str, recipe: dict, places: list[Place], job_id: str | None, code_version: str | None) -> str:
    """Store a running stat_runs row in its own transaction, so a failure can be recorded."""
    with connect(database_url) as conn:
        with conn.transaction():
            row = conn.execute(
                INSERT_RUN,
                (sprout_id(recipe), METHOD_ID, json.dumps(SELECTION), len(places), code_version, job_id),
            ).fetchone()
    return row["id"]


def stat_rows(run_id: str, recipe: dict, period: tuple, results: list[PlaceResult]) -> list[tuple]:
    sprout = sprout_id(recipe)
    rows = []
    for item in results:
        values = stat_values(item.cropland_km2, item.valid_km2, item.place.area_km2)
        for stat, value in zip(STAT_IDS, values):
            rows.append(
                (run_id, sprout, METHOD_ID, item.place.id, item.place.boundary_id, stat, period[0], period[1], value)
            )
    return rows


def save_stats(database_url: str, run_id: str, rows: list[tuple]) -> None:
    """Every value and the run's success in one transaction."""
    with connect(database_url) as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.executemany(INSERT_STAT, rows)
            stored = conn.execute(COUNT_STATS, (run_id,)).fetchone()["stored"]
            if stored != len(rows):
                raise SystemExit(f"stored {stored} stats, expected {len(rows)}")
            if conn.execute(FINISH_RUN, ("succeeded", run_id)).rowcount != 1:
                raise SystemExit(f"run {run_id} is no longer running")


def fail_run(database_url: str, run_id: str) -> None:
    try:
        with connect(database_url) as conn:
            with conn.transaction():
                conn.execute(FINISH_RUN, ("failed", run_id))
    except BaseException as exc:  # The original error matters more.
        print(f"run {run_id} could not be marked failed: {type(exc).__name__}", flush=True)


def fetch_database_url() -> str:
    token_request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    token = json.load(urllib.request.urlopen(token_request))["access_token"]
    secret_url = (
        "https://secretmanager.googleapis.com/v1/projects/"
        f"{PROJECT}/secrets/{SECRET}/versions/latest:access"
    )
    secret_request = urllib.request.Request(secret_url, headers={"Authorization": "Bearer " + token})
    payload = json.load(urllib.request.urlopen(secret_request))["payload"]["data"]
    return base64.b64decode(payload).decode().strip("\n")


def gs_join(prefix: str, name: str) -> str:
    if not prefix.endswith("/"):
        prefix += "/"
    return prefix + name


def gs_to_vsi(gs_path: str) -> str:
    if not gs_path.startswith("gs://"):
        raise ValueError(f"not a gs path: {gs_path}")
    return "/vsigs/" + gs_path[len("gs://") :]


def delete_scratch(prefix: str) -> None:
    folder = gs_to_vsi(prefix).rstrip("/")
    names = gdal.ReadDir(folder) or []
    for name in names:
        if gdal.Unlink(f"{folder}/{name}") != 0:
            print(f"scratch object left in place: {gs_join(prefix, name)}", flush=True)


def batch_commands(script_prefix: str, job_id: str, code_version: str) -> str:
    prefix = script_prefix.removeprefix("gs://").rstrip("/")
    fetch = r"""python3 - << 'PY'
import json
import urllib.request
from pathlib import Path
request = urllib.request.Request(
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
    headers={"Metadata-Flavor": "Google"},
)
token = json.load(urllib.request.urlopen(request))["access_token"]

def download(name, dest):
    url = "https://storage.googleapis.com/PREFIX/" + name
    body = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    dest.write_bytes(urllib.request.urlopen(body).read())

folder = Path("/tmp/in")
folder.mkdir(exist_ok=True)
download("cropland.py", folder / "cropland.py")
download("requirements.txt", folder / "requirements.txt")
download("recipe.json", folder / "recipe.json")
PY
""".replace("PREFIX", prefix)
    return (
        "set -eu\n"
        + fetch
        + "apt-get update\n"
        + "DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pip\n"
        + "python3 -m pip install --break-system-packages -r /tmp/in/requirements.txt "
        + "|| python3 -m pip install -r /tmp/in/requirements.txt\n"
        + "python3 /tmp/in/cropland.py run --recipe /tmp/in/recipe.json"
        + f" --scratch {script_prefix} --job-id {job_id} --code-version {code_version}\n"
    )


def batch_job(script_prefix: str, job_id: str, code_version: str) -> dict:
    """One spot VM in europe-west1. Batch deletes it when the task ends."""
    return {
        "taskGroups": [
            {
                "taskCount": "1",
                "taskSpec": {
                    "maxRunDuration": "10800s",
                    "maxRetryCount": 1,
                    "computeResource": {"cpuMilli": "4000", "memoryMib": "12288"},
                    "environment": {"variables": {"CPL_MACHINE_IS_GCE": "YES", BATCH_ENV: "1"}},
                    "runnables": [
                        {
                            "container": {
                                "imageUri": IMAGE,
                                "entrypoint": "sh",
                                "commands": ["-c", batch_commands(script_prefix, job_id, code_version)],
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


def ensure_secret(database_url: str) -> None:
    described = subprocess.run(
        ["gcloud", "secrets", "describe", SECRET, f"--project={PROJECT}"],
        capture_output=True,
        text=True,
    )
    if described.returncode != 0:
        gcloud(
            [
                "secrets",
                "create",
                SECRET,
                "--replication-policy=user-managed",
                "--locations=europe-west1",
            ]
        )
    subprocess.run(
        ["gcloud", "secrets", "versions", "add", SECRET, "--data-file=-", f"--project={PROJECT}"],
        input=database_url,
        text=True,
        check=True,
    )
    gcloud(
        [
            "secrets",
            "add-iam-policy-binding",
            SECRET,
            f"--member=serviceAccount:{SERVICE_ACCOUNT}",
            "--role=roles/secretmanager.secretAccessor",
        ]
    )


def command_run(recipe: dict, scratch: str | None, job_id: str | None, code_version: str | None) -> None:
    if os.environ.get(BATCH_ENV) != "1":
        raise SystemExit(
            "The stats run is the Batch job in europe-west1. "
            "From this computer, use: python stats/cropland.py submit"
        )
    database_url = os.environ.get("DATABASE_URL") or fetch_database_url()
    gdal.SetCacheMax(512 * 1024 * 1024)
    raster_path = recipe["sprout"]["path"]
    dataset = gdal.Open(gs_to_vsi(raster_path))
    if dataset is None:
        raise SystemExit(f"cannot open {raster_path}")
    require_sprout(dataset, recipe)
    period = require_registered(database_url, recipe)
    places = load_places(database_url)
    if not places:
        raise SystemExit("listed_places returned no South Africa rows")
    print(f"places {len(places)}", flush=True)
    run_id = start_run(database_url, recipe, places, job_id, code_version)
    print(f"run {run_id}", flush=True)
    try:
        codes = class_codes(recipe)
        srs_wkt = wgs84_wkt()
        results = []
        counted = 0.0
        for index, place in enumerate(places, start=1):
            started = time.perf_counter()
            cropland, valid = summarize_exact(dataset, place.geom, codes, srs_wkt)
            seconds = time.perf_counter() - started
            counted += seconds
            results.append(PlaceResult(place, cropland, valid, seconds))
            print(f"count {index}/{len(places)} {seconds:.1f}s {place.area_km2:.2f} {place.name}", flush=True)
        report_dominance(results, counted)
        check_results(dataset, results, codes, srs_wkt)
        print("checks passed", flush=True)
        rows = stat_rows(run_id, recipe, period, results)
        save_stats(database_url, run_id, rows)
    except BaseException:
        fail_run(database_url, run_id)
        raise
    print(f"stored {len(rows)} stats for {len(results)} places", flush=True)
    if scratch:
        delete_scratch(scratch)
    dataset = None


def code_version() -> str:
    """The commit the job runs. Stops if the script or the recipes have uncommitted changes."""
    dirty = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain", "--", "stats", "cookbook"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if dirty:
        raise SystemExit("stats/ or cookbook/ has uncommitted changes. Commit them before a run.")
    return subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def command_submit(recipe_path: Path) -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set. Export it for this command. Do not commit it.")
    recipe = load_recipe(recipe_path)
    version = version_of(recipe)
    commit = code_version()
    job_id = f"stats-{recipe['id']}-{version}-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}"
    gcloud(["services", "enable", "batch.googleapis.com", "secretmanager.googleapis.com"])
    ensure_secret(database_url)
    work = ROOT / "data" / "stats" / recipe["id"]
    work.mkdir(parents=True, exist_ok=True)
    recipe_json = work / "recipe.json"
    recipe_json.write_text(json.dumps(recipe) + "\n")
    upload_with_gcloud(Path(__file__).resolve(), gs_join(SCRATCH, "cropland.py"))
    upload_with_gcloud(ROOT / "stats" / "requirements.txt", gs_join(SCRATCH, "requirements.txt"))
    upload_with_gcloud(recipe_json, gs_join(SCRATCH, "recipe.json"))
    config = batch_job(SCRATCH, job_id, commit)
    config_path = work / "batch_job.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    print(config_path.read_text(), flush=True)
    gcloud(["batch", "jobs", "submit", job_id, f"--location={REGION}", f"--config={config_path}"])
    print(f"submitted {job_id} in {REGION}", flush=True)
    print(f"secret {SECRET}", flush=True)


def peak_memory_bytes() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss
    return rss * 1024


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "submit"))
    parser.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
    parser.add_argument("--scratch", default=None)
    parser.add_argument("--job-id", default=None)
    parser.add_argument("--code-version", default=None)
    args = parser.parse_args()
    started = time.perf_counter()
    try:
        if args.command == "submit":
            command_submit(args.recipe)
        else:
            command_run(load_recipe(args.recipe), args.scratch, args.job_id, args.code_version)
    finally:
        elapsed = time.perf_counter() - started
        print(f"wall_seconds {elapsed:.1f}")
        print(f"peak_memory_bytes {peak_memory_bytes()}")


if __name__ == "__main__":
    main()
