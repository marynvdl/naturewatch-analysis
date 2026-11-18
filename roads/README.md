# roads: Overture roads seed, sprout and tiles

Scripts for the Roads layer: a seed read from Overture's transportation theme, a sprout on the stats grid, and raster PMTiles from that sprout. The recipe is [`cookbook/recipes/roads-2026-10-v1.yaml`](../cookbook/recipes/roads-2026-10-v1.yaml). The parcel is `docs/parcels/roads.md` in naturewatch-app.

Task 01 ran these on the laptop for ten small boxes and two windows. The user approved the levels, the burn rule, top zoom 11 and `share_full` 0.25 on 17 November 2026, and kept the seed. The global seed and sprout were written on 18 November 2026 by Batch jobs from `roads/batch.py` (task 02), and the tiles the same day (task 03).

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
MAPBOX_ACCESS_TOKEN=pk... python roads/preview.py --remote https://storage.googleapis.com/nature-watch-tiles/roads/2026-10/v1/roads.pmtiles
python roads/render.py tiles https://storage.googleapis.com/nature-watch-tiles/roads/2026-10/v1/roads.pmtiles --zoom 8 --bbox 27 -26.5 29 -24.5
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

Zoom 11 (about 38 m a pixel at the equator with 512 px tiles, against 28 m cells) draws continuous lines in both windows. Zoom 12 is a little sharper and 2.6 times the bytes in the dense window. Zoom 11 is chosen, the same as cropland version 3.

### Shades

Road share is small: a rural network fills 1–5% of cells, a city 20–40%. With shades spaced in √share up to a share of 1 (cropland's curve), 78% of the dense window's road pixels at zoom 4 fall in the two faintest shades. Spacing them in √(share / 0.25), so a share of 25% or more is the darkest shade, spreads them across all eight and keeps cities and rural networks apart (`render.py` comparison at zoom 6, `z6-full.png`). The whole share-0.25 archive is 3,460,034 bytes against 3,697,509.

| Zoom 4 road pixels per shade 1–8 | full share 1 | full share 0.25 |
| --- | --- | --- |
| gauteng-waterberg | 1,159; 604; 248; 121; 122; 6; 0; 0 | 234; 659; 457; 307; 186; 132; 64; 221 |
| chinko | 169; 0; 0; 0; 0; 0; 0; 0 | 130; 38; 1; 0; 0; 0; 0; 0 |

## Global seed and sprout (task 02)

```bash
python roads/batch.py submit seed --pilot 30S_020E [--approved]   # one block; prints the job, submits only with --approved
python roads/batch.py submit seed --approved                      # all 648 blocks in 20 tasks
python roads/batch.py records seed                                # totals from the seed records written so far
python roads/batch.py submit sprout --approved                    # after every seed block has a record
python roads/batch.py collect                                     # the VRT and STAC files, after every sprout block has a record
```

**Blocks.** 648 blocks of 10°, named from the south-west corner (`30S_020E`). A segment belongs to the block that holds the centre of its `bbox`, so each seed row is stored once (`blocks.py`). The blocks are cut, north to south and west to east, into 20 runs of similar weight: footer rows for the seed, the rows of a block and its neighbours for the sprout. One Batch task does one run.

**Seed.** A task opens one DuckDB session, so the 128 Overture footers are read once per task. For each block it reads the row groups whose bbox meets the block, keeps the rows whose centre is in it, sorts them along a Hilbert curve over the block and writes `<block>.parquet` and `<block>.json` under the seed path. A block with no row writes only the record. Spot `e2-standard-4`, DuckDB limited to 10 GB.

**Sprout.** For each block a task copies the seed files of the block and its eight neighbours (keeping the ones the next block needs), and DuckDB writes the rows whose bbox meets the block, padded by a cell, to one FlatGeobuf sorted by burn code, with no spatial index so the order holds. GDAL burns it in one pass with `ALL_TOUCHED`, so a later, higher code wins a cell. The codes are decoded in strips of 2,048 rows into the two bands and written as a COG under `blocks/`, with a record. A block without a road cell writes only the record. `collect` writes `roads-2026-10.vrt` over the COGs, with no nodata, so a block without a COG reads 0, and `collection.json` and `roads-2026-10-v1.json`. On the two task 01 windows, the one-pass burn gives the same cells as the twelve-pass `burn` of task 01.

A record is written after its object, so a block with a record is done, and a rerun of the same job skips it. Batch retries a task up to 3 times, and retries a spot preemption (exit 50001) separately. A sprout pilot writes to `gs://nature-watch-bucket/scratch/roads/roads-2026-10-v1/pilot/`, since its neighbours' seed blocks may not exist yet. The container is `ghcr.io/osgeo/gdal:ubuntu-small-3.11.4` with the DuckDB 2.0.0 wheel copied to the scratch prefix.

### Runs

**Seed pilot, block `30S_020E`.** Submitted 17 November 2026 at 17:54 UTC, approved with the task file (expected under $0.05 with the sprout pilot). One spot `e2-standard-4`, 50 GB disk.

- job: `roads-2026-10-v1-seed-pilot-30s-020e-202611171753`, `europe-west1`
- output: `gs://nature-watch-bucket/seeds/roads/overture/2026-10-21.0/30S_020E.parquet` and `30S_020E.json`; the full run skips this block
- status: `gcloud batch jobs describe roads-2026-10-v1-seed-pilot-30s-020e-202611171753 --location=europe-west1 --project=nature-watch-387210`
- cancel: `gcloud batch jobs delete roads-2026-10-v1-seed-pilot-30s-020e-202611171753 --location=europe-west1 --project=nature-watch-387210`
- result: succeeded, run duration 57 s. `30S_020E`: 1,995,149 rows, 253,013,161 bytes (127 a row), 44.2 s for the block, peak memory 4.4 GB. Its rows that meet the gauteng-waterberg window are the 715,036 rows of task 01, the same ids.

**Sprout pilot, block `30S_020E`.** Submitted 17 November 2026 at 18:05 UTC. One spot `e2-standard-2`, 100 GB disk. Only this block's seed existed, so roads from its neighbours are missing near its edges (`seed_missing` in the record).

- job: `roads-2026-10-v1-sprout-pilot-30s-020e-202611171804`, `europe-west1`
- output: `gs://nature-watch-bucket/scratch/roads/roads-2026-10-v1/pilot/30S_020E.tif` and `.json`, removed on 18 November 2026
- result: succeeded, run duration 92 s. 1,995,149 features, 1.6 billion cells, 26,306,817 road cells (track 10,881,011; minor 9,653,522; main 5,210,866; major 561,418), 29,245,015 bytes (1.11 a road cell), 84.0 s for the block of which 4.4 s copying the seed, peak memory 3.6 GB.

**Window check.** The gauteng-waterberg window cut from the pilot COG against the task 01 window sprout: 98 of 64,000,000 cells differ, and the window has 27 fewer road cells (4,744,115 against 4,744,142). Each of the 87 cells that is a road cell in one and not the other has a road cell beside it in both; 2 road cells change level and 9 change only surface. These are lines that pass within rounding error of a cell corner, where `ALL_TOUCHED` gives a different answer for a different raster extent or machine: burning the same window rows on the laptop onto the whole block's raster still differs from the pilot in 67 cells. Every Batch task burns a whole block on the same image, so the global sprout is consistent with itself.

**Revised cost, from the pilots.** For approval before the full jobs.

| Job | Work | Machine and time | Compute | Stored | Storage a month |
| --- | --- | --- | --- | --- | --- |
| Seed | 648 blocks in 20 tasks; 291 million rows at 44 s per 2 million, plus about 1 s for each empty block and a few minutes of start-up per task | spot `e2-standard-4`, 2–5 task-hours, about 15–30 minutes of wall time | about $0.15–0.40 | about 37 GB | about $0.85 |
| Sprout | 648 blocks in 20 tasks; about 300 have roads, at 60–180 s each with the neighbour copies; the rest write only a record | spot `e2-standard-2`, 5–15 task-hours, about 20–45 minutes of wall time | about $0.15–0.60 | 2–3.5 GB (1.1 bytes a road cell, 1.8–3.1 billion road cells) | about $0.05–0.08 |

Reading Overture costs nothing, and reading the seed inside `europe-west1` is free. Both pilots together used about 2.5 machine-minutes, under $0.01.

**Seed, all blocks.** Approved by the user on 18 November 2026 with the revised cost above. Submitted at 06:33 UTC: 20 tasks, 648 blocks, spot `e2-standard-4`, 50 GB disk, up to 3 retries a task.

- job: `roads-2026-10-v1-seed-202611180633`, `europe-west1`
- output: `gs://nature-watch-bucket/seeds/roads/overture/2026-10-21.0/`, `<block>.parquet` and `<block>.json`
- status: `gcloud batch jobs describe roads-2026-10-v1-seed-202611180633 --location=europe-west1 --project=nature-watch-387210`
- tasks: `gcloud batch tasks list --job=roads-2026-10-v1-seed-202611180633 --location=europe-west1 --project=nature-watch-387210`
- cancel: `gcloud batch jobs delete roads-2026-10-v1-seed-202611180633 --location=europe-west1 --project=nature-watch-387210`
- totals: `python roads/batch.py records seed`
- result: succeeded, run duration 1,478 s (24.6 minutes), 20 of 20 tasks, no failed retry. 648 records, 363 blocks with rows: 307,681,940 rows, 41,171,304,479 bytes. Block time summed to 8,020 s (2.2 hours). The densest block, `40N_000E`, has 13,662,495 rows (2.11 GB) and took 350 s, at a peak of 12.2 GB.

**Sprout, all blocks.** Submitted 18 November 2026 at 07:04 UTC: 20 tasks, 648 blocks, spot `e2-highmem-2` (16 GB), 100 GB disk, up to 3 retries a task. The machine is larger than the pilot's `e2-standard-2`: `40N_000E` and its neighbours hold 61,884,677 rows, 31 times the pilot. That adds about $0.10 at most, and the whole run stays under the approved $1.0.

- job: `roads-2026-10-v1-sprout-202611180704`, `europe-west1`
- output: `gs://nature-watch-bucket/sprouts/roads/2026-10/v1/blocks/`, `<block>.tif` and `<block>.json`
- status: `gcloud batch jobs describe roads-2026-10-v1-sprout-202611180704 --location=europe-west1 --project=nature-watch-387210`
- tasks: `gcloud batch tasks list --job=roads-2026-10-v1-sprout-202611180704 --location=europe-west1 --project=nature-watch-387210`
- cancel: `gcloud batch jobs delete roads-2026-10-v1-sprout-202611180704 --location=europe-west1 --project=nature-watch-387210`
- totals: `python roads/batch.py records sprout`
- result: cancelled at about 07:20 UTC after 6 of 20 tasks failed. A block that its neighbours' roads do not reach, such as open sea, made DuckDB leave an empty FlatGeobuf that GDAL could not open, and every retry failed the same way. Only 4–5 machines ran at once: 100 GB `pd-balanced` disks count against the region's 500 GB `SSD_TOTAL_GB` quota. 51 blocks were written and checked by their records; the rerun skips them. Fixed in `write_burn_source`, with a test.

**Sprout, all blocks, second submit.** 18 November 2026 at 07:25 UTC. As above, with `pd-standard` disks, which count against the 4,096 GB `DISKS_TOTAL_GB` quota. The region's 24 `E2_CPUS` allow 12 of the 20 tasks at once.

- job: `roads-2026-10-v1-sprout-202611180725`, `europe-west1`
- status: `gcloud batch jobs describe roads-2026-10-v1-sprout-202611180725 --location=europe-west1 --project=nature-watch-387210`
- cancel: `gcloud batch jobs delete roads-2026-10-v1-sprout-202611180725 --location=europe-west1 --project=nature-watch-387210`
- result: succeeded, run duration 4,884 s (81 minutes), 20 of 20 tasks. 648 records, 367 blocks with road cells. `collect` wrote `roads-2026-10.vrt`, `collection.json` and `roads-2026-10-v1.json` at 09:31 UTC.

### Global result

| | Seed | Sprout |
| --- | --- | --- |
| Blocks with data | 363 | 367 |
| Rows or features | 307,681,940 rows | 307,820,053 features burned (a road that crosses a block edge is burned in each block it reaches) |
| Road cells | | 4,020,450,736: track 1,889,210,878; minor 1,296,023,376; main 694,017,118; major 141,199,364 |
| Road cells by surface | | paved 565,921,241; unpaved 843,373,541; the rest unknown |
| Bytes stored | 41,171,418,381 with the records | 4,893,546,903 with the VRT and STAC files |
| Block time summed | 8,020 s | 33,912 s, plus 3,925 s in the cancelled run |
| Peak memory | 12.2 GB (`40N_000E`) | 8.7 GB |

**Check against task 01.** Read through the published VRT: the chinko window is the task 01 sprout cell for cell. The gauteng-waterberg window has the same 98 corner-touch cells as the pilot (see the window check above) and is the pilot cell for cell. A cell in open sea reads 0.

**Cost.** At spot prices of about $0.07 an hour for `e2-standard-4` and $0.045 for `e2-highmem-2`: the seed, about 3 task-hours with start-up, is about $0.20; the sprout, about 11.5 task-hours with start-up and the cancelled run, is about $0.50, plus about $0.06 of standard disk. About $0.75 in all, inside the approved $1.0. Reading Overture and the seed cost nothing. Storage is about $0.95 a month for the seed and $0.11 for the sprout at $0.023 per GB. Both are above the estimate (37 GB and 2–3.5 GB): Overture has 6% more road rows than the footer sample implied, and there are 4.0 billion road cells against 1.8–3.1 billion.

## Global tiles (task 03)

```bash
python roads/batch.py submit tiles --pilot 5-18-17 [--approved]   # one shard; prints the job, submits only with --approved
python roads/batch.py submit tiles --approved                      # every shard, one task each, 12 at a time
python roads/batch.py records tiles                                # totals from the shard records written so far
python roads/batch.py submit merge --approved                      # after every shard has a record
python roads/tiles.py local --sources DIR --work DIR               # the same flow on a folder of block COGs, nothing published
```

**Shards.** One shard per zoom 5 tile that meets a sprout block with a COG: 694 shards, each reading 1–4 blocks. Blocks are clipped to Web Mercator's ±85.0511°, so roads beyond it are not drawn. A task copies its blocks, builds a VRT over them, renders zoom 11 and reduces to zoom 5. A top-zoom tile reads the sprout cells under it with a margin of 2 cells, derives the road mask and the burn code in memory, and warps them: average for the share, max for the code. A tile with no road cell under it is skipped before any warp. The shard writes its archive, its zoom 5 share unquantised (`.root.npy`) and a record, last, under `gs://nature-watch-bucket/scratch/roads/roads-2026-10-v1/tiles/shards/`. A record carries a hash of the tile settings, so a rerun skips a current shard and rebuilds a stale one.

**Merge.** One task: it checks every shard record, builds zooms 0–4 from the root shares, streams the shards in tile id order through `tiles/pyramid.py`, rebuilds 16 zoom 8 subtrees from the sprout VRT, weighted by shard tile count, and compares every tile byte for byte. It then uploads with `Cache-Control: public, max-age=31536000, immutable`, checks a 206 Range answer, and deletes the shards. It refuses to overwrite an archive that exists.

**Window check.** The task 01 windows built again through `local`. From the task 01 window sprouts, both archives are the task 01 archives tile for tile (262 and 90 tiles); `window` with the in-memory render also writes the task 01 files byte for byte, in 11.2 s at a peak of 0.4 GB for gauteng-waterberg against 15.5 s and 2.2 GB. From the published block COGs cut to the windows: chinko is identical; gauteng-waterberg differs in 65 zoom 11 pixels (in 30 tiles) and their parents, every one within 2 cells of the 98 corner-touch cells recorded in task 02.

### Runs

**Pilot, shard `5-18-17`** (22.5°E to 33.75°E, 11.18°S to 21.94°S: Zambia, Zimbabwe, Malawi and northern Mozambique; blocks 20S_020E, 20S_030E, 30S_020E, 30S_030E). The task named the shard holding Gauteng, which is `5-18-18`; `5-18-17` was picked by mistake and is as dense. Submitted 18 November 2026 at 09:59 UTC, approved with the task file (expected under $0.05). One spot `e2-standard-2`, 30 GB `pd-standard`.

- job: `roads-2026-10-v1-tiles-pilot-5-18-17-202611180958`, `europe-west1`
- output: `gs://nature-watch-bucket/scratch/roads/roads-2026-10-v1/tiles/shards/5-18-17.pmtiles`, `.root.npy` and `.json`; the full run skips this shard
- status: `gcloud batch jobs describe roads-2026-10-v1-tiles-pilot-5-18-17-202611180958 --location=europe-west1 --project=nature-watch-387210`
- cancel: `gcloud batch jobs delete roads-2026-10-v1-tiles-pilot-5-18-17-202611180958 --location=europe-west1 --project=nature-watch-387210`
- result: succeeded, run duration 476 s. 5,154 tiles (3,793 at zoom 11 of the 4,096 possible, every tile at zooms 5–9), 36,966,178 bytes, 469.5 s in the shard of which 1.9 s copying the four blocks, peak memory 0.46 GB. The shard holds 29,865,020 road cells (counted from the block COGs), so 1.24 bytes and 15.7 µs a road cell, 0.12 s a zoom 11 tile.

**Revised cost, from the pilot.** For approval before the full jobs.

| Job | Work | Machine and time | Compute | Stored | Storage a month |
| --- | --- | --- | --- | --- | --- |
| Shards | 693 more shards, 4.02 billion road cells, 135 times the pilot's. Scaled by road cells, 17.6 task-hours; sparse shards cost more per road cell (more tiles with few roads), so 15–35 | spot `e2-standard-2`, 30 GB `pd-standard`, 12 at a time, about 1.5–3 hours of wall time | about $0.55–1.25, plus about $0.06 of disk | 4–7 GB of shards in scratch until the merge deletes them | |
| Merge | fetch the shards, write the archive, rebuild 16 zoom 8 subtrees from the sprout VRT, upload | one spot `e2-standard-4`, 60 GB `pd-standard`, about 1 hour | about $0.10 | 4–7 GB (1.0–1.8 bytes a road cell; the task 01 windows stored 0.73 dense and 2.5 sparse) | about $0.09–0.16 |

About $0.7–1.4 in all, against $0.30–0.80 in the first estimate: the sprout has 4.0 billion road cells against 1.8–3.1 billion, and a zoom 11 tile takes 0.12 s against the 0.03–0.1 s assumed. Reads of the sprout and the shards inside `europe-west1` are free. Counting the pilot's road cells copied three block COGs (69 MB) to the laptop, under $0.01.

**Shards, all.** Approved by the user on 18 November 2026 with the revised cost above; the Gauteng pilot was skipped. Submitted at 11:12 UTC: 694 tasks, one shard each, 12 at a time, spot `e2-standard-2`, 30 GB `pd-standard`, up to 3 retries a task. Shard `5-18-17` is skipped, done by the pilot.

- job: `roads-2026-10-v1-tiles-202611181112`, `europe-west1`
- output: `gs://nature-watch-bucket/scratch/roads/roads-2026-10-v1/tiles/shards/`
- status: `gcloud batch jobs describe roads-2026-10-v1-tiles-202611181112 --location=europe-west1 --project=nature-watch-387210`
- tasks: `gcloud batch tasks list --job=roads-2026-10-v1-tiles-202611181112 --location=europe-west1 --project=nature-watch-387210`
- cancel: `gcloud batch jobs delete roads-2026-10-v1-tiles-202611181112 --location=europe-west1 --project=nature-watch-387210`
- totals: `python roads/batch.py records tiles`
- result: succeeded, run duration 6,933 s (1 h 56 min), 694 of 694 tasks. 423 shards with tiles: 617,344 tiles, 4,732,271,697 bytes. Shard time summed to 39,020 s (10.8 hours), of which 412 s copying blocks; peak memory 0.56 GB.

**Merge.** Submitted 18 November 2026 at 13:11 UTC: one spot `e2-standard-4`, 60 GB `pd-standard`, one retry.

- job: `roads-2026-10-v1-merge-202611181311`, `europe-west1`
- output: `gs://nature-watch-tiles/roads/2026-10/v1/roads.pmtiles`
- status: `gcloud batch jobs describe roads-2026-10-v1-merge-202611181311 --location=europe-west1 --project=nature-watch-387210`
- cancel: `gcloud batch jobs delete roads-2026-10-v1-merge-202611181311 --location=europe-west1 --project=nature-watch-387210`
- result: succeeded, run duration 578 s, peak memory 3.0 GB. 154.6 s fetching the shards, 74.2 s writing the archive, the rest the sample rebuilds and the upload. The shards were deleted after the upload.

### Published archive

`gs://nature-watch-tiles/roads/2026-10/v1/roads.pmtiles`

| | Result |
| --- | --- |
| Tiles | 617,574 (617,344 in 423 shards, 230 at zooms 0–4); 617,496 distinct bodies |
| By zoom, 5 to 11 | 423; 1,220; 3,670; 11,661; 38,189; 128,571; 433,610 |
| Bytes | 4,734,899,752 (4.41 GiB), 1.18 a road cell |
| Bounds | −180 to 180, ±85.0511°; zooms 0–11 |
| Metadata | version `roads-2026-10-v1`, release `2026-10-21.0`, attribution © OpenStreetMap contributors, Overture Maps Foundation, licence ODbL-1.0, the shades and the level and surface values |
| Sprout check | 16 zoom 8 subtrees rebuilt from the published VRT, 729 tiles, byte for byte the same (roots in the merge log) |
| Cache-Control | `public, max-age=31536000, immutable` |
| Range request | 206, `Content-Range: bytes 0-126/4734899752` |
| CORS | GET with Range from `http://localhost:5173` returns `Access-Control-Allow-Origin`; a preflight from `https://naturewatch.space` answers 200 |
| Looked at | `render.py tiles <public URL>` over Esri imagery: the world at zoom 1, Africa at zoom 4, gauteng-waterberg and chinko at zoom 8, the shard corner at 22.5°E 21.94°S at zoom 8 (no seam; the straight north–south line at 22.7975°E is a mapped track in the sprout), Nairobi at zoom 11 by level |
| Virtual machines left running | none |

**Cost.** The shard tasks ran 16.6 task-hours with start-up (10.8 hours in the shards), 8 of them a second time after a spot preemption, on at most 12 spot `e2-standard-2` for 1 h 56 min, so 17–23 machine-hours at about $0.035: about $0.60–0.80, plus about $0.03 of standard disk. The pilot and the merge add about $0.02. About $0.65–0.85 in all, inside the approved $0.7–1.4. Storage is about $0.11 a month at $0.023 per GB. Reading the sprout inside the region cost nothing.
