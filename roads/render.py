#!/usr/bin/env python3
"""Draw the roads sample for a look: archive tiles, or seed lines by level, over imagery.

  python roads/render.py tiles data/roads/tiles/chinko-z11.pmtiles --zoom 8 --bbox W S E N [--scale 2]
  python roads/render.py lines waterberg --zoom 14 [--bbox W S E N]

Imagery is Esri World Imagery, fetched once per tile into data/roads/imagery/.
The colours are for looking only. The app sets its own paint (task 04).
"""
from __future__ import annotations

import argparse
import math
import sys
import urllib.request
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tiles"))

import pyramid  # noqa: E402
import seed as seedmod  # noqa: E402
import transcode  # noqa: E402

IMAGERY = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
SHARE_COLOUR = (255, 79, 216)  # magenta
LEVEL_COLOURS = {4: (255, 45, 45), 3: (255, 159, 28), 2: (255, 225, 77), 1: (255, 79, 216)}
SURFACE_ALPHA = {0: 0.85, 1: 1.0, 2: 0.6}  # unknown, paved, unpaved


def world_px(lon: float, lat: float, zoom: int, size: int) -> tuple[float, float]:
    n = 2**zoom * size
    x = (lon + 180) / 360 * n
    lat = max(min(lat, 85.0511), -85.0511)
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def imagery(bbox, zoom: int) -> Image.Image:
    """Esri imagery for bbox at 256 px tile zoom `zoom`, cropped to the bbox."""
    west, south, east, north = bbox
    x0, y0 = world_px(west, north, zoom, 256)
    x1, y1 = world_px(east, south, zoom, 256)
    tx0, ty0, tx1, ty1 = int(x0 // 256), int(y0 // 256), int(x1 // 256), int(y1 // 256)
    count = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
    if count > 600:
        raise SystemExit(f"{count} imagery tiles is too many; use a smaller bbox or zoom")
    canvas = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256))
    cache = seedmod.DATA / "imagery" / str(zoom)
    cache.mkdir(parents=True, exist_ok=True)
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            path = cache / f"{tx}_{ty}.jpg"
            if not path.exists():
                request = urllib.request.Request(IMAGERY.format(z=zoom, x=tx, y=ty), headers={"User-Agent": "roads-sample-check"})
                path.write_bytes(urllib.request.urlopen(request, timeout=30).read())
            canvas.paste(Image.open(BytesIO(path.read_bytes())).convert("RGB"), ((tx - tx0) * 256, (ty - ty0) * 256))
    left, top = x0 - tx0 * 256, y0 - ty0 * 256
    return canvas.crop((int(left), int(top), int(left + x1 - x0), int(top + y1 - y0)))


def archive_tiles(path: Path, zoom: int) -> dict[tuple[int, int], bytes]:
    return {(x, y): data for (z, x, y), data in pyramid.read_archive(path) if z == zoom}


def colour_tile(red: np.ndarray, top: bool) -> np.ndarray:
    rgba = np.zeros(red.shape + (4,), dtype=np.uint8)
    if top:
        level, surface = red // 4, red % 4
        for value, colour in LEVEL_COLOURS.items():
            for s, alpha in SURFACE_ALPHA.items():
                mask = (level == value) & (surface == s)
                rgba[mask] = (*colour, int(alpha * 255))
    else:
        mask = red > 0
        opacity = 0.2 + 0.75 * np.sqrt(red / 255)
        rgba[mask, :3] = SHARE_COLOUR
        rgba[mask, 3] = (opacity[mask] * 255).astype(np.uint8)
    return rgba


def render_tiles(path: Path, zoom: int, bbox, top: bool, use_imagery: bool, scale: int) -> Image.Image:
    west, south, east, north = bbox
    x0, y0 = world_px(west, north, zoom, 512)
    x1, y1 = world_px(east, south, zoom, 512)
    width, height = int(round(x1 - x0)), int(round(y1 - y0))
    overlay = np.zeros((height, width, 4), dtype=np.uint8)
    for (tx, ty), data in archive_tiles(path, zoom).items():
        rgba = transcode.decode_rgba(data)
        red = np.where(rgba[..., 3] == 0, 0, rgba[..., 0]).astype(np.int32)
        coloured = colour_tile(red, top)
        ox, oy = int(round(tx * 512 - x0)), int(round(ty * 512 - y0))
        sx0, sy0 = max(0, -ox), max(0, -oy)
        sx1, sy1 = min(512, width - ox), min(512, height - oy)
        if sx0 < sx1 and sy0 < sy1:
            overlay[oy + sy0 : oy + sy1, ox + sx0 : ox + sx1] = coloured[sy0:sy1, sx0:sx1]
    base = imagery(bbox, zoom + 1).resize((width, height)) if use_imagery else Image.new("RGB", (width, height), (40, 44, 52))
    base = base.convert("RGBA")
    base.alpha_composite(Image.fromarray(overlay, "RGBA"))
    if scale > 1:
        base = base.resize((width * scale, height * scale), Image.NEAREST)
    return base


def render_lines(name: str, zoom: int, bbox, only_class: str | None = None) -> Image.Image:
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    west, south, east, north = bbox
    rows = con.execute(
        f"""SELECT level, coalesce(surface, 'unknown'), ST_AsWKB(geometry) FROM read_parquet('{seedmod.seed_path(name)}')
            WHERE bbox.xmin <= {east} AND bbox.xmax >= {west} AND bbox.ymin <= {north} AND bbox.ymax >= {south}
            {f"AND class = '{only_class}'" if only_class else ""}"""
    ).fetchall()
    from osgeo import ogr

    base = imagery(bbox, zoom).convert("RGBA")
    x0, y0 = world_px(west, north, zoom, 256)
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    values = {"track": 1, "minor": 2, "main": 3, "major": 4}
    surfaces = {"unknown": 0, "paved": 1, "unpaved": 2}
    for level, surface, wkb in sorted(rows, key=lambda r: values[r[0]]):
        geometry = ogr.CreateGeometryFromWkb(bytes(wkb))
        points = [world_px(lon, lat, zoom, 256) for lon, lat, *_ in geometry.GetPoints()]
        points = [(x - x0, y - y0) for x, y in points]
        colour = (*LEVEL_COLOURS[values[level]], int(SURFACE_ALPHA[surfaces[surface]] * 255))
        draw.line(points, fill=colour, width=3 if surface != "unpaved" else 2)
    base.alpha_composite(layer)
    return base


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("tiles", "lines"))
    parser.add_argument("source", help="archive path for tiles, box name for lines")
    parser.add_argument("--zoom", type=int, required=True)
    parser.add_argument("--bbox", type=float, nargs=4)
    parser.add_argument("--top", action="store_true", help="tiles: the zoom is the archive's top zoom")
    parser.add_argument("--no-imagery", action="store_true")
    parser.add_argument("--scale", type=int, default=1)
    parser.add_argument("--class", dest="only_class", help="lines: draw only this Overture class")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    out_dir = seedmod.DATA / "renders"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.command == "tiles":
        path = Path(args.source)
        image = render_tiles(path, args.zoom, args.bbox, args.top, not args.no_imagery, args.scale)
        out = args.out or out_dir / f"{path.stem}-at-z{args.zoom}.png"
    else:
        bbox = args.bbox or seedmod.box_bounds(seedmod.load_boxes()[args.source])
        image = render_lines(args.source, args.zoom, bbox, args.only_class)
        out = args.out or out_dir / f"{args.source}-lines-z{args.zoom}.png"
    image.save(out)
    print(out)


if __name__ == "__main__":
    main()
