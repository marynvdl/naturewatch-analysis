"""Read one box from Earth Engine on the sprout grid and compare it with compare.py's cache.

  python buildings/ee_box.py <name> --value mean                    the seed value: mean presence
  python buildings/ee_box.py <name> --value threshold --threshold 0.45 [--scale 0.5]
  python buildings/ee_box.py <name> --value cut --threshold 0.2 --scale 2 --export     start an export task
  python buildings/ee_box.py --status                                                  EECU and state of the exports

Uses computePixels on GOOGLE/Research/open-buildings-temporal/v1, 2023, onto the sprout grid
(EPSG:4326, 0.00025 degrees from -180, 90). No export task is started.

- mean: mean building_presence of the 2023 images. Earth Engine reads its mean pyramid. This is the
  value the Google seed exports.
- threshold: presence at or above the threshold on pixels of --scale metres (0.5 is Google's own grid,
  2 and 4 read Earth Engine's pyramid), then mean-reduced into the cell. Compared with the COG reads.
- cut: presence below the threshold set to 0, the rest kept, then mean-reduced into the cell (task 03b).

--export starts an Earth Engine export of the box instead, as the seed encodes it: uint8 share x 250,
255 where Google has no data. It goes to gs://nature-watch-bucket/scratch/buildings/google-cut-test/.
The task id is kept in data/buildings/cut-test/exports.json, and --status adds each task's EECU-seconds.

Log in first with `earthengine authenticate`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare import CACHE, CELL, Grid, box_grid, figures, load_box, load_boxes  # noqa: E402

COLLECTION = "GOOGLE/Research/open-buildings-temporal/v1"
TRANSFORM = [CELL, 0, -180, 0, -CELL, 90]
EXPORT_BUCKET = "nature-watch-bucket"
EXPORT_PREFIX = "scratch/buildings/google-cut-test"
EXPORTS = CACHE / "cut-test" / "exports.json"


def ee_image(grid: Grid, value: str, threshold: float = 0.45, scale: float = 0.5):
    import ee

    region = ee.Geometry.Rectangle(list(grid.bounds()), "EPSG:4326", False)
    images = ee.ImageCollection(COLLECTION).filterDate("2023-01-01", "2024-01-01").filterBounds(region)
    presence = images.select("building_presence")
    if value == "mean":
        return presence.mean()
    # A composite has no default projection. Use the 0.5 m UTM grid of the first image in the box.
    native = images.first().select("building_presence").projection()
    p = presence.mosaic().setDefaultProjection(native)
    if scale != 0.5:
        p = p.reproject(native.atScale(scale))
    if value == "cut":
        hit = p.multiply(p.gte(threshold))
    else:
        hit = p.gte(threshold).updateMask(p.mask())
    return hit.reduceResolution(reducer=ee.Reducer.mean(), maxPixels=4096).reproject(
        crs="EPSG:4326", crsTransform=TRANSFORM
    )


def compute(image, grid: Grid, piece: int) -> np.ndarray:
    import ee

    expression = image.unmask(-1).toFloat()
    result = np.full((grid.rows, grid.cols), np.nan, dtype=np.float32)
    # A threshold request may not reproject more than about 10,000 x 10,000 input pixels, so ask in pieces.
    for r0 in range(0, grid.rows, piece):
        for c0 in range(0, grid.cols, piece):
            rows, cols = min(piece, grid.rows - r0), min(piece, grid.cols - c0)
            request = {
                "expression": expression,
                "fileFormat": "NUMPY_NDARRAY",
                "grid": {
                    "dimensions": {"width": cols, "height": rows},
                    "affineTransform": {
                        "scaleX": CELL, "shearX": 0, "translateX": grid.west + c0 * CELL,
                        "shearY": 0, "scaleY": -CELL, "translateY": grid.north - r0 * CELL,
                    },
                    "crsCode": "EPSG:4326",
                },
            }
            out = ee.data.computePixels(request)
            arr = out[out.dtype.names[0]] if out.dtype.names else out
            result[r0:r0 + rows, c0:c0 + cols] = np.where(arr < 0, np.nan, arr)
    return result


def export_name(name: str, value: str, threshold: float, scale: float) -> str:
    return f"{name}-{value}{threshold:g}-{scale:g}m"


def start_export(image, grid: Grid, name: str) -> str:
    """Start one export of the box on the sprout grid as the seed encodes it. Returns the task id."""
    import ee

    encoded = image.multiply(250).round().unmask(255).toUint8()
    task = ee.batch.Export.image.toCloudStorage(
        image=encoded,
        description=name,
        bucket=EXPORT_BUCKET,
        fileNamePrefix=f"{EXPORT_PREFIX}/{name}",
        dimensions=f"{grid.cols}x{grid.rows}",
        crs="EPSG:4326",
        crsTransform=[CELL, 0, grid.west, 0, -CELL, grid.north],
        maxPixels=int(1e9),
        formatOptions={"cloudOptimized": True, "noData": 255},
    )
    task.start()
    return task.id


def load_exports() -> dict:
    return json.loads(EXPORTS.read_text()) if EXPORTS.exists() else {}


def save_exports(exports: dict) -> None:
    EXPORTS.parent.mkdir(parents=True, exist_ok=True)
    EXPORTS.write_text(json.dumps(exports, indent=2) + "\n")


def status(exports: dict) -> dict:
    """Add each task's state, EECU-seconds and times from its operation."""
    import ee

    for name, row in exports.items():
        meta = ee.data.getOperation(f"projects/{row['project']}/operations/{row['task']}").get("metadata", {})
        row.update({
            "state": meta.get("state"),
            "eecu_seconds": meta.get("batchEecuUsageSeconds"),
            "start": meta.get("startTime"),
            "end": meta.get("endTime"),
            "uris": meta.get("destinationUris"),
        })
    return exports


def agreement(cog: np.ndarray, eeg: np.ndarray, area: np.ndarray, covered: np.ndarray) -> dict:
    both = covered & ~np.isnan(eeg)
    a = np.where(both, cog, 0.0)
    b = np.where(both, np.nan_to_num(eeg), 0.0)
    diff = np.abs(a - b)[both]
    return {
        "cells": int(both.sum()),
        "cog_km2": float((a * area).sum()),
        "ee_km2": float((b * area).sum()),
        "mean_abs_diff": float(diff.mean()) if diff.size else float("nan"),
        "p99_abs_diff": float(np.percentile(diff, 99)) if diff.size else float("nan"),
        "correlation": float(np.corrcoef(a[both], b[both])[0, 1]) if diff.size > 1 else float("nan"),
    }


def main(argv: list[str] | None = None) -> int:
    import ee

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", nargs="?")
    p.add_argument("--value", choices=["mean", "threshold", "cut"], default="mean")
    p.add_argument("--threshold", type=float, default=0.45)
    p.add_argument("--scale", type=float, default=0.5, help="threshold pixel size in metres: 0.5, 2 or 4")
    p.add_argument("--project", default="nature-watch-387210")
    p.add_argument("--export", action="store_true", help="start an Earth Engine export of the box")
    p.add_argument("--status", action="store_true", help="state and EECU-seconds of the started exports")
    args = p.parse_args(argv)
    ee.Initialize(project=args.project)
    if args.status:
        exports = status(load_exports())
        save_exports(exports)
        for name, row in exports.items():
            print(name, row["task"], row["state"], row["eecu_seconds"])
        return 0
    if not args.name:
        p.error("name is required")
    if args.export:
        box = load_boxes()[args.name]
        grid = box_grid(box["lat"], box["lon"], box["size_km"])
        name = export_name(args.name, args.value, args.threshold, args.scale)
        exports = load_exports()
        if name in exports:
            p.error(f"{name} was already started as task {exports[name]['task']}")
        task = start_export(ee_image(grid, args.value, args.threshold, args.scale), grid, name)
        exports[name] = {"task": task, "project": args.project, "box": args.name, "value": args.value,
                         "threshold": args.threshold, "scale": args.scale,
                         "grid": [grid.col0, grid.row0, grid.cols, grid.rows]}
        save_exports(exports)
        print(name, task)
        return 0
    data = load_box(args.name)
    col0, row0, cols, rows = (int(v) for v in data["grid"])
    grid = Grid(col0, row0, cols, rows)
    piece = max(cols, rows) if args.value == "mean" or args.scale >= 4 else 48
    eeg = compute(ee_image(grid, args.value, args.threshold, args.scale), grid, piece)
    suffix = "mean" if args.value == "mean" else f"{'t' if args.value == 'threshold' else 'c'}{args.threshold:g}s{args.scale:g}"
    np.save(CACHE / f"{args.name}.ee-{suffix}.npy", eeg)
    i = int(np.argmin(np.abs(data["thresholds"] - args.threshold)))
    result = agreement(data["google"][i], eeg, data["area"], data["google_valid"] > 0.5)
    result["overture_km2"] = figures(data)["overture_km2"]
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
