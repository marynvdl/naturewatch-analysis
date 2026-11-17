"""Roads rules, burn and tile encoding on fixtures. No network."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import seed  # noqa: E402
import sprout  # noqa: E402
import tiles  # noqa: E402

RECIPE = {
    "grid": {"crs": "EPSG:4326", "pixel_size_deg": 0.00025, "origin": [-180, 90], "dtype": "uint8"},
    "source": {"release": "test"},
    "roads": {
        "subtype": "road",
        "surface": {"paved": ["paved", "paving_stones", "metal"], "unpaved": ["unpaved", "gravel", "dirt"]},
        "levels": {
            "major": {"value": 4, "classes": ["motorway", "trunk"]},
            "main": {"value": 3, "classes": ["primary", "secondary", "tertiary"]},
            "minor": {"value": 2, "classes": ["residential", "living_street", "service"]},
            "track": {"value": 1, "classes": ["track"]},
        },
        "by_surface": {"classes": ["unclassified", "unknown"], "paved": "minor", "otherwise": "track"},
    },
    "sprout": {"id": "roads-test", "bands": {"level": {0: "none", 1: "track", 2: "minor", 3: "main", 4: "major"}, "surface": {0: "unknown", 1: "paved", 2: "unpaved"}}},
}


def segment(cls, surfaces=None, subtype="road", sources=None, lon=10.0, lat=0.0):
    surface = "NULL" if surfaces is None else "[" + ", ".join(
        f"{{'value': '{v}', 'between': {'NULL' if b is None else f'[{b[0]}, {b[1]}]::DOUBLE[]'}}}" for v, b in surfaces
    ) + "]"
    src = "NULL" if sources is None else "[" + ", ".join(
        f"{{'dataset': '{d}', 'record_id': '{r}'}}" for d, r in sources
    ) + "]"
    return (
        f"SELECT '{cls}-{lon}' AS id, '{subtype}' AS subtype, '{cls}' AS class, NULL::VARCHAR AS subclass, "
        f"({surface})::STRUCT(value VARCHAR, \"between\" DOUBLE[])[] AS road_surface, "
        f"({src})::STRUCT(dataset VARCHAR, record_id VARCHAR)[] AS sources, {{'primary': 'n'}} AS names, "
        f"{{'xmin': {lon}, 'xmax': {lon + 0.001}, 'ymin': {lat}, 'ymax': {lat + 0.001}}} AS bbox, "
        f"ST_GeomFromText('LINESTRING({lon} {lat}, {lon + 0.001} {lat + 0.001})') AS geometry"
    )


def run(rows: list[str], bounds=None) -> dict:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("CREATE TABLE src AS " + " UNION ALL ".join(rows))
    result = con.execute(seed.seed_query("src", RECIPE["roads"], bounds)).fetchall()
    return {r[0]: {"class": r[1], "surface": r[3], "level": r[4], "osm_ids": r[5]} for r in result}


def test_levels_and_paths_left_out():
    out = run([segment(c) for c in ("motorway", "trunk", "primary", "tertiary", "residential", "service", "track", "footway", "path")]
              + [segment("standard_gauge", subtype="rail")])
    levels = {v["class"]: v["level"] for v in out.values()}
    assert levels == {"motorway": "major", "trunk": "major", "primary": "main", "tertiary": "main",
                      "residential": "minor", "service": "minor", "track": "track"}


def test_unclassified_and_unknown_go_by_surface():
    out = run([segment("unclassified", [("paved", None)], lon=1), segment("unclassified", [("gravel", None)], lon=2),
               segment("unclassified", None, lon=3), segment("unknown", [("metal", None)], lon=4), segment("unknown", None, lon=5)])
    assert {k: v["level"] for k, v in out.items()} == {
        "unclassified-1": "minor", "unclassified-2": "track", "unclassified-3": "track", "unknown-4": "minor", "unknown-5": "track"}


def test_surface_covers_most_of_the_length():
    out = run([
        segment("track", [("paved", (0, 0.3)), ("dirt", (0.3, 1.0))], lon=1),  # unpaved 0.7
        segment("track", [("paved", (0, 0.6)), ("dirt", (0.6, 1.0))], lon=2),  # paved 0.6
        segment("track", [("paved", (0, 0.2))], lon=3),  # unknown 0.8
        segment("track", [("paved", (0, 0.5)), ("unpaved", (0.5, 1.0))], lon=4),  # tie: paved
        segment("track", [("unpaved", (0, 0.5))], lon=5),  # tie with unknown: unpaved
        segment("track", [("paving_stones", None)], lon=6),
        segment("track", [("unknown", None)], lon=7),
    ])
    assert [out[f"track-{i}"]["surface"] for i in range(1, 8)] == ["unpaved", "paved", None, "paved", "unpaved", "paved", None]


def test_osm_way_ids_only():
    out = run([segment("primary", sources=[("OpenStreetMap", "w123@4"), ("OpenStreetMap", "r9@1"), ("OpenStreetMap", "w123@5"), ("Other", "w7@1")])])
    assert list(out.values())[0]["osm_ids"] == [123]


def test_bbox_filter():
    out = run([segment("primary", lon=1), segment("primary", lon=5)], bounds=(0.5, -1, 2, 1))
    assert list(out) == ["primary-1"]


def test_codes_order_and_decode():
    codes = [sprout.code(level, rank) for level in (1, 2, 3, 4) for rank in (0, 1, 2)]
    assert codes == list(range(1, 13))
    level, surface = sprout.decode(np.array(codes + [0]))
    assert level.tolist() == [1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4, 0]
    assert surface.tolist() == [0, 2, 1] * 4 + [0]


def test_snapped_window():
    west, north, cols, rows = sprout.snapped((10.0001, -1.0, 10.001, -0.9999), 0.00025)
    assert (west, north, cols, rows) == (10.0, -0.99975, 4, 1)


def test_burn_highest_level_and_paved_wins(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    lines = [  # three roads along the same row of cells, and one crossing
        ("track", "unpaved", "LINESTRING(0.0001 0.00012, 0.0009 0.00012)"),
        ("main", None, "LINESTRING(0.0001 0.00013, 0.0004 0.00013)"),
        ("main", "paved", "LINESTRING(0.0002 0.00013, 0.0003 0.00013)"),
        ("minor", "paved", "LINESTRING(0.00087 0.0001, 0.00087 0.0009)"),
    ]
    rows = " UNION ALL ".join(
        f"SELECT '{lvl}' AS level, {('NULL' if s is None else repr(s))}::VARCHAR AS surface, ST_GeomFromText('{w}') AS geometry"
        for lvl, s, w in lines
    )
    path = tmp_path / "seed.parquet"
    con.execute(f"COPY ({rows}) TO '{path}' (FORMAT parquet)")
    codes = sprout.burn(path, (0.0, 0.0, 0.001, 0.001), RECIPE)
    level, surface = sprout.decode(codes)
    bottom = level[-1]
    assert bottom.tolist() == [3, 3, 1, 2]  # main over track, minor crossing at the east end
    assert surface[-1].tolist()[:2] == [1, 1]  # the paved main wins the cell it shares with the unknown main
    assert level[0].tolist() == [0, 0, 0, 2]


def test_shade_keeps_any_road():
    share = np.array([[0, 1e-6, 0.25, 1.0]], dtype=np.float32)
    assert tiles.shade_index(share, 8).tolist() == [[0, 1, 4, 8]]


def test_reduce_counts_absent_children_as_no_road():
    one = tiles.RoadTile(np.ones((tiles.SIZE, tiles.SIZE), dtype=np.float32), None, 6)
    parent = tiles.reduce({(0, 0): one})
    assert parent.zoom == 5
    assert parent.share[0, 0] == 1.0
    assert parent.share[-1, -1] == 0.0
    assert tiles.reduce({}) is None


def test_encode_round_trip():
    transcode = tiles.transcode
    size = tiles.SIZE
    share = np.zeros((size, size), dtype=np.float32)
    share[0, :4] = [0, 0.01, 0.25, 1]
    red = transcode.decode_rgba(tiles.encode(tiles.RoadTile(share, None, 5), 8))
    assert red[0, :4, 0].tolist() == [0, 4, 64, 255]
    assert red[0, :4, 3].tolist() == [0, 255, 255, 255]
    code = np.zeros((size, size), dtype=np.uint8)
    code[0, :4] = [0, 1, 6, 12]  # none, track unknown, minor paved, major paved
    red = transcode.decode_rgba(tiles.encode(tiles.RoadTile(share, code, 11), 8))
    assert red[0, :4, 0].tolist() == [0, 4, 9, 17]
    assert red[0, 0, 3] == 0


# ---- global blocks (task 02) ----------------------------------------------------------

import blocks  # noqa: E402


def test_block_names_and_bounds():
    assert blocks.name(-30, 20) == "30S_020E"
    assert blocks.name(0, -180) == "00N_180W"
    assert blocks.bounds("30S_020E") == (20.0, -30.0, 30.0, -20.0)
    assert blocks.corner("80N_170E") == (80, 170)
    assert len(blocks.all_blocks()) == 648 and len(set(blocks.all_blocks())) == 648
    with pytest.raises(ValueError):
        blocks.corner("05N_020E")


def test_block_of_point_edges():
    assert blocks.of_point(28.0, -25.0) == "30S_020E"
    assert blocks.of_point(30.0, -20.0) == "20S_030E"  # east and north edges belong to the next block
    assert blocks.of_point(180.0, 90.0) == "80N_170E"  # except at 180° and 90°
    assert blocks.of_point(-180.0, -90.0) == "90S_180W"


def test_neighbours_wrap_and_stop_at_poles():
    around = blocks.neighbours("00N_170E")
    assert len(around) == 8 and "00N_180W" in around and "10S_180W" in around and "10N_160E" in around
    assert len(blocks.neighbours("80N_000E")) == 5
    assert len(blocks.neighbours("90S_180W")) == 5 and "90S_170E" in blocks.neighbours("90S_180W")


def test_groups_cover_every_block_in_order():
    weights = {"30S_020E": 1e6, "40N_000E": 3e6, "40N_010E": 2e6}
    out = blocks.groups(weights, 20, minimum=1000)
    assert len(out) <= 20
    assert [b for g in out for b in g] == blocks.all_blocks()
    heavy = [g for g in out if "40N_000E" in g][0]
    assert "30S_020E" not in heavy


def test_footer_weights_keys():
    assert blocks.footer_weights({"blocks": {"-30_20": {"rows": 5}, "unknown": {"rows": 1}}}) == {"30S_020E": 5.0}


def test_block_query_keeps_each_row_once():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    # centres at 19.9995 (west block), 20.0005 (this block), 29.9995 (this block), 30.0005 (east block)
    lons = (19.999, 20.0, 29.999, 30.0)
    con.execute("CREATE TABLE src AS " + " UNION ALL ".join(segment("primary", lon=x, lat=-25.0) for x in lons))
    kept = {r[0] for r in con.execute(seed.block_query("src", RECIPE["roads"], blocks.bounds("30S_020E"))).fetchall()}
    assert kept == {"primary-20.0", "primary-29.999"}
    west = {r[0] for r in con.execute(seed.block_query("src", RECIPE["roads"], blocks.bounds("30S_010E"))).fetchall()}
    assert west == {"primary-19.999"}


def test_burn_source_matches_burn_and_filters_neighbours(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    lines = [
        ("track", "unpaved", "LINESTRING(0.0001 0.00012, 0.0009 0.00012)"),
        ("main", None, "LINESTRING(0.0001 0.00013, 0.0004 0.00013)"),
        ("main", "paved", "LINESTRING(0.0002 0.00013, 0.0003 0.00013)"),
        ("minor", "paved", "LINESTRING(0.00087 0.0001, 0.00087 0.0009)"),
    ]
    def table(items):
        return " UNION ALL ".join(
            f"SELECT '{lvl}' AS level, {('NULL' if s is None else repr(s))}::VARCHAR AS surface, "
            f"ST_GeomFromText('{w}') AS geometry, "
            f"{{'xmin': ST_XMin(ST_GeomFromText('{w}')), 'xmax': ST_XMax(ST_GeomFromText('{w}')), "
            f"'ymin': ST_YMin(ST_GeomFromText('{w}')), 'ymax': ST_YMax(ST_GeomFromText('{w}'))}} AS bbox"
            for lvl, s, w in items
        )
    here, there = tmp_path / "here.parquet", tmp_path / "there.parquet"
    con.execute(f"COPY ({table(lines)}) TO '{here}' (FORMAT parquet)")
    con.execute(f"COPY ({table([('major', 'paved', 'LINESTRING(0.01 0.01, 0.02 0.02)')])}) TO '{there}' (FORMAT parquet)")
    bounds = (0.0, 0.0, 0.001, 0.001)
    source = tmp_path / "burn.fgb"
    assert sprout.write_burn_source(con, [str(here), str(there)], bounds, RECIPE, source) == 4  # the far road is left out
    burned = sprout.burn_source(source, bounds, RECIPE)
    codes = burned.GetRasterBand(1).ReadAsArray()
    assert (codes == sprout.burn(here, bounds, RECIPE)).all()
    recipe = {**RECIPE, "sprout": {**RECIPE["sprout"], "id": "roads-test"}}
    record = sprout.write_codes_cog(burned, recipe, tmp_path / "b.tif", tmp_path)
    level, surface = sprout.decode(codes)
    assert record["road_cells"] == int((level > 0).sum())
    assert record["level_3"] == int((level == 3).sum()) and record["paved"] == int(((level > 0) & (surface == 1)).sum())
    from osgeo import gdal
    written = gdal.Open(str(tmp_path / "b.tif")).ReadAsArray()
    assert (written[0] == level).all() and (written[1] == surface).all()
