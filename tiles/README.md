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

## Version 3: graded share tiles that Safari reads exactly (tasks 09 and 09b)

Status: **published.** `gs://nature-watch-tiles/cropland/2024/v3/cropland.pmtiles` is live. The app still reads v2 until task 10. v1 and v2 were not overwritten.

Code: `tiles/transcode.py` (read v2, write a new encoding, compare, benchmark), `tiles/v3_check.py` and `tiles/v3_check/` (browser checks through Mapbox GL JS and PMTiles), tests in `tiles/test_transcode.py`. Local outputs are in `data/tiles/v3/` (not committed).

### Recommendation

| | Choice | Why |
| --- | --- | --- |
| Encoding | **Palette PNG, red 1 is cropland** (`palette-r1`): index 0 is RGB 0,0,0, index 1 is RGB 1,0,0, index 2 is nodata with tRNS alpha 0. 1 bit a pixel when a tile has no nodata, 2 bits when it has | Exact in Safari and Chrome through the real map path. **31% smaller than v2** (about 1.95 GB against 2.81 GB). The red channel is 0 or 1 as today, so `classRasterPaint` keeps `raster-color-mix [255, 0, 0, 0]` and task 10 only changes the URL. Fastest to write (0.85 s per 1,000 tiles against 9 s for RGB) |
| Route | **Transcode v2**, no COG rebuild | Every tile keeps its pixels (checks below). One spot VM in `europe-west1` reads the 2.8 GB archive inside the region, so no egress. About $0.10 |
| Tile size | **512 px**, chosen 7 November 2026 | 512 px tiles come from the same transcode (a 512 tile at zoom z is the four v2 tiles at z + 1), not a rebuild. With Chrome's request queue (below) they halve the time to load a view and cut a pan by about 4. They change the tile ids and the zoom range (0 to 11), so stage 2's "same tile ids" check becomes "every v2 tile is a quarter of one v3 tile", and task 10 sets `tileSize: 512` and `maxzoom: 11` |
| Cache header | `Cache-Control: public, max-age=31536000, immutable` on the v3 object | The path is versioned. Free. Helps a repeat visit. It does not fix the slow first load (measured below) |
| CORS | No change | No preflight in Chrome or Safari: a single `Range` is a safelisted header |
| `raster-resampling` | Keep `nearest` | `linear` adds blended colours at nodata edges and tile seams (below) |
| Root directory | Optional in stage 2: put zooms 0–7 in the root | Saves the leaf request for views at tile zoom 7 and below (the app opens at map zoom 5). Needs a small custom directory writer and a local check that Mapbox reads it |
| CDN | Follow-up task, outside this parcel | The only change that removes the Chrome queue for good is one URL per tile behind an edge cache (below) |

### Why Safari is wrong, through the real path

Safari 26.4 on macOS, Mapbox GL JS 3.34.1 (the app's `node_modules`), raster source `url` set to the archive, `tileSize: 256`, `maxzoom: 12`, nearest resampling, and `classRasterPaint`'s mix. The check page paints class 0 blue, class 1 red and leaves nodata on a green background, then reads every texel centre back from the WebGL canvas and compares it with the class from v2. 12 views, 42 tiles, 2,752,512 texels per archive: Zakouma at zooms 2, 6, 9, 12 and 13 (overzoomed 12), the Madagascar nodata edge at 6, 9 and 12, Swartland farmland at 9, 12 and 13, and open ocean at 6. Each view at zoom 12 or below sits on the corner of four tiles, so tile boundaries are inside it.

| Archive | Safari, macOS: texels wrong (map) | Safari: pixels wrong (PNG on a 2D canvas) | Chrome 157 headless: wrong (map, canvas) |
| --- | --- | --- | --- |
| v2 on the bucket | 392,031 | — | 0 |
| v2 tiles, local copy (byte-identical) | 392,031 | 422,780 | 0, 0 |
| `rgb-r1`, `rgb-r255` | 0 | 0 | 0, 0 |
| `rgba-r1`, `rgba-r255` | 0 | 0 | 0, 0 |
| `palette-r1`, `palette-r255` | 0 | 0 | 0, 0 |
| `palette-r1` and `rgb-r1` at 512 px | — | — | 0 |
| Safari on iPhone, Brave on iPhone | not tested (skipped 7 November 2026) | | |

Safari does not only add cropland. An all-class-0 tile gains 8,960 false cropland pixels (35 rows), as in the parcel, and the Swartland tile at zoom 12 loses 63,660 true cropland pixels. Nodata as alpha 0 (RGBA and palette) and as a tRNS colour key (RGB) is exact in both browsers, at the Madagascar edge and over the ocean. Safari ran with device pixel ratio 2.

### Every pixel kept

`transcode.py sample` drew a stratified sample from the v2 directories: per zoom 0 to 12, 100 random addressed tiles, 100 random distinct tile bodies and up to 100 tiles with a missing neighbour (the nodata edge), plus the 10 most shared bodies and every tile over Zakouma. 2,324 tiles (1,042 mixed, 821 partly nodata, 461 all class 0). Each candidate was transcoded and compared with v2 through GDAL's PNG driver, which is independent of the transcoder's decoder.

| Encoding | Tiles checked | Tiles that differ | Missing or extra tile ids |
| --- | --- | --- | --- |
| every candidate in the table below | 2,324 | **0** | 0 |

The browser view archive (3,133 tiles: all of zooms 0–6 and three boxes to zoom 12) also has 0 differences in every candidate. Re-encoding v2 as gray and alpha gives back all 2,324 v2 tiles byte for byte, which checks the decoder.

There is no all-class-1 tile in v2. Any uniform tile compresses to 499 bytes, and all 19 distinct 499-byte bodies were read: one is the all-zero tile, shared by 3,688,282 tiles (72.5% of the archive), and the rest are mixed. The tests cover all-one tiles on synthetic data.

### Size and speed

On the 2,324-tile sample. Laptop, Python 3.14, numpy and zlib level 9, filter 0 as in v2. Archive size is extrapolated per zoom from the distinct-body stratum and the v2 distinct bytes at that zoom, plus 3.4 MB of directories.

| Encoding | Sample bytes (v2 = 3,515,275) | Mixed tile | All-zero tile | Partly nodata tile | Write, s per 1,000 tiles | Archive, extrapolated |
| --- | --- | --- | --- | --- | --- | --- |
| v2 gray + alpha | 3,515,275 | 2,648 | 499 | 641 | 7.7 | 2.81 GB (actual) |
| `rgb-r1` / `rgb-r255` | 3,491,318 / 3,492,686 | 2,615 | 270 | 782 | 9.5 | 2.74 GB |
| `rgba-r1` / `rgba-r255` | 4,145,536 / 4,159,281 | 2,983–2,996 | 659 | 893 | 8.4–9.0 | 3.22 GB |
| **`palette-r1`** / `palette-r255` | **2,201,987** | **1,840** | **106** | **287** | **0.85** | **1.95 GB** |
| `rgb-r1-nodata2` (nodata as red 2) | 3,478,082 | 2,615 | 270 | 766 | 9.1 | 2.74 GB |

Peak memory of the benchmark, holding all 2,324 tiles as arrays, was 285 MB. A transcode process peaks at about 100 MB. Decoding on a 2D canvas in Safari took about 85–105 ms per 42 tiles for every encoding, so decode time does not separate them. `rgb-r1-nodata2` was only benchmarked: alpha 0 is safe, so nodata does not need a class value.

At 512 px the browser view archive was 2,686,525 bytes in 892 tiles against 2,726,617 bytes in 3,133 tiles at 256 px (`palette-r1`), so the size is about the same.

### Load time in the browser (baseline, v2)

Method. `v3_check.py load` opens `load.html` in headless Chrome 157 with a new profile each run (cold cache) and a Chrome net log. Blank style, so only the layer's own requests count. Mapbox GL JS 3.34.1, globe, 1024 × 768, Zakouma at map zoom 8 (tile zoom 9), then one pan of 614 px at the same zoom. Same view for both layers. Cropland is the live v2 archive; treeloss is the app's Global Forest Watch template for 2001–2024. Times are from adding the layer. Safari was opened by hand on two places it had not loaded before.

| | Cropland v2 (Chrome, 3 runs) | Treeloss (Chrome, 3 runs) | Cropland v2 (Safari) | Treeloss (Safari) |
| --- | --- | --- | --- | --- |
| First tile | 2.2–2.6 s | 0.27–1.5 s | 0.34 s, 0.96 s | 1.04 s, 21.6 s (origin miss) |
| View loaded | 6.8–10.0 s | 0.43–2.3 s | 1.04 s, 1.05 s | 1.8 s, 23.3 s |
| Pan loaded | 1.5–2.2 s | 0.38–1.3 s | 0.9 s, 1.3 s | 1.2 s, 1.8 s |
| Requests | 31: 23 answered, 8 cancelled by Mapbox | 28 (20 tiles, 8 for the pan) | | |
| Bytes | 75,974 | 209,020 | | |
| Status | 206 | 200 | | |
| OPTIONS preflights | 0 | 0 | | |

Cropland moves a third of treeloss's bytes and is still slower in Chrome. The net log shows why: the 16 KB header read took 1.6 s (cold connection), the leaf 0.36 s, and then the 20 tile requests all started at once but **finished one after another, about 0.3–0.5 s apart**, the last at 7.3 s.

What causes the queue. 20 parallel `fetch` Range reads of the archive from the same page finish together in 1.3–2.3 s with `cache: 'no-store'`, and one after another in 6.8–10.9 s with the default cache. Chrome's HTTP cache lets one request at a time work on a URL, and every tile of an archive is the same URL. The `pmtiles` library inside Mapbox's provider only turns the cache off on Chrome for Windows. A local server with a 300 ms delay gave 6.1 s for 20 reads whatever the `Cache-Control` (`no-store`, `max-age=3600` or `immutable`), so headers on the object do not change it. Safari does not queue: 20 reads finished in 0.95 s with the default cache. So Chrome, Edge and Brave on desktop and Android pay one GCS round trip (about 0.3 s) per tile in a row. Treeloss has a URL per tile behind CloudFront, so it never queues.

The same Mapbox load from a local server took 0.2 s to the first tile and 0.24 s to the whole view, so decoding and drawing are not the cost.

512 px against 256 px, Chrome, local server with 300 ms per request, Zakouma at map zoom 10:

| | 256 px (`palette-r1`) | 512 px (`palette-r1-512`) |
| --- | --- | --- |
| Tiles for the view, for the pan | 20, 12 | 9, 3 |
| First tile | 1.11 s | 1.16 s |
| View loaded | 6.96 s | 3.63 s |
| Pan loaded | 3.71 s | 0.94 s |

Directories. v2 has a 1,624-byte root and 376 leaves of about 9 KB (4,096 entries each); a view needs one leaf after the header. Smaller leaves only make that read smaller (1,024 entries: 2.3 KB). Putting the tile entries of zooms 0–7 in the root (4,170 entries) with pointers to 4,096-entry leaves for the rest gives a 13 KB root, inside the first 16 KB read, so a view at tile zoom 7 or below needs no leaf request. Zooms 0–8 would need a 35 KB root.

### `raster-resampling`

Same check, map between zooms (zoom + 0.37), every device pixel compared with the texel it falls in, Chrome:

| | Pixels compared | Different | Blended colours |
| --- | --- | --- | --- |
| `nearest` | 3,145,728 per archive | 28,908 (14,148 gained, 14,530 lost) | **0** |
| `linear` | 3,145,728 per archive | 27,667 (12,919 gained, 13,955 lost) | **820** |

The differences under `nearest` are at texel boundaries (about as many lost as gained) and are the same for every encoding, so they are where the check's own rounding and Mapbox's placement meet, not wrong classes. Under `linear` the step expression keeps the edge crisp between class 0 and 1, but where a texel is next to nodata the colour blends: a 257-pixel line along the ocean tile edge at zoom 6 and 520 pixels at zoom 2. At texel centres (integer zoom) both give 0 differences. Keep `nearest`. No artefact from `nearest` at fractional zoom was seen.

### Delivery: what to change and who does it

| Change | Effect | Cost | Who |
| --- | --- | --- | --- |
| Palette encoding (v3) | Fixes Safari; 31% fewer bytes | about $0.10 once, about $0.04 a month for the new object | stage 2 |
| 512 px tiles (v3) | About half the load time and a quarter of the pan time in Chromium | the same transcode, about $0.10–0.20 | stage 2 if approved, `tileSize` and `maxzoom` in task 10 |
| `immutable` Cache-Control on v3 | Repeat visits read from the browser cache | none | stage 2 |
| Zooms 0–7 in the root | One request fewer when the map opens | none | stage 2, optional |
| CDN in front of the bucket | Each queued request about 30–50 ms instead of about 300 ms | Cloud CDN needs an external HTTPS load balancer: about $18 a month for the forwarding rule, plus about $0.08 per GB served. Low traffic about $18–20, 100 GB a month about $26. Versioned paths need no invalidation | follow-up task, infrastructure |
| One URL per tile (for example a Cloudflare Worker reading the archive from R2, or `go-pmtiles serve` on Cloud Run) | Removes the Chrome queue and caches each tile at the edge, like treeloss | R2 storage about $0.03 a month, Workers free up to 100,000 requests a day, about $5 a month above. The catalog would use a `{z}/{x}/{y}` template | follow-up task, infrastructure and app |

Prices are list prices as I read them on 7 November 2026; check them before enabling anything.

### Graded display like Global Forest Watch (local comparison, 7 November 2026)

Question: can cropland be drawn with opacity that follows how much of a cell is cropland, as Global Forest Watch does for treeloss? GFW says that below zoom 13 loss pixels are shaded by the density of loss at 30 m, and that shading is uniform from zoom 13. v3 as planned cannot do this: it stores yes or no, and `q3` throws the share away when it builds the coarse zooms.

What was built (laptop, nothing in the cloud, nothing committed under `data/`): two 1° windows, Zakouma (22.1–23.1°E, 10–11°N) and Swartland (18.3–19.3°E, 32.8–33.8°S), from sprout tiles `20N_020E` and `30S_010E` (8.6 MB read). For each window: the planned v3 tiles (`q3`, `palette-r1`), and share tiles (`cropland.py --resampling share`, the task 04 average-up pyramid) rewritten by `transcode.py share` as palette PNGs. Index 0 is nodata, the red channel is the share on a 0–255 scale, in 15 steps (4-bit palette) or 255 steps (8-bit palette). Any share above 0 keeps at least the faintest step, so sparse cropland does not vanish. A local copy of the recipe with `resampling: share` was used; the repo recipe is unchanged.

| Window | Planned class tiles (palette) | Share, 15 steps | Share, 255 steps |
| --- | --- | --- | --- |
| Zakouma, 220 tiles | 155,308 B | 486,501 B (3.1x) | 882,487 B (5.7x) |
| Swartland, 259 tiles | 423,811 B | 1,344,691 B (3.2x) | 2,303,149 B (5.4x) |

Written at 1.1–2.9 s per window. Share to palette: nodata is exact; 15 steps are within 17 of 255 (7%), 255 steps within 1; 0 cropland pixels fell to 0 in either. Safari safety is the same as the class palette: palette PNG, red channel, no gray.

Paint (`tiles/v3_check/shade.html`, Mapbox GL JS 3.34.1, nearest, the app's orange): `raster-color` interpolates `raster-value` to an orange whose opacity is 0.2 + 0.75 × √share, with 0 transparent. That is one layer below zoom 12 and a second, solid layer from zoom 12 up (`maxzoom` and `minzoom` on the layers). Zoom-dependent colour inside one layer was not tried. Looked at in Chrome on Zakouma at zooms 6.5, 8 and 10.5, Swartland at 9.3, 11.2 and 12.2, on the Standard monochrome and satellite basemaps (screenshots in `data/tiles/v3/shots/`).

What it looks like. The solid class tiles paint a flat orange mass. The graded panes show where the cropland is dense and where it is broken up by roads, rivers, bush and field edges, and the edge of a cropland area fades instead of ending in a hard line. This is clearest at zoom 8–10; at zoom 12 and above both are solid, as GFW's are. 15 steps and 255 steps could not be told apart on screen at any zoom I looked at. Farmland as dense as Swartland saturates to nearly solid orange with this curve, so the gain there is mostly in the gaps; sparse areas such as Zakouma gain the most. Zoom 6 and below looks almost the same as the solid map, because cropland is a few pixels wide there.

Cost, first pass, extrapolating the window ratios to the 1.95 GB class archive: about 6 GB with 15 steps, about 11 GB with 255. The next section brings that down to about 3.4 GB. Windows are cropland-dense, and the global archive is mostly uniform tiles stored once, so these are upper estimates. Storage is about $0.12 and $0.22 a month. A map view downloads 3–5 times the bytes. It would be a rebuild from the sprout (v2 cost about $1 and 3 hours in Batch), not a transcode, so it needs its own approval, and 512 px tiles would come from the same build.

**Decision, 8 November 2026 (Maryn): option B2.** Share tiles, 8 shades spaced in √share, top zoom solid, 512 px tiles. Version 3 is therefore a rebuild from the sprout, not a transcode of v2, and it is task 09b ([`docs/tasks/task-09b-cropland-share-tiles-v3.md`](../docs/tasks/task-09b-cropland-share-tiles-v3.md)). Task 09 stage 2 as written (v3 equals v2 pixel for pixel) is not run.

### Version 3 published (task 09b, 8 November 2026)

Recipe `cookbook/recipes/cropland-2024-v3.yaml` (same seed and sprout as v2). Palette PNG, 512 px, zoom 0–11, 8 shades on a √share curve, solid from archive zoom 11. Path `gs://nature-watch-tiles/cropland/2024/v3/cropland.pmtiles`.

| | Result |
| --- | --- |
| Tiles | 1,275,734 (1,275,593 in shards, 141 upper-zoom) |
| Distinct tile bodies | 413,107 |
| Bytes | 3,462,749,057 (3.22 GiB) |
| Cache-Control | `public, max-age=31536000, immutable` |
| Range request | 206, `Content-Range: bytes 0-126/3462749057`, `Accept-Ranges: bytes` |
| CORS | GET from `http://localhost:5173` returns `Access-Control-Allow-Origin`. No preflight: Range is a safelisted header |
| Shard job | `tiles-cropland-2024-v3-shards`, 302 of 302 succeeded, 2 h 18 min wall, 17.2 task-hours, peak 294 MB |
| Merge job | `tiles-cropland-2024-v3-merge`, 318 s wall, peak 3.16 GB, 12 sprout subtrees (848 tiles) byte-identical |
| Verify job | `tiles-cropland-2024-v3-verify`, 10 of 10 succeeded, 37 min wall |
| Tile ids vs v2 | every v3 tile has v2 tiles under it; every v2 tile at zoom 1 and up sits in a v3 tile (1,275,734 and 5,084,648) |
| Nodata vs v2 | 1,275,734 tiles, 0 pixels wrong |
| Sprout sample | 16 zoom-8 roots, 1,360 tiles byte-identical. Opacity error on 15,835,407 graded cells: mean 1.2 points, worst 23.3 |
| Looked at | Chrome, `preview.html`, v2 live against this archive: Zakouma, Swartland, Sahel, Kazakhstan, Iowa, Punjab. Maryn: v3 looks really good |
| Cost | about $1–2 (pilot + shards + merge + verify). Storage about $0.07 a month. Copy to the public bucket inside the region, no egress |
| Virtual machines left running | none |
| Scratch | shards kept at `gs://nature-watch-bucket/scratch/tiles/cropland-2024-v3/` until deleted by hand |

Limitation recorded, not rebuilt: the share is rounded to gray 0–255 before a shade is chosen, so a cell under 1/510 cropland (one isolated pixel in a coarse cell) draws transparent instead of at about 20% opacity. That is the 23.3-point worst error. The mean is 1.2 points. Approved to publish as is on 8 November 2026.

The live app still reads v2. Task 10 in naturewatch-app points the layer at v3, sets `tileSize: 512`, `maxzoom: 11`, the paint and the legend text.

#### Cutting the share tiles down (8 November 2026)

3x was the cost of storing what the map never uses. Where the bytes are, in the 15-step archives: zoom 12 is 62% (302 of 486 KB at Zakouma, 824 of 1,344 KB at Swartland), zoom 11 a further 24%. Levers tried on the same two windows, each one added to the last:

| Lever | Zakouma | Swartland | Lossless? |
| --- | --- | --- | --- |
| 15 steps as built | 3.13x | 3.17x | |
| PNG row filters (best of none, sub, up, paeth) | 3.13x | 3.17x | no gain on palette indices, not used |
| **Top zoom stored solid (2 shades, from share 0.5)** | 1.94x | 2.01x | yes: the map draws zoom 12 solid anyway |
| **Shades spaced evenly in the square root of the share, 8 shades** | **1.73x** | **1.77x** | near: see below |
| 6 shades | 1.65x | 1.68x | |
| 3 shades | 1.34x | 1.36x | no: visible steps |
| 8 shades and drop share under 3% | 1.70x | 1.75x | not worth it |

Why square-root spacing: the opacity curve is 0.2 + 0.75 × √share, so equal steps in √share are equal steps in opacity. 15 evenly spaced shares wasted steps at the dense end and were too coarse at the sparse end. Opacity error against the unquantised share, below zoom 12 (`transcode.py share --steps N --curve sqrt --solid-zoom 12`):

| Shades | Mean error | 95th percentile | Worst |
| --- | --- | --- | --- |
| 15, linear | 0.5–0.9 points | 2.3–3.7 | 15.2 |
| 8, √ | 0.8–1.3 | 4.5–4.9 | 5.8 |
| 6, √ | 1.2–1.9 | 6.3–6.8 | 10.2 |
| 3, √ | 3.1–5.3 | 17–21 | 32.9 |

The 8-shade √ version is more accurate in the worst case than the 15-shade linear one, and 40% smaller. At the screenshot scale I could not tell 15, 8, 6 and 3 shades apart (Zakouma zoom 7.6, 8.6, 9.2 and 11; Swartland 9.3); the error numbers are the evidence.

Extrapolated to the 1.95 GB class archive: about 3.4 GB at 8 shades (3.2 GB at 6, 2.6 GB at 3). An upper estimate, for the reason above. The archive's own pyramid, not this sample's, decides the real figure. With 512 px tiles the top zoom is 11, so `--solid-zoom 11` (the tile that holds zoom 12 pixels); the size shape is the same.

Solid from zoom 10 with 6 shades (`--steps 5 --curve sqrt --solid-zoom 10`): 1.25x at Zakouma, 1.27x at Swartland, about 2.5 GB extrapolated. Shading then only shows below map zoom 10. What it gives up at zooms 10 and 11, measured as the opacity change if those zooms were drawn solid instead of graded: Zakouma 18 and 13 points on average (20% and 14% of cropland cells are drawn faint, under 60% opacity, in the graded version), Swartland 11 and 8 points (12% and 8%). Zoom 9 and below keep the full effect. Looked at on Zakouma at 10.4 on satellite: the edges of cropland areas are harder than the graded version, and the result reads like the solid tiles with a fainter fringe below zoom 10. This is the cheapest option that still shades the regional view. Compressing tile bodies again in the archive would not help: a PNG tile is already deflated.

What it changes besides cost. The layer would show a share, not a class, so the info text and legend change, and the colour curve becomes a design choice. Place figures are unaffected, since they come from the sprout.

### Stage 2 estimate (for approval)

512 px was chosen; the 256 px column is kept for comparison.

| | 256 px palette | 512 px palette (chosen) |
| --- | --- | --- |
| Where | one spot `e2-standard-2`, `europe-west1`, Batch, 30 GB disk | same |
| Read | v2 archive 2.81 GB inside the region | same |
| Work | 1,357,087 distinct bodies at about 2 ms each on the VM, about 1 hour, then compare every tile through GDAL, about 1.5 hours | about 1.27 million 512 tiles from 5.08 million children, about 2 hours, then compare, about 2 hours |
| Output | about 1.95 GB | about 1.9 GB |
| Memory | under 500 MB | under 1 GB |
| Cost | under $0.15 compute, no egress, about $0.04 a month storage while v2 stays | under $0.25 compute, the same storage |

The check before upload compares every tile, not a sample: tile ids (or quarters at 512), class values and nodata. The upload sets the cache header and checks a 206 Range answer and CORS from `http://localhost:5173` and the app origins. Nothing is written under `v2/`.

### Cost of stage 1

No compute in the cloud. Reads of the public v2 archive from this laptop: 3.4 MB of directories, 4.6 MB for the sample, 4.0 MB for the browser views, a few MB to look for all-one tiles, and under 1 MB in the load runs: under 20 MB, under $0.01 of egress. `gcloud storage ls` confirmed the v2 shards are gone (`scratch/tiles/` is empty) and that `cropland/2024/` holds `v1/` and `v2/` only. No virtual machine ran.

### Run it again

```bash
pip install -r cookbook/requirements.txt -r tiles/requirements.txt
python tiles/transcode.py inventory --out data/tiles/v3/inventory
python tiles/transcode.py sample --inventory data/tiles/v3/inventory.npy --out data/tiles/v3/sample
python tiles/transcode.py transcode --src data/tiles/v3/sample/v2-sample.pmtiles --dest data/tiles/v3/sample/palette-r1.pmtiles --encoding palette-r1
python tiles/transcode.py verify --a data/tiles/v3/sample/v2-sample.pmtiles --b data/tiles/v3/sample/palette-r1.pmtiles --encoding palette-r1
python tiles/transcode.py bench --src data/tiles/v3/sample/v2-sample.pmtiles --rows data/tiles/v3/sample/sample.json --encoding palette-r1 --encoding rgb-r1
python tiles/transcode.py views --inventory data/tiles/v3/inventory.npy --dest data/tiles/v3/views/gray-alpha.pmtiles \
  --box 21.8,9.2,23.4,10.8 --box 49.2,-18.9,50.8,-17.4 --box 18.0,-34.1,19.6,-32.6
# then transcode views/gray-alpha.pmtiles into views/<encoding>.pmtiles, and --tile-size 512 into views/<encoding>-512.pmtiles

export MAPBOX_ACCESS_TOKEN=<public token>
python tiles/v3_check.py chrome --page check                 # headless Chrome, all archives
python tiles/v3_check.py load --runs 3                        # cold load, cropland and treeloss, with net logs
python tiles/v3_check.py serve --lan                          # check.html and compare.html for Safari, an iPhone or Brave
```

The pages run on `http://localhost:5173` because the tile bucket's CORS allows that origin, so the live v2 archive can be the control. On a phone the address is the laptop's LAN address; the live archive is left out there, and the local v2 copy (byte-identical) is the control. `compare.html` shows every archive side by side with the app's paint and globe, with Standard monochrome, light, satellite and dark basemaps; `?single=1` is one map with a picker, for a phone. Results go to `data/tiles/v3/results.jsonl`.
