# Task 05 — Republish places PMTiles

Parcel: [Places Tiles](https://github.com/marynvdl/naturewatch-app/blob/develop/docs/parcels/places-tiles.md) in naturewatch-app.

Repository: naturewatch-analysis. Start only after Places Tiles task 04 has finished the hosted import.

## Goal

Rebuild that archive from the global places and replace the published object. Record the run cost.

## Facts already stored

Task 02 published `gs://nature-watch-tiles/places/protected-area/v1/places.pmtiles` from the South Africa rows. Task 04 in naturewatch-app then stored terrestrial protected areas worldwide of at least 10 km². The app already draws that object path. Raster tiles stay as they are.

## Do this

- Rerun the task 02 publish against the global places. Follow that step README. Do not invent a second pipeline.
- Replace the object at the same path so the app does not need a new URL.
- Keep the copied fields: `places.id`, `area_km2`, and cropland percent when a fact exists. Most global places have no cropland fact.
- Keep the same zoom generalization: largest places at a world view, smaller places as you zoom in.
- Record the new run in the step README: bytes stored, wall time, peak memory, zoom range, place count, and how to cancel the job. Do not copy that procedure into naturewatch-app.
- Before the hosted publish, record the expected cost and wait for approval. After an authorized run, record the task id, output, status check, and how to cancel it, then return. Do not wait in the chat for the job.

## Do not

- Do not change the object path unless the step cannot replace the file in place. If the path must change, stop and say so; the app read path lives in naturewatch-app.
- Do not calculate new cropland facts.
- Do not convert raster tiles to PMTiles.
- Do not start a virtual machine or a Batch job until it is approved.

## Done when

- The published archive is built from the global places, or this pull request stops before the hosted publish and records how to run it.
- The step README has the new run cost, or the expected cost waiting for approval.
- The work is on a branch from `develop`, in one pull request into `develop`. Report the pull request address. Do not merge it.

## How to work

Branch from `develop`. Do this task only. Explain billable processing and get approval before starting it. Do not commit secrets. Do not add an AI or tool attribution to the commit or the pull request.
