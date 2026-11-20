# buildings: comparison boxes and the tile sample for the Buildings 2023 layer

Scripts that read Google Open Buildings 2.5D Temporal, Microsoft TEMPO and Overture building outlines in small boxes, on the sprout grid. They informed the values in [`cookbook/recipes/buildings-2023-v1.yaml`](../cookbook/recipes/buildings-2023-v1.yaml), and task 03 uses them again to check the sprout. `tiles.py` builds the share pyramid on small windows of the published sprout (task 04, [Tile sample](#tile-sample-task-04)). The parcel is `docs/parcels/buildings-2023.md` in naturewatch-app.

Nothing here writes to the cloud. Reads are public and box sized: a 5 or 6 km box reads a few tens of MB from Google.

## Run

```bash
pip install -r buildings/requirements.txt
python buildings/compare.py index                   # Google 2023 tile footprints, from the 151 manifests (about 5 minutes)
python buildings/compare.py box kongoussi           # one box: data/buildings/kongoussi.npz and one line of figures
python buildings/compare.py all --kind empty        # every box of a kind in boxes.yaml
python buildings/compare.py tables                  # data/buildings/thresholds.csv and floors.csv
python buildings/compare.py tempo-scan -17 15 35 23 # TEMPO low-density pixels per tile in a region
python buildings/render.py kongoussi --threshold 0.45 --floor 0
python buildings/ee_box.py kongoussi --value mean      # needs `earthengine authenticate`
python buildings/ee_box.py kongoussi --value threshold --threshold 0.45 --scale 4
python -m pytest buildings
```

`boxes.yaml` names the boxes. `settlement` boxes are the 10 November 2026 comparison, centred on a settlement. `empty` boxes are land with no buildings where Google has data. `sparse` boxes are scattered settlement outside Google's area, where TEMPO is the only source.

A box takes about 2 minutes and 2 GB of memory on the laptop, mostly the Google read.

## Method

Every figure is on the sprout grid: EPSG:4326, 0.00025° cells aligned to (−180, 90).

- **Google, COG reads:** band 3 `building_presence` of the 2023 tiles, nodata −99. Sampled on 50 × 50 points per cell (about 0.5 m, nearest neighbour), counted at or above the threshold, and divided by the valid points. A cell is covered where more than half its points are valid.
- **TEMPO:** 2023 Q4 band 1, nodata −1, averaged onto the cell. Band 1 is already a share: the Algeria tile reads 0 to 0.084, Kongoussi 0 to 0.42.
- **Overture:** release `2026-10-21.0` outlines, burned on the same 50 × 50 points and averaged.
- **Area:** cell share times the cell's area on the WGS84 spheroid.

Overture is the reference, not the truth. In Borkou it holds about 1,400 Microsoft ML outlines per box on bare rock (below).

## Google value: mean presence

Chosen by the user on 17 November 2026. A cell's value is the mean `building_presence` of Google's 2023 images in the cell. Earth Engine reads it from its mean pyramid at the sprout grid, which is the approach of the user's earlier Africa built layer (`built.ipynb`, 2025) without that layer's cut at 0.1. A cell with one 50 m² roof and sand around it reads about 0.07.

Built km² in the six settlement boxes Google covers, from `ee_box.py --value mean`:

| Box | Overture | TEMPO | Google mean presence | ÷ Overture | Google 0.5 m share at 0.45 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Kongoussi | 0.963 | 1.036 | 1.304 | 1.35 | 1.088 |
| Mopti | 0.824 | 0.917 | 0.858 | 1.04 | 0.833 |
| Timia | 0.073 | 0.056 | 0.079 | 1.08 | 0.037 |
| Bardaï | 0.085 | 0.086 | 0.131 | 1.54 | 0.086 |
| Mueda | 0.683 | 0.775 | 0.952 | 1.39 | 0.745 |
| Bria | 0.826 | 0.814 | 0.796 | 0.96 | 0.586 |
| All six | 3.454 | 3.684 | 4.120 | **1.19** | 3.375 |

TEMPO is 1.07 times Overture over the same boxes, so the seam where Google ends steps up by about a tenth in value as well as in sharpness. Cell by cell, mean presence and the 0.45 share correlate at 0.93 in Mopti. Cells below 0.002 round to 0 in the uint8 sprout. In Kongoussi those are 750 of the 10,247 cells the 0.45 share marks built, slivers where a roof edge crosses a cell border.

On empty land mean presence stays clean. In the noisy Sahara boxes 3 to 7 cells of about 50,000 reach 0.01, and the highest is 0.019–0.022. Tagant-east reaches 0.14 where Google sees a few roofs of its own.

### Earth Engine paths, measured

Interactive profiles of whole boxes on 16 and 17 November 2026, project `nature-watch-387210`. A batch export may use somewhat less.

| Path | EECU-seconds per box (25–36 km²) | Over 75.3 million km² | Built km² vs 0.5 m share at 0.45, Kongoussi, Mueda, Bria |
| --- | ---: | ---: | --- |
| Mean presence (pyramid) | 0.09–0.13 | 60–90 EECU-hours | +20 to +36% |
| Threshold 4 m pixels at 0.45 | 2.2–5.5 | 1,800–3,200 EECU-hours | −4.8%, −6.6%, −7.8% |
| Threshold 2 m pixels at 0.45 | 8.4–23 | 7,500–13,500 EECU-hours | −1.2%, −1.6%, −1.9% |
| Threshold 0.5 m pixels | 14.2 for 1.73 km² | 100,000–171,000 EECU-hours | reference |

The parcel's tile pyramid decides how lone buildings show when zoomed out. Task 04 compares a share-weighted mean, as for cropland, with a maximum.

## Google presence threshold, compared

Not used for the seed. Kept because task 03 can compare against it.

Built km² in the six settlement boxes Google covers. Ratio is Google ÷ Overture.

| Box | Overture | TEMPO | 0.40 | 0.45 | 0.50 | ratio 0.40 | ratio 0.45 | ratio 0.50 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Kongoussi | 0.963 | 1.036 | 1.250 | 1.088 | 0.994 | 1.30 | 1.13 | 1.03 |
| Mopti | 0.824 | 0.917 | 1.000 | 0.833 | 0.726 | 1.21 | 1.01 | 0.88 |
| Timia | 0.073 | 0.056 | 0.053 | 0.037 | 0.030 | 0.73 | 0.51 | 0.41 |
| Bardaï | 0.085 | 0.086 | 0.100 | 0.086 | 0.078 | 1.18 | 1.01 | 0.92 |
| Mueda | 0.683 | 0.775 | 0.842 | 0.745 | 0.689 | 1.23 | 1.09 | 1.01 |
| Bria | 0.826 | 0.814 | 0.692 | 0.586 | 0.525 | 0.84 | 0.71 | 0.64 |
| All six | 3.454 | 3.684 | 3.937 | 3.375 | 3.042 | 1.14 | **0.98** | 0.88 |

At 0.30 the total is 5.05 km² (1.46), at 0.60 2.24 (0.65), at 0.70 1.59 (0.46). 0.45 is closest to 1 over all six boxes, and its median ratio is 1.01. Timia and Bria read low at every threshold. Why was not looked into.

Saint-Flour has no Google tile. Its TEMPO ÷ Overture is 0.99.

### Overviews

The Google COGs store their overviews smallest first: overview index 13 is 1 m and 12 is 2 m. At Kongoussi, 0.5 threshold:

| Read | Google km² | vs 0.5 m |
| --- | ---: | ---: |
| 0.5 m | 0.994 | |
| 1 m (overview 13) | 0.973 | −2.1% |
| 2 m (overview 12) | 0.949 | −4.5% |

### Earth Engine agrees

`ee_box.py` reduced Kongoussi in Earth Engine (`computePixels`, 48 × 48 cell pieces, project `nature-watch-387210`, no export task) on 16 November 2026:

| Threshold | Cells | COG km² | Earth Engine km² | Mean cell difference | 99th percentile | Correlation |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.50 | 33,852 | 0.99403 | 0.99394 | 0.00061 | 0.0066 | 0.99985 |
| 0.45 | 33,852 | 1.08831 | 1.08822 | 0.00062 | 0.0067 | 0.99987 |

These are 0.5 m thresholds. The chosen mean presence needs no comparison with the COGs.

A mosaic has no default projection in Earth Engine, so `ee_box.py` sets the first image's 0.5 m UTM grid. One request may not reproject more than about 10,000 × 10,000 input pixels.

## TEMPO false positives

`tempo-scan -17 15 35 23` read all 14,437 TEMPO tiles between 15° and 23° N, 17° W and 35° E (14 minutes). 11,500 tiles have a pixel above 0. Eleven tiles have more than 5% of their pixels above 0 with a mean below 0.02. They cluster at 5.9° E 20.5–21.2° N, 3.6° E 21.5° N, 19° E 18.3° N, and in Mauritania at 17.2° N 9° W and 17.9° N 14.2° W. The noise is patchy. The ten empty boxes picked by eye had almost none: at most 52 cells, none above 0.014.

Empty boxes, 6 × 6 km, all fully covered by Google:

| Box | Google top presence | Google km² at 0.45 | TEMPO km² | TEMPO cells above 0 | TEMPO max |
| --- | ---: | ---: | ---: | ---: | ---: |
| in-salah-south (20.8892° N, 5.8708° E) | 0.16 | 0 | 0.031 | 6,513 | 0.058 |
| in-salah-south-north | 0.28 | 0 | 0.055 | 11,656 | 0.063 |
| in-salah-south-south | 0.34 | 0 | 0.059 | 12,364 | 0.077 |
| ahnet | 0.08 | 0 | 0.042 | 9,982 | 0.059 |
| borkou-north | 0.24 | 0 | 0.055 | 10,798 | 0.072 |
| borkou-south | 0.40 | 0 | 0.071 | 12,221 | 0.069 |
| trarza | 0.42 | 0 | 0.026 | 7,025 | 0.045 |
| tagant-east | 0.92 | 0.0002 | 0.112 | 20,415 | 0.070 |
| hodh-north | 0.92 | 0.0001 | 0.118 | 13,311 | 0.090 |
| tanezrouft, majabat, aouker, tenere, djado-north, djourab, nubian-sudan, egypt-south, hoggar-west, mali-north | 0.00–0.88 | 0–0.0003 | 0.0000–0.0001 | 0–52 | 0.000–0.014 |

In the boxes with noise, half the TEMPO cells above 0 are below 0.003–0.008, and 90% are below 0.013–0.028. No noise cell reaches 0.10. Imagery of borkou-north shows rock outcrops between dunes and no buildings. Overture's 1,457 outlines there are all Microsoft ML Buildings, with a median of 53 m².

### A floor alone

Noise: TEMPO cells left in the eight noisy boxes. Real buildings: Overture km² in cells the floor empties. Settlement is the six Google-covered settlement boxes together. Sparse is Kansas, Ti Tree, Korgalzhyn, Lairg and Saint-Flour.

| Floor | Noise cells left | Noise km² left | Settlement Overture lost | Worst settlement box | Worst sparse box |
| ---: | ---: | ---: | ---: | --- | --- |
| 0.005 | 47.4% | 85.2% | 0.2% | | |
| 0.01 | 25.5% | 64.3% | 0.6% | Bardaï 2.3% | Ti Tree 1.6% |
| 0.02 | 8.3% | 32.2% | 1.7% | Bardaï 7.7% | Lairg 4.0% |
| 0.03 | 2.8% | 14.9% | 3.4% | Bardaï 15.6% | Ti Tree 10.0% |
| 0.05 | 0.4% | 3.0% | 8.3% | Bardaï 31.9% | Lairg 16.8% |
| 0.08 | 0.0% | 0.0% | 18.8% | Bardaï 58.3% | |

No floor removes the noise and keeps scattered buildings. At 0.03 about 3% of the noise cells stay, and they draw in the second shade.

### The Overture mask

The parcel's fallback keeps a TEMPO cell only near an Overture outline. In Borkou that keeps the noise, because Overture holds Microsoft's outlines on the same rocks. It would also need the global Overture buildings. It is not recommended.

### A seed rule: chosen

Approved by the user on 17 November 2026: seed 0.15 within 2 km.


Keep a TEMPO cell only if a cell within a radius reaches a seed share. The noise never reaches 0.10. Every settlement box reaches 0.21 or more.

| Seed | Radius | Noise cells left | Settlement Overture lost | Worst settlement box | Worst sparse box |
| ---: | ---: | ---: | ---: | --- | --- |
| 0.10 | 1 km | 0% | 0.2% | Timia 1.5% | Kansas 1.8% |
| 0.10 | 2 km | 0% | 0.0% | Timia 0.2% | Kansas 0.3% |
| 0.15 | 1 km | 0% | 2.2% | Bardaï 9.4% | |
| **0.15** | **2 km** | **0%** | **0.0%** | **Bardaï 1.1%** | **Kansas 1.3%** |
| 0.20 | 2 km | 0% | 0.1% | Bardaï 3.1% | Kansas 10.6% |

Seed 0.15 within 2 km leaves 0.06 between the highest noise and the seed. Its cost is a lone building more than 2 km from any cell of 0.15 or more. The radius is measured on the grid, 72 cells either side.

The noise outside the Sahara (Arabia, Iran, Central Asia, inland Australia) is not measured here. Task 04's sample includes empty desert where only TEMPO exists.

## Sprout grid: chosen

Approved by the user on 17 November 2026, with the share stored as uint8, value ÷ 250, 255 nodata.


EPSG:4326 at 0.00025°, the cropland grid. A cell is about 730–755 m² in these boxes. With the 0.45 threshold, 27–37% of built cells in Kongoussi, Mueda and Timia have a share below 0.05, and the 5th percentile is 0.006–0.008. A single compound keeps a share of its own. A coarser grid would dilute it into its neighbours.

## Cost of this step

No transfer, export task or Batch job. Earth Engine: about 150 `computePixels` requests (two 0.5 m passes over Kongoussi in 48 × 48 cell pieces, then whole-box requests) and about 25 profiled `getInfo` requests, on the noncommercial project. Laptop: about 55 box runs of up to 2 minutes, peak resident memory 2.0 GB. Cached arrays in `data/buildings/` are local only.

## Google cut test (task 03b)

Seed v1 shows faint speckle in the bush around Kocho. Google's explorer draws presence only from 0.2. This test sets presence below 0.2 to 0 before the mean into sprout cells, in Earth Engine, with the cut made on 0.5 m, 2 m and 4 m pixels. The boxes are `kocho-cut`, `bria-cut` and `yalinga-cut` in `boxes.yaml`, each 10 × 10 km.

The project's Earth Engine allowance is 540,000 EECU-seconds a month (150 EECU-hours, the Community tier), read from Service Usage on 19 November 2026.

### Exports, submitted 19 November 2026

Nine exports, approved by the user, expected under 1 EECU-hour in all. The task ids are in `data/buildings/cut-test/exports.json` (local).

| Box | 0.5 m | 2 m | 4 m |
| --- | --- | --- | --- |
| kocho-cut | B67TCPVWEJWUXQZXBHLQVSII | UY7TXOI67OWIVDTLQYGUTFXQ | VNVBAHPWPLEZ54CAIX7PLWSG |
| bria-cut | IYDRCLGYBNOVTO6DRBQEJFDY | WD7WB5WQBQ7ZTPXCDJ3OHURX | HO2GYPVDYTOJTFCCRJC4UE44 |
| yalinga-cut | 55ILXT6HEJUQVYBAM5IIZR5P | JXYKOMAHP6U7YP4PC4KCKXWO | CBJLK44JEAQRMNJJVZ2J6G7Y |

- Output: `gs://nature-watch-bucket/scratch/buildings/google-cut-test/<box>-cut0.2-<scale>m.tif`, deleted once the figures are recorded.
- Status and EECU: `python buildings/ee_box.py --status`.
- Cancel: `earthengine --project nature-watch-387210 task cancel <task id>`.
- Then: `python buildings/cut_test.py fetch` and `python buildings/cut_test.py report`.

### Results

The three 0.5 m exports failed: "Reprojection output too large (about 14,200 × 14,200 pixels)". Earth Engine will not cut a 10 km box at 0.5 m in one export, so a 0.5 m seed would need thousands of small exports. The 0.5 m column below is the same cut read from Google's COGs on the laptop (`cut_test.py read`, 8.4 minutes, 2.2 GB peak memory for the three boxes). That read is exact.

Google cells only. Speckle is a seed v1 cell above 0 with no Overture outline in it.

| Box | Layer | Built km² | Cells above 0 | v1 speckle left | EECU-seconds |
| --- | --- | ---: | ---: | ---: | ---: |
| Kocho | seed v1 | 0.0102 | 268 | 187 | |
| Kocho | cut, 0.5 m (COGs) | 0.0096 | 131 | 19 | |
| Kocho | cut, 2 m | 0.0096 | 120 | 16 | 30.4 |
| Kocho | cut, 4 m | 0.0094 | 119 | 16 | 8.4 |
| Yalinga | seed v1 | 0.0130 | 386 | 128 | |
| Yalinga | cut, 0.5 m (COGs) | 0.0098 | 379 | 44 | |
| Yalinga | cut, 2 m | 0.0095 | 331 | 30 | 46.5 |
| Yalinga | cut, 4 m | 0.0090 | 326 | 24 | 8.1 |
| Bria | seed v1 | 0.9181 | 11,134 | 2,713 | |
| Bria | cut, 0.5 m (COGs) | 0.7022 | 10,500 | 1,340 | |
| Bria | cut, 2 m | 0.6886 | 9,920 | 1,185 | 30.2 |
| Bria | cut, 4 m | 0.6787 | 9,846 | 1,155 | 8.6 |

Overture has 0.0122 km² at Kocho, 0.0238 at Yalinga and 0.9503 at Bria. Counting pixels at 0.2 or more instead of keeping their values gives 0.0141, 0.0205 and 1.4829 km².

- The cut removes 90% of the Kocho speckle. In Esri imagery, most of the cells left with no Overture outline sit beside a roof at Kocho camp or inside the villages at Yalinga and Bria: the buildings are real, and Overture has no outline in that cell. A few lone cells at 0.004–0.016 remain in the bush.
- The 4 m cut is within 3–8% of the 0.5 m built area and keeps 86–94% of its cells. The 2 m cut is no closer, at 3.6–5.7 times the cost.
- Lone Overture outlines (none within 100 m) are not a usable check here: seed v1 keeps only 0–22% of them, and in Chinko they are termite mounds and bright patches.

Global cost, from EECU-seconds per km² of each box over Google's 75.3 million km²:

| Cut made on | EECU-seconds per km² | Global EECU-hours | Months of the 150 EECU-hour allowance |
| --- | ---: | ---: | ---: |
| 4 m | 0.081–0.086 | 1,700–1,800 | 11–12 |
| 2 m | 0.30–0.46 | 6,300–9,700 | 42–65 |
| 0.5 m | not possible in one export per box | 100,000–171,000 (task 01) | 670+ |

The six exports used 132 EECU-seconds; the three failed exports report none. The 44 KB of outputs in the scratch folder were deleted on 19 November 2026.

### Decision, 19 November 2026

The user chose not to make seed v2: a global cut costs 11–12 months of the allowance at 4 m, and a higher tier or a Batch run is too expensive for now. Seed v1 stays. The speckle is thinned when the tiles are built (task 04), and these boxes calibrate that rule. Speckle below is a seed v1 cell above 0 that the exact 0.5 m cut empties.

| Rule on seed v1 | Speckle removed | Real built km² kept | Real cells kept |
| --- | --- | --- | --- |
| Floor 2 of 250 (0.008) | 53–75% | 99–100% | 83–95% |
| Floor 3 of 250 (0.012) | 70–88% | 97–99% | 73–88% |
| Floor 4 of 250 (0.016) | 80–95% | 96–99% | 66–84% |
| Floor 6 of 250 (0.024) | 89–98% | 94–98% | 59–78% |
| A Google cell within 2 km reaches 0.15 | 32–60% at Kocho and Yalinga, 4% at Bria | 95–100% | |

The floor thins the noise around villages. The seed rule clears empty bush, where the boxes hold little speckle but the 1° Kocho window holds about 20,000 cells.

## Tile sample (task 04)

`tiles.py` cuts seven 1° windows from the published sprout VRT and builds the share pyramid on each under several settings, on the laptop. `preview.py` shows the archives on a Mapbox map with the app's share paint.

```bash
python buildings/tiles.py fetch                     # data/buildings/tiles/sprout/<window>.tif, read with the gcloud account's token
python buildings/tiles.py sample                    # the baseline and each variant, every window
python buildings/tiles.py window kongoussi-window --top-zoom 12 --top-rule graded --full 0.25 --floor 0.01 --empty drop
python buildings/tiles.py compare                   # the tables below, from data/buildings/tiles/*.json
python roads/render.py tiles data/buildings/tiles/<archive>.pmtiles --zoom 8 --bbox W S E N --out data/buildings/renders/<name>.png
MAPBOX_ACCESS_TOKEN=pk... python buildings/preview.py   # http://127.0.0.1:8765/preview.html
python -m pytest buildings
```

**Method.** A window reads the sprout under every zoom 11 tile (512 px) that meets it, with 2 cells more on each side, so every top-zoom tile is whole. The top zoom is warped from the window to Web Mercator with `average`: the share with nodata read as 0, and a valid mask beside it. So a pixel's share is the built fraction of the whole pixel and the built area is kept; a pixel is nodata only where no cell under it has a source. Lower zooms are reduced from four children. Tiles are 4-bit palette PNGs in the cropland version 3 layout: index 0 is nodata, index k + 1 is shade k of 7, red = 255 × (k / 7)². With `full` 1 and the 0.5 solid rule the tiles are byte for byte what `tiles/transcode.py share_palette_from_gray` writes (`test_tile_sample.py`). Shade k is round(√(share / `full`) × 7), and any share above 0 keeps shade 1.

**Windows** (`boxes.yaml`, kind `window`):

| Window | Centre | Source | What it tests |
| --- | --- | --- | --- |
| kongoussi | 13.326 N, 1.533 W | Google | a rural Sahel town and its villages |
| bria | 6.537 N, 21.987 E | Google | a town in forest and savanna |
| kocho | 6.30532 N, 23.859 E | Google | a remote village with an airstrip |
| mueda | 11.660 S, 39.546 E | Google | a plateau town and scattered villages |
| saint-flour | 45.034 N, 3.093 E | TEMPO | rural France |
| rebiana | 24.2 N, 22.0 E | TEMPO | empty desert beyond Google's edge |
| edge | 27.5 N, 9.8 E | both | Google's edge on the Algeria–Libya border, at 9.67–9.91° E in the window |

**Settings.** The baseline is cropland's: top zoom 11, solid from share 0.5, `full` 1, mean parent, every tile with a valid cell stored (`keep`), no floor. Each variant changes one setting.

### Built area against the sprout

At the top zoom, the share summed over the tiles against the sprout under the same tiles, edge cells weighted by overlap. The box columns are the task 03 check boxes, read from the window cut and from the top-zoom tiles.

| Window | Sprout km² | Tiles km² at z11, z12, z13 | Box | Box km², sprout (task 03) | Box km², z11 tiles |
| --- | ---: | --- | --- | ---: | ---: |
| kongoussi | 36.3186 | 36.3186, 36.3186, 36.3186 | kongoussi | 1.3035 | 1.3036 |
| bria | 1.6340 | 1.6340, 1.6340, 1.6340 | bria | 0.7953 | 0.7949 |
| kocho | 0.1116 | 0.1116, 0.1116, 0.1116 | | | |
| mueda | 9.6059 | 9.6059, 9.6059, 9.6059 | mueda | 0.9519 | 0.9521 |
| saint-flour | 51.5937 | 51.5937, 51.5937, 51.5937 | saint-flour | 0.6780 | 0.6779 |
| rebiana | 0 | 0, 0, 0 | | | |
| edge | 1.3858 | 1.3858, 1.3858, 1.3858 | | | |

The box figures read from the windows are the task 03 figures. The tiles keep the area at every top zoom. A box's tile figure differs from the sprout by up to 0.0004 km², from pixels that straddle the box edge.

Rebiana has no building cell: TEMPO's noise there is removed by the sprout's seed rule. At the edge, Google holds 18,959 built cells and TEMPO 511.

### Top-zoom rule

Share of the built area in pixels drawn solid, and the area painted, at top zoom 11:

| Window | Built km² | ≥ 0.5 | ≥ 0.25 | ≥ 0.1 | ≥ 0.05 |
| --- | ---: | --- | --- | --- | --- |
| kongoussi | 36.32 | 0%, 0.14 km² | 11%, 12.5 km² | 55%, 119 km² | 79%, 237 km² |
| bria | 1.63 | 0%, 0.01 km² | 17%, 0.84 km² | 49%, 4.3 km² | 70%, 9.1 km² |
| kocho | 0.11 | 0%, 0 | 3%, 0.01 km² | 8%, 0.05 km² | 14%, 0.15 km² |
| mueda | 9.61 | 0%, 0.02 km² | 2%, 0.77 km² | 35%, 23.6 km² | 68%, 67.7 km² |
| saint-flour | 51.59 | 1%, 1.2 km² | 16%, 24.2 km² | 58%, 169 km² | 80%, 323 km² |
| edge | 1.39 | 8%, 0.20 km² | 46%, 1.7 km² | 79%, 4.5 km² | 89%, 6.5 km² |

Cropland's solid rule (0.5) erases the layer at the top zoom: a 38 m pixel is rarely half covered by buildings. A threshold low enough to keep most buildings paints 5–7 times their area. A graded top zoom keeps every building at its share, for about twice the bytes of a solid one (Kongoussi 847,115 against 345,970).

### Top zoom

A 512 px tile at zoom 11 is 38 m a pixel at the equator, 12 at 19 m and 13 at 9.5 m. The sprout cell is 28 m.

| Window | z11 tiles, bytes | z12 tiles, bytes | z12 ÷ z11 | Wall s, z11 and z12 |
| --- | --- | --- | ---: | --- |
| kongoussi | 79, 794,884 | 275, 2,011,832 | 2.53 | 3.2, 7.9 |
| bria | 70, 119,482 | 232, 302,867 | 2.53 | 1.8, 4.7 |
| kocho | 72, 69,128 | 234, 193,403 | 2.80 | 1.7, 4.3 |
| mueda | 79, 278,612 | 245, 735,933 | 2.64 | 2.1, 5.2 |
| saint-flour | 108, 1,156,530 | 360, 2,718,975 | 2.35 | 3.6, 9.1 |
| edge | 67, 68,798 | 172, 171,805 | 2.50 | 2.1, 5.5 |

These are the recommended settings below (graded, `full` 0.25, mean, floor 0.01, `drop`); Rebiana stores no tile. Zoom 13 is 4–6 times zoom 11's bytes on the baseline settings.

Renders of Kongoussi town at each top zoom (`kongoussi-top-z11.png`, `-z12`, `-z13` in `data/buildings/renders/`): zoom 11 draws the town as one blurred mass, because its pixel is coarser than the sprout cell. Zoom 12 shows the street grid, the open ground between quarters and single compounds at the edge. Zoom 13 only redraws the same 28 m cells more sharply.

### Shade curve and parent rule

Built pixels in shades 1–7, and the share in the two faintest, on the baseline settings except as named:

| Window, zoom | `full` 1 | `full` 0.25 | `full` 0.1 | max parent |
| --- | --- | --- | --- | --- |
| kongoussi, 8 | 70,003; 1,375; 326; 14; 0; 0; 0 (100%) | 62,977; 6,011; 1,587; 619; 338; 149; 37 (96%) | 53,988; 9,870; 4,242; 1,655; 802; 464; 697 (89%) | 44,176; 15,086; 10,155; 2,010; 283; 8; 0 (83%) |
| kongoussi, 10 | 97% | 81% | 67% | 89% |
| mueda, 10 | 99% | 83% | 71% | 94% |
| saint-flour, 4 | 1,004; 3; 0; 0; 0; 0; 0 (100%) | 953; 44; 9; 1; 0; 0; 0 (99%) | 745; 223; 26; 9; 3; 1; 0 (96%) | 11; 19; 120; 592; 236; 26; 3 (3%) |
| saint-flour, 10 | 93% | 75% | 63% | 91% |
| edge, 10 | 94% | 86% | 82% | 89% |

Buildings cover a small share of a pixel: at top zoom 11, 0–8% of the built area is in pixels at 0.5 or more, and 2–46% at 0.25 or more (table above). With `full` 1, nearly every built pixel is the faintest shade at every zoom, so towns and lone compounds look alike. `full` 0.25 (the roads value) spreads villages over the shades at zooms 8–11, and a town core reaches the darkest. `full` 0.1 makes 12% of Saint-Flour's zoom 10 pixels the darkest shade and flattens the town. The max parent darkens coarse zooms: at zoom 4, 97% of Saint-Flour's built pixels are shade 3 or darker, so rural France would read as dense as a town centre. The mean parent keeps the built area at every zoom, and lone buildings still show at zoom 4 because any share keeps shade 1. The `full` settings cost 5–20% more bytes; max 4–10%.

### Google's faint cells, and a floor

The Kocho render at the top zoom shows the village and airstrip, and single faint pixels scattered across the savanna. They are Google's mean presence at 1 or 2 of 250 (share 0.004 or 0.008, 3–6 m² in a 28 m cell), smaller than a hut. 75% of Kocho's built cells are 1, and 97% are 5 or less. The Algerian side of the edge window shows the same speckle at zoom 8. TEMPO's noise is removed in the sprout by the seed rule; Google's is not, and `any share keeps shade 1` draws it as a faint haze at every zoom.

A floor reads a sprout cell below it as 0 when the tiles are built. The sprout is not changed.

| Window | Built km² kept, floor 0.01 | floor 0.02 | Zoom 8 built pixels: none, 0.01, 0.02 |
| --- | ---: | ---: | --- |
| kongoussi | 98.2% | 96.8% | 71,718; 48,680; 43,679 |
| bria | 94.9% | 92.1% | 17,685; 6,133; 4,378 |
| kocho | 42.9% | 31.0% | 17,717; 2,000; 675 |
| mueda | 97.0% | 94.5% | 33,334; 16,050; 12,738 |
| saint-flour | 97.8% | 95.3% | 83,612; 67,600; 60,039 |
| edge | 98.2% | 96.9% | 5,628; 1,741; 1,206 |

At floor 0.01, the Kocho speckle is gone and the village is whole (`kocho-top-z11-floor0.01.png`). In the towns it removes 2–5% of the built area, from cells at the fringe of real buildings. 0.02 removes half as much again for little visible gain.

### Empty land

`keep` and `drop` differ by under 1% in bytes in every window. PMTiles stores one copy of the all-empty tile, and almost every inhabited tile has a building. `drop` stores 0 of Rebiana's 77 tiles and 67 of the edge window's 96. The map draws nodata and share 0 the same, and the parcel names no use for the difference.

### The edge

With the recommended settings, the edge window at zoom 8 (`edge-z8-recommended.png`) shows no line where Google meets TEMPO, at 9.67–9.91° E, and no haze on either side. The TEMPO side holds 0.0155 km² of buildings in 511 cells, and the Google side 1.55 km².

### Recommendation

| Choice | Recommended | Why |
| --- | --- | --- |
| Top zoom | **12** | The first zoom finer than the 28 m sprout cell. It shows compounds and streets that zoom 11 blurs. 2.4–2.8 times zoom 11's bytes |
| Top-zoom rule | **graded** | Solid at 0.5 keeps 0–8% of the built area; lower thresholds paint several times the built area |
| `full` | **0.25** | The roads value. Spreads villages and towns over the shades; 0.1 flattens town centres |
| Parent | **mean** | Keeps the built area at every zoom; max makes coarse zooms read as dense |
| Empty tiles | **drop** | The same bytes, fewer tiles, as roads. Nothing reads the difference |
| Google speckle | **floor 0.012 and seed 0.15 within 2 km, Google cells only** | See "Google speckle, resumed" below. Replaces the floor of 0.01 on every cell |

Paint is task 06's. The preview's faintest opacity can be set to 0.2 (cropland) or 0.05 (roads).

### Google speckle, resumed 19 November 2026

The sample was paused for task 03b, which found a cut of Google presence in Earth Engine too costly. The tiles thin seed v1's speckle instead, with `speckle_rule` on the window cut before the top zoom is warped. It touches Google cells only (source 1), because TEMPO cells already passed the sprout's seed rule:

- floor: a Google cell below the floor is 0;
- seed: a Google cell is 0 unless a Google cell within 72 cells (2 km) reaches 0.15, the sprout's TEMPO rule.

`fetch` now reads 72 more cells around each window, so the seed sees the full 2 km at the window's edge. The five windows read 4.0 MB from the bucket. `tiles.py speckle` builds the recommended settings with each rule.

| Window | Rule | Cells above 0 in the window | Built km² kept |
| --- | --- | ---: | ---: |
| Kocho | none | 17,027 | 0.112 (100%) |
| Kocho | floor 0.012 | 1,619 | 43% |
| Kocho | floor 0.012 + seed | 108 | 20% |
| Bria | none | 40,342 | 1.634 (100%) |
| Bria | floor 0.012 | 21,828 | 95% |
| Bria | floor 0.012 + seed | 20,088 | 92% |
| Kongoussi | none | 420,853 | 36.32 (100%) |
| Kongoussi | floor 0.012 + seed | 305,554 | 98% |
| Mueda | none | 169,032 | 9.606 (100%) |
| Mueda | floor 0.012 + seed | 110,064 | 96% |
| Edge | none | 3,422 | 1.386 (100%) |
| Edge | floor 0.012 + seed | 893 | 97% |

Floors of 0.008 and 0.016 with the seed keep 1–3 points more or less of the built area; the archives are in the preview. At Kocho, almost everything removed is the faint speckle on the bush, and the camp stays (`renders/kocho-speckle-pair.png`). At Bria, the speckle in the south-western bush goes, and the town and roadside villages stay (`renders/bria-speckle-pair.png`). Against the exact cut in task 03b, a floor of 0.012 keeps 97–99% of real built area in the 10 km boxes, so most of what it removes in the windows is speckle.

The sprout keeps the speckle. A stats run applies the same rule (parcel, Left for later).

### Global estimate

Assumptions: about 0.9–1.0 million zoom 11 tiles of land with a source, as cropland v3 has 1.28 million tiles at zooms 0–11 over land. The sprout has 5.46 billion cells above 0 (1.40 Google, 4.06 TEMPO, `sprout/README.md`), and the floor removes 25–40% of them (Kongoussi 29%, Saint-Flour 25%, Kocho 89%), leaving 3.3–4.1 billion. With the recommended settings at zoom 11 the windows store 1.0 (Saint-Flour) to 2.0 (Mueda) bytes a built cell, plus about 1 KB a tile where buildings are sparse.

| | Top zoom 11 | Top zoom 12 |
| --- | --- | --- |
| Tiles | 0.6–1.0 million at the top zoom, up to 1.3 million in all | 2.4–4 million at the top zoom, up to 5.3 million in all |
| Bytes | 4–9 GB | 10–25 GB (2.4–2.8 times zoom 11) |
| Task hours | 16–40, at 0.06–0.15 s a top-zoom tile as cropland v3 and roads | 40–100, 2.5 times zoom 11 in the windows |
| Compute | about $0.6–1.5 on spot `e2-standard-2`, plus a merge under $0.20 | about $1.5–3.5, plus a merge under $0.30 |
| Storage | $0.09–0.21 a month | $0.23–0.58 a month |

Task 05 prices the run from its pilot and asks for approval first.

### Cost of the sample

No compute in the cloud. Reads from the sprout through the VRT: 6,524,064 bytes for the seven windows (Saint-Flour the most, 2.4 MB), inside the free monthly egress, under $0.01. Esri imagery for the renders is public. On the laptop: 137 window archives of 1.6–28 s each, peak resident memory 1.6 GB (the whole sample in one process). The archives and renders are in `data/buildings/tiles/` and `data/buildings/renders/`, not committed. No virtual machine ran. Resumed on 19 November 2026: five windows read again with the 2 km margin, 4.0 MB, and 30 archives of 4–9 s each.

### Approval

Approved by the user on 19 November 2026: top zoom 12, graded, `full` 0.25, mean parent, empty tiles dropped, and the Google speckle rule (floor 0.012, seed 0.15 within 2 km). The settings are on the recipe. The global estimate above holds: the Google rule removes about as many cells as the floor did (27% at Kongoussi).

## Global tiles (task 05)

```bash
python buildings/batch.py plan                                  # data/buildings/batch/plan.json: 557 shards over 363 blocks
python buildings/batch.py local kocho-window edge-window        # the shard and merge code on a task 04 cut, against the task 04 archive
python buildings/batch.py submit tiles --pilot 5-15-14 [--approved]   # one shard; prints the job, submits only with --approved
python buildings/batch.py submit tiles --approved               # every shard, one task each, 12 at a time
python buildings/batch.py records                               # totals from the shard records written so far
python buildings/batch.py submit merge --approved               # after every shard has a record
```

**Shards.** One shard per zoom 5 tile that meets a sprout block with a COG: 557 shards. A shard copies every block that meets it widened by 74 cells (0.0185°), 1–9 blocks and at most 0.9 GB, builds a VRT over them, renders zoom 12 and reduces to zoom 5 with the recipe's settings. A top-zoom tile reads the share and source bands under it with 72 more cells on every side, applies Google's speckle rule to what it read (`tiles.py speckle_rule`), and warps the share with `average`. So a tile is the same wherever the source is cut, and shards meet without a seam. A tile with no building is not stored, but feeds its parent. The shard writes its archive, its zoom 5 root (share and valid, unquantised, `.root.npz`) and a record, last, under `gs://nature-watch-bucket/scratch/buildings/buildings-2023-v1/tiles/shards/`. A record carries a hash of the settings, so a rerun skips a current shard. The shard and merge code is `tiles/shards.py`, shared with roads. A tile works out its window and warp origin on the global cell grid (180° W, 90° N), so a shard's block mosaic and the sprout VRT give the same bytes. Spot `n2-standard-2`, 64 at a time (E2 allows only 24 CPUs in the region), 30 GB `pd-standard`.

**Merge.** One task: it checks every shard record, builds zooms 0–4 from the roots, streams the shards in tile id order, rebuilds 12 random zoom 8 subtrees and 4 on a shard's west or north edge from the sprout VRT and compares them byte for byte, uploads with `Cache-Control: public, max-age=31536000, immutable`, checks a 206 Range answer and deletes the shards. It refuses to overwrite an archive that exists. Spot `e2-standard-4`, 150 GB `pd-standard`.

**Local check, 19 November 2026.** Through `local`, from the task 04 cuts on the laptop. Every tile that lies wholly inside the window is the task 04 archive's tile byte for byte (approved settings): 17 of 17 at Kocho, 63 of 63 at the edge window. The edge archive holds 11 more tiles, in the read margin outside the window. The sample check rebuilt 42 tiles from the Kocho cut and agreed. Each window took 4–6 s at a peak of 0.45 GB.

### Runs

**Pilot, shard `5-15-14`** (11.25° W to 0°, 11.18° N to 21.94° N: Burkina Faso, Mali and Mauritania, with Kongoussi; blocks `E000N10`, `E000N20`, `W010N10`, `W010N20`, `W020N10`, `W020N20`, 117,525,228 bytes). Approved with the task file, expected under $0.05. Submitted 19 November 2026 at 19:52 UTC. One spot `e2-standard-2`, 30 GB `pd-standard`.

- job: `buildings-2023-v1-tiles-pilot-5-15-14-202611191951`, `europe-west1`
- output: `gs://nature-watch-bucket/scratch/buildings/buildings-2023-v1/tiles/shards/5-15-14.pmtiles`, `.root.npz` and `.json`; the full run skips this shard
- status: `gcloud batch jobs describe buildings-2023-v1-tiles-pilot-5-15-14-202611191951 --location=europe-west1 --project=nature-watch-387210`
- cancel: `gcloud batch jobs delete buildings-2023-v1-tiles-pilot-5-15-14-202611191951 --location=europe-west1 --project=nature-watch-387210`
- result: succeeded, run duration 1,052 s. 11,017 tiles (7,901 at zoom 12 of the 16,384 possible; 5 to 12: 1, 4, 16, 51, 177, 624, 2,243, 7,901), 43,928,046 bytes, 4.0 KB a tile, and a 2,097,906 byte root. 1,042 s in the shard, of which 3.2 s copying the six blocks; peak memory 0.53 GB.

**Revised cost, from the pilot.** For approval before the full jobs. The block records give each block's valid and built cells. Spread evenly over a block, they put about 15,900 valid zoom 12 tiles (land or Google's coverage, which includes some sea) and 16 million built cells in the pilot shard. Over the 557 shards they give 3.19 million valid zoom 12 tiles, 201 times the pilot, and 5.46 billion built cells, 341 times.

| Job | Work | Machine and time | Compute | Stored |
| --- | --- | --- | --- | --- |
| Shards | 556 more shards. A valid tile is read and warped whether or not it has a building, so time scales with valid tiles: 58 task-hours at the pilot's 0.066 s a valid tile, 45–80 with denser or sparser shards. The longest shard is about 18 minutes | spot `e2-standard-2`, 30 GB `pd-standard`, 12 at a time, about 4–7 hours of wall time | about $1.2–2.8, plus about $0.10 of disk | 6–15 GB of shards in scratch until the merge deletes them |
| Merge | fetch the shards and roots, write the archive, rebuild 16 zoom 8 subtrees from the sprout VRT, upload | one spot `e2-standard-4`, 150 GB `pd-standard`, about 1–2 hours | about $0.15–0.30 | the archive: 6–15 GB, $0.12–0.30 a month |

Bytes scale with built cells: the pilot stored 2.7 bytes a sprout built cell, and the task 04 windows 1.0–2.0, where TEMPO's dense towns pack better than Sahel villages. That gives 2.2–3.8 million tiles and 6–15 GB, against the task 04 estimate of 10–25 GB. About $1.4–3.2 in all, inside the task 04 estimate of $1.5–3.8. Reads of the sprout and the shards inside `europe-west1` are free.

**Shards, all.** Approved by the user on 19 November 2026 with the revised cost above. Submitted at 20:16 UTC: 557 tasks, one shard each, 12 at a time, spot `e2-standard-2`, 30 GB `pd-standard`, up to 3 retries a task. Shard `5-15-14` is skipped, done by the pilot.

- job: `buildings-2023-v1-tiles-202611192015`, `europe-west1`
- output: `gs://nature-watch-bucket/scratch/buildings/buildings-2023-v1/tiles/shards/`
- status: `gcloud batch jobs describe buildings-2023-v1-tiles-202611192015 --location=europe-west1 --project=nature-watch-387210`
- tasks: `gcloud batch tasks list --job=buildings-2023-v1-tiles-202611192015 --location=europe-west1 --project=nature-watch-387210`
- cancel: `gcloud batch jobs delete buildings-2023-v1-tiles-202611192015 --location=europe-west1 --project=nature-watch-387210`
- totals: `python buildings/batch.py records`
- result: succeeded, run duration 25,519 s (7 h 5 min), 557 of 557 tasks. 362 shards with tiles: 1,519,825 tiles, 9,891,945,147 bytes. Shard time summed to 196,201 s (54.5 hours, the pilot included), of which 811 s copying blocks; the longest shard 1,759 s; peak memory 0.83 GB. Fewer tiles than estimated (2.2–3.8 million) and bytes inside the range (6–15 GB): more valid tiles than expected held no building and were not stored. Compute about $1.4–1.9 at spot `e2-standard-2` rates, plus about $0.09 of disk.

**Merge.** Approved by the user on 20 November 2026 (expected 1–2 hours, $0.15–0.30). Submitted at 07:00 UTC: one spot `e2-standard-4`, 150 GB `pd-standard`, one retry.

- job: `buildings-2023-v1-merge-202611200700`, `europe-west1`
- output: `gs://nature-watch-tiles/buildings/2023/v1/buildings.pmtiles`
- status: `gcloud batch jobs describe buildings-2023-v1-merge-202611200700 --location=europe-west1 --project=nature-watch-387210`
- cancel: `gcloud batch jobs delete buildings-2023-v1-merge-202611200700 --location=europe-west1 --project=nature-watch-387210`
- result: failed after 1,233 s, before upload; about $0.02. The sprout check found 12 tiles under `8/205/112` that differed from shard `5-25-14`: 2–74 pixels each, `no building` against the faintest shade. The cause was not the data: a tile worked out its window and warp origin from its raster's corner, so on a zoom 12 column that starts on a cell edge (here 108.28125° E) a shard's mosaic and the VRT rounded apart by about 10⁻⁸ of share. Fixed in code version 2 by working on the global cell grid. Rebuilt from both corners, 1,176 tiles under eight subtrees of that shard matched, and the Kocho and edge windows still matched task 04 (17 of 17, 63 of 63). Nothing was published.

**Shards, rerun with code version 2.** Approved by the user on 20 November 2026, with a switch to spot `n2-standard-2`, 64 at a time, to cut the wall time to about 1–1.5 hours (about $1.5–2.5, plus about $0.10 of disk). Submitted at 11:19 UTC: 557 tasks, 30 GB `pd-standard`, up to 3 retries a task.

- job: `buildings-2023-v1-tiles-202611201119`, `europe-west1`
- output: `gs://nature-watch-bucket/scratch/buildings/buildings-2023-v1/tiles/shards/`
- status: `gcloud batch jobs describe buildings-2023-v1-tiles-202611201119 --location=europe-west1 --project=nature-watch-387210`
- cancel: `gcloud batch jobs delete buildings-2023-v1-tiles-202611201119 --location=europe-west1 --project=nature-watch-387210`
- result: pending
