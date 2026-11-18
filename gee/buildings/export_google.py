#!/usr/bin/env python3
"""Export the Google seed of the Buildings 2023 layer from Earth Engine to Cloud Storage.

For each 10 degree block in google-2023-blocks.json, one export writes a uint8 COG on the
sprout grid (EPSG:4326, 0.00025 degrees from -180, 90) to the Google seed path of
cookbook/recipes/buildings-2023-v1.yaml. A cell holds the mean building_presence of
Google Open Buildings 2.5D Temporal's 2023 images times 250, rounded, and 255 where
Google has no 2023 data. Earth Engine reads its mean pyramid at the sprout grid, as
buildings/ee_box.py --value mean does for one box.

Subcommands
  plan    List the blocks and object names. No network, no credentials.
  check   Log in, and compare one box of the export image with buildings/ee_box.py's cache.
  export  Start one Earth Engine export task per block. Returns straight away.
  status  Show the state of the export tasks.

Examples
  python gee/buildings/export_google.py plan
  python gee/buildings/export_google.py check --box kongoussi
  python gee/buildings/export_google.py export --dry-run
  python gee/buildings/export_google.py export --blocks W010N10
  python gee/buildings/export_google.py export
  python gee/buildings/export_google.py status

Log in once with `earthengine authenticate`.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DEFAULT_RECIPE = ROOT / "cookbook" / "recipes" / "buildings-2023-v1.yaml"
DEFAULT_BLOCKS = HERE / "google-2023-blocks.json"
PROJECT = "nature-watch-387210"
TASK_PREFIX = "buildings-google-2023-"
# A block is 40,000 x 40,000 cells. One file per block: a multiple of the 256 shard size above that.
FILE_DIMENSIONS = 40192
MAX_PIXELS = 2_000_000_000


@dataclass(frozen=True)
class Block:
    west: int
    south: int
    size: int = 10

    @property
    def name(self) -> str:
        """The south-west corner, e.g. W010N00 for 10 W to 0, 0 to 10 N."""
        ew = "E" if self.west >= 0 else "W"
        ns = "N" if self.south >= 0 else "S"
        return f"{ew}{abs(self.west):03d}{ns}{abs(self.south):02d}"

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        return self.west, self.south, self.west + self.size, self.south + self.size


# --------------------------------------------------------------------------
# Pure helpers: no Earth Engine, no network. These are what the tests cover.
# --------------------------------------------------------------------------


def load_recipe(path: Path = DEFAULT_RECIPE) -> dict:
    return yaml.safe_load(Path(path).read_text())


def load_blocks(recipe: dict, path: Path = DEFAULT_BLOCKS) -> list[Block]:
    size = recipe["seeds"]["google"]["blocks_deg"]
    blocks = [Block(w, s, size) for w, s in json.loads(Path(path).read_text())["blocks"]]
    if len({b.name for b in blocks}) != len(blocks):
        raise ValueError(f"duplicate blocks in {path}")
    expected = recipe["seeds"]["google"]["block_count"]
    if len(blocks) != expected:
        raise ValueError(f"{path} lists {len(blocks)} blocks, the recipe says {expected}")
    return blocks


def select_blocks(text: str | None, blocks: list[Block]) -> list[Block]:
    """'W010N10,E000N00' picks those blocks. None means all of them."""
    if not text:
        return blocks
    wanted = {part.strip() for part in text.split(",") if part.strip()}
    known = {b.name for b in blocks}
    missing = sorted(wanted - known)
    if missing:
        raise ValueError(f"no such block(s): {missing}")
    return [b for b in blocks if b.name in wanted]


def split_gs(path: str) -> tuple[str, str]:
    if not path.startswith("gs://"):
        raise ValueError(f"not a gs path: {path}")
    bucket, _, prefix = path[len("gs://"):].partition("/")
    return bucket, prefix.rstrip("/")


def crs_transform(recipe: dict) -> list[float]:
    grid = recipe["grid"]
    cell = grid["pixel_size_deg"]
    x0, y0 = grid["origin"]
    return [cell, 0, x0, 0, -cell, y0]


def export_settings(recipe: dict, block: Block) -> dict:
    """The Export.image.toCloudStorage arguments except the image and the region."""
    seed = recipe["seeds"]["google"]
    bucket, prefix = split_gs(seed["path"])
    return {
        "description": TASK_PREFIX + block.name,
        "bucket": bucket,
        "fileNamePrefix": f"{prefix}/{block.name}",
        "crs": recipe["grid"]["crs"],
        "crsTransform": crs_transform(recipe),
        "maxPixels": MAX_PIXELS,
        "fileDimensions": [FILE_DIMENSIONS, FILE_DIMENSIONS],
        "fileFormat": "GeoTIFF",
        "formatOptions": {"cloudOptimized": True, "noData": seed["nodata"]},
    }


def block_cells(recipe: dict, block: Block) -> int:
    return int(round(block.size / recipe["grid"]["pixel_size_deg"])) ** 2


# --------------------------------------------------------------------------
# Earth Engine and Cloud Storage. Needs credentials.
# --------------------------------------------------------------------------


def seed_image(recipe: dict):
    """Mean 2023 building_presence times 250, rounded, as uint8. 255 where Google has no 2023 data."""
    import ee

    google = recipe["sources"]["google"]
    seed = recipe["seeds"]["google"]
    year = recipe["year"]
    presence = (
        ee.ImageCollection(google["earth_engine"])
        .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
        .select(google["band"])
        .mean()
    )
    return presence.multiply(seed["scale"]).round().unmask(seed["nodata"]).toByte()


def block_region(block: Block):
    import ee

    return ee.Geometry.Rectangle(list(block.bounds), "EPSG:4326", False)


def existing_names(recipe: dict) -> set[str]:
    """Uses the gcloud login, not application default credentials."""
    import subprocess

    result = subprocess.run(
        ["gcloud", "storage", "ls", recipe["seeds"]["google"]["path"], f"--project={PROJECT}"],
        text=True, capture_output=True,
    )
    if result.returncode != 0 and "matched no objects" not in result.stderr:
        raise SystemExit(result.stderr)
    return {Path(line).stem for line in result.stdout.split() if line.endswith(".tif")}


def our_tasks():
    import ee

    return [t for t in ee.batch.Task.list() if (t.config or {}).get("description", "").startswith(TASK_PREFIX)]


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_plan(args) -> int:
    recipe = load_recipe(args.recipe)
    blocks = select_blocks(args.blocks, load_blocks(recipe, args.blocks_file))
    seed = recipe["seeds"]["google"]
    print(f"{seed['id']}: {len(blocks)} blocks of {seed['blocks_deg']} degrees")
    print(f"  grid {recipe['grid']['crs']} {crs_transform(recipe)}, {block_cells(recipe, blocks[0]):,} cells a block")
    for block in blocks:
        print(f"  {block.name} {block.bounds} -> {seed['path']}{block.name}.tif")
    return 0


def cmd_check(args) -> int:
    """The export image on one cached box must match ee_box.py --value mean to within rounding."""
    import ee
    import numpy as np

    sys.path.insert(0, str(ROOT / "buildings"))
    from compare import CACHE, Grid, load_box  # noqa: E402

    recipe = load_recipe(args.recipe)
    ee.Initialize(project=args.project)
    data = load_box(args.box)
    col0, row0, cols, rows = (int(v) for v in data["grid"])
    grid = Grid(col0, row0, cols, rows)
    cached = np.load(CACHE / f"{args.box}.eemean.npy")
    cell = recipe["grid"]["pixel_size_deg"]
    request = {
        "expression": seed_image(recipe),
        "fileFormat": "NUMPY_NDARRAY",
        "grid": {
            "dimensions": {"width": cols, "height": rows},
            "affineTransform": {
                "scaleX": cell, "shearX": 0, "translateX": grid.west,
                "shearY": 0, "scaleY": -cell, "translateY": grid.north,
            },
            "crsCode": recipe["grid"]["crs"],
        },
    }
    out = ee.data.computePixels(request)
    values = (out[out.dtype.names[0]] if out.dtype.names else out).astype(np.int16)
    seed = recipe["seeds"]["google"]
    expected = np.where(np.isnan(cached), seed["nodata"], np.rint(np.nan_to_num(cached) * seed["scale"]))
    diff = np.abs(values - expected)
    print(f"{args.box}: {values.size} cells, {int((values == seed['nodata']).sum())} nodata, "
          f"max {int(values[values != seed['nodata']].max(initial=0))}, "
          f"{int((diff > 0).sum())} differ, largest difference {int(diff.max())}")
    if diff.max() > 1:
        print("The export image does not match the cached box.")
        return 1
    return 0


def cmd_export(args) -> int:
    recipe = load_recipe(args.recipe)
    blocks = select_blocks(args.blocks, load_blocks(recipe, args.blocks_file))
    done = set() if args.dry_run else existing_names(recipe)
    todo = [b for b in blocks if b.name not in done]
    for block in blocks:
        if block.name in done:
            print(f"skip {block.name}: already in the seed path")
    if args.dry_run:
        print(f"Dry run: would start {len(todo)} export task(s). Nothing was started.")
        return 0
    import ee

    ee.Initialize(project=args.project)
    image = seed_image(recipe)
    for block in todo:
        task = ee.batch.Export.image.toCloudStorage(
            image=image, region=block_region(block), **export_settings(recipe, block)
        )
        task.start()
        print(f"started {block.name}  task {task.id}", flush=True)
    print(f"Started {len(todo)} task(s), skipped {len(blocks) - len(todo)}. "
          "Follow them with: export_google.py status")
    return 0


def cmd_status(args) -> int:
    import ee

    ee.Initialize(project=args.project)
    counts: dict[str, int] = {}
    eecu = 0.0
    for task in sorted(our_tasks(), key=lambda t: t.config.get("description", "")):
        status = task.status()
        state = status.get("state", "UNKNOWN")
        counts[state] = counts.get(state, 0) + 1
        eecu += float(status.get("batch_eecu_usage_seconds") or 0)
        if args.verbose or state not in ("COMPLETED",):
            extra = f"  {status.get('error_message')}" if status.get("error_message") else ""
            print(f"{task.config.get('description'):<30} {state:<10} {task.id}{extra}")
    print("Summary:", ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "no tasks")
    print(f"EECU-hours so far: {eecu / 3600:.2f}")
    return 1 if counts.get("FAILED") else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
        p.add_argument("--blocks-file", type=Path, default=DEFAULT_BLOCKS)
        p.add_argument("--blocks", help="only these blocks, e.g. W010N10,E000N00 (default: all)")
        p.add_argument("--project", default=PROJECT)

    p = sub.add_parser("plan", help="list the blocks (no network)")
    common(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("check", help="compare one cached box with the export image")
    common(p)
    p.add_argument("--box", default="kongoussi", help="a box with a buildings/ee_box.py --value mean cache")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("export", help="start the export tasks")
    common(p)
    p.add_argument("--dry-run", action="store_true", help="start nothing")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("status", help="show the export tasks")
    common(p)
    p.add_argument("--verbose", action="store_true", help="also list completed tasks")
    p.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
