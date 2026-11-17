"""Read one box from Earth Engine on the sprout grid and compare it with compare.py's cache.

  python buildings/ee_box.py <name> --value mean                    the seed value: mean presence
  python buildings/ee_box.py <name> --value threshold --threshold 0.45 [--scale 0.5]

Uses computePixels on GOOGLE/Research/open-buildings-temporal/v1, 2023, onto the sprout grid
(EPSG:4326, 0.00025 degrees from -180, 90). No export task is started.

- mean: mean building_presence of the 2023 images. Earth Engine reads its mean pyramid. This is the
  value the Google seed exports.
- threshold: presence at or above the threshold on pixels of --scale metres (0.5 is Google's own grid,
  2 and 4 read Earth Engine's pyramid), then mean-reduced into the cell. Compared with the COG reads.

Log in first with `earthengine authenticate`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare import CACHE, CELL, Grid, figures, load_box  # noqa: E402

COLLECTION = "GOOGLE/Research/open-buildings-temporal/v1"
TRANSFORM = [CELL, 0, -180, 0, -CELL, 90]


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
    p.add_argument("name")
    p.add_argument("--value", choices=["mean", "threshold"], default="mean")
    p.add_argument("--threshold", type=float, default=0.45)
    p.add_argument("--scale", type=float, default=0.5, help="threshold pixel size in metres: 0.5, 2 or 4")
    p.add_argument("--project", default="nature-watch-387210")
    args = p.parse_args(argv)
    ee.Initialize(project=args.project)
    data = load_box(args.name)
    col0, row0, cols, rows = (int(v) for v in data["grid"])
    grid = Grid(col0, row0, cols, rows)
    piece = max(cols, rows) if args.value == "mean" or args.scale >= 4 else 48
    eeg = compute(ee_image(grid, args.value, args.threshold, args.scale), grid, piece)
    suffix = "mean" if args.value == "mean" else f"t{args.threshold:g}s{args.scale:g}"
    np.save(CACHE / f"{args.name}.ee-{suffix}.npy", eeg)
    i = int(np.argmin(np.abs(data["thresholds"] - args.threshold)))
    result = agreement(data["google"][i], eeg, data["area"], data["google_valid"] > 0.5)
    result["overture_km2"] = figures(data)["overture_km2"]
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
