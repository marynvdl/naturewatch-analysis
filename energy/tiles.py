#!/usr/bin/env python3
"""The Energy tile sample: a raster archive for power lines, a vector archive for power sites.

  python energy/tiles.py lines karoo        data/energy/tiles/power-lines-karoo.pmtiles
  python energy/tiles.py sites karoo        data/energy/tiles/power-sites-karoo.pmtiles

power-lines  The roads design (roads/tiles.py) on the one-band sprout. Below TOP_ZOOM a palette PNG of
             SHARE_STEPS shades spaced in √(share / SHARE_FULL); at TOP_ZOOM red = voltage class × 4.
power-sites  Tippecanoe on the kept sites (energy/sprout.py sites), one layer `sites`. Each site is a point at
             a spot inside it up to SITE_POINT_MAX_ZOOM, for the client's heatmap, and its own shape after.
             Crowded points merge into one (`--cluster-densest-as-needed`) that keeps `point_count` and sums
             `area_m2` and, per source group g, `area_<g>` and `n_<g>`, so no site's weight is lost zoomed out.
             The kept attributes are the feature card (CARD).

Approved by the user on 27 November 2026, after the sample preview (energy/preview.py).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "roads"))
sys.path.insert(0, str(HERE.parent / "tiles"))
sys.path.append(str(HERE))

import pyramid  # noqa: E402
import rules  # noqa: E402
import seed as roads_seed  # noqa: E402
import tiles as roads_tiles  # noqa: E402

DATA = HERE.parent / "data" / "energy"
_line_tiles = yaml.safe_load(rules.LINES_RECIPE.read_text())["tiles"]
_site_tiles = yaml.safe_load(rules.SITES_RECIPE.read_text())["tiles"]
TOP_ZOOM = _line_tiles["max_zoom"]
SHARE_STEPS = _line_tiles["share_steps"]
SHARE_FULL = _line_tiles["share_full"]
SITE_MIN_ZOOM = _site_tiles["min_zoom"]
SITE_POINT_MAX_ZOOM = _site_tiles["point_max_zoom"]  # sites are points to here, shapes from the next zoom
SITE_MAX_ZOOM = _site_tiles["max_zoom"]
CLUSTER_DISTANCE = _site_tiles["cluster_distance"]  # pixels
CARD = ["class", "site_group", "area_m2", *rules.CARD]
GROUPS = list(rules.SITE_VALUES)


def bounds_of(name: str):
    return tuple({b["name"]: b for b in yaml.safe_load((HERE / "boxes.yaml").read_text())["boxes"]}[name]["bounds"])


def lines(name: str) -> dict:
    dest = DATA / "tiles" / f"power-lines-{name}.pmtiles"
    dest.parent.mkdir(parents=True, exist_ok=True)
    record = roads_tiles.build(
        DATA / "sprout" / "power-lines" / f"{name}.tif", bounds_of(name), TOP_ZOOM, SHARE_STEPS, dest, SHARE_FULL, name="power-lines"
    )
    print(json.dumps(record))
    return record


def properties(row: dict) -> dict:
    """The card attributes, and the per-group area and count that clusters sum."""
    props = {k: v for k, v in row.items() if v is not None}
    group = props.pop("site_group")
    props["group"] = group
    props["area_m2"] = int(props.get("area_m2", 0))
    props[f"area_{group}"] = props["area_m2"]
    props[f"n_{group}"] = 1
    return props


def site_features(name: str):
    """GeoJSON features with a Tippecanoe zoom range: a point to SITE_POINT_MAX_ZOOM, then the shape."""
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; SET geometry_always_xy = true")
    rows = con.execute(
        f"SELECT {', '.join(CARD)}, ST_AsGeoJSON(geometry), ST_AsGeoJSON(ST_PointOnSurface(geometry)), "
        f"ST_GeometryType(geometry)::VARCHAR FROM read_parquet('{DATA / 'sites' / (name + '.parquet')}')"
    ).fetchall()
    for row in rows:
        props = properties(dict(zip(CARD, row[: len(CARD)])))
        shape, point, kind = json.loads(row[-3]), json.loads(row[-2]), row[-1]
        if kind == "POINT":
            yield {"type": "Feature", "properties": props, "geometry": shape, "tippecanoe": {"minzoom": 0, "maxzoom": SITE_MAX_ZOOM}}
            continue
        yield {"type": "Feature", "properties": props, "geometry": point, "tippecanoe": {"minzoom": 0, "maxzoom": SITE_POINT_MAX_ZOOM}}
        yield {"type": "Feature", "properties": props, "geometry": shape,
               "tippecanoe": {"minzoom": SITE_POINT_MAX_ZOOM + 1, "maxzoom": SITE_MAX_ZOOM}}


def tippecanoe_args(source: Path, dest: Path) -> list[str]:
    accumulate = ["--accumulate-attribute=area_m2:sum"]
    for group in GROUPS:
        accumulate += [f"--accumulate-attribute=area_{group}:sum", f"--accumulate-attribute=n_{group}:sum"]
    return [
        "tippecanoe", "-o", str(dest), "--force", "-l", "sites", "-Z", str(SITE_MIN_ZOOM), "-z", str(SITE_MAX_ZOOM),
        "--cluster-densest-as-needed", f"--cluster-distance={CLUSTER_DISTANCE}", *accumulate,
        "--extend-zooms-if-still-dropping", "--detect-shared-borders", "--simplification=4", "-P", "--quiet", str(source),
    ]


def sites(name: str) -> dict:
    started = time.perf_counter()
    work = DATA / "work" / f"power-sites-{name}.geojsons"
    work.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with work.open("w") as handle:
        for feature in site_features(name):
            handle.write(json.dumps(feature) + "\n")
            count += 1
    dest = DATA / "tiles" / f"power-sites-{name}.pmtiles"
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(tippecanoe_args(work, dest), check=True)
    by_zoom: dict[int, dict] = {}
    for (z, _x, _y), data in pyramid.read_archive(dest):
        stat = by_zoom.setdefault(z, {"tiles": 0, "bytes": 0, "largest": 0})
        stat["tiles"] += 1
        stat["bytes"] += len(data)
        stat["largest"] = max(stat["largest"], len(data))
    record = {
        "archive": dest.name, "features_written": count, "bytes": dest.stat().st_size,
        "zooms": {str(z): by_zoom[z] for z in sorted(by_zoom)},
        "wall_seconds": round(time.perf_counter() - started, 1), "peak_memory_bytes": roads_seed.peak_memory_bytes(),
    }
    print(json.dumps(record))
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("lines", "sites"))
    parser.add_argument("name")
    args = parser.parse_args()
    (lines if args.command == "lines" else sites)(args.name)


if __name__ == "__main__":
    main()
