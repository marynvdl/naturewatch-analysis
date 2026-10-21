# naturewatch-analysis
Shell scripts using gdal and docker to create raster tiles to be served in the naturewatch-app.

The live task is fire, in `tasks/fireTiles`. It runs on a GitHub-hosted runner, started by hand from the Actions tab (see below). There is no virtual machine.

Built, bare, and crop used to start a Compute Engine virtual machine on push. Those workflows are retired, and the task folders are in `archive/vm-tile-tasks/`.

## Creating fire tiles for a new year
Using the example of fire for 2025:
1. Create the fire tiff images with Google Earth Engine (`fire_exportFirms.ipynb`) so that they land in `gs://nature-watch-bucket/COGS/fire/2025/*`
2. In GitHub go to **Actions -> Fire tiles -> Run workflow**, enter the year, and leave **dry run** off. Tick **dry run** to build and check the tiles without uploading them.
3. The workflow downloads the COGs, builds the tiles with GDAL in a pinned container, and uploads them to `gs://nature-watch-tiles/fire/2025/`. It takes a few minutes.
4. Add the year to the fire layer in the naturewatch-app (`frontend/naturewatch/src/catalog/layers.json`).

Pull requests that change `tasks/fireTiles/` or the workflow do a dry run automatically.

### How the fire tiles are built
`tasks/fireTiles/build_tiles.sh <cog_dir> <work_dir> [max_zoom]` does the work and can be run on a laptop with GDAL installed. It makes zoom 0-9 only.

The fire counts are aggregated to a 1 km grid in the GEE export, so the 30 m COGs hold far more pixels than information. The script resamples to that 1 km grid (GDAL reads the COG overviews), colours it with `color.txt`, and cuts tiles. Zoom 9 is about 300 m per pixel, which is already finer than the data. The app sets `maxzoom: 9` and `resampling: nearest` for the fire layer in its layer catalog, so it enlarges the last level itself. This makes about 25,000 tiles per year instead of about 1.6 million.

Needed once: the `GCP_SA_KEY` secret must belong to a service account that can read `nature-watch-bucket` and write to `nature-watch-tiles`.
