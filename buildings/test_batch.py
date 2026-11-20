"""Offline tests for the Buildings 2023 shard plan and the per-tile Google rule. Run: python -m pytest buildings"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
from osgeo import gdal

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tiles"))

_spec = importlib.util.spec_from_file_location("buildings_batch", HERE / "batch.py")
batch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(batch)
btiles = batch.btiles


def test_block_bounds_from_names_and_the_mercator_clip():
    assert batch.block_bounds("W010S10") == (-10, -10, 0, 0)
    assert batch.block_bounds("E170N00") == (170, 0, 180, 10)
    assert batch.block_bounds("E000N80") == (0, 80, 10, batch.MERCATOR_LAT)


def test_shard_plan_reads_blocks_within_the_margin():
    """Zoom 5 column 24 starts at 90° E, where block E080N00 ends: the shard has no cell of it, but its edge tiles
    read 74 cells into it, so it is a source. Column 23 ends there too and reads E090N00."""
    cells = {name: batch.block_bounds(name) for name in ("E080N00", "E090N00")}
    plan = {shard["id"]: shard["sources"] for shard in batch.shard_plan(cells)}
    assert plan["5-24-15"] == ["E080N00", "E090N00"]
    assert plan["5-23-15"] == ["E080N00", "E090N00"]
    assert "5-22-15" not in plan  # 67.5 to 78.75° E, short of the block
    assert "5-25-15" not in plan


def test_shard_plan_without_a_neighbour_in_reach():
    cells = {"E010N00": batch.block_bounds("E010N00"), "E030N00": batch.block_bounds("E030N00")}
    plan = {shard["id"]: shard["sources"] for shard in batch.shard_plan(cells)}
    assert plan["5-16-15"] == ["E010N00"]  # 0 to 11.25° E
    assert plan["5-18-15"] == ["E030N00"]  # 22.5 to 33.75° E


def test_dilate_matches_a_square_maximum_filter():
    from scipy.ndimage import maximum_filter

    rng = np.random.default_rng(5)
    mask = rng.random((60, 45)) > 0.97
    expected = maximum_filter(mask.astype(np.uint8), size=2 * 4 + 1, mode="constant") > 0
    assert (btiles.dilate(mask, 4) == expected).all()


def test_the_rule_per_tile_equals_the_rule_on_the_whole_cut(tmp_path):
    """A tile that applies the Google rule to what it reads gives the window flow's tile, cut cleaned once."""
    cell, west, north, size = 0.00025, 10.0, 0.2, 1200
    rng = np.random.default_rng(11)
    share = np.where(rng.random((size, size)) > 0.6, rng.integers(1, 6, (size, size)), 0).astype(np.uint8)
    share[rng.integers(0, size, 40), rng.integers(0, size, 40)] = 60  # seeds
    source = np.where(np.arange(size)[None, :] < size // 2, 1, 2).repeat(size, axis=0).astype(np.uint8)

    def write(path, values):
        out = gdal.GetDriverByName("GTiff").Create(str(path), size, size, 2, gdal.GDT_Byte)
        out.SetGeoTransform((west, cell, 0, north, 0, -cell))
        out.SetProjection("EPSG:4326")
        out.GetRasterBand(1).WriteArray(values)
        out.GetRasterBand(2).WriteArray(source)
        out = None

    write(tmp_path / "raw.tif", share)
    write(tmp_path / "clean.tif", btiles.speckle_rule(share, source, 0.012, 0.15, 72))
    per_tile = btiles.layer(tmp_path / "raw.tif", 0.0, 0.012, 0.15, 72)
    whole = btiles.layer(tmp_path / "clean.tif")
    tested = 0
    for x, y in btiles.cropland.tiles_covering((west + 0.06, north - 0.24, west + 0.24, north - 0.06), 12):
        a, b = per_tile(12, x, y), whole(12, x, y)
        assert (a is None) == (b is None)
        if a is not None:
            assert np.array_equal(a.share, b.share) and np.array_equal(a.valid, b.valid)
            tested += 1
    assert tested >= 4


def test_settings_refuse_what_the_code_does_not_build():
    import yaml

    recipe = yaml.safe_load(btiles.RECIPE.read_text())
    config = batch.settings(recipe)
    assert (config["top_zoom"], config["share_full"], config["google_floor"], config["google_seed"]) == (12, 0.25, 0.012, 0.15)
    recipe["tiles"]["parent"] = "max"
    try:
        batch.settings(recipe)
    except SystemExit as error:
        assert "parent" in str(error)
    else:
        raise AssertionError("a max parent was accepted")


def test_empty_tiles_are_not_stored_but_feed_their_parent():
    config = {"top_zoom": 12, "share_steps": 7, "share_full": 0.25}
    encode = batch.encoder(config)
    empty = btiles.Tile(np.zeros((512, 512), np.float32), np.ones((512, 512), np.float32), 12)
    built = btiles.Tile(np.full((512, 512), 0.1, np.float32), np.ones((512, 512), np.float32), 12)
    assert encode(empty) == b""
    assert encode(built) == btiles.encode(built, 12, "graded", 0.25, 7)


def test_a_tile_renders_alike_from_a_block_mosaic_and_the_global_vrt(tmp_path):
    """Column 3280 at zoom 12 starts on a cell edge (108.28125° E). A shard reads a mosaic cornered at its blocks,
    the merge check the VRT cornered at 180° W, 90° N; the render must round both the same way."""
    cell, west, north, size = 0.00025, 108.0, 22.0, 2000
    rng = np.random.default_rng(3)
    share = np.where(rng.random((size, size)) > 0.7, rng.integers(1, 9, (size, size)), 0).astype(np.uint8)
    share[rng.integers(0, size, 60), rng.integers(0, size, 60)] = 80
    block = tmp_path / "block.tif"
    out = gdal.GetDriverByName("GTiff").Create(str(block), size, size, 2, gdal.GDT_Byte)
    out.SetGeoTransform((west, cell, 0, north, 0, -cell))
    out.SetProjection("EPSG:4326")
    out.GetRasterBand(1).WriteArray(share)
    out.GetRasterBand(2).WriteArray(np.ones((size, size), np.uint8))  # Google
    out = None
    whole = tmp_path / "global.vrt"
    gdal.BuildVRT(str(whole), [str(block)], outputBounds=(-180, north - size * cell, west + size * cell, 90)).FlushCache()
    mosaic, world = btiles.layer(block, 0.0, 0.012, 0.15, 72), btiles.layer(whole, 0.0, 0.012, 0.15, 72)
    for y in range(1792, 1797):
        a, b = mosaic(12, 3280, y), world(12, 3280, y)
        assert a is not None and b is not None
        assert np.array_equal(a.share, b.share) and np.array_equal(a.valid, b.valid)
