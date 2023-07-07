#!/bin/bash

# Make the temporary data directories
mkdir -p /container/data/input
mkdir -p /container/data/output

# Authenticate to Google Cloud Storage
echo "1. Authenticating GC"
gcloud auth activate-service-account --key-file=/tmp/keys/key.json

# Copy all images from gc to local repo
echo "2. Copying from GC"
gsutil -m cp gs://nature-watch-bucket/COGS/built/built2022/* /container/data/input

# Build a virtual dataset from all input images
echo "3. Building virtual dataset"
gdalbuildvrt /container/data/merged.vrt /container/data/input/*.tif

# Apply a color-relief to the merged image
echo "4. Applying color relief"
gdaldem color-relief /container/data/merged.vrt ./color.txt /container/data/colored.vrt -of VRT

# Mask no data values
echo "5. Mask no data"
gdal_translate -a_nodata 0 /container/data/colored.vrt /container/data/masked.vrt -of VRT

# Make tiles
echo "6. Make tiles"
gdal2tiles.py -z 0-10 -s EPSG:4326 -r max -w none -a 0 --xyz /container/data/masked.vrt /container/data/output

# Copy local tiles to gc
echo "7. Upload to GC"
gsutil -m cp -r /container/data/output gs://nature-watch-bucket/tiles/built/2022

# Delete temporary data
rm -r /container/data/*
