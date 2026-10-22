"""Checks for cropland facts. They need GDAL, exactextract, and no network.

Run: python -m pytest facts
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml
from osgeo import gdal, osr
from shapely.geometry import MultiPolygon, box

_spec = importlib.util.spec_from_file_location("facts_cropland", Path(__file__).parent / "cropland.py")
cropland = importlib.util.module_from_spec(_spec)
sys.modules["facts_cropland"] = cropland
_spec.loader.exec_module(cropland)

RECIPE_PATH = Path(__file__).resolve().parents[1] / "cookbook" / "recipes" / "cropland-2024.yaml"
RECIPE = yaml.safe_load(RECIPE_PATH.read_text())
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "naturewatch-app"
    / "supabase"
    / "migrations"
    / "20261022190000_fact_methods.sql"
)
SCRIPT = Path(cropland.__file__)
CODES = cropland.class_codes(RECIPE)
SRS = cropland.wgs84_wkt()
# One source pixel, near 26°S. Two columns, one row.
ORIGIN = (28.0, -26.0)
PIXEL = 0.00025


def place(place_id, area, seconds=1.0, geom=None):
    if geom is None:
        geom = MultiPolygon([box(0, 0, 1, 1)])
    row = cropland.Place(place_id, place_id, area, geom)
    return row


def result(place_id, area, seconds):
    return cropland.PlaceResult(place(place_id, area), 0.0, 0.0, seconds)


def write_raster(path, values, pixel=PIXEL, origin=ORIGIN, nodata=255, version="v1", class_values=None):
    height = len(values)
    width = len(values[0])
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), width, height, 1, gdal.GDT_Byte)
    dataset.SetGeoTransform((origin[0], pixel, 0, origin[1], 0, -pixel))
    spatial = osr.SpatialReference()
    spatial.ImportFromEPSG(4326)
    spatial.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dataset.SetProjection(spatial.ExportToWkt())
    band = dataset.GetRasterBand(1)
    band.WriteArray(np.array(values, dtype=np.uint8))
    band.SetNoDataValue(nodata)
    if version is not None:
        dataset.SetMetadataItem("version", version)
    if class_values is not None:
        dataset.SetMetadataItem("class_values", class_values)
    dataset.FlushCache()
    dataset = None


def extent_for(values, pixel=PIXEL, origin=ORIGIN):
    height = len(values)
    width = len(values[0])
    xmin, ymax = origin
    return xmin, ymax - height * pixel, xmin + width * pixel, ymax


def test_pixel_area_uses_the_wgs84_spheroid():
    area = cropland.pixel_area_km2(1, 0, 1)
    assert area == pytest.approx(12308.778, abs=0.001)
    south = cropland.pixel_area_km2(PIXEL, -26, -26 + PIXEL)
    assert 0.0006 < south < 0.0008


def test_stored_facts_keep_full_precision():
    area, percent, coverage = cropland.fact_values(1, 3, 4)
    assert area == 1
    assert percent == pytest.approx(100 / 3)
    assert percent != round(percent, 1)
    assert coverage == 0.75
    assert cropland.fact_values(0, 0, 10) == (0.0, 0.0, 0.0)


def test_exact_count_matches_the_intersection_count():
    values = np.array([[1, 0, 255]], dtype=np.uint8)
    extent = extent_for(values)
    polygon = box(ORIGIN[0], ORIGIN[1] - PIXEL, ORIGIN[0] + 1.5 * PIXEL, ORIGIN[1])
    exact = cropland.summarize_arrays(values, extent, polygon, CODES, SRS)
    reference = cropland.summarize_reference(values, extent, polygon, CODES)
    assert exact[0] == pytest.approx(reference[0], abs=1e-9)
    assert exact[1] == pytest.approx(reference[1], abs=1e-9)
    row_area = cropland.pixel_area_km2(PIXEL, ORIGIN[1] - PIXEL, ORIGIN[1])
    assert exact[0] == pytest.approx(row_area, abs=1e-9)
    assert exact[1] == pytest.approx(1.5 * row_area, abs=1e-9)


def test_nodata_and_a_miss_are_not_valid():
    values = np.array([[255, 255]], dtype=np.uint8)
    extent = extent_for(values)
    polygon = box(*extent)
    assert cropland.summarize_arrays(values, extent, polygon, CODES, SRS) == (0.0, 0.0)
    outside = box(ORIGIN[0] + 1, ORIGIN[1] - PIXEL, ORIGIN[0] + 1 + PIXEL, ORIGIN[1])
    assert cropland.summarize_arrays(values, extent, outside, CODES, SRS) == (0.0, 0.0)


def test_percent_uses_valid_area_and_coverage_uses_the_place_area():
    values = np.array([[1, 0]], dtype=np.uint8)
    extent = extent_for(values)
    polygon = box(*extent)
    cropland_km2, valid_km2 = cropland.summarize_arrays(values, extent, polygon, CODES, SRS)
    _area, percent, coverage = cropland.fact_values(cropland_km2, valid_km2, valid_km2)
    assert percent == pytest.approx(50)
    assert coverage == pytest.approx(1)

    nodata_row = np.array([[1, 255]], dtype=np.uint8)
    cropland_km2, valid_km2 = cropland.summarize_arrays(nodata_row, extent, polygon, CODES, SRS)
    place_area = valid_km2 * 2
    _area, percent, coverage = cropland.fact_values(cropland_km2, valid_km2, place_area)
    assert percent == pytest.approx(100)
    assert coverage == pytest.approx(0.5)


def test_overlapping_places_each_keep_the_shared_ground():
    values = np.array([[1, 1, 1]], dtype=np.uint8)
    extent = extent_for(values)
    xmin, ymin, _xmax, ymax = extent
    left = box(xmin, ymin, xmin + 1.5 * PIXEL, ymax)
    right = box(xmin + PIXEL, ymin, xmin + 3 * PIXEL, ymax)
    union = box(xmin, ymin, xmin + 3 * PIXEL, ymax)
    left_area = cropland.summarize_arrays(values, extent, left, CODES, SRS)[0]
    right_area = cropland.summarize_arrays(values, extent, right, CODES, SRS)[0]
    union_area = cropland.summarize_arrays(values, extent, union, CODES, SRS)[0]
    assert left_area + right_area > union_area
    reference_union = cropland.summarize_reference(values, extent, union, CODES)[0]
    assert union_area == pytest.approx(reference_union, abs=1e-9)


def test_a_stray_pixel_value_stops_the_count():
    values = np.array([[1, 2]], dtype=np.uint8)
    with pytest.raises(ValueError, match="values"):
        cropland.summarize_arrays(values, extent_for(values), box(*extent_for(values)), CODES, SRS)


def test_row_strips_match_one_window(tmp_path):
    values = [[1, 0], [255, 1]]
    path = tmp_path / "strips.tif"
    write_raster(path, values, version=None, class_values=None)
    dataset = gdal.Open(str(path))
    polygon = box(*extent_for(values))
    whole = cropland.summarize_exact(dataset, polygon, CODES, SRS)
    strips = cropland.summarize_exact(dataset, polygon, CODES, SRS, max_pixels=2)
    assert strips[0] == pytest.approx(whole[0], abs=1e-12)
    assert strips[1] == pytest.approx(whole[1], abs=1e-12)
    right = box(ORIGIN[0] + PIXEL, ORIGIN[1] - 2 * PIXEL, ORIGIN[0] + 2 * PIXEL, ORIGIN[1])
    assert cropland.summarize_exact(dataset, right, CODES, SRS)[0] == pytest.approx(
        cropland.pixel_area_km2(PIXEL, ORIGIN[1] - 2 * PIXEL, ORIGIN[1] - PIXEL),
        abs=1e-9,
    )
    dataset = None


def test_the_sprout_must_carry_the_recipe_version(tmp_path):
    path = tmp_path / "sprout.tif"
    class_values = "cropland:1,not_cropland:0,nodata:255"
    write_raster(path, [[1, 0]], class_values=class_values)
    dataset = gdal.Open(str(path))
    cropland.require_sprout(dataset, RECIPE)
    dataset = None
    write_raster(path, [[1, 0]], version="v2", class_values=class_values)
    dataset = gdal.Open(str(path))
    with pytest.raises(ValueError, match="version"):
        cropland.require_sprout(dataset, RECIPE)
    dataset = None


def test_check_places_are_two_small_parks_and_the_largest():
    places = [place("a", 12), place("b", 80), place("c", 40), place("d", 9)]
    chosen = cropland.choose_check_places(places)
    assert [item.id for item in chosen["intersection"]] == ["a", "c"]
    assert chosen["pixel_center"][0].id == "b"
    with pytest.raises(ValueError, match="between 10 and 50"):
        cropland.choose_check_places([place("a", 12), place("b", 80)])


def test_five_largest_places_can_dominate_the_counting_time():
    rows = [result(str(index), area, seconds) for index, (area, seconds) in enumerate([
        (10, 1),
        (20, 1),
        (30, 1),
        (100, 10),
        (200, 10),
        (300, 10),
    ])]
    _largest, share = cropland.largest_share(rows)
    assert share == pytest.approx(32 / 33)
    assert share >= cropland.DOMINANCE_SHARE


def test_the_batch_job_is_a_spot_machine_and_keeps_the_database_url_out():
    job = cropland.batch_job(cropland.SCRATCH)
    policy = job["allocationPolicy"]["instances"][0]["policy"]
    assert policy["machineType"] == "e2-standard-4"
    assert policy["provisioningModel"] == "SPOT"
    assert job["allocationPolicy"]["location"]["allowedLocations"] == ["regions/europe-west1"]
    spec = job["taskGroups"][0]["taskSpec"]
    assert spec["environment"]["variables"][cropland.BATCH_ENV] == "1"
    assert "DATABASE_URL" not in spec["environment"]["variables"]
    script = spec["runnables"][0]["container"]["commands"][1]
    assert "nature-watch-bucket/scratch/facts/cropland-2024/" in script
    assert 'download("cropland.py"' in script
    assert "postgresql://" not in script
    assert cropland.SECRET not in script
    dumped = str(job)
    assert "postgresql://" not in dumped
    assert "DATABASE_URL" not in dumped


def test_the_laptop_cannot_start_the_facts_run():
    result = subprocess.run([sys.executable, str(SCRIPT), "run"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "Batch job" in result.stderr


def test_method_summary_matches_the_app_migration():
    if not MIGRATION.exists():
        pytest.skip("app migration is not beside this clone")
    text = MIGRATION.read_text()
    assert cropland.METHOD_ID in text
    assert cropland.METHOD_SUMMARY in text


def test_pilot_query_uses_the_view_and_the_upsert_names_the_method():
    assert "public.pilot_places" in cropland.PILOT_SQL
    assert "ST_MakeValid" in cropland.PILOT_SQL
    assert "method_id" in cropland.UPSERT_FACT
    assert "on conflict (place_id, layer, layer_version, method_id)" in cropland.UPSERT_FACT
