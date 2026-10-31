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


# ---- Raster PMTiles pyramid -------------------------------------------------

V2_PATH = Path(__file__).resolve().parents[1] / "cookbook" / "recipes" / "cropland-2024-v2.yaml"
V2 = json.loads(json.dumps(yaml.safe_load(V2_PATH.read_text())))


def tile(fill, spots=()):
    out = np.full((256, 256), fill, dtype=np.uint8)
    for row, col, value in spots:
        out[row, col] = value
    return out


def test_the_v2_recipe_leaves_resampling_unset_until_a_sample_is_approved():
    assert "resampling" not in V2["tiles"]
    try:
        cropland.tile_contract(V2)
    except ValueError as error:
        assert "unset" in str(error)
    else:
        raise AssertionError("an unset resampling must stop the build")
    assert cropland.tile_contract(V2, "q3")["resampling"] == "q3"
    for name in ("max", "q3", "q2", "nearest"):
        assert cropland.tile_contract(V2, name)["scheme"] == "pmtiles"
    try:
        cropland.tile_contract(V2, "bilinear")
    except ValueError as error:
        assert "bilinear" in str(error)
    else:
        raise AssertionError("bilinear is not an allowed value")


def test_the_version_1_recipe_still_means_xyz_and_max():
    contract = cropland.tile_contract(RECIPE)
    assert contract["scheme"] == "xyz"
    assert contract["resampling"] == "max"


def block(top_left, others):
    """A 2 x 2 block at the top left of a child tile. Values are row-major."""
    child = tile(0)
    child[0, 0], child[0, 1], child[1, 0], child[1, 1] = top_left, *others
    return child


def test_max_keeps_one_cropland_pixel_and_ignores_nodata():
    children = {(0, 0): block(0, (0, 0, 1)), (1, 1): block(255, (255, 255, 255))}
    parent = cropland.reduce_children(children, "max")
    assert parent[0, 0] == 1
    # The block of nodata in the second child stays nodata at the parent.
    assert parent[128, 128] == 255
    # A missing child is nodata.
    assert parent[0, 128] == 255 and parent[128, 0] == 255


def test_q3_needs_more_than_a_quarter_of_the_valid_pixels():
    one_of_four = cropland.reduce_children({(0, 0): block(1, (0, 0, 0))}, "q3")
    two_of_four = cropland.reduce_children({(0, 0): block(1, (1, 0, 0))}, "q3")
    one_of_three = cropland.reduce_children({(0, 0): block(1, (0, 0, 255))}, "q3")
    assert one_of_four[0, 0] == 0
    assert two_of_four[0, 0] == 1
    # Three valid pixels: floor(3 / 4) = 0, so one cropland pixel is enough.
    assert one_of_three[0, 0] == 1


def test_q3_matches_a_sorted_third_quartile_by_nearest_rank():
    import itertools

    for values in itertools.product((0, 1), repeat=4):
        child = block(values[0], values[1:])
        got = cropland.reduce_children({(0, 0): child}, "q3")[0, 0]
        ranked = sorted(values)
        assert got == ranked[int(np.ceil(0.75 * 4)) - 1]


def test_q2_is_the_median_by_nearest_rank():
    import itertools

    for values in itertools.product((0, 1), repeat=4):
        got = cropland.reduce_children({(0, 0): block(values[0], values[1:])}, "q2")[0, 0]
        assert got == sorted(values)[int(np.ceil(0.5 * 4)) - 1]
    assert cropland.reduce_children({(0, 0): block(1, (1, 0, 255))}, "q2")[0, 0] == 1
    assert cropland.reduce_children({(0, 0): block(1, (0, 255, 255))}, "q2")[0, 0] == 0


def test_nearest_takes_one_child_pixel_and_loses_the_others():
    lost = cropland.reduce_children({(0, 0): block(0, (1, 1, 1))}, "nearest")
    kept = cropland.reduce_children({(0, 0): block(1, (0, 0, 0))}, "nearest")
    assert lost[0, 0] == 0
    assert kept[0, 0] == 1


def test_an_all_nodata_parent_is_left_out():
    for name in ("max", "q3", "q2", "nearest"):
        assert cropland.reduce_children({(0, 0): tile(255)}, name) is None


def pmtiles_recipe(max_zoom):
    recipe = json.loads(json.dumps(V2))
    recipe["tiles"]["max_zoom"] = max_zoom
    return recipe


def test_a_pmtiles_archive_has_every_zoom_and_readable_png_tiles(tmp_path):
    from pmtiles.reader import Reader, MmapSource
    from pmtiles.tile import TileType, zxy_to_tileid  # noqa: F401

    source = tmp_path / "sprout.tif"
    write_raster(source, field_with_a_nodata_edge(), pixel=0.2, origin=(16.0, -22.0))
    sizes = {}
    for name in ("max", "q3", "q2", "nearest"):
        dest = tmp_path / f"{name}.pmtiles"
        stats = cropland.build_pmtiles(source, dest, pmtiles_recipe(4), name)
        sizes[name] = stats["bytes"]
        assert dest.stat().st_size == stats["bytes"]
        assert stats["min_zoom"] == 0 and stats["max_zoom"] == 4
        assert all(stats["tiles_by_zoom"][zoom] >= 1 for zoom in range(5))
        with open(dest, "rb") as handle:
            reader = Reader(MmapSource(handle))
            header = reader.header()
            assert header["tile_type"] == TileType.PNG
            assert header["min_zoom"] == 0 and header["max_zoom"] == 4
            assert reader.metadata()["resampling"] == name
            png = reader.get(0, 0, 0)
        assert png.startswith(b"\x89PNG")
        path = tmp_path / f"{name}.png"
        path.write_bytes(png)
        gray, alpha = read_png(path)
        assert set(np.unique(gray[alpha == 255]).tolist()) <= {0, 1}
        assert int(gray[alpha == 0].sum()) == 0
        assert (alpha == 0).any()
    assert all(size > 0 for size in sizes.values())


def test_max_keeps_the_pixel_that_nearest_loses_at_a_coarse_zoom(tmp_path):
    source = tmp_path / "sprout.tif"
    write_raster(source, field_with_a_nodata_edge(), pixel=0.2, origin=(16.0, -22.0))
    kept = {}
    for name in ("max", "nearest"):
        stats = cropland.build_pmtiles(source, tmp_path / f"{name}.pmtiles", pmtiles_recipe(4), name)
        zooms = stats["visibility"]["zooms"]
        kept[name] = zooms[0]["cells"]
    assert kept["max"] >= 1
    assert kept["max"] >= kept["nearest"]


def test_visibility_of_max_is_every_zoom_12_pixel(tmp_path):
    source = tmp_path / "sprout.tif"
    write_raster(source, field_with_a_nodata_edge(), pixel=0.2, origin=(16.0, -22.0))
    stats = cropland.build_pmtiles(source, tmp_path / "max.pmtiles", pmtiles_recipe(6), "max")
    for zoom in (6, 5, 4):
        assert stats["visibility"]["zooms"][zoom]["pixels_seen"] == 1.0


def test_the_global_run_is_not_wired_for_pmtiles_yet(tmp_path):
    recipe_path = tmp_path / "recipe.json"
    recipe_path.write_text(json.dumps(V2))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "submit", "--recipe", str(recipe_path)], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "task 05" in result.stderr


def test_a_byte_range_is_inclusive_and_open_ended_ranges_work():
    assert cropland.parse_byte_range("bytes=0-15", 100) == (0, 15, True)
    assert cropland.parse_byte_range("bytes=90-", 100) == (90, 99, True)
    assert cropland.parse_byte_range("bytes=-10", 100) == (90, 99, True)
    assert cropland.parse_byte_range(None, 100) == (0, 99, False)


def test_fraction_needs_a_threshold_between_zero_and_one():
    for bad in (None, 0, 1, 1.5):
        try:
            cropland.tile_contract(V2, "fraction", bad)
        except ValueError as error:
            assert "threshold" in str(error)
        else:
            raise AssertionError(f"threshold {bad} should be refused")
    assert cropland.tile_contract(V2, "fraction", 0.35)["threshold"] == 0.35
    try:
        cropland.tile_contract(V2, "max", 0.35)
    except ValueError as error:
        assert "threshold" in str(error)
    else:
        raise AssertionError("a threshold with max should be refused")


def share_tile(top_left_value):
    """A tile whose 2 x 2 blocks each hold the given share in the top left pixel's row."""
    return np.full((256, 256), top_left_value, dtype=np.float32), np.ones((256, 256))


def test_a_parent_share_is_the_weighted_mean_of_its_children():
    # A parent cell covers a 2 x 2 block of one child, so each quadrant follows its child.
    full, empty = share_tile(1.0), share_tile(0.0)
    gap = (np.full((256, 256), -1.0, dtype=np.float32), np.zeros((256, 256)))
    share, weight = cropland.reduce_fraction({(0, 0): full, (1, 0): empty, (0, 1): gap})
    assert np.allclose(share[:128, :128], 1.0) and np.allclose(share[:128, 128:], 0.0)
    assert np.allclose(share[128:, :128], -1.0) and np.allclose(weight[128:, :128], 0)
    assert np.allclose(weight[:128, :128], 4)
    # A cell with mixed children: one child tile where half the pixels are cropland.
    half = np.zeros((256, 256), dtype=np.float32)
    half[:, ::2] = 1.0
    share, _ = cropland.reduce_fraction({(0, 0): (half, np.ones((256, 256)))})
    assert np.allclose(share[:128, :128], 0.5)
    # The share survives two levels. Thresholding at 0.4 each time would not keep 0.25.
    quarter = np.zeros((256, 256), dtype=np.float32)
    quarter[::2, ::2] = 1.0
    level1 = cropland.reduce_fraction({(0, 0): (quarter, np.ones((256, 256)))})
    assert np.allclose(level1[0][:128, :128], 0.25)
    level2 = cropland.reduce_fraction({(0, 0): level1})
    assert np.allclose(level2[0][:64, :64], 0.25)


def test_a_fraction_pyramid_equals_the_share_taken_from_the_source(tmp_path):
    source = tmp_path / "sprout.tif"
    write_raster(source, field_with_a_nodata_edge(), pixel=0.2, origin=(16.0, -22.0))
    recipe = pmtiles_recipe(5)
    contract = cropland.tile_contract(recipe, "fraction", 0.25)
    fractions = cropland.build_fraction_pyramid(source, cropland.raster_bounds(source), contract)
    for zoom in (4, 2):
        direct = cropland.q3_from_source(source, zoom, sorted(fractions[zoom]), 255, 0.25)
        classes = cropland.threshold_levels(fractions, 0.25)[zoom]
        assert set(direct) == set(classes)
        for key in direct:
            both = (direct[key] != 255) & (classes[key] != 255)
            assert (direct[key][both] != classes[key][both]).mean() < 0.02


def test_fidelity_is_perfect_when_the_levels_are_the_thresholded_shares(tmp_path):
    source = tmp_path / "sprout.tif"
    write_raster(source, field_with_a_nodata_edge(), pixel=0.2, origin=(16.0, -22.0))
    contract = cropland.tile_contract(pmtiles_recipe(4), "fraction", 0.5)
    fractions = cropland.build_fraction_pyramid(source, cropland.raster_bounds(source), contract)
    levels = cropland.threshold_levels(fractions, 0.5)
    rows = cropland.fidelity(levels, fractions, (4, 3))
    assert rows[4]["true_km2"] > 0
    assert 0 <= rows[4]["recall"] <= 1 and 0 <= rows[4]["precision"] <= 1
