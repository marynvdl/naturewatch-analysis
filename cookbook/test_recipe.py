"""The cookbook is the only hand-edited catalog.

Run: python -m pytest cookbook
"""
from pathlib import Path

import yaml

RECIPES = Path(__file__).resolve().parent / "recipes"

SEED = "gs://nature-watch-bucket/seeds/cropland/glad/2024/v1/"
SPROUT = "gs://nature-watch-bucket/sprouts/cropland/2024/v1/cropland-2024-zaf.tif"
TILES = [
    "20S_010E.tif",
    "20S_020E.tif",
    "20S_030E.tif",
    "30S_010E.tif",
    "30S_020E.tif",
    "30S_030E.tif",
]


def load_recipes():
    files = sorted(RECIPES.glob("*.yaml"))
    return [(path.name, yaml.safe_load(path.read_text())) for path in files]


def test_the_catalog_lists_the_cropland_and_places_recipes():
    loaded = load_recipes()
    assert [name for name, _ in loaded] == [
        "cropland-2024-v2.yaml",
        "cropland-2024.yaml",
        "places-protected-areas.yaml",
    ]
    recipe = dict(loaded)["cropland-2024.yaml"]
    assert recipe["id"] == "cropland-2024"
    assert recipe["year"] == 2024
    assert recipe["extent"] == "south-africa"


def test_the_places_recipe_names_the_protected_areas_archive():
    recipe = dict(load_recipes())["places-protected-areas.yaml"]
    assert recipe["id"] == "places-protected-areas"
    assert recipe["kind"] == "places"
    assert recipe["source"]["table"] == "public.places"
    assert recipe["facts"]["sprout_id"] == "cropland-2024-v1"
    assert recipe["facts"]["method_id"] == "area-weighted-spheroid-v1"
    assert recipe["tiles"]["path"] == (
        "gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles"
    )
    assert recipe["tiles"]["layer"] == "places"
    assert recipe["tiles"]["scheme"] == "pmtiles"
    assert recipe["tiles"]["min_zoom"] == 0
    assert recipe["tiles"]["max_zoom"] == 12
    assert recipe["tiles"]["properties"] == ["id", "area_km2", "cropland_percent"]


def test_recipe_names_the_seed_and_the_sprout_raster():
    recipe = dict(load_recipes())["cropland-2024.yaml"]
    assert recipe["seed"]["id"] == "seed-cropland-glad-2024-v1"
    assert recipe["seed"]["path"] == SEED
    assert recipe["seed"]["tiles"] == TILES
    assert recipe["sprout"]["id"] == "cropland-2024-v1"
    assert recipe["sprout"]["path"] == SPROUT
    assert "/published/" not in recipe["sprout"]["path"]
    assert recipe["grid"]["crs"] == "EPSG:4326"
    assert recipe["grid"]["pixel_size_deg"] == 0.00025
    assert recipe["grid"]["dtype"] == "uint8"
    assert recipe["seed"]["values"]["cropland"] == 100
    assert recipe["seed"]["values"]["other"] == 0
    assert recipe["seed"]["values"]["nodata"] is None
    assert recipe["seed"]["clip"]["file"] == "cropland-glad-2024-zaf.tif"
    assert recipe["seed"]["clip"]["nodata"] == 255
    assert recipe["seed"]["clip"]["boundary"]["name"] == "geoBoundaries ZAF ADM0"
    assert recipe["sprout"]["values"] == {"cropland": 1, "not_cropland": 0, "nodata": 255}
    assert recipe["sprout"]["outside_extent"] == "nodata"
    assert recipe["sprout"]["from_seed"] == {0: 0, 100: 1, 255: 255}
    assert recipe["tiles"]["path"] == "gs://nature-watch-tiles/cropland/2024/v1/"
    assert recipe["tiles"]["min_zoom"] == 0
    assert recipe["tiles"]["max_zoom"] == 12
    assert recipe["tiles"]["scheme"] == "xyz"
    assert recipe["tiles"]["tile_size"] == 256
    assert recipe["tiles"]["resampling"] == "max"
    assert recipe["tiles"]["encoding"] == "class-value"


def tile_sort_key(name):
    """North to south, then west to east. 000E is longitude 0."""
    lat_s, lon_s = name[:-4].split("_")
    lat = int(lat_s[:2]) * (1 if lat_s[2] == "N" else -1)
    lon = int(lon_s[:3]) * (1 if lon_s[3] == "E" else -1)
    return (-lat, lon)


def test_the_global_recipe_names_version_2_and_every_source_tile():
    recipe = dict(load_recipes())["cropland-2024-v2.yaml"]
    assert recipe["id"] == "cropland-2024-v2"
    assert recipe["year"] == 2024
    assert recipe["extent"] == "global"
    assert recipe["source"]["download"] == "https://glad.umd.edu/datasets/annual-croplands/2024/"
    assert recipe["grid"]["crs"] == "EPSG:4326"
    assert recipe["grid"]["pixel_size_deg"] == 0.00025
    assert recipe["grid"]["dtype"] == "uint8"
    assert recipe["seed"]["id"] == "seed-cropland-glad-2024-v2"
    assert recipe["seed"]["path"] == "gs://nature-watch-bucket/seeds/cropland/glad/2024/v2/"
    assert "clip" not in recipe["seed"]
    assert recipe["seed"]["values"] == {"cropland": 100, "other": 0, "nodata": None}
    tiles = recipe["seed"]["tiles"]
    assert len(tiles) == 223
    assert len(set(tiles)) == 223
    assert tiles == sorted(tiles, key=tile_sort_key)
    assert set(TILES) <= set(tiles)
    assert "20S_020E.tif" in tiles
    assert "00N_000E.tif" in tiles
    assert "50S_070W.tif" in tiles
    assert "00N_010W.tif" not in tiles
    assert "60S_000E.tif" not in tiles
    for name in tiles:
        assert name.endswith(".tif")
        lat_s, lon_s = name[:-4].split("_")
        assert lat_s[2] in "NS" and lon_s[3] in "EW"
        assert len(lat_s) == 3 and len(lon_s) == 4
    assert recipe["sprout"]["id"] == "cropland-2024-v2"
    assert recipe["sprout"]["layout"] == "per-tile"
    assert recipe["sprout"]["path"] == (
        "gs://nature-watch-bucket/sprouts/cropland/2024/v2/cropland-2024.vrt"
    )
    assert recipe["sprout"]["tiles_path"] == (
        "gs://nature-watch-bucket/sprouts/cropland/2024/v2/tiles/"
    )
    assert "/published/" not in recipe["sprout"]["path"]
    assert recipe["sprout"]["values"] == {"cropland": 1, "not_cropland": 0, "nodata": 255}
    assert recipe["sprout"]["outside_extent"] == "nodata"
    assert recipe["sprout"]["from_seed"] == {0: 0, 100: 1}
    assert recipe["tiles"]["path"] == "gs://nature-watch-tiles/cropland/2024/v2/cropland.pmtiles"
    assert recipe["tiles"]["min_zoom"] == 0
    assert recipe["tiles"]["max_zoom"] == 12
    assert recipe["tiles"]["scheme"] == "pmtiles"
    assert recipe["tiles"]["encoding"] == "class-value"
    assert "resampling" not in recipe["tiles"]
