"""Offline tests for the buildings comparison. Run: python -m pytest buildings"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import compare  # noqa: E402
import ee_box  # noqa: E402
import render  # noqa: E402


def test_box_grid_snaps_outward_to_sprout_cells():
    g = compare.box_grid(13.326, -1.533, 5)
    assert g.west <= -1.533 - 0.0229 and g.east >= -1.533 + 0.0229
    assert g.south <= 13.326 - 0.0225 and g.north >= 13.326 + 0.0225
    for edge in (g.west + 180, 90 - g.north):
        assert edge / compare.CELL == pytest.approx(round(edge / compare.CELL))
    assert 180 <= g.cols <= 190 and 180 <= g.rows <= 185


def test_cell_area_matches_the_spheroid_at_the_equator_and_shrinks_north():
    eq = compare.cell_area_km2(compare.Grid(720000, 359999, 1, 2))
    assert eq[0, 0] == pytest.approx(27.83e-3 * 27.65e-3, rel=0.01)
    north = compare.cell_area_km2(compare.box_grid(60.0, 10.0, 1))
    assert north[0, 0] < eq[0, 0] * 0.55


def test_block_mean_averages_whole_blocks():
    fine = np.zeros((4, 6), dtype=np.float32)
    fine[:2, :2] = 1
    fine[2:, 4:] = 0.5
    assert compare.block_mean(fine, 2).tolist() == [[1.0, 0.0, 0.0], [0.0, 0.0, 0.5]]


def test_tempo_names_count_rows_from_the_south():
    # The index row for L15-1204E-1463N has tile_x 1204 and tile_y 584 at zoom 11.
    assert compare.tempo_name(1204, 584) == "L15-1204E-1463N.tif"
    x, y = compare.xyz(31.72, 60.887)
    assert (x, y) == (1204, 584)


def _box(tempo, overture, google=None):
    rows, cols = tempo.shape
    return {
        "area": np.ones((rows, 1)),
        "thresholds": np.array(compare.THRESHOLDS),
        "google": np.zeros((len(compare.THRESHOLDS), rows, cols)) if google is None else google,
        "google_valid": np.ones((rows, cols)),
        "google_top": np.array(0.0),
        "tempo": tempo,
        "overture": overture,
    }


def test_floor_drops_low_tempo_cells_and_counts_buildings_lost():
    tempo = np.array([[0.004, 0.03], [0.0, np.nan]])
    overture = np.array([[0.1, 0.2], [0.05, 0.0]])
    data = _box(tempo, overture)
    none = compare.floor_effect(data, 0.0)
    assert none["tempo_cells"] == 2
    assert none["overture_km2_in_dropped_cells"] == 0
    assert none["overture_km2_without_tempo"] == pytest.approx(0.05)
    f = compare.floor_effect(data, 0.01)
    assert f["tempo_cells"] == 1 and f["tempo_km2"] == pytest.approx(0.03)
    assert f["overture_km2_in_dropped_cells"] == pytest.approx(0.1)
    assert f["overture_km2_without_tempo"] == pytest.approx(0.15)


def test_figures_count_google_only_where_it_covers():
    google = np.zeros((len(compare.THRESHOLDS), 1, 2))
    google[3] = [[0.5, 0.5]]
    data = _box(np.zeros((1, 2)), np.zeros((1, 2)), google)
    data["google_valid"] = np.array([[1.0, 0.0]])
    f = compare.figures(data)
    assert f["google_km2_0.50"] == pytest.approx(0.5)
    assert f["google_covered"] == 0.5


def test_shades_are_even_in_sqrt_share_and_any_building_shows():
    k = render.shade_index(np.array([0.0, 0.0001, 0.01, 0.25, 1.0, np.nan]))
    assert k.tolist() == [0, 1, 1, math.ceil(0.5 * render.STEPS), render.STEPS, 0]


def test_agreement_compares_covered_cells_only():
    cog = np.array([[0.2, 0.4], [0.0, 1.0]])
    eeg = np.array([[0.2, 0.5], [0.0, np.nan]])
    covered = np.array([[True, True], [True, False]])
    a = ee_box.agreement(cog, eeg, np.ones((2, 1)), covered)
    assert a["cells"] == 3
    assert a["cog_km2"] == pytest.approx(0.6) and a["ee_km2"] == pytest.approx(0.7)
    assert a["mean_abs_diff"] == pytest.approx(0.1 / 3)


def test_seed_rule_keeps_low_cells_only_near_a_dense_cell():
    tempo = np.zeros((1, 200))
    tempo[0, 10] = 0.2  # a village core
    tempo[0, 50] = 0.02  # 40 cells, about 1.1 km, from the core
    tempo[0, 150] = 0.08  # 140 cells, about 3.9 km away: noise
    kept = compare.tempo_kept(tempo, seed=0.15, radius_m=2000)
    assert kept[0, 10] == 0.2 and kept[0, 50] == 0.02 and kept[0, 150] == 0
    assert compare.tempo_kept(tempo, seed=0.15, radius_m=500)[0, 50] == 0
    assert compare.tempo_kept(tempo, floor=0.05)[0, 50] == 0
