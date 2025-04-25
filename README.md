# naturewatch-analysis
Shell scripts using gdal and docker to create raster tiles to be served in the naturewatch-app.

For each type (i.e. fire, built, bare) there is a task folder inside `tasks`. There's also a corresponding `yml` file in `.github/workflows` which triggers a GitHub Action when changes are made to either the yml file or when a file inside the `tasks` folder is changed.

## Creating tiles for a new year
Using the example of fire for 2023:
1. Create the fire tiff images using Google Earth Engine script named `fire_exportFirms.ipynb` to write into `gs://nature-watch-bucket/COGS/fire/2023/*`
2. In this repo, update the year in `createFireTiles/createFireTiles.sh` (there are two places to update)
3. Update the tag number in `createFireTiles/deploy.sh` i.e. create-fire-tiles:1.1.1 (there are two places to update)
4. When you commit changes to GitHub, a GitHub Action workflow is triggered, that starts a VM in Google Cloud, starts up a Docker, runs the code and finally create the tiles in `gs://nature-watch-tiles/fire/2023`
5. There will now be a Compute Engine created on Google Cloud. You can install the ops agent for better statistics on available storage spcae. You can also ssh into the machine and run this `sudo docker logs -f create-fire-tiles`
