# Sprout

How the pipelines fit together is [`docs/pipelines.md`](../docs/pipelines.md). This page is the cropland 2024 run.

Reads the South Africa clip named in the cookbook recipe, remaps it to the published classes, and writes one Cloud Optimized GeoTIFF plus a static STAC Collection and Item. The global recipe has no clip. That command mosaics the checked tiles instead. The recipe is the catalog. This script reads it and does not edit it. The STAC files are generated from the recipe. They are not a second catalog, and nothing in this release serves them through an API.

The sprout id is `cropland-2024-v1`. The published run is a Google Batch job in `europe-west1`, started by hand. There is no orchestrator in this release, and no GitHub workflow for this step. A later decision can add an orchestrator that submits jobs like this one. Batch deletes the virtual machine when the task ends.

## What the file contains

The clip's pixels are remapped with `sprout.from_seed`:

- `0` becomes `0` (not cropland)
- `100` becomes `1` (cropland)
- `255` stays `255` (nodata, including land outside South Africa)

Any other value stops the run. The grid stays `EPSG:4326` at `0.00025°`. The version from the sprout path, `v1`, is stored in the file. The class values are stored as `class_values` metadata (`cropland:1,not_cropland:0,nodata:255`) and in the STAC item. Nodata is 255.

Overviews use maximum. GDAL 3.11 cannot build a COG overview with that method, so the job warps each level with `max` and copies those levels into the COG. Nodata is ignored, so one cropland pixel survives in a coarse cell, and a cell of only nodata stays nodata. Area counts use the full-resolution pixels.

## Run

```bash
pip install -r cookbook/requirements.txt
python -m pytest sprout cookbook seed
python sprout/cropland.py submit
```

`submit` enables the Batch API if needed, copies this script and the recipe to `gs://nature-watch-bucket/scratch/sprout/cropland-2024/`, and submits a job named with the sprout id. The machine is a spot `e2-standard-2` with a 30 GB disk. It uses the project's default compute service account, which is already an Editor, so no new account is created. The image is `ghcr.io/osgeo/gdal:ubuntu-small-3.11.4`.

The task reads the seed clip from the bucket, writes the COG and the two JSON files to `sprout.path`, then deletes the scratch copies. A laptop `run` refuses to do that work.

The objects are:

- `gs://nature-watch-bucket/sprouts/cropland/2024/v1/cropland-2024-zaf.tif`
- `gs://nature-watch-bucket/sprouts/cropland/2024/v1/collection.json`
- `gs://nature-watch-bucket/sprouts/cropland/2024/v1/cropland-2024-zaf.json`

The `.keep` marker in that prefix stays.

## Cost for this step

Run on 22 October 2026 as Batch job `sprout-cropland-2024-v1` in `europe-west1`. That job name is from the id this sprout had on that day. Spot `e2-standard-2`. The virtual machine was removed when the task ended. No virtual machine was left running.

The Python process took 317 seconds and peaked at 1,335,562,240 bytes. The Batch task ran for 321 seconds, including the image start. The COG is 39,337,060 bytes. With the two STAC files, the new objects are 39,340,059 bytes. The prefix total is 39,340,108 bytes, which includes the 49-byte `.keep` marker. The scratch copies were deleted.

Two earlier submits failed in a few seconds while the container was being mounted, before any raster was written. Compute for the successful task is under $0.01 at the spot price of about $0.04 an hour. Reading and writing about 37 MB in Europe is a fraction of a cent. Keeping the prefix is under $0.01 a month.

## cropland 2024 global

The first submit, uid `cropland-2024-v2-f7c0999b-b228-424f-80`, was queued at `2026-10-30T13:05:15Z` and failed at `2026-10-30T13:29:10Z`. It had read all 223 tiles. GDAL then stopped because the dataset pool was 2, which is below the worker thread count. No COG was written, and the virtual machine was removed.

Submitted again at `2026-10-30T15:06:21Z` as the same job name, uid `cropland-2024-v2-24c63baa-aeb6-4f88-b0`. The dataset pool is 239, one handle per tile plus the worker threads. Decoded pixels stay in the 512 MB cache. Bytes stored, wall time, and peak memory wait until it finishes.

`submit --recipe cookbook/recipes/cropland-2024-v2.yaml` reads `cookbook/recipes/cropland-2024-v2.yaml` and mosaics the 223 checked tiles under `gs://nature-watch-bucket/seeds/cropland/glad/2024/v2/`. A tile on the Storage Transfer host path is used when both copies exist. The flat file name is the 2024 copy. There is no country clip. A 10° tile the publisher does not offer stays nodata. `0` becomes `0` and `100` becomes `1`. Any other value stops the run before the COG is written. `gs://nature-watch-bucket/sprouts/cropland/2024/v1/cropland-2024-zaf.tif` is not a destination.

The mosaic places each tile from its own geotransform. The GLAD name is the northwest corner of a 10° tile, so the 223 names describe a window from 170°W to 180°E and from 70°N to 60°S. At `0.00025°` that window is 1,400,000 by 520,000 pixels. The 223 tiles cover 356,800,000,000 of those pixels. The rest of the window is nodata. Overviews stay maximum, with nodata ignored, the same as version 1.

The objects, once the job finishes, are:

- `gs://nature-watch-bucket/sprouts/cropland/2024/v2/cropland-2024.tif`
- `gs://nature-watch-bucket/sprouts/cropland/2024/v2/collection.json`
- `gs://nature-watch-bucket/sprouts/cropland/2024/v2/cropland-2024.json`

```bash
python sprout/cropland.py submit --recipe cookbook/recipes/cropland-2024-v2.yaml
```

The job id is `cropland-2024-v2`. It is a spot `e2-standard-4` in `europe-west1` with a 100 GB disk, a 12 GB memory limit, and a 12 hour limit. Batch deletes the virtual machine when the task ends. The task deletes `gs://nature-watch-bucket/scratch/sprout/cropland-2024-v2/`.

```bash
gcloud batch jobs describe cropland-2024-v2 --location=europe-west1 --project=nature-watch-387210
gcloud batch jobs delete cropland-2024-v2 --location=europe-west1 --project=nature-watch-387210
```

## Expected cost

Approved on 30 October 2026. The first run failed before upload. The replacement job is `cropland-2024-v2`, uid `cropland-2024-v2-24c63baa-aeb6-4f88-b0`.

The version 1 disk was 30 GB for a 39 MB COG. This job is sized from the global seed instead.

The seed is 5,289,882,209 bytes for 356,800,000,000 source pixels, about 15 bytes on disk for every 1,000 pixels. Remapping `0` and `100` to `0` and `1` keeps that pattern. Nodata blocks in the gaps compress to almost nothing. The COG, including maximum overviews, is expected to be about 6–12 GB. The working set is the remapped file, the overview files, and the COG together, about 30 GB. The 100 GB disk is there so a several-times-worse compression ratio still fits. If it does not, the task fails before upload.

One tile is 1.6 GB uncompressed. The process keeps two sources open and a 512 MB cache, on a machine with 16 GB of RAM. The request is 12 GB for the task.

Spot `e2-standard-4` is about $0.08 an hour. A 100 GB balanced disk is about $0.10 per GB-month, so a few hours of disk is a few cents. Reading the seed in the same EU bucket has no egress. Keeping a 12 GB COG is about $0.02 per GB-month, under $0.30 a month.

This estimate was too low on time. The run is expected to take 8–9 hours, not a few. One clean run is about $0.85. A spot interruption restarts the task from the tiles, because nothing is uploaded or saved before the end, so a retry would roughly double that to about $1.70.

## Direction for later years

Decided on 30 October 2026, while job `cropland-2024-v2` was running. This is not built. The version 2 global COG is written the way this page describes, because the job was already running and is past the steps that failed. The next sprout run changes as follows.

1. **One Sprout COG per geographic tile, not one global mosaic.** Each of the 223 seed tiles becomes its own COG at a path named from the recipe. The tiles already sit on whole-pixel offsets, which `mosaic_canvas` checks today.
2. **Sprout keeps the full-resolution classification only.** It remaps `0` and `100` to `0` and `1` and keeps nodata `255`. It does not decide how a map should be drawn, so it carries no maximum or `q3` overviews. By default a Sprout tile has no overviews. A different overview method needs its own decision.
3. **Resampling moves to the Tiles pipeline.** Tiles builds its own pyramid from the full-resolution pixels with the method chosen in task 04, `max` or `q3`. The recipe's `tiles.resampling` field is the only place that choice lives. Overviews written by a Sprout run must not set it by accident. A Warp that reads a Sprout overview takes a quartile of values that were already maximums.
4. **Facts and Tiles read a collection of Sprout COGs**, through a VRT or an explicit list. Facts only opens the tiles a place touches. Still to check: whether a VRT over `/vsigs/` paths in a private bucket works for both readers, and how STAC lists 223 assets.
5. **Resumable per tile.** One Batch task per tile, with capped parallelism. A task skips a tile whose output exists and is complete. Each tile writes to a final object name only when it is finished, since a GCS object is not visible until the upload completes. A manifest records bytes and a checksum per tile, so a rerun redoes only the missing or failed ones. The cost of a spot interruption becomes one tile, not the whole run.
6. **Benchmark compression before choosing it.** Compare DEFLATE with `PREDICTOR=2` and level 9, ZSTD, and LZW on a few representative tiles: one dense cropland tile, one mostly ocean tile, and one mixed. Record bytes stored, write time, and the time to read a place-sized window. The version 2 global run used DEFLATE at level 9 and is the reference. A format a reader cannot open, for example ZSTD in an older reader, counts against it.

The finest zoom is not the source pixel. At zoom 12 one 256-pixel tile pixel is about 38 m at the equator, and GLAD is 0.00025°, about 28 m. Keeping original pixels at the finest zoom needs a higher `max_zoom` in the recipe, such as 13. That is about four times the tiles, so it is an open decision for task 04.

Consequences for the version 2 tasks. Task 04's `max` against `q3` sample must be built from full-resolution pixels, not from the global COG's maximum overviews, or both samples will start from maximums. Facts is not affected, because it counts full-resolution pixels. Task 06 can read the global COG this year.
