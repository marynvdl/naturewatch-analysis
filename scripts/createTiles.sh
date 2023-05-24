gsutil cp gs://nature-watch-bucket/COGS/built/built2022/built2022.tif ./data/input

gdaldem color-relief ./data/input/built2022.tif ./scripts/color.txt ./data/colored/colored.tif

gdal_translate -a_nodata 0 ./data/colored/colored.tif ./data/masked/masked.tif

gdal2tiles.py -z 0-10 -s EPSG:4326 -r max -w none -a 0 --xyz ./data/masked/masked.tif ./data/output

gsutil -m cp -r ./data/output gs://nature-watch-bucket/tiles/built/2022