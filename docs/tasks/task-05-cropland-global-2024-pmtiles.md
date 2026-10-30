# Task 05 — Cropland global PMTiles

Parcel: [Cropland 2024 global](https://github.com/marynvdl/naturewatch-app/blob/develop/docs/parcels/cropland-2024-global.md) in naturewatch-app.

Repository: naturewatch-analysis. Start only after task 04 has recorded an approved resampling on the global recipe.

## Goal

After the sample is approved, write zooms 0–12 to the version 2 PMTiles object. Record the run cost.

## Facts already stored

Task 04 sets the resampling on `cookbook/recipes/cropland-2024-global.yaml` and proves the PMTiles command on a small window. The archive path is `gs://nature-watch-tiles/cropland/2024/v2/cropland.pmtiles`. The sprout is `gs://nature-watch-bucket/sprouts/cropland/2024/v2/cropland-2024.tif`.

Version 1 tiles stay at `gs://nature-watch-tiles/cropland/2024/v1/`. The app still reads that PNG template until task 07 in naturewatch-app. This task publishes the object. It does not change the app.

The version 1 tile job was Batch `tiles-cropland-2024-v1` in `europe-west1`, spot `e2-standard-2`, 937 seconds, 23,467 PNG objects. A global XYZ upload would be a much larger object-write cost. This run writes one archive.

## Do this

- Refuse to start when the recipe’s resampling is unset.
- Build zooms 0–12 from the version 2 sprout with the approved resampling. Gray 1 is cropland, gray 0 is other land, nodata is empty. Colour stays in the client.
- Write one object at the recipe tile path. The object must answer HTTP Range requests. The places archive on this bucket already does.
- Estimate bytes and Batch time from the sample and from the version 1 tile README, then wait for approval. Size the disk for the global sprout. The version 1 tile job’s 30 GB disk is not assumed to be enough.
- Use a spot machine in `europe-west1`. Delete scratch. Batch must delete the virtual machine when the task ends.
- After an authorized run, record the job id, the output, how to check status, and how to cancel, then return.
- Record the finished run in `tiles/README.md`: archive bytes, zoom range, wall time, peak memory, resampling, and that no virtual machine was left running. Update the status table in `docs/pipelines.md`.

## Do not

- Do not write `{z}/{x}/{y}.png` objects for the global layer.
- Do not delete the version 1 PNG prefix.
- Do not change the app catalog. Task 07 in naturewatch-app does that.
- Do not calculate facts. Task 06 does that.
- Do not add a GitHub workflow that starts on push.
- Do not submit the global job until the expected cost is approved.

## Done when

- `gs://nature-watch-tiles/cropland/2024/v2/cropland.pmtiles` is the global zooms 0–12 archive, or this pull request stops before that job and records how to run it.
- The archive uses the resampling approved in task 04.
- `python -m pytest tiles cookbook` passes.
- `tiles/README.md` has the run cost, or the expected cost waiting for approval.
- The work is on a branch from `main`, in one pull request into `main`. Report the pull request address. Do not merge it.

## How to work

Branch from `main`. Do this task only. Explain billable processing and get approval before starting it. Do not commit secrets. Do not add an AI or tool attribution to the commit or the pull request.
