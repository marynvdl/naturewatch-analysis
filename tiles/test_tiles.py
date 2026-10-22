"""Checks for the cropland tiles. They need GDAL and no network.

Run: python -m pytest tiles
"""
import array
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import yaml
from osgeo import gdal, osr

_spec = importlib.util.spec_from_file_location("tiles_cropland", Path(__file__).parent / "cropland.py")
cropland = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cropland)

RECIPE_PATH = Path(__file__).resolve().parents[1] / "cookbook" / "recipes" / "cropland-2024.yaml"
RECIPE = json.loads(json.dumps(yaml.safe_load(RECIPE_PATH.read_text())))
SCRIPT = Path(cropland.__file__)


def write_raster(path, values, nodata=255, pixel=0.05, origin=(27.0, -27.0)):
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


def low_zoom_recipe():
    recipe = json.loads(json.dumps(RECIPE))
    recipe["tiles"]["max_zoom"] = 4
    return recipe


def field_with_a_nodata_edge():
    """About 16° across, so zoom 0 can see it. One cropland pixel on the nodata edge."""
    rows = []
    for y in range(80):
        row = []
        for x in range(80):
            if x >= 40:
                row.append(255)
            elif x == 39 and y == 20:
                row.append(1)
            else:
                row.append(0)
        rows.append(row)
    return rows


def read_png(path):
    dataset = gdal.Open(str(path))
    array = dataset.ReadAsArray()
    dataset = None
    return array


def test_xyz_index_matches_the_slippy_map():
    assert cropland.tile_index(27.0, -27.0, 12) == (2355, 2367)


def test_max_keeps_one_cropland_pixel_beside_nodata(tmp_path):
    source = tmp_path / "sprout.tif"
    write_raster(source, field_with_a_nodata_edge(), pixel=0.2, origin=(16.0, -22.0))
    stats = cropland.build_tiles(source, tmp_path / "tiles", low_zoom_recipe())
    assert stats["min_zoom"] == 0
    assert stats["max_zoom"] == 4
    assert stats["tiles"] >= 1
    assert stats["bytes"] == sum(path.stat().st_size for path in (tmp_path / "tiles").rglob("*.png"))
    pngs = list((tmp_path / "tiles").rglob("*.png"))
    saw_cropland = False
    saw_clear = False
    for path in pngs:
        bands = read_png(path)
        assert bands.shape == (2, 256, 256)
        gray, alpha = bands
        opaque = alpha == 255
        assert set(np.unique(gray[opaque]).tolist()) <= {0, 1}
        assert int((alpha == 0).sum()) + int(opaque.sum()) == 256 * 256
        if (gray[opaque] == 1).any():
            saw_cropland = True
        if (alpha == 0).any():
            saw_clear = True
        # A transparent pixel must not keep a class value.
        assert int(gray[alpha == 0].sum()) == 0
    assert saw_cropland
    assert saw_clear
    zoom0 = tmp_path / "tiles" / "0" / "0" / "0.png"
    assert zoom0.exists()
    assert (read_png(zoom0)[0] == 1).any()


def test_a_nodata_raster_writes_no_tiles(tmp_path):
    source = tmp_path / "empty.tif"
    write_raster(source, [[255, 255], [255, 255]])
    stats = cropland.build_tiles(source, tmp_path / "tiles", low_zoom_recipe())
    assert stats["tiles"] == 0
    assert stats["bytes"] == 0
    assert list((tmp_path / "tiles").rglob("*.png")) == []


def test_an_unknown_class_is_refused(tmp_path):
    source = tmp_path / "bad.tif"
    rows = field_with_a_nodata_edge()
    rows[20][10] = 7
    write_raster(source, rows, pixel=0.2, origin=(16.0, -22.0))
    try:
        cropland.build_tiles(source, tmp_path / "tiles", low_zoom_recipe())
    except ValueError as error:
        assert "7" in str(error)
    else:
        raise AssertionError("expected value 7 to fail")


def test_a_local_folder_can_be_synced(tmp_path):
    source = tmp_path / "tiles" / "0" / "0"
    source.mkdir(parents=True)
    (source / "0.png").write_bytes(b"png")
    dest = tmp_path / "copied"
    cropland.sync_tree(tmp_path / "tiles", str(dest))
    assert (dest / "0" / "0" / "0.png").read_bytes() == b"png"


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
    assert "nature-watch-bucket/scratch/tiles/cropland-2024/" in script
    assert 'download("cropland.py"' in script
    assert "/tmp/in/cropland.py run" in script
    assert job["taskGroups"][0]["taskSpec"]["environment"]["variables"]["NATUREWATCH_TILES_BATCH"] == "1"


def test_the_laptop_cannot_start_the_tile_run():
    result = subprocess.run([sys.executable, str(SCRIPT), "run"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "Batch job" in result.stderr
