#!/usr/bin/env python3
"""Build the protected-area PMTiles archive from the app database.

The recipe is the catalog. This script reads it and does not edit it.
Each feature carries places.id, area_km2, and cropland percent when a fact
exists. Colour is applied in the client. A laptop prove writes a local
archive and does not upload it. The hosted publish waits for approval.

  python places/protected_areas.py prove
  python places/protected_areas.py build --source places.geojson --dest out.pmtiles
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from osgeo import gdal, ogr, osr

gdal.UseExceptions()

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECIPE = ROOT / "cookbook" / "recipes" / "places-protected-areas.yaml"
PROJECT = "nature-watch-387210"
REGION = "europe-west1"
IMAGE = "ghcr.io/osgeo/gdal:ubuntu-small-3.11.4"
MACHINE = "e2-standard-2"
SERVICE_ACCOUNT = "637146989512-compute@developer.gserviceaccount.com"
SCRATCH = "gs://nature-watch-bucket/scratch/places/protected-areas/"
SECRET = "naturewatch-facts-database-url"
BATCH_ENV = "NATUREWATCH_PLACES_BATCH"
PROPERTIES = ("id", "area_km2", "cropland_percent")


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
        "simplification_max_zoom": float(tiles.get("simplification_max_zoom", 0)),
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


def lod_name(minzoom: int) -> str:
    return f"places_z{minzoom}"


def wgs84() -> osr.SpatialReference:
    spatial = osr.SpatialReference()
    spatial.ImportFromEPSG(4326)
    spatial.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return spatial


def polygonal_wkb(geom: ogr.Geometry) -> bytes:
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


def load_geojson(path: Path) -> list[PlaceFeature]:
    dataset = gdal.OpenEx(str(path), gdal.OF_VECTOR)
    if dataset is None:
        raise ValueError(f"cannot open {path}")
    layer = dataset.GetLayer(0)
    places = [place_from_feature(feature) for feature in layer]
    dataset = None
    return places


PLACES_SQL = """
select
  places.id::text as id,
  places.area_km2,
  facts.percent as cropland_percent,
  extensions.ST_AsBinary(
    extensions.ST_Multi(
      extensions.ST_CollectionExtract(
        extensions.ST_MakeValid(places.geom),
        3
      )
    )
  ) as geom
from public.places
left join public.facts
  on facts.place_id = places.id
 and facts.sprout_id = %(sprout_id)s
 and facts.method_id = %(method_id)s
where places.geom is not null
order by places.area_km2 desc, places.id
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


def load_places(database_url: str, recipe: dict) -> list[PlaceFeature]:
    contract = places_contract(recipe)
    with connect(database_url) as conn:
        rows = conn.execute(
            PLACES_SQL,
            {
                "sprout_id": contract["sprout_id"],
                "method_id": contract["method_id"],
            },
        ).fetchall()
    places = []
    for row in rows:
        geom = ogr.CreateGeometryFromWkb(bytes(row["geom"]))
        if geom is None:
            raise SystemExit(f"{row['id']} geometry cannot be used")
        try:
            wkb = polygonal_wkb(geom)
        except ValueError as exc:
            raise SystemExit(f"{row['id']} geometry cannot be used: {exc}") from None
        crop = row["cropland_percent"]
        places.append(
            PlaceFeature(
                id=str(row["id"]),
                area_km2=float(row["area_km2"]),
                cropland_percent=None if crop is None else float(crop),
                wkb=wkb,
            )
        )
    return places


def group_by_minzoom(places: list[PlaceFeature], recipe: dict) -> dict[int, list[PlaceFeature]]:
    bands = places_contract(recipe)["minzoom_by_area_km2"]
    groups: dict[int, list[PlaceFeature]] = {}
    for place in places:
        zoom = minzoom_for_area(place.area_km2, bands)
        groups.setdefault(zoom, []).append(place)
    return dict(sorted(groups.items()))


def write_gpkg(places: list[PlaceFeature], dest: Path, recipe: dict) -> dict[str, dict]:
    if dest.exists():
        dest.unlink()
    dataset = ogr.GetDriverByName("GPKG").CreateDataSource(str(dest))
    spatial = wgs84()
    conf = {}
    contract = places_contract(recipe)
    max_zoom = contract["max_zoom"]
    for minzoom, group in group_by_minzoom(places, recipe).items():
        name = lod_name(minzoom)
        layer = dataset.CreateLayer(name, spatial, ogr.wkbMultiPolygon)
        layer.CreateField(ogr.FieldDefn("id", ogr.OFTString))
        layer.CreateField(ogr.FieldDefn("area_km2", ogr.OFTReal))
        layer.CreateField(ogr.FieldDefn("cropland_percent", ogr.OFTReal))
        for place in group:
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetField("id", place.id)
            feature.SetField("area_km2", place.area_km2)
            if place.cropland_percent is not None:
                feature.SetField("cropland_percent", place.cropland_percent)
            feature.SetGeometry(ogr.CreateGeometryFromWkb(place.wkb))
            layer.CreateFeature(feature)
        conf[name] = {
            "target_name": contract["layer"],
            "minzoom": minzoom,
            "maxzoom": max_zoom,
        }
    dataset = None
    return conf


def write_archive(places: list[PlaceFeature], dest: Path, recipe: dict) -> dict:
    if not places:
        raise ValueError("no places to write")
    contract = places_contract(recipe)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    with tempfile.TemporaryDirectory() as folder:
        gpkg = Path(folder) / "places.gpkg"
        conf = write_gpkg(places, gpkg, recipe)
        gdal.VectorTranslate(
            str(dest),
            str(gpkg),
            format="PMTiles",
            makeValid=True,
            datasetCreationOptions=[
                f"MINZOOM={contract['min_zoom']}",
                f"MAXZOOM={contract['max_zoom']}",
                "NAME=places",
                "TYPE=overlay",
                f"SIMPLIFICATION={contract['simplification']}",
                f"SIMPLIFICATION_MAX_ZOOM={contract['simplification_max_zoom']}",
                "CONF=" + json.dumps(conf),
            ],
        )
    stats = {
        "places": len(places),
        "with_cropland": sum(1 for place in places if place.cropland_percent is not None),
        "bytes": dest.stat().st_size,
        "min_zoom": contract["min_zoom"],
        "max_zoom": contract["max_zoom"],
        "lods": {str(zoom): len(group) for zoom, group in group_by_minzoom(places, recipe).items()},
        "path": str(dest),
    }
    return stats


def unique_ids_at_zoom(path: Path, zoom: int) -> set[str]:
    dataset = gdal.OpenEx(
        str(path),
        gdal.OF_VECTOR,
        open_options=[f"ZOOM_LEVEL={zoom}", "CLIP=NO"],
    )
    if dataset is None:
        raise ValueError(f"cannot open {path}")
    ids = set()
    for index in range(dataset.GetLayerCount()):
        layer = dataset.GetLayer(index)
        for feature in layer:
            ids.add(str(feature.GetField("id")))
    dataset = None
    return ids


def properties_at_zoom(path: Path, zoom: int) -> dict[str, dict]:
    dataset = gdal.OpenEx(
        str(path),
        gdal.OF_VECTOR,
        open_options=[f"ZOOM_LEVEL={zoom}", "CLIP=NO"],
    )
    if dataset is None:
        raise ValueError(f"cannot open {path}")
    found = {}
    for index in range(dataset.GetLayerCount()):
        layer = dataset.GetLayer(index)
        for feature in layer:
            place_id = str(feature.GetField("id"))
            if place_id in found:
                continue
            crop = feature.GetField("cropland_percent")
            found[place_id] = {
                "id": place_id,
                "area_km2": feature.GetField("area_km2"),
                "cropland_percent": None if crop is None else float(crop),
            }
    dataset = None
    return found


def prove_archive(places: list[PlaceFeature], path: Path, recipe: dict) -> dict:
    contract = places_contract(recipe)
    at_max = unique_ids_at_zoom(path, contract["max_zoom"])
    expected = {place.id for place in places}
    missing = expected - at_max
    if missing:
        raise SystemExit(f"archive is missing {len(missing)} places at zoom {contract['max_zoom']}")
    extra = at_max - expected
    if extra:
        raise SystemExit(f"archive has {len(extra)} unexpected places")
    z0 = unique_ids_at_zoom(path, contract["min_zoom"])
    bands = contract["minzoom_by_area_km2"]
    world_area = max(area for area, zoom in bands if zoom == contract["min_zoom"])
    too_small = [place.id for place in places if place.id in z0 and place.area_km2 < world_area]
    if too_small:
        raise SystemExit(f"zoom 0 includes places smaller than {world_area} km²")
    copied = properties_at_zoom(path, contract["max_zoom"])
    for place in places:
        row = copied[place.id]
        if abs(float(row["area_km2"]) - place.area_km2) > 1e-6:
            raise SystemExit(f"{place.id} area_km2 was not copied")
        if place.cropland_percent is None:
            if row["cropland_percent"] is not None:
                raise SystemExit(f"{place.id} should have no cropland percent")
        elif abs(row["cropland_percent"] - place.cropland_percent) > 1e-6:
            raise SystemExit(f"{place.id} cropland percent was not copied")
    return {
        "ids_at_min_zoom": len(z0),
        "ids_at_max_zoom": len(at_max),
    }


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
    const archive = new URL("places.pmtiles", window.location.href).href;
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
    map.on("load", () => {
      map.addSource("places", { type: "vector", url: "pmtiles://" + archive });
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


def print_stats(stats: dict) -> None:
    for key, value in stats.items():
        if isinstance(value, dict):
            print(f"{key} {json.dumps(value)}", flush=True)
        else:
            print(f"{key} {value}", flush=True)


def command_build(recipe: dict, source: Path, dest: Path) -> dict:
    places = load_geojson(source)
    stats = write_archive(places, dest, recipe)
    print_stats(stats)
    return stats


def command_prove(recipe: dict, dest: Path) -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set. Export it for this command. Do not commit it.")
    places = load_places(database_url, recipe)
    if not places:
        raise SystemExit("places returned no rows")
    stats = write_archive(places, dest, recipe)
    proof = prove_archive(places, dest, recipe)
    stats.update(proof)
    preview = write_preview(dest)
    stats["preview"] = str(preview)
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


def delete_scratch(prefix: str) -> None:
    folder = gs_to_vsi(prefix).rstrip("/")
    names = gdal.ReadDir(folder) or []
    for name in names:
        if gdal.Unlink(f"{folder}/{name}") != 0:
            print(f"scratch object left in place: {gs_join(prefix, name)}", flush=True)


def batch_commands(script_prefix: str) -> str:
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
        + f"python3 /tmp/in/protected_areas.py run --recipe /tmp/in/recipe.json --scratch {script_prefix}\n"
    )


def batch_job(script_prefix: str) -> dict:
    """One spot VM in europe-west1. Batch deletes it when the task ends."""
    return {
        "taskGroups": [
            {
                "taskCount": "1",
                "taskSpec": {
                    "maxRunDuration": "3600s",
                    "maxRetryCount": 1,
                    "computeResource": {"cpuMilli": "2000", "memoryMib": "8192"},
                    "environment": {"variables": {"CPL_MACHINE_IS_GCE": "YES", BATCH_ENV: "1"}},
                    "runnables": [
                        {
                            "container": {
                                "imageUri": IMAGE,
                                "entrypoint": "sh",
                                "commands": ["-c", batch_commands(script_prefix)],
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


def object_exists(path: str) -> bool:
    listed = subprocess.run(
        ["gcloud", "storage", "ls", path, f"--project={PROJECT}"],
        text=True,
        capture_output=True,
    )
    return listed.returncode == 0 and listed.stdout.strip() != ""


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


def command_run(recipe: dict, dest: Path, upload: bool, scratch: str | None) -> None:
    if os.environ.get(BATCH_ENV) != "1":
        raise SystemExit(
            "The places run is the Batch job in europe-west1. "
            "From this computer, prove locally with: python places/protected_areas.py prove"
        )
    database_url = os.environ.get("DATABASE_URL") or fetch_database_url()
    places = load_places(database_url, recipe)
    if not places:
        raise SystemExit("places returned no rows")
    print(f"places {len(places)}", flush=True)
    stats = write_archive(places, dest, recipe)
    prove_archive(places, dest, recipe)
    print_stats(stats)
    print("proof passed", flush=True)
    if upload:
        published = places_contract(recipe)["path"]
        if object_exists(published):
            raise SystemExit(f"{published} already exists. Delete it before writing again.")
        upload_with_gcloud(dest, published)
        print(f"uploaded {published}", flush=True)
    if scratch:
        delete_scratch(scratch)


def command_submit(recipe_path: Path) -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set. Export it for this command. Do not commit it.")
    recipe = load_recipe(recipe_path)
    published = places_contract(recipe)["path"]
    if object_exists(published):
        raise SystemExit(f"{published} already exists. Delete it before submitting again.")
    job_id = f"places-{recipe['id']}-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}"
    gcloud(["services", "enable", "batch.googleapis.com", "secretmanager.googleapis.com"])
    work = ROOT / "data" / "places" / recipe["id"]
    work.mkdir(parents=True, exist_ok=True)
    recipe_json = work / "recipe.json"
    recipe_json.write_text(json.dumps(recipe) + "\n")
    upload_with_gcloud(Path(__file__).resolve(), gs_join(SCRATCH, "protected_areas.py"))
    upload_with_gcloud(ROOT / "places" / "requirements.txt", gs_join(SCRATCH, "requirements.txt"))
    upload_with_gcloud(recipe_json, gs_join(SCRATCH, "recipe.json"))
    config = batch_job(SCRATCH)
    config_path = work / "batch_job.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    print(config_path.read_text(), flush=True)
    gcloud(["batch", "jobs", "submit", job_id, f"--location={REGION}", f"--config={config_path}"])
    print(f"submitted {job_id} in {REGION}", flush=True)
    print(f"secret {SECRET}", flush=True)
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
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss
    return rss * 1024


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "prove", "run", "submit"))
    parser.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
    parser.add_argument("--source", type=Path)
    parser.add_argument(
        "--dest",
        type=Path,
        default=ROOT / "data" / "places" / "protected-areas" / "places.pmtiles",
    )
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--scratch", default=None)
    args = parser.parse_args()
    started = time.perf_counter()
    try:
        recipe = load_recipe(args.recipe)
        if args.command == "submit":
            command_submit(args.recipe)
        elif args.command == "build":
            if args.source is None:
                raise SystemExit("build needs --source")
            command_build(recipe, args.source, args.dest)
        elif args.command == "prove":
            command_prove(recipe, args.dest)
        else:
            command_run(recipe, args.dest, args.upload, args.scratch)
    finally:
        elapsed = time.perf_counter() - started
        print(f"wall_seconds {elapsed:.1f}")
        print(f"peak_memory_bytes {peak_memory_bytes()}")


if __name__ == "__main__":
    main()
