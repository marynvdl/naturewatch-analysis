#!/usr/bin/env python3
"""Read Overture base/infrastructure in a box, and count what it holds.

  python energy/seed.py box karoo           every infrastructure row in a box: data/energy/raw/<name>.parquet
  python energy/seed.py all                 every box in energy/boxes.yaml
  python energy/seed.py seed                the window seeds from the raw reads: data/energy/seed/<name>.parquet
  python energy/seed.py schema              rows by subtype, class and geometry type, and source_tags keys, per box
  python energy/seed.py footers             infrastructure rows per 10° block from the Parquet footers only

Only the row groups whose bbox meets the box are read from Overture's public bucket.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "roads"))

import seed as roads_seed  # noqa: E402

DATA = ROOT / "data" / "energy"
BOXES = HERE / "boxes.yaml"
RELEASE = "2026-10-21.0"
SOURCE = {
    "release": RELEASE,
    "path": f"s3://overturemaps-us-west-2/release/{RELEASE}/theme=base/type=infrastructure/*.parquet",
    "s3_region": "us-west-2",
}


def load_boxes() -> dict[str, dict]:
    import yaml

    return {b["name"]: b for b in yaml.safe_load(BOXES.read_text())["boxes"]}


def raw_path(name: str) -> Path:
    return DATA / "raw" / f"{name}.parquet"


def bbox_where(bounds) -> str:
    west, south, east, north = bounds
    return f"bbox.xmin <= {east} AND bbox.xmax >= {west} AND bbox.ymin <= {north} AND bbox.ymax >= {south}"


# A seed row: every infrastructure subtype, with the columns a later layer may need. The OpenStreetMap id is
# the first source record without its version, such as n2309093640 or w104203629.
SEED_SELECT = """
    SELECT id, subtype, class, names.primary AS name,
           split_part(list_filter(coalesce(sources, []), lambda s: s.dataset = 'OpenStreetMap')[1].record_id, '@', 1) AS osm_id,
           wikidata, source_tags, height, surface, level, bbox, geometry
"""


def seed_path(name: str) -> Path:
    return DATA / "seed" / f"{name}.parquet"


def write_seed(name: str) -> dict:
    """The window seed, from the raw read. The global seed runs the same SELECT on Overture."""
    import duckdb

    dest = seed_path(name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    started = time.perf_counter()
    con.execute(
        f"COPY ({SEED_SELECT} FROM read_parquet('{raw_path(name)}') ORDER BY subtype, class, id) "
        f"TO '{dest}' (FORMAT parquet, COMPRESSION zstd)"
    )
    record = {"box": name, "rows": con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0],
              "bytes": dest.stat().st_size, "raw_bytes": raw_path(name).stat().st_size,
              "wall_seconds": round(time.perf_counter() - started, 1)}
    by = con.execute(
        f"SELECT subtype = 'power', sum(1) FROM read_parquet('{dest}') GROUP BY 1"
    ).fetchall()
    record["power_rows"] = int(sum(n for p, n in by if p))
    print(json.dumps(record))
    return record


def connect():
    con = roads_seed.connect()
    con.execute(f"SET s3_region='{SOURCE['s3_region']}'")
    return con


def write_box(name: str, box: dict, force: bool = False) -> dict:
    dest = raw_path(name)
    if dest.exists() and not force:
        return {"box": name, "skipped": True}
    dest.parent.mkdir(parents=True, exist_ok=True)
    con = connect()
    started = time.perf_counter()
    con.execute(
        f"COPY (SELECT * FROM read_parquet('{SOURCE['path']}') WHERE {bbox_where(box['bounds'])} ORDER BY subtype, class, id) "
        f"TO '{dest}' (FORMAT parquet, COMPRESSION zstd)"
    )
    rows = con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]
    record = {
        "box": name,
        "rows": rows,
        "bytes": dest.stat().st_size,
        "wall_seconds": round(time.perf_counter() - started, 1),
        "peak_memory_bytes": roads_seed.peak_memory_bytes(),
    }
    print(json.dumps(record))
    return record


def schema(names: list[str]) -> None:
    """data/energy/schema-classes.csv and data/energy/schema-tags.csv."""
    con = roads_seed.connect()
    classes, tags = [], []
    for name in names:
        path = raw_path(name)
        if not path.exists():
            continue
        src = f"read_parquet('{path}')"
        for row in con.execute(
            f"SELECT subtype, class, ST_GeometryType(geometry) AS geom, count(*) FROM {src} GROUP BY ALL ORDER BY 1, 2, 3"
        ).fetchall():
            classes.append({"box": name, "subtype": row[0], "class": row[1], "geometry": str(row[2]), "rows": row[3]})
        for row in con.execute(
            f"""
            SELECT subtype, k, count(*) AS filled, any_value(total) AS rows FROM (
                SELECT subtype, unnest(map_keys(source_tags)) AS k, count(*) OVER (PARTITION BY subtype) AS total
                FROM {src} WHERE source_tags IS NOT NULL
            ) GROUP BY ALL ORDER BY subtype, filled DESC
            """
        ).fetchall():
            tags.append({"box": name, "subtype": row[0], "key": row[1], "filled": row[2], "rows_with_tags": row[3]})
    for fname, rows in (("schema-classes.csv", classes), ("schema-tags.csv", tags)):
        out = DATA / fname
        with out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"{out}: {len(rows)} rows")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("box")
    b.add_argument("name")
    b.add_argument("--force", action="store_true")
    a = sub.add_parser("all")
    a.add_argument("--force", action="store_true")
    sub.add_parser("seed")
    sub.add_parser("schema")
    sub.add_parser("footers")
    args = p.parse_args()
    boxes = load_boxes()
    if args.cmd == "box":
        write_box(args.name, boxes[args.name], args.force)
    elif args.cmd == "all":
        for name, box in boxes.items():
            write_box(name, box, args.force)
    elif args.cmd == "seed":
        for name in boxes:
            write_seed(name)
    elif args.cmd == "schema":
        schema(list(boxes))
    elif args.cmd == "footers":
        print(json.dumps(roads_seed.footers({"source": SOURCE}, DATA / "footers.json"))[:2000])


if __name__ == "__main__":
    main()
