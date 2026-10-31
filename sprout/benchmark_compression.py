#!/usr/bin/env python3
"""Compare COG compression for one Sprout tile. Local, on small windows only.

Each seed tile is 40,000 by 40,000 pixels. This cuts a square window out of
each tile you name, remaps it with the recipe's from_seed, and writes it as a
COG with each codec. It prints bytes, write time, and read time per window,
then the same numbers scaled by the pixel ratio to a whole tile. The scaling
is a guess: a window is not the tile.

  gcloud storage cp gs://nature-watch-bucket/seeds/cropland/glad/2024/v2/30N_080E.tif data/sprout/benchmark/
  python sprout/benchmark_compression.py data/sprout/benchmark/30N_080E.tif --window 5000
"""
from __future__ import annotations

import argparse
import importlib.util
import statistics
import time
from pathlib import Path

from osgeo import gdal

gdal.UseExceptions()

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "cookbook" / "recipes" / "cropland-2024-v2.yaml"
TILE_PIXELS = 40_000

_spec = importlib.util.spec_from_file_location("sprout_cropland", Path(__file__).with_name("cropland.py"))
cropland = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cropland)


def codecs(supported: set[str]) -> list[tuple[str, list[str]]]:
    """The predictor is tried on and off. The version 2 global run used deflate-9+pred."""
    rows = []
    for level in (1, 6, 9):
        rows.append((f"deflate-{level}", ["COMPRESS=DEFLATE", f"LEVEL={level}", "PREDICTOR=NO"]))
    rows.append(("deflate-6+pred", ["COMPRESS=DEFLATE", "LEVEL=6", "PREDICTOR=STANDARD"]))
    rows.append(("deflate-9+pred", ["COMPRESS=DEFLATE", "LEVEL=9", "PREDICTOR=STANDARD"]))
    rows.append(("lzw", ["COMPRESS=LZW", "PREDICTOR=NO"]))
    if "ZSTD" in supported:
        for level in (3, 9, 15):
            rows.append((f"zstd-{level}", ["COMPRESS=ZSTD", f"LEVEL={level}", "PREDICTOR=NO"]))
        rows.append(("zstd-9+pred", ["COMPRESS=ZSTD", "LEVEL=9", "PREDICTOR=STANDARD"]))
    return rows


def supported_codecs() -> set[str]:
    options = gdal.GetDriverByName("COG").GetMetadataItem("DMD_CREATIONOPTIONLIST") or ""
    return {name for name in ("ZSTD", "LZW", "DEFLATE") if name in options}


def window_of(source: Path, size: int) -> tuple[int, int, int, int]:
    dataset = gdal.Open(str(source))
    width, height = dataset.RasterXSize, dataset.RasterYSize
    dataset = None
    size = min(size, width, height)
    return (width - size) // 2, (height - size) // 2, size, size


def read_seconds(path: Path, repeats: int = 3) -> float:
    times = []
    for _ in range(repeats):
        dataset = gdal.Open(str(path))
        band = dataset.GetRasterBand(1)
        started = time.perf_counter()
        band.ReadAsArray()
        times.append(time.perf_counter() - started)
        dataset = None
    return statistics.median(times)


def run(source: Path, size: int, threads: int, work: Path, repeats: int) -> list[dict]:
    recipe = cropland.load_recipe(RECIPE)
    facts_dataset = gdal.Open(str(source))
    projection = facts_dataset.GetProjection()
    facts_dataset = None
    vrt = work / f"{source.stem}.vrt"
    cropland.write_remap_vrt(source, vrt, recipe, projection)
    win = window_of(source, size)
    rows = []
    for name, options in codecs(supported_codecs()):
        dest = work / f"{source.stem}-{name}.tif"
        write_times = []
        for _ in range(repeats):
            if dest.exists():
                dest.unlink()
            started = time.perf_counter()
            gdal.Translate(
                str(dest),
                str(vrt),
                format="COG",
                srcWin=list(win),
                creationOptions=[
                    *options,
                    "BLOCKSIZE=512",
                    "OVERVIEWS=NONE",
                    "STATISTICS=NO",
                    f"NUM_THREADS={threads}",
                ],
            )
            write_times.append(time.perf_counter() - started)
        rows.append(
            {
                "tile": source.stem,
                "codec": name,
                "bytes": dest.stat().st_size,
                "write_s": statistics.median(write_times),
                "read_s": read_seconds(dest),
                "window": win[2],
            }
        )
        dest.unlink()
    vrt.unlink()
    return rows


def print_table(rows: list[dict]) -> None:
    scale = (TILE_PIXELS / rows[0]["window"]) ** 2
    print(f"tile {rows[0]['tile']} window {rows[0]['window']}x{rows[0]['window']} (tile scale x{scale:.0f})")
    print(f"{'codec':<18}{'bytes':>12}{'write s':>10}{'read s':>9}{'tile MB':>10}{'tile write min':>16}")
    for row in sorted(rows, key=lambda r: r["bytes"]):
        print(
            f"{row['codec']:<18}{row['bytes']:>12}{row['write_s']:>10.2f}{row['read_s']:>9.2f}"
            f"{row['bytes'] * scale / 1e6:>10.1f}{row['write_s'] * scale / 60:>16.1f}"
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tiles", nargs="+", type=Path)
    parser.add_argument("--window", type=int, default=5000)
    parser.add_argument("--threads", type=int, default=2, help="2 matches an e2-standard-2")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--work", type=Path, default=ROOT / "data" / "sprout" / "benchmark" / "work")
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    print(f"GDAL {gdal.__version__}, codecs {sorted(supported_codecs())}, threads {args.threads}")
    for tile in args.tiles:
        print_table(run(tile, args.window, args.threads, args.work, args.repeats))


if __name__ == "__main__":
    main()
