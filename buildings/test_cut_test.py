"""Offline tests for the Google cut test (task 03b). Run: python -m pytest buildings"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import compare  # noqa: E402
import cut_test  # noqa: E402
import ee_box  # noqa: E402


def test_cut_keeps_values_at_or_above_the_cut_and_averages_over_valid_pixels():
    fine = np.full((2, 4), 0.1, dtype=np.float32)  # two cells of 2 x 2 pixels
    fine[0, 0] = 0.8
    fine[1, 1] = 0.2
    fine[:, 3] = np.nan  # half of the second cell is nodata
    v = cut_test.cell_values(fine, cut=0.2, sub=2)
    assert v["valid"].tolist() == [[1.0, 0.5]]
    assert v["mean"][0, 0] == pytest.approx((0.8 + 0.1 + 0.1 + 0.2) / 4)
    assert v["cut"][0, 0] == pytest.approx((0.8 + 0.2) / 4)
    assert v["hit"][0, 0] == pytest.approx(0.5)
    assert v["cut"][0, 1] == 0 and v["mean"][0, 1] == pytest.approx(0.1)


def test_faint_noise_is_cut_to_zero():
    fine = np.full((50, 50), 0.15, dtype=np.float32)
    v = cut_test.cell_values(fine)
    assert v["mean"][0, 0] == pytest.approx(0.15) and v["cut"][0, 0] == 0 and v["hit"][0, 0] == 0


def test_lone_outlines_have_no_neighbour_within_100_m():
    # Two outlines 50 m apart, one 1 km away. 0.0001 degree of latitude is about 11 m.
    pts = np.array([[23.0, 6.0], [23.0, 6.00045], [23.0, 6.009]])
    assert cut_test.lone_mask(pts).tolist() == [False, False, True]


def test_points_fall_in_their_sprout_cell():
    g = compare.Grid(812000, 334000, 10, 10)
    pts = np.array([[g.west + 0.5 * compare.CELL, g.north - 0.5 * compare.CELL],
                    [g.west + 3.2 * compare.CELL, g.north - 7.9 * compare.CELL]])
    rows, cols = cut_test.cells_of(g, pts)
    assert rows.tolist() == [0, 7] and cols.tolist() == [0, 3]


def test_cut_test_boxes_are_10_km_and_left_out_of_compare_all():
    boxes = compare.load_boxes()
    cut = [n for n, b in boxes.items() if b["kind"] == "cut-test"]
    assert sorted(cut) == ["bria-cut", "kocho-cut", "yalinga-cut"]
    assert all(boxes[n]["size_km"] == 10 for n in cut)


def test_export_names_carry_the_value_threshold_and_scale():
    assert ee_box.export_name("kocho-cut", "cut", 0.2, 0.5) == "kocho-cut-cut0.2-0.5m"
    assert ee_box.export_name("bria-cut", "cut", 0.2, 4.0) == "bria-cut-cut0.2-4m"
