"""Checks for the protected-area archive. They need GDAL and no network.

Run: python -m pytest places
"""
import json
import subprocess
import sys
from pathlib import Path

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
            "area_km2": 1500,
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
    assert places.minzoom_for_area(10000, bands) == 0
    assert places.minzoom_for_area(9999, bands) == 3
    assert places.minzoom_for_area(1000, bands) == 3
    assert places.minzoom_for_area(999, bands) == 6
    assert places.minzoom_for_area(100, bands) == 6
    assert places.minzoom_for_area(99, bands) == 8
    assert places.minzoom_for_area(10, bands) == 8
    assert places.minzoom_for_area(9, bands) == 8


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
    assert "ST_MakeValid" in places.PLACES_SQL
    assert "ST_AsBinary" in places.PLACES_SQL
    assert "ST_AsEWKB" not in places.PLACES_SQL


def test_large_places_appear_before_small_ones(tmp_path):
    source = tmp_path / "places.geojson"
    dest = tmp_path / "places.pmtiles"
    write_geojson(source, sample_places())
    recipe = low_zoom_recipe()
    loaded = places.load_geojson(source)
    stats = places.write_archive(loaded, dest, recipe)
    assert stats["places"] == 3
    assert stats["with_cropland"] == 2
    assert dest.stat().st_size == stats["bytes"]
    large, medium, small = (row["id"] for row in sample_places())
    at0 = places.unique_ids_at_zoom(dest, 0)
    at3 = places.unique_ids_at_zoom(dest, 3)
    at6 = places.unique_ids_at_zoom(dest, 6)
    at8 = places.unique_ids_at_zoom(dest, 8)
    assert at0 == {large}
    assert medium not in at0
    assert small not in at0
    assert medium in at3
    assert small not in at3
    assert small not in at6
    assert {large, medium, small} <= at8
    copied = places.properties_at_zoom(dest, 8)
    assert copied[large]["area_km2"] == 19000
    assert copied[large]["cropland_percent"] == 0.06
    assert copied[small]["cropland_percent"] is None
    places.prove_archive(loaded, dest, recipe)


def test_the_batch_job_is_a_spot_machine_and_keeps_the_database_url_out():
    job = places.batch_job(places.SCRATCH)
    policy = job["allocationPolicy"]["instances"][0]["policy"]
    assert policy["machineType"] == "e2-standard-2"
    assert policy["provisioningModel"] == "SPOT"
    assert job["allocationPolicy"]["location"]["allowedLocations"] == ["regions/europe-west1"]
    spec = job["taskGroups"][0]["taskSpec"]
    assert spec["environment"]["variables"][places.BATCH_ENV] == "1"
    assert "DATABASE_URL" not in spec["environment"]["variables"]
    script = spec["runnables"][0]["container"]["commands"][1]
    assert "nature-watch-bucket/scratch/places/protected-areas/" in script
    assert 'download("protected_areas.py"' in script
    assert "postgresql://" not in script
    assert places.SECRET not in script
    dumped = str(job)
    assert "postgresql://" not in dumped
    assert "DATABASE_URL" not in dumped


def test_the_laptop_cannot_start_the_places_run():
    result = subprocess.run([sys.executable, str(SCRIPT), "run"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "Batch job" in result.stderr
