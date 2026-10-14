"""Tests for export_fire.py. They need no Earth Engine login and no network.

Run: python -m pytest gee/fire
"""
import json
from datetime import date

import pytest

import export_fire as ef


def test_a_year_has_27_windows_like_the_notebook():
    # pd.date_range("YYYY-01-01", "YYYY-12-31", freq="14d") gives 27 dates in every year
    for year in (2023, 2024, 2025):
        assert len(ef.year_windows(year)) == 27


def test_windows_start_on_1_january_and_step_14_days():
    windows = ef.year_windows(2025)
    assert windows[0] == date(2025, 1, 1)
    assert all((b - a).days == 14 for a, b in zip(windows, windows[1:]))


def test_last_window_is_30_or_31_december_and_reaches_into_january():
    assert ef.year_windows(2024)[-1] == date(2024, 12, 30)  # leap year
    assert ef.year_windows(2025)[-1] == date(2025, 12, 31)
    assert ef.last_window_end(2025) == date(2026, 1, 14)


def test_windows_match_pandas_when_it_is_installed():
    pd = pytest.importorskip("pandas")
    for year in (2024, 2025):
        expected = [d.date() for d in pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="14D")]
        assert ef.year_windows(year) == expected


def test_blocks_file_has_25_blocks_with_ids_1_to_25():
    blocks = ef.load_blocks()
    assert [b.id for b in blocks] == list(range(1, 26))


def test_file_names_continue_the_zero_based_names_of_earlier_years():
    # fire2024_0 ... fire2024_24 are in the bucket; block id 1 is file index 0
    blocks = ef.load_blocks()
    assert [ef.output_name(2024, b) for b in blocks] == [f"fire2024_{i}" for i in range(25)]
    assert ef.output_name(2025, blocks[0]) == "fire2025_0"
    assert ef.output_name(2025, blocks[-1]) == "fire2025_24"


def test_export_settings_match_the_notebook():
    block = ef.load_blocks()[3]  # id 4 -> fire2025_3
    settings = ef.export_settings(2025, block)
    assert settings == {
        "description": "fire2025_3",
        "bucket": "nature-watch-bucket",
        "fileNamePrefix": "COGS/fire/2025/fire2025_3",
        "crs": "EPSG:4326",
        "scale": 30,
        "maxPixels": 3984245980,
        "fileFormat": "GeoTIFF",
        "formatOptions": {"cloudOptimized": True},
    }


def test_blocks_are_15_degree_squares_that_tile_without_gaps():
    for block in ef.load_blocks():
        props = block.feature["properties"]
        assert props["right"] - props["left"] == pytest.approx(15)
        assert props["top"] - props["bottom"] == pytest.approx(15)
    # blocks 1 and 4 are neighbours in longitude
    b = {blk.id: blk.feature["properties"] for blk in ef.load_blocks()}
    assert b[1]["right"] == pytest.approx(b[4]["left"])


def test_block_region_has_straight_edges(monkeypatch):
    # Regression: with Earth Engine's default (geodesic) edges the exported file was
    # about 0.24 degrees taller than the block. Planar edges match the notebook.
    import sys
    import types

    calls = []

    class FakeGeometry:
        def __init__(self, *args):
            calls.append(args)

    monkeypatch.setitem(sys.modules, "ee", types.SimpleNamespace(Geometry=FakeGeometry))
    block = ef.load_blocks()[0]
    ef.block_region(block)
    geojson, crs, geodesic = calls[0]
    assert geojson == block.feature["geometry"]
    assert crs == "EPSG:4326"
    assert geodesic is False


def test_block_selection():
    blocks = ef.load_blocks()
    assert [b.id for b in ef.parse_block_selection("3,7-9", blocks)] == [3, 7, 8, 9]
    assert len(ef.parse_block_selection(None, blocks)) == 25
    with pytest.raises(ValueError, match="no such block"):
        ef.parse_block_selection("26", blocks)


def test_load_blocks_rejects_duplicate_or_missing_ids(tmp_path):
    def write(ids):
        features = [{"type": "Feature", "properties": {"id": i}, "geometry": None} for i in ids]
        path = tmp_path / "b.geojson"
        path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
        return path

    with pytest.raises(ValueError, match="duplicate"):
        ef.load_blocks(write([1, 1]))
    with pytest.raises(ValueError, match="integer id"):
        ef.load_blocks(write([0]))


def test_plan_runs_without_credentials(capsys):
    assert ef.main(["plan", "--year", "2025"]) == 0
    out = capsys.readouterr().out
    assert "windows: 27" in out
    assert "gs://nature-watch-bucket/COGS/fire/2025/fire2025_24.tif" in out


def test_dry_run_export_starts_nothing_and_needs_no_credentials(capsys):
    assert ef.main(["export", "--year", "2025", "--dry-run", "--blocks", "1-2"]) == 0
    out = capsys.readouterr().out
    assert "Nothing was started" in out
    assert "fire2025_1.tif" in out and "fire2025_2.tif" not in out


def test_year_in_the_future_is_refused():
    assert ef.main(["plan", "--year", "2999"]) == 2
