# Seed

Downloads the GLAD annual cropland tiles named in a cookbook recipe, checks them, and writes a mainland South Africa clip under the recipe's seed path.

The recipe is the catalog. This script reads it and does not edit it. Open one tile with `sample` and record its source codes in the recipe before `run` downloads the rest.

## Checks

Each tile must match the recipe:

- geographic CRS, WGS84 (`EPSG:4326`)
- pixel size `0.00025°`
- the cropland value `100` appears in the set of tiles
- no pixel value other than the codes recorded in the recipe
- the source file's nodata flag matches the recipe (unset for this year)

The clip keeps those source codes inside the boundary. Pixels outside mainland South Africa are the clip nodata value, `255`. Parts of the boundary that miss the tiles, including the Prince Edward Islands, are left out. One pixel at 28°E, 26.2°S must match the source tile, so the clip stays on the same grid.

## Run

```bash
pip install -r cookbook/requirements.txt
python seed/cropland.py sample
python seed/cropland.py run
python -m pytest seed cookbook
```

`run` fetches every tile in the recipe, writes the clip, and uploads both to `seed.path`. GDAL's Python bindings come with the GDAL install. No virtual machine is started, and there is no GitHub workflow for this step.

Tiles are saved under `data/seed/cropland-2024/`, which git ignores. A download resumes in 2 MB ranges because the GLAD server closes long connections.

## cropland 2024

`20S_020E.tif` was opened on 22 October 2026 before the other tiles. Every pixel is `0` or `100`. The file sets no nodata value. `0` is everything that is not cropland, and `100` is cropland. The recipe records that. The clip is `gs://nature-watch-bucket/seeds/cropland/glad/2024/v1/cropland-glad-2024-zaf.tif`.

The boundary is geoBoundaries ZAF ADM0 for 2020 (OCHA ROSEA and the South African Municipal Demarcation Board), pinned to geoBoundaries commit `9469f09`.

## Cost for this step

Run on 22 October 2026 from a laptop. No virtual machine was started.

The six tiles and the clip are in `gs://nature-watch-bucket/seeds/cropland/glad/2024/v1/`. New objects are 150,779,903 bytes. The prefix total is 150,779,952 bytes, which includes the 49-byte `.keep` marker from the cookbook step. The clip is 38,478,988 bytes. The sprout prefix was not written.

Downloading the tiles took 568 seconds, including two attempts that the GLAD server reset before the ranged download. `python seed/cropland.py run` then took 847 seconds to check the six files, clip them, and upload. Peak memory of that process was 1,253,572,608 bytes.

All six tiles are `EPSG:4326`, `0.00025°`, uint8, with no nodata flag. Every pixel is `0` or `100`, and `100` appears in each tile. The clip origin is `(16.45175, -22.125)`. Inside the boundary it has 1,680,109,285 pixels of `0` and 130,854,661 pixels of `100`. The boundary kept for the clip has 11 parts that meet the tiles, including the small islands along the Cape coast, and one interior ring. The Prince Edward Islands fall outside the tiles and are not in the clip.
