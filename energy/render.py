#!/usr/bin/env python3
"""Draw the energy sample over Esri World Imagery for a look.

  python energy/render.py check karoo --bbox 24.0 -30.70 24.12 -30.60 --zoom 15
      OpenStreetMap power features from the window's raw read, and Global Renewables Watch, in a small bbox.
  python energy/render.py sample karoo --lines data/energy/tiles/power-lines-karoo-z11-f0.05.pmtiles --zoom 8 [--top]
      the power-lines archive at a zoom, with the kept sites drawn as the site archive holds them at that zoom.

Lines are coloured by voltage class, OpenStreetMap sites are outlined by source group, wind turbines are dots.
Global Renewables Watch solar is outlined in cyan and its turbines are white rings.
Imagery tiles are cached in data/energy/imagery/.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "roads"))
sys.path.insert(0, str(HERE.parent / "tiles"))
sys.path.append(str(HERE))

import importlib.util as _iu  # noqa: E402
_spec = _iu.spec_from_file_location("roads_render", HERE.parent / "roads" / "render.py")
roads_render = _iu.module_from_spec(_spec)
_spec.loader.exec_module(roads_render)
import seed as roads_seed  # noqa: E402
import rules  # noqa: E402

DATA = HERE.parent / "data" / "energy"
roads_seed.DATA = DATA  # the imagery cache sits under data/energy/
roads_render.LEVEL_COLOURS = {1: (255, 225, 77), 2: (255, 159, 28), 3: (255, 45, 45), 4: (200, 0, 255)}  # by voltage class
roads_render.SHARE_COLOUR = (255, 159, 28)
VOLTAGE_COLOURS = {1: (255, 225, 77), 2: (255, 159, 28), 3: (255, 45, 45), 4: (200, 0, 255)}
GROUP_COLOURS = {
    "solar": (255, 230, 0), "wind": (120, 200, 255), "hydro": (0, 120, 255), "nuclear": (0, 255, 120),
    "fossil": (90, 60, 40), "coal": (0, 0, 0), "bio_waste": (130, 180, 60), "storage": (255, 255, 255),
    "substation": (255, 0, 255), "other": (180, 180, 180),
}
GRW_SOLAR = (0, 255, 255)


def imagery(bbox, zoom: int) -> Image.Image:
    return roads_render.imagery(bbox, zoom)


def check(name: str, bbox, zoom: int) -> Image.Image:
    import duckdb
    from osgeo import ogr

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; SET geometry_always_xy = true")
    west, south, east, north = bbox
    meets = f"bbox.xmin <= {east} AND bbox.xmax >= {west} AND bbox.ymin <= {north} AND bbox.ymax >= {south}"
    raw = con.execute(
        f"SELECT class, source_tags, ST_AsWKB(geometry) FROM read_parquet('{DATA / 'raw' / (name + '.parquet')}') "
        f"WHERE subtype = 'power' AND {meets}"
    ).fetchall()
    env = f"ST_MakeEnvelope({west}, {south}, {east}, {north})"
    grw = []
    for kind in ("solar", "wind"):
        path = DATA / "grw" / f"{kind}-{name}.parquet"
        if path.exists():
            grw += [(kind, w) for (w,) in con.execute(
                f"SELECT ST_AsWKB(geom::GEOMETRY) FROM read_parquet('{path}') WHERE ST_Intersects(geom::GEOMETRY, {env})"
            ).fetchall()]

    base = imagery(bbox, zoom).convert("RGBA")
    x0, y0 = roads_render.world_px(west, north, zoom, 256)
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    def px(points):
        return [(x - x0, y - y0) for x, y in (roads_render.world_px(p[0], p[1], zoom, 256) for p in points)]

    def rings(geometry):
        kind = geometry.GetGeometryName()
        if kind == "POLYGON":
            yield geometry.GetGeometryRef(0).GetPoints()
        elif kind == "MULTIPOLYGON":
            for i in range(geometry.GetGeometryCount()):
                yield geometry.GetGeometryRef(i).GetGeometryRef(0).GetPoints()

    for cls, tags, wkb in raw:
        tags = dict(tags or {})
        geometry = ogr.CreateGeometryFromWkb(bytes(wkb))
        kind = geometry.GetGeometryName()
        line_class = rules.line_class(cls, tags)
        site = rules.site_group(cls, tags, area_m2=None)
        if line_class and kind == "LINESTRING":
            draw.line(px(geometry.GetPoints()), fill=(*VOLTAGE_COLOURS[line_class], 255), width=3)
        elif site and kind == "POINT":
            x, y = px(geometry.GetPoints())[0]
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(*GROUP_COLOURS[site], 255))
        elif site:
            for ring in rings(geometry):
                draw.line(px(ring), fill=(*GROUP_COLOURS[site], 255), width=2)
    for kind, wkb in grw:
        geometry = ogr.CreateGeometryFromWkb(bytes(wkb))
        if kind == "wind":
            x, y = px(geometry.GetPoints())[0]
            draw.ellipse((x - 7, y - 7, x + 7, y + 7), outline=(255, 255, 255, 255), width=2)
        else:
            for ring in rings(geometry):
                draw.line(px(ring), fill=(*GRW_SOLAR, 255), width=2)
    base.alpha_composite(layer)
    return base


def sample(name: str, lines_path: Path, zoom: int, bbox, top: bool, scale: int) -> Image.Image:
    import duckdb
    from osgeo import ogr

    base = roads_render.render_tiles(lines_path, zoom, bbox, top, True, 1)
    west, south, east, north = bbox
    x0, y0 = roads_render.world_px(west, north, zoom, 512)
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; SET geometry_always_xy = true")
    rows = con.execute(
        f"SELECT site_group, ST_AsWKB(geometry), ST_AsWKB(ST_PointOnSurface(geometry)) FROM read_parquet('{DATA / 'sites' / (name + '.parquet')}')"
    ).fetchall()
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    def px(points):
        return [(x - x0, y - y0) for x, y in (roads_render.world_px(p[0], p[1], zoom, 512) for p in points)]

    for group, shape, centre in rows:
        colour = (*GROUP_COLOURS[group], 255)
        geometry = ogr.CreateGeometryFromWkb(bytes(shape))
        if zoom <= 9 or geometry.GetGeometryName() == "POINT":  # the archive holds a centre point to zoom 9
            x, y = px(ogr.CreateGeometryFromWkb(bytes(centre)).GetPoints())[0]
            r = 2 if zoom <= 9 else 4
            draw.ellipse((x - r, y - r, x + r, y + r), fill=colour, outline=(0, 0, 0, 255))
            continue
        kind = geometry.GetGeometryName()
        parts = [geometry] if kind == "POLYGON" else [geometry.GetGeometryRef(i) for i in range(geometry.GetGeometryCount())]
        for part in parts:
            draw.polygon(px(part.GetGeometryRef(0).GetPoints()), fill=(*GROUP_COLOURS[group], 110), outline=colour)
    base.alpha_composite(layer)
    if scale > 1:
        base = base.resize((base.width * scale, base.height * scale), Image.NEAREST)
    return base


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("name")
    c.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("W", "S", "E", "N"))
    c.add_argument("--zoom", type=int, default=15)
    c.add_argument("--out", type=Path)
    t = sub.add_parser("sample")
    t.add_argument("name")
    t.add_argument("--lines", type=Path, required=True)
    t.add_argument("--zoom", type=int, required=True)
    t.add_argument("--bbox", type=float, nargs=4)
    t.add_argument("--top", action="store_true")
    t.add_argument("--scale", type=int, default=1)
    t.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.cmd == "sample":
        import yaml

        bbox = args.bbox or {b["name"]: b for b in yaml.safe_load((HERE / "boxes.yaml").read_text())["boxes"]}[args.name]["bounds"]
        image = sample(args.name, args.lines, args.zoom, bbox, args.top, args.scale)
        out = args.out or DATA / "renders" / f"sample-{args.lines.stem}-z{args.zoom}{'-' + '_'.join(f'{v:g}' for v in args.bbox) if args.bbox else ''}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        image.convert("RGB").save(out)
        print(out)
        return
    image = check(args.name, args.bbox, args.zoom)
    out = args.out or DATA / "renders" / f"check-{args.name}-{'_'.join(f'{v:g}' for v in args.bbox)}-z{args.zoom}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(out)
    print(out)


if __name__ == "__main__":
    main()
