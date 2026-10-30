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

Run on 30 October 2026. The transfer is finished. The check is submitted and has not finished.

`transfer` read `cookbook/recipes/cropland-2024-v2.yaml`, wrote a URL list, and submitted one Storage Transfer Service job into `gs://nature-watch-bucket/seeds/cropland/glad/2024/v2/`. The job is not on a schedule. Storage Transfer writes each object as the source host plus the URL path. `check` then reads those objects. A listed tile that fails stops the run and is left on the host path. A tile that passes is moved to the file name alone, and the host path is deleted. There is no country clip and no clip file.

The kept name, after the check passes, is `gs://nature-watch-bucket/seeds/cropland/glad/2024/v2/20S_020E.tif`. Until that move, the transferred object is `gs://nature-watch-bucket/seeds/cropland/glad/2024/v2/glad.umd.edu/datasets/annual-croplands/2024/20S_020E.tif`. The version 1 prefix is not a destination. The transfer uses overwrite never, and the move refuses to replace a flat file that already exists.

The check is one spot `e2-standard-2` in `europe-west1` with a 30 GB disk and a 12 hour limit. It reads one tile at a time from the bucket, checks it, and deletes the local copy. The move is a same-bucket rewrite. Batch deletes the virtual machine when the task ends. The URL list stays at `gs://nature-watch-bucket/scratch/seed/cropland-2024-v2/urls.tsv`.

The transfer command grants `roles/storage.legacyBucketWriter` and `roles/storage.objectViewer` on `nature-watch-bucket` to `project-637146989512@storage-transfer-service.iam.gserviceaccount.com`. The writer role does not include object reads. The private URL list and the never-overwrite check need `storage.objects.get`.

```bash
python seed/cropland.py transfer --recipe cookbook/recipes/cropland-2024-v2.yaml
python seed/cropland.py check --recipe cookbook/recipes/cropland-2024-v2.yaml
```

| | |
| --- | --- |
| Transfer job | `transferJobs/cropland-2024-v2-seed` |
| Transfer operation | `transferOperations/transferJobs-cropland-2024-v2-seed-13675014377511653293` |
| Transfer status | `gcloud transfer operations describe transferOperations/transferJobs-cropland-2024-v2-seed-13675014377511653293 --project=nature-watch-387210` |
| Cancel a running transfer | `gcloud transfer operations cancel OPERATION --project=nature-watch-387210` |
| Check job | `seed-cropland-glad-2024-v2` |
| Check status | `gcloud batch jobs describe seed-cropland-glad-2024-v2 --location=europe-west1 --project=nature-watch-387210` |
| Cancel the check | `gcloud batch jobs delete seed-cropland-glad-2024-v2 --location=europe-west1 --project=nature-watch-387210` |

## Cost for the global seed

The estimate before the run was 4,173,850,674 bytes, from [`cookbook/README.md`](../cookbook/README.md). The transfer stored 5,289,882,209 bytes, 223 objects, all of the bytes it found. Storage Transfer Service does not charge for an agentless URL-list transfer. The bucket holds those bytes in `nature-watch-bucket` (EU). The sprout and the PMTiles archive are later steps.

The successful operation started at `2026-10-30T09:58:08Z` and ended at `2026-10-30T10:02:32Z`, 264 seconds. An earlier operation, `transferOperations/transferJobs-cropland-2024-v2-seed-15313645261421247161`, failed with `PERMISSION_DENIED` and copied nothing. The transfer service does not report peak memory. No virtual machine was started for the transfer, and none was left running. The version 1 seed was not changed.

The check `seed-cropland-glad-2024-v2` was submitted at `2026-10-30T11:15:57Z`. Its wall time and peak memory are not known until it finishes. It rewrites each tile that passes onto the flat file name and deletes the host path, so one copy remains. The check uses the version 1 grid and class rules.
