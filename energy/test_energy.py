"""Energy rules, burn and tile settings on fixtures, without network.

Run: python -m pytest energy
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "roads"))
sys.path.append(str(HERE))

import rules  # noqa: E402

import importlib.util  # noqa: E402


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"energy_{name}", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sprout = load("sprout")
tiles = load("tiles")


@pytest.mark.parametrize(
    "tag, volts",
    [("400000", 400_000), ("225000;63000", 225_000), ("20000, 400", 20_000), ("", None), (None, None), ("high", None)],
)
def test_volts_takes_the_highest_number(tag, volts):
    assert rules.volts(tag) == volts


@pytest.mark.parametrize(
    "cls, tags, expected",
    [
        ("power_line", {"voltage": "765000"}, 4),
        ("power_line", {"voltage": "400000"}, 4),
        ("power_line", {"voltage": "225000;63000"}, 3),
        ("power_line", {"voltage": "132000"}, 2),
        ("minor_line", {"voltage": "33000"}, 1),
        ("power_line", {}, 2),
        ("minor_line", {}, 1),
        ("power_line", {"voltage": "400000", "location": "underground"}, None),
        ("cable", {"voltage": "63000"}, None),
        ("cable", {"voltage": "63000", "location": "overhead"}, 2),
        ("power_tower", {}, None),
    ],
)
def test_line_class(cls, tags, expected):
    assert rules.line_class(cls, tags) == expected


@pytest.mark.parametrize(
    "source, expected",
    [("solar", "solar"), ("biomass;gas", "fossil"), ("waste;gas", "fossil"), ("coal;biomass", "coal"),
     ("geothermal", "other"), (None, "other"), ("battery", "storage"), ("lignite", "coal")],
)
def test_group_takes_the_dirtiest_source(source, expected):
    assert rules.group(source) == expected


@pytest.mark.parametrize(
    "cls, tags, area, expected",
    [
        ("plant", {"plant:source": "solar"}, 6_000_000, "solar"),
        ("plant", {"plant:source": "oil"}, 0, "fossil"),
        ("generator", {"generator:source": "solar"}, 5_000, "solar"),
        ("generator", {"generator:source": "solar"}, 500, None),
        ("generator", {"generator:source": "solar"}, 0, None),
        ("generator", {"generator:source": "solar", "generator:location": "roof"}, 50_000, None),
        ("generator", {"generator:source": "wind"}, 0, "wind"),
        ("substation", {}, 5_000, "substation"),
        ("substation", {}, 40, None),
        ("substation", {}, 0, None),
        ("power_tower", {}, 0, None),
    ],
)
def test_site_group(cls, tags, area, expected):
    assert rules.site_group(cls, tags, area) == expected


def test_the_burn_values_put_dirtier_groups_higher():
    order = [rules.SITE_VALUES[g] for g in rules.GROUPS]
    assert order == sorted(order, reverse=True)
    assert rules.SITE_VALUES["substation"] < rules.SITE_VALUES["solar"]


@pytest.fixture()
def con():
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; SET geometry_always_xy = true")
    return con


LINES = [
    ("power_line", {"voltage": "765000"}), ("power_line", {"voltage": "225000;63000"}), ("power_line", {}),
    ("minor_line", {"voltage": "400"}), ("minor_line", {}), ("cable", {"voltage": "63000"}),
    ("cable", {"location": "overhead", "voltage": "90000"}), ("power_line", {"location": "underground"}),
]
SITES = [
    ("plant", {"plant:source": "solar"}, 6_000_000.0), ("plant", {"plant:source": "biomass;gas"}, 2_000.0),
    ("generator", {"generator:source": "solar"}, 5_000.0), ("generator", {"generator:source": "solar"}, 500.0),
    ("generator", {"generator:source": "wind"}, 0.0), ("generator", {"generator:source": "solar"}, 0.0),
    ("generator", {"generator:source": "solar", "location": "rooftop"}, 9_000.0),
    ("substation", {}, 5_000.0), ("substation", {}, 40.0), ("plant", {}, 3_000.0),
]


def tags_table(con, rows):
    con.execute("CREATE OR REPLACE TABLE t (i INTEGER, class VARCHAR, source_tags MAP(VARCHAR, VARCHAR), area_m2 DOUBLE)")
    for i, (cls, tags, *area) in enumerate(rows):
        con.execute("INSERT INTO t VALUES (?, ?, MAP(?::VARCHAR[], ?::VARCHAR[]), ?)", [i, cls, list(tags), list(tags.values()), area[0] if area else 0])


def test_the_line_sql_matches_python(con):
    tags_table(con, LINES)
    got = [v for (v,) in con.execute(f"SELECT {rules.line_class_sql()} FROM t ORDER BY i").fetchall()]
    assert got == [rules.line_class(c, t) for c, t in LINES]


def test_the_site_sql_matches_python(con):
    tags_table(con, SITES)
    got = [v for (v,) in con.execute(f"SELECT {rules.site_group_sql()} FROM t ORDER BY i").fetchall()]
    assert got == [rules.site_group(c, t, a) for c, t, a in SITES]


def test_the_burn_marks_line_cells_and_site_cells(con, tmp_path):
    pixel = sprout.PIXEL
    con.execute("CREATE TABLE seed (subtype VARCHAR, class VARCHAR, source_tags MAP(VARCHAR, VARCHAR), id VARCHAR, name VARCHAR, osm_id VARCHAR, geometry GEOMETRY)")
    west, north = 10.0, -20.0
    rows = [
        ("power_line", {"voltage": "132000"}, f"LINESTRING({west + 2.5 * pixel} {north - 0.5 * pixel}, {west + 2.5 * pixel} {north - 9.5 * pixel})"),
        ("power_line", {"voltage": "400000"}, f"LINESTRING({west + 0.5 * pixel} {north - 5.5 * pixel}, {west + 9.5 * pixel} {north - 5.5 * pixel})"),
        ("plant", {"plant:source": "solar"}, f"POLYGON(({west + 6 * pixel} {north - 6 * pixel}, {west + 9 * pixel} {north - 6 * pixel}, {west + 9 * pixel} {north - 9 * pixel}, {west + 6 * pixel} {north - 9 * pixel}, {west + 6 * pixel} {north - 6 * pixel}))"),
        ("generator", {"generator:source": "wind"}, f"POINT({west + 1.3 * pixel} {north - 1.7 * pixel})"),
    ]
    for i, (cls, tags, wkt) in enumerate(rows):
        con.execute("INSERT INTO seed VALUES ('power', ?, MAP(?::VARCHAR[], ?::VARCHAR[]), ?, NULL, NULL, ST_GeomFromText(?))",
                    [cls, list(tags), list(tags.values()), str(i), wkt])
    bounds = (west, north - 10 * pixel, west + 10 * pixel, north)

    lines_fgb = tmp_path / "lines.fgb"
    assert sprout.burn_file(con, sprout.lines_query("seed"), "voltage", lines_fgb, point_as_cell=False) == 2
    lines = sprout.burn(lines_fgb, bounds, all_touched=True)
    assert lines.shape == (10, 10)
    assert (lines[:, 2] >= 2).all()  # the 132 kV line down column 2
    assert (lines[5, :] == 4).all()  # the 400 kV line across row 5 wins where they cross
    assert lines[0, 0] == 0

    sites_fgb = tmp_path / "sites.fgb"
    # the polygon is 3 × 3 cells; ST_Area_Spheroid of that is far over the minimum generator area, and a plant is kept anyway
    assert sprout.burn_file(con, sprout.sites_query("seed"), "value", sites_fgb, point_as_cell=True) == 2
    sites = sprout.burn(sites_fgb, bounds, all_touched=False)
    assert (sites[6:9, 6:9] == rules.SITE_VALUES["solar"]).all()
    assert sites[1, 1] == rules.SITE_VALUES["wind"]
    assert int((sites > 0).sum()) == 10


def test_site_properties_carry_the_group_area_and_count():
    props = tiles.properties({"class": "plant", "site_group": "solar", "area_m2": 6449202.4, "name": "Mulilo", "operator": None})
    assert props == {"class": "plant", "group": "solar", "area_m2": 6449202, "name": "Mulilo", "area_solar": 6449202, "n_solar": 1}


def test_tippecanoe_clusters_and_sums_every_group(tmp_path):
    args = tiles.tippecanoe_args(tmp_path / "in.geojsons", tmp_path / "out.pmtiles")
    assert "--cluster-densest-as-needed" in args
    assert "--accumulate-attribute=area_m2:sum" in args
    for g in rules.SITE_VALUES:
        assert f"--accumulate-attribute=area_{g}:sum" in args and f"--accumulate-attribute=n_{g}:sum" in args
    assert args[args.index("-Z") + 1] == "2" and args[args.index("-z") + 1] == "14"
    assert tiles.SITE_POINT_MAX_ZOOM == 9 and (tiles.TOP_ZOOM, tiles.SHARE_FULL) == (11, 0.1)
