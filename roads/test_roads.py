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
