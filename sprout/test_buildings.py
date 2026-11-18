"""Tests for sprout/buildings.py: the merge, the seed rule, blocks and the VRT."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "buildings"))

import buildings as b  # noqa: E402

RECIPE = b.load_recipe(b.DEFAULT_RECIPE)
MARGIN = b.seed_radius_cells(RECIPE)


def test_radius_is_72_cells():
    assert MARGIN == 72


def test_block_names_round_trip():
    assert b.block_name(-10, 0) == "W010N00"
    assert b.block_name(0, -10) == "E000S10"
    assert b.block_corner("W180S90") == (-180, -90)
    for name in b.all_blocks():
        assert b.block_name(*b.block_corner(name)) == name
    assert len(b.all_blocks()) == 648
    with pytest.raises(ValueError):
        b.block_corner("E005N00")


def test_google_block_names_match_seed():
    import json

    blocks = json.loads(b.GOOGLE_BLOCKS.read_text())["blocks"]
    names = [b.block_name(w, s) for w, s in blocks]
    assert len(set(names)) == RECIPE["seeds"]["google"]["block_count"]
    assert set(names) <= set(b.all_blocks())


def test_block_grid():
    g = b.block_grid("E000N20", RECIPE)
    assert (g["west"], g["north"], g["cols"], g["rows"]) == (0.0, 30.0, 40000, 40000)


def test_mosaics_meeting():
    # z4-8-6 is 0 to 22.5 E, 21.94 to 40.98 N; z4-8-7 is 0 to 22.5 E, 0 to 21.94 N.
    w, s, e, n = b.mosaic_lonlat(8, 7, 4)
    assert (w, s, e) == (0.0, 0.0, 22.5)
    assert n == pytest.approx(21.943, abs=1e-3)
    got = b.mosaics_meeting((0, 20, 10, 30), ["z4-8-6.tif", "z4-8-7.tif", "z4-7-7.tif", "z4-9-7.tif"], 4)
    assert got == ["z4-8-6.tif", "z4-8-7.tif"]


def test_plan_blocks_union():
    names = b.plan_blocks(["W010N00"], ["z4-8-7.tif"], 4, RECIPE)
    # z4-8-7 is 0 to 22.5 E and 0 to 21.94 N: three columns and three rows of blocks.
    want = {f"E{w:03d}N{s:02d}" for w in (0, 10, 20) for s in (0, 10, 20)} | {"W010N00"}
    assert set(names) == want
    assert names == [n for n in b.all_blocks() if n in set(names)]


def test_groups_keep_order_and_count():
    names = [f"E{i:03d}N00" for i in range(0, 100, 10)]
    out = b.groups(names, {n: 1.0 for n in names}, 3)
    assert len(out) == 3
    assert [n for g in out for n in g] == names


def test_dilate_matches_scipy():
    from scipy.ndimage import maximum_filter

    rng = np.random.default_rng(1)
    mask = rng.random((300, 260)) > 0.9995
    for radius in (0, 1, 5, 72):
        want = maximum_filter(mask.astype(np.uint8), size=2 * radius + 1, mode="constant") > 0
        assert (b.dilate(mask, radius) == want).all()


def strip(tempo_inner: np.ndarray, margin: int = MARGIN, fill=np.nan) -> np.ndarray:
    rows, cols = tempo_inner.shape
    out = np.full((rows + 2 * margin, cols + 2 * margin), fill, np.float32)
    out[margin:margin + rows, margin:margin + cols] = tempo_inner
    return out


def test_merge_sources_and_nodata():
    t = np.array([[0.0, 0.2, np.nan], [0.5, 0.01, 0.01]], np.float32)
    g = np.array([[255, 255, 255], [100, 0, 255]], np.uint8)
    r = b.merge(g, strip(t), RECIPE, MARGIN)
    assert r["source"].tolist() == [[2, 2, 0], [1, 1, 2]]
    # 0.2 and 0.5 are seeds, so every TEMPO cell here is kept. 0.01 x 250 = 2.5 rounds to 2.
    assert r["share"].tolist() == [[0, 50, 255], [100, 0, 2]]
    assert r["counts"]["google_cells"] == 2
    assert r["counts"]["tempo_cells"] == 3
    assert r["counts"]["tempo_dropped"] == 0


def test_merge_seed_rule_drops_isolated_tempo():
    t = np.zeros((200, 200), np.float32)
    t[10, 10] = 0.05  # noise, no seed near
    t[150, 150] = 0.2  # seed
    t[150, 150 - 72] = 0.05  # 72 cells away: kept
    t[150 - 73, 150] = 0.05  # 73 cells away: dropped
    r = b.merge(None, strip(t, fill=0.0), RECIPE, MARGIN)
    share = r["share"]
    assert share[10, 10] == 0
    assert share[150, 150] == 50
    assert share[150, 78] == 12
    assert share[77, 150] == 0
    assert r["counts"]["tempo_dropped"] == 2
    assert (r["source"] == 2).all()


def test_seed_beyond_the_strip_counts():
    inner = np.zeros((10, 10), np.float32)
    inner[0, 0] = 0.05
    full = strip(inner, fill=0.0)
    full[0, 0] = 0.3  # in the margin, 72 cells up and left of inner[0, 0]
    r = b.merge(None, full, RECIPE, MARGIN)
    assert r["share"][0, 0] == 12
    full[0, 0] = 0.0
    assert b.merge(None, full, RECIPE, MARGIN)["share"][0, 0] == 0


def test_google_cells_are_not_seeds():
    t = np.zeros((20, 20), np.float32)
    t[5, 5] = 0.05
    g = np.full((20, 20), 255, np.uint8)
    g[5, 6] = 250  # Google sees a building beside it
    r = b.merge(g, strip(t, fill=0.0), RECIPE, MARGIN)
    assert r["share"][5, 5] == 0
    assert r["share"][5, 6] == 250


def test_merge_matches_tempo_kept_on_a_cached_box():
    compare = pytest.importorskip("compare")
    path = compare.CACHE / "kansas.npz"
    if not path.exists():
        pytest.skip("no cached box")
    tempo = compare.load_box("kansas")["tempo"]
    rule = RECIPE["sprout"]["tempo"]
    want = compare.tempo_kept(tempo, 0.0, rule["seed_share"], rule["seed_radius_m"])
    # compare.py sees nothing outside the box, so pad with no data.
    r = b.merge(None, strip(tempo, fill=np.nan), RECIPE, MARGIN)
    got = np.where(r["source"] == 2, r["share"], 0) / 250
    assert np.abs(got - want).max() <= 0.5 / 250 + 1e-6
    assert ((got > 0) == (np.rint(want * 250) > 0)).all()


def test_vrt_text_offsets_and_nodata():
    records = [
        {"block": "E000N20", "bytes": 10, "width": 40000, "height": 40000, "transform": [0.0, 0.00025, 0, 30.0, 0, -0.00025]},
        {"block": "E010N20", "bytes": 0, "width": 40000, "height": 40000, "transform": [10.0, 0.00025, 0, 30.0, 0, -0.00025]},
    ]
    text = b.vrt_text(RECIPE, records)
    assert 'rasterXSize="1440000" rasterYSize="720000"' in text
    assert '<DstRect xOff="720000" yOff="240000"' in text
    assert "E010N20" not in text
    assert text.count("<NoDataValue>255</NoDataValue>") == 1
    assert text.count("<SimpleSource>") == 2


def test_stac_lists_blocks_with_data():
    records = [
        {"block": "E000N20", "bytes": 10, "width": 1, "height": 1, "transform": [0.0, 0.00025, 0, 30.0, 0, -0.00025]},
        {"block": "E010N20", "bytes": 0, "width": 1, "height": 1, "transform": [10.0, 0.00025, 0, 30.0, 0, -0.00025]},
    ]
    collection, item = b.stac_documents(RECIPE, records)
    assert item["id"] == "buildings-2023-v1"
    assert "block-E000N20" in item["assets"] and "block-E010N20" not in item["assets"]
    assert item["assets"]["data"]["href"].endswith("/v1/buildings-2023.vrt")
    assert item["assets"]["data"]["raster:bands"][0]["scale"] == pytest.approx(0.004)
