# Task 04 — Cropland tile sample

Parcel: [Cropland 2024 global](https://github.com/marynvdl/naturewatch-app/blob/develop/docs/parcels/cropland-2024-global.md) in naturewatch-app.

Repository: naturewatch-analysis. Start only after task 03 has written the sprout.

## Goal

Build the tiles step as one PMTiles archive. Prove resampling on a small window and stop for approval. Do not build the global archive.

## Facts already stored

Task 03 writes `gs://nature-watch-bucket/sprouts/cropland/2024/v2/cropland-2024.tif`. The global recipe’s tile path is `gs://nature-watch-tiles/cropland/2024/v2/cropland.pmtiles`. Resampling on that recipe is unset. The South Africa tiles used `max`. `q3` is still open. Those version 1 tiles are individual XYZ PNG files, gray class values plus alpha, zooms 0–12. Colour is applied in the client.

`tiles/cropland.py` reads the South Africa sprout and writes `{z}/{x}/{y}.png` with a hand-started Batch job. `build` can write a local directory and does not upload. The version 1 run wrote 23,467 tiles and 28,294,128 bytes for South Africa alone.

The places pipeline writes one local PMTiles file and a `preview.html` that is served with HTTP Range requests. A plain static server often returns the whole file and PMTiles then fails.

## Do this

- Change the global tiles command so it writes one raster PMTiles archive of class values, not a folder of PNG files. Keep the version 1 XYZ command working for the South Africa recipe.
- Build two local samples from a small window of the version 2 sprout, one with `max` and one with `q3`. Zooms 0–12 for that window only. Nodata stays empty. One cropland pixel must survive in a coarse cell when the method is `max`.
- Serve the samples the way the places preview does, so Range requests work. Show both and stop for approval.
- When one resampling is approved, write it on the global recipe. Until then the recipe field stays unset.
- Record the sample in `tiles/README.md`: window, bytes, wall time, peak memory, and the approval. Update the status table in `docs/pipelines.md`. A local sample has no cloud cost. Say so.

## Do not

- Do not build or upload the global archive. Task 05 does that.
- Do not upload the sample to `nature-watch-tiles`.
- Do not delete the version 1 PNG prefix.
- Do not add a GitHub workflow that starts on push.
- Do not start task 05 until the resampling is approved and written on the recipe.

## Done when

- A local preview shows the same window under `max` and under `q3`.
- The approved resampling is on the global recipe, or this pull request is waiting on that approval and says so.
- The global PMTiles object does not exist yet.
- `python -m pytest tiles cookbook` passes.
- The work is on a branch from `main`, in one pull request into `main`. Report the pull request address. Do not merge it.

## How to work

Branch from `main`. Do this task only. Explain billable processing and get approval before starting it. This sample stays on the machine that builds it. Do not commit secrets. Do not add an AI or tool attribution to the commit or the pull request.
