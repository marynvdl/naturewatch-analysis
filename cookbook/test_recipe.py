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


def test_the_only_hand_edited_catalog_is_the_cropland_recipe():
    loaded = load_recipes()
    assert [name for name, _ in loaded] == ["cropland-2024.yaml"]
    recipe = loaded[0][1]
    assert recipe["id"] == "cropland-2024"
    assert recipe["year"] == 2024
    assert recipe["extent"] == "south-africa"


def test_recipe_names_the_seed_and_the_sprout_raster():
    recipe = load_recipes()[0][1]
    assert recipe["seed"]["id"] == "seed-cropland-glad-2024-v1"
    assert recipe["seed"]["path"] == SEED
    assert recipe["seed"]["tiles"] == TILES
    assert recipe["sprout"]["id"] == "sprout-cropland-2024-v1"
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
