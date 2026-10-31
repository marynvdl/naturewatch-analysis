#!/usr/bin/env python3
"""Build the protected-area PMTiles archive from the app database.

The recipe is the catalog. This script reads it and does not edit it.
Each feature carries places.id, area_km2, and cropland percent when a fact
exists. Colour is applied in the client. The tile engine is Tippecanoe
(felt/tippecanoe 2.x), which streams newline-delimited GeoJSON and writes
PMTiles directly. GDAL and OGR read, validate, and prove the archive.
A laptop prove writes a local archive and does not upload. The hosted
publish waits for approval.

  python places/protected_areas.py prove [--bbox W,S,E,N]
  python places/protected_areas.py build --source places.ndjson --dest out.pmtiles
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import resource
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from osgeo import gdal, ogr

gdal.UseExceptions()

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECIPE = ROOT / "cookbook" / "recipes" / "places-protected-areas.yaml"
PROJECT = "nature-watch-387210"
REGION = "europe-west1"
TIPPECANOE_VERSION = "2.79.0"
# Built by places/Dockerfile. Pinned to the Tippecanoe version. Not pushed until approved.
IMAGE = f"europe-west1-docker.pkg.dev/{PROJECT}/places/places-tippecanoe:{TIPPECANOE_VERSION}"
# Provisional until the cropped samples size the job. See places/README.md.
MACHINE = "e2-standard-8"
CPU_MILLI = "8000"
MEMORY_MIB = "32768"
BOOT_DISK_GB = 100
MAX_RUN_SECONDS = 7200
SERVICE_ACCOUNT = "637146989512-compute@developer.gserviceaccount.com"
SCRATCH = "gs://nature-watch-bucket/scratch/places/protected-areas/"
SECRET = "naturewatch-facts-database-url"
BATCH_ENV = "NATUREWATCH_PLACES_BATCH"
PROPERTIES = ("id", "area_km2", "cropland_percent")
PREVIOUS_NAME = "places.previous.pmtiles"
FETCH_ROWS = 200

# Every feature is kept: no per-tile feature or byte cap, no merging of small
# polygons, no dropping at low zoom. Each place's first zoom is its own
# tippecanoe.minzoom taken from the recipe.
TIPPECANOE_FLAGS = (
    "--no-feature-limit",
    "--no-tile-size-limit",
    "--no-tiny-polygon-reduction",
    "--read-parallel",
    "--force",
)


@dataclass(frozen=True)
class PlaceFeature:
    id: str
    area_km2: float
    cropland_percent: float | None
    wkb: bytes


def load_recipe(path: Path) -> dict:
    text = path.read_text()
    if path.suffix == ".json":
        return json.loads(text)
    import yaml

    return yaml.safe_load(text)


def places_contract(recipe: dict) -> dict:
    """The recipe's places archive. A test can lower max_zoom; the file says 12."""
    if recipe.get("kind") != "places":
        raise ValueError("recipe kind must be places")
    tiles = recipe.get("tiles")
    if not isinstance(tiles, dict):
        raise ValueError("recipe has no tiles block")
    problems = []
    if tiles.get("scheme") != "pmtiles":
        problems.append("tiles.scheme must be pmtiles")
    if tiles.get("layer") != "places":
        problems.append("tiles.layer must be places")
    if int(tiles.get("min_zoom", -1)) != 0:
        problems.append("tiles.min_zoom must be 0")
    max_zoom = int(tiles.get("max_zoom", -1))
    if max_zoom < 0:
        problems.append("tiles.max_zoom must be at least 0")
    path = str(tiles.get("path", ""))
    if not path.startswith("gs://nature-watch-tiles/places/"):
        problems.append("tiles.path must be under nature-watch-tiles/places/")
    if not path.endswith(".pmtiles"):
        problems.append("tiles.path must be a .pmtiles object")
    if list(tiles.get("properties") or []) != list(PROPERTIES):
        problems.append("tiles.properties must be id, area_km2, cropland_percent")
    bands = tiles.get("minzoom_by_area_km2") or []
    if not bands:
        problems.append("tiles.minzoom_by_area_km2 is missing")
    if problems:
        raise ValueError("; ".join(problems))
    return {
        "path": path,
        "layer": str(tiles["layer"]),
        "min_zoom": 0,
        "max_zoom": max_zoom,
        "simplification": float(tiles.get("simplification", 1)),
        "minzoom_by_area_km2": [
            (float(band["area_km2"]), int(band["minzoom"])) for band in bands
        ],
        "sprout_id": str(recipe["facts"]["sprout_id"]),
        "method_id": str(recipe["facts"]["method_id"]),
    }


def minzoom_for_area(area_km2: float, bands: list[tuple[float, int]]) -> int:
    """Largest matching size band. Places below the last band still use it."""
    ordered = sorted(bands, key=lambda item: item[0], reverse=True)
    chosen = ordered[-1][1]
    for minimum, zoom in ordered:
        if area_km2 >= minimum:
            return zoom
    return chosen


def polygonal_wkb(geom: ogr.Geometry, make_valid: bool = True) -> bytes:
    """Polygonal WKB. With make_valid False the stored geometry is trusted."""
    if make_valid:
        geom = geom.MakeValid()
    name = geom.GetGeometryName()
    if name == "GEOMETRYCOLLECTION":
        collected = ogr.Geometry(ogr.wkbMultiPolygon)
        for i in range(geom.GetGeometryCount()):
            part = geom.GetGeometryRef(i)
            part_name = part.GetGeometryName()
            if part_name == "POLYGON":
                collected.AddGeometry(part.Clone())
            elif part_name == "MULTIPOLYGON":
                for j in range(part.GetGeometryCount()):
                    collected.AddGeometry(part.GetGeometryRef(j).Clone())
        geom = collected
        name = geom.GetGeometryName()
    if name == "POLYGON":
        collected = ogr.Geometry(ogr.wkbMultiPolygon)
        collected.AddGeometry(geom)
        geom = collected
        name = geom.GetGeometryName()
    if name != "MULTIPOLYGON" or geom.IsEmpty():
        kind = "empty" if geom.IsEmpty() else name
        raise ValueError(f"geometry is {kind}")
    return bytes(geom.ExportToWkb())


def place_from_feature(feature: ogr.Feature) -> PlaceFeature:
    geom = feature.GetGeometryRef()
    if geom is None:
        raise ValueError("feature has no geometry")
    crop = feature.GetField("cropland_percent")
    if crop is not None:
        crop = float(crop)
    return PlaceFeature(
        id=str(feature.GetField("id")),
        area_km2=float(feature.GetField("area_km2")),
        cropland_percent=crop,
        wkb=polygonal_wkb(geom),
    )


def places_from_file(path: Path) -> Iterator[PlaceFeature]:
    """Read a GeoJSON or newline-delimited GeoJSON file one place at a time."""
    dataset = gdal.OpenEx(str(path), gdal.OF_VECTOR)
    if dataset is None:
        raise ValueError(f"cannot open {path}")
    layer = dataset.GetLayer(0)
    for feature in layer:
        yield place_from_feature(feature)


# No ST_MakeValid and no ST_CollectionExtract here. The app database does not
# validate 51,001 rows. Validation is --validate ogr on the build machine.
# No order by: Tippecanoe does not need an order and a sort holds every
# geometry in database memory.
PLACES_SQL = """
select
  places.id::text as id,
  places.area_km2,
  facts.percent as cropland_percent,
  extensions.ST_AsBinary(places.geom) as geom
from public.places
left join public.facts
  on facts.place_id = places.id
 and facts.sprout_id = %(sprout_id)s
 and facts.method_id = %(method_id)s
where places.geom is not null
{bbox}
"""

BBOX_SQL = "and places.geom && extensions.ST_MakeEnvelope(%(west)s, %(south)s, %(east)s, %(north)s, 4326)"

VALID_SQL = """
select
  count(*) as places,
  count(*) filter (where not extensions.ST_IsValid(geom)) as invalid,
  count(*) filter (where extensions.GeometryType(geom) not in ('POLYGON', 'MULTIPOLYGON')) as not_polygonal
from public.places
where geom is not null
"""


def connect(database_url: str):
    import psycopg
    from psycopg.rows import dict_row

    try:
        return psycopg.connect(
            database_url,
            application_name="naturewatch-places-protected-areas",
            connect_timeout=30,
            row_factory=dict_row,
        )
    except psycopg.Error as exc:
        message = str(exc).replace(database_url, "[redacted]")
        raise SystemExit(f"database connection failed: {message}") from None


def parse_bbox(text: str | None) -> tuple[float, float, float, float] | None:
    if not text:
        return None
    west, south, east, north = (float(part) for part in text.split(","))
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise SystemExit(f"bbox must be west,south,east,north in degrees: {text}")
    return west, south, east, north


def command_check_valid(database_url: str) -> None:
    """Count invalid and non-polygonal stored geometries. Counts only, no geometry leaves."""
    with connect(database_url) as conn:
        row = conn.execute(VALID_SQL).fetchone()
    print(f"places {row['places']}", flush=True)
    print(f"invalid {row['invalid']}", flush=True)
    print(f"not_polygonal {row['not_polygonal']}", flush=True)


def stream_places(
    database_url: str,
    recipe: dict,
    make_valid: bool,
    bbox: tuple[float, float, float, float] | None = None,
) -> Iterator[PlaceFeature]:
    """Yield places from a server-side cursor. Memory does not grow with place count."""
    contract = places_contract(recipe)
    params: dict = {"sprout_id": contract["sprout_id"], "method_id": contract["method_id"]}
    clause = ""
    if bbox:
        clause = BBOX_SQL
        params.update(dict(zip(("west", "south", "east", "north"), bbox)))
    sql = PLACES_SQL.format(bbox=clause)
    with connect(database_url) as conn:
        with conn.cursor(name="places_stream") as cursor:
            cursor.itersize = FETCH_ROWS
            cursor.execute(sql, params)
            for row in cursor:
                geom = ogr.CreateGeometryFromWkb(bytes(row["geom"]))
                if geom is None:
                    raise SystemExit(f"{row['id']} geometry cannot be used")
                try:
                    wkb = polygonal_wkb(geom, make_valid)
                except ValueError as exc:
                    raise SystemExit(f"{row['id']} geometry cannot be used: {exc}") from None
                crop = row["cropland_percent"]
                yield PlaceFeature(
                    id=str(row["id"]),
                    area_km2=float(row["area_km2"]),
                    cropland_percent=None if crop is None else float(crop),
                    wkb=wkb,
                )


def feature_line(place: PlaceFeature, recipe: dict) -> str:
    """One newline-delimited GeoJSON feature with its own minzoom."""
    bands = places_contract(recipe)["minzoom_by_area_km2"]
    properties = {"id": place.id, "area_km2": place.area_km2}
    if place.cropland_percent is not None:
        properties["cropland_percent"] = place.cropland_percent
    geometry = ogr.CreateGeometryFromWkb(place.wkb).ExportToJson(["COORDINATE_PRECISION=7"])
    return (
        '{"type":"Feature","properties":'
        + json.dumps(properties, separators=(",", ":"))
        + ',"tippecanoe":{"minzoom":'
        + str(minzoom_for_area(place.area_km2, bands))
        + '},"geometry":'
        + geometry
        + "}\n"
    )


def write_ndjson(places: Iterator[PlaceFeature], dest: Path, recipe: dict) -> dict:
    """Write places to disk one line at a time. Returns counts and the id to minzoom map."""
    bands = places_contract(recipe)["minzoom_by_area_km2"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = with_cropland = 0
    minzooms: dict[str, int] = {}
    with dest.open("w") as handle:
        for place in places:
            handle.write(feature_line(place, recipe))
            minzooms[place.id] = minzoom_for_area(place.area_km2, bands)
            count += 1
            with_cropland += place.cropland_percent is not None
    return {"places": count, "with_cropland": with_cropland, "minzooms": minzooms}


def tippecanoe_command(source: Path, dest: Path, recipe: dict) -> list[str]:
    contract = places_contract(recipe)
    return [
        "tippecanoe",
        "-o", str(dest),
        "-l", contract["layer"],
        "-Z", str(contract["min_zoom"]),
        "-z", str(contract["max_zoom"]),
        f"--simplification={contract['simplification']:g}",
        "-y", "id", "-y", "area_km2", "-y", "cropland_percent",
        "-T", "id:string", "-T", "area_km2:float", "-T", "cropland_percent:float",
        *TIPPECANOE_FLAGS,
        str(source),
    ]


def tippecanoe_version() -> str:
    found = subprocess.run(["tippecanoe", "--version"], text=True, capture_output=True)
    return (found.stdout + found.stderr).strip()


def run_tippecanoe(source: Path, dest: Path, recipe: dict) -> None:
    if shutil.which("tippecanoe") is None:
        raise SystemExit("tippecanoe is not installed. Use felt/tippecanoe 2.x, not the Debian package.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(tippecanoe_command(source, dest, recipe), check=True)


def archive_stats(info: dict, dest: Path, recipe: dict) -> dict:
    contract = places_contract(recipe)
    return {
        "places": info["places"],
        "with_cropland": info["with_cropland"],
        "bytes": dest.stat().st_size,
        "min_zoom": contract["min_zoom"],
        "max_zoom": contract["max_zoom"],
        "tippecanoe": tippecanoe_version(),
        "tippecanoe_flags": " ".join(tippecanoe_command(Path("IN"), Path("OUT"), recipe)[1:-1]),
        "path": str(dest),
    }


def ids_by_zoom(path: Path, zoom: int) -> dict[str, dict]:
    """Every distinct id at this zoom with the properties it carries."""
    dataset = gdal.OpenEx(
        str(path),
        gdal.OF_VECTOR,
        open_options=[f"ZOOM_LEVEL={zoom}", "CLIP=NO"],
    )
    if dataset is None:
        raise ValueError(f"cannot open {path}")
    found: dict[str, dict] = {}
    for index in range(dataset.GetLayerCount()):
        layer = dataset.GetLayer(index)
        # A layer with no cropland fact anywhere has no such field.
        has_crop = layer.GetLayerDefn().GetFieldIndex("cropland_percent") >= 0
        for feature in layer:
            place_id = str(feature.GetField("id"))
            if place_id in found:
                continue
            crop = feature.GetField("cropland_percent") if has_crop else None
            found[place_id] = {
                "area_km2": feature.GetField("area_km2"),
                "cropland_percent": None if crop is None else float(crop),
            }
    dataset = None
    return found


def unique_ids_at_zoom(path: Path, zoom: int) -> set[str]:
    return set(ids_by_zoom(path, zoom))


def properties_at_zoom(path: Path, zoom: int) -> dict[str, dict]:
    return ids_by_zoom(path, zoom)


def largest_tile_by_zoom(path: Path) -> dict[int, int]:
    """Largest stored tile in bytes at each zoom, as written in the archive."""
    from pmtiles.reader import MmapSource, all_tiles

    largest: dict[int, int] = {}
    with path.open("rb") as handle:
        for (zoom, _x, _y), data in all_tiles(MmapSource(handle)):
            if len(data) > largest.get(zoom, 0):
                largest[zoom] = len(data)
    return dict(sorted(largest.items()))


def prove_archive(
    expected: dict[str, dict],
    path: Path,
    recipe: dict,
    full_by_zoom: int = 6,
) -> dict:
    """Check every zoom from min to max.

    expected maps id to {"minzoom", "area_km2", "cropland_percent"}.
    A place must be absent below its minzoom and present at and after it.
    Every id is present by full_by_zoom. Copied fields are checked at max zoom.
    """
    contract = places_contract(recipe)
    problems: list[str] = []
    report: dict[int, dict] = {}
    last_ids: dict[str, dict] = {}
    for zoom in range(contract["min_zoom"], contract["max_zoom"] + 1):
        found = ids_by_zoom(path, zoom)
        last_ids = found
        early = [i for i in found if i not in expected]
        if early:
            problems.append(f"zoom {zoom}: {len(early)} ids are not places")
        before = [i for i in found if i in expected and expected[i]["minzoom"] > zoom]
        due = [i for i, row in expected.items() if row["minzoom"] <= zoom]
        missing = [i for i in due if i not in found]
        if before:
            problems.append(f"zoom {zoom}: {len(before)} places appear before their minzoom")
        if missing:
            problems.append(f"zoom {zoom}: {len(missing)} places missing after their minzoom")
        if zoom >= full_by_zoom and len(found) != len(expected):
            problems.append(f"zoom {zoom}: {len(found)} of {len(expected)} ids present")
        report[zoom] = {"ids": len(found), "due": len(due)}
    for place_id, row in expected.items():
        got = last_ids.get(place_id)
        if got is None:
            continue
        if abs(float(got["area_km2"]) - row["area_km2"]) > 1e-6:
            problems.append(f"{place_id} area_km2 was not copied")
        if row["cropland_percent"] is None:
            if got["cropland_percent"] is not None:
                problems.append(f"{place_id} should have no cropland percent")
        elif got["cropland_percent"] is None or abs(got["cropland_percent"] - row["cropland_percent"]) > 1e-6:
            problems.append(f"{place_id} cropland percent was not copied")
    largest = largest_tile_by_zoom(path)
    if problems:
        shown = "; ".join(problems[:10])
        raise SystemExit(f"proof failed ({len(problems)} problems): {shown}")
    return {"ids_by_zoom": report, "largest_tile_bytes_by_zoom": largest}


def expected_from(info: dict, areas: dict[str, tuple[float, float | None]]) -> dict[str, dict]:
    return {
        place_id: {
            "minzoom": zoom,
            "area_km2": areas[place_id][0],
            "cropland_percent": areas[place_id][1],
        }
        for place_id, zoom in info["minzooms"].items()
    }


def build_and_prove(
    places: Iterator[PlaceFeature],
    dest: Path,
    recipe: dict,
    prove: bool = True,
) -> dict:
    """Stream places to disk, run Tippecanoe, then prove the archive."""
    work = dest.parent
    work.mkdir(parents=True, exist_ok=True)
    source = work / "places.ndjson"
    areas: dict[str, tuple[float, float | None]] = {}

    def remember(items: Iterator[PlaceFeature]) -> Iterator[PlaceFeature]:
        for item in items:
            areas[item.id] = (item.area_km2, item.cropland_percent)
            yield item

    info = write_ndjson(remember(places), source, recipe)
    if info["places"] == 0:
        raise SystemExit("places returned no rows")
    print(f"places {info['places']}", flush=True)
    run_tippecanoe(source, dest, recipe)
    stats = archive_stats(info, dest, recipe)
    if prove:
        proof = prove_archive(expected_from(info, areas), dest, recipe)
        stats["ids_by_zoom"] = proof["ids_by_zoom"]
        stats["largest_tile_bytes_by_zoom"] = proof["largest_tile_bytes_by_zoom"]
    return stats


PREVIEW_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Places PMTiles preview</title>
  <link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css"/>
  <style>
    html, body, #map { margin: 0; height: 100%; }
    #note {
      position: absolute; z-index: 1; left: 12px; top: 12px;
      max-width: 28rem; padding: 0.75rem 1rem;
      background: #fff; color: #1a1a1a; font: 14px/1.4 system-ui, sans-serif;
      box-shadow: 0 1px 4px rgba(0,0,0,.2);
    }
  </style>
</head>
<body>
  <div id="note">
    Protected-area archive. Paint is the current map green, applied here only.
    Zoom in: smaller places appear. Zoom <span id="zoom">5</span>.
  </div>
  <div id="map"></div>
  <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
  <script src="https://unpkg.com/pmtiles@3.2.0/dist/pmtiles.js"></script>
  <script>
    const protocol = new pmtiles.Protocol();
    maplibregl.addProtocol("pmtiles", protocol.tile);
    const archive = new URL("places.pmtiles", window.location.href);
    const map = new maplibregl.Map({
      container: "map",
      style: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
      center: [24.7, -28.5],
      zoom: 5
    });
    map.on("error", (event) => {
      const note = document.getElementById("note");
      note.textContent = (event.error && event.error.message) || "map error";
    });
    map.on("load", async () => {
      const archiveUrl = archive.href;
      const file = new File([await (await fetch(archiveUrl)).arrayBuffer()], "places.pmtiles");
      protocol.add(new pmtiles.PMTiles({
        getKey: () => archiveUrl,
        getBytes: (offset, length) => file.slice(offset, offset + length).arrayBuffer().then((data) => ({ data }))
      }));
      map.addSource("places", { type: "vector", url: "pmtiles://" + archiveUrl });
      map.addLayer({
        id: "places-fill",
        type: "fill",
        source: "places",
        "source-layer": "places",
        paint: { "fill-color": "#1f4b3a", "fill-opacity": 0.18 }
      });
      map.addLayer({
        id: "places-line",
        type: "line",
        source: "places",
        "source-layer": "places",
        paint: { "line-color": "#1f4b3a", "line-width": 1.5 }
      });
    });
    map.on("zoom", () => {
      document.getElementById("zoom").textContent = map.getZoom().toFixed(1);
    });
  </script>
</body>
</html>
"""


def write_preview(dest: Path) -> Path:
    html = dest.with_name("preview.html")
    html.write_text(PREVIEW_HTML)
    return html


def parse_byte_range(header: str | None, size: int) -> tuple[int, int, bool]:
    """Inclusive start and end, and whether the client asked for a range."""
    if not header:
        return 0, size - 1, False
    if not header.startswith("bytes="):
        raise ValueError(f"unsupported range: {header}")
    spec = header.removeprefix("bytes=").split(",", 1)[0]
    start_text, _, end_text = spec.partition("-")
    if start_text == "":
        start = max(0, size - int(end_text))
        end = size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    if start < 0 or end < start or start >= size:
        raise ValueError(f"unsatisfiable range: {header}")
    return start, min(end, size - 1), True


class ByteRangeHandler(http.server.SimpleHTTPRequestHandler):
    """Serve PMTiles with HTTP Range. Python's default handler often returns 200 for the whole file."""

    def end_headers(self) -> None:
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Access-Control-Expose-Headers", "Accept-Ranges, Content-Length, Content-Range")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_head(self):
        path = Path(self.translate_path(self.path.split("?", 1)[0]))
        if not path.is_file():
            self.send_error(404, "File not found")
            return None
        size = path.stat().st_size
        try:
            start, end, ranged = parse_byte_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_error(416, "Requested range not satisfiable")
            return None
        length = end - start + 1
        handle = path.open("rb")
        handle.seek(start)
        if ranged:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Length", str(length))
        self.end_headers()
        handle.length = length  # type: ignore[attr-defined]
        return handle

    def copyfile(self, source, outputfile) -> None:
        remaining = getattr(source, "length", None)
        if remaining is None:
            super().copyfile(source, outputfile)
            return
        while remaining > 0:
            chunk = source.read(min(65536, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)


def command_preview(dest: Path, port: int) -> None:
    if not dest.exists():
        raise SystemExit(f"no archive at {dest}. Run prove first.")
    write_preview(dest)
    handler = functools.partial(ByteRangeHandler, directory=str(dest.parent))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"preview http://127.0.0.1:{port}/preview.html", flush=True)
    print("cancel: Ctrl+C", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped", flush=True)
    finally:
        server.server_close()



def print_stats(stats: dict) -> None:
    for key, value in stats.items():
        if isinstance(value, dict):
            print(f"{key} {json.dumps(value)}", flush=True)
        else:
            print(f"{key} {value}", flush=True)


def command_build(recipe: dict, source: Path, dest: Path) -> dict:
    """Tile a local GeoJSON or newline-delimited GeoJSON file."""
    stats = build_and_prove(places_from_file(source), dest, recipe)
    print_stats(stats)
    return stats


def command_prove(recipe: dict, dest: Path, make_valid: bool, bbox) -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set. Export it for this command. Do not commit it.")
    stats = build_and_prove(stream_places(database_url, recipe, make_valid, bbox), dest, recipe)
    stats["preview"] = str(write_preview(dest))
    print_stats(stats)
    print("proof passed", flush=True)


def gs_join(prefix: str, name: str) -> str:
    if not prefix.endswith("/"):
        prefix += "/"
    return prefix + name


def gs_to_vsi(gs_path: str) -> str:
    if not gs_path.startswith("gs://"):
        raise ValueError(f"not a gs path: {gs_path}")
    return "/vsigs/" + gs_path[len("gs://") :]


def https_url(gs_path: str) -> str:
    return "https://storage.googleapis.com/" + gs_path.removeprefix("gs://")


def delete_scratch(prefix: str) -> None:
    folder = gs_to_vsi(prefix).rstrip("/")
    names = gdal.ReadDir(folder) or []
    for name in names:
        if gdal.Unlink(f"{folder}/{name}") != 0:
            print(f"scratch object left in place: {gs_join(prefix, name)}", flush=True)


def batch_commands(script_prefix: str, replace: bool = False, make_valid: bool = True) -> str:
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
download("protected_areas.py", folder / "protected_areas.py")
download("recipe.json", folder / "recipe.json")
PY
""".replace("PREFIX", prefix)
    flags = " --upload" + (" --replace" if replace else "") + ("" if make_valid else " --validate none")
    return (
        "set -eu\n"
        + fetch
        + f"python3 /tmp/in/protected_areas.py run --recipe /tmp/in/recipe.json --scratch {script_prefix}{flags}\n"
    )


def batch_job(script_prefix: str, replace: bool = False, make_valid: bool = True) -> dict:
    """One spot VM in europe-west1. Batch deletes it when the task ends."""
    return {
        "taskGroups": [
            {
                "taskCount": "1",
                "taskSpec": {
                    "maxRunDuration": f"{MAX_RUN_SECONDS}s",
                    "maxRetryCount": 1,
                    "computeResource": {"cpuMilli": CPU_MILLI, "memoryMib": MEMORY_MIB},
                    "environment": {"variables": {"CPL_MACHINE_IS_GCE": "YES", BATCH_ENV: "1"}},
                    "runnables": [
                        {
                            "container": {
                                "imageUri": IMAGE,
                                "entrypoint": "sh",
                                "commands": ["-c", batch_commands(script_prefix, replace, make_valid)],
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
                        "bootDisk": {"sizeGb": BOOT_DISK_GB, "type": "pd-balanced"},
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


def upload_with_gcloud(local: Path, destination: str, cache_control: str | None = None) -> None:
    extra = [f"--cache-control={cache_control}"] if cache_control else []
    gcloud(["storage", "cp", *extra, str(local), destination])


def object_exists(path: str) -> bool:
    listed = subprocess.run(
        ["gcloud", "storage", "ls", path, f"--project={PROJECT}"],
        text=True,
        capture_output=True,
    )
    return listed.returncode == 0 and listed.stdout.strip() != ""


def previous_path(published: str) -> str:
    return published.rsplit("/", 1)[0] + "/" + PREVIOUS_NAME


def check_range(url: str) -> int:
    """Status of a one-byte-range request. A reader of the archive needs 206."""
    import urllib.request

    request = urllib.request.Request(url, headers={"Range": "bytes=0-15"})
    with urllib.request.urlopen(request) as response:
        return response.status


def replace_published(dest: Path, published: str) -> None:
    """Back up the current object, replace it, set no-cache, and check Range."""
    backup = previous_path(published)
    if object_exists(published):
        if object_exists(backup):
            raise SystemExit(
                f"{backup} already exists and is the rollback copy. Remove it only on request, then run again."
            )
        gcloud(["storage", "cp", published, backup])
        print(f"copied {published} to {backup}", flush=True)
    upload_with_gcloud(dest, published, cache_control="no-cache")
    print(f"uploaded {published}", flush=True)
    status = check_range(https_url(published))
    print(f"range_status {status}", flush=True)
    if status != 206:
        raise SystemExit(f"Range request returned {status}, expected 206")


def fetch_database_url() -> str:
    import base64
    import urllib.request

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


def command_run(
    recipe: dict,
    dest: Path,
    upload: bool,
    scratch: str | None,
    replace: bool,
    make_valid: bool,
) -> None:
    if os.environ.get(BATCH_ENV) != "1":
        raise SystemExit(
            "The places run is the Batch job in europe-west1. "
            "From this computer, prove locally with: python places/protected_areas.py prove --bbox W,S,E,N"
        )
    published = places_contract(recipe)["path"]
    if upload and object_exists(published) and not replace:
        raise SystemExit(f"{published} already exists. Run again with --replace to replace it.")
    database_url = os.environ.get("DATABASE_URL") or fetch_database_url()
    stats = build_and_prove(stream_places(database_url, recipe, make_valid), dest, recipe)
    stats["validate"] = "ogr" if make_valid else "none"
    print_stats(stats)
    print("proof passed", flush=True)
    if upload:
        replace_published(dest, published)
    if scratch:
        delete_scratch(scratch)


def command_submit(recipe_path: Path, replace: bool, make_valid: bool) -> None:
    recipe = load_recipe(recipe_path)
    published = places_contract(recipe)["path"]
    if object_exists(published) and not replace:
        raise SystemExit(f"{published} already exists. Submit with --replace to replace it.")
    job_id = f"places-{recipe['id']}-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}"
    gcloud(["services", "enable", "batch.googleapis.com", "secretmanager.googleapis.com"])
    work = ROOT / "data" / "places" / recipe["id"]
    work.mkdir(parents=True, exist_ok=True)
    recipe_json = work / "recipe.json"
    recipe_json.write_text(json.dumps(recipe) + "\n")
    upload_with_gcloud(Path(__file__).resolve(), gs_join(SCRATCH, "protected_areas.py"))
    upload_with_gcloud(recipe_json, gs_join(SCRATCH, "recipe.json"))
    config = batch_job(SCRATCH, replace, make_valid)
    config_path = work / "batch_job.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    print(config_path.read_text(), flush=True)
    gcloud(["batch", "jobs", "submit", job_id, f"--location={REGION}", f"--config={config_path}"])
    print(f"submitted {job_id} in {REGION}", flush=True)
    print(f"secret {SECRET}", flush=True)
    print(f"output {published}", flush=True)
    print(
        "status: gcloud batch jobs describe "
        f"{job_id} --location={REGION} --project={PROJECT}",
        flush=True,
    )
    print(
        "cancel: gcloud batch jobs delete "
        f"{job_id} --location={REGION} --project={PROJECT}",
        flush=True,
    )


def peak_memory_bytes() -> int:
    """Larger of this process and its finished children, such as Tippecanoe."""
    rss = max(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
    )
    if sys.platform == "darwin":
        return rss
    return rss * 1024


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("build", "prove", "preview", "run", "submit", "check-valid")
    )
    parser.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bbox", default=None, help="west,south,east,north for a cropped laptop sample")
    parser.add_argument(
        "--dest",
        type=Path,
        default=ROOT / "data" / "places" / "protected-areas" / "places.pmtiles",
    )
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--replace", action="store_true", help="replace the published object after a rollback copy")
    parser.add_argument(
        "--validate",
        choices=("ogr", "none"),
        default="none",
        help="none trusts the stored geometry: check-valid found 0 invalid of 51,001. ogr runs MakeValid here.",
    )
    parser.add_argument("--scratch", default=None)
    args = parser.parse_args()
    make_valid = args.validate == "ogr"
    started = time.perf_counter()
    try:
        if args.command == "preview":
            command_preview(args.dest, args.port)
            return
        if args.command == "check-valid":
            database_url = os.environ.get("DATABASE_URL")
            if not database_url:
                raise SystemExit("DATABASE_URL is not set. Export it for this command. Do not commit it.")
            command_check_valid(database_url)
            return
        recipe = load_recipe(args.recipe)
        if args.command == "submit":
            command_submit(args.recipe, args.replace, make_valid)
        elif args.command == "build":
            if args.source is None:
                raise SystemExit("build needs --source")
            command_build(recipe, args.source, args.dest)
        elif args.command == "prove":
            command_prove(recipe, args.dest, make_valid, parse_bbox(args.bbox))
        else:
            command_run(recipe, args.dest, args.upload, args.scratch, args.replace, make_valid)
    finally:
        elapsed = time.perf_counter() - started
        print(f"wall_seconds {elapsed:.1f}")
        print(f"peak_memory_bytes {peak_memory_bytes()}")


if __name__ == "__main__":
    main()
