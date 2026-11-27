# Cookbook

How the pipelines fit together is [`docs/pipelines.md`](../docs/pipelines.md). Recipes for Sooty Earth layers and place archives. `recipes/` is the only catalog edited by hand. Later steps read a recipe. They do not grow a second inventory.

Fire exports stay at `gs://nature-watch-bucket/COGS/fire/`. The 2015 and 2019 crop tiles stay at `gs://nature-watch-tiles/crop/`. New layers use the prefixes below.

## Paths

`gs://nature-watch-bucket` is private. Public access prevention is enforced. Map tiles stay on the public bucket `gs://nature-watch-tiles`.

| Role | Pattern |
| --- | --- |
| Seed | `gs://nature-watch-bucket/seeds/<product>/<source>/<year>/v<n>/` |
| Sprout | `gs://nature-watch-bucket/sprouts/<product>/<year>/v<n>/<product>-<year>-<extent>.tif` |
| Tiles | `gs://nature-watch-tiles/<product>/<year>/v<n>/{z}/{x}/{y}.png` |
| Places | `gs://nature-watch-tiles/places/<type>/v<n>/places.pmtiles` |

The sprout object is the published raster. Facts count its class pixels. Tiles are built from it and written to `nature-watch-tiles`. A recipe can name one PMTiles archive instead of the `{z}/{x}/{y}.png` pattern. The static STAC files for a version go in that same `v<n>/` prefix and point at the sprout. There is no separate published copy.

Ids:

- Seed: `seed-<product>-<source>-<year>-v<n>`, for example `seed-cropland-glad-2024-v1`
- Sprout: `<product>-<year>-v<n>`, for example `cropland-2024-v1`
- Recipe file and recipe id: the sprout id, for example `recipes/cropland-2024-v2.yaml` with id `cropland-2024-v2`

A version is a folder, so the next version can sit beside this one. A new raster recipe uses the sprout id as its file name and its id. A new source year is a new sprout id, such as `cropland-2025-v1`. `recipes/cropland-2024.yaml` is the version 1 South Africa record and keeps that name. While two versions of a year are both read, both files stay in `recipes/`. When a version is no longer read, move its recipe out of `recipes/`. A place archive keeps its own file name, such as `places-protected-areas.yaml`.

## Adding a recipe

Write the paths and the sprout classes when the recipe is added. The sprout classes are the published values: for cropland, 1 is cropland, 0 is other land, and 255 is nodata.

Read one sample before the bulk download. The sample is the smallest piece the publisher distributes, such as one tile. Record the source codes from that sample. The seed step then checks the remaining files against the recipe. Where the publisher’s page and the sample disagree, the recipe keeps the sample.

## cropland-2024

`recipes/cropland-2024.yaml` names the GLAD annual cropland 2024 seed and the South Africa sprout. The seed tiles are the six 10° files whose cells meet mainland South Africa. A `.keep` object in each prefix marks the folder. It is not a raster. The seed step copies the tile names in the recipe, and the clip named beside them, into the seed prefix.

`sprout/cropland.py` writes the sprout file from the seed clip. Its classes are 1 for cropland, 0 for other land, and 255 for nodata, including land outside South Africa. `tiles/cropland.py` reads that sprout and writes class-value tiles for zooms 0–12. The tile prefix is on the recipe. Seed value 100 is cropland and maps to sprout value 1. Seed value 0 is everything else and maps to sprout value 0. Clip value 255 is nodata and stays 255. The source tiles set no nodata value. `seed/cropland.py` downloads the tiles, checks them, and writes a mainland South Africa clip under the seed path. Pixels outside that boundary are 255 in the clip. That 255 is not a GLAD code.

## cropland-2024-v2

`recipes/cropland-2024-v2.yaml` names the global GLAD annual cropland 2024 seed and sprout. Its id is `cropland-2024-v2`, the same as its sprout id. `cropland-2024.yaml` stays the version 1 record. The seed path is `gs://nature-watch-bucket/seeds/cropland/glad/2024/v2/`. The seed tiles are the 223 files on the publisher’s 2024 index, read on 30 October 2026. There is no country clip. A 10° tile that index does not list is absent. Inside a listed tile, 0 stays other land.

The sprout is one COG per seed tile under `gs://nature-watch-bucket/sprouts/cropland/2024/v2/tiles/`, with a VRT over them at `gs://nature-watch-bucket/sprouts/cropland/2024/v2/cropland-2024.vrt`. `sprout.layout` is `per-tile`, `sprout.path` is the VRT, and `sprout.tiles_path` is the folder. Its id is `cropland-2024-v2`. The tile COGs carry full-resolution classes and no overviews. Source 100 maps to 1 and source 0 maps to 0. The display object is `gs://nature-watch-tiles/cropland/2024/v2/cropland.pmtiles`, zooms 0–12, scheme `pmtiles`, encoding class-value. Resampling is `q3`.

## cropland-2024-v3

`recipes/cropland-2024-v3.yaml` names the version 3 tiles of the same seed and sprout. Its id is `cropland-2024-v3`. The display object is `gs://nature-watch-tiles/cropland/2024/v3/cropland.pmtiles`: 512 px palette tiles, zooms 0–11, resampling `share`, 8 shades on a √share curve, solid from zoom 11. The live app still reads v2 until the app task points at v3.

`20S_020E.tif` was opened on 22 October 2026, before the other South Africa tiles. Every pixel is 0 or 100, and the file sets no nodata value. That file is on the 2024 index. The publisher’s page names only value 100. The recipe keeps the sample.

## places-protected-areas

`recipes/places-protected-areas.yaml` names the protected-area PMTiles archive. It is not a raster year. `places/protected_areas.py` reads `places.geom` and the stored cropland percent from the app database and writes `gs://nature-watch-tiles/places/protected-areas/v1/places.pmtiles`. The map paints the layer in the client. A later place type adds another recipe and another object path.

## Checks

```bash
python -m pytest cookbook
```

Dependencies are listed in `requirements.txt`.

## Cost for this step

Two prefix markers in `nature-watch-bucket`, 49 bytes each, 98 bytes stored in total. No raster was downloaded or written. No virtual machine was started. Wall time and peak memory do not apply to a catalog step. Adding the global recipe stored no further objects. No transfer was started and no Batch job was submitted.

## Expected seed storage for cropland 2024 global

The version 1 prefix is 150,779,952 bytes, which includes the 49-byte `.keep` marker. The clip is 38,478,988 bytes. The six 10° tiles are 112,300,915 bytes. For 223 tiles that is 112,300,915 × 223 / 6, or 4,173,850,674 bytes and 1/6 byte. The recorded expectation before the transfer was 4,173,850,674 bytes. Compressed sizes on the publisher’s index are not equal, so this scaled the version 1 total. It was not a measured sum of the 223 files.

The transfer on 30 October 2026 stored 5,289,882,209 bytes. The run record is [`seed/README.md`](../seed/README.md).

The sprout and the PMTiles archive are later steps. They are not in this figure.

## buildings-2023-v1

`recipes/buildings-2023-v1.yaml` names the Buildings 2023 seeds, sprout and tiles: Google Open Buildings 2.5D Temporal 2023 where Google has data, otherwise Microsoft TEMPO 2023 Q4. The measurements behind the Google value, the TEMPO seed rule and the grid are in [`buildings/README.md`](../buildings/README.md).

### Expected cost, written 17 November 2026 before any transfer, export or job

**Google: Earth Engine, mean presence. Chosen.** For each sprout cell, Earth Engine averages `building_presence` over the 2023 images at the sprout grid. It reads its own mean pyramid, so no 0.5 m pixel is processed. One export task per 10° block writes a COG to the Google seed path. 145 blocks of 10° meet a Google 2023 tile.

- Interactive profiles of six whole boxes (25 and 36 km²) took 0.092–0.130 EECU-seconds each, 0.0030–0.0042 per km². Over the 75.3 million km² of Google tile footprints (union on a 0.05° grid, including nodata) that is about 60–90 EECU-hours.
- Project `nature-watch-387210` is `REGISTERED_NOT_COMMERCIALLY`. Noncommercial use is not billed. The export uses its monthly EECU quota.
- Output: one byte per cell at 0.00025°, about 97 billion cells, mostly 0 or nodata. Expected 5–20 GB compressed, under $0.50 a month in `nature-watch-bucket`.

**Routes not chosen**, all for the same sprout grid:

| Route | Value per cell | Estimate | Built km² vs 0.5 m threshold |
| --- | --- | --- | --- |
| Earth Engine, threshold 4 m pixels at 0.45, then average | share of 4 m pixels at or above 0.45 | 1,800–3,200 EECU-hours | −5 to −8% |
| Earth Engine, threshold 2 m pixels, then average | share of 2 m pixels | 7,500–13,500 EECU-hours | −1 to −2% |
| Earth Engine, threshold 0.5 m pixels, then average | share of 0.5 m pixels | 100,000–171,000 EECU-hours | reference |
| Batch on Google's public COGs, 0.5 m | share of 0.5 m pixels | 2,600–6,100 spot vCPU-hours, about $40–120 | reference |
| Batch, 1 m overview | share of 1 m pixels | about half the 0.5 m Batch route | −2% |

The Batch route reads 627,480 tiles of 25,000 × 25,000 px in 151 manifests. A sample of 40 of the 14,081 tile folders (1,752 tiles) averaged 29.3 MB per tile for three bands, so about 18.4 TB for 2023 and about 6 TB for band 3. The bucket is `US` multi-region and not requester pays, so reading it costs this project nothing. Decoding and thresholding one full tile took 16 CPU-seconds on the laptop.

**TEMPO 2023 Q4.** 822,199 tiles of 256 × 256 px float32 in the index `https://opendata.aiforgood.ai/building-density/tile_index.gpkg` (384 MB, read 16 November 2026). HEAD on a random sample of 490 tiles gave a mean of 13,317 bytes (median 4,981, largest 208,961), so about 11 GB for all of them. Stored one by one, the 822,199 objects would cost about $8 in write operations. Task 02 instead fetches them in one Batch job and stores band 1 as zoom 4 mosaics. Its expected cost is in [`seed/README.md`](../seed/README.md).

The sprout and the PMTiles archive are later steps. They are not in these figures.

## roads-2026-10-v1

`recipes/roads-2026-10-v1.yaml` names the Roads seed, sprout and tiles from Overture release `2026-10-21.0`. The level rules, the burn rule, the tile encoding and the sample figures are in [`roads/README.md`](../roads/README.md).

### Expected cost, written 17 November 2026 before any job

Scaled from the task 01 sample. The global count comes from the Parquet footers (`roads/seed.py footers`): 353,327,860 transportation segments in 128 files, 71,962,027,313 compressed bytes, with rows in 272 blocks of 10°. A read of the `subtype` and `class` columns of 4 random files (10,949,955 rows) found 82.4% in the kept road classes, so about 291 million seed rows. Overture's bucket is AWS Open Data and not requester pays, and Google Cloud does not charge for data coming in, so reading it costs this project nothing. Storage is priced at $0.023 per GB a month, as above.

| Step | Work | Machine and time | Compute | Stored | Storage a month |
| --- | --- | --- | --- | --- | --- |
| Seed (task 02) | DuckDB reads the release once, at most 72 GB, and writes GeoParquet split by 10° block | one spot `e2-standard-8`, `europe-west1`, 1–2 hours | about $0.10–0.30 | about 37 GB (129 bytes a row, as in the dense window) | about $0.86 |
| Sprout (task 02) | burn 272 blocks of 10° (40,000 × 40,000 cells each) and write COGs, a VRT and STAC | 8 spot tasks, 4–6 task-hours in all, 16 GB memory a block or 4 strips a block | about $0.15–0.40 | 2.5–3.5 GB | about $0.06–0.08 |
| Tiles (task 03) | top zoom 11 rendered from the sprout VRT, lower zooms reduced, sharded as cropland version 3 | spot `e2-standard-2` shards, 4–12 task-hours, plus merge and verify | about $0.30–0.80 | 1.5–4 GB | about $0.03–0.09 |

How the ranges were made:

- **Road length.** The dense window averages 157 m a segment. At 150–250 m a segment, 291 million segments are 44–73 million km of road.
- **Sprout.** The dense window has 42.2 road cells per km and stores 0.78 bytes per road cell, so 1.8–3.1 billion road cells and 1.4–2.4 GB. Empty land adds about 37 KB per square degree (the sparse window), about 1 GB over the 272 blocks. One block's array is 1.6 GB, and the decode holds about 8 GB, so a block runs on a 16 GB machine or in 4 strips. Writing scales from 1.6 s per 64 million cells.
- **Tiles.** The dense window stores 0.73 bytes per road cell at top zoom 11, the sparse one 2.5. Land with roads is taken as 100 million km², and a 512 px zoom 11 tile covers about 230 km² at a typical latitude, so about 435,000 top-zoom tiles and 580,000 in all. Cropland version 3 did 31 top-zoom tiles a second per shard. Roads warps twice per tile, so 10–30.
- Spot prices are about $0.03–0.04 an hour for `e2-standard-2` and four times that for `e2-standard-8`. The submit step checks the current price.

One-off compute is about $0.5–1.5. The seed is most of the storage. Overture does not keep every release in its public bucket, so the seed is what keeps this snapshot rebuildable. The user chose on 17 November 2026 to keep the whole seed, `id` included.

## power-lines-2026-10-v1 and power-sites-2026-10-v1

`recipes/power-lines-2026-10-v1.yaml` and `recipes/power-sites-2026-10-v1.yaml` name the Energy layer's seed, sprouts and tiles from Overture release `2026-10-21.0`. They share one seed of every `base/infrastructure` subtype. The rules, the Global Renewables Watch comparison and the sample figures are in [`energy/README.md`](../energy/README.md).

### Expected cost, written 27 November 2026 before any job

Scaled from the task 01 sample. The footers (`energy/seed.py footers`) give 158,979,260 infrastructure rows in 16 files and 4,608 row groups, 13,888,744,176 compressed bytes. A footer has no subtype counts, and the rows are not sorted by subtype, so the share that is power comes from the windows: 16% of rows in Gironde and 86% in the Karoo, with 4.8% and 20% in the kept classes. Reading Overture costs this project nothing, as for roads. Storage is $0.023 per GB a month.

| Step | Work | Machine and time | Compute | Stored | Storage a month |
| --- | --- | --- | --- | --- | --- |
| Seed (task 02) | DuckDB reads the theme once, 13.9 GB, and writes GeoParquet split by 10° block, as roads | spot `e2-standard-4`, 1–2 task-hours | about $0.05–0.20 | about 13 GB (the window seeds are 94% of the raw read) | about $0.30 |
| Sprouts (task 02) | burn `power-lines` and `power-sites` per 10° block, with VRTs and STAC | spot `e2-highmem-2`, 5–15 task-hours | about $0.20–0.60 | 0.3–1.3 GB | under $0.03 |
| Power-lines tiles (task 03) | the roads shard and merge flow, top zoom 11 | spot `e2-standard-2` shards, 5–12 task-hours, plus a merge | about $0.25–0.60 | 0.5–1.5 GB | about $0.01–0.03 |
| Power-sites tiles (task 03) | write the kept sites from the seed, then one Tippecanoe run | one spot `e2-highmem-4`, 1–3 hours | about $0.05–0.20 | 0.5–2.5 GB | about $0.01–0.06 |

How the ranges were made:

- **Seed.** The roads seed read 72 GB and 353 million rows in about 3 task-hours with start-up. This theme is a fifth of the bytes and less than half the rows.
- **Kept power features.** 159 million rows at 5–20% kept gives 8–32 million lines and sites. Roads had 307 million.
- **Sprouts.** A block's time is mostly its 40,000 × 40,000-cell array, not its features. About 250–350 blocks have power features. At 30–90 s a block for each sprout, that is 5–15 task-hours. The windows store 1.1 bytes a line cell and 1.2–3.2 bytes a site cell.
- **Power-lines tiles.** Roads took 16.6 task-hours for 4.0 billion road cells. Lines have far fewer cells, but they cross many tiles thinly, and a tile's cost is mostly its warps, so the time is taken as a third to two thirds of the roads run. The windows store 1.8 bytes a line cell in the archive.
- **Power-sites tiles.** 1–4 million sites at 330–580 bytes each in the windows' archives.

One-off compute is about $0.55–1.6. The seed is most of the storage, about $0.35–0.40 a month in all. A column read of `subtype` and `class` from a few files in the task 02 pilot would narrow the kept share before the full run.
