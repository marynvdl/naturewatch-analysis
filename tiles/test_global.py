"""Checks for the sharded global PMTiles run. They need GDAL and no network.

Run: python -m pytest tiles
"""
import array
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml
from osgeo import gdal, osr

sys.path.insert(0, str(Path(__file__).parent))

import cropland  # noqa: E402
import global_cropland as gc  # noqa: E402
import pyramid  # noqa: E402

RECIPE = yaml.safe_load((Path(__file__).resolve().parents[1] / "cookbook" / "recipes" / "cropland-2024-v2.yaml").read_text())
TOP = 8
PIXEL = 0.05


def write_cell(folder: Path, name: str, seed: int, nodata_band: bool = False) -> None:
    """A 10 degree cell at 0.05 degrees: sea of class 0 with a few cropland blocks."""
    west, south, east, north = gc.cell_bounds(name)
    size = int(round(10 / PIXEL))
    rng = np.random.default_rng(seed)
    values = np.zeros((size, size), dtype=np.uint8)
    for _ in range(12):
        y, x = rng.integers(0, size - 12, 2)
        values[y : y + rng.integers(1, 12), x : x + rng.integers(1, 12)] = 1
    if nodata_band:
        values[:, : size // 4] = 255
    folder.mkdir(parents=True, exist_ok=True)
    dataset = gdal.GetDriverByName("GTiff").Create(str(folder / f"{name}.tif"), size, size, 1, gdal.GDT_Byte)
    dataset.SetGeoTransform((west, PIXEL, 0, north, 0, -PIXEL))
    spatial = osr.SpatialReference()
    spatial.ImportFromEPSG(4326)
    spatial.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dataset.SetProjection(spatial.ExportToWkt())
    band = dataset.GetRasterBand(1)
    band.WriteRaster(0, 0, size, size, array.array("B", values.ravel().tolist()).tobytes())
    band.SetNoDataValue(255)
    dataset.FlushCache()


@pytest.fixture(scope="module")
def sprout(tmp_path_factory):
    folder = tmp_path_factory.mktemp("sprout")
    for seed, name in enumerate(["20S_020E", "20S_030E", "30S_020E"]):
        write_cell(folder, name, seed, nodata_band=name == "20S_030E")
    return folder


def small_recipe(sprout: Path) -> dict:
    recipe = json.loads(json.dumps(RECIPE))
    recipe["tiles"]["max_zoom"] = TOP
    recipe["sprout"]["tiles_path"] = str(sprout)
    return recipe


def tiles_of(path: Path) -> dict:
    return {key: data for key, data in pyramid.read_archive(path)}


def test_cell_bounds_use_the_name_as_the_top_left_corner():
    assert gc.cell_bounds("20S_020E.tif") == (20, -30, 30, -20)
    assert gc.cell_bounds("70N_160W") == (-160, 60, -150, 70)
    with pytest.raises(ValueError):
        gc.cell_bounds("tile.tif")


def test_the_real_recipe_plans_every_cell_once_or_more():
    plan = gc.make_plan(RECIPE)
    ids = [shard["id"] for shard in plan]
    assert len(ids) == len(set(ids))
    covered = {name for shard in plan for name in shard["sources"]}
    assert covered == {Path(name).stem for name in RECIPE["seed"]["tiles"]}
    from pmtiles.tile import zxy_to_tileid

    order = [zxy_to_tileid(s["z"], s["x"], s["y"]) for s in plan]
    assert order == sorted(order)
    assert 250 < len(plan) < 400


def test_refuses_an_unset_resampling():
    recipe = json.loads(json.dumps(RECIPE))
    recipe["tiles"]["resampling"] = None
    with pytest.raises(ValueError, match="resampling is unset"):
        gc.require_contract(recipe)


def test_refuses_a_resampling_that_is_not_a_class_method():
    recipe = json.loads(json.dumps(RECIPE))
    recipe["tiles"]["resampling"] = "share"
    with pytest.raises(SystemExit):
        gc.require_contract(recipe)


def test_png_round_trip_keeps_class_and_nodata():
    values = np.full((256, 256), 255, dtype=np.uint8)
    values[:100] = 0
    values[:10, :10] = 1
    assert np.array_equal(gc.decode_png(cropland.tile_png(values)), values)


def test_sharded_archive_equals_a_single_process_build(sprout, tmp_path):
    recipe = small_recipe(sprout)
    stats = gc.command_local(recipe, str(sprout), tmp_path / "run", TOP, None)
    assert stats["shards"] >= 3
    merged = tiles_of(tmp_path / "run" / "cropland.pmtiles")

    names = [path.name for path in sorted(sprout.glob("*.tif"))]
    vrt = tmp_path / "all.vrt"
    gdal.BuildVRT(str(vrt), [str(sprout / name) for name in names], srcNodata=255, VRTNodata=255).FlushCache()
    reference_path = tmp_path / "reference.pmtiles"
    cropland.build_pmtiles(vrt, reference_path, recipe)
    reference = tiles_of(reference_path)

    assert set(merged) == set(reference)
    assert [key for key in merged if key[0] == 0], "zoom 0 tile is missing"
    different = [key for key in merged if merged[key] != reference[key]]
    assert different == []


def test_archive_is_in_tile_id_order_across_zooms(sprout, tmp_path):
    from pmtiles.tile import zxy_to_tileid

    gc.command_local(small_recipe(sprout), str(sprout), tmp_path, TOP, None)
    ids = [zxy_to_tileid(*key) for key in tiles_of(tmp_path / "cropland.pmtiles")]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))
    zooms = [key[0] for key in tiles_of(tmp_path / "cropland.pmtiles")]
    assert zooms == sorted(zooms)


def test_merge_stops_when_a_shard_is_out_of_order(sprout, tmp_path):
    recipe = small_recipe(sprout)
    plan = gc.make_plan(recipe, {"20S_020E", "20S_030E", "30S_020E"})
    scratch = str(tmp_path / "scratch")
    for index in range(len(plan)):
        gc.run_shard(recipe, index, tmp_path / "tmp", scratch, str(sprout), plan, TOP)
    with pytest.raises(pyramid.MergeError):
        gc.run_merge(
            recipe, tmp_path / "merge", scratch, str(tmp_path / "out.pmtiles"), plan=list(reversed(plan)),
            top_zoom=TOP, keep_scratch=True, publish=False,
        )


def test_merge_stops_when_a_tile_is_written_twice(tmp_path):
    shard = {"id": "5-0-0", "z": 5, "x": 0, "y": 0, "sources": []}
    tile = (5, 0, 0)
    data = cropland.tile_png(np.zeros((256, 256), dtype=np.uint8))
    path = tmp_path / "a.pmtiles"
    pyramid.write_archive(
        [(pyramid.zxy_to_tileid(*tile), data)], path, pyramid.archive_header((0, 0, 1, 1), 5, 5), {}
    )
    record = {"tiles_by_zoom": {"5": 1}}
    twice = [shard, {**shard, "id": "5-0-0-again"}]
    with pytest.raises(pyramid.MergeError):
        list(
            pyramid.merge_entries(
                twice, {s["id"]: path for s in twice}, {s["id"]: record for s in twice}, 5, 5, []
            )
        )


def test_merge_stops_when_a_tile_comes_from_the_wrong_shard(tmp_path):
    data = cropland.tile_png(np.zeros((256, 256), dtype=np.uint8))
    path = tmp_path / "a.pmtiles"
    pyramid.write_archive(
        [(pyramid.zxy_to_tileid(5, 3, 3), data)], path, pyramid.archive_header((0, 0, 1, 1), 5, 5), {}
    )
    shard = {"id": "5-0-0", "z": 5, "x": 0, "y": 0, "sources": []}
    with pytest.raises(pyramid.MergeError, match="not its ancestor"):
        list(pyramid.merge_entries([shard], {shard["id"]: path}, {shard["id"]: {"tiles_by_zoom": {"5": 1}}}, 5, 5, []))


def test_a_finished_shard_is_skipped_and_a_damaged_one_is_rebuilt(sprout, tmp_path, capsys):
    recipe = small_recipe(sprout)
    plan = gc.make_plan(recipe, {"20S_020E"})
    scratch = str(tmp_path / "scratch")
    first = gc.run_shard(recipe, 0, tmp_path / "tmp", scratch, str(sprout), plan, TOP)
    capsys.readouterr()
    again = gc.run_shard(recipe, 0, tmp_path / "tmp", scratch, str(sprout), plan, TOP)
    assert "skipping" in capsys.readouterr().out
    assert again["bytes"] == first["bytes"]
    file_path, _record = gc.shard_paths(scratch, plan[0])
    Path(file_path).write_bytes(Path(file_path).read_bytes()[:-5])
    gc.run_shard(recipe, 0, tmp_path / "tmp", scratch, str(sprout), plan, TOP)
    assert "skipping" not in capsys.readouterr().out
    assert Path(file_path).stat().st_size == first["bytes"]


def test_a_changed_resampling_does_not_reuse_a_shard(sprout, tmp_path, capsys):
    recipe = small_recipe(sprout)
    plan = gc.make_plan(recipe, {"20S_020E"})
    scratch = str(tmp_path / "scratch")
    gc.run_shard(recipe, 0, tmp_path / "tmp", scratch, str(sprout), plan, TOP)
    other = json.loads(json.dumps(recipe))
    other["tiles"]["resampling"] = "max"
    capsys.readouterr()
    gc.run_shard(other, 0, tmp_path / "tmp", scratch, str(sprout), plan, TOP)
    assert "skipping" not in capsys.readouterr().out


def test_merge_names_the_missing_shards(sprout, tmp_path):
    recipe = small_recipe(sprout)
    plan = gc.make_plan(recipe, {"20S_020E", "30S_020E"})
    with pytest.raises(SystemExit, match="shards are missing"):
        gc.run_merge(
            recipe, tmp_path / "merge", str(tmp_path / "empty"), str(tmp_path / "o.pmtiles"), plan=plan,
            top_zoom=TOP, publish=False,
        )


def test_verify_against_the_sprout_agrees_and_catches_a_change(sprout, tmp_path):
    recipe = small_recipe(sprout)
    vrt = tmp_path / "sprout.vrt"
    names = ["20S_020E", "20S_030E", "30S_020E"]
    gdal.BuildVRT(str(vrt), [str(sprout / f"{n}.tif") for n in names], srcNodata=255, VRTNodata=255).FlushCache()
    recipe["sprout"]["path"] = str(vrt)
    plan = gc.make_plan(recipe, set(names))
    gc.command_local(recipe, str(sprout), tmp_path / "run", TOP, None)
    archive = tmp_path / "run" / "cropland.pmtiles"
    contract = gc.require_contract(recipe)
    result = gc.verify_against_sprout(recipe, contract, archive, plan, 4, TOP, root_zoom=5)
    assert result["tiles_compared"] > 0
    other = json.loads(json.dumps(recipe))
    other["tiles"]["resampling"] = "max"
    with pytest.raises(pyramid.MergeError, match="disagrees"):
        gc.verify_against_sprout(other, gc.require_contract(other), archive, plan, 8, TOP, root_zoom=5)


def test_range_check_needs_206_and_the_magic(monkeypatch):
    class Response:
        def __init__(self, status, body):
            self.status, self._body, self.headers = status, body, {"Content-Range": "bytes 0-126/9", "Cache-Control": "x"}

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(gc.urllib.request, "urlopen", lambda request: Response(206, b"PMTiles" + b"\0" * 120))
    assert gc.check_range("gs://b/o.pmtiles")["status"] == 206
    monkeypatch.setattr(gc.urllib.request, "urlopen", lambda request: Response(200, b"PMTiles" + b"\0" * 120))
    with pytest.raises(OSError):
        gc.check_range("gs://b/o.pmtiles")


def test_batch_jobs_use_spot_in_the_region_with_capped_parallelism():
    plan = gc.make_plan(RECIPE)
    job = gc.batch_job("gs://nature-watch-bucket/scratch/tiles/x/", gc.SHARD_SHAPE, gc.SHARD_COMMAND, len(plan), "pmtiles-3.whl", "shards")
    group = job["taskGroups"][0]
    assert group["taskCount"] == str(len(plan))
    assert int(group["parallelism"]) * 2 <= 16
    policy = job["allocationPolicy"]["instances"][0]["policy"]
    assert policy["provisioningModel"] == "SPOT"
    assert job["allocationPolicy"]["location"]["allowedLocations"] == ["regions/europe-west1"]
    assert group["taskSpec"]["lifecyclePolicies"][0]["actionCondition"]["exitCodes"] == [50001]
    merge = gc.batch_job("gs://x/", gc.MERGE_SHAPE, gc.MERGE_COMMAND, 1, "pmtiles-3.whl", "merge")
    assert merge["allocationPolicy"]["instances"][0]["policy"]["bootDisk"]["sizeGb"] >= 50


def test_submit_without_approval_does_not_call_gcloud(monkeypatch, tmp_path, capsys):
    called = []
    monkeypatch.setattr(cropland, "gcloud", lambda args: called.append(args))
    monkeypatch.setattr(cropland, "upload_with_gcloud", lambda *a: called.append(a))
    monkeypatch.setattr(gc, "ROOT", tmp_path)
    work = tmp_path / "data" / "tiles" / RECIPE["id"] / "global"
    work.mkdir(parents=True)
    (work / "pmtiles-3.8.1-py3-none-any.whl").write_bytes(b"")
    gc.command_submit(Path(__file__).resolve().parents[1] / "cookbook" / "recipes" / "cropland-2024-v2.yaml", None, False, False)
    assert called == []
    assert "not submitted" in capsys.readouterr().out
