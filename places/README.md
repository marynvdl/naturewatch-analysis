# Places

How the pipelines fit together is [`docs/pipelines.md`](../docs/pipelines.md). This page is the protected-area archive.

Reads place geometries from the app database and writes one PMTiles object. The recipe is the catalog. This script reads it and does not edit it. There is no GitHub workflow for this step. Colour is applied in the client.

The archive is `gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles`. Task 05 rebuilds that same object from the global places. Raster tiles stay as they are.

## What a feature contains

The vector layer is `places`. Each feature carries:

- `id` — `places.id`
- `area_km2` — the stored place area
- `cropland_percent` — `facts.percent` for sprout `cropland-2024-v1`, method `area-weighted-spheroid-v1`, omitted when no fact exists

`places.geom` is read with the same database URL as the facts step. It is not granted to `anon`. The archive does not include names or a polygon download.

Larger places are written from a lower zoom. The bands are on the recipe: 1,000 km² from zoom 0, 300 km² from zoom 2, 100 km² from zoom 3, 30 km² from zoom 4, 10 km² from zoom 6. Every copied place is present by zoom 6. Geometry is simplified at low zoom and left at full resolution at zoom 12. Colour and line width are not in the archive. The app paints the layer when it draws the map.

## Run

```bash
pip install -r cookbook/requirements.txt -r places/requirements.txt
python -m pytest places cookbook
```

`pytest` uses a few synthetic polygons. It does not read the database and it does not upload.

The South Africa proof is local. Export `DATABASE_URL`, the same variable as the facts step. Do not commit it.

```bash
python places/protected_areas.py prove
```

That writes `data/places/protected-areas/places.pmtiles` and a `preview.html` beside it. It does not upload. Cancel with Ctrl+C.

To look at the generalization:

```bash
python places/protected_areas.py preview
```

Then open `http://127.0.0.1:8765/preview.html`. That server answers HTTP Range requests. A plain `python -m http.server` often returns the whole file instead, and PMTiles then shows a byte-serving error. The hosted object on `nature-watch-tiles` does support Range; the app will read it that way. Do not open `preview.html` as a file.

A later hosted run can use a Batch job in `europe-west1`, started by hand. That path is `python places/protected_areas.py submit`. It is not used for this first archive. Batch deletes the virtual machine when the task ends. Cancel with `gcloud batch jobs delete <job-id> --location=europe-west1 --project=nature-watch-387210`.

## Cost for this step

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

Copied on 29 October 2026 from the laptop. No Batch job. Ingress is free. The stored object is 3,592,259 bytes (3.43 MiB). Keeping that in `nature-watch-tiles` is under $0.01 a month. A later global rebuild can use `python places/protected_areas.py submit`.

The object is `gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles`. Check it with `gcloud storage ls -l gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles --project=nature-watch-387210`. The copy had already finished, so there was nothing to cancel in flight. Remove it with `gcloud storage rm gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles --project=nature-watch-387210`.
