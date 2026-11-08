"""Checks for the v3 verifier. They need GDAL and no network. Run: python -m pytest tiles"""
import sys
from pathlib import Path

import numpy as np
import pytest
from osgeo import gdal

sys.path.insert(0, str(Path(__file__).parent))

import cropland  # noqa: E402
import global_cropland as gc  # noqa: E402
import pyramid  # noqa: E402
import test_global as tg  # noqa: E402
import transcode  # noqa: E402
import verify_v3 as v  # noqa: E402

sprout = tg.sprout  # the fixture


@pytest.fixture(scope="module")
def archives(sprout, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("verify")
    recipe = tg.share_test_recipe(sprout)
    gc.command_local(recipe, str(sprout), tmp / "run", tg.SHARE_TOP, None)
    names = ["20S_020E", "20S_030E", "30S_020E"]
    vrt = tmp / "sprout.vrt"
    gdal.BuildVRT(str(vrt), [str(sprout / f"{n}.tif") for n in names], srcNodata=255, VRTNodata=255).FlushCache()
    recipe["sprout"]["path"] = str(vrt)
    v2 = tmp / "v2.pmtiles"
    cropland.build_pmtiles(vrt, v2, tg.small_recipe(sprout))
    return recipe, tmp / "run" / "cropland.pmtiles", v2


def test_tile_ids_agree_between_v2_and_v3(archives):
    _recipe, v3, v2 = archives
    result = v.check_tile_ids(v.read_table(v3)[0], v.read_table(v2)[0])
    assert result["v3_without_a_v2_tile"] == 0
    assert result["v2_without_a_v3_tile"] == 0
    assert result["v3_zoom_range"] == [0, tg.SHARE_TOP]


def test_the_ids_check_catches_a_missing_tile(archives, tmp_path):
    _recipe, v3, v2 = archives
    ids3 = v.read_table(v3)[0]
    result = v.check_tile_ids(ids3[:-3], v.read_table(v2)[0])
    assert result["v2_without_a_v3_tile"] > 0


def test_nodata_of_v3_equals_v2_in_every_slice(archives):
    _recipe, v3, v2 = archives
    results = [v.check_slice(v3, v2, i, 3) for i in range(3)]
    assert sum(r["tiles_checked"] for r in results) == len(v.read_table(v3)[0])
    assert [r["pixels_wrong"] for r in results] == [0, 0, 0]


def test_the_nodata_check_catches_a_changed_tile(archives, tmp_path):
    from pmtiles.tile import zxy_to_tileid

    _recipe, v3, v2 = archives
    tiles = tg.tiles_of(v3)
    key = next(k for k, data in tiles.items() if k[0] == tg.SHARE_TOP and (transcode.decode_rgba(data)[..., 3] == 0).any())
    rgba_all_valid = np.zeros((512, 512), dtype=np.float32)
    tiles[key] = cropland.tile_png_share(rgba_all_valid)  # gray 0 everywhere, no nodata
    broken = tmp_path / "broken.pmtiles"
    entries = sorted((zxy_to_tileid(*k), d) for k, d in tiles.items())
    pyramid.write_archive(entries, broken, pyramid.archive_header((20, -40, 40, -20), 0, tg.SHARE_TOP), {})
    assert sum(v.check_slice(broken, v2, i, 2)["pixels_wrong"] for i in range(2)) > 0


def test_the_sprout_check_agrees_and_reports_an_error_in_opacity_points(archives):
    recipe, v3, _v2 = archives
    result = v.check_sprout(recipe, v3, 3, 1, min_body=0, root_zoom=5)
    assert result["bytes_equal"] and result["tiles_compared"] > 0
    error = result["opacity_error_graded_zooms"]
    assert error["cells"] > 0 and 0 <= error["mean"] <= error["max"] < 0.1


def test_the_opacity_curve_is_the_paint():
    share = np.array([0.0, 0.25, 1.0])
    assert np.allclose(v.opacity(share), [0.0, 0.575, 0.95])


def test_submit_without_approval_calls_nothing(monkeypatch, tmp_path, capsys):
    called = []
    monkeypatch.setattr(cropland, "gcloud", lambda args: called.append(args))
    monkeypatch.setattr(cropland, "upload_with_gcloud", lambda *a: called.append(a))
    monkeypatch.setattr(gc, "ROOT", tmp_path)
    work = tmp_path / "data" / "tiles" / "cropland-2024-v3" / "global"
    work.mkdir(parents=True)
    (work / "pmtiles-3.8.1-py3-none-any.whl").write_bytes(b"")
    path = Path(__file__).resolve().parents[1] / "cookbook" / "recipes" / "cropland-2024-v3.yaml"
    v.command_submit(path, "gs://nature-watch-bucket/scratch/x.pmtiles", False)
    assert called == [] and "not submitted" in capsys.readouterr().out
    assert v.VERIFY_SHAPE["machine"] == "e2-standard-2" and "verify_v3.py" in gc.CODE_FILES
