"""Checks for the protected-area archive. They need GDAL and no network.

Run: python -m pytest places
"""
import http.client
import shutil
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "places_protected_areas", Path(__file__).parent / "protected_areas.py"
)
places = importlib.util.module_from_spec(_spec)
sys.modules["places_protected_areas"] = places
_spec.loader.exec_module(places)

RECIPE_PATH = Path(__file__).resolve().parents[1] / "cookbook" / "recipes" / "places-protected-areas.yaml"
RECIPE = json.loads(json.dumps(yaml.safe_load(RECIPE_PATH.read_text())))
SCRIPT = Path(places.__file__)


def low_zoom_recipe():
    recipe = json.loads(json.dumps(RECIPE))
    recipe["tiles"]["max_zoom"] = 8
    return recipe


def write_geojson(path, rows):
    features = []
    for row in rows:
        west, south, east, north = row["bounds"]
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "id": row["id"],
                    "area_km2": row["area_km2"],
                    **(
                        {"cropland_percent": row["cropland_percent"]}
                        if row.get("cropland_percent") is not None
                        else {}
                    ),
                },
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [
                        [
                            [
                                [west, south],
                                [east, south],
                                [east, north],
                                [west, north],
                                [west, south],
                            ]
                        ]
                    ],
                },
            }
        )
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))


def sample_places():
    return [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "area_km2": 19000,
            "cropland_percent": 0.06,
            "bounds": (31.0, -25.0, 32.0, -24.0),
        },
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "area_km2": 150,
            "cropland_percent": 12.5,
            "bounds": (29.0, -26.0, 29.4, -25.6),
        },
        {
            "id": "33333333-3333-3333-3333-333333333333",
            "area_km2": 12,
            "cropland_percent": None,
            "bounds": (28.0, -26.5, 28.04, -26.46),
        },
    ]


def test_minzoom_follows_the_size_bands():
    bands = places.places_contract(RECIPE)["minzoom_by_area_km2"]
    assert places.minzoom_for_area(19000, bands) == 0
    assert places.minzoom_for_area(1000, bands) == 0
    assert places.minzoom_for_area(999, bands) == 2
    assert places.minzoom_for_area(300, bands) == 2
    assert places.minzoom_for_area(299, bands) == 3
    assert places.minzoom_for_area(100, bands) == 3
    assert places.minzoom_for_area(99, bands) == 4
    assert places.minzoom_for_area(30, bands) == 4
    assert places.minzoom_for_area(29, bands) == 6
    assert places.minzoom_for_area(10, bands) == 6


def test_the_recipe_names_the_protected_areas_archive():
    contract = places.places_contract(RECIPE)
    assert contract["path"] == "gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles"
    assert contract["layer"] == "places"
    assert contract["min_zoom"] == 0
    assert contract["max_zoom"] == 12
    assert contract["sprout_id"] == "cropland-2024-v1"
    assert contract["method_id"] == "area-weighted-spheroid-v1"


def test_the_query_reads_places_geom_and_the_stored_cropland_fact():
    assert "from public.places" in places.PLACES_SQL
    assert "places.geom" in places.PLACES_SQL
    assert "pilot_place_map" not in places.PLACES_SQL
    assert "anon" not in places.PLACES_SQL
    assert "facts.percent" in places.PLACES_SQL
    assert "sprout_id" in places.PLACES_SQL
    assert "ST_AsBinary" in places.PLACES_SQL
    assert "ST_AsEWKB" not in places.PLACES_SQL
    # The app database does not validate rows. OGR does that on the build machine.
    assert "ST_MakeValid(places.geom" not in places.PLACES_SQL
    assert "ST_CollectionExtract" not in places.PLACES_SQL
    assert "order by" not in places.PLACES_SQL


def test_places_are_read_with_a_server_side_cursor():
    source = Path(places.__file__).read_text()
    assert "fetchall" not in source
    assert "conn.cursor(name=" in source


def test_each_feature_line_carries_its_own_minzoom(tmp_path):
    rows = {row["id"]: row for row in sample_places()}
    source = tmp_path / "places.geojson"
    write_geojson(source, sample_places())
    lines = {}
    for place in places.places_from_file(source):
        line = json.loads(places.feature_line(place, RECIPE))
        lines[place.id] = line
    large, medium, small = rows
    assert lines[large]["tippecanoe"] == {"minzoom": 0}
    assert lines[medium]["tippecanoe"] == {"minzoom": 3}
    assert lines[small]["tippecanoe"] == {"minzoom": 6}
    assert lines[small]["properties"] == {"id": small, "area_km2": 12}
    assert lines[large]["properties"]["cropland_percent"] == 0.06


def test_tippecanoe_keeps_every_feature():
    command = places.tippecanoe_command(Path("in.ndjson"), Path("out.pmtiles"), RECIPE)
    for flag in ("--no-feature-limit", "--no-tile-size-limit", "--no-tiny-polygon-reduction"):
        assert flag in command
    assert "--drop-densest-as-needed" not in command
    assert "--coalesce-smallest-as-needed" not in command
    assert command[command.index("-z") + 1] == "12"
    assert command[command.index("-l") + 1] == "places"


@pytest.mark.skipif(shutil.which("tippecanoe") is None, reason="needs felt/tippecanoe")
def test_large_places_appear_before_small_ones(tmp_path):
    source = tmp_path / "places.geojson"
    dest = tmp_path / "out" / "places.pmtiles"
    write_geojson(source, sample_places())
    recipe = low_zoom_recipe()
    stats = places.build_and_prove(places.places_from_file(source), dest, recipe)
    assert stats["places"] == 3
    assert stats["with_cropland"] == 2
    assert dest.stat().st_size == stats["bytes"]
    assert set(stats["largest_tile_bytes_by_zoom"]) == set(range(0, 9))
    large, medium, small = (row["id"] for row in sample_places())
    assert places.unique_ids_at_zoom(dest, 0) == {large}
    assert medium not in places.unique_ids_at_zoom(dest, 2)
    assert small not in places.unique_ids_at_zoom(dest, 5)
    assert {large, medium, small} <= places.unique_ids_at_zoom(dest, 6)
    copied = places.properties_at_zoom(dest, 8)
    assert copied[large]["cropland_percent"] == 0.06
    assert copied[small]["cropland_percent"] is None


@pytest.mark.skipif(shutil.which("tippecanoe") is None, reason="needs felt/tippecanoe")
def test_the_proof_fails_when_a_place_appears_too_early(tmp_path):
    source = tmp_path / "places.geojson"
    dest = tmp_path / "places.pmtiles"
    write_geojson(source, sample_places())
    recipe = low_zoom_recipe()
    places.build_and_prove(places.places_from_file(source), dest, recipe)
    wrong = {
        row["id"]: {"minzoom": 8, "area_km2": row["area_km2"], "cropland_percent": row.get("cropland_percent")}
        for row in sample_places()
    }
    with pytest.raises(SystemExit, match="before their minzoom"):
        places.prove_archive(wrong, dest, recipe)


def test_the_batch_job_is_a_spot_machine_and_keeps_the_database_url_out():
    job = places.batch_job(places.SCRATCH)
    policy = job["allocationPolicy"]["instances"][0]["policy"]
    assert policy["provisioningModel"] == "SPOT"
    assert job["allocationPolicy"]["location"]["allowedLocations"] == ["regions/europe-west1"]
    spec = job["taskGroups"][0]["taskSpec"]
    assert spec["environment"]["variables"][places.BATCH_ENV] == "1"
    assert "DATABASE_URL" not in spec["environment"]["variables"]
    container = spec["runnables"][0]["container"]
    assert places.TIPPECANOE_VERSION in container["imageUri"]
    script = container["commands"][1]
    assert "nature-watch-bucket/scratch/places/protected-areas/" in script
    assert 'download("protected_areas.py"' in script
    assert "--replace" not in script
    assert "postgresql://" not in script
    assert places.SECRET not in script
    dumped = str(job)
    assert "postgresql://" not in dumped
    assert "DATABASE_URL" not in dumped


def test_replace_is_passed_to_the_batch_job_only_when_asked():
    job = places.batch_job(places.SCRATCH, replace=True)
    script = job["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1]
    assert "--upload --replace" in script


def test_an_existing_object_is_refused_without_replace(monkeypatch):
    monkeypatch.setenv(places.BATCH_ENV, "1")
    monkeypatch.setattr(places, "object_exists", lambda path: True)
    with pytest.raises(SystemExit, match="--replace"):
        places.command_run(RECIPE, Path("x.pmtiles"), True, None, False, True)
    with pytest.raises(SystemExit, match="--replace"):
        places.command_submit(RECIPE_PATH, False, True)


def test_replace_copies_the_old_object_first_and_sets_no_cache(monkeypatch, tmp_path):
    calls = []
    existing = {"gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles"}
    monkeypatch.setattr(places, "object_exists", lambda path: path in existing)
    monkeypatch.setattr(places, "gcloud", lambda args: calls.append(("gcloud", args)))
    monkeypatch.setattr(
        places, "upload_with_gcloud", lambda local, dest, cache_control=None: calls.append(("upload", dest, cache_control))
    )
    monkeypatch.setattr(places, "check_range", lambda url: 206)
    published = "gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles"
    places.replace_published(tmp_path / "x.pmtiles", published)
    assert calls[0][1][:2] == ["storage", "cp"]
    assert calls[0][1][3].endswith("/v1/places.previous.pmtiles")
    assert calls[1] == ("upload", published, "no-cache")


def test_the_rollback_copy_is_never_overwritten(monkeypatch, tmp_path):
    monkeypatch.setattr(places, "object_exists", lambda path: True)
    with pytest.raises(SystemExit, match="rollback copy"):
        places.replace_published(tmp_path / "x.pmtiles", "gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles")


def test_a_range_that_is_not_206_stops_the_run(monkeypatch, tmp_path):
    monkeypatch.setattr(places, "object_exists", lambda path: False)
    monkeypatch.setattr(places, "upload_with_gcloud", lambda *a, **k: None)
    monkeypatch.setattr(places, "check_range", lambda url: 200)
    with pytest.raises(SystemExit, match="expected 206"):
        places.replace_published(tmp_path / "x.pmtiles", "gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles")


def test_the_preview_loads_the_archive_from_memory():
    html = places.PREVIEW_HTML
    assert "getBytes" in html
    assert "arrayBuffer" in html


def test_byte_ranges_are_inclusive():
    assert places.parse_byte_range(None, 100) == (0, 99, False)
    assert places.parse_byte_range("bytes=0-127", 1000) == (0, 127, True)
    assert places.parse_byte_range("bytes=10-", 50) == (10, 49, True)


def test_the_preview_server_serves_a_byte_range(tmp_path):
    archive = tmp_path / "places.pmtiles"
    archive.write_bytes(bytes(range(256)))
    handler = places.functools.partial(places.ByteRangeHandler, directory=str(tmp_path))
    server = places.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/places.pmtiles", headers={"Range": "bytes=0-7"})
        response = conn.getresponse()
        body = response.read()
        assert response.status == 206
        assert response.getheader("Content-Length") == "8"
        assert response.getheader("Content-Range") == "bytes 0-7/256"
        assert body == bytes(range(8))
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_the_laptop_cannot_start_the_places_run():
    result = subprocess.run([sys.executable, str(SCRIPT), "run"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "Batch job" in result.stderr
