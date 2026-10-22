# Cookbook

Recipes for Sooty Earth layers. `recipes/` is the only catalog edited by hand. Later steps read a recipe. They do not grow a second inventory.

Fire exports stay at `gs://nature-watch-bucket/COGS/fire/`. The 2015 and 2019 crop tiles stay at `gs://nature-watch-tiles/crop/`. New layers use the prefixes below.

## Paths

`gs://nature-watch-bucket` is private. Public access prevention is enforced. Map tiles stay on the public bucket `gs://nature-watch-tiles`.

| Role | Pattern |
| --- | --- |
| Seed | `gs://nature-watch-bucket/seeds/<product>/<source>/<year>/v<n>/` |
| Sprout | `gs://nature-watch-bucket/sprouts/<product>/<year>/v<n>/<product>-<year>-<extent>.tif` |

The sprout object is the published raster. Facts count its class pixels. Tiles are built from it and written to `nature-watch-tiles`. The static STAC files for a version go in that same `v<n>/` prefix and point at the sprout. There is no separate published copy.

Ids:

- Seed: `seed-<product>-<source>-<year>-v<n>`, for example `seed-cropland-glad-2024-v1`
- Sprout: `sprout-<product>-<year>-v<n>`, for example `sprout-cropland-2024-v1`

A version is a folder, so the next version can sit beside this one. Recipes stay as a flat list of files until a product has a second year.

## Adding a recipe

Write the paths and the sprout classes when the recipe is added. The sprout classes are the published values: for cropland, 1 is cropland, 0 is other land, and 255 is nodata.

Read one sample before the bulk download. The sample is the smallest piece the publisher distributes, such as one tile. Record the source codes from that sample. The seed step then checks the remaining files against the recipe. Where the publisher’s page and the sample disagree, the recipe keeps the sample.

## cropland-2024

`recipes/cropland-2024.yaml` names the GLAD annual cropland 2024 seed and the South Africa sprout. The seed tiles are the six 10° files whose cells meet mainland South Africa. A `.keep` object in each prefix marks the folder. It is not a raster. The seed step copies the tile names in the recipe, and the clip named beside them, into the seed prefix.

The sprout file is named now and written in a later step. Its classes are 1 for cropland, 0 for other land, and 255 for nodata, including land outside South Africa. Seed value 100 is cropland and maps to sprout value 1. Seed value 0 is everything else and maps to sprout value 0. The source tiles set no nodata value. `seed/cropland.py` downloads the tiles, checks them, and writes a mainland South Africa clip under the seed path. Pixels outside that boundary are 255 in the clip. That 255 is not a GLAD code.

## Checks

```bash
python -m pytest cookbook
```

Dependencies are listed in `requirements.txt`.

## Cost for this step

Two prefix markers in `nature-watch-bucket`, 49 bytes each, 98 bytes stored in total. No raster was downloaded or written. No virtual machine was started. Wall time and peak memory do not apply to a catalog step.
