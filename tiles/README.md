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

The recipe's `tiles.resampling` stays unset until a choice is approved. `--resampling` names one for a local sample and is refused if the recipe sets a different one. Allowed values: `max`, `q3`, `nearest`.

### How the pyramid is built

Zoom 12 is warped from the source once per tile, with maximum resampling. Every lower zoom is built from the four child tiles of the zoom below, never from the source. A missing child, or a cell with no valid child pixel, is nodata and stays empty. A tile that is all nodata is not written. The same zoom 12 base is used for all three choices, so the comparison starts from one image. At zoom 12 a tile pixel is about 32 m near 33°S and a source pixel is about 23 × 28 m, so the choice at zoom 12 matters little.

| Choice | A parent cell is |
| --- | --- |
| `max` | the largest valid class among its four children. Exact. One cropland pixel survives at any zoom. |
| `q3` | the third quartile of the valid child classes, by nearest rank: the value at rank ceil(0.75 n) of n sorted values. For classes 0 and 1 that is cropland when more than a quarter of the valid child pixels are cropland (more than floor(n/4) of n). |
| `nearest` | the top left child pixel. Cropland on the other three is lost. |

A q3 of q3 values is an approximation of a q3 taken over the source pixels. It is checked below against a direct q3 from the source: a cell is cropland when more than a quarter of the valid source pixels under it are cropland, using GDAL average resampling on 0 and 1.

### Sample window

West 18.0, south −33.6, east 18.5, north −33.1 (EPSG:4326), Swartland and the Atlantic coast north of Cape Town, 2,000 × 2,000 source pixels, cut from the v2 sprout (`tiles/30S_010E.tif`, through the sprout VRT). It has wheat fields (709,089 cropland source pixels, about 458 km²), other land, and sea. The sprout stores sea as class 0, because GLAD writes 0 for everything that is not cropland.

Nodata. There is no nodata inside a South Africa window: nodata is only a 10° tile GLAD does not provide, and none is near land here. In the sample, nodata is everything outside the window, which shows as clear at every zoom, including the edge tiles where it meets cropland. The nodata rules are covered by pytest on synthetic rasters.

Zoom 12 has 56 tiles in the window. Zoom 12 draws 488,256 cropland pixels, about 498 km² against 458 km² in the source, so zoom 12 already overstates by about 9% under maximum resampling.

### Result per sample

Local run on 31 October 2026, laptop, GDAL 3.11.4, Python 3.14, one process per sample. Wall time includes cutting the window. Peak memory is the process maximum resident size; the q3 run also holds the comparison.

| | `max` | `q3` | `nearest` |
| --- | --- | --- | --- |
| Archive bytes | 88,750 | 90,552 | 92,777 |
| Tiles (zooms 0–12) | 98 | 98 | 98 |
| Wall time | 0.72 s | 0.78 s | 0.68 s |
| Peak memory | 107,757,568 | 145,604,608 | 110,215,168 |
| Zoom 12 cropland pixels still shown at zoom 8 | 100.0% | 97.5% | 82.3% |
| at zoom 6 | 100.0% | 96.6% | 73.8% |
| at zoom 4 | 100.0% | 96.4% | 65.3% |

Tiles per zoom 0–12: 1, 1, 1, 1, 1, 1, 1, 1, 4, 4, 6, 20, 56. The tile count is the same for every choice because the tiles are the same; only the pixels differ. The archive is about 7% smaller than the sum of its PNG files for `max`, because the writer stores identical tiles once.

"Still shown" is the share of zoom 12 cropland pixels whose cell at that zoom is cropland.

Cropland area kept compared with zoom 12 (cells × 4^(12 − zoom) divided by zoom 12 cropland pixels; 1.00 is the same area):

| Zoom | `max` | `q3` | `nearest` |
| --- | --- | --- | --- |
| 10 | 1.16 | 1.08 | 1.00 |
| 8 | 1.46 | 1.22 | 0.98 |
| 6 | 2.16 | 1.45 | 1.03 |
| 4 | 4.83 | 2.28 | 1.48 |

Reading it. `max` never loses a field but draws more cropland at each coarser zoom, 4.8 times the area at zoom 4. `nearest` keeps about the right total area, but 18% of the pixels at zoom 8 and 35% at zoom 4 are gone, so small fields vanish and the area that stays is a random subset (the 1.48 at zoom 4 is from only 11 cells). `q3` loses 2.5% to 3.6% of the pixels and overdraws less than `max`.

### q3 of q3 against q3 from the source

| Zoom | Pyramid cropland cells | Direct from source | Cells compared | Cells that differ |
| --- | --- | --- | --- | --- |
| 8 | 2,318 | 2,188 | 10,120 | 146 (1.4%) |
| 6 | 173 | 153 | 621 | 16 (2.6%) |
| 4 | 17 | 8 | 30 | 1 (3.3%) |

The pyramid q3 draws a little more cropland than a direct q3: 6% more cells at zoom 8, 13% at zoom 6. The two agree on 97–99% of the cells. At zoom 4 the counts are too small to carry weight.

### Does Mapbox GL JS read it?

No, not directly. The app uses `mapbox-gl` `^3.11.0`. The sample preview reads the archive with the `pmtiles` JavaScript library through `maplibregl.addProtocol`, and MapLibre reads it by byte ranges. Mapbox GL JS has no `addProtocol` and no public custom source type. This was checked on 31 October 2026 in 3.9.4 in a browser and in the 3.34.1 file (the latest); neither contains `addProtocol`. A raster source in Mapbox GL JS takes `http(s)` tile URLs, and `transformRequest` can only change the URL or headers, not supply the bytes. The `pmtiles://` URL therefore fails to load, and no layer appears. Mapbox itself was not loaded with a token, so the failure was not seen on screen.

If the app cannot read the archive, the app task has these ways out. Each costs something:

- Put a small service in front of the archive that answers `{z}/{x}/{y}.png` by reading one byte range from the PMTiles object. Mapbox then reads ordinary XYZ URLs. It is a new running service and its cost.
- Draw the archive with a Mapbox custom layer that fetches tiles with the `pmtiles` JavaScript library. More client code.
- Move the map to MapLibre, which the preview already uses. Larger change.
- Keep individual XYZ PNG files for the app (version 1 layout) and use the archive for something else. Millions of objects globally.

This needs a decision before task 07 and before task 05 is worth building.

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

Open `http://127.0.0.1:8766/preview.html`. Three maps move together: `max`, `q3`, `nearest`. The server answers HTTP Range requests (206), like the places preview. A plain `python -m http.server` often returns the whole file. Colour is applied in the page: class 1 orange, class 0 faint green, nodata clear. The basemap is Esri World Street Map, for a local look only. Cancel with Ctrl+C.

### Approval

Not yet given. When one resampling is approved, set `tiles.resampling` on `cookbook/recipes/cropland-2024-v2.yaml` and record the choice here. Task 05 does not start before then.
