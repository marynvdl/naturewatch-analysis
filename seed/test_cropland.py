"""Checks for the cropland seed. They need GDAL and no network.

Run: python -m pytest seed
"""
import array
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

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


GLOBAL = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "cookbook" / "recipes" / "cropland-2024-v2.yaml").read_text()
)
SCRIPT = Path(__file__).resolve().parent / "cropland.py"


def test_the_url_list_names_every_global_tile_in_order():
    lines = cropland.url_list_tsv(GLOBAL).splitlines()
    assert lines[0] == "TsvHttpData-1.0"
    urls = lines[1:]
    assert urls == sorted(urls)
    assert len(urls) == len(GLOBAL["seed"]["tiles"]) == 223
    assert all("\t" not in line for line in lines)
    assert "https://glad.umd.edu/datasets/annual-croplands/2024/20S_020E.tif" in urls


def test_a_checked_tile_is_stored_as_the_flat_file_name():
    url = "https://glad.umd.edu/datasets/annual-croplands/2024/20S_020E.tif"
    nested = cropland.transferred_object_name(url)
    assert nested == "glad.umd.edu/datasets/annual-croplands/2024/20S_020E.tif"
    assert cropland.transferred_object_name("https://glad.umd.edu:443/a.tif") == "glad.umd.edu:443/a.tif"
    plan = cropland.tile_plan("20S_020E.tif", url, {nested, ".keep"})
    stored = cropland.gs_join(GLOBAL["seed"]["path"], plan.move_to)
    assert stored == "gs://nature-watch-bucket/seeds/cropland/glad/2024/v2/20S_020E.tif"
    assert "glad.umd.edu" not in stored
    assert "/2024/v1/" not in stored

    already_flat = cropland.tile_plan("20S_020E.tif", url, {"20S_020E.tif"})
    assert already_flat.read_name == "20S_020E.tif"
    assert already_flat.move_to is None
    assert already_flat.delete_name is None

    leftover = cropland.tile_plan("20S_020E.tif", url, {"20S_020E.tif", nested})
    assert leftover.read_name == "20S_020E.tif"
    assert leftover.move_to is None
    assert leftover.delete_name == nested


def test_the_flat_move_does_not_overwrite_and_encodes_the_host_path():
    source = "seeds/cropland/glad/2024/v2/glad.umd.edu/datasets/annual-croplands/2024/20S_020E.tif"
    dest = "seeds/cropland/glad/2024/v2/20S_020E.tif"
    url = cropland.rewrite_url("nature-watch-bucket", source, dest, None)
    assert "ifGenerationMatch=0" in url
    assert quote(source, safe="") in url
    assert quote(dest, safe="") in url
    assert "/2024/v1/" not in url
    names = cropland.names_from_list_page(
        {"items": [{"name": source}, {"name": dest}, {"name": "seeds/cropland/glad/2024/v2/.keep"}]},
        "seeds/cropland/glad/2024/v2/",
    )
    assert names == {
        "glad.umd.edu/datasets/annual-croplands/2024/20S_020E.tif",
        "20S_020E.tif",
        ".keep",
    }
    recipe = {
        "seed": {"tiles": ["20S_020E.tif"]},
        "source": {"download": GLOBAL["source"]["download"]},
    }
    assert cropland.missing_tiles(recipe, names) == []
    assert cropland.missing_tiles(recipe, {".keep"}) == ["20S_020E.tif"]
    assert cropland.blocking_names(recipe, {"20S_020E.tif"}) == ["20S_020E.tif"]
    nested = "glad.umd.edu/datasets/annual-croplands/2024/20S_020E.tif"
    assert cropland.blocking_names(recipe, {nested}) == [nested]


def test_an_existing_tile_is_recognised_and_a_marker_is_not():
    prefix = "gs://nature-watch-bucket/seeds/cropland/glad/2024/v2/"
    name = "glad.umd.edu/datasets/annual-croplands/2024/20S_020E.tif"
    listing = f"{prefix}.keep\n{prefix}{name}\n"
    assert cropland.already_stored(listing, prefix, {name}) == [name]
    assert cropland.relative_names(f"{prefix}.keep\n", prefix) == {".keep"}


def test_the_transfer_job_does_not_overwrite_and_does_not_target_version_1():
    args = cropland.transfer_create_args(
        "gs://nature-watch-bucket/scratch/seed/cropland-2024-v2/urls.tsv",
        GLOBAL["seed"]["path"],
    )
    assert args[:4] == [
        "transfer",
        "jobs",
        "create",
        "gs://nature-watch-bucket/scratch/seed/cropland-2024-v2/urls.tsv",
    ]
    assert args[4] == "gs://nature-watch-bucket/seeds/cropland/glad/2024/v2/"
    assert "--overwrite-when=never" in args
    assert f"--name={cropland.TRANSFER_JOB}" in args
    grant = cropland.transfer_agent_grant_args()
    assert f"--member=serviceAccount:{cropland.TRANSFER_AGENT}" in grant
    assert "--role=roles/storage.legacyBucketWriter" in grant


def test_the_transfer_refuses_the_version_1_recipe():
    try:
        cropland.require_transfer_recipe(RECIPE)
    except SystemExit as error:
        assert "version 1" in str(error)
    else:
        raise AssertionError("expected the version 1 recipe to be refused")
    try:
        cropland.require_laptop_recipe(GLOBAL)
    except SystemExit as error:
        assert "no clip" in str(error)
    else:
        raise AssertionError("expected the global recipe to skip the laptop download")


def test_the_check_job_is_a_spot_machine_next_to_the_bucket():
    job = cropland.batch_job(cropland.SCRATCH)
    policy = job["allocationPolicy"]["instances"][0]["policy"]
    assert policy["machineType"] == "e2-standard-2"
    assert policy["provisioningModel"] == "SPOT"
    assert policy["bootDisk"] == {"sizeGb": 30, "type": "pd-balanced"}
    assert job["allocationPolicy"]["location"]["allowedLocations"] == ["regions/europe-west1"]
    spec = job["taskGroups"][0]["taskSpec"]
    assert spec["maxRunDuration"] == "43200s"
    assert "volumes" not in spec
    runnable = spec["runnables"][0]["container"]
    assert runnable["imageUri"] == "ghcr.io/osgeo/gdal:ubuntu-small-3.11.4"
    script = runnable["commands"][1]
    assert "nature-watch-bucket/scratch/seed/cropland-2024-v2/" in script
    assert "/tmp/in/cropland.py verify" in script
    assert "cropland.py run" not in script
    assert spec["environment"]["variables"]["NATUREWATCH_SEED_BATCH"] == "1"


def test_the_laptop_cannot_fetch_the_global_tiles_or_run_the_check():
    recipe = SCRIPT.parents[1] / "cookbook/recipes/cropland-2024-v2.yaml"
    fetch = subprocess.run(
        [sys.executable, str(SCRIPT), "run", "--recipe", str(recipe)],
        capture_output=True,
        text=True,
    )
    assert fetch.returncode != 0
    assert "no clip" in fetch.stderr
    verify = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", "--recipe", str(recipe)],
        capture_output=True,
        text=True,
    )
    assert verify.returncode != 0
    assert "europe-west1" in verify.stderr
