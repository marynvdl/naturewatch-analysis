# buildings: comparison boxes for the Buildings 2023 layer

Scripts that read Google Open Buildings 2.5D Temporal, Microsoft TEMPO and Overture building outlines in small boxes, on the sprout grid. They informed the values in [`cookbook/recipes/buildings-2023-v1.yaml`](../cookbook/recipes/buildings-2023-v1.yaml), and task 03 uses them again to check the sprout. The parcel is `docs/parcels/buildings-2023.md` in naturewatch-app.

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
