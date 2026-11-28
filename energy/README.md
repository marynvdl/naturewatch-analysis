# energy: power lines and power sites

Scripts for the Energy layer: one seed read from Overture's `base/infrastructure` theme, two sprouts on the stats grid, a raster PMTiles archive for power lines and a vector one for power sites. The recipes are [`cookbook/recipes/power-lines-2026-10-v1.yaml`](../cookbook/recipes/power-lines-2026-10-v1.yaml) and [`power-sites-2026-10-v1.yaml`](../cookbook/recipes/power-sites-2026-10-v1.yaml), and `rules.py` reads its rules from them. The parcel is `docs/parcels/energy.md` in naturewatch-app.

Task 01 ran these on the laptop for two windows. The user approved the classes, the attributes, leaving out Global Renewables Watch, a seed of every infrastructure subtype, the tile settings and the heatmap design on 27 November 2026.

## Run

```bash
pip install -r energy/requirements.txt
python energy/seed.py all                 # every infrastructure row in each window: data/energy/raw/<name>.parquet
python energy/seed.py seed                # the window seeds: data/energy/seed/<name>.parquet
python energy/seed.py schema              # data/energy/schema-classes.csv and schema-tags.csv
python energy/seed.py footers             # data/energy/footers.json: rows per 10° block, footers only
python energy/sprout.py sites karoo       # data/energy/sites/karoo.parquet: kept sites, group and card
python energy/sprout.py window karoo      # data/energy/sprout/power-lines/karoo.tif and power-sites/karoo.tif
python energy/tiles.py lines karoo        # data/energy/tiles/power-lines-karoo.pmtiles
python energy/tiles.py sites karoo        # data/energy/tiles/power-sites-karoo.pmtiles (Tippecanoe)
python energy/render.py check karoo --bbox 23.98 -30.70 24.12 -30.60 --zoom 14   # features over imagery
python energy/render.py sample karoo --lines data/energy/tiles/power-lines-karoo.pmtiles --zoom 8
MAPBOX_ACCESS_TOKEN=pk... python energy/preview.py   # http://127.0.0.1:8766/preview.html
python -m pytest energy
```

The global runs are Batch jobs in `europe-west1`, as roads (`roads/batch.py`), with the helpers reused:

```bash
python energy/batch.py submit seed --pilot 40N_000E [--approved]    # one block; prints the job, submits only with --approved
python energy/batch.py submit sprout --pilot 40N_000E [--approved]  # both sprouts for one block, to the scratch prefix
python energy/batch.py submit seed --approved                       # every block, at most 20 tasks
python energy/batch.py submit sprout --approved                     # after every seed block has a record
python energy/batch.py collect                                      # both VRTs and STAC files
python energy/batch.py records seed|sprout [--pilot]                # totals from the records so far
```

A seed block holds every infrastructure row whose bbox centre is in it, and its record counts rows by subtype and power class. One sprout task writes both sprouts for a block, from the power rows of its seed and its eight neighbours' whose bbox reaches into it, so a site across a block edge marks both sides with the group from its whole shape. One record per block, beside the `power-lines` blocks, holds both layers. The container has no PyYAML, so a task reads JSON copies of the two recipes.

`boxes.yaml` names the two 2° windows: `karoo` (22.3–24.3°E, 32.6–30.6°S: Beaufort West, Karoo National Park, De Aar) and `gironde` (1.5°W–0.5°E, 43.8–45.8°N: Bordeaux, the Landes, Cestas). Overture is read with DuckDB from its public bucket, only the row groups whose bbox meets the window. Global Renewables Watch v1.0 is read from its two GeoPackages in `data/energy/grw/`. Imagery is Esri World Imagery, cached in `data/energy/imagery/`.

## Method

**Seed.** Every `base/infrastructure` row, so Water can reuse the read: Overture `id`, `subtype`, `class`, the primary name, the first OpenStreetMap record without its version (`n…`, `w…`, `r…`), `wikidata`, `source_tags` (the raw OpenStreetMap tags), `height`, `surface`, `level`, `bbox` and the geometry.

**Power lines.** `power_line`, `minor_line`, and `cable` only when tagged `location=overhead`. A line tagged underground, underwater or indoor is left out. The voltage class comes from the highest value in `voltage`:

| Class | Name | Voltage | Seen in the windows |
| --- | --- | --- | --- |
| 4 | extra-high | 330 kV and up | 400, 765 kV |
| 3 | transmission | 200–330 kV | 220, 225 kV |
| 2 | sub-transmission | 50–200 kV | 63, 66, 90, 132 kV |
| 1 | distribution | under 50 kV | 20, 33 kV, 400 V |

Without a voltage, a `power_line` is 2 and a `minor_line` 1. A cell is a line cell when a kept line touches it (all-touched, as roads), and the highest class wins.

**Power sites.** Each kept site has a source group. A site with several sources takes the first in this order, so the dirtiest wins: coal, oil and gas (`fossil`), nuclear, bioenergy and waste, hydro, wind, solar, other, storage. Substations are their own group.

- Plants: kept, as polygons and points, group from `plant:source`.
- Generators: group from `generator:source`. Polygons from 0.1 ha. Points are kept except solar, so a wind turbine marks its own cell. A generator on a roof is left out.
- Substations: polygons from 100 m². Points are left out.
- Towers, poles, switches, portals, transformers and insulators are left out.

A polygon marks the cells whose centre it holds, a point the cell it falls in. The burn value rises with the group, so the dirtiest group wins a cell: 1 substation, 2 storage, 3 other, 4 solar, 5 wind, 6 hydro, 7 bioenergy and waste, 8 nuclear, 9 oil and gas, 10 coal.

**Feature card.** Each site keeps `name`, `operator`, `source` (raw `plant:source` or `generator:source`), `method`, `output` (`plant:output:electricity` or `generator:output:electricity`), `voltage`, `start_date` and `osm_id`, with `class`, `group` and `area_m2`.

**Tiles.** Power lines use the roads encoding (`roads/tiles.py`, which reads a one-band sprout as level with no surface): 512 px palette PNG, top zoom 11, below it 8 shades in √(share / 0.1), at zoom 11 red = class × 4. Power sites are one Tippecanoe layer `sites`, zooms 2–14. To zoom 9 each site is a point inside it, for a heatmap drawn in the client. From zoom 10 it is its own shape. Crowded points merge (`--cluster-densest-as-needed`, 12 px), keeping `point_count` and summing `area_m2` and, per group g, `area_<g>` and `n_<g>`. No site's weight is lost zoomed out, and the client can draw the heat of one group.

## Task 01 sample (27 November 2026)

Overture release `2026-10-21.0`. Laptop, DuckDB 2.0.0, GDAL 3.11.4, Tippecanoe, Python 3.14.

### What the theme holds

| | karoo | gironde |
| --- | --- | --- |
| Infrastructure rows | 9,023 (86% `power`, mostly 5,655 towers) | 398,918 (16% `power`) |
| Raw read | 17.5 s, 0.79 MB, peak 0.57 GB | 26.8 s, 34.5 MB, peak 1.24 GB |
| Power lines with `voltage` | 95 of 99 | 1,508 of 1,541 |
| Plants with source / name / output / start date | 11 / 10 / 11 / 5 of 11 | 219 / 138 / 208 / 145 of 221 |
| Plant `operator` | 5 of 11 | 55 of 221 |
| Generators with a source | 1,646 of 1,646 | 6,688 of 6,705 |
| Generator polygons inside a plant | 1,307 of 1,646 | 4,985 of 6,705 |
| Wind turbines | 205 points, no wind-farm polygon | 21 |

`owner` is never tagged. Water and sewage treatment plants and pumping stations are not in `base/infrastructure` in either window. The `water` subtype holds dams, weirs and drinking water, and `utility` holds pipelines, covered reservoirs and water towers. The schema counts are in `data/energy/schema-classes.csv` and `schema-tags.csv` (not committed).

### Global Renewables Watch, compared

Global Renewables Watch v1.0 (March 2025): solar PV polygons and onshore wind turbine points to the second quarter of 2024, detected from imagery. The repository's only licence is MIT, and the dataset states none of its own.

| | karoo | gironde |
| --- | --- | --- |
| Solar, OpenStreetMap (union) | 17.14 km² | 38.52 km² |
| Solar, Global Renewables Watch | 5 features, 1.88 km² | 223 features, 30.96 km² |
| Overlap | 1.69 km² | 28.66 km² |
| Only in Global Renewables Watch | 1 feature, 0.13 km² | 33 features, 1.07 km² |
| OpenStreetMap sites of 1 ha and up not in Global Renewables Watch | 4, 13.27 km² | 38, 4.79 km² |
| Wind turbines, OpenStreetMap / Global Renewables Watch | 205 / 108, all 108 within 150 m of an OpenStreetMap turbine | 21 / 0 |

Over imagery: the four large De Aar plants (645, 296, 200 and 187 ha) are real and missing from Global Renewables Watch. So are the 97 OpenStreetMap turbines near 24.0°E 31.85°S, on pads and access roads. The largest Global Renewables Watch-only feature in Gironde (8 ha at 0.30°E 44.34°N) is a vineyard, and the largest OpenStreetMap-only one (59 ha at 0.65°W 44.11°N) is a solar park. Global Renewables Watch is not used. It is in the parcel's Deferred list as a possible gap-fill where OpenStreetMap is thin.

### Sprouts and tiles per window

| | karoo | gironde |
| --- | --- | --- |
| Line features burned | 114 | 5,159 |
| Line cells (of 64,000,000) | 104,348 | 351,347 |
| `power-lines` COG | 144,160 bytes | 399,119 bytes |
| Sites kept | 296 (9 solar plants 17.1 km², 51 solar generators, 205 turbines, 29 substations, 1 storage, 1 oil) | 1,141 (188 solar plants 37.7 km², 650 solar generators, 229 substations, 21 turbines, 1 nuclear plant, 15 bioenergy, 6 oil and gas, 5 hydro) |
| Site cells | 24,907 | 74,846 |
| `power-sites` COG | 79,093 bytes | 92,592 bytes |
| Burn, both layers | under 1 s, peak 1.0 GB | under 1 s, peak 1.0 GB |
| `power-lines` archive | 105 tiles, 139,835 bytes, largest 5,640 | 251 tiles, 645,991 bytes, largest 12,478 |
| `power-sites` archive | 387 features written, 97,708 bytes, largest tile 1,411 | 2,242 features written, 657,889 bytes, largest tile 5,970 |

A burn holds the 64 million-cell window in memory. The tile builds take under 6 s at a peak of 0.3 GB.

### Choices

- **Share curve.** `share_full` 0.25 (the roads value) left the Karoo transmission lines faint at zoom 6. At 0.05, Gironde's 20 kV network became a haze. 0.1 shows lines at zooms 4–8 in both. France maps its distribution lines and the Karoo barely does, so Gironde looks denser zoomed out partly for that reason. The voltage band lets the pressure sandbox weight only lines from 50 kV up.
- **Top zoom.** 11, as roads. The 765 and 400 kV corridors at De Aar and the 225 kV lines around Cestas are crisp there.
- **Sites zoomed out.** Five views were compared in `preview.py`: points with the densest dropped, clusters with a count, points by size, a heatmap, and the footprint as a raster share. The user chose the heatmap. Its colours, radius, intensity, opacity and weight are paint only. The preview offers five palettes, and a weight of √area with each site at least 1 ha.

Renders are in `data/energy/renders/` (not committed).

## Task 02 runs (28 November 2026)

**Seed pilot, `40N_000E`.** The first submission, `energy-2026-10-seed-pilot-40n-000e-202611280936`, failed at start-up: `roads/batch.py` imported `tiles/shards.py`, which needs `pmtiles`, at the top. It now imports it only where a job is built. Resubmitted at 09:41 UTC as `energy-2026-10-seed-pilot-40n-000e-202611280940`: one spot `e2-standard-4`, 50 GB disk. It writes `gs://nature-watch-bucket/seeds/infrastructure/overture/2026-10-21.0/40N_000E.parquet` and `.json`, as the full run would.

```bash
gcloud batch jobs describe energy-2026-10-seed-pilot-40n-000e-202611280940 --location=europe-west1 --project=nature-watch-387210
gcloud batch jobs delete energy-2026-10-seed-pilot-40n-000e-202611280940 --location=europe-west1 --project=nature-watch-387210   # cancel
```

Seed pilot result: 14,518,760 rows (2,595,327 `power`), 1,068,153,362 bytes, 48.0 s of query, 69 s of task, peak 9.2 GB.

**Sprout pilot, `40N_000E`.** Submitted at 09:46 UTC as `energy-2026-10-sprout-pilot-40n-000e-202611280945`: one spot `e2-highmem-2`, 100 GB `pd-standard`. Its neighbours have no seed yet, so it reads only its own block, and writes to `gs://nature-watch-bucket/scratch/energy/seed-infrastructure-overture-2026-10-21/pilot/`.

```bash
gcloud batch jobs describe energy-2026-10-sprout-pilot-40n-000e-202611280945 --location=europe-west1 --project=nature-watch-387210
gcloud batch jobs delete energy-2026-10-sprout-pilot-40n-000e-202611280945 --location=europe-west1 --project=nature-watch-387210   # cancel
```

Sprout pilot result: 111 s of task, 101 s for the block (30 s fetching its 1.07 GB seed), peak 3.0 GB.

| | `power-lines` | `power-sites` |
| --- | --- | --- |
| Features burned | 200,342 | 32,903 |
| Marked cells | 12,897,827 (7.03 M distribution, 3.50 M sub-transmission, 1.36 M transmission, 0.99 M extra-high) | 626,333 (416,037 solar, 100,525 substation, 28,873 bioenergy and waste, 28,825 nuclear, 16,360 wind, 15,188 oil and gas, 8,909 hydro, 7,854 coal, 3,470 other, 292 storage) |
| COG | 13,182,982 bytes, 1.0 a cell | 2,071,666 bytes, 3.3 a cell |
| Burn and write | 32.6 s | 38.8 s |

**Check against task 01.** In the part of the Gironde window inside the block (0–0.5°E, 43.8–45.8°N), `power-sites` matches task 01 in every cell. `power-lines` has 109,934 cells against 114,639. Task 01's window sprout burned again from only the lines whose bbox centre is east of 0° gives 109,933 cells, one cell from the pilot: the 4,705 missing cells are lines stored in `40N_010W`, which the pilot did not read, and the one extra is on the window edge, from a line just outside the window that the one-cell margin reads. The full run reads every neighbour.

**Revised cost, from the pilots.** For approval before the full jobs. Spot prices about $0.07 an hour for `e2-standard-4` and $0.045 for `e2-highmem-2`. The project's 32-CPU Batch quota runs 8 seed machines or 16 sprout machines at once, which lengthens the wall time, not the cost.

| Step | Work | Machine and time | Compute | Stored | Storage a month |
| --- | --- | --- | --- | --- | --- |
| Seed | 648 blocks in 20 tasks; 159 million rows at 48 s per 14.5 million (the densest block), plus about 1 s for each empty block and a few minutes of start-up a task | spot `e2-standard-4`, 1–2 task-hours, about 20–40 minutes of wall time | about $0.07–0.15 | about 11.7 GB (1.07 GB for 14.5 million rows) | about $0.27 |
| Sprouts | 648 blocks in 20 tasks; about 330 have power in or beside them, at 40–100 s each for both layers, plus neighbour seed fetches at about 35 MB/s; the rest write only a record | spot `e2-highmem-2`, 4–10 task-hours, about 20–45 minutes of wall time | about $0.20–0.45, plus about $0.03 of standard disk | 0.1–0.3 GB (1.0 byte a line cell, 3.3 a site cell; the densest block holds 13.5 million marked cells) | under $0.01 |

One-off compute about $0.30–0.65, against $0.25–0.80 in the task 01 estimate. Both pilots used about 4 machine-minutes, under $0.01. The pilot COGs in the scratch prefix are deleted after the full run.

**Seed, all blocks.** Approved by the user on 28 November 2026 with the revised cost above. Submitted at 09:53 UTC as `energy-2026-10-seed-202611280952`: 20 tasks, 648 blocks (1–195 a task), spot `e2-standard-4`, 50 GB disk, up to 3 retries a task. `40N_000E` is skipped, done by the pilot.

```bash
gcloud batch jobs describe energy-2026-10-seed-202611280952 --location=europe-west1 --project=nature-watch-387210
gcloud batch tasks list --job=energy-2026-10-seed-202611280952 --location=europe-west1 --project=nature-watch-387210
gcloud batch jobs delete energy-2026-10-seed-202611280952 --location=europe-west1 --project=nature-watch-387210   # cancel
python energy/batch.py records seed
```

Seed result: the job ran 4 min 14 s on 20 spot machines, 09:54–09:59 UTC, every task succeeding first time. 648 records, 392 blocks with rows. 158,979,260 rows, the footer count exactly, in 11,619,756,637 bytes, 1,081 s of query in all, peak 9.2 GB (the densest block, the pilot). By subtype: 51,620,449 `power`, 34,833,886 `barrier`, 27,112,484 `transportation`, 19,697,953 `transit`, 7,686,102 `bridge`, 7,110,480 `pedestrian`, 2,545,426 `emergency`, 2,180,019 `utility`, 2,164,164 `waste_management`, 1,056,858 `water`, and under a million each of `pier`, `communication`, `airport`, `manhole`, `tower`, `aerialway`, `recreation` and `quay`. The power rows the sprouts keep are among 1,182,078 `power_line`, 1,647,311 `minor_line`, 183,182 `cable`, 151,792 `plant`, 6,749,564 `generator` and 878,544 `substation`.

**Sprouts, all blocks.** Submitted at 10:01 UTC as `energy-2026-10-sprout-202611281001`, after every seed block had a record: 20 tasks, 648 blocks (11–77 a task), spot `e2-highmem-2`, 100 GB `pd-standard`, up to 3 retries a task.

```bash
gcloud batch jobs describe energy-2026-10-sprout-202611281001 --location=europe-west1 --project=nature-watch-387210
gcloud batch tasks list --job=energy-2026-10-sprout-202611281001 --location=europe-west1 --project=nature-watch-387210
gcloud batch jobs delete energy-2026-10-sprout-202611281001 --location=europe-west1 --project=nature-watch-387210   # cancel
python energy/batch.py records sprout
```

Sprout result: the job ran 31 min, 10:04–10:35 UTC, every task succeeding first time; the 32-CPU quota held some machines back until others finished. 648 records. Block time 11,669 s in all (738 s of seed fetches), the longest block 129 s (`50N_010E`), peak 3.1 GB.

| | `power-lines` | `power-sites` |
| --- | --- | --- |
| Blocks with a COG | 266 | 298 |
| Marked cells | 388,310,531: 100.9 M distribution, 156.7 M sub-transmission, 64.5 M transmission, 66.2 M extra-high | 80,963,169: 39.2 M wind, 31.9 M solar, 3.9 M substation, 2.9 M coal, 1.5 M oil and gas, 0.5 M nuclear, 0.4 M other, 0.3 M bioenergy and waste, 0.2 M hydro, 27,000 storage |
| COGs | 794,346,803 bytes, 2.0 a cell | 552,484,810 bytes, 6.8 a cell |

`collect` wrote `power-lines-2026-10.vrt` and `power-sites-2026-10.vrt` with their `collection.json` and item JSON beside the blocks.

**Check against task 01.** Karoo matches in every cell for both sprouts. Gironde `power-sites` matches in every cell. Gironde `power-lines` has 351,350 cells against 351,347, with 27 differing: 26 are one segment lying exactly on the cell edge at 45.7005°N, 0.0655–0.0688°E, burned one row lower in the block grid than in the window grid, and one is a single cell at 45.451°N 0.170°E. The burn origin differs (the block corner, not the window corner), so a segment on a cell edge can fall on either side.

**Wind cells.** 39.2 million of the 81.0 million site cells are wind, against none from polygons in the task 01 windows, where wind was turbine points. Most are offshore wind farms mapped as plant polygons over open sea: in `50N_000E` the most wind cells per 0.5° are at Dogger Bank, Hornsea, the German Bight and the Thames estuary, and in `30N_120E` off the Jiangsu coast.

The user chose on 28 November 2026 to burn wind farms by their turbines only: `outline_only: [wind]` in the `power-sites` recipe. A wind plant polygon keeps its group and card for the tiles, and is left out of the burn. Neither task 01 window has a wind plant polygon, so their figures above are unchanged (rebuilt on the laptop: the same cells and bytes).

**Sprouts, second run.** The 648 block records and the 298 `power-sites` COGs of the first run were deleted, and the `power-lines` COGs are written again unchanged. Submitted at 10:43 UTC as `energy-2026-10-sprout-202611281042`, the same shape.

```bash
gcloud batch jobs describe energy-2026-10-sprout-202611281042 --location=europe-west1 --project=nature-watch-387210
gcloud batch jobs delete energy-2026-10-sprout-202611281042 --location=europe-west1 --project=nature-watch-387210   # cancel
```

Second run result: 31 min, 10:44–11:15 UTC, every task succeeding first time, block time 11,969 s in all (625 s of fetches), peak 3.1 GB. `power-lines` is the same as in the first run, cell for cell in the totals. `power-sites` has 42,231,558 marked cells in 298 blocks, 551,995,696 bytes: 31.9 M solar, 3.9 M substation, 2.9 M coal, 1.5 M oil and gas, 0.52 M nuclear, 0.47 M wind (from 39.2 M), 0.42 M other, 0.34 M bioenergy and waste, 0.24 M hydro, 27,000 storage. The windows match as in the first run: Karoo and Gironde sites in every cell, Gironde lines but for the 27 edge cells above. `collect` wrote both VRTs and STAC sets again, and the pilot files in the scratch prefix were deleted.

Each block COG keeps about 1.9 MB of empty 512-pixel tiles, so the sprouts are about 1.35 GB in all, not the 0.1–0.3 GB estimated from the marked cells.

**Cost.** At spot prices of about $0.07 an hour for `e2-standard-4` and $0.045 for `e2-highmem-2`:

| Run | Machine time | Compute |
| --- | --- | --- |
| Pilots, the failed first seed pilot included | about 5 machine-minutes | under $0.01 |
| Seed | 20 tasks of about 4 minutes with start-up, about 1.3 machine-hours | about $0.10 |
| Sprouts, first run | 3.2 hours of blocks, about 4 machine-hours with start-up | about $0.18 |
| Sprouts, second run | 3.3 hours of blocks, about 4 machine-hours with start-up | about $0.19 |
| Standard disk, 100 GB a sprout machine | about 800 GB-hours | about $0.05 |

About $0.50 in all, inside the approved $0.30–0.65 for the seed and one sprout run plus $0.20–0.45 for the rerun. Reading Overture and the seed inside the region cost nothing; the window check copied about 50 MB out. Storage is about $0.27 a month for the seed and $0.03 for the sprouts at $0.023 per GB.
