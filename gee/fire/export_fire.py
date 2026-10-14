#!/usr/bin/env python3
"""Export annual fire counts from NASA FIRMS (Google Earth Engine) to Cloud Storage.

This is the Python version of the fire_exportFirms notebook. For one year it
counts, per pixel, in how many 14-day windows a fire was detected, and exports
the result as one cloud-optimised GeoTIFF per block of blocks/africa_blocks.geojson
to gs://<bucket>/COGS/fire/<year>/fire<year>_<n>.tif. The tile workflow
(tasks/fireTiles) reads those files.

Subcommands
  plan    Show what an export would do. No network, no credentials.
  check   Log in to Earth Engine and check the FIRMS data covers the year.
  export  Start one Earth Engine export task per block. Returns straight away.
  status  Show the state of the export tasks for a year.

Examples
  python export_fire.py plan   --year 2025
  python export_fire.py check  --year 2025
  python export_fire.py export --year 2025 --dry-run
  python export_fire.py export --year 2025
  python export_fire.py export --year 2025 --blocks 3,7-9      # re-run some blocks
  python export_fire.py status --year 2025

Credentials: set GOOGLE_APPLICATION_CREDENTIALS to a service account key file
(that is what the GitHub workflow does), or log in once with
`earthengine authenticate`.

The calculation matches the notebook on purpose, so a new year is comparable
with 2016-2024. Two quirks are kept and documented in README.md: a year has 27
windows (not 26), and the last window runs into January of the next year.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BLOCKS = HERE.parent / "blocks" / "africa_blocks.geojson"

# Where the COGs go. The tile workflow reads the same location.
BUCKET = "nature-watch-bucket"
PREFIX = "COGS/fire"

# The calculation (same values as the notebook)
FIRMS_COLLECTION = "FIRMS"
FIRMS_BAND = "T21"
WINDOW_DAYS = 14
AGGREGATE_CRS = "EPSG:3857"
AGGREGATE_SCALE_M = 1000

# The export (same values as the notebook)
EXPORT_CRS = "EPSG:4326"
EXPORT_SCALE_M = 30
MAX_PIXELS = 3_984_245_980


@dataclass(frozen=True)
class Block:
    """One export region."""

    id: int  # 1-based, as in blocks.geojson
    feature: dict  # the GeoJSON feature

    @property
    def file_index(self) -> int:
        """The number in the file name. 0-based, as the notebook wrote them."""
        return self.id - 1


# --------------------------------------------------------------------------
# Pure helpers: no Earth Engine, no network. These are what the tests cover.
# --------------------------------------------------------------------------


def year_windows(year: int) -> list[date]:
    """Start dates of the 14-day windows: 1 January, then every 14 days up to 31 December.

    Gives 27 windows in every year. The 27th starts on 30 or 31 December and,
    like every window, runs 14 days, so it reaches into the next January.
    """
    first, last = date(year, 1, 1), date(year, 12, 31)
    starts, day = [], first
    while day <= last:
        starts.append(day)
        day += timedelta(days=WINDOW_DAYS)
    return starts


def last_window_end(year: int) -> date:
    """The first day that is no longer part of the last window (end is exclusive)."""
    return year_windows(year)[-1] + timedelta(days=WINDOW_DAYS)


def load_blocks(path: Path = DEFAULT_BLOCKS) -> list[Block]:
    """Read the blocks file. Fails on duplicate or missing ids."""
    data = json.loads(Path(path).read_text())
    blocks = []
    for feature in data["features"]:
        block_id = feature.get("properties", {}).get("id")
        if not isinstance(block_id, int) or block_id < 1:
            raise ValueError(f"every block needs an integer id >= 1, got {block_id!r}")
        blocks.append(Block(id=block_id, feature=feature))
    ids = [b.id for b in blocks]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate block ids in " + str(path))
    return sorted(blocks, key=lambda b: b.id)


def parse_block_selection(text: str | None, blocks: list[Block]) -> list[Block]:
    """Turn '3,7-9' into those blocks. None means all of them."""
    if not text:
        return blocks
    wanted: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, high = part.split("-", 1)
            wanted.update(range(int(low), int(high) + 1))
        else:
            wanted.add(int(part))
    known = {b.id for b in blocks}
    missing = sorted(wanted - known)
    if missing:
        raise ValueError(f"no such block id(s): {missing}. Known ids: {min(known)}-{max(known)}")
    return [b for b in blocks if b.id in wanted]


def output_name(year: int, block: Block) -> str:
    """File and task name, e.g. fire2025_0. The tile workflow reads *.tif so the name is free,
    but this keeps the names of 2016-2024."""
    return f"fire{year}_{block.file_index}"


def output_prefix(year: int, block: Block, prefix: str = PREFIX) -> str:
    return f"{prefix}/{year}/{output_name(year, block)}"


def export_settings(year: int, block: Block, bucket: str = BUCKET, prefix: str = PREFIX) -> dict:
    """The Export.image.toCloudStorage arguments except the image and the region."""
    name = output_name(year, block)
    return {
        "description": name,
        "bucket": bucket,
        "fileNamePrefix": output_prefix(year, block, prefix),
        "crs": EXPORT_CRS,
        "scale": EXPORT_SCALE_M,
        "maxPixels": MAX_PIXELS,
        "fileFormat": "GeoTIFF",
        "formatOptions": {"cloudOptimized": True},
    }


# --------------------------------------------------------------------------
# Earth Engine and Cloud Storage. Needs credentials.
# --------------------------------------------------------------------------


def init_earth_engine(project: str | None = None) -> None:
    import ee

    key_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if key_file and Path(key_file).is_file():
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            key_file, scopes=ee.oauth.SCOPES
        )
        ee.Initialize(credentials, project=project or credentials.project_id)
    else:
        ee.Initialize(project=project)


def block_region(block: Block):
    """The export region of a block, with straight (planar) edges.

    Earth Engine draws the edges of a polygon from GeoJSON as great-circle arcs
    unless told otherwise. On a 15 degree wide block those arcs bulge towards the
    pole, which made the exported file about 0.24 degrees (26 km) taller than the
    block. The notebook's exports had straight edges, so do the same.
    """
    import ee

    return ee.Geometry(block.feature["geometry"], "EPSG:4326", False)


def build_fire_image(year: int):
    """Burn count per pixel for the year, as a byte image on a 1 km Web Mercator grid.

    For each 14-day window: did any FIRMS detection fall in the window (1/0)?
    Sum the windows. Same steps and parameters as the notebook.
    """
    import ee

    starts = ee.List([d.isoformat() for d in year_windows(year)])

    def burned_in_window(day):
        start = ee.Date(day)
        return (
            ee.ImageCollection(FIRMS_COLLECTION)
            .filter(ee.Filter.date(start, start.advance(WINDOW_DAYS, "day")))
            .select(FIRMS_BAND)
            .sum()
            .gt(0)
        )

    per_window = ee.ImageCollection(starts.map(burned_in_window))
    return (
        per_window.sum()
        .reproject(ee.Projection(AGGREGATE_CRS), None, AGGREGATE_SCALE_M)
        .reduceResolution(reducer=ee.Reducer.sum())
        .toByte()
    )


def existing_outputs(year: int, bucket: str, prefix: str) -> set[str]:
    """Names of the .tif files already in the bucket for this year."""
    from google.cloud import storage

    client = storage.Client()
    blobs = client.list_blobs(bucket, prefix=f"{prefix}/{year}/")
    return {Path(b.name).stem for b in blobs if b.name.endswith(".tif")}


def firms_latest_date() -> date | None:
    """Date of the newest FIRMS image in Earth Engine."""
    import ee

    newest = (
        ee.ImageCollection(FIRMS_COLLECTION)
        .sort("system:time_start", False)
        .first()
        .date()
        .format("YYYY-MM-dd")
        .getInfo()
    )
    return date.fromisoformat(newest) if newest else None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_plan(args) -> int:
    blocks = parse_block_selection(args.blocks, load_blocks(args.blocks_file))
    windows = year_windows(args.year)
    print(f"Year {args.year}")
    print(f"  windows: {len(windows)} x {WINDOW_DAYS} days, {windows[0]} to {windows[-1]}")
    print(f"  the last window ends {last_window_end(args.year)} (exclusive), so it reaches into {args.year + 1}")
    print(f"  grid: {AGGREGATE_CRS} at {AGGREGATE_SCALE_M} m, exported as {EXPORT_CRS} at {EXPORT_SCALE_M} m")
    print(f"  blocks: {len(blocks)} from {args.blocks_file}")
    for block in blocks:
        print(f"    block {block.id:>2} -> gs://{args.bucket}/{output_prefix(args.year, block, args.prefix)}.tif")
    return 0


def cmd_check(args) -> int:
    init_earth_engine(args.project)
    import ee

    print("Earth Engine login ok:", ee.Number(1).add(1).getInfo() == 2)
    latest = firms_latest_date()
    needed = last_window_end(args.year) - timedelta(days=1)
    print(f"FIRMS latest image: {latest}")
    print(f"Needed for a complete {args.year}: up to {needed}")
    if latest is None or latest < needed:
        print(f"WARNING: FIRMS does not yet cover all of {args.year}'s windows. "
              f"The last window(s) would be incomplete.")
        return 1 if args.strict else 0
    print("FIRMS covers the whole year.")

    # Run the real calculation on a small area (about 1 x 1 degree of the Katanga
    # savanna, where it burns every year) so a mistake shows up before 25 exports start.
    sample = ee.Geometry.Rectangle([27.0, -12.5, 28.0, -11.5])
    stats = (
        build_fire_image(args.year)
        .reduceRegion(reducer=ee.Reducer.minMax(), geometry=sample, scale=AGGREGATE_SCALE_M, crs=AGGREGATE_CRS)
        .getInfo()
    )
    # The band keeps the FIRMS band name, so the keys are T21_min and T21_max
    low = next((v for k, v in stats.items() if k.endswith("_min")), None)
    high = next((v for k, v in stats.items() if k.endswith("_max")), None)
    print(f"Sample area burn count: min {low}, max {high} (expect 0 to about 27, with fire)")
    if high is None or high < 1 or high > 27:
        print("WARNING: the sample area gives an unexpected burn count.")
        return 1
    return 0


def cmd_export(args) -> int:
    blocks = parse_block_selection(args.blocks, load_blocks(args.blocks_file))

    if not args.overwrite and not args.dry_run:
        done = existing_outputs(args.year, args.bucket, args.prefix)
    else:
        done = set()
    todo = [b for b in blocks if output_name(args.year, b) not in done]
    skipped = [b for b in blocks if output_name(args.year, b) in done]
    for block in skipped:
        print(f"skip block {block.id}: {output_name(args.year, block)}.tif already exists (use --overwrite to replace)")

    if args.dry_run:
        print(f"Dry run: would start {len(todo)} export task(s) for {args.year}. Nothing was started.")
        return cmd_plan(args)

    init_earth_engine(args.project)
    import ee

    fire = build_fire_image(args.year)
    started = []
    for block in todo:
        region = block_region(block)
        task = ee.batch.Export.image.toCloudStorage(
            image=fire, region=region, **export_settings(args.year, block, args.bucket, args.prefix)
        )
        task.start()
        started.append((block, task))
        print(f"started block {block.id:>2}: {output_name(args.year, block)}  task {task.id}")

    print(f"Started {len(started)} task(s), skipped {len(skipped)}. "
          f"Follow them with: export_fire.py status --year {args.year}")
    if args.wait:
        return wait_for(args.year)
    return 0


def year_tasks(year: int):
    import ee

    prefix = f"fire{year}_"
    return [t for t in ee.batch.Task.list() if (t.config or {}).get("description", "").startswith(prefix)]


def summarise_tasks(tasks) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        state = task.status().get("state", "UNKNOWN")
        counts[state] = counts.get(state, 0) + 1
    return counts


def cmd_status(args) -> int:
    init_earth_engine(args.project)
    tasks = year_tasks(args.year)
    if not tasks:
        print(f"No Earth Engine tasks found for fire{args.year}_*")
        return 0
    for task in sorted(tasks, key=lambda t: t.config.get("description", "")):
        status = task.status()
        extra = f"  {status.get('error_message')}" if status.get("error_message") else ""
        print(f"{task.config.get('description'):<16} {status.get('state'):<10}{extra}")
    counts = summarise_tasks(tasks)
    print("Summary:", ", ".join(f"{n} {state}" for state, n in sorted(counts.items())))
    return 1 if counts.get("FAILED") else 0


def wait_for(year: int, poll_seconds: int = 120) -> int:
    """Poll until no task for the year is running. Exports take hours; prefer `status`."""
    while True:
        counts = summarise_tasks(year_tasks(year))
        print(time.strftime("%H:%M:%S"), counts)
        if not any(counts.get(s) for s in ("READY", "RUNNING")):
            return 1 if counts.get("FAILED") else 0
        time.sleep(poll_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--year", type=int, required=True, help="calendar year, e.g. 2025")
        p.add_argument("--blocks-file", type=Path, default=DEFAULT_BLOCKS)
        p.add_argument("--blocks", help="only these block ids, e.g. 3,7-9 (default: all)")
        p.add_argument("--bucket", default=BUCKET)
        p.add_argument("--prefix", default=PREFIX)
        p.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID"),
                       help="Cloud project for Earth Engine (default: $GCP_PROJECT_ID or the key's project)")

    p = sub.add_parser("plan", help="show what an export would do (no network)")
    common(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("check", help="check the Earth Engine login and FIRMS coverage")
    common(p)
    p.add_argument("--strict", action="store_true", help="exit 1 if FIRMS does not cover the year")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("export", help="start the export tasks")
    common(p)
    p.add_argument("--dry-run", action="store_true", help="show the plan, start nothing")
    p.add_argument("--overwrite", action="store_true", help="also export blocks whose file already exists")
    p.add_argument("--wait", action="store_true", help="wait until all tasks finish (can take hours)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("status", help="show the export tasks for the year")
    common(p)
    p.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 2000 <= args.year <= date.today().year:
        print(f"year {args.year} is outside 2000-{date.today().year}", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
