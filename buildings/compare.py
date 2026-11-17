"""Compare Google Open Buildings Temporal, Microsoft TEMPO and Overture outlines in small boxes.

  python buildings/compare.py index                 cache the Google 2023 tile footprints from the 151 manifests
  python buildings/compare.py box <name> [...]      read each box, write data/buildings/<name>.npz and print its row
  python buildings/compare.py all                   every box in boxes.yaml
  python buildings/compare.py tables                threshold and floor tables from the cached boxes
  python buildings/compare.py tempo-scan W S E N     TEMPO low-density pixels per tile, to find floor boxes

Every figure is on the sprout grid: EPSG:4326, 0.00025 degree cells aligned to (-180, 90).
Google: building_presence is sampled on a sub-grid of SUB x SUB points per cell (nearest neighbour,
about 0.5 m), thresholded, and averaged into the cell. TEMPO: band 1 density, averaged onto the cell.
Overture: outlines burned on the same sub-grid and averaged. Areas are cell share times the cell's
area on the WGS84 spheroid.

Reads are public and box sized. Nothing here writes to the cloud.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml
from osgeo import gdal, ogr, osr
from pyproj import Geod, Transformer

gdal.UseExceptions()

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CACHE = ROOT / "data" / "buildings"
BOXES = HERE / "boxes.yaml"

CELL = 0.00025  # sprout grid, degrees
SUB = 50  # sub-grid points per cell side for Google and Overture, about 0.5 m

THRESHOLDS = [0.3, 0.4, 0.45, 0.5, 0.6, 0.7]
FLOORS = [0.0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10]
SEED_RULES = [(0.10, 1000), (0.10, 2000), (0.15, 1000), (0.15, 2000), (0.20, 1000), (0.20, 2000)]  # (seed share, radius m)

GOOGLE_BUCKET = "open-buildings-temporal-data"
GOOGLE_EPOCH = "2023_06_30"
GOOGLE_NODATA = -99.0
GOOGLE_PRESENCE_BAND = 3
TEMPO_URL = "https://opendata.aiforgood.ai/building-density/v1/2023q4/12-39-p3_ensemble_downsampled/{name}"
TEMPO_ZOOM = 11
TEMPO_NODATA = -1.0
OVERTURE_RELEASE = "2026-10-21.0"
OVERTURE_PATH = (
    "s3://overturemaps-us-west-2/release/{release}/theme=buildings/type=building/*"
)

GEOD = Geod(ellps="WGS84")

for key, value in {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": str(256 * 1024 * 1024),
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
}.items():
    gdal.SetConfigOption(key, value)


# ---------------------------------------------------------------- grid


@dataclass(frozen=True)
class Grid:
    """A window of the sprout grid. col0/row0 count cells from (-180, 90)."""

    col0: int
    row0: int
    cols: int
    rows: int

    @property
    def west(self) -> float:
        return -180.0 + self.col0 * CELL

    @property
    def north(self) -> float:
        return 90.0 - self.row0 * CELL

    @property
    def east(self) -> float:
        return self.west + self.cols * CELL

    @property
    def south(self) -> float:
        return self.north - self.rows * CELL

    def bounds(self) -> tuple[float, float, float, float]:
        return (self.west, self.south, self.east, self.north)


def box_grid(lat: float, lon: float, size_km: float) -> Grid:
    """Square box of size_km centred on lat, lon, snapped outward to whole sprout cells."""
    half = size_km * 500.0
    north = GEOD.fwd(lon, lat, 0, half)[1]
    south = GEOD.fwd(lon, lat, 180, half)[1]
    east = GEOD.fwd(lon, lat, 90, half)[0]
    west = GEOD.fwd(lon, lat, 270, half)[0]
    col0 = math.floor((west + 180.0) / CELL)
    col1 = math.ceil((east + 180.0) / CELL)
    row0 = math.floor((90.0 - north) / CELL)
    row1 = math.ceil((90.0 - south) / CELL)
    return Grid(col0, row0, col1 - col0, row1 - row0)


def cell_area_km2(grid: Grid) -> np.ndarray:
    """Area of one cell per row, km2, on the WGS84 spheroid. Shape (rows, 1)."""
    out = np.empty((grid.rows, 1))
    for r in range(grid.rows):
        n = grid.north - r * CELL
        s = n - CELL
        w = grid.west
        e = w + CELL
        area, _ = GEOD.polygon_area_perimeter([w, e, e, w], [s, s, n, n])
        out[r, 0] = abs(area) / 1e6
    return out


def block_mean(fine: np.ndarray, sub: int) -> np.ndarray:
    """Mean of each sub x sub block. fine must be a whole number of blocks."""
    rows, cols = fine.shape[0] // sub, fine.shape[1] // sub
    return fine.reshape(rows, sub, cols, sub).mean(axis=(1, 3))


# ---------------------------------------------------------------- Google


def _https(gs: str) -> str:
    return gs.replace("gs://", "https://storage.googleapis.com/", 1)


def google_index(refresh: bool = False) -> list[dict]:
    """Every 2023 Google tile with its lon/lat footprint. Cached in data/buildings/google-2023-index.json."""
    path = CACHE / f"google-{GOOGLE_EPOCH[:4]}-index.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    CACHE.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    token = None
    while True:
        url = (
            f"https://storage.googleapis.com/storage/v1/b/{GOOGLE_BUCKET}/o"
            f"?prefix=v1/manifests/&matchGlob=**_{GOOGLE_EPOCH}.json&fields=items(name),nextPageToken"
        )
        if token:
            url += f"&pageToken={token}"
        page = json.load(urllib.request.urlopen(url))
        names += [item["name"] for item in page.get("items", [])]
        token = page.get("nextPageToken")
        if not token:
            break
    tiles = []
    for name in sorted(names):
        manifest = json.load(urllib.request.urlopen(f"https://storage.googleapis.com/{GOOGLE_BUCKET}/{name}"))
        epsg = int(re.search(r"EPSG_(\d+)", name).group(1))
        to_ll = Transformer.from_crs(epsg, 4326, always_xy=True)
        for tileset in manifest["tilesets"]:
            for source in tileset["sources"]:
                t = source["affineTransform"]
                d = source["dimensions"]
                x0, y0 = t["translateX"], t["translateY"]
                x1, y1 = x0 + t["scaleX"] * d["width"], y0 + t["scaleY"] * d["height"]
                xs = np.linspace(x0, x1, 5)
                ys = np.linspace(y0, y1, 5)
                gx, gy = np.meshgrid(xs, ys)
                lon, lat = to_ll.transform(gx.ravel(), gy.ravel())
                tiles.append(
                    {
                        "url": _https(manifest["uriPrefix"] + source["uris"][0]),
                        "epsg": epsg,
                        "bounds": [min(lon), min(lat), max(lon), max(lat)],
                    }
                )
    path.write_text(json.dumps({"manifests": len(names), "tiles": tiles}))
    return json.loads(path.read_text())


def google_tiles_for(grid: Grid) -> list[str]:
    w, s, e, n = grid.bounds()
    index = google_index()
    return [
        t["url"]
        for t in index["tiles"]
        if t["bounds"][0] < e and t["bounds"][2] > w and t["bounds"][1] < n and t["bounds"][3] > s
    ]


def _warp_fine(sources, grid: Grid, row_a: int, row_b: int, band: int, nodata: float, overview: int | None):
    """Warp sources onto the SUB sub-grid for sprout rows row_a..row_b. Returns float32, nodata as NaN."""
    north = grid.north - row_a * CELL
    south = grid.north - row_b * CELL
    opts = gdal.WarpOptions(
        format="MEM",
        dstSRS="EPSG:4326",
        outputBounds=(grid.west, south, grid.east, north),
        width=grid.cols * SUB,
        height=(row_b - row_a) * SUB,
        resampleAlg="near",
        srcBands=[band],
        srcNodata=nodata,
        dstNodata=np.nan,
        outputType=gdal.GDT_Float32,
        overviewLevel=("AUTO" if overview is None else overview),
        multithread=True,
    )
    ds = gdal.Warp("", sources, options=opts)
    return ds.GetRasterBand(1).ReadAsArray()


def google_box(grid: Grid, overview: int | None = None, strip: int = 40):
    """Google share per threshold, valid fraction, and highest presence, on the sprout grid.

    overview None reads full resolution (0.5 m). 0 is the first overview (1 m), 1 the second (2 m).
    """
    urls = google_tiles_for(grid)
    share = np.zeros((len(THRESHOLDS), grid.rows, grid.cols), dtype=np.float32)
    valid = np.zeros((grid.rows, grid.cols), dtype=np.float32)
    top = 0.0
    if not urls:
        return share, valid, float("nan"), 0
    sources = [gdal.Open("/vsicurl/" + u) for u in urls]
    level = None if overview is None else overview
    for a in range(0, grid.rows, strip):
        b = min(a + strip, grid.rows)
        fine = _warp_fine(sources, grid, a, b, GOOGLE_PRESENCE_BAND, GOOGLE_NODATA,
                          "NONE" if level is None else level)
        ok = ~np.isnan(fine)
        count = block_mean(ok.astype(np.float32), SUB)
        valid[a:b] = count
        if ok.any():
            top = max(top, float(np.nanmax(fine)))
        with np.errstate(invalid="ignore", divide="ignore"):
            for i, t in enumerate(THRESHOLDS):
                hit = block_mean((fine >= t).astype(np.float32), SUB)
                share[i, a:b] = np.where(count > 0, hit / count, 0.0)
    return share, valid, top, len(urls)


# ---------------------------------------------------------------- TEMPO


def tempo_name(x: int, y: int, z: int = TEMPO_ZOOM) -> str:
    """Index file name for XYZ tile x, y. The name counts rows from the south (TMS)."""
    return f"L15-{x:04d}E-{(2**z - 1 - y):04d}N.tif"


def xyz(lon: float, lat: float, z: int = TEMPO_ZOOM) -> tuple[int, int]:
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tempo_box(grid: Grid) -> tuple[np.ndarray, int]:
    """TEMPO 2023 Q4 band 1 averaged onto the sprout grid. Cells with no TEMPO tile are NaN."""
    x0, y0 = xyz(grid.west, grid.north)
    x1, y1 = xyz(grid.east, grid.south)
    sources = []
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            try:
                sources.append(gdal.Open("/vsicurl/" + TEMPO_URL.format(name=tempo_name(x, y))))
            except RuntimeError:
                pass  # no tile: the index has none here
    if not sources:
        return np.full((grid.rows, grid.cols), np.nan, dtype=np.float32), 0
    opts = gdal.WarpOptions(
        format="MEM",
        dstSRS="EPSG:4326",
        outputBounds=grid.bounds(),
        width=grid.cols,
        height=grid.rows,
        resampleAlg="average",
        srcBands=[1],
        srcNodata=TEMPO_NODATA,
        dstNodata=np.nan,
        outputType=gdal.GDT_Float32,
    )
    ds = gdal.Warp("", sources, options=opts)
    return ds.GetRasterBand(1).ReadAsArray(), len(sources)


# ---------------------------------------------------------------- Overture


def overture_polygons(grid: Grid, release: str = OVERTURE_RELEASE) -> list[bytes]:
    import duckdb

    con = duckdb.connect()
    for ext in ("httpfs", "spatial"):
        con.execute(f"INSTALL {ext}; LOAD {ext};")
    con.execute("SET s3_region='us-west-2';")
    w, s, e, n = grid.bounds()
    rows = con.execute(
        f"""
        SELECT ST_AsWKB(geometry)
        FROM read_parquet('{OVERTURE_PATH.format(release=release)}', hive_partitioning=1)
        WHERE bbox.xmin < ? AND bbox.xmax > ? AND bbox.ymin < ? AND bbox.ymax > ?
        """,
        [e, w, n, s],
    ).fetchall()
    return [bytes(r[0]) for r in rows]


def overture_box(grid: Grid, polygons: list[bytes], strip: int = 40) -> np.ndarray:
    """Share of each sprout cell inside an Overture outline."""
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    mem = ogr.GetDriverByName("Memory").CreateDataSource("")
    layer = mem.CreateLayer("b", srs, ogr.wkbUnknown)
    for wkb in polygons:
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(ogr.CreateGeometryFromWkb(wkb))
        layer.CreateFeature(feature)
    share = np.zeros((grid.rows, grid.cols), dtype=np.float32)
    for a in range(0, grid.rows, strip):
        b = min(a + strip, grid.rows)
        ds = gdal.GetDriverByName("MEM").Create("", grid.cols * SUB, (b - a) * SUB, 1, gdal.GDT_Byte)
        step = CELL / SUB
        ds.SetGeoTransform((grid.west, step, 0, grid.north - a * CELL, 0, -step))
        ds.SetProjection(srs.ExportToWkt())
        gdal.RasterizeLayer(ds, [1], layer, burn_values=[1])
        share[a:b] = block_mean(ds.GetRasterBand(1).ReadAsArray().astype(np.float32), SUB)
    return share


# ---------------------------------------------------------------- boxes


def load_boxes() -> dict[str, dict]:
    return {b["name"]: b for b in yaml.safe_load(BOXES.read_text())["boxes"]}


def run_box(box: dict, overview: int | None = None) -> Path:
    grid = box_grid(box["lat"], box["lon"], box["size_km"])
    google, valid, top, n_google = google_box(grid, overview=overview)
    tempo, n_tempo = tempo_box(grid)
    polygons = overture_polygons(grid)
    overture = overture_box(grid, polygons)
    CACHE.mkdir(parents=True, exist_ok=True)
    suffix = "" if overview is None else f".ov{overview}"
    path = CACHE / f"{box['name']}{suffix}.npz"
    np.savez_compressed(
        path,
        grid=np.array([grid.col0, grid.row0, grid.cols, grid.rows]),
        area=cell_area_km2(grid),
        thresholds=np.array(THRESHOLDS),
        google=google,
        google_valid=valid,
        google_top=np.array(top),
        tempo=tempo,
        overture=overture,
        counts=np.array([n_google, n_tempo, len(polygons)]),
    )
    return path


def load_box(name: str, overview: int | None = None) -> dict:
    suffix = "" if overview is None else f".ov{overview}"
    with np.load(CACHE / f"{name}{suffix}.npz") as z:
        return {k: z[k] for k in z.files}


def figures(data: dict) -> dict:
    """km2 figures for one cached box."""
    area = data["area"]
    tempo = np.nan_to_num(data["tempo"])
    covered = data["google_valid"] > 0.5
    out = {
        "google_covered": float(covered.mean()),
        "google_top": float(data["google_top"]),
        "overture_km2": float((data["overture"] * area).sum()),
        "tempo_km2": float((tempo * area).sum()),
    }
    for i, t in enumerate(data["thresholds"]):
        out[f"google_km2_{t:.2f}"] = float((data["google"][i] * area * covered).sum())
    return out


def tempo_kept(tempo: np.ndarray, floor: float = 0.0, seed: float | None = None, radius_m: float = 0.0) -> np.ndarray:
    """TEMPO share after the rule. A cell below floor is 0. With a seed, a cell is kept only if a cell
    within radius_m (counted in sprout cells of about 27.8 m, a square window) reaches the seed share."""
    from scipy.ndimage import maximum_filter

    t = np.nan_to_num(tempo)
    kept = np.where(t >= floor, t, 0.0) if floor > 0 else t
    if seed is not None:
        k = 2 * int(round(radius_m / 27.8)) + 1
        kept = np.where(maximum_filter(t, size=k, mode="constant") >= seed, kept, 0.0)
    return kept


def floor_effect(data: dict, floor: float, seed: float | None = None, radius_m: float = 0.0) -> dict:
    """What a TEMPO floor, or a seed rule, removes in one box."""
    area = data["area"]
    tempo = np.nan_to_num(data["tempo"])
    kept = tempo_kept(tempo, floor, seed, radius_m)
    overture = data["overture"]
    dropped = (tempo > 0) & (kept == 0)
    return {
        "tempo_km2": float((kept * area).sum()),
        "tempo_cells": int((kept > 0).sum()),
        "overture_km2_in_dropped_cells": float((overture * area * dropped).sum()),
        "overture_km2_without_tempo": float((overture * area * (kept == 0)).sum()),
    }


def print_row(name: str, data: dict) -> None:
    f = figures(data)
    n_google, n_tempo, n_poly = data["counts"]
    print(
        f"{name:12s} google tiles {n_google:2d} covered {f['google_covered']:.2f} top {f['google_top']:.3f} | "
        f"tempo tiles {n_tempo} {f['tempo_km2']:.3f} km2 | overture {n_poly} outlines {f['overture_km2']:.3f} km2 | "
        + " ".join(f"g{t:.2f} {f[f'google_km2_{t:.2f}']:.3f}" for t in THRESHOLDS)
    )


def tables(out_dir: Path = CACHE) -> None:
    boxes = load_boxes()
    present = {n: b for n, b in boxes.items() if (CACHE / f"{n}.npz").exists()}
    # threshold table: Google km2 / Overture km2 in settlement boxes Google covers
    with open(out_dir / "thresholds.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["box", "overture_km2", "tempo_km2"] + [f"google_{t:.2f}" for t in THRESHOLDS]
                   + [f"ratio_{t:.2f}" for t in THRESHOLDS] + ["tempo_ratio"])
        for name, box in present.items():
            if box["kind"] != "settlement":
                continue
            f = figures(load_box(name))
            g = [f[f"google_km2_{t:.2f}"] for t in THRESHOLDS]
            o = f["overture_km2"]
            covered = f["google_covered"] > 0.9
            w.writerow([name, f"{o:.4f}", f"{f['tempo_km2']:.4f}"] + [f"{x:.4f}" for x in g]
                       + [(f"{x / o:.2f}" if covered and o else "") for x in g]
                       + [f"{f['tempo_km2'] / o:.2f}" if o else ""])
    # floor table: per floor, TEMPO km2 left in empty boxes and Overture km2 lost in settlement boxes
    with open(out_dir / "floors.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rule", "box", "kind", "tempo_km2", "tempo_cells", "overture_km2",
                    "overture_km2_in_dropped_cells", "overture_km2_without_tempo"])
        rules = [(f"floor {f:g}", f, None, 0.0) for f in FLOORS]
        rules += [(f"seed {sd:g} within {r:g} m", 0.0, sd, r) for sd, r in SEED_RULES]
        for label, floor, seed, radius in rules:
            for name, box in present.items():
                data = load_box(name)
                e = floor_effect(data, floor, seed, radius)
                w.writerow([label, name, box["kind"], f"{e['tempo_km2']:.4f}", e["tempo_cells"],
                            f"{figures(data)['overture_km2']:.4f}",
                            f"{e['overture_km2_in_dropped_cells']:.4f}", f"{e['overture_km2_without_tempo']:.4f}"])
    print(f"wrote {out_dir / 'thresholds.csv'} and {out_dir / 'floors.csv'}")


def tempo_scan(west: float, south: float, east: float, north: float, workers: int = 24) -> Path:
    """Low-density TEMPO pixels per zoom 11 tile in a region. Writes data/buildings/tempo-scan.csv.

    Each row: tile centre, valid pixels, pixels above 0, sum of density, and whether Google has a tile there.
    Used to find empty land where TEMPO marks buildings, for the floor boxes.
    """
    from concurrent.futures import ThreadPoolExecutor

    x0, y0 = xyz(west, north)
    x1, y1 = xyz(east, south)
    index = google_index()["tiles"]
    cells = np.zeros((360, 180), dtype=bool)  # 1 degree cells with a Google tile footprint
    for t in index:
        w, s, e, n = t["bounds"]
        cells[int(math.floor(w)) + 180:int(math.floor(e)) + 181, int(math.floor(s)) + 90:int(math.floor(n)) + 91] = True

    def one(xy):
        x, y = xy
        url = TEMPO_URL.format(name=tempo_name(x, y))
        try:
            data = urllib.request.urlopen(url, timeout=60).read()
        except Exception:
            return None
        path = f"/vsimem/{x}_{y}.tif"
        gdal.FileFromMemBuffer(path, data)
        ds = gdal.Open(path)
        a = ds.GetRasterBand(1).ReadAsArray()
        ds = None
        gdal.Unlink(path)
        v = a[a >= 0]
        n = 2**TEMPO_ZOOM
        lon = (x + 0.5) / n * 360 - 180
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / n))))
        google = bool(cells[int(math.floor(lon)) + 180, int(math.floor(lat)) + 90])
        return (round(lat, 4), round(lon, 4), int(v.size), int((v > 0).sum()), float(v.sum()), int(google))

    jobs = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    path = CACHE / "tempo-scan.csv"
    with ThreadPoolExecutor(workers) as pool, open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["lat", "lon", "valid", "above_zero", "density_sum", "google_tile"])
        for row in pool.map(one, jobs):
            if row:
                w.writerow(row)
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("index")
    i.add_argument("--refresh", action="store_true")
    b = sub.add_parser("box")
    b.add_argument("names", nargs="+")
    b.add_argument("--overview", type=int, default=None, help="Google overview level: 0 is 1 m, 1 is 2 m")
    a = sub.add_parser("all")
    a.add_argument("--kind", choices=["settlement", "empty"])
    sub.add_parser("tables")
    sc = sub.add_parser("tempo-scan")
    sc.add_argument("bounds", nargs=4, type=float, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    args = p.parse_args(argv)

    if args.cmd == "index":
        idx = google_index(refresh=args.refresh)
        print(f"{idx['manifests']} manifests, {len(idx['tiles'])} tiles")
    elif args.cmd in ("box", "all"):
        boxes = load_boxes()
        names = args.names if args.cmd == "box" else [
            n for n, x in boxes.items() if not args.kind or x["kind"] == args.kind]
        overview = getattr(args, "overview", None)
        for name in names:
            run_box(boxes[name], overview=overview)
            print_row(name if overview is None else f"{name}.ov{overview}", load_box(name, overview))
    elif args.cmd == "tables":
        tables()
    elif args.cmd == "tempo-scan":
        print(tempo_scan(*args.bounds))
    return 0


if __name__ == "__main__":
    sys.exit(main())
