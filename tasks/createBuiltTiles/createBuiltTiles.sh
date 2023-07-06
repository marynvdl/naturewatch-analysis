#!/bin/bash

# Make the temporary data directories
mkdir -p /container/data/input
mkdir -p /container/data/merged
mkdir -p /container/data/colored
mkdir -p /container/data/masked
mkdir -p /container/data/output

# Authenticate to Google Cloud Storage
echo "1. Authenticating GC"
gcloud auth activate-service-account --key-file=/tmp/keys/key.json

# Copy all images from gc to local repo
echo "2. Copying from GC"
gsutil -m cp gs://nature-watch-bucket/COGS/built/built2022/* /container/data/input

# Merge all images into a single image and delete input
echo "3. Merging into single image"
gdal_merge.py -o /container/data/merged/merged.tif /container/data/input/*.tif
rm -r /container/data/input/*

# Apply a color-relief to the merged image and delete merged
echo "4. Applying color relief"
gdaldem color-relief /container/data/merged/merged.tif ./color.txt /container/data/colored/colored.tif
rm -r /container/data/merged/*

# Mask no data values and delete colored
echo "5. Mask no data"
gdal_translate -a_nodata 0 /container/data/colored/colored.tif /container/data/masked/masked.tif
rm -r /container/data/colored/*

# Make tiles and delete masked
echo "6. Make tiles"
gdal2tiles.py -z 0-10 -s EPSG:4326 -r max -w none -a 0 --xyz /container/data/masked/masked.tif /container/data/output
rm -r /container/data/masked/*

# Copy local tiles to gc
echo "7. Upload to GC"
gsutil -m cp -r /container/data/output gs://nature-watch-bucket/tiles/built/2022

# Delete temporary data
rm -r /container/data/*
