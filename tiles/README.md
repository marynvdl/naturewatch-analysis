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

Status: task 04 complete. **`q3`** is approved and set on `cookbook/recipes/cropland-2024-v2.yaml` (2 November 2026). Local samples only; the global archive does not exist and nothing was uploaded. Task 05 may start after explicit approval of its Batch estimate.

A recipe with `tiles.scheme: pmtiles` writes one archive of class-value PNG tiles, zooms 0–12, with the `pmtiles` package `Writer` in tile id order. The GDAL PMTiles driver is not used. A recipe with `scheme: xyz` still writes the version 1 folder. The version 1 `submit` and `run` commands refuse a pmtiles recipe. The global run is `tiles/global_cropland.py`, described in the last section.

```bash
pip install -r cookbook/requirements.txt -r tiles/requirements.txt
python -m pytest tiles cookbook
python tiles/cropland.py build --recipe cookbook/recipes/cropland-2024-v2.yaml \
  --source <sprout VRT or tif> --dest cropland.pmtiles --resampling q3 \
  [--window WEST SOUTH EAST NORTH] [--stats stats.json]
python tiles/cropland.py preview --dest <folder with max, q3 and nearest .pmtiles>
```

The recipe sets **`tiles.resampling: q3`**. `--resampling` still names one for a local comparison sample and is refused if it differs from the recipe. Allowed values: `max`, `q3`, `q2`, `nearest`, and `fraction` with a `tiles.threshold` (or `--threshold`) above 0 and below 1. `q2` was added after the first sample, at the request of the reviewer; the task text lists three.

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
- From a threshold of 0.25 up, `fraction` is at least as good as `nearest` on both recall and precision at zooms 8, 6 and 4. At 0.1 its precision is lower (73% against 79% at zoom 8). `nearest` at zoom 6 draws 1.08 of the true area, but only 72% of the true cropland is under a drawn cell and 67% of what is drawn is cropland.
- `fraction` > 0.5 is the closest to the true area: 1.00 at zoom 12, 1.04 at zooms 8 and 6, 0.96 at zoom 4. It gives up recall to get it: 90%, 82%, 71%.
- `fraction` > 0.35 is the middle choice: 1.16 / 1.28 / 1.39 of the true area at zooms 8 / 6 / 4, recall 95% / 92% / 89%, precision 82% / 72% / 64%.
- `q3` keeps 97% recall at every zoom but its precision falls to 54% at zoom 4, so about half of what it draws is not cropland. `fraction` > 0.1 behaves like it.
- `q2` loses recall quickly (9% at zoom 4) and is not worth considering.
- Because the share is carried, the threshold can change per zoom, for example lower at coarse zooms where cells are mixed. That is a recipe field, not a different method.
- The archive is 5% to 11% larger than `max` (93–99 KB against 89 KB), since more cells are mixed. Storing the share in the gray value at coarse zooms is another option. It changes the "class value" encoding and is not built.
- Cost: no more than the others on this window. The extra work is one weighted mean per parent. Memory per in-flight tile roughly triples. The zoom 12 warp and PNG encoding still dominate.

The wall times and peak memory in the table above were taken before this section and without the comparison pass. Runs of `max`, `q3`, `q2` and `nearest` now also build a share pyramid for the comparison, so their latest peak memory (208–239 MB) is higher than in that table. The `fraction` runs peaked at 203–213 MB with a wall time of 0.83–0.85 s.

### Share archive and hybrid display (`share`, `hybrid-10`, `hybrid-11`)

One more sample encoding, plus preview panes that compare it to the binary choices.

**Build.** `--resampling share` writes the cropland **share** in each cell (gray 0–255 = round(share × 255), alpha 0 = nodata). Metadata `encoding: cropland-share`. Same average-up pyramid as `fraction`, without thresholding. On the Swartland window: 98 tiles, **318,665 bytes**, 0.82 s, peak 204 MB. The file is larger than `max` because PNGs hold many distinct gray values, not only 0 and 1.

**Preview.** After `share.pmtiles` exists, the preview adds virtual panes `hybrid-10` and `hybrid-11`. They read the same archive; paint is applied in the page:

| Pane | Paint |
| --- | --- |
| `share` | Orange opacity = share everywhere (fully graded). |
| `hybrid-10` | Map zoom **&lt; 10**: opacity = share. Map zoom **≥ 10**: binary (orange if share &gt; 0.5, faint green if 0 &lt; share ≤ 0.5). |
| `hybrid-11` | Same with cutoff **11**. |

Zoom the maps together and watch a hybrid pane switch from soft fields to crisp boundaries. Example:

`http://127.0.0.1:8766/preview.html?show=max,fraction-0.5,share,hybrid-10`

**Stats in `share.json`.** `hybrid_fidelity` scores each **tile zoom** as if display used graded below `--hybrid-binary-zoom` (default 10) and binary above (threshold 0.5). That is slightly different from the preview, which keys off **map zoom** so you can see the switch while panning. On this window with `hybrid_binary_zoom: 10`:

| Tile zoom | Hybrid (graded below 10, binary at 10+) shown/true · recall · precision | Binary `fraction` &gt; 0.5 at same zoom |
| --- | --- | --- |
| 12 | 1.00 · 98% · 98% | 1.00 · 98% · 98% |
| 8 | 1.00 · 100% · 100% | 1.04 · 90% · 86% |
| 6 | 1.00 · 100% · 100% | 1.04 · 82% · 79% |
| 4 | 1.00 · 100% · 100% | 0.96 · 71% · 73% |

Below zoom 10, hybrid keeps the full cropland signal with honest opacity (recall 100%, shown area equals true area). At zoom 12 it matches a 0.5 binary cut. Task 07 would implement the same rules in Mapbox paint (`raster-opacity` from band values below `{zoom}` N, threshold above).

This is a **sample only**. The global recipe still has `encoding: class-value`. Approving hybrid for production means a new encoding in the cookbook and app styling, not only `tiles.resampling`.

### Limpopo / Mpumalanga evaluation (option 2)

Built on 2 November 2026 for resampling approval (**`q3`** chosen). Window west 27.0, south −25.5, east 32.5, north −22.0 (EPSG:4326). Sprout tiles `20S_020E`, `20S_030E`, `30S_020E`, `30S_030E` under `data/tiles/limpopo/src/`. Outputs in `data/tiles/limpopo/out/`. Not uploaded. No cloud compute; sprout read from the bucket to this machine (~35 MB).

| Archive | Tiles | Bytes | Wall | Peak memory | Streaming |
| --- | --- | --- | --- | --- | --- |
| `q3.pmtiles` | 3,858 | 3,906,097 | 34 s | 448,561,152 | yes |
| `fraction-0.5.pmtiles` | 3,858 | 3,755,972 | 36 s | 447,873,024 | yes |
| `max.pmtiles` | 3,858 | 3,940,680 | 35 s | 433,979,392 | yes |
| `nearest.pmtiles` | 3,858 | 3,882,986 | 34 s | 434,388,992 | yes |
| `fraction-0.35.pmtiles` | 3,858 | 3,875,211 | 39 s | 433,291,264 | yes |

Zoom 12 tiles in the window: about 2,835. Builds use the disk cache when zoom 12 count exceeds 120 tiles.

**Preview**

```bash
python tiles/cropland.py preview --dest data/tiles/limpopo/out --port 8767
```

Open `http://127.0.0.1:8767/preview.html?show=q3,fraction-0.35,max,nearest` for the four-way comparison (`fraction-0.35` between `max` and `q3`). Default `preview.html` lists all five archives in the folder.

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

**`q3`**, approved 2 November 2026. Compared on the Swartland window (`max`, `q3`, `q2`, `nearest`, and optional `fraction` thresholds) and on a Limpopo / Mpumalanga window (`q3`, `max`, `nearest`, `fraction-0.35`, `fraction-0.5`). **`max`** kept too much cropland at coarse zoom; **`nearest`** dropped too much; **`fraction`** at 0.5 was closer to true area but lost small patches. **`q3`** balanced recall and overdraw for map use. Recorded on the recipe as `tiles.resampling: q3`. Task 05 still needs its own cost approval before any global Batch run.

## Global run: sharded PMTiles (task 05)

Status: **done.** The archive is published at `gs://nature-watch-tiles/cropland/2024/v2/cropland.pmtiles` (run record below). Written by Batch jobs `tiles-cropland-2024-v2-shards` and `tiles-cropland-2024-v2-merge`. It waits for approval of the estimate below. Nothing was uploaded and no virtual machine ran.

Code: `tiles/pyramid.py` (planning, one shard, merge; knows nothing about cropland), `tiles/global_cropland.py` (binds a recipe, storage and Batch to it), tests in `tiles/test_global.py`. Another year is a new recipe. Another layer type is a new `render`, `reduce`, `encode` and `decode` in place of `layer()`.

### Design

- **Refuses** to start when `tiles.resampling` is unset, or is not a class-value method (`max`, `q3`, `q2`, `nearest`). The recipe says `q3`.
- **Shards.** One per zoom 5 tile that touches a source tile. For the 223 tiles that is **302 shards** (`plan` prints it), at most 4 source tiles each and at most 16,384 zoom 12 tiles. Shard names are `5-<x>-<y>`.
- **One shard task.** Copies its source COGs from `sprout.tiles_path` to local disk, builds a VRT over them, then walks its subtree depth first: zoom 12 is warped from the source, every lower zoom down to 5 is reduced from its four children with `q3`. Memory holds one path of arrays and the PNG bytes of the shard, not a zoom level. The zoom 12 warp is the same `render_tile` the sample used.
- **Shard output.** A PMTiles file `5-<x>-<y>.pmtiles` and then a record `5-<x>-<y>.json`, in `gs://nature-watch-bucket/scratch/tiles/cropland-2024-v2/shards/`. The record is written last, so a record means a finished shard. It holds the tile counts per zoom, bytes, wall time, peak memory and a hash of the settings (recipe id, resampling, max zoom, shard zoom, sprout path, code version).
- **Resume.** A task skips a shard whose record has the current settings hash and whose file size matches the record. A changed resampling, a truncated file or a missing record is rebuilt. Batch retries a task 3 times and retries a spot preemption (exit 50001) separately.
- **Merge.** One task. Refuses to run when any shard is missing or not current, and names them. It reads the zoom 5 root tile of every shard, builds zooms 0–4 from them with the same `q3`, then writes the archive with `Writer`.
- **Order and uniqueness.** PMTiles tile ids order zoom first, then along a Hilbert curve. So the archive is zooms 0–4 from the merge, then zoom 5 across all shards, then zoom 6 across all shards, and so on. Within one zoom, the tiles under one zoom 5 tile have consecutive ids and shards are walked in zoom 5 id order, so no sort is needed. The merge does not assume this. It stops if a tile id is not strictly after the one before, if a tile at zoom 5 or above comes from a shard that is not its ancestor (so it has exactly one shard), or if a shard gives different counts per zoom than its record. Tests cover shards in the wrong order, a tile written twice, and a tile in the wrong shard. The sharded archive is byte-identical, tile by tile, to a single-process `build_pmtiles` of the same sources (test `test_sharded_archive_equals_a_single_process_build`).
- **Dedup.** The `pmtiles` `Writer` stores one body for identical tiles (sea, desert), keyed by Python's 64-bit `hash` of the bytes with no equality check. A collision among about a million distinct tiles has a chance near 3 in 100 million. Accepted.
- **Check against the sprout.** After the archive is built, the merge task rebuilds 12 random zoom 8 subtrees (341 tiles each, 256 of them warped) straight from the sprout VRT and compares every tile, present or absent, byte for byte with the archive. Any difference stops the task before upload.
- **Upload.** Archive to `gs://nature-watch-tiles/cropland/2024/v2/cropland.pmtiles`, refused if it already exists. Then `Cache-Control: public, max-age=3600` is set, which is what the version 1 PNG objects serve (they have no explicit setting; the bucket default for public objects gives that). Then a Range request on the public URL must answer **206**, `Content-Range`, and start with `PMTiles`. The task fails if not.
- **Scratch.** The merge task deletes `shards/` after a successful upload. `code/` (the scripts, recipe and `pmtiles` wheel, about 100 KB) stays under the scratch prefix until someone removes it.
- **Pilot.** `submit --pilot N` runs shard N alone. The Batch container, its Python and the bootstrap (numpy from `apt`, `pmtiles` from a wheel copied to scratch) are **not yet tested on the real image** because Docker is not running here. A one-shard pilot costs a few cents and proves the image before the 302-task job.

### Commands

```bash
python tiles/global_cropland.py plan --recipe cookbook/recipes/cropland-2024-v2.yaml
python tiles/global_cropland.py submit --pilot 0              # writes the job file, prints status and cancel commands, submits nothing
python tiles/global_cropland.py submit --pilot 0 --approved   # after approval: one shard task
python tiles/global_cropland.py submit --approved             # after approval: 302 shard tasks, 8 at a time
python tiles/global_cropland.py submit --merge --approved     # after the shard job succeeded
python tiles/global_cropland.py local --sources <folder of sprout COGs> --work <dir> --top-zoom 10   # no cloud
```

`submit` without `--approved` only writes `data/tiles/cropland-2024-v2/global/<job>.json` and prints it. Job names are `tiles-cropland-2024-v2-shards`, `tiles-cropland-2024-v2-shards-pilot<N>` and `tiles-cropland-2024-v2-merge`.

### Local checks (no cloud)

Limpopo window, four sprout tiles read from disk (9 shards). Laptop, not an `e2-standard-2`.

| Run | Zoom 12 tiles | Wall | Peak memory |
| --- | --- | --- | --- |
| Shard `5-17-17` at zoom 12 | 696 (943 tiles with lower zooms) | 15.8 s | 127 MB |
| Four tiles, 9 shards, merge, top zoom 10 | 5,256 tiles, 4,821,675 bytes | 90 s | 175 MB |

The shard run is **about 44 zoom 12 tiles a second** including the lower zooms. The merge of nine shards took 0.1 s; the archive was 12 KB smaller than the shards added, so the loss from not sharing tile bodies across shards is small. `python -m pytest tiles cookbook` passes.

### Pilot shard (run)

Batch job `tiles-cropland-2024-v2-shards-pilot228`, submitted 2 November 2026 09:37 UTC, one spot `e2-standard-2` in `europe-west1`, 30 GB disk. State SUCCEEDED. The bootstrap on the real image worked (numpy was present, `pmtiles` loaded from the wheel).

Shard `5-23-14` (India, four source tiles, every zoom 12 tile present, the full 16,384):

| | |
| --- | --- |
| Tiles | 21,845 (16,384 at zoom 12) |
| Shard file | 28,575,868 bytes (1.31 KB a tile) |
| Shard wall time | 522 s, **31 zoom 12 tiles a second** on the e2-standard-2 |
| Job run duration | 532 s (VM start to success about 9 min 30 s, including about 41 s scheduling) |
| Peak memory | 156,803,072 bytes |
| Virtual machines left running | none (`gcloud compute instances list` is empty) |

The shard stays in scratch. A full run skips it, since its settings hash matches. This is a worst case shard for time: most shards are partly sea or outside the data. Time per task comes out inside the 25–44 tiles a second range in the estimate. Bytes per tile (1.31 KB) are above the sample's 1.1 KB; this shard is dense, so the 1.5–6 GB range stands as the range, with the top of it more likely than the bottom.

### Estimate for approval (approved 2 November 2026, now run)

| | |
| --- | --- |
| Shard tasks | 302, one per zoom 5 tile that touches the data |
| Machine | spot `e2-standard-2`, `europe-west1`, 30 GB `pd-balanced` boot disk (a task holds at most 4 source COGs, about 300 MB, and a shard under about 100 MB) |
| Parallelism | 8 tasks, 16 vCPU. `sprout/README.md` records 8 for its per-tile job. The task text says 20; the project's vCPU quota was not checked from here |
| Per task | 16,384 zoom 12 tiles at most, average about 12,700. At 25 to 44 tiles a second that is 5 to 8.5 minutes, plus about 3 minutes for the VM, the downloads and the bootstrap |
| Compute | about 40 to 60 task hours |
| Wall time | **about 5 to 7.5 hours**. One machine would be about 24 to 43 hours, the 18 to 50 hours above |
| Archive | 5.1 million tiles, **1.5 to 6 GB**, as estimated in task 04 (the Limpopo sample is about 1.1 KB a tile, the low end) |
| Merge | one spot `e2-standard-4`, 12 GB memory, **60 GB disk** (shards, the Writer's tile spool, and the output are each about the archive size, so up to 18 GB at 6 GB), 4 hour limit. About 30 to 60 minutes: download, merge, verify, upload |
| Cost | shards about $2 to $3 (60 VM hours at about $0.03 to $0.04), merge under $0.20, scratch storage a few cents for a day, archive $0.03 to $0.12 a month. No egress inside the region. Class A operations are in the hundreds |

The pilot measured 31 tiles a second on the real machine (44 on the laptop). If the pilot shard runs slower, the wall time scales with it.

### Run record (2 November 2026)

| | |
| --- | --- |
| Archive | `gs://nature-watch-tiles/cropland/2024/v2/cropland.pmtiles` |
| Archive bytes | **2,814,675,979** (2.62 GiB), inside the 1.5–6 GB estimate |
| Zoom range | 0–12 |
| Tiles | **5,084,649** (5,084,508 from shards, 141 at zooms 0–4), 1,357,087 distinct tile bodies |
| Resampling | `q3` |
| Shard job | `tiles-cropland-2024-v2-shards`, 302 tasks, 8 at a time, spot `e2-standard-2`, 09:57–12:57 UTC, **10,764 s (about 3 h)**, 302 succeeded. Sum of shard wall times 80,631 s (22.4 task hours), slowest shard 949 s, peak memory per shard at most 197,812,224 bytes. 3,808,915 zoom 12 tiles. Shard files 2,815,914,581 bytes in total |
| Merge job | `tiles-cropland-2024-v2-merge`, one spot `e2-standard-4`, job run duration 468 s, merge wall 463 s (fetch 82 s, build 96 s), **peak memory 1,407,229,952 bytes** |
| Checks | Tile ids strictly increasing across all zooms, every tile at zoom 5 and above from exactly one shard, per-zoom counts equal each shard's record. 12 zoom 8 subtrees (3,393 tiles) rebuilt from the sprout VRT matched the archive byte for byte |
| Range request | 206, `Content-Range: bytes 0-126/2814675979`, `Accept-Ranges: bytes` |
| Cache-Control | `public, max-age=3600` |
| Wall time end to end | about 3 h 10 min (pilot excluded) |
| Cost | about 24 VM hours of `e2-standard-2` and 8 minutes of `e2-standard-4`, about $1 (the estimate was $2–3). Storage of the archive about $0.06 a month. No egress |
| Pilot | `tiles-cropland-2024-v2-shards-pilot228`, shard `5-23-14`, 532 s |
| Scratch | `gs://nature-watch-bucket/scratch/tiles/cropland-2024-v2/` deleted, shards by the merge task, `code/` afterwards by hand |
| Virtual machines left running | none |

Faster than estimated: the real mean was 267 s a shard (the estimate assumed 5–11 minutes) because many shards are partly sea or outside the data. Bytes per tile were 0.55 KB on average.

Not done here: the app still reads the version 1 PNG template. Task 07 in naturewatch-app changes that, and needs `mapbox-gl` 3.23.0 or later.
