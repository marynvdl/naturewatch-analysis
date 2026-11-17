# roads: Overture roads seed, sprout and tiles

Scripts for the Roads layer: a seed read from Overture's transportation theme, a sprout on the stats grid, and raster PMTiles from that sprout. The recipe is [`cookbook/recipes/roads-2026-10-v1.yaml`](../cookbook/recipes/roads-2026-10-v1.yaml). The parcel is `docs/parcels/roads.md` in naturewatch-app.

Task 01 ran these on the laptop for ten small boxes and two windows. Nothing here writes to the cloud yet. The global runs are tasks 02 and 03.

## Run

```bash
pip install -r roads/requirements.txt
python roads/seed.py all                          # every box in boxes.yaml: data/roads/seed/<name>.parquet
python roads/seed.py summary --kind compare       # data/roads/levels.csv: km by class, level and surface
python roads/seed.py footers                      # data/roads/footers.json: segment rows per 10° block, footers only
python roads/sprout.py window chinko              # data/roads/sprout/chinko.tif
python roads/sprout.py compare chinko             # road cells, all-touched against line-centre
python roads/tiles.py window chinko --top-zoom 11 # data/roads/tiles/chinko-z11.pmtiles
python roads/render.py tiles data/roads/tiles/chinko-z11.pmtiles --zoom 8 --bbox 23 5.5 25 7.5
python roads/render.py lines waterberg --zoom 13 [--class unknown]
MAPBOX_ACCESS_TOKEN=pk... python roads/preview.py   # http://127.0.0.1:8765/preview.html: the sample archives on a Mapbox map
python -m pytest roads
```

`boxes.yaml` names the boxes. `compare` boxes are the ten sites of the 11 November 2026 dataset comparison, 0.3° on a side. `window` boxes are the 2° sample windows. Overture is read from its public bucket with DuckDB, and only the row groups whose bbox meets the box are read. Each read also lists the footers of all 128 files, which takes about 30 seconds. Imagery for the renders is Esri World Imagery, cached in `data/roads/imagery/`.

## Method

**Seed.** Overture segments with `subtype = road` and a kept class. Paths (`footway`, `path`, `steps`, `cycleway`, `bridleway`, `pedestrian`) are left out. Each row keeps the Overture `id`, `class`, `subclass`, `surface`, `level`, the OSM way ids from `sources`, the primary name, `bbox` and the geometry.

- `surface` is the value that covers most of the segment's length, from `road_surface` and its `between` ranges. `paved`, `paving_stones` and `metal` are paved; `unpaved`, `gravel` and `dirt` are unpaved; the rest of the length is unknown. Paved wins a tie, then unpaved.
- `level` comes from the class (recipe `roads.levels`). `unclassified` and `unknown` say nothing about the road, so they are minor when paved and tracks otherwise (`roads.by_surface`).

**Sprout.** The seed is burned onto EPSG:4326 at 0.00025°, aligned to (−180, 90), with GDAL `ALL_TOUCHED`. Roads are burned from the lowest level to the highest, and within a level from unknown to unpaved to paved, so a cell takes the highest level and the paved surface wins. Band 1 `level` is 0 (no mapped road) or 1–4. Band 2 `surface` is 0 unknown, 1 paved, 2 unpaved. There is no nodata. A COG with DEFLATE level 6, 512 blocks and no overviews, as `sprout/README.md` chose for cropland.

**Tiles.** 512 px palette PNGs. Below the top zoom, a pixel holds the share of the sprout cells under it that are road cells, in 8 shades spaced in √(share / full), and any road keeps the first shade. `full` is the share drawn darkest (`--full`). At the top zoom, a pixel holds `level × 4 + surface` of the highest road under it. A tile with no road is not stored. A parent's share is the mean of its four children's, an absent child counting as 0. The pyramid is `tiles/pyramid.py`, and the top-zoom share is `tiles/cropland.py fraction_tile`.

## Task 01 sample (17 November 2026)

Overture release `2026-10-21.0`. Laptop, DuckDB 2.0, GDAL 3.11.4, Python 3.14. Renders are in `data/roads/renders/` (not committed).

### Levels in the comparison boxes

Kilometres inside each 0.3° box, by level, and the share of length by surface.

| Box | Segments | Major km | Main km | Minor km | Track km | Paved % | Unpaved % | Unknown % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| kocho | 6 | 0 | 0 | 0 | 75 | 0 | 38 | 62 |
| fode | 12 | 0 | 19 | 0 | 56 | 0 | 25 | 75 |
| bon | 94 | 0 | 0 | 5 | 100 | 0 | 75 | 25 |
| fada | 206 | 0 | 68 | 34 | 130 | 0 | 86 | 14 |
| kruger | 207 | 0 | 53 | 8 | 327 | 15 | 79 | 7 |
| waterberg | 5,896 | 46 | 174 | 708 | 718 | 26 | 33 | 41 |
| suriname | 15 | 0 | 0 | 0 | 28 | 0 | 98 | 2 |
| pokola | 890 | 0 | 71 | 68 | 275 | 0 | 22 | 78 |
| br163 | 2,841 | 39 | 70 | 279 | 289 | 4 | 26 | 71 |
| rebiana | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

Rebiana's one segment ends outside the box: there is no mapped road in it.

Checked against Esri World Imagery at zoom 13 (whole boxes) and zoom 16 (sample segments):

- `unknown` (OSM `highway=road`) was minor in the parcel's first proposal. 189 of its 203 km are in the Waterberg box, all with no surface, and every sample there is a dirt track through bush or at a settlement edge. It now follows the `unclassified` rule: minor when paved, otherwise a track.
- `unclassified` with no surface (BR-163, Waterberg) is dirt side roads and farm tracks. Track is right.
- `residential` with no surface (Pokola) is town streets. Minor is right.

### Burn rule

Road cells in each window, all-touched against line-centre (GDAL's default, Bresenham).

| Window | Cells | All-touched road cells | Line-centre road cells | Ratio |
| --- | --- | --- | --- | --- |
| gauteng-waterberg | 64,000,000 | 4,744,142 | 3,752,764 | 1.26 |
| chinko | 64,000,000 | 32,904 | 23,786 | 1.38 |

At the top zoom the two look the same: the line-centre lines are 8-connected and a top-zoom pixel holds any road under it. All-touched is kept, so a line never passes between two cells it crosses. The road-cell share is a coverage of 28 m cells, not road area.

### Seed, sprout and tiles per window

| | gauteng-waterberg | chinko |
| --- | --- | --- |
| Seed rows | 715,036 | 138 |
| Road km in the window | 112,517 | 718 |
| Seed bytes | 92,178,339 | 157,141 |
| Seed wall time, peak memory | 66 s, 2.2 GB | 58 s, 2.2 GB |
| Sprout bytes (8,000 × 8,000 cells) | 3,825,322 | 182,295 |
| Sprout road cells: track, minor, main, major | 986,081; 3,008,723; 641,809; 107,529 | 20,743; 108; 12,053; 0 |
| Sprout wall time, peak memory | 3.7 s, 2.0 GB | 1.5 s, 1.3 GB |
| Tiles, top zoom 11: tiles, bytes | 262, 3,697,509 | 90, 82,295 |
| Tiles, top zoom 12: tiles, bytes | 856, 9,599,855 | 175, 167,325 |
| Largest tile, top zoom 11 | 74,953 (zoom 10) | 3,512 (zoom 9) |
| Tiles wall time, peak memory (top zoom 11) | 15.5 s, 2.2 GB | 6.5 s, 2.2 GB |

A seed read's wall time is mostly listing the footers of all 128 files. Seed bytes are 69% geometry, 15% Overture `id`, 11% `bbox`.

### Top zoom

Zoom 11 (about 38 m a pixel at the equator with 512 px tiles, against 28 m cells) draws continuous lines in both windows. Zoom 12 is a little sharper and 2.6 times the bytes in the dense window. Zoom 11 is proposed, the same as cropland version 3.

### Shades

Road share is small: a rural network fills 1–5% of cells, a city 20–40%. With shades spaced in √share up to a share of 1 (cropland's curve), 78% of the dense window's road pixels at zoom 4 fall in the two faintest shades. Spacing them in √(share / 0.25), so a share of 25% or more is the darkest shade, spreads them across all eight and keeps cities and rural networks apart (`render.py` comparison at zoom 6, `z6-full.png`). The whole share-0.25 archive is 3,460,034 bytes against 3,697,509.

| Zoom 4 road pixels per shade 1–8 | full share 1 | full share 0.25 |
| --- | --- | --- |
| gauteng-waterberg | 1,159; 604; 248; 121; 122; 6; 0; 0 | 234; 659; 457; 307; 186; 132; 64; 221 |
| chinko | 169; 0; 0; 0; 0; 0; 0; 0 | 130; 38; 1; 0; 0; 0; 0; 0 |
