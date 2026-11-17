"""Checks for the Buildings 2023 seed job. They need GDAL and no network.

Run: python -m pytest seed
"""
import hashlib
import json
from pathlib import Path

import numpy as np
import yaml
from osgeo import gdal, osr

import buildings as b

ROOT = Path(__file__).resolve().parents[1]
RECIPE = yaml.safe_load((ROOT / "cookbook" / "recipes" / "buildings-2023-v1.yaml").read_text())
BLOCKS = json.loads((ROOT / "gee" / "buildings" / "google-2023-blocks.json").read_text())["blocks"]
URL = "https://opendata.aiforgood.ai/building-density/v1/2023q4/12-39-p3_ensemble_downsampled/L15-{x:04d}E-{y:04d}N.tif"


def write(path, array, gt, epsg, dtype, nodata):
    ds = gdal.GetDriverByName("GTiff").Create(str(path), array.shape[1], array.shape[0], 1, dtype)
    ds.SetGeoTransform(gt)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(nodata)
    band.WriteArray(array)
    ds.FlushCache()
    return ds


def test_url_list_hash_sorts_bytewise_with_a_trailing_newline():
    urls = ["b", "a", "B"]
    assert b.url_list_sha256(urls) == hashlib.sha256(b"B\na\nb\n").hexdigest()


def test_tile_names_count_rows_from_the_south():
    assert b.tile_xy(URL.format(x=1057, y=1152), 11) == (1057, 895)
    with __import__("pytest").raises(ValueError):
        b.tile_xy("https://example.org/other.tif", 11)


def test_tile_bounds_match_a_real_tempo_tile():
    # gdalinfo of L15-1057E-1152N.tif on 17 November 2026
    west, _, _, north = b.tile_bounds(1057, 895, 11)
    assert abs(west - 645740.014953169) < 1e-3
    assert abs(north - 2524256.42208966) < 1e-3
    assert abs(b.pixel_size(11) - 76.437028285176) < 1e-9


def test_tiles_group_into_zoom_4_mosaics():
    urls = [URL.format(x=x, y=2047 - y) for x, y in ((0, 0), (127, 127), (128, 0), (1057, 895))]
    groups = b.group_by_mosaic(urls, 11, 4)
    assert {k: len(v) for k, v in groups.items()} == {(0, 0): 2, (1, 0): 1, (8, 6): 1}
    assert b.mosaic_name(RECIPE, 8, 6) == "z4-8-6.tif"
    w, s, e, n = b.tile_bounds(8, 6, 4)
    assert abs((e - w) / b.pixel_size(11) - 32768) < 1e-6


def test_a_good_tempo_tile_passes_and_a_bad_one_fails(tmp_path):
    west, _, _, north = b.tile_bounds(1057, 895, 11)
    res = b.pixel_size(11)
    values = np.zeros((256, 256), dtype=np.float32)
    values[0, :3] = [-1, 0.5, 1]
    good = write(tmp_path / "good.tif", values, (west, res, 0, north, 0, -res), 3857, gdal.GDT_Float32, -1)
    assert b.tempo_problems(good, 1057, 895, 11, -1) == []
    values[1, 0] = 1.5
    bad = write(tmp_path / "bad.tif", values, (west + res, res, 0, north, 0, -res), 3857, gdal.GDT_Float32, -1)
    problems = b.tempo_problems(bad, 1057, 895, 11, -1)
    assert any("geotransform" in p for p in problems)
    assert any("outside 0-1" in p for p in problems)


def test_a_google_block_is_checked_and_counted(tmp_path, monkeypatch):
    # A 10 degree block at 0.00025 is 40,000 px. Use a 1 degree block at the same cell size.
    recipe = json.loads(json.dumps(RECIPE))
    recipe["seeds"]["google"]["blocks_deg"] = 1
    values = np.full((4000, 4000), 255, dtype=np.uint8)
    values[:10, :10] = 0
    values[0, :3] = [1, 250, 7]
    ds = write(tmp_path / "g.tif", values, (5, 0.00025, 0, 21, 0, -0.00025), 4326, gdal.GDT_Byte, 255)
    problems, histogram = b.google_problems(ds, 5, 20, recipe)
    assert problems == []
    assert sum(histogram) == 4000 * 4000
    assert histogram[255] == 4000 * 4000 - 100
    assert histogram[250] == histogram[1] == histogram[7] == 1
    values[5, 5] = 252
    ds = write(tmp_path / "h.tif", values, (5, 0.00025, 0, 21, 0, -0.00025), 4326, gdal.GDT_Byte, 255)
    problems, _ = b.google_problems(ds, 5, 20, recipe)
    assert problems == ["1 cells between 251 and 254"]


def test_google_object_names_match_the_export_script():
    names = b.google_names(BLOCKS)
    assert len(names) == len(set(names)) == 145
    assert "W120S30.tif" in names


def test_the_job_is_one_spot_machine_that_resumes():
    job = b.batch_job(b.SCRATCH)
    policy = job["allocationPolicy"]["instances"][0]["policy"]
    assert policy["provisioningModel"] == "SPOT"
    assert policy["machineType"] == "e2-standard-2"
    assert job["taskGroups"][0]["taskSpec"]["maxRetryCount"] >= 1
    assert "buildings.py run" in job["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1]
