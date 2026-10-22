# Facts

How the pipelines fit together is [`docs/pipelines.md`](../docs/pipelines.md). This page is the cropland 2024 run.

Counts the sprout raster inside each pilot place and writes one row to `facts`. The recipe is the catalog. This script reads it and does not edit it. The published run is a Google Batch job in `europe-west1`, started by hand. There is no orchestrator in this release, and no GitHub workflow for this step. Batch deletes the virtual machine when the task ends.

The hosted run is recorded below. It stored 941 rows.

## Overlap

Count each place on its own. Ground shared by two places stays in both rows. A total across places has to union the boundaries first. This step does not write a total.

## What is stored

The sprout is `gs://nature-watch-bucket/sprouts/cropland/2024/v1/cropland-2024-zaf.tif`. The job reads its `version` and `class_values` metadata and stops if they differ from the recipe.

| Column | Value |
| --- | --- |
| `layer` | `cropland-2024` |
| `layer_version` | `v1` |
| `method_id` | `area-weighted-spheroid-v1` |
| `area_km2` | Cropland inside the place, in km² |
| `percent` | `100 × area_km2 / valid km²`, or `0` when the valid area is `0` |
| `valid_coverage` | `valid km² / places.area_km2` |

Valid square kilometres are the same pixel sum for values `0` and `1`. Value `255` and pixels outside the raster are left out. They are not a stored column. `valid_coverage × places.area_km2` returns them.

A pixel contributes the fraction of its area inside the place, multiplied by the WGS84 spheroidal area of the whole pixel. That is the same spheroid as `places.area_km2`. The raster is not resampled. The count uses the full-resolution pixels.

The three numbers are stored at full precision. Shown square kilometres use two decimal places, which is one hectare. Percent uses one decimal place. Valid coverage uses three decimal places.

`fact_methods.summary` carries this formula. The app migration is `supabase/migrations/20261022190000_fact_methods.sql` in `naturewatch-app`. It is applied on the hosted database.

## Independent check

Before any row is written, the job checks three places and stops if a check fails.

- The two smallest pilot places between 10 and 50 km² are counted again by polygon intersection on the same grid. Cropland and valid area must agree within 0.01 km².
- The largest pilot place is counted again by pixel centre. Cropland and valid area must agree within 1 percent.

Run on 22 October 2026. The stored figure is the area-weighted count. The other figure is the check.

| Place | Check | Stored km² | Other km² |
| --- | --- | --- | --- |
| Sardaville Game and Holiday Farm | intersection, cropland | 0.648454 | 0.648454 |
| Sardaville Game and Holiday Farm | intersection, valid | 10.010497 | 10.010497 |
| Biddulphsberg Oord Private Nature Reserve | intersection, cropland | 1.793736 | 1.793736 |
| Biddulphsberg Oord Private Nature Reserve | intersection, valid | 10.045730 | 10.045730 |
| Kruger National Park | pixel centre, cropland | 11.455618 | 11.452442 |
| Kruger National Park | pixel centre, valid | 19162.348919 | 19162.401447 |

## Run

The laptop needs the GDAL Python bindings. From the repository root, with the virtualenv that already has them:

```bash
.venv/bin/pip install -r cookbook/requirements.txt -r facts/requirements.txt
.venv/bin/python -m pytest facts
```

`pytest` uses a few synthetic pixels. It does not read the South Africa raster and it does not write to the database.

The pilot run is the Batch job. Export `DATABASE_URL` for the command below, the same variable as the place import. Do not commit it. This creates secret `naturewatch-facts-database-url` in `europe-west1`, grants the Batch service account access, and submits one spot `e2-standard-4`. The database URL is not stored in the job spec.

```bash
python facts/cropland.py submit
```

## Cost for this step

Run on 22 October 2026 as Batch job `facts-cropland-2024-v1-20261022180517` in `europe-west1`. Spot `e2-standard-4`. The first attempt was preempted after a few minutes, before it wrote any rows. Batch retried once. That virtual machine was removed when the task ended. No virtual machine was left running.

941 places. The successful Python process took 432 seconds and peaked at 1,479,888,896 bytes. Counting the polygons took 168 seconds. The five largest places took 37.5 percent of that counting time, so they did not dominate it. Kruger National Park was the slowest of them, at 19 seconds. The Batch task, including the preempted attempt, the image start, and the package install, ran for 872 seconds.

Compute for both attempts is a few cents at the spot price. Reading the 39 MB raster in Europe and writing 941 rows is a fraction of a cent. The scratch copies were deleted. The database URL is in secret `naturewatch-facts-database-url`.
