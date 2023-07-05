#!/bin/bash

# Make the temporary data directory
mkdir -p /container/data/input
mkdir -p /container/data/colored
mkdir -p /container/data/masked
mkdir -p /container/data/output

# Authenticate to Google Cloud Storage
gcloud auth activate-service-account --key-file=/tmp/keys/key.json

# Copy images from gc to local repo
gsutil cp gs://nature-watch-bucket/COGS/built/built2022/built2022_16.tif /container/data/input

# Apply a color-relief to the image
gdaldem color-relief /container/data/input/built2022_16.tif ./color.txt /container/data/colored/colored.tif

# Mask no data values
gdal_translate -a_nodata 0 /container/data/colored/colored.tif /container/data/masked/masked.tif

# Make tiles
gdal2tiles.py -z 0-10 -s EPSG:4326 -r max -w none -a 0 --xyz /container/data/masked/masked.tif /container/data/output

# Copy local tiles to gc
gsutil -m cp -r /container/data/output gs://nature-watch-bucket/tiles/built/2022

# Delete temporary data
rm -r /container/data/*