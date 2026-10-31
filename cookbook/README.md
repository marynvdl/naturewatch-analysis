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

The sprout is one COG per seed tile under `gs://nature-watch-bucket/sprouts/cropland/2024/v2/tiles/`, with a VRT over them at `gs://nature-watch-bucket/sprouts/cropland/2024/v2/cropland-2024.vrt`. `sprout.layout` is `per-tile`, `sprout.path` is the VRT, and `sprout.tiles_path` is the folder. Its id is `cropland-2024-v2`. The tile COGs carry full-resolution classes and no overviews. Source 100 maps to 1 and source 0 maps to 0. The display object is `gs://nature-watch-tiles/cropland/2024/v2/cropland.pmtiles`, zooms 0–12, scheme `pmtiles`, encoding class-value. Resampling is unset.

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
