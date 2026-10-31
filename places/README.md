# Places

How the pipelines fit together is [`docs/pipelines.md`](../docs/pipelines.md). This page is the protected-area archive.

Reads place geometries from the app database and writes one PMTiles object with Tippecanoe. The recipe is the catalog. This script reads it and does not edit it. There is no GitHub workflow for this step. Colour is applied in the client.

The archive is `gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles`. Task 05 rebuilds that same object from the global places. Raster tiles stay as they are.

## What a feature contains

The vector layer is `places`. Each feature carries:

- `id` — `places.id`
- `area_km2` — the stored place area
- `cropland_percent` — `facts.percent` for sprout `cropland-2024-v1`, method `area-weighted-spheroid-v1`, omitted when no fact exists

`places.geom` is read with the same database URL as the facts step. It is not granted to `anon`. The archive does not include names or a polygon download.

Larger places are written from a lower zoom. The bands are on the recipe: 1,000 km² from zoom 0, 300 km² from zoom 2, 100 km² from zoom 3, 30 km² from zoom 4, 10 km² from zoom 6. Every copied place is present by zoom 6. Geometry is simplified at low zoom and left at full resolution at zoom 12. Colour and line width are not in the archive. The app paints the layer when it draws the map.

## Tile engine

Tippecanoe 2.79.0 from [felt/tippecanoe](https://github.com/felt/tippecanoe) (`tippecanoe --version` prints `v2.79.0`). The Debian package is older and cannot write PMTiles. `places/Dockerfile` builds that tag on `ghcr.io/osgeo/gdal:ubuntu-small-3.11.4` for the Batch job. The image is `europe-west1-docker.pkg.dev/nature-watch-387210/places/places-tippecanoe:2.79.0`. It is not built or pushed yet; that waits for approval. Tile encoding is Mapbox Vector Tile inside PMTiles. GDAL and OGR read, validate, and prove.

Flags, from `TIPPECANOE_FLAGS` and the recipe:

```
-l places -Z 0 -z 12 --simplification=1 -y id -y area_km2 -y cropland_percent
-T id:string -T area_km2:float -T cropland_percent:float
--no-feature-limit --no-tile-size-limit --no-tiny-polygon-reduction --read-parallel --force
```

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
