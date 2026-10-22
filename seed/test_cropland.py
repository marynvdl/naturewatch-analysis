"""Checks for the cropland seed. They need GDAL and no network.

Run: python -m pytest seed
"""
import array
from pathlib import Path

import yaml
from osgeo import gdal, osr

import cropland

RECIPE = yaml.safe_load((Path(__file__).resolve().parents[1] / "cookbook" / "recipes" / "cropland-2024.yaml").read_text())


def write_tile(path, origin, values, nodata=None, pixel=0.00025, crs="EPSG:4326"):
    height = len(values)
    width = len(values[0])
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), width, height, 1, gdal.GDT_Byte)
    dataset.SetGeoTransform((origin[0], pixel, 0, origin[1], 0, -pixel))
    spatial = osr.SpatialReference()
    spatial.ImportFromEPSG(int(crs.split(":")[1]))
    spatial.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dataset.SetProjection(spatial.ExportToWkt())
    band = dataset.GetRasterBand(1)
    flat = array.array("B", [value for row in values for value in row])
    band.WriteRaster(0, 0, width, height, flat.tobytes())
    if nodata is not None:
        band.SetNoDataValue(nodata)
    dataset.FlushCache()
    dataset = None


def test_pending_codes_stop_a_full_download():
    recipe = {"seed": {"values": {"cropland": 100, "other": "confirm-from-one-tile", "nodata": None}}}
    assert cropland.pending_values(recipe) == ["other"]
    assert cropland.pending_values(RECIPE) == []


def test_a_matching_tile_passes_and_a_changed_grid_fails(tmp_path):
    path = tmp_path / "tile.tif"
    width = 4
    pixel = 2.5
    values = [[0, 100, 0, 0] for _ in range(width)]
    write_tile(path, (20, -20), values, pixel=pixel)
    info = cropland.inspect_raster(path)
    recipe = yaml.safe_load(yaml.safe_dump(RECIPE))
    recipe["grid"]["pixel_size_deg"] = pixel
    assert cropland.check_tile(info, recipe) == []

    recipe["grid"]["crs"] = "EPSG:3857"
    assert any("CRS" in problem for problem in cropland.check_tile(info, recipe))


def test_an_unexpected_code_fails(tmp_path):
    path = tmp_path / "tile.tif"
    values = [[0, 7], [100, 0]]
    write_tile(path, (10, -30), values, pixel=5)
    info = cropland.inspect_raster(path)
    recipe = yaml.safe_load(yaml.safe_dump(RECIPE))
    recipe["grid"]["pixel_size_deg"] = 5
    problems = cropland.check_tile(info, recipe)
    assert any("unexpected values [7]" in problem for problem in problems)


def test_value_100_must_appear_somewhere(tmp_path):
    path = tmp_path / "tile.tif"
    write_tile(path, (30, -30), [[0, 0], [0, 0]], pixel=5)
    info = cropland.inspect_raster(path)
    recipe = yaml.safe_load(yaml.safe_dump(RECIPE))
    recipe["grid"]["pixel_size_deg"] = 5
    assert cropland.check_tile(info, recipe) == []
    assert any("100" in problem for problem in cropland.check_tiles([info], recipe))


def test_distant_islands_are_left_out_of_the_cutline():
    mainland = cropland.envelope_polygon((16, 33, -35, -22))
    island = cropland.envelope_polygon((37, 38, -47, -46))
    tiles = (10, 40, -40, -20)
    kept = cropland.polygons_meeting([mainland, island], tiles)
    assert len(kept) == 1
    assert kept[0].Intersects(mainland)
    assert not kept[0].Intersects(island)


def test_clip_keeps_source_codes_inside_and_nodata_outside(tmp_path):
    tile = tmp_path / "20S_020E.tif"
    values = [
        [0, 0, 0, 0],
        [0, 100, 100, 0],
        [0, 100, 0, 0],
        [0, 0, 0, 0],
    ]
    write_tile(tile, (20, -20), values, pixel=1)
    boundary = tmp_path / "boundary.geojson"
    # Covers the centre of the tile, not the whole tile, so the snapped edge is outside.
    polygon = cropland.envelope_polygon((21.2, 22.2, -22.2, -21.2))
    cropland.write_polygons(boundary, [polygon])
    dest = tmp_path / "clip.tif"
    cropland.clip_tiles([tile], boundary, dest, nodata=255, pixel=1)
    info = cropland.inspect_raster(dest)
    recipe = yaml.safe_load(yaml.safe_dump(RECIPE))
    recipe["grid"]["pixel_size_deg"] = 1
    assert cropland.check_clip(info, recipe) == []
    assert cropland.read_pixel(dest, 21.5, -21.5) == 100
    assert cropland.read_pixel(dest, 22.5, -21.5) == 255
