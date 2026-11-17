#!/usr/bin/env python3
"""Read Overture road segments in a box and write the roads seed as GeoParquet.

The recipe is the catalog. This script reads its `roads` rules and does not edit it.
Only the row groups whose bbox meets the box are read from Overture's public bucket.

  python roads/seed.py box waterberg                 one box from roads/boxes.yaml
  python roads/seed.py all --kind compare            every box of a kind
  python roads/seed.py summary                       class, level and surface counts and km per box
  python roads/seed.py footers                       segment rows per 10° block from Parquet footers only

Each seed row: Overture `id`, `class`, `subclass`, `surface` (paved, unpaved or null),
`level` (major, main, minor, track), `osm_ids` (OSM way ids), `name`, `bbox`, `geometry`.
"""
from __future__ import annotations

import argparse
import csv
import json
import resource
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).parent
DATA = ROOT / "data" / "roads"
BOXES = HERE / "boxes.yaml"
DEFAULT_RECIPE = ROOT / "cookbook" / "recipes" / "roads-2026-10-v1.yaml"
COMPARE_HALF = 0.15  # the 11 November 2026 comparison boxes were 0.3° on a side


def load_recipe(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def load_boxes() -> dict[str, dict]:
    return {b["name"]: b for b in yaml.safe_load(BOXES.read_text())["boxes"]}


def box_bounds(box: dict) -> tuple[float, float, float, float]:
    if "bounds" in box:
        return tuple(box["bounds"])
    return (box["lon"] - COMPARE_HALF, box["lat"] - COMPARE_HALF, box["lon"] + COMPARE_HALF, box["lat"] + COMPARE_HALF)


def quoted(values) -> str:
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)


def kept_classes(rules: dict) -> list[str]:
    classes = [c for level in rules["levels"].values() for c in level["classes"]]
    return classes + list(rules.get("by_surface", {}).get("classes", []))


def surface_fraction_sql(values: list[str]) -> str:
    """Share of the segment's length whose road_surface is one of values."""
    return (
        "coalesce(list_sum(list_transform("
        f"list_filter(coalesce(road_surface, []), lambda r: r.value IN ({quoted(values)})), "
        "lambda r: coalesce(r.\"between\"[2] - r.\"between\"[1], 1.0))), 0.0)"
    )


def surface_sql(rules: dict) -> str:
    """paved, unpaved or NULL: the value that covers most of the length. Paved wins a tie, then unpaved."""
    p = surface_fraction_sql(rules["surface"]["paved"])
    u = surface_fraction_sql(rules["surface"]["unpaved"])
    return (
        f"CASE WHEN {p} > 0 AND {p} >= {u} AND {p} >= 1 - {p} - {u} THEN 'paved' "
        f"WHEN {u} > 0 AND {u} >= 1 - {p} - {u} THEN 'unpaved' END"
    )


def level_sql(rules: dict, surface: str = "surface") -> str:
    parts = [f"WHEN class IN ({quoted(level['classes'])}) THEN '{name}'" for name, level in rules["levels"].items()]
    by_surface = rules.get("by_surface")
    if by_surface:
        classes = quoted(by_surface["classes"])
        parts.append(f"WHEN class IN ({classes}) AND {surface} = 'paved' THEN '{by_surface['paved']}'")
        parts.append(f"WHEN class IN ({classes}) THEN '{by_surface['otherwise']}'")
    return "CASE " + " ".join(parts) + " END"


OSM_IDS_SQL = (
    "list_distinct(list_transform("
    "list_filter(coalesce(sources, []), lambda s: s.dataset = 'OpenStreetMap' AND s.record_id LIKE 'w%'), "
    "lambda s: CAST(split_part(substr(s.record_id, 2), '@', 1) AS BIGINT)))"
)


def seed_query(source: str, rules: dict, bounds: tuple[float, float, float, float] | None) -> str:
    """The seed rows from `source`, a read_parquet() call or a table name."""
    where = [f"subtype = '{rules['subtype']}'", f"class IN ({quoted(kept_classes(rules))})"]
    if bounds is not None:
        west, south, east, north = bounds
        where.append(f"bbox.xmin <= {east} AND bbox.xmax >= {west} AND bbox.ymin <= {north} AND bbox.ymax >= {south}")
    return f"""
        WITH s AS (
            SELECT id, class, subclass, {surface_sql(rules)} AS surface, {OSM_IDS_SQL} AS osm_ids,
                   names.primary AS name, bbox, geometry
            FROM {source}
            WHERE {' AND '.join(where)}
        )
        SELECT id, class, subclass, surface, {level_sql(rules)} AS level, osm_ids, name, bbox, geometry FROM s
    """


def connect():
    import duckdb

    con = duckdb.connect()
    for ext in ("httpfs", "spatial"):
        con.execute(f"INSTALL {ext}; LOAD {ext};")
    con.execute("SET geometry_always_xy = true")  # longitude, latitude, as Overture stores them
    return con


def overture_source(recipe: dict) -> str:
    return f"read_parquet('{recipe['source']['path']}')"


def seed_path(name: str) -> Path:
    return DATA / "seed" / f"{name}.parquet"


def write_box(recipe: dict, name: str, box: dict, force: bool = False) -> dict:
    dest = seed_path(name)
    if dest.exists() and not force:
        return {"box": name, "skipped": True}
    dest.parent.mkdir(parents=True, exist_ok=True)
    con = connect()
    con.execute(f"SET s3_region='{recipe['source']['s3_region']}'")
    started = time.perf_counter()
    query = seed_query(overture_source(recipe), recipe["roads"], box_bounds(box))
    con.execute(f"COPY ({query} ORDER BY level, id) TO '{dest}' (FORMAT parquet, COMPRESSION zstd)")
    rows = con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]
    record = {
        "box": name,
        "rows": rows,
        "bytes": dest.stat().st_size,
        "wall_seconds": round(time.perf_counter() - started, 1),
        "peak_memory_bytes": peak_memory_bytes(),
    }
    print(json.dumps(record))
    return record


def summary(names: list[str]) -> list[dict]:
    """Segments and km by class, level and surface, per box, clipped to the box."""
    con = connect()
    boxes = load_boxes()
    rows = []
    for name in names:
        path = seed_path(name)
        if not path.exists():
            continue
        west, south, east, north = box_bounds(boxes[name])
        result = con.execute(
            f"""
            SELECT class, level, coalesce(surface, 'unknown') AS surface, count(*) AS segments,
                   sum(ST_Length_Spheroid(
                       ST_Intersection(geometry, ST_MakeEnvelope({west}, {south}, {east}, {north})))) / 1000 AS km
            FROM read_parquet('{path}')
            GROUP BY ALL ORDER BY level, class, surface
            """
        ).fetchall()
        for cls, level, surface, segments, km in result:
            rows.append({"box": name, "class": cls, "level": level, "surface": surface, "segments": segments, "km": round(km or 0, 2)})
    return rows


def footers(recipe: dict, dest: Path) -> dict:
    """Rows per 10° block from the Parquet footers. Each row group counts in the block of its bbox centre.

    Counts every transportation segment, roads and others, because a footer has no class counts.
    """
    con = connect()
    con.execute(f"SET s3_region='{recipe['source']['s3_region']}'")
    started = time.perf_counter()
    path = recipe["source"]["path"]
    stats = con.execute(
        f"""
        SELECT file_name, row_group_id, any_value(row_group_num_rows) AS rows,
               min(CASE WHEN path_in_schema = 'bbox, xmin' THEN TRY_CAST(stats_min_value AS DOUBLE) END) AS xmin,
               max(CASE WHEN path_in_schema = 'bbox, xmax' THEN TRY_CAST(stats_max_value AS DOUBLE) END) AS xmax,
               min(CASE WHEN path_in_schema = 'bbox, ymin' THEN TRY_CAST(stats_min_value AS DOUBLE) END) AS ymin,
               max(CASE WHEN path_in_schema = 'bbox, ymax' THEN TRY_CAST(stats_max_value AS DOUBLE) END) AS ymax,
               sum(total_compressed_size) AS compressed_bytes
        FROM parquet_metadata('{path}')
        GROUP BY file_name, row_group_id
        """
    ).fetchall()
    blocks: dict[str, dict] = {}
    total_rows = 0
    total_bytes = 0
    for _file, _rg, rows, xmin, xmax, ymin, ymax, compressed in stats:
        total_rows += rows
        total_bytes += compressed
        if None in (xmin, xmax, ymin, ymax):
            key = "unknown"
        else:
            lon = (xmin + xmax) / 2
            lat = (ymin + ymax) / 2
            key = f"{int((lat + 90) // 10) * 10 - 90}_{int((lon + 180) // 10) * 10 - 180}"
        block = blocks.setdefault(key, {"rows": 0, "row_groups": 0, "compressed_bytes": 0})
        block["rows"] += rows
        block["row_groups"] += 1
        block["compressed_bytes"] += compressed
    record = {
        "files": len({row[0] for row in stats}),
        "row_groups": len(stats),
        "rows": total_rows,
        "compressed_bytes": total_bytes,
        "wall_seconds": round(time.perf_counter() - started, 1),
        "blocks": dict(sorted(blocks.items())),
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(record, indent=1))
    return record


def peak_memory_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("box", "all", "summary", "footers"))
    parser.add_argument("name", nargs="?")
    parser.add_argument("--kind", choices=("compare", "window"))
    parser.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    recipe = load_recipe(args.recipe)
    boxes = load_boxes()
    if args.command == "box":
        write_box(recipe, args.name, boxes[args.name], args.force)
    elif args.command == "all":
        for name, box in boxes.items():
            if args.kind is None or box["kind"] == args.kind:
                write_box(recipe, name, box, args.force)
    elif args.command == "summary":
        names = [n for n, b in boxes.items() if args.kind is None or b["kind"] == args.kind]
        rows = summary(names)
        dest = DATA / "levels.csv"
        with dest.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["box", "class", "level", "surface", "segments", "km"])
            writer.writeheader()
            writer.writerows(rows)
        print(dest)
    else:
        record = footers(recipe, DATA / "footers.json")
        print(json.dumps({k: v for k, v in record.items() if k != "blocks"}))


if __name__ == "__main__":
    main()
