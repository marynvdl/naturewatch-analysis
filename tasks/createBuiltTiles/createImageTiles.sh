#!/bin/bash

# Make the temporary data directory
mkdir -p ./data/input
mkdir -p ./data/colored
mkdir -p ./data/masked
mkdir -p ./data/output

# Copy images from gc to local repo
gsutil cp gs://nature-watch-bucket/COGS/built/built2022/built2022_16.tif ./data/input

# Apply a color-relief to the image
gdaldem color-relief ./data/input/built2022_16.tif ./color.txt ./data/colored/colored.tif

# Mask no data values
gdal_translate -a_nodata 0 ./data/colored/colored.tif ./data/masked/masked.tif

# Make tiles
gdal2tiles.py -z 0-10 -s EPSG:4326 -r max -w none -a 0 --xyz ./data/masked/masked.tif ./data/output

# Copy local tiles to gc
gsutil -m cp -r ./data/output gs://nature-watch-bucket/tiles/built/2022

# Delete temporary data
rm -r ./data/*