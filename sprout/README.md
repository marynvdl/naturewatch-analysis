# Sprout

How the pipelines fit together is [`docs/pipelines.md`](../docs/pipelines.md). This page is the cropland 2024 run.

Reads the South Africa clip named in the cookbook recipe, remaps it to the published classes, and writes one Cloud Optimized GeoTIFF plus a static STAC Collection and Item. The recipe is the catalog. This script reads it and does not edit it. The STAC files are generated from the recipe. They are not a second catalog, and nothing in this release serves them through an API.

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
