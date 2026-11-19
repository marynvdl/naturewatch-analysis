"""Offline tests for the buildings tile sample. Run: python -m pytest buildings"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
from osgeo import gdal

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tiles"))

import transcode  # noqa: E402

# Loaded under its own name: roads/test_roads.py imports roads/tiles.py as `tiles` in the same session.
_spec = importlib.util.spec_from_file_location("buildings_tiles", HERE / "tiles.py")
tiles = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tiles)

SIZE = tiles.SIZE


def tile(share, valid=None, zoom=11):
    share = np.asarray(share, dtype=np.float32)
    valid = np.ones_like(share) if valid is None else np.asarray(valid, dtype=np.float32)
    return tiles.Tile(share, valid, zoom)


def test_graded_encoding_is_the_cropland_v3_palette_when_full_is_1():
    rng = np.random.default_rng(1)
    gray = rng.integers(0, 256, (SIZE, SIZE))
    gray[:40] = rng.integers(0, 4, (40, SIZE))  # sparse shares, where shade 1 is kept
    alpha = np.where(rng.random((SIZE, SIZE)) < 0.1, 0, 255)
    ours = tiles.encode(tile(gray / 255, alpha > 0, zoom=8), top_zoom=11, top_rule="solid0.5", full=1.0)
    theirs = transcode.share_palette_from_gray(gray, alpha, steps=7, curve="sqrt")
    assert ours == theirs


def test_solid_top_zoom_is_the_cropland_v3_solid_tile_at_one_half():
    rng = np.random.default_rng(2)
    gray = rng.integers(0, 256, (SIZE, SIZE))
    alpha = np.where(rng.random((SIZE, SIZE)) < 0.1, 0, 255)
    gray[gray == 127] = 126  # share 127/255 is below 0.5; cropland's gray test is >= 128
    ours = tiles.encode(tile(gray / 255, alpha > 0), top_zoom=11, top_rule="solid0.5", full=1.0)
    theirs = transcode.share_palette_from_gray(gray, alpha, solid=True)
    assert ours == theirs


def test_shade_index_with_full_and_the_faintest_shade():
    share = np.array([0.0, 1e-6, 0.01, 0.0625, 0.25, 0.9])
    assert tiles.shade_index(share, 7, 1.0).tolist() == [0, 1, 1, 2, 4, 7]
    assert tiles.shade_index(share, 7, 0.25).tolist() == [0, 1, 1, 4, 7, 7]


def test_mean_parent_keeps_the_built_fraction_and_absent_children_count_as_zero():
    child = tile(np.full((SIZE, SIZE), 0.4, dtype=np.float32), zoom=11)
    parent = tiles.reduce({(0, 0): child}, "mean")
    assert parent.zoom == 10
    assert parent.share[: SIZE // 2, : SIZE // 2] == pytest.approx(0.4)
    assert parent.share[SIZE // 2 :, :].max() == 0
    assert parent.valid[: SIZE // 2, : SIZE // 2] == pytest.approx(1.0)
    assert parent.valid[SIZE // 2 :, SIZE // 2 :].max() == 0


def test_max_parent_takes_the_largest_child_pixel():
    share = np.zeros((SIZE, SIZE), dtype=np.float32)
    share[0, 1] = 0.8
    parent = tiles.reduce({(0, 0): tile(share)}, "max")
    assert parent.share[0, 0] == pytest.approx(0.8)
    mean = tiles.reduce({(0, 0): tile(share)}, "mean")
    assert mean.share[0, 0] == pytest.approx(0.2)


def test_overlap_weights_partial_intervals_in_either_order():
    edges = np.array([0.0, 1.0, 2.0, 3.0])
    assert tiles.overlap(edges, 0.5, 2.0).tolist() == [0.5, 1.0, 0.0]
    assert tiles.overlap(edges[::-1], 0.5, 2.0).tolist() == [0.0, 1.0, 0.5]


def write_sprout(path: Path, share: np.ndarray, west: float, north: float) -> None:
    ds = gdal.GetDriverByName("GTiff").Create(str(path), share.shape[1], share.shape[0], 2, gdal.GDT_Byte)
    ds.SetGeoTransform((west, tiles.compare.CELL, 0, north, 0, -tiles.compare.CELL))
    ds.SetProjection("EPSG:4326")
    ds.GetRasterBand(1).WriteArray(share)
    ds.GetRasterBand(1).SetNoDataValue(tiles.SHARE_NODATA)
    ds.GetRasterBand(2).WriteArray(np.where(share == tiles.SHARE_NODATA, 0, 1).astype(np.uint8))
    ds = None


def test_top_zoom_render_keeps_the_area_of_the_sprout(tmp_path):
    z, x, y = 13, 4400, 3900  # about 13.3 N, 13.4 E
    west, south, east, north = tiles.tile_bounds_deg(z, x, y)
    m = tiles.MARGIN + 2
    col0 = int(np.floor((west + 180) / tiles.compare.CELL)) - m
    row0 = int(np.floor((90 - north) / tiles.compare.CELL)) - m
    cols = int(np.ceil((east + 180) / tiles.compare.CELL)) + m - col0
    rows = int(np.ceil((90 - south) / tiles.compare.CELL)) + m - row0
    rng = np.random.default_rng(3)
    share = np.where(rng.random((rows, cols)) < 0.05, rng.integers(1, 251, (rows, cols)), 0).astype(np.uint8)
    share[:, : cols // 3] = tiles.SHARE_NODATA  # sea on the west
    path = tmp_path / "w.tif"
    grid = tiles.compare.Grid(col0, row0, cols, rows)
    write_sprout(path, share, grid.west, grid.north)
    rendered = tiles.layer(path)(z, x, y)
    assert rendered is not None
    # nodata only where every cell under the pixel is sea
    assert (rendered.valid == 0).any() and (rendered.valid == 1).any()
    tile_km2 = float((rendered.share * tiles.pixel_area_km2(z, y)).sum())
    sprout = np.where(share == tiles.SHARE_NODATA, 0, share / tiles.SHARE_SCALE)
    wx = tiles.overlap(grid.west + np.arange(cols + 1) * tiles.compare.CELL, west, east)
    wy = tiles.overlap(grid.north - np.arange(rows + 1) * tiles.compare.CELL, south, north)
    sprout_km2 = float((sprout * tiles.compare.cell_area_km2(grid) * wy[:, None] * wx[None, :]).sum())
    assert tile_km2 == pytest.approx(sprout_km2, rel=0.01)
    assert tiles.km2_in(rendered, x, y, (west, south, east, north)) == pytest.approx(tile_km2)


def test_floor_reads_small_cells_as_zero(tmp_path):
    z, x, y = 13, 4400, 3900
    west, south, east, north = tiles.tile_bounds_deg(z, x, y)
    col0 = int(np.floor((west + 180) / tiles.compare.CELL)) - 4
    row0 = int(np.floor((90 - north) / tiles.compare.CELL)) - 4
    cols = int(np.ceil((east + 180) / tiles.compare.CELL)) + 4 - col0
    rows = int(np.ceil((90 - south) / tiles.compare.CELL)) + 4 - row0
    share = np.full((rows, cols), 2, dtype=np.uint8)  # 0.008 everywhere
    path = tmp_path / "w.tif"
    grid = tiles.compare.Grid(col0, row0, cols, rows)
    write_sprout(path, share, grid.west, grid.north)
    assert tiles.layer(path)(z, x, y).share.max() > 0
    floored = tiles.layer(path, floor=0.01)(z, x, y)
    assert floored.share.max() == 0 and floored.valid.min() == 1


def test_settings_name():
    assert tiles.settings_name(tiles.BASELINE) == "z11-solid0.5-full1-mean-keep"
    assert tiles.settings_name({**tiles.BASELINE, "floor": 0.01}) == "z11-solid0.5-full1-mean-keep-floor0.01"


def test_speckle_rule_touches_google_cells_only():
    share = np.array([[2, 3, 2, 255], [1, 0, 2, 2]], dtype=np.uint8)
    source = np.array([[1, 1, 2, 0], [1, 1, 2, 2]], dtype=np.uint8)
    out = tiles.speckle_rule(share, source, floor=0.012)  # 3 of 250
    assert out.tolist() == [[0, 3, 2, 255], [0, 0, 2, 2]]


def test_google_seed_rule_keeps_cells_near_a_strong_google_cell():
    share = np.zeros((1, 20), dtype=np.uint8)
    source = np.ones((1, 20), dtype=np.uint8)
    share[0, 0] = 40  # 0.16, a seed
    share[0, 3] = 5   # within 4 cells of it
    share[0, 12] = 5  # too far
    share[0, 19] = 255  # nodata is not a seed
    share[0, 18] = 5
    source[0, 15] = 2
    share[0, 15] = 200  # a TEMPO cell is not a seed for Google
    out = tiles.speckle_rule(share, source, seed=0.15, radius=4)
    assert out[0, [0, 3, 12, 15, 18, 19]].tolist() == [40, 5, 0, 200, 0, 255]


def test_settings_name_carries_the_google_rule():
    name = tiles.settings_name({**tiles.RECOMMENDED, "google_floor": 0.012, "google_seed": 0.15})
    assert name == "z12-graded-full0.25-mean-drop-gfloor0.012-gseed0.15"
