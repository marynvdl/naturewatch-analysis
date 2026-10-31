# Tiles

How the pipelines fit together is [`docs/pipelines.md`](../docs/pipelines.md). This page is the cropland 2024 run. Version 1 (South Africa, XYZ PNG files) is first. The version 2 raster PMTiles sample is the last section.

Reads the sprout named in the cookbook recipe and writes XYZ tiles for zooms 0–12. The recipe is the catalog. This script reads it and does not edit it. There is no GitHub workflow for this step. The published run is a Google Batch job in `europe-west1`, started by hand. Batch deletes the virtual machine when the task ends.

The sprout is `gs://nature-watch-bucket/sprouts/cropland/2024/v1/cropland-2024-zaf.tif`. The tile prefix is `gs://nature-watch-tiles/cropland/2024/v1/`. The 2015 and 2019 crop tiles stay at `crop/`.

## What a tile contains

Each file is `{z}/{x}/{y}.png`, 256 pixels, XYZ. The pixel is an 8-bit gray value plus alpha:

- Gray `1` is cropland. Alpha is 255.
- Gray `0` is other land. Alpha is 255.
- Nodata, including land outside South Africa, is alpha 0. A tile that is only nodata is left out. A missing tile is empty.

Resampling is maximum, and nodata is left out of that maximum. One cropland pixel keeps a coarse cell. Colour is applied in the client. These files do not carry a display colour.

Zoom 12 is the top of the pyramid. At that zoom a pixel is about 34 m near 27°S. The sprout pixel is about 28 m. A local comparison on 22 October 2026, on an 8.9 km patch near Viljoenskroon, showed field shapes at zoom 12 and merged fields from zoom 11 down. Zoom 13 matched the raster more closely and was set aside. The map should use nearest resampling above zoom 12.

## Run

```bash
pip install -r cookbook/requirements.txt
python -m pytest tiles cookbook
python tiles/cropland.py submit
```

`submit` enables the Batch API if needed, copies this script and the recipe to `gs://nature-watch-bucket/scratch/tiles/cropland-2024/`, and submits a job named `tiles-cropland-2024-v1`. The machine is a spot `e2-standard-2` with a 30 GB disk. It uses the project's default compute service account. The image is `ghcr.io/osgeo/gdal:ubuntu-small-3.11.4`.

The task reads the sprout from the bucket, writes the tiles to the recipe path, then deletes the scratch copies. A laptop `run` refuses to do that work. `build --source <tif> --dest <dir>` writes tiles for a local file and does not upload them.

`submit` stops if the tile prefix already has objects. A run that stops during the upload leaves a partial prefix. Delete that prefix before submitting again.

## Cost for this step

Run on 22 October 2026 as Batch job `tiles-cropland-2024-v1` in `europe-west1`. Spot `e2-standard-2`. The virtual machine was removed when the task ended. No virtual machine was left running. The scratch copies were deleted.

The task ran for 937 seconds. The Python process took 933 seconds and peaked at 145,616,896 bytes. Zooms 0–12. 23,467 tiles were written and 18,353 empty tiles were left out. The tile bytes are 28,294,128.

| Zoom | Tiles |
| --- | --- |
| 0 | 1 |
| 1 | 1 |
| 2 | 1 |
| 3 | 1 |
| 4 | 2 |
| 5 | 4 |
| 6 | 10 |
| 7 | 28 |
| 8 | 93 |
| 9 | 322 |
| 10 | 1,186 |
| 11 | 4,477 |
| 12 | 17,341 |

Compute for 937 seconds at the spot price of about $0.04 an hour is under $0.02. Keeping 28 MB in `nature-watch-tiles` is under $0.01 a month. Reading the 39 MB sprout and writing the tiles inside Europe is a fraction of a cent.

## Version 2: one raster PMTiles archive (sample, task 04)

Status: local sample built and compared. Waiting for approval of one resampling. The global archive does not exist and nothing was uploaded.

A recipe with `tiles.scheme: pmtiles` writes one archive of class-value PNG tiles, zooms 0–12, with the `pmtiles` package `Writer` in tile id order. The GDAL PMTiles driver is not used. A recipe with `scheme: xyz` still writes the version 1 folder. The Batch `submit` and `run` commands refuse a pmtiles recipe until task 05 wires them.

```bash
pip install -r cookbook/requirements.txt -r tiles/requirements.txt
python -m pytest tiles cookbook
python tiles/cropland.py build --recipe cookbook/recipes/cropland-2024-v2.yaml \
  --source <sprout VRT or tif> --dest cropland.pmtiles --resampling q3 \
  [--window WEST SOUTH EAST NORTH] [--stats stats.json]
python tiles/cropland.py preview --dest <folder with max, q3 and nearest .pmtiles>
```

The recipe's `tiles.resampling` stays unset until a choice is approved. `--resampling` names one for a local sample and is refused if the recipe sets a different one. Allowed values: `max`, `q3`, `q2`, `nearest`, and `fraction` with a `tiles.threshold` (or `--threshold`) above 0 and below 1. `q2` was added after the first sample, at the request of the reviewer; the task text lists three.

### How the pyramid is built

Zoom 12 is warped from the source once per tile, with maximum resampling. Every lower zoom is built from the four child tiles of the zoom below, never from the source. A missing child, or a cell with no valid child pixel, is nodata and stays empty. A tile that is all nodata is not written. The same zoom 12 base is used for all three choices, so the comparison starts from one image. At zoom 12 a tile pixel is about 32 m near 33°S and a source pixel is about 23 × 28 m, so the choice at zoom 12 matters little.

| Choice | A parent cell is |
| --- | --- |
| `max` | the largest valid class among its four children. Exact. One cropland pixel survives at any zoom. |
| `q3` | the third quartile of the valid child classes, by nearest rank: the value at rank ceil(0.75 n) of n sorted values. For classes 0 and 1 that is cropland when more than a quarter of the valid child pixels are cropland (more than floor(n/4) of n). |
| `q2` | the median of the valid child classes, by nearest rank: cropland when more than floor(n/2) of n valid pixels are cropland (3 of 4, 2 of 3, 2 of 2). |
| `nearest` | the top left child pixel. Cropland on the other three is lost. |

A q3 of q3 values is an approximation of a q3 taken over the source pixels. It is checked below against a direct q3 from the source: a cell is cropland when more than a quarter of the valid source pixels under it are cropland, using GDAL average resampling on 0 and 1.

### Sample window

West 18.0, south −33.6, east 18.5, north −33.1 (EPSG:4326), Swartland and the Atlantic coast north of Cape Town, 2,000 × 2,000 source pixels, cut from the v2 sprout (`tiles/30S_010E.tif`, through the sprout VRT). It has wheat fields (709,089 cropland source pixels, about 458 km²), other land, and sea. The sprout stores sea as class 0, because GLAD writes 0 for everything that is not cropland.

Nodata. There is no nodata inside a South Africa window: nodata is only a 10° tile GLAD does not provide, and none is near land here. In the sample, nodata is everything outside the window, which shows as clear at every zoom, including the edge tiles where it meets cropland. The nodata rules are covered by pytest on synthetic rasters.

Zoom 12 has 56 tiles in the window. Zoom 12 draws 488,256 cropland pixels, about 498 km² against 458 km² in the source, so zoom 12 already overstates by about 9% under maximum resampling.

### Result per sample

Local run on 31 October 2026, laptop, GDAL 3.11.4, Python 3.14, one process per sample. Wall time includes cutting the window. Peak memory is the process maximum resident size; the q3 and q2 runs also hold the comparison with the source.

| | `max` | `q3` | `q2` | `nearest` |
| --- | --- | --- | --- | --- |
| Archive bytes | 88,750 | 90,552 | 91,561 | 92,777 |
| Tiles (zooms 0–12) | 98 | 98 | 98 | 98 |
| Wall time | 0.70 s | 0.78 s | 0.81 s | 0.67 s |
| Peak memory | 113,852,416 | 150,667,264 | 160,153,600 | 110,346,240 |
| Zoom 12 cropland pixels still shown at zoom 8 | 100.0% | 97.5% | 74.5% | 82.3% |
| at zoom 6 | 100.0% | 96.6% | 48.0% | 73.8% |
| at zoom 4 | 100.0% | 96.4% | 6.0% | 65.3% |

`q2` is the median by nearest rank: a parent cell is cropland when more than half of its valid child pixels are cropland. Tiles per zoom 0–12: 1, 1, 1, 1, 1, 1, 1, 1, 4, 4, 6, 20, 56. The tile count is the same for every choice because the tiles are the same; only the pixels differ. The archive is about 7% smaller than the sum of its PNG files for `max`, because the writer stores identical tiles once.

"Still shown" is the share of zoom 12 cropland pixels whose cell at that zoom is cropland.

Cropland area kept compared with zoom 12 (cells × 4^(12 − zoom) divided by zoom 12 cropland pixels; 1.00 is the same area):

| Zoom | `max` | `q3` | `q2` | `nearest` |
| --- | --- | --- | --- | --- |
| 10 | 1.16 | 1.08 | 0.93 | 1.00 |
| 8 | 1.46 | 1.22 | 0.79 | 0.98 |
| 6 | 2.16 | 1.45 | 0.53 | 1.03 |
| 4 | 4.83 | 2.28 | 0.13 | 1.48 |

Reading it. `max` never loses a field but draws more cropland at each coarser zoom, 4.8 times the area at zoom 4. `nearest` keeps about the right total area, but 18% of the pixels at zoom 8 and 35% at zoom 4 are gone, and what stays is a random subset (the 1.48 at zoom 4 is from only 11 cells). `q3` loses 2.5% to 3.6% of the pixels and overdraws less than `max`.

`q2` does not sit between `q3` and `nearest`. It is close to `nearest` at zoom 10 and 8, then it erodes. A median of medians keeps the majority class at every step, so a window that is 28% cropland loses it: at zoom 6 about half the area, at zoom 4 about an eighth (one cell). A median is a poor choice for a pyramid when the class of interest is the minority. Between `q3` and `nearest` a lower threshold than 0.25 but above 0 (for example 0.125) would keep more than `q2`, but it is the same idea as `max` with a tail cut, and it has not been built.

### q3 and q2 against the source

A q3 or q2 of child values against the same quantile taken from the source pixels (cropland when more than 25% or 50% of the valid source pixels under the cell are cropland).

| Zoom | q3 pyramid cells | q3 from source | q3 cells that differ | q2 pyramid cells | q2 from source | q2 cells that differ |
| --- | --- | --- | --- | --- | --- | --- |
| 8 | 2,318 | 2,188 | 146 of 10,120 (1.4%) | 1,510 | 1,853 | 347 (3.4%) |
| 6 | 173 | 153 | 16 of 621 (2.6%) | 63 | 115 | 53 (8.5%) |
| 4 | 17 | 8 | 1 of 30 (3.3%) | 1 | 5 | 5 (16.7%) |

The pyramid q3 draws a little more cropland than a direct q3 (6% more cells at zoom 8, 13% at zoom 6). The pyramid q2 draws less than a direct q2 (19% fewer cells at zoom 8, 45% fewer at zoom 6), so q2 is the choice where building from children departs most from the source. At zoom 4 the counts are too small to carry weight.

### Average, then threshold (`fraction`)

`q3` and `q2` threshold at every level and pass 0 and 1 up, so each step rounds away some of the minority class. `fraction` carries the cropland share instead. A zoom 12 cell holds the share of valid source pixels under it (GDAL average resampling, nodata left out). A parent holds the weighted mean of its four children, weighted by their valid zoom 12 cells, so at every zoom it is the share of valid zoom 12 cells under the cell. A threshold is applied once, when the PNG is written: cropland when the share is above it. The PNGs stay binary class values. `--resampling fraction --threshold 0.35` builds it, and pytest checks that the pyramid share matches a share taken from the source.

Every sample was judged against the same truth, the cropland share from the source at each zoom. True cropland area in the window is 465 km². Per zoom: shown area over true area / recall (share of true cropland area under a drawn cell) / precision (share of drawn area that is cropland). `max` and `q3` get their recall by drawing too much. Areas are exact cell areas at each latitude.

| Choice | Bytes | Zoom 12 | Zoom 8 | Zoom 6 | Zoom 4 |
| --- | --- | --- | --- | --- | --- |
| `max` | 88,750 | 1.08 / 100% / 92% | 1.57 / 100% / 64% | 2.25 / 100% / 44% | 3.85 / 100% / 26% |
| `q3` | 90,552 | 1.08 / 100% / 92% | 1.30 / 98% / 75% | 1.51 / 97% / 64% | 1.82 / 97% / 54% |
| `q2` | 91,561 | 1.08 / 100% / 92% | 0.85 / 78% / 92% | 0.55 / 49% / 89% | 0.11 / 9% / 87% |
| `nearest` | 92,777 | 1.08 / 100% / 92% | 1.05 / 84% / 79% | 1.08 / 72% / 67% | 1.18 / 62% / 52% |
| `fraction` > 0.1 | 93,342 | 1.06 / 100% / 94% | 1.36 / 99% / 73% | 1.58 / 99% / 62% | 1.71 / 97% / 57% |
| `fraction` > 0.25 | 96,161 | 1.04 / 99% / 96% | 1.23 / 97% / 79% | 1.38 / 95% / 69% | 1.61 / 95% / 59% |
| `fraction` > 0.35 | 97,350 | 1.02 / 99% / 97% | 1.16 / 95% / 82% | 1.28 / 92% / 72% | 1.39 / 89% / 64% |
| `fraction` > 0.5 | 98,513 | 1.00 / 98% / 98% | 1.04 / 90% / 86% | 1.04 / 82% / 79% | 0.96 / 71% / 73% |

Reading it.
- At every threshold `fraction` is at least as good as `nearest` on both recall and precision at zooms 8, 6 and 4. `nearest` at zoom 6 draws 1.08 of the true area, but only 72% of the true cropland is under a drawn cell and 67% of what is drawn is cropland.
- `fraction` > 0.5 is the closest to the true area: 1.00 at zoom 12, 1.04 at zooms 8 and 6, 0.96 at zoom 4. It gives up recall to get it: 90%, 82%, 71%.
- `fraction` > 0.35 is the middle choice: 1.16 / 1.28 / 1.39 of the true area at zooms 8 / 6 / 4, recall 95% / 92% / 89%, precision 82% / 72% / 64%.
- `q3` keeps 97% recall at every zoom but its precision falls to 54% at zoom 4, so about half of what it draws is not cropland. `fraction` > 0.1 behaves like it.
- `q2` loses recall quickly (9% at zoom 4) and is not worth considering.
- Because the share is carried, the threshold can change per zoom, for example lower at coarse zooms where cells are mixed. That is a recipe field, not a different method.
- The archive is 5% to 11% larger than `max` (93–99 KB against 89 KB), since more cells are mixed. Storing the share in the gray value at coarse zooms is another option. It changes the "class value" encoding and is not built.
- Cost: no more than the others on this window. The extra work is one weighted mean per parent. Memory per in-flight tile roughly triples. The zoom 12 warp and PNG encoding still dominate.

The wall times and peak memory in the table above were taken before this section and without the comparison pass. Runs of `max`, `q3`, `q2` and `nearest` now also build a share pyramid for the comparison, so their latest peak memory (208–239 MB) is higher than in that table. The `fraction` runs peaked at 203–213 MB with a wall time of 0.83–0.85 s.

### Does Mapbox GL JS read it?

Yes, from `mapbox-gl` 3.23.0, as a raster source whose `url` ends in `.pmtiles`. Checked on 31 October 2026 with a public token from `MAPBOX_ACCESS_TOKEN`, in a browser, on `max.pmtiles` served with Range requests, using a blank style and `{type: "raster", url: ".../max.pmtiles"}`. The window drew and no error was raised in 3.23.0, 3.24.0, 3.26.0, 3.28.0, 3.30.0, 3.32.0 and 3.34.1.

| `mapbox-gl` | Raster `.pmtiles` source |
| --- | --- |
| 3.11.0 (the app's lock file) | fails: `Unexpected token 'P', "PMTiles..." is not valid JSON` |
| 3.21.0, 3.22.0 | fails with the same error |
| 3.23.0 and later | works |

The app's `package.json` allows `^3.11.0` and the lock file says 3.11.0, while `node_modules` holds 3.34.1. The app task must pin `mapbox-gl` at 3.23.0 or later. An earlier note here said Mapbox could not read the archive. That was wrong. It came from searching the library for `addProtocol`, which MapLibre uses and Mapbox does not. Mapbox 3.23 and later has a built-in PMTiles tile provider instead.

### Estimate for the global archive

Inputs. The 223 seed tiles hold 3,852,424 zoom 12 tiles in total (counted from the recipe tiles with the same tile grid), and 5.14 million with the lower zooms (× 4/3). Sea inside a 10° tile is class 0, so those tiles are written, not skipped. The sprout records give 21.2 billion cropland source pixels worldwide. In the sample one cropland-bearing zoom 12 tile holds about 13,600 zoom 12 cropland pixels, a mixed tile is about 1.14 KB, and an all-class-0 tile is about 0.5 KB and is stored once.

Assumptions.
- Tiles: all 3.85 million zoom 12 tiles in the 223 tiles and their parents, about 5.1 million.
- Bytes: most tiles are uniform class 0 (sea, desert, forest) and are stored once. Mixed tiles (cropland and other land) are the bytes. Low: 1.0 million mixed tiles at 1.14 KB, from the sample's cropland density, plus lower zooms. High: every tile as heavy as the version 1 average of 1.2 KB with no sharing. Version 1 was 28.3 MB for 23,467 tiles. Global cropland is spread thinly in many places, so the sample's density is the low end.
- Time: the version 1 job did about 25 tiles a second warping every tile at every zoom on a spot `e2-standard-2`. Here only zoom 12 is warped, and the sample did 56 zoom 12 tiles in about 0.2 s from a local file. A read through the VRT from a private bucket is slower, so 25 to 100 zoom 12 tiles a second, plus PNG encoding at about 250 tiles a second and the lower zooms.

| | Low | High |
| --- | --- | --- |
| Tiles | 5.1 million | 5.1 million |
| Archive bytes | 1.5 GB | 6 GB |
| Single machine, hours | about 18 | about 50 |

Hours are one process. With the zoom 12 work split by 10° tile across 8 tasks it is about 2–6 hours, but one archive then needs a merge step, which is task 05's design. The low figure is 3.85 million ÷ 100 per second (about 11 hours) plus about 6 hours of encoding and the lower zooms. The high figure is 3.85 million ÷ 25 per second (about 43 hours) plus the same.

Memory. The sample keeps every tile in memory. That works for 98 tiles and not for 5 million. The global run has to build one zoom 8 block at a time (a block is the 16 × 16 zoom 12 tiles under one zoom 8 tile) and spill its PNG bytes, then write in tile id order. That is task 05.

Cost. Spot `e2-standard-2` is about $0.03–0.04 an hour, so 18–50 hours is about $0.6–2. Reading the sprout inside the EU region has no egress. 1.5–6 GB in `nature-watch-tiles` is about $0.03–0.12 a month. This is an estimate and needs approval before task 05.

### Cost of the sample

No cloud cost. The sample ran on the laptop and stayed there. It read one 3.2 MB sprout tile and the VRT from `nature-watch-bucket` for a small read (a few cents at most of egress, under 20 MB) and the 223 sprout records (73 KB). Nothing was uploaded to `nature-watch-tiles`. No virtual machine ran.

### Preview

```bash
python tiles/cropland.py preview --dest data/tiles/sample/out
```

Open `http://127.0.0.1:8766/preview.html`. Four maps move together: `max`, `q3`, `q2`, `nearest`. The server answers HTTP Range requests (206), like the places preview. A plain `python -m http.server` often returns the whole file. Colour is applied in the page: class 1 orange, class 0 faint green, nodata clear. The basemap is Esri World Street Map, for a local look only. Cancel with Ctrl+C.

### Approval

Not yet given. When one resampling is approved, set `tiles.resampling` on `cookbook/recipes/cropland-2024-v2.yaml` and record the choice here. Task 05 does not start before then.
