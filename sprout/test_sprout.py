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
    assert collection["id"] == "cropland-2024-v1"
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
    assert policy["bootDisk"]["sizeGb"] == 30
    assert job["allocationPolicy"]["location"]["allowedLocations"] == ["regions/europe-west1"]
    spec = job["taskGroups"][0]["taskSpec"]
    assert spec["maxRunDuration"] == "10800s"
    assert spec["computeResource"]["memoryMib"] == "6144"
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


GLOBAL_PATH = Path(__file__).resolve().parents[1] / "cookbook" / "recipes" / "cropland-2024-v2.yaml"
GLOBAL = json.loads(json.dumps(yaml.safe_load(GLOBAL_PATH.read_text())))
TILE_URL = "https://glad.umd.edu/datasets/annual-croplands/2024/20S_020E.tif"


def write_tile(path, origin, values, pixel=1):
    """A source tile. GLAD sets no nodata flag, so this file does not either."""
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
    dataset.FlushCache()
    dataset = None


def mosaic_recipe():
    recipe = json.loads(json.dumps(GLOBAL))
    recipe["grid"]["pixel_size_deg"] = 1
    return recipe


def solid(width, height, value):
    return [[value] * width for _y in range(height)]


def test_a_mosaic_remaps_tiles_and_leaves_the_gap_as_nodata(tmp_path):
    # 0.00025° matches the recipe. A 1° pixel makes GDAL's max warp drop an isolated cell.
    recipe = json.loads(json.dumps(GLOBAL))
    pixel = float(recipe["grid"]["pixel_size_deg"])
    left = tmp_path / "left.tif"
    right = tmp_path / "right.tif"
    rows = solid(600, 400, 0)
    rows[100][100] = 100
    write_tile(left, (0, 0), rows, pixel=pixel)
    write_tile(right, (800 * pixel, 0), solid(600, 400, 0), pixel=pixel)
    dest = tmp_path / "cropland-2024.tif"
    source, sprout = cropland.write_mosaic_sprout([left, right], dest, recipe, tmp_path)
    assert cropland.check_sprout(source, sprout, recipe) == []
    assert sprout["layout"] == "COG"
    assert sprout["crs"] == "EPSG:4326"
    assert sprout["nodata"] == 255
    assert sprout["version"] == "v2"
    assert sprout["class_values"] == "cropland:1,not_cropland:0,nodata:255"
    assert abs(sprout["pixel_size_deg"] - pixel) < 1e-12
    assert sprout["counts"][1] == 1
    assert sprout["counts"][0] == 600 * 400 * 2 - 1
    assert sprout["counts"][255] == 1400 * 400 - 600 * 400 * 2
    assert 100 not in sprout["counts"]

    dataset = gdal.Open(str(dest))
    band = dataset.GetRasterBand(1)
    assert band.ReadRaster(100, 100, 1, 1)[0] == 1
    assert band.ReadRaster(10, 10, 1, 1)[0] == 0
    assert band.ReadRaster(700, 10, 1, 1)[0] == 255
    assert band.ReadRaster(800, 10, 1, 1)[0] == 0
    overview = band.GetOverview(0)
    assert overview.ReadRaster(50, 50, 1, 1)[0] == 1
    assert overview.ReadRaster(350, 10, 1, 1)[0] == 255
    dataset = None

    collection, item = cropland.stac_documents(recipe, sprout)
    assert collection["id"] == "cropland-2024-v2"
    assert item["assets"]["data"]["href"] == recipe["sprout"]["path"]
    assert "cropland-2024-zaf" not in item["assets"]["data"]["href"]


def test_an_unmapped_tile_value_stops_the_mosaic(tmp_path):
    tile = tmp_path / "bad.tif"
    write_tile(tile, (0, 0), [[0, 7], [100, 0]])
    try:
        cropland.write_mosaic_sprout([tile], tmp_path / "out.tif", mosaic_recipe(), tmp_path)
    except ValueError as error:
        assert "7" in str(error)
    else:
        raise AssertionError("expected an unmapped tile value to fail")
    assert not (tmp_path / "out.tif").exists()


def test_overlapping_tiles_are_refused(tmp_path):
    first = tmp_path / "a.tif"
    second = tmp_path / "b.tif"
    write_tile(first, (0, 0), solid(8, 8, 0))
    write_tile(second, (4, 0), solid(8, 8, 0))
    try:
        cropland.write_mosaic_sprout([first, second], tmp_path / "out.tif", mosaic_recipe(), tmp_path)
    except ValueError as error:
        assert "overlaps" in str(error)
    else:
        raise AssertionError("expected overlapping tiles to fail")


def test_a_tile_off_the_recipe_grid_is_refused(tmp_path):
    tile = tmp_path / "coarse.tif"
    write_tile(tile, (0, 0), [[0, 100], [0, 0]], pixel=2)
    try:
        cropland.write_mosaic_sprout([tile], tmp_path / "out.tif", mosaic_recipe(), tmp_path)
    except ValueError as error:
        assert "pixel" in str(error)
    else:
        raise AssertionError("expected a changed pixel size to fail")


def test_the_host_path_wins_and_a_flat_name_is_the_fallback():
    nested = "glad.umd.edu/datasets/annual-croplands/2024/20S_020E.tif"
    assert cropland.tile_object_name("20S_020E.tif", TILE_URL, {nested}) == nested
    assert cropland.tile_object_name("20S_020E.tif", TILE_URL, {"20S_020E.tif"}) == "20S_020E.tif"
    assert cropland.tile_object_name("20S_020E.tif", TILE_URL, {"20S_020E.tif", nested}) == nested
    try:
        cropland.tile_object_name("20S_020E.tif", TILE_URL, {".keep"})
    except SystemExit as error:
        assert "missing" in str(error)
    else:
        raise AssertionError("expected a missing tile to stop")


def test_the_version_1_job_keeps_its_shape():
    assert cropland.scratch_prefix(RECIPE) == cropland.SCRATCH
    shape = cropland.job_shape(RECIPE)
    assert shape["machine"] == "e2-standard-2"
    assert shape["disk_gb"] == 30
    assert not cropland.is_per_tile(RECIPE)
    bad = json.loads(json.dumps(GLOBAL))
    bad["seed"].pop("clip", None)
    bad["sprout"]["path"] = RECIPE["sprout"]["path"]
    try:
        cropland.require_destination(bad)
    except SystemExit as error:
        assert "cropland-2024-zaf.tif" in str(error)
    else:
        raise AssertionError("expected the version 1 cog to be refused")


def small_recipe(tiles=("a.tif", "b.tif")):
    recipe = json.loads(json.dumps(GLOBAL))
    recipe["seed"]["tiles"] = list(tiles)
    return recipe


def pixel_of(recipe):
    return float(recipe["grid"]["pixel_size_deg"])


def test_the_version_2_recipe_is_per_tile_and_its_tiles_sit_inside_the_vrt_folder():
    assert cropland.is_per_tile(GLOBAL)
    cropland.require_per_tile(GLOBAL)
    assert GLOBAL["sprout"]["path"].endswith("/cropland-2024.vrt")
    assert cropland.sprout_folder(GLOBAL) == "gs://nature-watch-bucket/sprouts/cropland/2024/v2/"
    assert cropland.relative_tile_path(GLOBAL, "20S_020E.tif") == "tiles/20S_020E.tif"
    assert cropland.tile_output(GLOBAL, "20S_020E.tif") == (
        "gs://nature-watch-bucket/sprouts/cropland/2024/v2/tiles/20S_020E.tif"
    )
    outside = json.loads(json.dumps(GLOBAL))
    outside["sprout"]["tiles_path"] = "gs://nature-watch-bucket/sprouts/cropland/2024/v1/tiles/"
    try:
        cropland.relative_tile_path(outside, "x.tif")
    except ValueError as error:
        assert "not inside" in str(error)
    else:
        raise AssertionError("expected a tiles folder outside the sprout folder to fail")
    try:
        cropland.require_per_tile(RECIPE)
    except SystemExit as error:
        assert "per-tile" in str(error)
    else:
        raise AssertionError("expected the version 1 recipe to be refused")


def test_a_tile_becomes_a_cog_with_full_resolution_classes_and_no_overviews(tmp_path):
    recipe = small_recipe()
    pixel = pixel_of(recipe)
    rows = solid(2000, 1500, 0)
    rows[700][900] = 100
    source = tmp_path / "a.tif"
    write_tile(source, (16.0, -22.0), rows, pixel=pixel)
    dest = tmp_path / "sprout-a.tif"
    record = cropland.build_tile(source, dest, recipe, tmp_path)
    written = cropland.raster_facts(dest)
    assert written["layout"] == "COG"
    assert written["overviews"] == 0
    assert written["crs"] == "EPSG:4326"
    assert written["dtype"] == "uint8"
    assert written["nodata"] == 255
    assert written["version"] == "v2"
    assert written["class_values"] == "cropland:1,not_cropland:0,nodata:255"
    assert written["counts"] == {1: 1, 0: 2000 * 1500 - 1}
    assert record["tile"] == "a.tif"
    assert record["bytes"] == dest.stat().st_size
    assert record["counts"] == {"1": 1, "0": 2000 * 1500 - 1}
    assert record["transform"] == list(written["transform"])
    dataset = gdal.Open(str(dest))
    band = dataset.GetRasterBand(1)
    assert band.ReadRaster(900, 700, 1, 1)[0] == 1
    assert band.ReadRaster(901, 700, 1, 1)[0] == 0
    dataset = None
    json.dumps(record)


def test_an_unmapped_value_in_one_tile_stops_that_tile(tmp_path):
    recipe = small_recipe()
    source = tmp_path / "a.tif"
    write_tile(source, (16.0, -22.0), [[0, 7], [100, 0]], pixel=pixel_of(recipe))
    dest = tmp_path / "sprout-a.tif"
    try:
        cropland.build_tile(source, dest, recipe, tmp_path)
    except ValueError as error:
        assert "7" in str(error)
    else:
        raise AssertionError("expected an unmapped value to stop the tile")
    assert not dest.exists()


def test_the_collection_is_a_vrt_over_tile_cogs_with_gaps_as_nodata(tmp_path):
    recipe = small_recipe()
    pixel = pixel_of(recipe)
    folder = tmp_path / "sprout"
    (folder / "tiles").mkdir(parents=True)
    left_rows = solid(600, 400, 0)
    left_rows[100][100] = 100
    right_rows = solid(600, 400, 0)
    right_rows[10][20] = 100
    records = {}
    for name, rows, x in (("a.tif", left_rows, 16.0), ("b.tif", right_rows, 16.0 + 800 * pixel)):
        source = tmp_path / name
        write_tile(source, (x, -22.0), rows, pixel=pixel)
        records[name] = cropland.build_tile(source, folder / "tiles" / name, recipe, tmp_path)
    paths = cropland.write_collection(recipe, records, folder)
    assert [path.name for path in paths] == ["cropland-2024.vrt", "collection.json", "cropland-2024.json"]
    dataset = gdal.Open(str(folder / "cropland-2024.vrt"))
    band = dataset.GetRasterBand(1)
    assert (dataset.RasterXSize, dataset.RasterYSize) == (1400, 400)
    assert cropland.spatial_reference(dataset.GetProjection()) == "EPSG:4326"
    assert dataset.GetGeoTransform() == (16.0, pixel, 0.0, -22.0, 0.0, -pixel)
    assert band.GetNoDataValue() == 255
    assert band.GetOverviewCount() == 0
    assert dataset.GetMetadataItem("version") == "v2"
    assert dataset.GetMetadataItem("class_values") == "cropland:1,not_cropland:0,nodata:255"
    assert band.ReadRaster(100, 100, 1, 1)[0] == 1
    assert band.ReadRaster(50, 50, 1, 1)[0] == 0
    assert band.ReadRaster(700, 50, 1, 1)[0] == 255
    assert band.ReadRaster(820, 10, 1, 1)[0] == 1
    assert band.ReadRaster(1300, 300, 1, 1)[0] == 0
    # Columns 590-809: tile a ends at 599, the gap is 600-799, tile b starts at 800.
    seam = band.ReadRaster(590, 100, 220, 1)
    assert seam[9] == 0 and seam[10] == 255 and seam[209] == 255 and seam[210] == 0
    dataset = None

    collection = json.loads(paths[1].read_text())
    item = json.loads(paths[2].read_text())
    assert collection["id"] == "cropland-2024-v2"
    assert item["assets"]["data"]["href"] == recipe["sprout"]["path"]
    assert item["assets"]["data"]["type"] == cropland.VRT_TYPE
    assert item["assets"]["tile-a"]["href"] == cropland.tile_output(recipe, "a.tif")
    assert item["assets"]["tile-b"]["type"] == cropland.COG_TYPE
    assert item["bbox"] == [16.0, -22.0 - 400 * pixel, 16.0 + 1400 * pixel, -22.0]


def test_a_collection_with_a_missing_tile_record_is_not_written(tmp_path):
    recipe = small_recipe()
    try:
        cropland.write_collection(recipe, {}, tmp_path / "out")
    except ValueError as error:
        assert "no record" in str(error)
    else:
        raise AssertionError("expected a missing record to stop the collection")
    assert not (tmp_path / "out" / "cropland-2024.vrt").exists()


def local_bucket(monkeypatch, tmp_path):
    monkeypatch.setattr(cropland, "gs_to_vsi", lambda path: str(tmp_path / path.removeprefix("gs://")))


def test_a_tile_is_complete_only_when_its_record_matches_the_cog(monkeypatch, tmp_path):
    local_bucket(monkeypatch, tmp_path)
    recipe = small_recipe()
    folder = tmp_path / "nature-watch-bucket" / "sprouts" / "cropland" / "2024" / "v2" / "tiles"
    folder.mkdir(parents=True)
    assert not cropland.tile_is_complete(recipe, "a.tif")
    (folder / "a.tif").write_bytes(b"x" * 10)
    assert not cropland.tile_is_complete(recipe, "a.tif")
    (folder / "a.json").write_text(json.dumps({"tile": "a.tif", "bytes": 10}))
    assert cropland.tile_is_complete(recipe, "a.tif")
    (folder / "a.json").write_text(json.dumps({"tile": "a.tif", "bytes": 11}))
    assert not cropland.tile_is_complete(recipe, "a.tif")
    (folder / "a.json").write_text("{")
    assert not cropland.tile_is_complete(recipe, "a.tif")


def test_a_finished_tile_is_skipped_without_reading_the_seed(monkeypatch, tmp_path, capsys):
    recipe = small_recipe()
    monkeypatch.setenv(cropland.BATCH_ENV, "1")
    monkeypatch.setattr(cropland, "tile_is_complete", lambda _recipe, name: name == "b.tif")

    def no_read(*_args, **_kwargs):
        raise AssertionError("a finished tile must not be read")

    monkeypatch.setattr(cropland, "resolve_tile", no_read)
    cropland.command_run_tile(recipe, 1, tmp_path)
    assert "skip b.tif" in capsys.readouterr().out
    try:
        cropland.command_run_tile(recipe, 5, tmp_path)
    except SystemExit as error:
        assert "outside" in str(error)
    else:
        raise AssertionError("expected an index outside the recipe to fail")


def test_the_tile_job_is_one_retried_spot_task_per_tile():
    shape = cropland.job_shape(GLOBAL)
    assert shape["machine"] == "e2-standard-2"
    assert shape["disk_gb"] == 30
    prefix = cropland.scratch_prefix(GLOBAL)
    assert prefix == "gs://nature-watch-bucket/scratch/sprout/cropland-2024-v2/"
    job = cropland.tile_batch_job(GLOBAL, prefix, shape)
    group = job["taskGroups"][0]
    assert group["taskCount"] == str(len(GLOBAL["seed"]["tiles"])) == "223"
    assert group["parallelism"] == str(shape["parallelism"])
    spec = group["taskSpec"]
    assert spec["maxRetryCount"] == 3
    assert spec["lifecyclePolicies"] == [
        {"action": "RETRY_TASK", "actionCondition": {"exitCodes": [50001]}}
    ]
    assert spec["maxRunDuration"] == f"{shape['max_run_seconds']}s"
    policy = job["allocationPolicy"]["instances"][0]["policy"]
    assert policy["provisioningModel"] == "SPOT"
    assert policy["bootDisk"]["sizeGb"] == 30
    assert job["allocationPolicy"]["location"]["allowedLocations"] == ["regions/europe-west1"]
    script = spec["runnables"][0]["container"]["commands"][1]
    assert "cropland.py run-tile --recipe /tmp/in/recipe.json" in script
    assert "--upload" not in script
    assert "cropland-2024-zaf" not in script
    assert spec["environment"]["variables"]["NATUREWATCH_SPROUT_BATCH"] == "1"
