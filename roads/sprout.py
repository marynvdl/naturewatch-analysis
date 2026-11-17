#!/usr/bin/env python3
"""Burn the roads seed onto the sprout grid: two uint8 bands, `level` and `surface`.

A cell is a road cell when a road line touches it (GDAL ALL_TOUCHED). When several
roads touch a cell, the cell takes the highest level, and the surface of a road of
that level: paved over unpaved over unknown. Roads are burned in that order, so a
later burn overwrites an earlier one.

  python roads/sprout.py window chinko                       data/roads/sprout/chinko.tif
  python roads/sprout.py window chinko --centre               line-centre burn instead, for the comparison
  python roads/sprout.py compare chinko                       road cells all-touched against line-centre

There is no nodata. 0 in `level` is a cell no mapped road touches.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
from osgeo import gdal, ogr, osr

import seed as seedmod

gdal.UseExceptions()
ogr.UseExceptions()

SURFACE_RANK = {None: 0, "unpaved": 1, "paved": 2}  # burn order inside a level
SURFACE_VALUE = {0: 0, 1: 2, 2: 1}  # rank -> surface band value: 0 unknown, 1 paved, 2 unpaved
COG_OPTIONS = ["COMPRESS=DEFLATE", "LEVEL=6", "BLOCKSIZE=512", "OVERVIEWS=NONE", "BIGTIFF=IF_SAFER", "NUM_THREADS=ALL_CPUS"]


def code(level_value: int, rank: int) -> int:
    """Burn code 1–12, ordered by level, then surface rank."""
    return (level_value - 1) * 3 + rank + 1


def decode(codes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """level 0–4 and surface 0–2 from burn codes."""
    codes = codes.astype(np.int16)
    road = codes > 0
    level = np.where(road, (codes - 1) // 3 + 1, 0).astype(np.uint8)
    rank = np.where(road, (codes - 1) % 3, 0)
    surface = np.select([rank == 1, rank == 2], [SURFACE_VALUE[1], SURFACE_VALUE[2]], 0).astype(np.uint8)
    surface[~road] = 0
    return level, surface


def snapped(bounds: tuple[float, float, float, float], pixel: float, origin=(-180.0, 90.0)) -> tuple[float, float, int, int]:
    """West, north, columns, rows of the grid window that holds bounds."""
    west, south, east, north = bounds
    col0 = int(np.floor((west - origin[0]) / pixel + 1e-9))
    col1 = int(np.ceil((east - origin[0]) / pixel - 1e-9))
    row0 = int(np.floor((origin[1] - north) / pixel + 1e-9))
    row1 = int(np.ceil((origin[1] - south) / pixel - 1e-9))
    return round(origin[0] + col0 * pixel, 9), round(origin[1] - row0 * pixel, 9), col1 - col0, row1 - row0


def burn(seed_path: Path, bounds, recipe: dict, all_touched: bool = True) -> np.ndarray:
    """Burn codes for the window, as an array of rows × columns."""
    pixel = recipe["grid"]["pixel_size_deg"]
    west, north, cols, rows = snapped(bounds, pixel, tuple(recipe["grid"]["origin"]))
    target = gdal.GetDriverByName("MEM").Create("", cols, rows, 1, gdal.GDT_Byte)
    target.SetGeoTransform((west, pixel, 0, north, 0, -pixel))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    target.SetProjection(srs.ExportToWkt())
    target.GetRasterBand(1).Fill(0)
    source = gdal.OpenEx(str(seed_path), gdal.OF_VECTOR)
    layer = source.GetLayer(0).GetName()
    levels = sorted(recipe["roads"]["levels"].items(), key=lambda item: item[1]["value"])
    for name, level in levels:
        for surface, rank in sorted(SURFACE_RANK.items(), key=lambda item: item[1]):
            test = "surface IS NULL" if surface is None else f"surface = '{surface}'"
            options = gdal.RasterizeOptions(
                layers=[layer], where=f"level = '{name}' AND {test}", burnValues=[code(level["value"], rank)], allTouched=all_touched
            )
            gdal.Rasterize(target, source, options=options)
    values = target.GetRasterBand(1).ReadAsArray()
    target = None
    return values


def write_sprout(codes: np.ndarray, bounds, recipe: dict, dest: Path) -> dict:
    pixel = recipe["grid"]["pixel_size_deg"]
    west, north, cols, rows = snapped(bounds, pixel, tuple(recipe["grid"]["origin"]))
    level, surface = decode(codes)
    mem = gdal.GetDriverByName("MEM").Create("", cols, rows, 2, gdal.GDT_Byte)
    mem.SetGeoTransform((west, pixel, 0, north, 0, -pixel))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    mem.SetProjection(srs.ExportToWkt())
    bands = recipe["sprout"]["bands"]
    for index, (name, array) in enumerate((("level", level), ("surface", surface)), start=1):
        band = mem.GetRasterBand(index)
        band.WriteArray(array)
        band.SetDescription(name)
        band.SetMetadataItem("values", json.dumps({str(k): v for k, v in bands[name].items()}))
    mem.SetMetadataItem("version", recipe["sprout"]["id"])
    mem.SetMetadataItem("source_release", str(recipe["source"]["release"]))
    dest.parent.mkdir(parents=True, exist_ok=True)
    gdal.Translate(str(dest), mem, format="COG", creationOptions=COG_OPTIONS)
    mem = None
    return {"bytes": dest.stat().st_size, "columns": cols, "rows": rows}


LEVEL_LUT = np.zeros(256, np.uint8)
SURFACE_LUT = np.zeros(256, np.uint8)
for _code in range(1, 13):
    LEVEL_LUT[_code] = (_code - 1) // 3 + 1
    SURFACE_LUT[_code] = SURFACE_VALUE[(_code - 1) % 3]
STRIP_ROWS = 2048


def code_sql(recipe: dict) -> str:
    """The burn code of a seed row, as in code(): level value, then surface rank."""
    parts = []
    for level_name, level in recipe["roads"]["levels"].items():
        for surface, rank in SURFACE_RANK.items():
            test = "surface IS NULL" if surface is None else f"surface = '{surface}'"
            parts.append(f"WHEN level = '{level_name}' AND {test} THEN {code(level['value'], rank)}")
    return "CASE " + " ".join(parts) + " END"


def write_burn_source(con, seed_paths: list[str], bounds, recipe: dict, dest: Path) -> int:
    """One FlatGeobuf of the roads that reach into bounds, sorted by burn code.

    GDAL burns features in file order, so a later, higher code wins a cell. There is no
    spatial index, which would reorder the features. The bbox test is padded by one cell.
    """
    pad = recipe["grid"]["pixel_size_deg"]
    west, south, east, north = bounds
    files = ", ".join(f"'{p}'" for p in seed_paths)
    where = (
        f"bbox.xmin <= {east + pad} AND bbox.xmax >= {west - pad} "
        f"AND bbox.ymin <= {north + pad} AND bbox.ymax >= {south - pad}"
    )
    query = f"SELECT {code_sql(recipe)} AS code, geometry FROM read_parquet([{files}]) WHERE {where} ORDER BY code"
    if dest.exists():
        dest.unlink()
    con.execute("SET geometry_always_xy = true")
    con.execute(
        f"COPY ({query}) TO '{dest}' (FORMAT GDAL, DRIVER 'FlatGeobuf', LAYER_CREATION_OPTIONS 'SPATIAL_INDEX=NO')"
    )
    return int(con.execute(f"SELECT count(*) FROM ({query})").fetchone()[0])


def burn_source(source: Path, bounds, recipe: dict, all_touched: bool = True):
    """A MEM dataset of burn codes for bounds, burned from a write_burn_source file in one pass."""
    pixel = recipe["grid"]["pixel_size_deg"]
    west, north, cols, rows = snapped(bounds, pixel, tuple(recipe["grid"]["origin"]))
    target = gdal.GetDriverByName("MEM").Create("", cols, rows, 1, gdal.GDT_Byte)
    target.SetGeoTransform((west, pixel, 0, north, 0, -pixel))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    target.SetProjection(srs.ExportToWkt())
    target.GetRasterBand(1).Fill(0)
    vector = gdal.OpenEx(str(source), gdal.OF_VECTOR)
    options = gdal.RasterizeOptions(layers=[vector.GetLayer(0).GetName()], attribute="code", allTouched=all_touched)
    gdal.Rasterize(target, vector, options=options)
    vector = None
    return target


def write_codes_cog(codes_ds, recipe: dict, dest: Path, work: Path) -> dict:
    """Decode a burn-code dataset into the two-band sprout COG, a strip at a time.

    Returns counts as counts() does. No file is written when there is no road cell.
    """
    cols, rows = codes_ds.RasterXSize, codes_ds.RasterYSize
    histogram = np.zeros(256, np.int64)
    source = codes_ds.GetRasterBand(1)
    for top in range(0, rows, STRIP_ROWS):
        height = min(STRIP_ROWS, rows - top)
        histogram += np.bincount(source.ReadAsArray(0, top, cols, height).ravel(), minlength=256)
    out = {"cells": int(cols) * int(rows), "road_cells": int(histogram[1:].sum())}
    for value in range(1, 5):
        out[f"level_{value}"] = int(histogram[1:][LEVEL_LUT[1:] == value].sum())
    for value, label in ((1, "paved"), (2, "unpaved")):
        out[label] = int(histogram[1:][SURFACE_LUT[1:] == value].sum())
    out["bytes"] = 0
    if out["road_cells"] == 0:
        return out
    work.mkdir(parents=True, exist_ok=True)
    plain = work / f"{dest.stem}-plain.tif"
    staged = gdal.GetDriverByName("GTiff").Create(
        str(plain), cols, rows, 2, gdal.GDT_Byte, ["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512", "BIGTIFF=YES"]
    )
    staged.SetGeoTransform(codes_ds.GetGeoTransform())
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    staged.SetProjection(srs.ExportToWkt())
    bands = recipe["sprout"]["bands"]
    for index, (label, lut) in enumerate((("level", LEVEL_LUT), ("surface", SURFACE_LUT)), start=1):
        band = staged.GetRasterBand(index)
        band.SetDescription(label)
        band.SetMetadataItem("values", json.dumps({str(k): v for k, v in bands[label].items()}))
        for top in range(0, rows, STRIP_ROWS):
            height = min(STRIP_ROWS, rows - top)
            band.WriteArray(lut[source.ReadAsArray(0, top, cols, height)], 0, top)
    staged.SetMetadataItem("version", recipe["sprout"]["id"])
    staged.SetMetadataItem("source_release", str(recipe["source"]["release"]))
    staged = None
    dest.parent.mkdir(parents=True, exist_ok=True)
    gdal.Translate(str(dest), str(plain), format="COG", creationOptions=COG_OPTIONS)
    plain.unlink()
    out["bytes"] = dest.stat().st_size
    return out


def counts(codes: np.ndarray) -> dict:
    level, surface = decode(codes)
    out = {"cells": int(codes.size), "road_cells": int((level > 0).sum())}
    for value in range(1, 5):
        out[f"level_{value}"] = int((level == value).sum())
    for value, name in ((1, "paved"), (2, "unpaved")):
        out[name] = int(((level > 0) & (surface == value)).sum())
    return out


def window_bounds(name: str):
    return seedmod.box_bounds(seedmod.load_boxes()[name])


def command_window(recipe: dict, name: str, centre: bool) -> dict:
    started = time.perf_counter()
    bounds = window_bounds(name)
    codes = burn(seedmod.seed_path(name), bounds, recipe, all_touched=not centre)
    burned = time.perf_counter() - started
    suffix = "-centre" if centre else ""
    dest = seedmod.DATA / "sprout" / f"{name}{suffix}.tif"
    record = {"window": name, "all_touched": not centre, **write_sprout(codes, bounds, recipe, dest), **counts(codes)}
    record["burn_seconds"] = round(burned, 1)
    record["wall_seconds"] = round(time.perf_counter() - started, 1)
    record["peak_memory_bytes"] = seedmod.peak_memory_bytes()
    print(json.dumps(record))
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("window", "compare"))
    parser.add_argument("name")
    parser.add_argument("--centre", action="store_true")
    parser.add_argument("--recipe", type=Path, default=seedmod.DEFAULT_RECIPE)
    args = parser.parse_args()
    recipe = seedmod.load_recipe(args.recipe)
    if args.command == "window":
        command_window(recipe, args.name, args.centre)
    else:
        bounds = window_bounds(args.name)
        touched = counts(burn(seedmod.seed_path(args.name), bounds, recipe, True))
        centre = counts(burn(seedmod.seed_path(args.name), bounds, recipe, False))
        ratio = {k: round(touched[k] / centre[k], 3) for k in touched if centre[k]}
        print(json.dumps({"window": args.name, "all_touched": touched, "centre": centre, "ratio": ratio}))


if __name__ == "__main__":
    main()
