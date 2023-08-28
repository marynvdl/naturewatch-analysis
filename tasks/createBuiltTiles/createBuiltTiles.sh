#!/bin/bash

# Make the temporary data directories
mkdir -p /container/data/input
mkdir -p /container/data/output

# Authenticate to Google Cloud Storage
echo "1. Authenticating GC"
gcloud auth activate-service-account --key-file=/tmp/keys/key.json

# Copy all images from gc to local repo
echo "2. Copying from GC"
gsutil -m cp gs://nature-watch-bucket/COGS/built/built2022/*  /container/data/input

# Build a virtual dataset from all input images
echo "3. Building virtual dataset"
gdalbuildvrt /container/data/merged.vrt /container/data/input/*.tif

# Mask no data values
echo "4. Mask no data"
gdal_translate -a_nodata 0 /container/data/merged.vrt /container/data/masked.vrt -of VRT

# Apply a color-relief to the merged image
echo "5. Applying color relief"
gdaldem color-relief /container/data/masked.vrt ./color.txt /container/data/colored.tif -co "COMPRESS=DEFLATE"

# Make tiles
echo "6. Make tiles"
gdal2tiles.py -z 0-6 -s EPSG:4326 -r bilinear -w none -a 0 --xyz /container/data/colored.tif /container/data/output

# Copy local tiles to gc
echo "7. Upload to GC"
gsutil -m cp -r /container/data/output/* gs://nature-watch-tiles/built/2022_temp

# Delete temporary data
rm -r /container/data/*





