# Tiles

How the pipelines fit together is [`docs/pipelines.md`](../docs/pipelines.md). This page is the cropland 2024 run.

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
