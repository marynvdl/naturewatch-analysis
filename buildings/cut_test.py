"""Google cut test (task 03b): compare the Earth Engine cut exports with seed v1, the 0.5 m COGs and Overture.

  python buildings/cut_test.py read <box> [...]     COGs, Overture and seed v1 for a cut-test box, cached
  python buildings/cut_test.py fetch                copy the finished exports from the scratch folder
  python buildings/cut_test.py report               one table per box, written to data/buildings/cut-test/report.md
  python buildings/cut_test.py render <box> [...]    panels over Esri imagery, and crops of the leftover dots

Every figure is on the sprout grid. From the 0.5 m COGs, per cell:
  mean    presence averaged, no cut (what seed v1 approximates through Earth Engine's mean pyramid)
  cut     presence below CUT set to 0, the rest kept, then averaged (what the exports compute)
  hit     share of pixels at CUT or more (task 01's threshold form)
Seed v1 is read from the published sprout, Google cells only. Lone buildings are Overture outlines
with no other outline centroid within LONE_M.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from osgeo import gdal, ogr

sys.path.insert(0, str(Path(__file__).resolve().parent))

import compare  # noqa: E402
from compare import CACHE, CELL, SUB, Grid  # noqa: E402

CUT = 0.2
LONE_M = 100.0
OUT = CACHE / "cut-test"
SPROUT = "gs://nature-watch-bucket/sprouts/buildings/2023/v1/buildings-2023.vrt"
SCALES = (0.5, 2.0, 4.0)


def grid_of(name: str) -> Grid:
    box = compare.load_boxes()[name]
    return compare.box_grid(box["lat"], box["lon"], box["size_km"])


def cog_box(grid: Grid, cut: float = CUT, strip: int = 20) -> dict[str, np.ndarray]:
    """mean, cut and hit share from the 0.5 m COGs, and the valid fraction."""
    urls = compare.google_tiles_for(grid)
    shape = (grid.rows, grid.cols)
    out = {k: np.zeros(shape, dtype=np.float32) for k in ("mean", "cut", "hit", "valid")}
    if not urls:
        return out
    sources = [gdal.Open("/vsicurl/" + u) for u in urls]
    for a in range(0, grid.rows, strip):
        b = min(a + strip, grid.rows)
        fine = compare._warp_fine(sources, grid, a, b, compare.GOOGLE_PRESENCE_BAND, compare.GOOGLE_NODATA, "NONE")
        for key, value in cell_values(fine, cut).items():
            out[key][a:b] = value
    return out


def cell_values(fine: np.ndarray, cut: float = CUT, sub: int = SUB) -> dict[str, np.ndarray]:
    """Per cell from sub x sub presence pixels (NaN = nodata): mean, cut then mean, hit share, valid fraction.
    Each mean is over the valid pixels only, as Earth Engine's reduceResolution skips masked pixels."""
    ok = ~np.isnan(fine)
    count = compare.block_mean(ok.astype(np.float32), sub)
    value = np.where(ok, fine, 0.0)
    out = {"valid": count}
    with np.errstate(invalid="ignore", divide="ignore"):
        for key, v in (("mean", value), ("cut", np.where(value >= cut, value, 0.0)), ("hit", (value >= cut).astype(np.float32))):
            out[key] = np.where(count > 0, compare.block_mean(v, sub) / count, 0.0).astype(np.float32)
    return out


def sprout_box(grid: Grid) -> tuple[np.ndarray, np.ndarray]:
    """Seed v1 share (value / 250, NaN for nodata) and source from the published sprout."""
    token = subprocess.run(["gcloud", "auth", "print-access-token"], text=True, capture_output=True, check=True).stdout.strip()
    gdal.SetConfigOption("GDAL_HTTP_HEADERS", f"Authorization: Bearer {token}")
    gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.vrt")
    try:
        ds = gdal.Open("/vsicurl/https://storage.googleapis.com/" + SPROUT.removeprefix("gs://"))
        gt = ds.GetGeoTransform()
        c, r = round((grid.west - gt[0]) / CELL), round((gt[3] - grid.north) / CELL)
        share = ds.GetRasterBand(1).ReadAsArray(c, r, grid.cols, grid.rows).astype(np.float32)
        source = ds.GetRasterBand(2).ReadAsArray(c, r, grid.cols, grid.rows)
    finally:
        gdal.SetConfigOption("GDAL_HTTP_HEADERS", None)
        gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif")
    return np.where(share == 255, np.nan, share / 250.0), source


def centroids(polygons: list[bytes]) -> np.ndarray:
    pts = [ogr.CreateGeometryFromWkb(w).Centroid() for w in polygons]
    return np.array([(p.GetX(), p.GetY()) for p in pts]).reshape(-1, 2)


def lone_mask(lonlat: np.ndarray, radius_m: float = LONE_M) -> np.ndarray:
    """True for points with no other point within radius_m (local equirectangular metres)."""
    from scipy.spatial import cKDTree

    if len(lonlat) < 2:
        return np.ones(len(lonlat), dtype=bool)
    lat0 = np.radians(lonlat[:, 1].mean())
    xy = np.c_[lonlat[:, 0] * 111_320 * np.cos(lat0), lonlat[:, 1] * 110_574]
    d, _ = cKDTree(xy).query(xy, k=2)
    return d[:, 1] > radius_m


def cells_of(grid: Grid, lonlat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    col = np.floor((lonlat[:, 0] - grid.west) / CELL).astype(int)
    row = np.floor((grid.north - lonlat[:, 1]) / CELL).astype(int)
    return np.clip(row, 0, grid.rows - 1), np.clip(col, 0, grid.cols - 1)


def read(name: str) -> Path:
    grid = grid_of(name)
    cog = cog_box(grid)
    v1, source = sprout_box(grid)
    polygons = compare.overture_polygons(grid)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.npz"
    np.savez_compressed(
        path,
        grid=np.array([grid.col0, grid.row0, grid.cols, grid.rows]),
        area=compare.cell_area_km2(grid),
        v1=v1,
        source=source,
        overture=compare.overture_box(grid, polygons),
        centroids=centroids(polygons),
        **{f"cog_{k}": v for k, v in cog.items()},
    )
    return path


def fetch() -> None:
    import ee_box

    exports = json.loads((OUT / "exports.json").read_text())
    for name, row in exports.items():
        if (OUT / f"{name}.tif").exists() or row.get("state") != "SUCCEEDED":
            continue
        src = f"gs://{ee_box.EXPORT_BUCKET}/{ee_box.EXPORT_PREFIX}/{name}.tif"
        subprocess.run(["gcloud", "storage", "cp", src, str(OUT)], check=True)


def read_export(path: Path, grid: Grid) -> np.ndarray:
    ds = gdal.Open(str(path))
    gt = ds.GetGeoTransform()
    assert abs(gt[0] - grid.west) < CELL / 100 and abs(gt[3] - grid.north) < CELL / 100, (path, gt)
    a = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    return np.where(a == 255, np.nan, a / 250.0)


def box_rows(name: str, exports: dict) -> list[dict]:
    with np.load(OUT / f"{name}.npz") as z:
        d = {k: z[k] for k in z.files}
    grid = Grid(*(int(v) for v in d["grid"]))
    area = d["area"]
    google = d["source"] == 1
    lone = lone_mask(d["centroids"])
    r, c = cells_of(grid, d["centroids"])
    lone_r, lone_c = r[lone], c[lone]
    speckle = google & (np.nan_to_num(d["v1"]) > 0) & (d["overture"] == 0)
    layers = {"seed v1": d["v1"], "COG mean": d["cog_mean"], "COG cut": d["cog_cut"], "COG hit": d["cog_hit"]}
    for key, row in exports.items():
        if row["box"] == name and (OUT / f"{key}.tif").exists():
            layers[f"EE cut {row['scale']:g} m"] = read_export(OUT / f"{key}.tif", grid)
    rows = []
    for label, layer in layers.items():
        v = np.where(google, np.nan_to_num(layer), 0.0)
        built = v > 0
        rows.append({
            "layer": label,
            "km2": float((v * area).sum()),
            "cells": int(built.sum()),
            "speckle_left": int((built & speckle).sum()),
            "lone_kept": float(built[lone_r, lone_c].mean()) if lone.any() else float("nan"),
            "eecu_s": next((x.get("eecu_seconds") for k, x in exports.items()
                            if x["box"] == name and label == f"EE cut {x['scale']:g} m"), None),
        })
    head = {"overture_km2": float((d["overture"] * area * google).sum()), "outlines": int(len(lone)),
            "lone": int(lone.sum()), "v1_speckle_cells": int(speckle.sum()), "google_cells": int(google.sum())}
    return [head] + rows


def report() -> Path:
    exports_path = OUT / "exports.json"
    exports = json.loads(exports_path.read_text()) if exports_path.exists() else {}
    lines = []
    for npz in sorted(OUT.glob("*.npz")):
        head, *rows = box_rows(npz.stem, exports)
        lines += [f"### {npz.stem}", "",
                  f"Overture {head['overture_km2']:.4f} km² in Google cells; {head['outlines']} outlines, {head['lone']} lone "
                  f"(none within {LONE_M:g} m); {head['v1_speckle_cells']} seed v1 cells above 0 with no Overture outline, "
                  f"of {head['google_cells']} Google cells.", "",
                  "| Layer | Built km² | Cells above 0 | v1 speckle cells left | Lone outlines kept | EECU-seconds |",
                  "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for x in rows:
            eecu = "" if x["eecu_s"] is None else f"{x['eecu_s']:.1f}"
            lines.append(f"| {x['layer']} | {x['km2']:.4f} | {x['cells']} | {x['speckle_left']} | {x['lone_kept']:.0%} | {eecu} |")
        lines.append("")
    path = OUT / "report.md"
    path.write_text("\n".join(lines))
    return path


def box_layers(name: str) -> tuple[dict, dict[str, np.ndarray], Grid]:
    with np.load(OUT / f"{name}.npz") as z:
        d = {k: z[k] for k in z.files}
    grid = Grid(*(int(v) for v in d["grid"]))
    layers = {"seed v1": np.nan_to_num(d["v1"]), "0.5 m cut (COGs)": d["cog_cut"]}
    for scale in (2, 4):
        path = OUT / f"{name}-cut{CUT:g}-{scale}m.tif"
        if path.exists():
            layers[f"{scale} m cut (Earth Engine)"] = np.nan_to_num(read_export(path, grid))
    google = d["source"] == 1
    return d, {k: np.where(google, v, 0.0) for k, v in layers.items()}, grid


def render(name: str, zoom: int = 15, crops: int = 15) -> list[Path]:
    """One panel per layer over imagery, cells above 0 in red. Then imagery crops at zoom 17 around the
    cells the 4 m cut keeps where Overture has no outline: real buildings, or noise the cut leaves."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "roads"))
    import render as road_render
    from PIL import Image, ImageDraw

    d, layers, grid = box_layers(name)
    base = road_render.imagery(grid.bounds(), zoom).resize((grid.cols * 2, grid.rows * 2)).convert("RGBA")
    panels = []
    for label, v in layers.items():
        over = np.zeros((grid.rows, grid.cols, 4), dtype=np.uint8)
        over[v > 0] = (255, 40, 40, 255)
        im = base.copy()
        im.alpha_composite(Image.fromarray(over).resize(base.size, Image.NEAREST))
        ImageDraw.Draw(im).text((8, 8), f"{label}: {int((v > 0).sum())} cells", fill=(255, 255, 255))
        panels.append(im)
    sheet = Image.new("RGB", (base.width * len(panels), base.height))
    for i, im in enumerate(panels):
        sheet.paste(im.convert("RGB"), (i * base.width, 0))
    out = [OUT / f"{name}-panels.png"]
    sheet.save(out[0])
    key = next((k for k in layers if k.startswith("4 m")), "0.5 m cut (COGs)")
    left = np.argwhere((layers[key] > 0) & (d["overture"] == 0))
    if len(left):
        pick = left[np.linspace(0, len(left) - 1, min(crops, len(left))).astype(int)]
        tiles = []
        for r, c in pick:
            lon, lat = grid.west + (c + 0.5) * CELL, grid.north - (r + 0.5) * CELL
            im = road_render.imagery((lon - 3 * CELL, lat - 3 * CELL, lon + 3 * CELL, lat + 3 * CELL), 17).resize((210, 210))
            ImageDraw.Draw(im).rectangle((88, 88, 122, 122), outline=(255, 40, 40), width=2)
            ImageDraw.Draw(im).text((4, 4), f"{lat:.5f}, {lon:.5f} v={layers[key][r, c]:.3f}", fill=(255, 255, 255))
            tiles.append(im)
        crop = Image.new("RGB", (210 * 5, 210 * ((len(tiles) + 4) // 5)))
        for i, t in enumerate(tiles):
            crop.paste(t, ((i % 5) * 210, (i // 5) * 210))
        out.append(OUT / f"{name}-leftover.png")
        crop.save(out[1])
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read")
    r.add_argument("names", nargs="+")
    sub.add_parser("fetch")
    sub.add_parser("report")
    rd = sub.add_parser("render")
    rd.add_argument("names", nargs="+")
    args = p.parse_args(argv)
    if args.cmd == "read":
        for name in args.names:
            print(read(name))
    elif args.cmd == "fetch":
        fetch()
    elif args.cmd == "render":
        for name in args.names:
            print(*render(name), sep="\n")
    else:
        print(report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
