# Seed

How the pipelines fit together is [`docs/pipelines.md`](../docs/pipelines.md). This page is the cropland 2024 run.

Downloads the GLAD annual cropland tiles named in a cookbook recipe, checks them, and writes a mainland South Africa clip under the recipe's seed path. That is the version 1 command. The global recipe uses `transfer` and `check`. Those commands do not download the tiles to the laptop and do not write a clip.

The recipe is the catalog. This script reads it and does not edit it. Open one tile with `sample` and record its source codes in the recipe before `run` downloads the rest.

## Checks

Each tile must match the recipe:

- geographic CRS, WGS84 (`EPSG:4326`)
- pixel size `0.00025°`
- the cropland value `100` appears in the set of tiles
- no pixel value other than the codes recorded in the recipe
- the source file's nodata flag matches the recipe (unset for this year)

The clip keeps those source codes inside the boundary. Pixels outside mainland South Africa are the clip nodata value, `255`. Parts of the boundary that miss the tiles, including the Prince Edward Islands, are left out. One pixel at 28°E, 26.2°S must match the source tile, so the clip stays on the same grid.

## Run

```bash
pip install -r cookbook/requirements.txt
python seed/cropland.py sample
python seed/cropland.py run
python -m pytest seed cookbook
```

`run` fetches every tile in the recipe, writes the clip, and uploads both to `seed.path`. GDAL's Python bindings come with the GDAL install. No virtual machine is started, and there is no GitHub workflow for this step.

Tiles are saved under `data/seed/cropland-2024/`, which git ignores. A download resumes in 2 MB ranges because the GLAD server closes long connections.

## cropland 2024

`20S_020E.tif` was opened on 22 October 2026 before the other tiles. Every pixel is `0` or `100`. The file sets no nodata value. `0` is everything that is not cropland, and `100` is cropland. The recipe records that. The clip is `gs://nature-watch-bucket/seeds/cropland/glad/2024/v1/cropland-glad-2024-zaf.tif`.

The boundary is geoBoundaries ZAF ADM0 for 2020 (OCHA ROSEA and the South African Municipal Demarcation Board), pinned to geoBoundaries commit `9469f09`.

## Cost for this step

Run on 22 October 2026 from a laptop. No virtual machine was started.

The six tiles and the clip are in `gs://nature-watch-bucket/seeds/cropland/glad/2024/v1/`. New objects are 150,779,903 bytes. The prefix total is 150,779,952 bytes, which includes the 49-byte `.keep` marker from the cookbook step. The clip is 38,478,988 bytes. The sprout prefix was not written.

Downloading the tiles took 568 seconds, including two attempts that the GLAD server reset before the ranged download. `python seed/cropland.py run` then took 847 seconds to check the six files, clip them, and upload. Peak memory of that process was 1,253,572,608 bytes.

All six tiles are `EPSG:4326`, `0.00025°`, uint8, with no nodata flag. Every pixel is `0` or `100`, and `100` appears in each tile. The clip origin is `(16.45175, -22.125)`. Inside the boundary it has 1,680,109,285 pixels of `0` and 130,854,661 pixels of `100`. The boundary kept for the clip has 11 parts that meet the tiles, including the small islands along the Cape coast, and one interior ring. The Prince Edward Islands fall outside the tiles and are not in the clip.

## cropland 2024 global

`transfer` reads `cookbook/recipes/cropland-2024-v2.yaml`, writes a URL list, and submits one Storage Transfer Service job into `gs://nature-watch-bucket/seeds/cropland/glad/2024/v2/`. The job is not on a schedule. `check` submits the GDAL check after every listed tile is in that prefix. The check uses the version 1 grid and class rules. A listed tile that fails stops the run. There is no country clip and no clip file.

Storage Transfer names each object as the source host plus the URL path. `20S_020E.tif` lands at `gs://nature-watch-bucket/seeds/cropland/glad/2024/v2/glad.umd.edu/datasets/annual-croplands/2024/20S_020E.tif`. The version 1 prefix is not a destination. The transfer uses `--overwrite-when=never` and refuses to start when one of those objects is already there.

The check is one spot `e2-standard-2` in `europe-west1` with a 30 GB disk and a 12 hour limit. It reads one tile at a time from the bucket, checks it, and deletes the local copy. Batch deletes the virtual machine when the task ends. The URL list stays at `gs://nature-watch-bucket/scratch/seed/cropland-2024-v2/urls.tsv`.

`glad.umd.edu/robots.txt` does not disallow `/datasets/`. The version 1 download already used Range requests on this server.

The transfer command grants `roles/storage.legacyBucketWriter` on `nature-watch-bucket` to `project-637146989512@storage-transfer-service.iam.gserviceaccount.com`, so the service can read the URL list and write the version 2 prefix.

Neither command has been run.

```bash
python seed/cropland.py transfer --recipe cookbook/recipes/cropland-2024-v2.yaml
python seed/cropland.py check --recipe cookbook/recipes/cropland-2024-v2.yaml
```

Do not run those until the expected cost below is approved. `check` refuses to submit while any listed tile is missing. Each command prints how to read status and how to cancel, then returns.

| | Command |
| --- | --- |
| Transfer status | `gcloud transfer jobs describe cropland-2024-v2-seed --project=nature-watch-387210` |
| Transfer operations | `gcloud transfer operations list --job-names=cropland-2024-v2-seed --project=nature-watch-387210` |
| Cancel the running transfer | `gcloud transfer operations cancel OPERATION --project=nature-watch-387210` |
| Delete the transfer job | `gcloud transfer jobs delete cropland-2024-v2-seed --project=nature-watch-387210` |
| Check status | `gcloud batch jobs describe seed-cropland-glad-2024-v2 --location=europe-west1 --project=nature-watch-387210` |
| Cancel the check | `gcloud batch jobs delete seed-cropland-glad-2024-v2 --location=europe-west1 --project=nature-watch-387210` |

## Expected cost, waiting for approval

The expected seed storage is 4,173,850,674 bytes, from [`cookbook/README.md`](../cookbook/README.md). That scales the six version 1 tiles (112,300,915 bytes) to 223 tiles. It is not a measured sum of the 223 files. The sprout and the PMTiles archive are later steps.

Storage Transfer Service does not charge for an agentless URL-list transfer. The bucket will store those bytes in `nature-watch-bucket` (EU). The transfer also writes about 223 objects, one Class A write per tile, and reads the URL list, one Class B read per tile. A larger tile can take more than one write. The URL list itself is a few kilobytes in the scratch prefix.

The check has not started, so its wall time and peak memory are not measured. No virtual machine was started for this step, and none was left running. The version 1 seed was not changed.
