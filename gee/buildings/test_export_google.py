"""Tests for export_google.py. They need no Earth Engine login and no network.

Run: python -m pytest gee/buildings
"""
import pytest

import export_google as eg

RECIPE = eg.load_recipe()


def test_the_block_list_has_the_recipe_count_and_aligned_corners():
    blocks = eg.load_blocks(RECIPE)
    assert len(blocks) == 145
    assert all(b.west % 10 == 0 and b.south % 10 == 0 for b in blocks)
    assert all(-180 <= b.west < 180 and -90 <= b.south < 90 for b in blocks)


def test_block_names_use_the_south_west_corner():
    assert eg.Block(-10, 0).name == "W010N00"
    assert eg.Block(0, -10).name == "E000S10"
    assert eg.Block(-120, -30).name == "W120S30"
    assert eg.Block(100, 30).name == "E100N30"


def test_a_block_is_one_file_on_the_sprout_grid():
    block = eg.Block(0, 10)
    settings = eg.export_settings(RECIPE, block)
    assert settings["bucket"] == "nature-watch-bucket"
    assert settings["fileNamePrefix"] == "seeds/buildings/google-temporal/2023/v1/E000N10"
    assert settings["crs"] == "EPSG:4326"
    assert settings["crsTransform"] == [0.00025, 0, -180, 0, -0.00025, 90]
    assert settings["formatOptions"] == {"cloudOptimized": True, "noData": 255}
    side = round(block.size / 0.00025)
    assert side == 40000
    assert settings["fileDimensions"][0] >= side and settings["fileDimensions"][0] % 256 == 0
    assert eg.block_cells(RECIPE, block) <= settings["maxPixels"]


def test_selecting_blocks_by_name():
    blocks = eg.load_blocks(RECIPE)
    picked = eg.select_blocks("W010N10, E000N00", blocks)
    assert {b.name for b in picked} == {"W010N10", "E000N00"}
    assert eg.select_blocks(None, blocks) == blocks
    with pytest.raises(ValueError):
        eg.select_blocks("E170N80", blocks)


def test_the_plan_needs_no_network(capsys):
    assert eg.main(["plan", "--blocks", "E000N10"]) == 0
    assert "E000N10.tif" in capsys.readouterr().out
