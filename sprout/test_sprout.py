"""Checks for the cropland sprout. They need GDAL and no network.

Run: python -m pytest sprout
"""
import array
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml
from osgeo import gdal, osr

_spec = importlib.util.spec_from_file_location("sprout_cropland", Path(__file__).parent / "cropland.py")
cropland = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cropland)

RECIPE_PATH = Path(__file__).resolve().parents[1] / "cookbook" / "recipes" / "cropland-2024.yaml"
RECIPE = json.loads(json.dumps(yaml.safe_load(RECIPE_PATH.read_text())))
SCRIPT = Path(cropland.__file__)


def write_clip(path, values, nodata=255, pixel=0.00025, origin=(16.0, -22.0)):
    height = len(values)
    width = len(values[0])
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), width, height, 1, gdal.GDT_Byte)
    dataset.SetGeoTransform((origin[0], pixel, 0, origin[1], 0, -pixel))
    spatial = osr.SpatialReference()
    spatial.ImportFromEPSG(4326)
    spatial.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dataset.SetProjection(spatial.ExportToWkt())
    band = dataset.GetRasterBand(1)
    flat = array.array("B", [value for row in values for value in row])
    band.WriteRaster(0, 0, width, height, flat.tobytes())
    band.SetNoDataValue(nodata)
    dataset.FlushCache()
    dataset = None


def south_africa_like(width=1024, height=512):
    """Left half is other land with one cropland pixel. Right half is nodata."""
    rows = []
    for y in range(height):
        row = []
        for x in range(width):
            if x >= width // 2:
                row.append(255)
            elif x == 100 and y == 100:
                row.append(100)
            else:
                row.append(0)
        rows.append(row)
    return rows


def test_remap_writes_a_versioned_cog_and_keeps_cropland_in_the_overview(tmp_path):
    clip = tmp_path / "clip.tif"
    values = south_africa_like()
    write_clip(clip, values)
    dest = tmp_path / "cropland-2024-zaf.tif"
    clip_facts, sprout = cropland.write_sprout(clip, dest, RECIPE, tmp_path)
    assert cropland.check_sprout(clip_facts, sprout, RECIPE) == []
    assert sprout["layout"] == "COG"
    assert sprout["crs"] == "EPSG:4326"
    assert sprout["nodata"] == 255
    assert sprout["version"] == "v1"
    assert sprout["class_values"] == "cropland:1,not_cropland:0,nodata:255"
    assert sprout["counts"][1] == 1
    assert 100 not in sprout["counts"]
    assert sprout["counts"][0] == 1024 * 512 // 2 - 1
    assert sprout["counts"][255] == 1024 * 512 // 2

    dataset = gdal.Open(str(dest))
    band = dataset.GetRasterBand(1)
    assert band.ReadRaster(100, 100, 1, 1)[0] == 1
    assert band.ReadRaster(10, 10, 1, 1)[0] == 0
    assert band.ReadRaster(800, 10, 1, 1)[0] == 255
    overview = band.GetOverview(0)
    assert overview.ReadRaster(50, 50, 1, 1)[0] == 1
    assert overview.ReadRaster(400, 50, 1, 1)[0] == 255
    dataset = None


def test_an_unmapped_clip_value_is_refused():
    with_seven = {"0": 10, "7": 4, "100": 1}
    try:
        cropland.require_mapped(with_seven, RECIPE)
    except ValueError as error:
        assert "7" in str(error)
    else:
        raise AssertionError("expected an unmapped value to fail")


def test_stac_files_are_generated_from_the_recipe(tmp_path):
    clip = tmp_path / "clip.tif"
    write_clip(clip, south_africa_like(width=600, height=400))
    dest = tmp_path / "cropland-2024-zaf.tif"
    _clip_facts, sprout = cropland.write_sprout(clip, dest, RECIPE, tmp_path)
    collection, item = cropland.stac_documents(RECIPE, sprout)
    assert collection["id"] == "sprout-cropland-2024-v1"
    assert collection["license"] == "other"
    assert "Khan et al. 2026" in collection["description"]
    assert collection["extent"]["temporal"]["interval"] == [["2024-01-01T00:00:00Z", "2025-01-01T00:00:00Z"]]
    assert item["collection"] == collection["id"]
    assert item["assets"]["data"]["href"] == RECIPE["sprout"]["path"]
    classes = item["assets"]["data"]["raster:bands"][0]["classification:classes"]
    assert classes == [
        {"value": 1, "name": "cropland"},
        {"value": 0, "name": "not_cropland"},
        {"value": 255, "name": "nodata", "nodata": True},
    ]
    assert item["assets"]["data"]["raster:bands"][0]["nodata"] == 255
    paths = cropland.write_stac(RECIPE, sprout, tmp_path)
    assert [path.name for path in paths] == ["collection.json", "cropland-2024-zaf.json"]


def test_the_batch_job_is_a_spot_machine_next_to_the_bucket():
    job = cropland.batch_job(cropland.SCRATCH)
    policy = job["allocationPolicy"]["instances"][0]["policy"]
    assert policy["machineType"] == "e2-standard-2"
    assert policy["provisioningModel"] == "SPOT"
    assert job["allocationPolicy"]["location"]["allowedLocations"] == ["regions/europe-west1"]
    spec = job["taskGroups"][0]["taskSpec"]
    assert "volumes" not in spec
    runnable = spec["runnables"][0]["container"]
    assert runnable["imageUri"] == "ghcr.io/osgeo/gdal:ubuntu-small-3.11.4"
    assert runnable["entrypoint"] == "sh"
    script = runnable["commands"][1]
    assert "nature-watch-bucket/scratch/sprout/cropland-2024/" in script
    assert 'download("cropland.py"' in script
    assert "/tmp/in/cropland.py run" in script
    assert job["taskGroups"][0]["taskSpec"]["environment"]["variables"]["NATUREWATCH_SPROUT_BATCH"] == "1"


def test_the_laptop_cannot_start_the_raster_run():
    result = subprocess.run([sys.executable, str(SCRIPT), "run"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "Batch job" in result.stderr
