# Places

How the pipelines fit together is [`docs/pipelines.md`](../docs/pipelines.md). This page is the three places archives: protected areas (`cookbook/recipes/places-protected-areas.yaml`), recognitions (`cookbook/recipes/places-recognitions.yaml`), and ecoregions (`cookbook/recipes/places-ecoregions.yaml`). One script builds all three. `--recipe` picks the archive, and the protected areas are the default.

Reads the current boundary of each published place of the recipe's types (`conservation_area`, the six recognition types, or `ecoregion`) from `place_boundaries`, and writes one PMTiles object with Tippecanoe. Merged and draft places are left out. The recipe is the catalog. This script reads it and does not edit it. There is no GitHub workflow for this step. Colour is applied in the client.

The protected-area archive is `gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles`. It was built on 31 October 2026 from `places.geom` of every place. Since Database Honing task 07 the script reads the current boundaries of published places instead. App task 09 rebuilds it from the published conservation areas only, after the recognitions moved out, and removes the stale rollback copy `v1/places.previous.pmtiles` (the South Africa build of 29 October) first, so the rollback copy becomes the 31 October global build. Until then the 64 parcels merged by app task 06 still draw as their own shapes, and the recognitions are still in it. Raster tiles stay as they are.

The recognition archive is `gs://nature-watch-tiles/places/recognitions/v1/recognitions.pmtiles`: World Heritage Sites, Ramsar sites, UNESCO-MAB biosphere reserves, Natura 2000 and Emerald sites, and other regional recognitions (app decisions.md, record 21). App task 09 builds it first. The app reads it from task 09b.

The ecoregion archive is `gs://nature-watch-tiles/places/ecoregions/v1/ecoregions.pmtiles`, layer `ecoregions`: the 846 published terrestrial ecoregions of RESOLVE Ecoregions 2017 (app Ecoregions task 01). App Ecoregions task 04 reads it.

## What a feature contains

The recipe names the vector layer and the properties. `id` and `area_km2` come first, then any of `cropland_percent` and `place_type_id`. The protected-area layer is `places` with `cropland_percent`. The recognition layer is `recognitions` with `place_type_id`, and has no `stats:` block. The ecoregion layer is `ecoregions` with `id` and `area_km2` only. Each feature carries:

- `id` — `places.id`
- `area_km2` — the current boundary's area
- `cropland_percent` — the `crops_percent` value for 2024 in `place_stats_preferred`, omitted when there is none. Its sprout and method are the app's `stat_selections` row, not the recipe.
- `place_type_id` — the place's type, for example `world_heritage_site`. A place without one stops the build.

`place_boundaries.geom` is read with the same database URL as the stats step. It is not granted to `anon`. The archive does not include names or a polygon download.

Larger places are written from a lower zoom. The bands are on the recipe: 1,000 km² from zoom 0, 300 km² from zoom 2, 100 km² from zoom 3, 30 km² from zoom 4, 10 km² from zoom 6. Every copied place is present by zoom 6. Geometry is simplified at low zoom and left at full resolution at zoom 12. Colour and line width are not in the archive. The app paints the layer when it draws the map.

## Tile engine

Tippecanoe 2.79.0 from [felt/tippecanoe](https://github.com/felt/tippecanoe) (`tippecanoe --version` prints `v2.79.0`). The Debian package is older and cannot write PMTiles. `places/Dockerfile` builds that tag on `ghcr.io/osgeo/gdal:ubuntu-small-3.11.4` for the Batch job. The image is `europe-west1-docker.pkg.dev/nature-watch-387210/places/places-tippecanoe:2.79.0`. It is not built or pushed yet; that waits for approval. Tile encoding is Mapbox Vector Tile inside PMTiles. GDAL and OGR read, validate, and prove.

Flags, from `TIPPECANOE_FLAGS` and the protected-area recipe. The recognition recipe has `-l recognitions`, and `place_type_id` (a string) in place of `cropland_percent`:

```
-l places -Z 0 -z 12 --simplification=1 -y id -y area_km2 -y cropland_percent
-T id:string -T area_km2:float -T cropland_percent:float
--no-feature-limit --no-tile-size-limit --no-tiny-polygon-reduction --read-parallel --force
```

A recipe with `tiles.detect_shared_borders: true` adds `--detect-shared-borders`, so neighbours that share an edge simplify it the same way and a fill shows no slivers. Only the ecoregion recipe sets it. Its bands write ecoregions of 30 km² and more from zoom 0, because ecoregions cover the land without gaps, and the 11 smaller island ecoregions from zoom 1.

Every place is its own feature carrying `tippecanoe.minzoom` from the recipe bands. No dropping by density, no coalescing, no size cap. `cropland_percent` is left off a feature that has no fact.

## Run

```bash
pip install -r cookbook/requirements.txt -r places/requirements.txt
python -m pytest places cookbook
```

`pytest` uses a few synthetic polygons and needs `tippecanoe` on the path (those two tests skip without it). It does not read the database or upload.

Places stream from a server-side cursor to `places.ndjson` on disk, then Tippecanoe reads that file. Geometry is never held in memory. Only an id to area map is kept for the proof.

Stored geometry check, 31 October 2026: `python places/protected_areas.py check-valid` returned 51,001 places, 0 invalid, 0 not polygonal. So the query has no `ST_MakeValid` or `ST_CollectionExtract`, and `--validate none` is the default. `--validate ogr` runs OGR `MakeValid` on the build machine if that ever changes.

Cropped laptop proof. Export `DATABASE_URL` (do not commit it) and pass a window:

```bash
python places/protected_areas.py prove --bbox=2.5,47.2,15.1,55.1   # Germany and Benelux
python places/protected_areas.py prove --bbox=29.3,-11.8,40.5,-0.9 # Tanzania
```

Pass `--recipe cookbook/recipes/places-recognitions.yaml` for the recognitions. The local archive goes to `data/places/<recipe name>/`, and a Batch job's scratch files to `gs://nature-watch-bucket/scratch/places/<recipe name>/`. On 12 November 2026 both recipes passed `prove --bbox=29.5,-20.5,31.5,-19.5` against the local Supabase stack loaded with the app's `records_fixture.sql`: one park in the protected-area archive, and the World Heritage and Ramsar fixture sites in the recognition archive, each with its `place_type_id`.

Without `--bbox`, `prove` reads every place. Do not do that on the laptop. The proof checks every zoom 0 to 12: a place is absent below its minzoom and present from it on, every id is present by zoom 6, copied fields match, and the largest tile in bytes per zoom is recorded. `python places/protected_areas.py preview` serves the result with Range support at `http://127.0.0.1:8765/preview.html`. Cancel with Ctrl+C.

## Replace the published object

`submit` and `run` refuse when the object exists, as before. `--replace` changes that:

1. Copy `v1/places.pmtiles` to `v1/places.previous.pmtiles`. If that copy already exists the run stops and never overwrites it.
2. Upload the new archive with `Cache-Control: no-cache`.
3. Request bytes 0-15 over HTTPS and fail unless the answer is 206.

Roll back with `gcloud storage cp gs://nature-watch-tiles/places/protected-areas/v1/places.previous.pmtiles gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles --cache-control=no-cache --project=nature-watch-387210`. Remove the previous copy only when told to.

```bash
python places/protected_areas.py submit --replace   # starts a Batch job: needs approval
```

Batch deletes the virtual machine when the task ends. Cancel with `gcloud batch jobs delete <job-id> --location=europe-west1 --project=nature-watch-387210`. `submit` prints the job id, the status command, and the cancel command.

## Rebuild after admin changes

Admins change places in the app: they hide a place, show it again, or approve a replacement boundary (app Shaping Boundaries task 05). The archives change only when rebuilt, so the map lags. Until then the app leaves hidden places out of what it draws. It cannot draw a new boundary, or a place shown again after a rebuild left it out.

Rebuilds are started by hand, per batch of changes, after approval. There is no schedule. Rebuild only the archives whose places changed: `conservation_area` places are in protected areas, the six recognition types in recognitions, and ecoregions in ecoregions.

1. Delete the old rollback copy `v1/places.previous.pmtiles` of each archive to rebuild, if it exists, so that `--replace` keeps the archive now published as the new rollback copy. Deleting it needs approval too.
2. Run `submit --replace` for each, with `--recipe` for recognitions or ecoregions. It starts a Batch job.
3. Record the job ids, the result, and the cost in this file, as the runs below are.

Expected cost per rebuild, from the runs below, on spot `e2-standard-8` in `europe-west1`:

| Archive | Batch time | Machine time | Database read |
| --- | --- | --- | --- |
| Protected areas | about 26 min | about $0.04 | about 3.3 GB for these two together |
| Recognitions | about 12 min | about $0.02 | |
| Ecoregions | about 34 min | about $0.06 | about 180 MB |

Plus cents of disk and logging. Each rollback copy is the size of its archive, about $0.01 a month. These are estimates, not a bill. The database read leaves the database host, so check it against the plan's egress allowance.

## Cropped samples, 31 October 2026

Laptop only, Tippecanoe 2.79.0, flags above. Reading the database from the laptop dominated the Benelux wall time.

| Window | Places | Archive bytes | Wall time | Peak memory | Proof |
| --- | --- | --- | --- | --- | --- |
| Tanzania | 626 | 4,162,973 | 9.4 s | 188 MB | passed, zoom 0 to 12 |
| Germany and Benelux | 3,892 | 67,992,628 | 599 s (Tippecanoe about 65 s, the rest reading the database over the network) | 1,019 MB | passed, zoom 0 to 12 |

Largest tile in bytes (compressed, as stored):

| Zoom | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Tanzania | 7,517 | 8,648 | 14,849 | 23,921 | 41,889 | 31,998 | 32,246 | 19,080 | 8,298 | 7,091 | 4,556 | 3,013 | 3,590 |
| Germany and Benelux | 3,016 | 4,420 | 26,818 | 96,479 | 336,916 | 351,694 | **636,038** | 388,810 | 238,611 | 121,601 | 61,745 | 36,765 | 25,257 |

**Finding.** Germany and Benelux has a zoom 6 tile of 636,038 bytes, above the 500 KB mark, and zoom 4 and 5 are near 340 to 350 KB. This is with every place kept and no cap. It is what keeping all places at zoom 6 costs in dense regions. The global archive is likely to have similar or larger tiles. Reported, not reduced.

## Ecoregions, Ecoregions task 03

Cropped laptop proof, 21 November 2026, against the local Supabase stack holding the 846 ecoregions from app Ecoregions task 01: `prove --recipe cookbook/recipes/places-ecoregions.yaml --bbox=29.5,-20.5,31.5,-19.5`. 5 ecoregions, 743,340 vertices. Proof passed at every zoom 0 to 12, with all 5 present from zoom 0. Archive 6,035,246 bytes, wall time 10.6 s, peak memory 689,487,872 bytes.

| Zoom | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Largest tile, bytes | 3,657 | 6,976 | 13,053 | 24,308 | 41,434 | 50,407 | 30,251 | 33,967 | 20,732 | 18,013 | 12,780 | 6,431 | 3,847 |

**Expected cost, before the run.** Hosted, read only: 846 published ecoregions, 180 MB of stored geometry, 13,974,327 vertices, all valid. That is about 19 times the proof window, and well under the recognitions' 11,480 places.

| Item | Estimate |
| --- | --- |
| Database read from the Batch job | about 180 MB |
| Archive | about 100 to 200 MB |
| Batch run | 5 to 15 minutes (it was 33 minutes per attempt) on spot `e2-standard-8`, `europe-west1`, `maxRunDuration` 7,200 s, one retry |
| Peak memory | a few GiB, inside the 32 GiB machine |
| Machine time | about $0.02, plus cents of disk and logging |
| Storage | under $0.01 a month |

The image of 31 October is reused. The job downloads the script and the recipe from `gs://nature-watch-bucket/scratch/places/ecoregions/`, so no image is built or pushed. The object is new, so there is no `--replace` and no rollback copy. These are my estimates, not a quote.

**Submitted 21 November 2026, 18:12 UTC**, approved by the user. Job `places-places-ecoregions-20261121181209`, `europe-west1`, command `submit --recipe cookbook/recipes/places-ecoregions.yaml`, validation `none`. Output `gs://nature-watch-tiles/places/ecoregions/v1/ecoregions.pmtiles`.

Status: `gcloud batch jobs describe places-places-ecoregions-20261121181209 --location=europe-west1 --project=nature-watch-387210`. Cancel: `gcloud batch jobs delete places-places-ecoregions-20261121181209 --location=europe-west1 --project=nature-watch-387210`.

**Result: FAILED, nothing uploaded.** Both attempts (18:13 to 18:47 and 18:47 to 19:21 UTC, Batch run duration 4,063 s) built the archive, then the proof stopped them with exit 1: `zoom 0: 2 places missing after their minzoom`. Script wall time 2,002 s per attempt, peak memory 1,446,215,680 bytes. Scratch was cleaned. About 68 minutes of spot `e2-standard-8`, roughly $0.11 at my estimate.

Cause: an island ecoregion smaller than a zoom 0 tile unit (360° / 4,096, about 10 km) can quantise to nothing, and Tippecanoe drops it even with `--no-tiny-polygon-reduction`, with or without `--detect-shared-borders`. Tiling the 100 smallest ecoregions on the laptop (up to 3,089 km²) dropped the same two at zoom 0: San Félix-San Ambrosio Islands temperate forests (6.5 km², two islets) and Clipperton Island shrub and grasslands (28.8 km²). Both were present from zoom 1. Which islets drop depends on where they fall on the tile grid.

Fix, chosen by the user: the 11 ecoregions under 30 km² start at zoom 1. With that band the same 100 passed the proof: 89 at zoom 0, all 100 from zoom 1 to 12, 8.2 s, 475 MB peak.

**Resubmitted 21 November 2026, 19:28 UTC**, with the zoom 1 band. Job `places-places-ecoregions-20261121192845`. Status: `gcloud batch jobs describe places-places-ecoregions-20261121192845 --location=europe-west1 --project=nature-watch-387210`.

**Result: SUCCEEDED**, 19:29 to 20:03 UTC, no retry. Proof passed at every zoom 0 to 12. `Cache-Control: no-cache`, and the range request answered 206, in the job and again from the laptop.

| Measure | Ecoregions |
| --- | --- |
| Places | 846 |
| Present at zoom 0 | 835. The 11 under 30 km² are present from zoom 1 |
| Present from zoom 1 | all 846 |
| Bytes stored | 284,352,541 (271 MiB) |
| Batch run duration | 2,044 s |
| Script wall time | 2,037 s |
| Peak memory | 1,501,011,968 bytes (1.4 GiB) |

Largest tile in bytes (compressed, as stored):

| Zoom | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Ecoregions | 227,300 | 150,360 | 148,807 | 123,870 | 124,819 | 111,091 | 117,593 | 107,013 | 58,947 | 31,031 | 36,793 | 36,886 | 21,876 |

Every tile is under 250 KB.

**Cost.** About 34 minutes on spot `e2-standard-8`, roughly $0.06 at my estimate of $0.10 an hour, plus the failed job's 68 minutes, roughly $0.11: about $0.17 together, plus cents of disk and logging. This is an estimate, not a bill. Storage is 284 MB, under $0.01 a month. The two jobs read about 180 MB from the database three times.

## Hosted runs, submitted 12 November 2026 (Database Honing task 09)

Approved by the user. First the stale South Africa rollback copy `v1/places.previous.pmtiles` (3,592,259 bytes) was deleted, so that `--replace` keeps the global build of 31 October as the rollback copy. Both jobs use the image of 31 October, spot `e2-standard-8` in `europe-west1`, `maxRunDuration` 7,200 s, one retry, validation `none`.

| Job | Command | Output |
| --- | --- | --- |
| `places-places-protected-areas-20261112180636` | `submit --replace` | `gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles`, the 39,457 published conservation areas; rollback copy `v1/places.previous.pmtiles` |
| `places-places-recognitions-20261112180655` | `submit --recipe cookbook/recipes/places-recognitions.yaml` | `gs://nature-watch-tiles/places/recognitions/v1/recognitions.pmtiles`, the 11,480 published recognitions |

Status: `gcloud batch jobs describe <job> --location=europe-west1 --project=nature-watch-387210`. Cancel: `gcloud batch jobs delete <job> --location=europe-west1 --project=nature-watch-387210`.

**Result: both SUCCEEDED.** No retry. Proof passed at every zoom 0 to 12 for both, `Cache-Control: no-cache`, and the range request answered 206.

| Measure | Places | Recognitions |
| --- | --- | --- |
| Places | 39,457 (915 with a cropland percent) | 11,480 |
| Bytes stored | 491,711,008 (469 MiB) | 211,821,181 (202 MiB) |
| Batch run duration | 1,579 s, ended 18:34 UTC | 736 s, ended 18:20 UTC |
| Script wall time | 1,573 s | 730 s |
| Peak memory | 4,777,197,568 bytes (4.4 GiB) | 4,103,176,192 bytes (3.8 GiB) |
| Present at zoom 0 and 1, 2, 3, 4 and 5 | 2,870; 6,457; 12,176; 22,340 | 679; 1,905; 3,899; 7,208 |
| Present from zoom 6 | all 39,457 | all 11,480 |

The places archive holds exactly the published conservation areas: no recognition, and no merged parcel, which draws as the place it was merged into. The global build of 31 October (677,532,109 bytes, 51,001 places) is now `v1/places.previous.pmtiles`.

Largest tile in bytes (compressed, as stored):

| Zoom | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Places | 172,311 | 82,017 | 154,103 | 188,448 | 333,503 | 252,822 | 401,580 | 348,438 | 278,140 | 131,747 | 163,989 | 93,008 | 58,065 |
| Recognitions | 43,847 | 38,022 | 114,427 | 287,176 | 409,406 | 197,672 | 244,750 | 191,750 | 179,331 | 142,872 | 67,933 | 36,403 | 22,437 |

Taking the recognitions out brought every places tile under 500 KB. The largest was 723,745 bytes at zoom 4 on 31 October.

**Cost.** About 26 and 12 minutes on spot `e2-standard-8`. At my estimate of about $0.10 an hour that is roughly $0.07 together, plus cents of disk and logging. This is an estimate, not a bill. Storage is about 1.4 GB across the three objects, about $0.03 a month. The database read was again about 3.3 GB in all.

## Hosted run, submitted 31 October 2026

Approved and submitted. Job `places-places-protected-areas-20261031103015`, `europe-west1`, spot `e2-standard-8`, 100 GB disk, `maxRunDuration` 7,200 s, command `submit --replace`. Image built for linux/amd64 from `places/Dockerfile` and pushed to Artifact Registry repository `places` (Cloud Build was refused for this account, so it was built locally). Output `gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles`, rollback copy `v1/places.previous.pmtiles`.

Status: `gcloud batch jobs describe places-places-protected-areas-20261031103015 --location=europe-west1 --project=nature-watch-387210`. Cancel: `gcloud batch jobs delete places-places-protected-areas-20261031103015 --location=europe-west1 --project=nature-watch-387210`.

**Result: SUCCEEDED.** Running 10:31:27 to 11:08:12 UTC (Batch run duration 2,206 s). No retry.

| Measure | Value |
| --- | --- |
| Places | 51,001 (941 with a cropland percent) |
| Bytes stored | 677,532,109 (646 MiB). The rollback copy is 3,592,259 bytes |
| Wall time | 2,199 s for the script; about 37 minutes for the job |
| Peak memory | 7,925,538,816 bytes (7.4 GiB), measured in the job |
| Zoom range | 0 to 12, layer `places` |
| Tippecanoe | v2.79.0, flags above, validation `none` |
| Cache-Control | `no-cache` on the replaced object |
| Range request | 206, checked in the job and again afterwards |

Proof passed at every zoom 0 to 12. Places present: zoom 0 and 1, 3,555; zoom 2, 8,372; zoom 3, 16,091; zoom 4 and 5, 29,571; zoom 6 to 12, all 51,001.

Largest tile in bytes (compressed, as stored):

| Zoom | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Global | 211,502 | 116,688 | 260,329 | 462,129 | 723,745 | 386,580 | 636,038 | 388,810 | 288,587 | 142,801 | 163,989 | 93,008 | 58,065 |

**Findings.** Zoom 4 has a 723,745-byte tile and zoom 6 has 636,038 bytes. Both are over 500 KB, and zoom 3 is 462,129 bytes. Every place is kept, as required, so these are reported and not reduced. Peak memory was 7.4 GiB, well above the 1 GB seen in the samples, so the 32 GiB machine had room but a smaller one would need checking.

**Cost.** About 37 minutes on a spot `e2-standard-8` in `europe-west1`. At my estimate of about $0.10 an hour that is roughly $0.06, plus a few cents of disk and logging. This is an estimate, not a bill. Storage is about 1.3 GB across the new archive and the rollback copy, about $0.03 a month. The database read of about 3.3 GB left the database host; check it against the plan's egress allowance.

Roll back with the `gcloud storage cp` command above. The rollback copy stays until you say to remove it.

## Expected cost (estimate made before the run)

Not run. Estimates from the samples and from the database (read only): 51,001 places, 3,284 MB stored geometry, 214.8 million vertices. The Benelux window is 22.8 million vertices, so the global build is about 9.4 times that.

| Item | Estimate |
| --- | --- |
| NDJSON on disk | about 5.8 GB |
| Tippecanoe temporary files | up to about 30 GB |
| Archive | about 0.6 to 0.7 GB |
| Tippecanoe wall time | about 10 min on a laptop core count, so 15 to 30 min on 8 vCPUs |
| Peak memory | about 1 GB per sample; sized 32 GiB to stay safe |
| Database read from the Batch job | about 3.3 GB over the wire; much faster than from a laptop |

Proposed job: Batch, spot `e2-standard-8` (8 vCPUs, 32 GiB), `pd-balanced` boot disk 100 GB, `maxRunDuration` 7,200 s, `europe-west1`, `maxRetryCount` 1. Machine time at spot price is likely about $0.10 for an hour, disk and logging cents, and the stored archive plus the rollback copy about $0.03 a month. These are my estimates, not a quote. One cost to confirm: the database read leaves the database host, so check whether 3.3 GB falls inside the plan's egress allowance. Also building and pushing the image to Artifact Registry is a small billable step that needs approval.

## Task 02 record: South Africa, GDAL writer

South Africa proof, 29 October 2026. Local. No virtual machine.

941 places, all with a cropland percent. The Python process took 29.3 seconds and peaked at 402,341,888 bytes. The archive is 3,592,259 bytes. Zoom 0–12.

| Min zoom | Area | Places first written |
| --- | --- | --- |
| 0 | ≥ 1,000 km² | 12 |
| 2 | ≥ 300 km² | 48 |
| 3 | ≥ 100 km² | 98 |
| 4 | ≥ 30 km² | 288 |
| 6 | ≥ 10 km² | 495 |

All 941 are present by zoom 6. Cancel a local prove with Ctrl+C.

The hosted publish is one object copy from the laptop into `nature-watch-tiles`:

```bash
gcloud storage cp data/places/protected-areas/places.pmtiles \
  gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles \
  --project=nature-watch-387210
```

Copied on 29 October 2026 from the laptop. No Batch job. Ingress is free. The stored object is 3,592,259 bytes (3.43 MiB). Keeping that in `nature-watch-tiles` is under $0.01 a month. 

The object is `gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles`. Check it with `gcloud storage ls -l gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles --project=nature-watch-387210`. The copy had already finished, so there was nothing to cancel in flight. Remove it with `gcloud storage rm gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles --project=nature-watch-387210`.
