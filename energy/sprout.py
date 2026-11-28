#!/usr/bin/env python3
"""Burn the energy seed onto the sprout grid: `power-lines` and `power-sites`, one uint8 band each.

  python energy/sprout.py window karoo        data/energy/sprout/power-lines/karoo.tif and power-sites/karoo.tif
  python energy/sprout.py sites karoo         data/energy/sites/karoo.parquet: the kept sites with their group and attributes

power-lines  `voltage` 0 none, 1 distribution, 2 sub-transmission, 3 transmission, 4 extra-high.
             A cell is a line cell when a kept line touches it (all-touched, as roads). The highest class wins.
power-sites  `source` 0 none, then rules.SITE_VALUES. A polygon marks the cells whose centre it holds.
             A point marks the cell it falls in. The highest value, the dirtiest group, wins.

Features are burned in ascending value from one FlatGeobuf, so a later, higher value overwrites a cell.
There is no nodata.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import yaml
from osgeo import gdal

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "roads"))
sys.path.append(str(HERE))

import rules  # noqa: E402
import seed as roads_seed  # noqa: E402  roads/seed.py: connect, peak memory
import sprout as roads_sprout  # noqa: E402  roads/sprout.py: grid snapping, COG options

gdal.UseExceptions()

DATA = HERE.parent / "data" / "energy"
_lines_recipe = yaml.safe_load(rules.LINES_RECIPE.read_text())
_sites_recipe = yaml.safe_load(rules.SITES_RECIPE.read_text())
PIXEL = _lines_recipe["grid"]["pixel_size_deg"]
ORIGIN = tuple(_lines_recipe["grid"]["origin"])
RELEASE = str(_lines_recipe["source"]["release"])
LAYERS = {
    "power-lines": {"id": _lines_recipe["sprout"]["id"], "band": "voltage", "values": _lines_recipe["sprout"]["bands"]["voltage"]},
    "power-sites": {"id": _sites_recipe["sprout"]["id"], "band": "source", "values": _sites_recipe["sprout"]["bands"]["source"]},
}
CARD = rules.CARD  # the feature-card attributes kept on a site, from the seed


def seed_path(name: str) -> Path:
    return DATA / "seed" / f"{name}.parquet"


def lines_query(source: str) -> str:
    return f"""
        SELECT voltage, geometry FROM (
            SELECT {rules.line_class_sql()} AS voltage, geometry FROM {source}
            WHERE subtype = 'power' AND ST_GeometryType(geometry)::VARCHAR IN ('LINESTRING', 'MULTILINESTRING')
        ) WHERE voltage IS NOT NULL ORDER BY voltage
    """


def sites_query(source: str) -> str:
    """Kept sites with their group, value, area and card attributes, in burn order."""
    card = ", ".join(f"{sql} AS {key}" for key, sql in CARD.items())
    return f"""
        WITH s AS (
            SELECT id, class, {card}, geometry,
                   CASE WHEN ST_GeometryType(geometry)::VARCHAR LIKE '%POLYGON' THEN ST_Area_Spheroid(geometry) ELSE 0 END AS area_m2,
                   source_tags
            FROM {source}
            WHERE subtype = 'power' AND class IN ('plant', 'generator', 'substation')
        ), g AS (
            SELECT *, {rules.site_group_sql()} AS site_group FROM s
        )
        SELECT id, class, site_group, {rules.site_value_sql()} AS value, round(area_m2) AS area_m2,
               {', '.join(CARD)}, geometry
        FROM g WHERE site_group IS NOT NULL ORDER BY value, id
    """


def write_sites(name: str) -> dict:
    con = roads_seed.connect()
    dest = DATA / "sites" / f"{name}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({sites_query(f'read_parquet({chr(39)}{seed_path(name)}{chr(39)})')}) TO '{dest}' (FORMAT parquet, COMPRESSION zstd)")
    rows = con.execute(
        f"SELECT site_group, class, count(*), round(sum(area_m2) / 1e6, 3) FROM read_parquet('{dest}') GROUP BY ALL ORDER BY 1, 2"
    ).fetchall()
    return {"sites": dest, "groups": [{"group": g, "class": c, "features": n, "km2": a} for g, c, n, a in rows]}


def burn_file(con, query: str, value: str, dest: Path, point_as_cell: bool) -> int:
    """A FlatGeobuf of (value, geometry) in burn order. Points become one-cell squares for a centre burn."""
    geometry = (
        f"CASE WHEN ST_GeometryType(geometry)::VARCHAR = 'POINT' THEN ST_Envelope(ST_Buffer(geometry, {PIXEL / 2})) ELSE geometry END"
        if point_as_cell else "geometry"
    )
    if dest.exists():
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"COPY (SELECT {value} AS value, {geometry} AS geometry FROM ({query})) "
        f"TO '{dest}' (FORMAT GDAL, DRIVER 'FlatGeobuf', LAYER_CREATION_OPTIONS 'SPATIAL_INDEX=NO')"
    )
    if not dest.exists() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        return 0
    vector = gdal.OpenEx(str(dest), gdal.OF_VECTOR)
    count = vector.GetLayer(0).GetFeatureCount()
    vector = None
    return int(count)


def burn(source: Path, bounds, all_touched: bool) -> np.ndarray:
    west, north, cols, rows = roads_sprout.snapped(bounds, PIXEL, ORIGIN)
    target = gdal.GetDriverByName("MEM").Create("", cols, rows, 1, gdal.GDT_Byte)
    target.SetGeoTransform((west, PIXEL, 0, north, 0, -PIXEL))
    target.SetProjection("EPSG:4326")
    target.GetRasterBand(1).Fill(0)
    vector = gdal.OpenEx(str(source), gdal.OF_VECTOR)
    gdal.Rasterize(target, vector, options=gdal.RasterizeOptions(
        layers=[vector.GetLayer(0).GetName()], attribute="value", allTouched=all_touched))
    return target.GetRasterBand(1).ReadAsArray()


def write_cog(values: np.ndarray, bounds, layer: str, dest: Path) -> int:
    west, north, cols, rows = roads_sprout.snapped(bounds, PIXEL, ORIGIN)
    mem = gdal.GetDriverByName("MEM").Create("", cols, rows, 1, gdal.GDT_Byte)
    mem.SetGeoTransform((west, PIXEL, 0, north, 0, -PIXEL))
    mem.SetProjection("EPSG:4326")
    band = mem.GetRasterBand(1)
    band.WriteArray(values)
    band.SetDescription(LAYERS[layer]["band"])
    band.SetMetadataItem("values", json.dumps({str(k): v for k, v in LAYERS[layer]["values"].items()}))
    mem.SetMetadataItem("version", LAYERS[layer]["id"])
    mem.SetMetadataItem("source_release", RELEASE)
    dest.parent.mkdir(parents=True, exist_ok=True)
    gdal.Translate(str(dest), mem, format="COG", creationOptions=roads_sprout.COG_OPTIONS)
    return dest.stat().st_size


def command_window(name: str, bounds) -> list[dict]:
    con = roads_seed.connect()
    con.execute("SET geometry_always_xy = true")
    source = f"read_parquet('{seed_path(name)}')"
    records = []
    for layer, query, all_touched in (
        ("power-lines", lines_query(source), True),
        ("power-sites", sites_query(source), False),
    ):
        started = time.perf_counter()
        work = DATA / "work" / f"{layer}-{name}.fgb"
        features = burn_file(con, query, "voltage" if layer == "power-lines" else "value", work, point_as_cell=layer == "power-sites")
        values = burn(work, bounds, all_touched)
        dest = DATA / "sprout" / layer / f"{name}.tif"
        stored = write_cog(values, bounds, layer, dest)
        counts = np.bincount(values.ravel(), minlength=256)
        record = {
            "layer": layer, "window": name, "features": features, "bytes": stored, "cells": int(values.size),
            "marked_cells": int(counts[1:].sum()),
            "by_value": {LAYERS[layer]["values"].get(v, str(v)): int(counts[v]) for v in range(1, 256) if counts[v]},
            "wall_seconds": round(time.perf_counter() - started, 1),
            "peak_memory_bytes": roads_seed.peak_memory_bytes(),
        }
        print(json.dumps(record))
        records.append(record)
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("window", "sites"))
    parser.add_argument("name")
    args = parser.parse_args()
    boxes = {b["name"]: b for b in yaml.safe_load((HERE / "boxes.yaml").read_text())["boxes"]}
    if args.command == "window":
        command_window(args.name, tuple(boxes[args.name]["bounds"]))
    else:
        print(json.dumps(write_sites(args.name), default=str, indent=1))


if __name__ == "__main__":
    main()
